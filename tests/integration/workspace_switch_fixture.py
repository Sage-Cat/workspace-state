"""Native workspace off/on lifecycle, run by the private GNOME harness."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import subprocess
import time
import uuid


CALCULATOR = 'org.gnome.Calculator'
LAUNCHER = ['/usr/bin/gtk-launch', CALCULATOR]
SESSION_STATE = ['systemctl', '--user', 'show', '-p', 'ActiveState', '-p', 'Job', 'graphical-session.target']
WORKSPACES = ['Integration One', 'Integration Two', 'Integration Three']
TARGET = WORKSPACES[1]
APP = 'fixture-calculator'


def _require_private(root: Path) -> None:
    expected = {'HOME': root / 'home', 'TMUX_TMPDIR': root / 'tmp', 'XDG_RUNTIME_DIR': root / 'runtime',
                'XDG_CONFIG_HOME': root / 'config', 'XDG_DATA_HOME': root / 'data'}
    if (any(os.environ.get(key) != str(path) for key, path in expected.items())
            or str(root / 'runtime') not in os.environ.get('DBUS_SESSION_BUS_ADDRESS', '')
            or os.environ.get('DBUS_SYSTEM_BUS_ADDRESS') != os.environ.get('DBUS_SESSION_BUS_ADDRESS')
            or not (root / 'runtime/wsctl-integration').is_socket()):
        raise RuntimeError('workspace-switch fixture requires the live private GNOME session')


def _launch_calculator(run_command, root: Path, *, env=None):
    # gtk-launch may leave its output descriptors open in the application.
    # Use a file so waiting for the launcher does not wait for window closure.
    output = root / 'calculator.log'
    with output.open('a') as log:
        result = run_command(LAUNCHER, stdout=log, stderr=subprocess.STDOUT, timeout=5, env=env)
    return subprocess.CompletedProcess(LAUNCHER, result.returncode, '', output.read_text() if result.returncode else '')


def bridge_systemd() -> None:
    """Adapt the graphical-service launch and session readiness in the private bus."""
    root = Path(os.environ['XDG_RUNTIME_DIR']).parent
    _require_private(root)
    run_command = subprocess.run

    def run(arguments, *args, **kwargs):
        if arguments == SESSION_STATE:
            # Corroborate that GNOME is currently serving the fixture bus before
            # translating its readiness to the systemd properties wsctl expects.
            native = run_command(['gnome-winctl', 'status', '--json'], capture_output=True, text=True, timeout=3)
            state = json.loads(native.stdout) if native.returncode == 0 else {}
            if state.get('build', {}).get('revision') == 'headless-integration' and state.get('available') is True:
                (root / 'session-active').touch()
                return subprocess.CompletedProcess(arguments, 0, 'ActiveState=active\nJob=\n', '')
            return subprocess.CompletedProcess(arguments, 1, '', 'private GNOME is unavailable')
        if arguments[:1] == ['/usr/bin/systemd-run']:
            split = arguments.index('--') if '--' in arguments else len(arguments)
            cmd = arguments[split + 1:]
            # wsctl finds the new window by the launch receipt in its process
            # environment, for terminals and applications alike.
            env = os.environ.copy()
            for arg in arguments[:split]:
                if arg.startswith('--setenv='):
                    match = re.fullmatch(r'--setenv=WSCTL_WORKSPACE_LAUNCH=([0-9a-f]{32})', arg)
                    if not match:
                        raise RuntimeError('unexpected fixture service environment')
                    env['WSCTL_WORKSPACE_LAUNCH'] = match[1]
            if (cmd[:2] == ['/usr/bin/alacritty', '--title'] and
                    cmd[3:] == ['-o', 'window.dynamic_title=true', '-e', 'tmux',
                                'attach-session', '-t', '=integration:']):
                with (root / 'terminal.log').open('a') as log:
                    subprocess.Popen(cmd, stdout=log, stderr=log, env=env)
                return subprocess.CompletedProcess(arguments, 0, '', '')
            if cmd != LAUNCHER:
                raise RuntimeError('fixture cannot start other graphical services')
            with (root / 'launches').open('a') as log:
                log.write(CALCULATOR + '\n')
            return _launch_calculator(run_command, root, env=env)
        return run_command(arguments, *args, **kwargs)

    subprocess.run = run


def run(root: Path, output: Path, wsctl_source: Path, results: list[dict]) -> None:
    _require_private(root)
    config = root / 'config/workspace-state'
    config.mkdir()
    (config / 'desktop-apps.toml').write_text(f'''[[apps]]
id = "{APP}"
label = "Fixture Calculator"
aliases = ["{CALCULATOR}", "gnome-calculator"]
desktop_ids = ["{CALCULATOR}"]
executables = ["workspace-switch-fixture-calculator"]
''')
    desktop_files = root / 'data/applications'
    desktop_files.mkdir()
    (desktop_files / f'{CALCULATOR}.desktop').write_text('''[Desktop Entry]
Type=Application
Name=Fixture Calculator
Exec=/usr/bin/gnome-calculator
DBusActivatable=false
''')
    python_hook = root / 'python'
    python_hook.mkdir()
    (python_hook / 'sitecustomize.py').write_text(
        'from workspace_switch_fixture import bridge_systemd\nbridge_systemd()\n')
    wsctl_env = dict(os.environ, PYTHONPATH=f'{python_hook}:{Path(__file__).parent}')
    generation = root / 'runtime/workspace-state/login-generation'
    generation.parent.mkdir(exist_ok=True)
    generation.write_text(uuid.uuid4().hex + '\n')

    def record(case, **detail):
        results.append({'case': case, 'passed': True, **detail})

    def command(arguments, *, env=None, code=0, timeout=12):
        process = subprocess.run(arguments, env=env, capture_output=True, text=True, timeout=timeout)
        with (output / 'workspace-commands.log').open('a') as log:
            log.write(f'{arguments}\n{process.stdout}{process.stderr}\n')
        if process.returncode != code:
            raise RuntimeError(f'{arguments} exited {process.returncode}: {process.stdout}{process.stderr}')
        return process.stdout

    def wsctl(*arguments, timeout=20, startup_context=None):
        env = wsctl_env if startup_context is None else {**wsctl_env, 'WSCTL_OPERATION_CONTEXT': startup_context}
        return command([str(wsctl_source / 'bin/wsctl'), *arguments], env=env, timeout=timeout)

    def winctl(*arguments, code=0):
        return json.loads(command(['gnome-winctl', *arguments, '--json'], code=code))

    def window(selected):
        found = [item for item in winctl('state')['windows'] if selected(item)]
        assert len(found) <= 1, found
        return found[0] if found else None

    def calculator():
        return window(lambda item: CALCULATOR.lower() in item['app_ids'])

    def neighbor():
        return window(lambda item: item['title'] == 'Workspace integration fixture')

    def live_projection(item):
        return {key: item[key] for key in ('id', 'pid', 'workspace', 'monitor', 'state', 'geometry')}

    def report(target_state):
        rows = json.loads(wsctl('workspace', 'list', '--json'))['workspaces']
        assert [item['name'] for item in rows] == WORKSPACES, rows
        assert [item['state'] for item in rows] == ['on', target_state, 'on'], rows

    def wait_for_window(get_window):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if result := get_window():
                return result
            time.sleep(.1)
        raise RuntimeError('fixture application window did not appear')

    tmux_socket = root / 'tmp' / f'tmux-{os.getuid()}/default'
    deadline = time.monotonic() + 5
    while not tmux_socket.is_socket() and time.monotonic() < deadline:
        time.sleep(.1)
    assert tmux_socket.is_socket(), 'the private tmux server did not start'
    command(['tmux', 'new-session', '-d', '-s', 'integration', 'sleep 180'])
    tmux_session = command(['tmux', 'list-sessions', '-F', '#{session_id} #{session_name}'])

    launch = _launch_calculator(subprocess.run, root)
    if launch.returncode:
        raise RuntimeError(f'fixture calculator launcher failed: {launch.stderr}')
    initial = wait_for_window(calculator)
    untouched = live_projection(wait_for_window(neighbor))
    assert untouched['workspace'] == 0, untouched
    for index in (0, 1):
        result = winctl('place', str(initial['id']), '--target-json', json.dumps({
            'workspace': index, 'monitor': 1, 'state': 'maximized', 'stage_before_handoff': True,
        }), '--wait', '--timeout', '9')
        assert result['status'] == 'verified', result
    original = live_projection(calculator())
    assert original['workspace'] == 1 and original['monitor'] == 1, original
    report('on')
    # Chrome is deliberately absent from the fixture. Verify the target's
    # successful capture separately from other desktop provider coverage.
    wsctl('save', '--allow-partial')
    checkpoint = json.loads(wsctl('show', '--json'))
    assert checkpoint['category_provenance']['social-apps']['state'] == 'captured'
    saved = checkpoint['social_apps'][APP]['windows']
    assert len(saved) == 1 and saved[0]['workspace_name'] == TARGET and saved[0]['monitor'] == 1, saved
    record('workspace-switch-saves-native-configured-app', original=original, neighboring_window=untouched)

    epoch = winctl('state')['enable_epoch']
    close = ['close', str(original['id']), '--workspace', '1', '--pid', str(original['pid'])]
    assert winctl(*close, '--epoch', 'stale-fixture-epoch', code=1)['reason'] == 'stale_epoch'
    assert winctl(*close, '--epoch', epoch, '--workspace-name', 'Renamed Fixture', code=1)['reason'] == 'workspace_name_changed'
    assert live_projection(calculator()) == original
    record('workspace-switch-native-refuses-stale-epoch-and-mismatched-name')

    wsctl('workspace', 'off', '--', TARGET)
    report('off')
    assert calculator() is None and live_projection(neighbor()) == untouched
    [archive] = (root / 'data/workspace-state/workspaces').glob('*.json')
    assert archive.stat().st_mode & 0o777 == 0o600
    assert archive.parent.stat().st_mode & 0o777 == 0o700
    preserved = archive.read_bytes()
    parked = json.loads(preserved)
    assert parked['phase'] == 'off' and parked['snapshot']['social_apps'][APP]['windows'][0]['_native_id'] == original['id'], parked
    assert json.loads(wsctl('show', '--json'))['social_apps'][APP]['windows'] == saved
    assert winctl(*close, '--epoch', epoch)['status'] == 'absent'
    record('workspace-switch-off-closes-exact-window-and-archives-recipe')

    # New worker generation equivalent to the login identity inputs. It
    # starts with no category marker while the ordinary checkpoint still
    # contains the recipe for the now absent calculator window.
    startup_generation = uuid.uuid4().hex
    generation.write_text(startup_generation + '\n')
    initializer = '''
import json
import sys
from workspace_state.login_status import initialize
from workspace_state.operations import current

if not initialize(sys.argv[1], show_startup_hud=False):
    raise SystemExit('could not initialize the private startup owner')
print(json.dumps(current().to_dict()))
'''
    startup_context = command(['python3', '-c', initializer, startup_generation],
                              env=dict(os.environ, PYTHONPATH=str(wsctl_source / 'src'))).strip()
    startup = wsctl('startup', 'social-apps', '--wait', '8', timeout=20, startup_context=startup_context)
    assert 'Startup has no saved social-apps items.' in startup, startup
    assert not (root / 'launches').exists() and calculator() is None
    report('off')
    record('workspace-switch-new-startup-process-suppresses-archived-slot')

    autosave = wsctl('save', '--auto')
    assert 'checkpoint saved to' in autosave and (root / 'session-active').exists(), autosave
    periodic = json.loads(wsctl('show', '--json'))
    assert periodic['category_provenance']['social-apps']['source'] == 'auto-save'
    assert archive.read_bytes() == preserved and calculator() is None
    wsctl('workspace', 'off', '--', TARGET)
    assert archive.read_bytes() == preserved
    record('workspace-switch-auto-checkpoint-preserves-independent-archive')

    wsctl('workspace', 'on', '--', TARGET, timeout=45)
    report('on')
    restored = live_projection(calculator())
    assert restored['id'] != original['id'], restored
    assert all(restored[key] == original[key] for key in ('workspace', 'monitor', 'state')), (original, restored)
    assert all(abs(restored['geometry'][key] - original['geometry'][key]) <= 3
               for key in original['geometry']), (original, restored)
    assert live_projection(neighbor()) == untouched and winctl('state')['active_workspace'] == 0
    assert command(['tmux', 'list-sessions', '-F', '#{session_id} #{session_name}']) == tmux_session
    assert (root / 'launches').read_text().splitlines() == [CALCULATOR]
    assert json.loads(archive.read_text())['phase'] == 'on'
    resumed = json.loads(wsctl('show', '--json'))['social_apps'][APP]['windows']
    assert len(resumed) == 1 and resumed[0]['workspace_name'] == TARGET and resumed[0]['monitor'] == 1, resumed
    wsctl('workspace', 'on', '--', TARGET)
    assert live_projection(calculator()) == restored and (root / 'launches').read_text().splitlines() == [CALCULATOR]
    record('workspace-switch-on-restores-native-placement-without-touching-neighbor', restored=restored)


    def terminal():
        return window(lambda item: 'alacritty' in item['app_ids'])

    # Detach an actual terminal and prove the same pane process survives.
    with (root / 'terminal.log').open('a') as log:
        subprocess.Popen(['/usr/bin/alacritty', '--title', 'Fixture terminal',
                          '-o', 'window.dynamic_title=false', '-e', 'tmux',
                          'attach-session', '-t', '=integration:'], stdout=log, stderr=log)
    client = wait_for_window(terminal)
    for slot in (0, 2):
        target = {'workspace': slot, 'monitor': 1, 'state': 'maximized', 'stage_before_handoff': True}
        assert winctl('place', str(client['id']), '--target-json', json.dumps(target),
                      '--wait', '--timeout', '9')['status'] == 'verified'
    before = live_projection(terminal())
    panes = ['tmux', 'list-panes', '-t', '=integration:', '-F', '#{pane_id} #{pane_pid}']
    process_identity = command(panes)
    wsctl('workspace', 'off', '--', WORKSPACES[2])
    assert terminal() is None and command(panes) == process_identity
    wsctl('workspace', 'on', '--', WORKSPACES[2], timeout=45)
    after = live_projection(terminal())
    assert after['id'] != before['id'] and command(panes) == process_identity
    assert all(before[key] == after[key] for key in ('workspace', 'monitor', 'geometry', 'state'))
    wsctl('workspace', 'on', '--', WORKSPACES[2])
    assert live_projection(terminal()) == after and live_projection(calculator()) == restored
    assert live_projection(neighbor()) == untouched
    record('workspace-switch-terminal-reattaches-the-original-tmux-pane', original=before, restored=after)
