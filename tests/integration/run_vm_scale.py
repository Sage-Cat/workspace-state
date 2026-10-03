#!/usr/bin/python3
"""Opt-in full-scale desktop checkpoint in a disposable Ubuntu KVM guest.

Run inside the explicitly named guest, never on a user's desktop. All content is
synthetic. Applications/companions are real; social apps optionally use labeled
GTK proxies, and the 23 conversation processes are local stand-ins. No credentials or
host profiles are copied. See README.md for the guarded phases and evidence.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime
import hashlib
import json
import os
import re
from pathlib import Path
import select
import signal
import shlex
import shutil
import socket
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
ROOT = Path.home() / '.local/state/wsctl-scale'
EXPECTED = {'terminals': 6, 'tmux_sessions': 10, 'conversations': 23,
            'chrome_windows': 7, 'chrome_groups': 3, 'chrome_tabs': 42, 'nemo_windows': 4,
            'vscode_windows': 1, 'social_windows': 4, 'monitors': 3, 'viewer_windows': 1,
            'native_windows': 23}


def run(*args, timeout=30, check=True):
    result = subprocess.run(list(map(str, args)), text=True, capture_output=True,
                            timeout=timeout, check=check)
    return result.stdout.strip()


def write(name, value):
    destination = ROOT / (name + '.json')
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(destination)


def real_social():
    path = ROOT / 'social-mode.json'
    return path.exists() and json.loads(path.read_text()).get('real') is True


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def process_identity(pid):
    try:
        # Linux stat field 22; the parenthesized comm can contain spaces.
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': int(pid), 'start_time': int(fields[19])}
    except (OSError, ValueError, IndexError):
        return None


def wayland_login():
    found = []
    for entry in run('loginctl', 'list-sessions', '--no-legend').splitlines():
        identifier = entry.split()[0]
        properties = run('loginctl', 'show-session', identifier, '-p', 'Name', '-p', 'Type')
        if 'Name=tester' in properties and 'Type=wayland' in properties:
            found.append(identifier)
    if len(found) != 1:
        raise RuntimeError('Cannot uniquely identify the disposable Wayland login')
    return found[0]


def baseline_digests():
    from workspace_state.storage import path_for
    return {'fixture': digest(ROOT / 'checkpoint.json'), 'canonical': digest(path_for()),
            'viewer': digest(ROOT / 'viewer-placement.json')}


def archive_cycle():
    before = json.loads((ROOT / 'cycle-before.json').read_text())
    destination = ROOT / 'cycles' / before['cycle_id']
    destination.mkdir(parents=True, exist_ok=True)
    for name in ('cycle-before', 'cycle-started', 'checkpoint', 'baseline-digests', 'viewer-placement',
                 'counts', 'coverage', 'verified-live-checkpoint', 'verified-desktop', 'coordinator-status',
                 'chrome-login-original', 'chrome-login-final', 'chrome-launch', 'chrome-observation-launch',
                 'chrome-cache-provisioning', 'chrome-last-companion-build', 'running-companions',
                 'semantic-verification', 'result', 'failure-verify'):
        source = ROOT / (name + '.json')
        if source.exists():
            shutil.copy2(source, destination / source.name)


def wait_for(function, *, timeout=45):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            result = function()
            if result:
                return result
        except (OSError, RuntimeError, ValueError) as error:
            last = str(error)
        time.sleep(.4)
    raise RuntimeError(f'Bounded readiness expired: {last}')


def launch(command, label):
    with (ROOT / (label + '.log')).open('a') as stream:
        return subprocess.Popen(list(map(str, command)), stdout=stream,
                                stderr=subprocess.STDOUT, start_new_session=True)


def shell():
    from workspace_state.desktop import capture_shell
    return capture_shell()


def configure():
    ROOT.mkdir(parents=True, mode=0o700, exist_ok=True)
    write('consent', {'hostname': socket.gethostname(), 'synthetic_only': True})
    for command in ('alacritty', 'tmux', 'google-chrome', 'code', 'nemo', 'gnome-winctl'):
        if not shutil.which(command):
            raise RuntimeError('Missing guest prerequisite: ' + command)
    if len(shell()['monitors']) != 3:
        raise RuntimeError('Configure three real Mutter outputs before seeding')
    run('gsettings', 'set', 'org.gnome.mutter', 'dynamic-workspaces', 'false')
    run('gsettings', 'set', 'org.gnome.desktop.wm.preferences', 'num-workspaces', '4')
    run('gsettings', 'set', 'org.gnome.desktop.wm.preferences', 'workspace-names',
        "['Scale terminals', 'Scale browser', 'Scale social', 'Scale files']")
    run('gsettings', 'set', 'org.gnome.mutter', 'workspaces-only-on-primary', 'false')
    run('gsettings', 'set', 'org.gnome.desktop.session', 'idle-delay', '0')
    run('gsettings', 'set', 'org.gnome.desktop.screensaver', 'lock-enabled', 'false')
    (Path.home() / '.config/gnome-initial-setup-done').write_text('yes\n')
    # This credential-free guest has no login keyring. Persist Code's supported
    # runtime argument so its normal recovery CLI cannot raise a keyring modal.
    code_runtime = Path.home() / '.vscode/argv.json'
    code_runtime.parent.mkdir(exist_ok=True)
    code_runtime.write_text(json.dumps({'password-store': 'basic'}) + '\n')
    autostart = Path.home() / '.config/autostart/apport-gtk.desktop'
    autostart.parent.mkdir(parents=True, exist_ok=True)
    autostart.write_text('[Desktop Entry]\nType=Application\nName=Disabled in synthetic fixture\nHidden=true\n')
    run('xdg-mime', 'default', 'nemo.desktop', 'inode/directory')
    initial = run('pgrep', '-u', str(os.getuid()), '-f', '^/usr/libexec/gnome-initial-setup', check=False)
    for pid in initial.splitlines():
        os.kill(int(pid), 15)
    fixture = ROOT / 'fixture.py'
    shutil.copy2(HERE / 'vm_scale_fixture.py', fixture)
    binary = Path.home() / '.local/bin'
    binary.mkdir(exist_ok=True)
    codex = binary / 'codex'
    if codex.exists() and 'wsctl-scale' not in codex.read_text():
        raise RuntimeError('Refusing to replace an existing non-fixture Codex executable')
    codex.write_text(f'#!/bin/sh\nexec /usr/bin/python3 {shlex.quote(str(fixture))} conversation "$@"\n')
    codex.chmod(0o700)
    for name in ('slack', 'discord', 'telegramdesktop', 'org.telegram.desktop', 'viber'):
        old_proxy = Path.home() / '.local/share/applications' / (name + '.desktop')
        if old_proxy.exists() and 'wsctl-scale' in old_proxy.read_text():
            old_proxy.unlink()
    for alias, identifier, title in [('slack', 'slack_slack', 'Slack'), ('discord', 'discord_discord', 'Discord'),
                                      ('telegram-desktop', 'telegram-desktop_telegram-desktop', 'Telegram'),
                                      ('viber', 'viber_viber', 'Viber')]:
        desktop = Path.home() / '.local/share/applications' / (identifier + '.desktop')
        desktop.parent.mkdir(parents=True, exist_ok=True)
        if desktop.exists() and 'wsctl-scale' not in desktop.read_text():
            raise RuntimeError('Refusing to replace a non-fixture desktop override: ' + str(desktop))
        if real_social():
            if desktop.exists() and 'wsctl-scale' in desktop.read_text():
                desktop.unlink()
            if alias == 'slack':
                original = Path('/var/lib/snapd/desktop/applications/slack_slack.desktop')
                desktop.write_text('# wsctl-scale: credential-free guest has no persistent keyring\n' +
                    original.read_text().replace('/snap/bin/slack ', '/snap/bin/slack --password-store=basic ', 1))
            continue
        desktop.write_text('[Desktop Entry]\nType=Application\nName=' + title + ' scale proxy\n'
                           'Exec=/usr/bin/python3 ' + str(fixture) + ' window ' + alias +
                           ' "' + title + ' — SYNTHETIC PROXY"\nStartupWMClass=' + alias + '\n')
    run('systemctl', '--user', 'import-environment', 'PATH', 'WAYLAND_DISPLAY',
        'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR')
    write('coverage', {'real': ['Ubuntu/GNOME Wayland', 'Alacritty', 'tmux', 'Chrome with native groups',
                               'Nemo companion', 'VS Code companion', 'three virtual Mutter outputs',
                               'checkpoint and startup coordinator'],
                       'synthetic': ['23 conversation processes with open rollout UUIDs',
                                     'Slack/Discord/Telegram/Viber GTK windows'],
                       'not_tested': ['real service authentication', 'model execution',
                                      'physical GPU/EDID/hotplug', 'Windows hibernation']})
    if real_social():
        from workspace_state.social_apps import APPS, desktop_id
        for app in APPS:
            desktop_id(app)
        coverage = json.loads((ROOT / 'coverage.json').read_text())
        coverage['real'].append('Slack, Discord, Telegram and Viber unauthenticated native applications')
        coverage['synthetic'].remove('Slack/Discord/Telegram/Viber GTK windows')
        coverage['synthetic'].append('Slack fixture desktop launcher uses --password-store=basic without a login keyring')
        write('coverage', coverage)


def services():
    """Configure local bind-mount substitutes and a real synthetic VM viewer."""
    from workspace_state.login_finalize import DRIVES
    units = Path.home() / '.config/systemd/user'
    units.mkdir(parents=True, exist_ok=True)
    for drive in DRIVES:
        source = ROOT / 'synthetic-mounts' / drive.stage
        source.mkdir(parents=True, exist_ok=True)
        run('sudo', '-n', 'mkdir', '-p', drive.mountpoint)
        unit = units / drive.unit
        if unit.exists() and 'Synthetic scale fixture' not in unit.read_text():
            raise RuntimeError('Refusing to replace an existing mount service')
        unit.write_text('[Unit]\nDescription=Synthetic scale fixture local bind mount\n'
                        '[Service]\nType=oneshot\nRemainAfterExit=yes\n'
                        f'ExecStart=/usr/bin/sudo -n /usr/bin/mount --bind {source} {drive.mountpoint}\n'
                        f'ExecStop=/usr/bin/sudo -n /usr/bin/umount {drive.mountpoint}\n')
    plugin = Path.home() / '.tmux/plugins'
    if not (plugin / 'tmux-resurrect/resurrect.tmux').exists() or not (plugin / 'tmux-continuum/continuum.tmux').exists():
        raise RuntimeError('Install the real tmux-resurrect and tmux-continuum prerequisites')
    tmux_config = Path.home() / '.tmux.conf'
    tmux_config.write_text("# Synthetic scale fixture configuration\n"
        "set -g @continuum-restore 'off'\nset -g @continuum-save-interval '0'\n"
        "set -g @resurrect-hook-post-save-layout '~/.local/bin/wsctl tmux save'\n"
        "set -g @resurrect-hook-pre-restore-all '~/.local/bin/wsctl tmux begin \"$$\"'\n"
        "set -g @resurrect-hook-post-restore-all '~/.local/bin/wsctl tmux restore'\n"
        "set -g @resurrect-processes '\"wsctl-codex->wsctl-codex-resume *\"'\n"
        f"run-shell {plugin / 'tmux-resurrect/resurrect.tmux'}\n"
        f"set -g @resurrect-save-script-path '{Path.home() / '.local/bin/wsctl-continuum-save'}'\n"
        f"set -g @resurrect-restore-script-path '{Path.home() / '.local/bin/wsctl-continuum-restore'}'\n"
        f"run-shell -b {plugin / 'tmux-continuum/continuum.tmux'}\n")
    run('wsctl', 'tmux', 'configure')
    release = Path.home() / '.local/share/workspace-state/desktop-releases/current/components/workspace-state'
    for name in ('wsctl-gnome-session', 'wsctl-login-finalize'):
        run('systemctl', '--user', 'link', release / 'systemd' / (name + '.service'))
    run('systemctl', '--user', 'enable', 'wsctl-login-finalize.service')
    terminal = Path.home() / '.config/alacritty/alacritty.toml'
    terminal.parent.mkdir(parents=True, exist_ok=True)
    terminal.write_text('[shell]\nprogram = "/usr/bin/tmux"\n'
                        'args = ["new-session", "-A", "-s", "main"]\n')
    autostart = Path.home() / '.config/autostart/wsctl-scale-viewer.desktop'
    autostart.parent.mkdir(parents=True, exist_ok=True)
    autostart.write_text('[Desktop Entry]\nType=Application\nName=Synthetic VM viewer\n'
                        f'Exec=/usr/bin/python3 {HERE / "run_vm_scale.py"} --disposable-guest viewer\n')
    profile = Path.home() / '.config/workspace-state/shutdown-profiles.d/scale-viewer.toml'
    profile.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    profile.parent.chmod(0o700)
    profile.write_text('schema_version = 1\nid = "scale-viewer"\n'
                       'label = "Synthetic VM display availability"\nadapter = "command"\n'
                       'actions = ["poweroff", "restart"]\ncritical = true\ntimeout_seconds = 10\n'
                       f'probe = ["/usr/bin/test", "-S", "{ROOT / "spice.sock"}"]\n'
                       'prepare = ["/usr/bin/true"]\n'
                       f'verify = ["/usr/bin/test", "-S", "{ROOT / "spice.sock"}"]\n'
                       'rollback = ["/usr/bin/true"]\n')
    profile.chmod(0o600)
    run('systemctl', '--user', 'daemon-reload')
    coverage = json.loads((ROOT / 'coverage.json').read_text())
    coverage['real'].append('remote-viewer connected to a local QEMU SPICE display')
    coverage['synthetic'].extend(['Chrome launcher re-registers through private CDP and archives its service-worker cache on companion revision changes',
                                 'three local bind mounts in place of cloud services',
                                 'QEMU firmware display with no guest operating system',
                                 'command shutdown profile observing the synthetic SPICE socket'])
    write('coverage', coverage)


def viewer():
    from workspace_state.desktop import move_window_result
    from workspace_state.provider_results import placement_accepted, placement_frame_matches
    for command in ('qemu-system-x86_64', 'remote-viewer'):
        if not shutil.which(command):
            raise RuntimeError('Missing viewer prerequisite: ' + command)
    endpoint = ROOT / 'spice.sock'
    if endpoint.exists():
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(endpoint))
        except ConnectionRefusedError:
            endpoint.unlink()
        finally:
            probe.close()
    if not endpoint.exists():
        launch(['qemu-system-x86_64', '-name', 'synthetic-scale-display', '-machine', 'q35,accel=tcg',
                '-m', '128', '-smp', '1', '-nodefaults', '-device', 'qxl-vga',
                '-display', 'none', '-spice', f'unix=on,addr={endpoint},disable-ticketing=on',
                '-monitor', 'none'], 'nested-qemu')
        wait_for(endpoint.exists, timeout=10)
    if not any(window.get('wm_class') == 'remote-viewer' for window in shell()['windows']):
        launch(['remote-viewer', '--title', 'SYNTHETIC VM — firmware display',
                'spice+unix://' + str(endpoint)], 'viewer')
    window = wait_for(lambda: next((window for window in shell()['windows']
                                   if window.get('wm_class') == 'remote-viewer'), None))
    saved = ROOT / 'viewer-placement.json'
    if saved.exists():
        placement = json.loads(saved.read_text())
        for target in ({**placement, 'workspace': shell()['active_workspace']}, placement):
            result = move_window_result(window['id'], target)
            if not placement_accepted(result):
                raise RuntimeError('Synthetic viewer placement was rejected')
            wait_for(lambda: any(w['id'] == window['id'] and placement_frame_matches(
                w, result.get('resolved_target') or target) for w in shell()['windows']), timeout=15)
    write('viewer', {'real_application': True, 'native_window': window,
                     'guest_payload': 'Synthetic QEMU firmware only; no Windows/hibernation claim'})


def terminals():
    sessions = run('tmux', 'list-sessions', '-F', '#{session_name}', check=False).splitlines()
    if sessions:
        raise RuntimeError('Seed requires an empty guest tmux server')
    identities = []
    for index in range(10):
        name = 'main' if index == 0 else f'scale-{index + 1:02}'
        directory = ROOT / 'projects' / name
        directory.mkdir(parents=True, exist_ok=True)
        count = 3 if index < 3 else 2
        for pane in range(count):
            identity = str(uuid.uuid5(uuid.NAMESPACE_URL, f'wsctl-scale/{index}/{pane}'))
            identities.append(identity)
            command = f'{Path.home()}/.local/bin/codex resume {identity}'
            if pane == 0:
                run('tmux', 'new-session', '-d', '-s', name, '-c', directory, command)
            else:
                run('tmux', 'split-window', '-d', '-t', name, '-c', directory, command)
            run('tmux', 'select-layout', '-t', name, 'tiled')
        # Automatic names reflect transient tmux modes as well as the running
        # command. Give reboot comparisons explicit, stable fixture intent.
        window_name = f'scale-window-{index + 1:02}'
        target = f'={name}:'
        run('tmux', 'rename-window', '-t', target, window_name)
        naming = run('tmux', 'display-message', '-p', '-t', target,
                     '#{window_name}\t#{automatic-rename}')
        if naming != window_name + '\t0':
            raise RuntimeError(f'Synthetic window name is not fixed: {name}: {naming!r}')
        if index < 6:
            launch(['/usr/bin/alacritty', '--title', name, '-o', 'window.dynamic_title=false',
                    '-e', 'tmux', 'attach-session', '-t', '=' + name], name)
    write('conversation-identities', identities)
    wait_for(lambda: len([w for w in shell()['windows'] if w.get('wm_class') == 'Alacritty']) == 6)


class ChromePipe:
    def __init__(self, *, restore=False):
        self.generation = uuid.uuid4().hex
        self.gate = ROOT / ('chrome-observation-ready-' + self.generation)
        profile = ROOT / 'chrome-profile'
        (profile / 'Default').mkdir(parents=True, exist_ok=True)
        build_info = Path.home() / '.local/share/workspace-state/chrome-extension/build-info.json'
        self.companion_revision = json.loads(build_info.read_text())['revision']
        previous_build = ROOT / 'chrome-last-companion-build.json'
        prior_revision = json.loads(previous_build.read_text()).get('revision') if previous_build.exists() else None
        cache = profile / 'Default/Service Worker'
        provisioning = {'generation': self.generation, 'old_revision': prior_revision,
                        'new_revision': self.companion_revision, 'companion_source': str(build_info.parent.resolve()),
                        'cache_archived': False}
        if prior_revision != self.companion_revision and cache.exists():
            # CDP's temporary registration can crash an extension worker when
            # reusing a different release's cached script. This isolated test
            # profile has no website workers; keep all session/group data and
            # retain the cache for diagnosis instead of deleting it.
            sessions = profile / 'Default/Sessions'
            before = {p.name: digest(p) for p in sessions.glob('*') if p.is_file()}
            archived = ROOT / 'chrome-cache-archive' / self.generation
            archived.mkdir(parents=True)
            cache.rename(archived / 'Service Worker')
            after = {p.name: digest(p) for p in sessions.glob('*') if p.is_file()}
            if before != after:
                raise RuntimeError('Chrome session files changed during fixture cache provisioning')
            provisioning.update(cache_archived=True, archive=str(archived),
                                session_digests_before=before, session_digests_after=after)
        write('chrome-cache-provisioning', provisioning)
        preferences = profile / 'Default/Preferences'
        if not preferences.exists():
            preferences.write_text(json.dumps({'extensions': {'ui': {'developer_mode': True}},
                                               'session': {'restore_on_startup': 1}}))
        incoming_read, self.incoming = os.pipe()
        self.outgoing, outgoing_write = os.pipe()
        trampoline = ('import os,sys; i=os.dup(int(sys.argv[1])); o=os.dup(int(sys.argv[2])); '
                      'os.dup2(i,3); os.dup2(o,4); os.set_inheritable(3,True); '
                      'os.set_inheritable(4,True); os.execv(sys.argv[3],sys.argv[3:])')
        hosts = profile / 'NativeMessagingHosts'
        hosts.mkdir(exist_ok=True)
        host = ROOT / 'native-host'
        gated_host = ROOT / ('native-host-ready-' + self.generation + '.py')
        executable = str(Path.home() / '.local/bin/wsctl-native-host')
        gated_host.write_text('from pathlib import Path\nimport os,time\n'
            f'gate=Path({str(self.gate)!r})\ndeadline=time.monotonic()+60\n'
            'while not gate.exists():\n'
            ' if time.monotonic() >= deadline: raise SystemExit("fixture observation barrier expired")\n'
            ' time.sleep(.05)\n'
            f'os.execv({executable!r}, [{executable!r}])\n')
        host.write_text('#!/bin/sh\nexec 2>>' + shlex.quote(str(ROOT / 'native-host.log')) +
                        '\nexec /usr/bin/python3 ' + shlex.quote(str(gated_host)) + '\n')
        host.chmod(0o700)
        manifest = json.loads((Path.home() / '.config/google-chrome/NativeMessagingHosts/org.sagecat.workspace_state.json').read_text())
        manifest['path'] = str(host)
        destination = hosts / 'org.sagecat.workspace_state.json'
        if destination.exists():
            destination.chmod(0o600)
        destination.write_text(json.dumps(manifest))
        wrapper = Path.home() / '.local/bin/google-chrome'
        wrapper.write_text('#!/bin/sh\nexec /usr/bin/python3 ' + shlex.quote(str(HERE / 'run_vm_scale.py')) + ' --disposable-guest chrome-launch\n')
        wrapper.chmod(0o700)
        command = ['/usr/bin/google-chrome', f'--user-data-dir={profile}', '--ozone-platform=wayland', '--no-first-run',
                   '--no-default-browser-check', '--password-store=basic', '--disable-sync',
                   '--disable-background-networking', '--disable-component-update', '--enable-logging=stderr',
                   '--remote-debugging-pipe', '--enable-unsafe-extension-debugging',
                   '--proxy-server=http://127.0.0.1:9', '--proxy-bypass-list=127.0.0.1;localhost',
                   '--restore-last-session' if restore else 'about:blank']
        with (ROOT / 'chrome.log').open('a') as log:
            self.process = subprocess.Popen([sys.executable, '-c', trampoline, str(incoming_read),
                                             str(outgoing_write), *command],
                                            pass_fds=(incoming_read, outgoing_write), stdout=log,
                                            stderr=subprocess.STDOUT, start_new_session=True)
        os.close(incoming_read)
        os.close(outgoing_write)
        self.number = 0
        self.buffer = bytearray()
        self.extension_id = None
        # The production executable is Chrome itself. This fixture inserts a
        # Python controller solely for temporary extension registration; it
        # must forward the session manager's TERM instead of dying first and
        # dropping Chrome's private pipes during an otherwise normal logout.
        signal.signal(signal.SIGTERM, self.session_stop)

    def session_stop(self, signum, _frame):
        record = {'generation': self.generation, 'signal': signum,
                  'browser_pid': self.process.pid, 'requested_at': time.time()}
        write('chrome-session-stop-' + self.generation, record)
        if self.process.poll() is None:
            self.process.send_signal(signum)
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                # Let the unit's normal bounded cleanup report/handle failure;
                # do not fabricate a graceful exit or use Browser.close here.
                write('chrome-session-stop-' + self.generation, {**record, 'timed_out': True})
                raise SystemExit(1)
        write('chrome-session-stop-' + self.generation,
              {**record, 'exit_code': self.process.returncode, 'finished_at': time.time()})
        raise SystemExit(0)

    def pipe_failure(self, method, error):
        status = self.process.poll()
        if status is None:
            try:
                status = self.process.wait(timeout=.25)
            except subprocess.TimeoutExpired:
                pass
        record = {'generation': self.generation, 'browser_pid': self.process.pid,
                  'exit_code': status, 'method': method, 'request_id': self.number,
                  'error': str(error), 'time': time.time()}
        failures = getattr(self, '_pipe_failures', [])
        failures.append(record)
        self._pipe_failures = failures
        write('chrome-pipe-failure-' + self.generation,
              {**failures[0], 'failures': failures})
        return RuntimeError(f'Chrome DevTools {method} failed (browser exit={status}): {error}')

    def call(self, method, params=None, session=None):
        self.number += 1
        request = {'id': self.number, 'method': method, 'params': params or {}}
        if session:
            request['sessionId'] = session
        try:
            os.write(self.incoming, json.dumps(request).encode() + b'\0')
        except OSError as error:
            raise self.pipe_failure(method, error) from error
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            while b'\0' in self.buffer:
                raw, _, self.buffer = self.buffer.partition(b'\0')
                reply = json.loads(raw)
                if reply.get('id') == self.number:
                    if 'error' in reply:
                        raise RuntimeError(str(reply['error']))
                    return reply['result']
            if select.select([self.outgoing], [], [], max(0, deadline - time.monotonic()))[0]:
                chunk = os.read(self.outgoing, 1024 * 1024)
                if not chunk:
                    raise self.pipe_failure(method, 'Chrome process closed its DevTools pipe')
                self.buffer.extend(chunk)
        raise RuntimeError('Chrome private DevTools response timed out')

    def detach(self, session):
        # A crash during evaluate can also break detach. Preserve the first
        # failure so fixture diagnostics identify the command that crashed.
        failed = sys.exc_info()[0] is not None
        try:
            self.call('Target.detachFromTarget', {'sessionId': session})
        except Exception:
            if not failed:
                raise

    def observe(self):
        target = next((target for target in self.call('Target.getTargets')['targetInfos']
                       if target['type'] == 'service_worker' and self.extension_id
                       and target['url'].startswith('chrome-extension://' + self.extension_id + '/')), None)
        if target is None:
            raise RuntimeError('Chrome companion worker is not ready for observation')
        session = self.call('Target.attachToTarget', {'targetId': target['targetId'], 'flatten': True})['sessionId']
        try:
            result = self.call('Runtime.evaluate', {'expression': '''(async () => {
              const windows = await chrome.windows.getAll({populate:true});
              const result = [];
              for (const window of windows) result.push({id:window.id,
                groups:await chrome.tabGroups.query({windowId:window.id}),
                tabs:window.tabs.map(t=>({id:t.id,url:t.url,pendingUrl:t.pendingUrl || '',
                  status:t.status,groupId:t.groupId,index:t.index}))});
              return result;
            })()''', 'awaitPromise': True, 'returnByValue': True}, session)
            if 'exceptionDetails' in result:
                raise RuntimeError(str(result['exceptionDetails']))
            return result['result']['value']
        finally:
            self.detach(session)

    def evolve_synthetic_tabs(self, *, slow=False, phase='all'):
        """Simulate daytime browsing inside this disposable fixture only."""
        if phase not in {'all', 'urls', 'group'} or (slow and phase != 'all'):
            raise ValueError('Invalid synthetic mutation phase')
        target = next(target for target in self.call('Target.getTargets')['targetInfos']
                      if target['type'] == 'service_worker'
                      and target['url'].startswith('chrome-extension://' + self.extension_id + '/'))
        session = self.call('Target.attachToTarget', {'targetId': target['targetId'], 'flatten': True})['sessionId']
        operation = uuid.uuid4().hex
        progress = {'generation': self.generation, 'operation': operation,
                    'slow': slow, 'phase': phase, 'steps': []}
        progress_name = 'chrome-evolve-progress-' + self.generation + '-' + operation
        def evaluate(step, expression):
            entry = {'step': step, 'started': time.time()}
            progress['steps'].append(entry)
            write(progress_name, progress)
            result = self.call('Runtime.evaluate', {'expression': expression,
                              'awaitPromise': True, 'returnByValue': True}, session)
            if 'exceptionDetails' in result:
                raise RuntimeError(str(result['exceptionDetails']))
            entry['completed'] = time.time()
            write(progress_name, progress)
            return result['result'].get('value')
        def summary(windows):
            return [{'id': w['id'], 'tabs': len(w['tabs']),
                     'groups': sorted({t['groupId'] for t in w['tabs'] if t['groupId'] >= 0})}
                    for w in windows]
        try:
            windows = evaluate('read-before', 'chrome.windows.getAll({populate:true})')
            if len(windows) != 7 or sum(len(w['tabs']) for w in windows) != 42 or any(
                    not t.get('url', '').startswith(('about:blank#scale-', 'http://127.0.0.1:18765/scale/'))
                    or t.get('pendingUrl') or t.get('status') != 'complete'
                    for w in windows for t in w['tabs']):
                raise RuntimeError('Refusing to mutate non-fixture or unsettled browser content')
            before = summary(windows)
            expected_urls = {t['id']: t['url'] for w in windows for t in w['tabs']}
            if phase != 'group':
                for window in windows:
                    for tab in window['tabs'] if slow else window['tabs'][:1]:
                        source = json.dumps(tab['url'])
                        url = ('"http://127.0.0.1:18765/scale/" + encodeURIComponent(' + source + ')'
                               if slow else source + ' + "-evolved"')
                        expected_urls[tab['id']] = evaluate(f'update-tab-{tab["id"]}',
                            f'(async () => {{const url = {url}; '
                            f'await chrome.tabs.update({tab["id"]}, {{url}}); return url;}})()')
            if not slow and phase != 'urls':
                grouped = next(w for w in windows if sum(t['groupId'] >= 0 for t in w['tabs']) > 3)
                members = [t for t in grouped['tabs'] if t['groupId'] >= 0]
                prefix = 'http://127.0.0.1:18765/scale/' if members[0]['url'].startswith('http:') else 'about:blank#scale-'
                replacement = evaluate('create-replacement', 'chrome.tabs.create(' + json.dumps(
                    {'windowId': grouped['id'], 'url': prefix + 'replacement', 'active': False}) + ')')
                evaluate('reuse-existing-group', 'chrome.tabs.group(' + json.dumps(
                    {'groupId': members[0]['groupId'], 'tabIds': [replacement['id']]}) + ')')
                evaluate('remove-replaced-tab', f'chrome.tabs.remove({members[-1]["id"]})')
                del expected_urls[members[-1]['id']]
                expected_urls[replacement['id']] = prefix + 'replacement'
            after = summary(evaluate('read-after', 'chrome.windows.getAll({populate:true})'))
            if before != after:
                raise RuntimeError('Fixture mutation changed window, tab or group counts')
            return {'before': before, 'after': after, 'slow': slow, 'phase': phase,
                    'expected_urls': [{'id': key, 'url': value} for key, value in expected_urls.items()]}
        finally:
            self.detach(session)

    def serve(self):
        """Expose fixed observation and orderly-close actions for this fixture."""
        endpoint = ROOT / 'chrome-observe.sock'
        endpoint.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(endpoint))
        endpoint.chmod(0o600)
        server.listen(1)
        server.settimeout(.5)
        observed = None
        def populated():
            nonlocal observed
            state = self.observe()
            observed = state
            write('chrome-login-observed', {'generation': self.generation, 'state': state,
                  'counts': {'windows': len(state), 'tabs': sum(len(w['tabs']) for w in state),
                             'groups': sum(len(w['groups']) for w in state)}})
            return state if (len(state) == 7 and sum(len(w['tabs']) for w in state) == 42
                             and sum(len(w['groups']) for w in state) == 3) else None
        baseline_error = None
        try:
            write('chrome-login-original', wait_for(populated, timeout=30))
        except RuntimeError as error:
            baseline_error = str(error)
            write('chrome-login-invalid', {'generation': self.generation,
                  'error': baseline_error, 'observed': observed})
        write('chrome-observation-launch', {'generation': self.generation,
                                            'process': process_identity(self.process.pid),
                                            'baseline_valid': baseline_error is None})
        if baseline_error is None:
            write('chrome-last-companion-build', {'revision': self.companion_revision})
        # No native-host request (and therefore no restore mutation) can pass
        # until the original browser IDs/membership are durably observed.
        if baseline_error is None:
            self.gate.write_text(str(self.process.pid) + '\n')
        try:
            while self.process.poll() is None:
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(5)
                    command = connection.recv(32)
                    if command == b'close\n':
                        self.call('Browser.close')
                        self.process.wait(timeout=8)
                        connection.sendall(b'{}\n')
                        return
                    if command in {b'evolve-synthetic\n', b'slow-synthetic\n', b'evolve-urls\n', b'evolve-group\n'}:
                        try:
                            if baseline_error is not None:
                                raise RuntimeError('Native restore counts are invalid; observation/close only: ' + baseline_error)
                            phase = {b'evolve-urls\n': 'urls', b'evolve-group\n': 'group'}.get(command, 'all')
                            value = self.evolve_synthetic_tabs(slow=command == b'slow-synthetic\n', phase=phase)
                        except Exception as error:
                            value = {'error': str(error)}
                        connection.sendall(json.dumps(value).encode() + b'\n')
                        continue
                    if command != b'observe\n':
                        continue
                    connection.sendall(json.dumps(self.observe()).encode() + b'\n')
        finally:
            server.close()
            endpoint.unlink(missing_ok=True)
            self.gate.unlink(missing_ok=True)


def observe_chrome(command='observe'):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        # A mutation may need the full bounded DevTools reply budget. Dropping
        # its caller early can hide the actual browser result behind EPIPE.
        connection.settimeout(10 if command in {'observe', 'close'} else 65)
        connection.connect(str(ROOT / 'chrome-observe.sock'))
        connection.sendall(command.encode() + b'\n')
        with connection.makefile('r') as stream:
            return json.loads(stream.readline())


def configure_pages(delay=0):
    """Serve real local HTTP responses across guest power-off cycles."""
    source = HERE / 'vm_slow_pages.py'
    if not source.is_file():
        raise RuntimeError('Copy vm_slow_pages.py beside this fixture first')
    unit = Path.home() / '.config/systemd/user/wsctl-scale-pages.service'
    if unit.exists() and 'Synthetic scale fixture' not in unit.read_text():
        raise RuntimeError('Refusing to overwrite a non-fixture service')
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text('[Unit]\nDescription=Synthetic scale fixture delayed HTTP pages\n'
                    '[Service]\nType=exec\nExecStart=/usr/bin/python3 ' + str(source) + '\n'
                    'TimeoutStopSec=2\n[Install]\nWantedBy=default.target\n')
    write('http-delay', {'seconds': delay})
    run('systemctl', '--user', 'daemon-reload')
    run('systemctl', '--user', 'enable', '--now', unit.name)
    from urllib.request import urlopen
    def ready():
        try:
            with urlopen('http://127.0.0.1:18765/health', timeout=1) as response:
                return response.read() == b'wsctl-scale-pages\n'
        except OSError:
            return False
    wait_for(ready, timeout=10)


def slow_pages():
    """Enable measured loading delays without replacing existing tab groups."""
    configure_pages(3)
    result = observe_chrome('slow-synthetic')
    if result.get('error'):
        raise RuntimeError(result['error'])
    write('slow-pages-start', result)


def chrome():
    for pid in {window['pid'] for window in shell()['windows']
                if 'google-chrome' in str(window.get('app_ids'))}:
        process = Path(f'/proc/{pid}/cmdline')
        if str(ROOT / 'chrome-profile').encode() not in process.read_bytes():
            raise RuntimeError('Refusing to replace a non-fixture Chrome process')
        os.kill(pid, 15)
        wait_for(lambda: not process.exists(), timeout=15)
    ready = ROOT / 'chrome-ready.json'
    if ready.exists():
        prior = json.loads(ready.read_text())
        process = Path(f'/proc/{prior["pid"]}/cmdline')
        if process.exists():
            if str(ROOT / 'chrome-profile').encode() not in process.read_bytes():
                raise RuntimeError('Old Chrome PID no longer belongs to this fixture')
            os.kill(prior['pid'], 15)
            wait_for(lambda: not process.exists(), timeout=10)
        ready.unlink()
    # Explicit seeding starts fresh synthetic browser content. Retain the old
    # test profile for diagnosis; ordinary cycle/reboot launches reuse it.
    profile = ROOT / 'chrome-profile'
    if profile.exists():
        archived = ROOT / 'seeds' / ('chrome-' + uuid.uuid4().hex)
        archived.parent.mkdir(exist_ok=True)
        profile.rename(archived)
    launch([sys.executable, HERE / 'run_vm_scale.py', '--disposable-guest', 'chrome-host'], 'chrome-host')
    wait_for(lambda: (ROOT / 'chrome-ready.json').exists(), timeout=120)


def chrome_launch():
    # CDP unpacked registration is temporary. The synthetic guest launcher
    # repeats it on each real browser start; this is fixture provisioning,
    # not evidence that Chrome persists an extension installed through CDP.
    pipe = ChromePipe(restore=True)
    extension = Path.home() / '.local/share/workspace-state/chrome-extension'
    pipe.extension_id = pipe.call('Extensions.loadUnpacked', {'path': str(extension)})['id']
    write('chrome-launch', {'pid': pipe.process.pid, 'controller': os.getpid(),
                            'generation': pipe.generation,
                            'registration': 'temporary CDP registration on every fixture launch'})
    pipe.serve()


def chrome_host():
    # Real HTTP pages exercise navigation and delayed loading. Chrome 154 can
    # trap when a restored single about:blank tab gets a fragment-only update.
    configure_pages()
    pipe = ChromePipe()
    extension = Path.home() / '.local/share/workspace-state/chrome-extension'
    loaded = pipe.call('Extensions.loadUnpacked', {'path': str(extension)})
    pipe.extension_id = loaded['id']
    write('chrome-extension-load', loaded)
    target = wait_for(lambda: next((target for target in pipe.call('Target.getTargets')['targetInfos']
                                   if target['type'] == 'service_worker'
                                   and target['url'].startswith('chrome-extension://' + loaded['id'] + '/')), None))
    session = pipe.call('Target.attachToTarget', {'targetId': target['targetId'], 'flatten': True})['sessionId']
    expression = '''(async () => {
      const initial = await chrome.windows.getAll(); const result = [];
      const sizes = [4,4,1,11,11,10,1]; const grouped = [0,3,4];
      for (let i=0; i<7; i++) {
        const w = await chrome.windows.create({url: 'http://127.0.0.1:18765/scale/' + i, focused: false});
        const tabIds = [w.tabs[0].id];
        for (let t=1; t<sizes[i]; t++)
          tabIds.push((await chrome.tabs.create({windowId:w.id, url:'http://127.0.0.1:18765/scale/' + i + '-tab-' + t})).id);
        let groupId = null;
        if (grouped.includes(i)) {
          groupId = await chrome.tabs.group({tabIds,createProperties:{windowId:w.id}});
          await chrome.tabGroups.update(groupId,{title:'Existing scale group '+i,color:'blue',collapsed:false});
        }
        result.push({window:w.id,group:groupId,tabs:sizes[i]});
      }
      for (const w of initial) await chrome.windows.remove(w.id);
      return result;
    })()'''
    result = pipe.call('Runtime.evaluate', {'expression': expression, 'awaitPromise': True,
                                           'returnByValue': True}, session)
    if 'exceptionDetails' in result:
        raise RuntimeError(str(result))
    write('chrome-original-groups', result['result']['value'])
    write('chrome-ready', {'pid': pipe.process.pid, 'controller': os.getpid()})
    pipe.serve()


def applications():
    from workspace_state import file_manager, vscode
    from workspace_state.social_apps import APPS, desktop_id, matching_windows
    for alias, title in [('slack', 'Slack'), ('discord', 'Discord'),
                         ('telegram-desktop', 'Telegram'), ('viber', 'Viber')]:
        app = next(app for app in APPS if app.label == title)
        if not matching_windows(app, shell()):
            if real_social():
                launch(['gtk-launch', desktop_id(app)], alias)
            else:
                launch(['/usr/bin/python3', ROOT / 'fixture.py', 'window', alias,
                        title + ' — SYNTHETIC PROXY'], alias)
    for index in range(4):
        folder = ROOT / 'folders' / f'folder-{index + 1}'
        folder.mkdir(parents=True, exist_ok=True)
        launch(['nemo', '--no-default-window', folder], f'nemo-{index}')
        time.sleep(.5)
    wait_for(lambda: len(file_manager._bridge('GetState').get('windows', [])) == 4)
    profile = ROOT / 'code-profile'
    (profile / 'User').mkdir(parents=True, exist_ok=True)
    (profile / 'User/settings.json').write_text(json.dumps({
        'telemetry.telemetryLevel': 'off', 'update.mode': 'none', 'update.showReleaseNotes': False,
        'extensions.autoUpdate': False, 'extensions.autoCheckUpdates': False,
        'workbench.startupEditor': 'none', 'workbench.enableExperiments': False,
        'security.workspace.trust.enabled': False, 'git.enabled': False,
        'http.proxy': 'http://127.0.0.1:9', 'http.proxySupport': 'override',
        'files.hotExit': 'onExitAndWindowClose', 'window.restoreWindows': 'none'}))
    project = ROOT / 'code-project'
    project.mkdir(exist_ok=True)
    (project / 'fixture.txt').write_text('Synthetic VM scale fixture.\n')
    launch(['code', '--user-data-dir', profile, '--new-window', '--ozone-platform=wayland',
            '--password-store=basic', '--skip-welcome', '--skip-release-notes',
            '--disable-workspace-trust', project, project / 'fixture.txt'], 'vscode')
    wait_for(lambda: len(vscode._states(time.monotonic() + 2)) == 1)
    wait_for(lambda: all(len(matching_windows(app, shell())) == 1 for app in APPS), timeout=90)
    wait_for(lambda: len(shell()['windows']) >= 22)


def place():
    from workspace_state.desktop import move_window_result
    from workspace_state.provider_results import placement_accepted, placement_frame_matches
    from workspace_state import browser
    from workspace_state.social_apps import APPS, matching_windows
    classes = {'Alacritty', 'nemo', 'com.microsoft.VSCode', 'slack', 'discord',
               'telegram-desktop', 'viber', 'remote-viewer'}
    state = shell()
    social_ids = {window['id'] for app in APPS for window in matching_windows(app, state)}
    windows = [window for window in state['windows'] if window.get('wm_class') in classes or window['id'] in social_ids]
    monitors = shell()['monitors']
    for index, window in enumerate(windows):
        monitor = monitors[index % 3]
        target = {'workspace': index % 4, 'monitor': monitor['index'],
                  'state': 'maximized' if window.get('wm_class') == 'com.microsoft.VSCode' else window['state'],
                  'coordinate_space': 'monitor',
                  'geometry': {'x': 80, 'y': 60, 'width': window['geometry']['width'],
                               'height': window['geometry']['height']}}
        for placement in ({**target, 'workspace': shell()['active_workspace']}, target):
            result = move_window_result(window['id'], placement)
            if not placement_accepted(result):
                raise RuntimeError(f'Placement rejected: {result}')
            resolved = result.get('resolved_target') or placement
            try:
                wait_for(lambda: any(w['id'] == window['id'] and placement_frame_matches(w, resolved)
                                     for w in shell()['windows']), timeout=10)
            except RuntimeError:
                write('placement-failure', {'window': window, 'request': result,
                                            'observed': [w for w in shell()['windows'] if w['id'] == window['id']]})
                raise
    # Chrome only acknowledges some Wayland frame changes after its exact
    # window is identified. Exercise the production identity/placement path.
    for index, window in enumerate(browser.request_browser('capture', profile='Default')['windows']):
        target = {'workspace': index % 4, 'monitor': index % 3, 'state': 'normal',
                  'coordinate_space': 'monitor',
                  # Fresh Chrome profiles can impose an 882px minimum even
                  # for a single tab. Keep the fixture above that constraint.
                  'geometry': {'x': 80, 'y': 60, 'width': 960, 'height': 600}}
        if not browser._place_browser_window(profile='Default', chrome_window_id=window['runtime_window_id'],
                                             app_id='google-chrome', placement=target):
            raise RuntimeError('Chrome seed placement did not verify')
    write('seed-desktop', shell())


def checkpoint():
    from workspace_state.cli import _capture_all
    from workspace_state.storage import load
    if (ROOT / 'baseline-digests.json').exists():
        raise RuntimeError('Validated baseline is frozen; start an explicit new seed to replace it')
    check_counts(_capture_all())
    result = subprocess.run(['wsctl', 'save'], text=True, capture_output=True, timeout=180)
    (ROOT / 'checkpoint.log').write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError('Full checkpoint failed; see checkpoint.log')
    snapshot = load()
    check_counts(snapshot)
    native_viewer = next(w for w in shell()['windows'] if w.get('wm_class') == 'remote-viewer')
    write('viewer-placement', {'coordinate_space': 'global', **{key: native_viewer[key] for key in
                              ('workspace', 'monitor', 'monitor_identity', 'state', 'geometry')}})
    # Save through the real resurrect contract, then arm next-login continuum.
    run('wsctl-continuum-save', timeout=60)
    snapshot = load()
    check_counts(snapshot)
    write('checkpoint', snapshot)
    write('baseline-digests', baseline_digests())
    config = Path.home() / '.tmux.conf'
    config.write_text(config.read_text().replace("@continuum-restore 'off'", "@continuum-restore 'on'"))
    run('tmux', 'set-option', '-g', '@continuum-restore', 'on')


def check_counts(snapshot):
    from workspace_state.social_apps import APPS, matching_windows
    state = shell()
    native_social = {app.id: len(matching_windows(app, state)) for app in APPS}
    if native_social != {app.id: 1 for app in APPS}:
        raise RuntimeError('Unexpected native social windows, including minimized duplicates: ' + repr(native_social))
    native_counts = {
        'Alacritty': sum(w.get('wm_class') == 'Alacritty' for w in state['windows']),
        'Chrome': sum('google-chrome' in str(w.get('app_ids')) for w in state['windows']),
        'Nemo': sum(w.get('wm_class') == 'nemo' for w in state['windows']),
        'Code': sum(w.get('wm_class') == 'com.microsoft.VSCode' for w in state['windows']),
    }
    if native_counts != {'Alacritty': 6, 'Chrome': 7, 'Nemo': 4, 'Code': 1}:
        raise RuntimeError('Unexpected complete native inventory: ' + repr(native_counts))
    profiles = snapshot.get('browsers', {}).get('google_chrome', {}).get('profiles', [])
    chrome_windows = [window for profile in profiles for window in profile['windows']]
    counts = {'terminals': len(snapshot['terminals']), 'tmux_sessions': len(snapshot['sessions']),
              'conversations': sum(bool(p.get('codex', {}).get('session_id')) for s in snapshot['sessions']
                                   for w in s['windows'] for p in w['panes'] if p.get('codex')),
              'chrome_windows': len(chrome_windows),
              'chrome_groups': sum(len(w['groups']) for w in chrome_windows),
              'chrome_tabs': sum(len(w['tabs']) for w in chrome_windows),
              'nemo_windows': len(snapshot.get('file_manager', {}).get('windows', [])),
              'vscode_windows': len(snapshot.get('vscode', {}).get('windows', [])),
              'social_windows': sum(len(app['windows']) for app in snapshot.get('social_apps', {}).values()),
              'monitors': len(snapshot['desktop']['monitors']),
              'viewer_windows': sum(w.get('wm_class') == 'remote-viewer' for w in state['windows']),
              'native_windows': len(state['windows'])}
    write('counts', {'expected': EXPECTED, 'actual': counts})
    if counts != EXPECTED:
        raise RuntimeError('Scale counts differ: ' + repr(counts))
    expected = set(json.loads((ROOT / 'conversation-identities.json').read_text()))
    actual = {p['codex']['session_id'] for s in snapshot['sessions'] for w in s['windows']
              for p in w['panes'] if p.get('codex')}
    if actual != expected:
        raise RuntimeError('Synthetic conversation UUIDs changed')
    return counts


def comparable_items(snapshot):
    """Stable content keys pair each saved window with its observed placement."""
    items = {}
    for terminal in snapshot['terminals']:
        items['terminal/' + terminal['session']] = ({}, terminal['placement'])
    for provider, key in [('file_manager', 'locations'), ('vscode', 'window_key')]:
        for window in snapshot[provider]['windows']:
            identity = json.dumps(window[key], sort_keys=True)
            fields = ('locations', 'active_tab') if provider == 'file_manager' else (
                'folders', 'editor_uris', 'dirty_count', 'profile', 'workspace_file')
            items[provider + '/' + identity] = ({field: window.get(field) for field in fields}, window['placement'])
    for alias, application in snapshot['social_apps'].items():
        for index, window in enumerate(application['windows']):
            items[f'social/{alias}/{index}'] = ({}, window)
    for profile in snapshot['browsers']['google_chrome']['profiles']:
        for window in profile['windows']:
            identity = json.dumps([tab['url'] for tab in window['tabs']])
            placement = {'workspace': window['workspace_index'], 'monitor': window['monitor']['index'],
                         'monitor_identity': window['monitor'], 'state': window['state'], 'geometry': window['geometry']}
            items['chrome/' + identity] = ({'tabs': window['tabs'], 'groups': window['groups']}, placement)
    return items


def verify_semantics(snapshot):
    from workspace_state.provider_results import placement_matches
    baseline = json.loads((ROOT / 'checkpoint.json').read_text())
    expected, actual = comparable_items(baseline), comparable_items(snapshot)
    if set(expected) != set(actual):
        raise RuntimeError('Saved application window/content identities changed')
    failures = []
    for identity, (content, placement) in expected.items():
        observed_content, observed_placement = actual[identity]
        if content != observed_content:
            failures.append({'item': identity, 'reason': 'content differs'})
        if (not placement_matches(observed_placement, placement, tolerance=3)
                or any(observed_placement.get('monitor_identity', {}).get(field)
                       != placement.get('monitor_identity', {}).get(field)
                       for field in ('connector', 'edid_hash'))):
            failures.append({'item': identity, 'reason': 'placement differs',
                             'expected': placement, 'actual': observed_placement})
    def conversations(document):
        return sorted((s['name'], w['index'], p['index'], p['cwd'], p.get('codex', {}).get('session_id'))
                      for s in document['sessions'] for w in s['windows'] for p in w['panes'])
    if conversations(snapshot) != conversations(baseline):
        failures.append({'reason': 'tmux session/pane/CWD/UUID ownership differs'})
    if {p['monitor'] for _, p in expected.values()} != {0, 1, 2}:
        failures.append({'reason': 'fixture did not populate all three monitors'})
    if {p['workspace'] for _, p in expected.values()} != {0, 1, 2, 3}:
        failures.append({'reason': 'fixture did not populate all four workspaces'})
    viewer_target = json.loads((ROOT / 'viewer-placement.json').read_text())
    viewer_windows = [w for w in shell()['windows'] if w.get('wm_class') == 'remote-viewer']
    if len(viewer_windows) != 1 or not placement_matches(viewer_windows[0], viewer_target, tolerance=3):
        failures.append({'reason': 'synthetic viewer placement differs'})
    write('semantic-verification', {'items': len(expected), 'failures': failures})
    if failures:
        raise RuntimeError(f'{len(failures)} semantic verification failures; see semantic-verification.json')
    return len(expected)


def cycle(reboot_guest=False):
    """End only this disposable desktop, then exercise normal login startup."""
    from workspace_state.cli import _capture_all
    before = _capture_all()
    check_counts(before)
    verify_semantics(before)
    frozen = json.loads((ROOT / 'baseline-digests.json').read_text())
    if baseline_digests() != frozen:
        raise RuntimeError('The frozen checkpoint changed before cycling')
    run('systemctl', '--user', 'stop', 'wsctl-gnome-session.service')
    sessions = run('tmux', 'list-sessions', '-F', '#{session_name}').splitlines()
    if set(sessions) != {record['name'] for record in before['sessions']}:
        raise RuntimeError('Guest tmux ownership changed')
    native_pids = sorted({window['pid'] for window in shell()['windows']})
    conversation_pids = sorted({pane['codex']['pid'] for session in before['sessions']
                                for window in session['windows'] for pane in window['panes']
                                if pane.get('codex')})
    login_id = wayland_login()
    cycle_id = time.strftime('%Y%m%dT%H%M%S', time.gmtime()) + '-' + uuid.uuid4().hex[:8]
    for name in ('result', 'failure-verify', 'verified-live-checkpoint', 'verified-desktop',
                 'coordinator-status', 'chrome-login-final', 'semantic-verification'):
        (ROOT / (name + '.json')).unlink(missing_ok=True)
    identities = [process_identity(pid) for pid in sorted(set(native_pids + conversation_pids))]
    if any(identity is None for identity in identities):
        raise RuntimeError('A captured process exited before the cycle began')
    write('cycle-before', {'session': login_id, 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                           'cycle_id': cycle_id, 'requested_mode': 'reboot' if reboot_guest else 'login',
                           'baseline_digests': frozen, 'process_identities': identities,
                           'native_pids': native_pids, 'conversation_pids': conversation_pids,
                           'counts': check_counts(before), 'time': time.time()})
    archive_cycle()
    run('systemctl', '--user', 'enable', 'wsctl-gnome-session.service')
    # Close the whole synthetic browser through its real API before ending the
    # session, preserving all windows/groups in Chrome's own session store.
    observe_chrome('close')
    code_pids = {window['pid'] for window in shell()['windows']
                 if window.get('wm_class') == 'com.microsoft.VSCode'}
    for pid in code_pids:
        if Path(f'/proc/{pid}/exe').resolve().name != 'code':
            raise RuntimeError('Code PID changed before orderly termination')
        os.kill(pid, 15)
    wait_for(lambda: not any(window['pid'] in code_pids or 'google-chrome' in str(window.get('app_ids'))
                             for window in shell()['windows']), timeout=20)
    run('gdbus', 'call', '--session', '--dest', 'org.gnome.SessionManager',
        '--object-path', '/org/gnome/SessionManager', '--method', 'org.gnome.SessionManager.Logout', '1')
    wait_for(lambda: not run('loginctl', 'show-session', login_id, '-p', 'State', '--value',
                             check=False), timeout=30)
    # These are exactly the ten just-captured synthetic sessions. Killing this
    # dedicated guest server proves startup reconstructs all 23 UUID workers.
    run('tmux', 'kill-server')
    if reboot_guest:
        run('sudo', '-n', 'systemctl', 'reboot')
    else:
        run('sudo', '-n', 'systemctl', 'restart', 'gdm3')
    write('cycle-started', {'old_login_ended': True, 'old_tmux_stopped': True, 'time': time.time()})


def coordinator():
    run('systemctl', '--user', 'start', 'wsctl-gnome-session.service')
    write('coordinator-start', {'time': time.time(), 'active': run('systemctl', '--user', 'is-active',
                                                                 'wsctl-gnome-session.service')})


def verify_running_companions():
    release = (Path.home() / '.local/share/workspace-state/desktop-releases/current').resolve()
    enabled = ast.literal_eval(run('gsettings', 'get', 'org.gnome.shell', 'enabled-extensions'))
    production = {'gnome-winctl-v3@sagecat.local', 'login-hud-v2@sagecat.local'}
    if not production.issubset(enabled) or any('scale-' in entry for entry in enabled):
        raise RuntimeError('Production companions are not exclusively enabled: ' + repr(enabled))
    native = json.loads(run('gnome-winctl', 'status'))
    hud_reply = run('gdbus', 'call', '--session', '--dest', 'org.gnome.Shell',
                    '--object-path', '/org/sagecat/LoginHud', '--method', 'org.sagecat.LoginHud.GetState')
    hud = json.loads(ast.literal_eval(hud_reply)[0])
    result = {'installed_release': release.name, 'enabled_extensions': enabled,
              'native': native, 'hud': hud}
    write('running-companions', result)
    for state, uuid, build_file in (
        (native, 'gnome-winctl-v3@sagecat.local',
         release / 'components/gnome-winctl/extension/gnome-winctl-v3@sagecat.local/buildInfo.js'),
        (hud, 'login-hud-v2@sagecat.local', release / 'components/login-hud/buildInfo.js'),
    ):
        match = re.search(r"BUILD_REVISION\s*=\s*['\"]([^'\"]+)['\"]", build_file.read_text())
        expected = match.group(1) if match else None
        if (not expected or expected != release.name or state.get('build', {}).get('uuid') != uuid
                or state.get('build', {}).get('revision') != expected):
            raise RuntimeError('Running companion does not match the sealed production release: ' + uuid)
    return result


def verify():
    from workspace_state.cli import _capture_all
    from workspace_state.login_status import status_path
    before = json.loads((ROOT / 'cycle-before.json').read_text())
    companions = verify_running_companions()
    if baseline_digests() != before['baseline_digests']:
        raise RuntimeError('The frozen checkpoint changed across the cycle')
    boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    login_id = wayland_login()
    if boot_id == before['boot_id'] and login_id == before['session']:
        raise RuntimeError('Verification requires a new graphical login')
    if before['requested_mode'] == 'reboot' and boot_id == before['boot_id']:
        raise RuntimeError('The requested guest reboot did not occur')
    if boot_id == before['boot_id']:
        survivors = [identity for identity in before['process_identities']
                     if process_identity(identity['pid']) == identity]
        if survivors:
            raise RuntimeError('Captured native/conversation processes survived: ' + repr(survivors))
    snapshot = _capture_all()
    write('verified-live-checkpoint', snapshot)
    counts = check_counts(snapshot)
    items = verify_semantics(snapshot)
    status = json.loads(status_path().read_text())
    write('coordinator-status', status)
    write('verified-desktop', shell())
    active = run('systemctl', '--user', 'is-active', 'wsctl-gnome-session.service')
    if active != 'active' or status.get('operation_state') != 'completed':
        raise RuntimeError('Coordinator has not completed: ' + str(status.get('operation_state')))
    stages = {stage['id']: stage for stage in status['stages']}
    required = {'gnome', 'displays', 'tmux', 'terminals', 'codex', 'browsers', 'social-apps',
                'file-manager', 'vscode', 'virtual-machines', 'workspace', 'gdrive', 'nextcloud',
                'pdrive', 'warmup', 'login-finalization'}
    if set(stages) != required or any(stage['state'] != 'ready' for key, stage in stages.items()
                                     if key != 'virtual-machines'):
        raise RuntimeError('A required coordinator stage is missing or did not succeed')
    if stages['virtual-machines']['state'] != 'skipped' or stages['virtual-machines'].get('message') != 'No committed VM restore jobs':
        raise RuntimeError('Unexpected VM transaction outcome in the command-profile fixture')
    for key, expected in [('browsers', 7), ('social-apps', 4), ('file-manager', 4), ('vscode', 1)]:
        receipts = stages[key].get('provider_results', [])
        if len(receipts) != expected or any(not receipt.get('success') or any(
                receipt.get(phase, {}).get('state') != 'verified' for phase in ('identity', 'placement'))
                for receipt in receipts):
            raise RuntimeError('Missing or unsuccessful provider receipts: ' + key)
        for receipt in receipts:
            content = receipt.get('content', {})
            expected_social_skip = (key == 'social-apps' and content.get('state') == 'skipped'
                                    and content.get('detail') == 'App content recovery is owned by the application')
            if content.get('state') != 'verified' and not expected_social_skip:
                raise RuntimeError('Unexpected provider content evidence: ' + key)
    if (status.get('operation_context', {}).get('boot_id') != boot_id
            or datetime.fromisoformat(status['started_at']).timestamp() < before['time'] - 1):
        raise RuntimeError('Coordinator completion belongs to an older login')
    chrome_before = json.loads((ROOT / 'chrome-login-original.json').read_text())
    launch_record = json.loads((ROOT / 'chrome-launch.json').read_text())
    observation_record = json.loads((ROOT / 'chrome-observation-launch.json').read_text())
    if (launch_record['generation'] != observation_record['generation']
            or process_identity(launch_record['pid']) != observation_record['process']):
        raise RuntimeError('Chrome original-ID baseline belongs to another launch')
    chrome_after = observe_chrome()
    write('chrome-login-final', chrome_after)
    if chrome_before != chrome_after:
        raise RuntimeError('Chrome native window/tab/group IDs or membership changed during restoration')
    fresh = {'all_native_and_conversation_processes_recreated': True,
             'old_login': [before['boot_id'], before['session']], 'new_login': [boot_id, login_id],
             'guest_rebooted': boot_id != before['boot_id']}
    package_versions = {}
    if real_social():
        for name in ('slack', 'discord', 'telegram-desktop', 'viber'):
            package = Path('/snap') / name / 'current'
            metadata = package / 'meta/snap.yaml'
            if metadata.is_file():
                version = next((line.partition(':')[2].strip().strip("\"'")
                                for line in metadata.read_text().splitlines() if line.startswith('version:')), None)
                package_versions[name] = {'revision': package.resolve().name, 'version': version}
    result = {'passed': True, 'counts': counts, 'semantic_items': items, 'fresh_processes': fresh,
              'coordinator_active': active, 'operation_id': status.get('operation_id'), 'time': time.time(),
              'cycle_id': before['cycle_id'], 'requested_mode': before['requested_mode'],
              'baseline_digests': before['baseline_digests'],
              'social_mode': 'real unauthenticated applications' if real_social() else 'synthetic GTK proxies',
              'coverage': json.loads((ROOT / 'coverage.json').read_text()),
              'installed_release': (Path.home() / '.local/share/workspace-state/desktop-releases/current').resolve().name,
              'social_package_versions': package_versions, 'running_companions': companions}
    write('result', result)
    history = ROOT / 'cycle-results.json'
    records = json.loads(history.read_text()) if history.exists() else []
    write('cycle-results', [record for record in records if record.get('cycle_id') != before['cycle_id']] + [result])
    archive_cycle()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-guest', action='store_true', required=True)
    parser.add_argument('--real-social', action='store_true', help='Seed installed unauthenticated social apps instead of GTK proxies')
    parser.add_argument('phase', choices=['seed', 'configure', 'services', 'viewer', 'terminals', 'chrome', 'chrome-host', 'chrome-launch', 'slow-pages', 'applications', 'place', 'checkpoint', 'cycle', 'reboot', 'coordinator', 'verify'])
    args = parser.parse_args()
    if socket.gethostname() != 'wsctl-validation' or run('systemd-detect-virt') != 'kvm':
        raise SystemExit('Refusing outside wsctl-validation KVM guest')
    # SSH has no graphical DISPLAY/Xauthority. Preserve the real login's Xwayland
    # environment for packaged applications whose normal backend still uses it.
    for line in run('systemctl', '--user', 'show-environment').splitlines():
        key, separator, value = line.partition('=')
        if separator and key in {'DISPLAY', 'XAUTHORITY'}:
            os.environ[key] = value
    os.environ.update({'PATH': str(Path.home() / '.local/bin') + ':' + os.environ['PATH'],
                       'XDG_RUNTIME_DIR': f'/run/user/{os.getuid()}',
                       'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{os.getuid()}/bus',
                       'WAYLAND_DISPLAY': 'wayland-0', 'XDG_SESSION_TYPE': 'wayland'})
    data_dirs = os.environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share').split(':')
    if '/var/lib/snapd/desktop' not in data_dirs:
        data_dirs.append('/var/lib/snapd/desktop')
    os.environ['XDG_DATA_DIRS'] = ':'.join(data_dirs)
    release = (Path.home() / '.local/bin/wsctl').resolve().parent.parent
    sys.path.insert(0, str(release / 'src'))
    ROOT.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        if args.phase in {'seed', 'configure'}:
            write('social-mode', {'real': args.real_social})
        if args.phase == 'seed':
            if (ROOT / 'baseline-digests.json').exists():
                saved = ROOT / 'seeds' / (time.strftime('%Y%m%dT%H%M%S', time.gmtime()) + '-' + uuid.uuid4().hex[:8])
                saved.mkdir(parents=True)
                for name in ('baseline-digests', 'checkpoint', 'viewer-placement'):
                    shutil.copy2(ROOT / (name + '.json'), saved / (name + '.json'))
                (ROOT / 'baseline-digests.json').unlink()
            for stage in ('configure', 'services', 'terminals', 'chrome', 'applications', 'viewer', 'place', 'checkpoint'):
                write('seed-progress', {'stage': stage, 'time': time.time()})
                print('Scale seed: ' + stage, flush=True)
                globals()[stage]()
        elif args.phase == 'reboot':
            cycle(reboot_guest=True)
        else:
            globals()[args.phase.replace('-', '_')]()
    except Exception as error:
        write('failure-' + args.phase, {'error': type(error).__name__ + ': ' + str(error),
                                      'time': time.time()})
        if args.phase == 'verify' and (ROOT / 'cycle-before.json').exists():
            archive_cycle()
        raise


if __name__ == '__main__':
    main()
