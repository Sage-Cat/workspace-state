#!/usr/bin/env python3
"""Opt-in real GNOME HUD modal-grab tests on a private bus and headless display."""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from run_headless import stop

WINCTL_UUID = 'gnome-winctl-v3@sagecat.local'
HUD_UUID = 'login-hud-headless-test@sagecat.local'
REVISION = 'headless-hud-integration'


def inside(root: Path, output: Path) -> int:
    if (Path(os.environ['XDG_RUNTIME_DIR']) != root / 'runtime'
            or str(root / 'runtime') not in os.environ.get('DBUS_SESSION_BUS_ADDRESS', '')):
        raise RuntimeError('private runtime and private bus are required')
    results = []
    processes = []
    began = time.monotonic()

    def control(action='state', **values):
        reply = subprocess.run(['gdbus', 'call', '--session', '--dest', 'org.sagecat.GnomeWinCtl1',
                                '--object-path', '/org/sagecat/GnomeWinCtl1',
                                '--method', 'org.sagecat.GnomeWinCtl1.HudTestControl',
                                json.dumps({'action': action, **values})],
                               capture_output=True, text=True, timeout=2)
        if reply.returncode:
            raise RuntimeError(reply.stderr.strip())
        return json.loads(ast.literal_eval(reply.stdout)[0])

    def running_build():
        reply = subprocess.run(['gdbus', 'call', '--session', '--dest', 'org.sagecat.GnomeWinCtl1',
                                '--object-path', '/org/sagecat/LoginHud',
                                '--method', 'org.sagecat.LoginHud.GetState'],
                               capture_output=True, text=True, timeout=2, check=True)
        return json.loads(ast.literal_eval(reply.stdout)[0])['build']

    def until(predicate, timeout=6):
        deadline = min(began + 85, time.monotonic() + timeout)
        last = None
        while time.monotonic() < deadline:
            try:
                last = control()
                if predicate(last):
                    return last
            except RuntimeError as error:
                last = str(error)
            time.sleep(.08)
        raise RuntimeError(f'HUD condition timed out: {last}')

    def record(case, state, **extra):
        assert state['confirmAttempts'] == 0 and not state['nativeHandoff'], state
        results.append({'case': case, 'passed': True, 'state': state, **extra})
        (output / 'results.json').write_text(json.dumps({'isolated': True, 'running': True, 'cases': results}, indent=2))

    def released(state):
        return state['modalCount'] == baseline and not state['modal'] and not state['reactive']

    try:
        with (output / 'gnome-shell.log').open('w') as log:
            shell = subprocess.Popen(['gnome-shell', '--headless', '--wayland', '--no-x11', '--sm-disable',
                                      '--wayland-display=wsctl-hud-integration', '--virtual-monitor=1280x720'],
                                     stdout=log, stderr=subprocess.STDOUT)
            processes.append(shell)
            until(lambda state: state['ready'], timeout=30)
            control('setup')
            until(lambda state: not state['overviewVisible'] and state['modalCount'] == 0)
            control('setup')  # Capture the baseline after overview animation releases its grab.
            initial = until(lambda state: not state['modal'] and state['baseline'] is not None)
            baseline = initial['baseline']
            assert isinstance(baseline, int), initial
            assert initial['modalCount'] == baseline and not initial['visible'], initial
            build = running_build()
            assert build['uuid'] == HUD_UUID and build['revision'] == REVISION, build
            record('passive-enable-restores-no-modal-grab', initial, build=build)

            # Push GNOME's actual unlock-dialog session mode in this private
            # Shell. The temporary extension copy supports the mode and the
            # native confirmation remains fixture-guarded, so no logind action
            # can escape the disposable session.
            control('publish', operation='7' * 32, state='running')
            visible = until(lambda state: state['operation'] == '7' * 32 and state['visible'] and state['modal'])
            assert visible['modalCount'] == baseline + 1, visible
            control('enter-lock-mode')
            locked_mode = until(lambda state: state['sessionMode'] == 'unlock-dialog' and state['locked'] and
                                state['actorPresent'] and state['confirmHookInstalled'] and
                                not state['visible'] and not state['modal'])
            assert locked_mode['modalCount'] == baseline, locked_mode
            locked = control('locked-confirm')
            locked = until(lambda state: state['confirmSettled'])
            assert locked['sessionMode'] == 'unlock-dialog' and locked['locked'], locked
            assert locked['confirmError'] is None, locked
            assert locked['confirmHookInstalled'] and not locked['visible'] and not locked['modal'] and not locked['reactive'], locked
            assert locked['modalCount'] == baseline and not locked['countdown'] and not locked['commit'], locked
            assert locked['nativeCancelCalls'] == 1 and locked['confirmAttempts'] == 0, locked
            assert locked['nativeHandoff'] is None and locked['preflightWrites'] == 0, locked
            record('locked-native-confirm-cancels-without-hidden-hud-or-preflight', locked,
                   session_modes=['user', 'unlock-dialog'])
            control('leave-lock-mode')
            unlocked = until(lambda state: state['sessionMode'] == 'user' and not state['locked'] and
                             state['actorPresent'] and state['confirmHookInstalled'] and
                             state['operation'] == '7' * 32 and state['visible'] and state['modal'])
            assert unlocked['modalCount'] == baseline + 1, unlocked
            cancelled = control('cancel', failWrite=True)
            assert released(cancelled) and cancelled['visible'] and not cancelled['countdown'], cancelled

            first = control('publish', operation='1' * 32, state='running')
            grabbed = until(lambda state: state['operation'] == '1' * 32 and state['modal'] and state['mapped'])
            assert grabbed['seatAll'] and grabbed['modalCount'] == baseline + 1, grabbed
            record('shutdown-hud-acquires-real-seat-grab', grabbed)
            cancelled = control('cancel', failWrite=True)
            assert released(cancelled) and cancelled['elapsedMs'] < 250, cancelled
            assert cancelled['localCancelled'] and cancelled['visible'] and not cancelled['cancelReturned'], cancelled
            record('cancel-write-failure-releases-seat-synchronously', cancelled)
            control('publish', operation='1' * 32, state='ready', **first_context(first))
            stale = until(lambda state: state['operation'] == '1' * 32 and state['localCancelled'])
            time.sleep(.5)
            stale = control()
            assert released(stale) and stale['visible'] and not stale['countdown'] and not stale['commit'], stale
            record('late-ready-cannot-rearm-cancelled-operation', stale)

            second = control('publish', operation='2' * 32, state='running')
            until(lambda state: state['operation'] == '2' * 32 and state['modal'])
            cancelled = control('cancel', failWrite=False)
            assert released(cancelled) and cancelled['elapsedMs'] < 250, cancelled
            assert cancelled['cancelReturned'] and cancelled['cancellationWritten'] and cancelled['visible'], cancelled
            time.sleep(.6)  # No backend acknowledges or changes the cancellation.
            stalled = control()
            assert released(stalled) and stalled['visible'] and stalled['localCancelled'], stalled
            record('stalled-cancellation-keeps-recovery-visible-without-grab', stalled)
            control('publish', operation='2' * 32, state='ready', **first_context(second))
            time.sleep(.5)
            stale = control()
            assert released(stale) and not stale['countdown'] and not stale['commit'], stale
            record('late-ready-after-unacknowledged-cancel-remains-withdrawn', stale)

            third = control('publish', operation='3' * 32, state='ready', stageCount=7)
            committed = until(lambda state: state['operation'] == '3' * 32 and bool(state['commit']), timeout=10)
            assert committed['modal'] and committed['preparedPolling'], committed
            assert not (root / 'runtime/workspace-state/shutdown-prepared.json').exists()
            time.sleep(.5)
            silent = control()
            assert silent['preparedPolling'] and silent['confirmAttempts'] == 0, silent
            record('backend-silence-after-countdown-does-not-authorize-handoff', silent)
            cancelled = control('cancel', failWrite=True)
            assert released(cancelled) and not cancelled['preparedPolling'] and cancelled['visible'], cancelled
            record('cancel-during-stalled-final-authorization-releases-seat', cancelled)
            control('publish', operation='3' * 32, state='ready', stageCount=7,
                    cancelled=True, **first_context(third))
            terminal = until(lambda state: state['titleText'] == 'System shutdown cancelled')
            assert released(terminal) and not terminal['commit'] and not terminal['countdown'], terminal
            assert 'verifying final safety marker' not in terminal['overallText'], terminal
            record('cancelled-completion-clears-stale-committed-progress', terminal)

            dismissed = control('dismiss')
            assert released(dismissed) and not dismissed['visible'], dismissed
            record('completed-cancellation-close-hides-old-operation', dismissed)
            control('publish', operation='8' * 32, state='ready', stageCount=4)
            retry = until(lambda state: state['operation'] == '8' * 32 and bool(state['commit']), timeout=10)
            assert len(retry['stages']) == 4 and all(stage['state'] == 'ready' for stage in retry['stages']), retry
            assert retry['rendered'] == '8' * 32 and retry['commit'] == '8' * 32, retry
            assert retry['panelMapped'] and retry['panelWidth'] > 0 and retry['panelHeight'] > 0, retry
            assert retry['modal'] and not retry['localCancelled'], retry
            record('fresh-complete-retry-after-cancel-close-paints-and-commits-fewer-rows', retry)
            control('cancel', failWrite=False)
            control('publish', operation='9' * 32, state='ready', stages=[
                {'id': 'workspace-save', 'state': 'ready', 'message': 'Saved checkpoint reused'},
                {'id': 'social-apps-save', 'state': 'pending', 'message': 'No result published'},
            ])
            incomplete = until(lambda state: state['operation'] == '9' * 32 and state['mapped'])
            time.sleep(.6)
            incomplete = control()
            assert not incomplete['rendered'] and not incomplete['renderScheduled'], incomplete
            assert not incomplete['commit'] and not incomplete['countdown'], incomplete
            assert incomplete['stages'][1]['state'] == 'pending', incomplete
            record('overall-ready-with-unpublished-category-refuses-render-ack-and-commit', incomplete)
            control('cancel', failWrite=False)

            for operation, recovery in [('5' * 32, False), ('6' * 32, True)]:
                failure = control('publish', operation=operation, state='failed',
                                  recoveryRunning=recovery)
                shown = until(lambda state: state['operation'] == operation and state['visible'] and state['mapped'])
                if not recovery:
                    control('publish', operation=operation, state='failed', **first_context(failure))
                    shown = until(lambda state: state['operation'] == operation and state['localCancelled'] and
                                  'Review the reported error' in state['noticeText'])
                    assert 'recovery remains pending' not in shown['noticeText'], shown
                record('failed-report-visible-during-recovery' if recovery else 'failed-report-visible', shown)
                dismissed = control('dismiss')
                assert released(dismissed) and not dismissed['visible'], dismissed
                assert dismissed['localCancelled'] and dismissed['elapsedMs'] < 250, dismissed
                record('failed-report-dismissal-preserves-recovery-without-grab' if recovery
                       else 'failed-report-dismissal-hides-and-releases-seat', dismissed)
                control('publish', operation=operation, state='ready', **first_context(failure))
                time.sleep(.6)
                delayed = control()
                assert released(delayed) and not delayed['visible'], delayed
                assert delayed['localCancelled'] and not delayed['countdown'] and not delayed['commit'], delayed
                record('late-ready-after-failed-report-dismissal-cannot-authorize-handoff', delayed,
                       recovery_was_running=recovery)

            control('publish', operation='4' * 32, state='running')
            until(lambda state: state['operation'] == '4' * 32 and state['modal'])
            disabled = control('disable-pending')
            assert disabled['modalCount'] == baseline and not disabled['modal'] and not disabled['actorPresent'], disabled
            record('disable-with-pending-gio-load-releases-real-grab', disabled)
            control('enable')
            enabled = until(lambda state: state['actorPresent'] and state['operation'] == '4' * 32 and state['localCancelled'])
            time.sleep(.5)
            enabled = control()
            assert released(enabled) and enabled['currentEpochOwnsOld'] is False, enabled
            record('reenable-rejects-old-epoch-and-retains-cancel-fence', enabled)
            (output / 'final-hud.json').write_text(json.dumps(enabled, indent=2))
    except Exception as error:
        results.append({'case': 'integration-error', 'passed': False, 'error': f'{type(error).__name__}: {error}'})
        return_code = 1
    else:
        return_code = 0
    finally:
        for process in reversed(processes):
            stop(process)
        (output / 'results.json').write_text(json.dumps({'isolated': True, 'elapsed_seconds': time.monotonic() - began,
                                                        'cases': results}, indent=2))
    return return_code


def first_context(publication):
    return {key: publication[key] for key in ('context', 'startedAt')}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--inside', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inside:
        return inside(args.inside, args.output)
    if not args.run:
        parser.error('--run is required; native integration is opt-in')
    for command in ('gnome-shell', 'dbus-run-session', 'gdbus', 'glib-compile-schemas'):
        if not shutil.which(command):
            raise RuntimeError(f'missing prerequisite: {command}')
    outer_deadline = time.monotonic() + 95
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.chmod(0o700)
    (output / 'results.json').write_text(json.dumps({'isolated':True, 'running':True, 'cases':[]}))
    (output / 'final-hud.json').unlink(missing_ok=True)
    workspace = Path(__file__).resolve().parents[3]
    with tempfile.TemporaryDirectory(prefix='wsctl-hud-headless-', dir='/tmp') as temporary:
        root = Path(temporary)
        for name in ('runtime', 'config', 'data', 'cache', 'state', 'schemas'):
            (root / name).mkdir(mode=0o700)
        winctl = root / 'data/gnome-shell/extensions' / WINCTL_UUID
        shutil.copytree(workspace / 'gnome-winctl/extension' / WINCTL_UUID, winctl)
        shutil.copyfile(Path(__file__).with_name('hud_fixture.js'), winctl / 'hudFixture.js')
        extension = winctl / 'extension.js'
        source = extension.read_text()
        assert source.count('<method name="GetCapabilities">') == 1
        assert source.count('    GetCapabilities() {') == 1
        source = "import {hudControl} from './hudFixture.js';\n" + source
        source = source.replace('<method name="GetCapabilities">', '<method name="HudTestControl"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>\n    <method name="GetCapabilities">')
        source = source.replace('    GetCapabilities() {', '    HudTestControl(value) { return hudControl.call(this, value); }\n\n    GetCapabilities() {')
        extension.write_text(source)
        hud = root / 'data/gnome-shell/extensions' / HUD_UUID
        hud.mkdir()
        for name in ('extension.js', 'stylesheet.css', 'metadata.json', 'buildInfo.js'):
            shutil.copyfile(workspace / 'login-hud' / name, hud / name)
        metadata = json.loads((hud / 'metadata.json').read_text())
        session_modes = metadata.get('session-modes')
        if session_modes != ['user', 'unlock-dialog']:
            raise RuntimeError(f'Login HUD must declare lock-screen session support, got {session_modes!r}')
        metadata.update(uuid=HUD_UUID, name='Disposable HUD integration fixture')
        (hud / 'metadata.json').write_text(json.dumps(metadata))
        for installed in (winctl, hud):
            (installed / 'buildInfo.js').write_text(f"export const BUILD_REVISION = '{REVISION}';\n")
        for schema in Path('/usr/share/glib-2.0/schemas').iterdir():
            if schema.name.endswith(('.xml', '.override')):
                (root / 'schemas' / schema.name).symlink_to(schema)
        (root / 'schemas/zz-wsctl-hud-integration.gschema.override').write_text(f'''[org.gnome.shell]
enabled-extensions=['{WINCTL_UUID}', '{HUD_UUID}']
disable-user-extensions=false
[org.gnome.mutter]
dynamic-workspaces=false
[org.gnome.desktop.wm.preferences]
num-workspaces=1
''')
        subprocess.run(['glib-compile-schemas', '--strict', str(root / 'schemas')], check=True, timeout=10)
        bus = root / 'session-bus.conf'
        bus.write_text(f'''<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN" "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig><type>session</type><listen>unix:tmpdir={root / 'runtime'}</listen><auth>EXTERNAL</auth>
<policy context="default"><allow send_destination="*"/><allow receive_sender="*"/><allow own="*"/></policy></busconfig>''')
        env = dict(os.environ, XDG_RUNTIME_DIR=str(root / 'runtime'), XDG_CONFIG_HOME=str(root / 'config'),
                   XDG_DATA_HOME=str(root / 'data'), XDG_CACHE_HOME=str(root / 'cache'), XDG_STATE_HOME=str(root / 'state'),
                   XDG_CONFIG_DIRS=str(root / 'config'), XDG_DATA_DIRS='/usr/local/share:/usr/share',
                   WAYLAND_DISPLAY='wsctl-hud-integration', DISPLAY='', GDK_BACKEND='wayland',
                   GSETTINGS_BACKEND='memory', GSETTINGS_SCHEMA_DIR=str(root / 'schemas'),
                   GIO_USE_VFS='local', GVFS_DISABLE_FUSE='1', NO_AT_BRIDGE='1', GTK_A11Y='none',
                   IBUS_ADDRESS='unix:path=' + str(root / 'runtime/no-ibus'), GTK_IM_MODULE='gtk-im-context-simple',
                   LIBGL_ALWAYS_SOFTWARE='1', GALLIUM_DRIVER='llvmpipe', GNOME_SHELL_SESSION_MODE='gnome',
                   XDG_SESSION_TYPE='wayland', XDG_CURRENT_DESKTOP='GNOME')
        for key in ('DBUS_SESSION_BUS_ADDRESS', 'GNOME_SETUP_DISPLAY', 'SESSION_MANAGER', 'DESKTOP_AUTOSTART_ID', 'WSCTL_OPERATION_CONTEXT'):
            env.pop(key, None)
        with (output / 'driver.log').open('w') as log:
            process = subprocess.Popen(['dbus-run-session', '--config-file', str(bus), '--', sys.executable,
                                        str(Path(__file__).resolve()), '--inside', str(root), '--output', str(output)],
                                       env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                return_code = process.wait(timeout=max(1, outer_deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                return_code = 124
                (output / 'results.json').write_text(json.dumps({'isolated':True, 'cases':[
                    {'case':'outer-deadline', 'passed':False, 'error':'private HUD integration exceeded 95 seconds'}]}, indent=2))
            finally:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
        return return_code


if __name__ == '__main__':
    raise SystemExit(main())
