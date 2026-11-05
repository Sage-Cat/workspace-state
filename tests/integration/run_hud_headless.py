#!/usr/bin/env python3
"""Opt-in real GNOME HUD startup tests on a private bus and headless display.

The HUD presents startup only; GNOME power-off stays native and a status
document of any other mode must be ignored.
"""
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
        results.append({'case': case, 'passed': True, 'state': state, **extra})
        (output / 'results.json').write_text(json.dumps({'isolated': True, 'running': True, 'cases': results}, indent=2))

    def released(state):
        return state['modalCount'] == baseline and not state['reactive']

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
            initial = until(lambda state: state['baseline'] is not None)
            baseline = initial['baseline']
            assert isinstance(baseline, int), initial
            assert initial['modalCount'] == baseline and not initial['visible'], initial
            build = running_build()
            assert build['uuid'] == HUD_UUID and build['revision'] == REVISION, build
            record('passive-enable-restores-no-modal-grab', initial, build=build)

            # A status document of a retired mode is never presented, and it
            # takes no modal grab or input from the session.
            control('publish', mode='shutdown', operation='7' * 32, state='running')
            time.sleep(.5)
            ignored = control()
            assert ignored['operation'] is None and not ignored['visible'] and not ignored['mapped'], ignored
            assert released(ignored) and not ignored['statusIndicatorVisible'], ignored
            record('shutdown-mode-status-is-ignored', ignored)

            # The GNOME unlock-dialog session mode is a session boundary: the
            # startup HUD must hide there, take no grab and never reserve input.
            control('publish', mode='startup', operation='e' * 32, state='running')
            shown = until(lambda state: state['operation'] == 'e' * 32 and state['visible'])
            assert released(shown), shown
            control('enter-lock-mode')
            locked = until(lambda state: state['sessionMode'] == 'unlock-dialog' and state['locked'] and
                           state['actorPresent'] and not state['visible'] and not state['statusIndicatorVisible'])
            assert released(locked), locked
            record('startup-hud-hides-at-the-lock-screen-without-a-grab', locked)
            control('leave-lock-mode')
            until(lambda state: state['sessionMode'] == 'user' and not state['locked'])

            # Disabling the extension while a Gio status load is in flight must
            # leave nothing behind; a re-enabled instance rejects the old epoch.
            disabled = control('disable-pending')
            assert released(disabled) and not disabled['actorPresent'], disabled
            record('disable-with-pending-gio-load-leaves-no-actor-or-grab', disabled)
            control('enable')
            enabled = until(lambda state: state['actorPresent'] and state['operation'] == 'e' * 32)
            time.sleep(.5)
            enabled = control()
            assert released(enabled) and enabled['currentEpochOwnsOld'] is False, enabled
            record('reenable-rejects-the-old-epoch-and-reloads-startup-status', enabled)
            # Exercise actual finite progress while mapped, resized and rebuilt.
            # Geometry observation itself must never allocate an unattached actor.
            publication = None
            for fraction, width in ((0, 1280), (.25, 1040), (.75, 1280), (1, 1100)):
                values = first_context(publication) if publication else {}
                publication = control('publish', mode='startup', operation='a' * 32, state='running', stages=[
                    {'id': 'workspace-save', 'label': 'Workspace progress', 'state': 'running', 'message': 'Synthetic finite progress', 'fraction': fraction},
                    {'id': 'fixture-progress', 'label': 'Second progress', 'state': 'running', 'message': 'Synthetic second progress', 'fraction': fraction},
                ], **values)
                until(lambda state: state['operation'] == 'a' * 32 and state['mapped'])
                control('layout', workArea={'x': 0, 'y': 0, 'width': width, 'height': 720})
                def correct_progress(state):
                    bars = [state.get('overallProgress'), *state.get('progressRows', [])]
                    return len(bars) == 3 and all(bar and bar['trackWidth'] > 0 and
                        abs(bar['fillWidth'] - bar['trackWidth'] * fraction) <= 2 and
                        (fraction == 0 or abs(bar['leftOffset']) <= 1) for bar in bars)
                progress = until(correct_progress)
            record('finite-progress-survives-native-resize-and-row-reconstruction', progress)
            startup = control('publish', mode='startup', operation='b' * 32, state='running')
            shown = until(lambda state: state['operation'] == 'b' * 32 and state['visible'])
            assert shown['statusIndicatorVisible'] and 'Abort restoration' in shown['actionLabels'], shown
            assert 'Run in background' in shown['actionLabels'], shown
            record('startup-accessible-actions-and-status-indicator', shown)
            control('publish', mode='shutdown', operation='7' * 32, state='running')
            time.sleep(.5)
            still = control()
            assert still['operation'] == 'b' * 32 and still['visible'] and still['titleText'] == shown['titleText'], still
            record('later-shutdown-mode-status-leaves-startup-presentation-unchanged', still)
            control('reopen-startup')
            focused = until(lambda state: state['focusLabel'] == 'Abort restoration')
            assert focused['focusGroupRegistered'], focused
            control('native-key', key='Tab')
            tabbed = until(lambda state: state['focusLabel'] == 'Run in background')
            record('native-tab-from-abort-reaches-background', tabbed)
            control('native-key', key='Return')
            hidden = until(lambda state: not state['visible'])
            assert hidden['dismissal']['reason'] == 'background' and not hidden['startupAbortPending'], hidden
            record('background-hides-hud-without-abort', hidden)
            control('publish', mode='startup', operation='b' * 32, state='running', **first_context(startup))
            time.sleep(.3)
            assert not control()['visible']
            control('reopen-startup')
            reopened = until(lambda state: state['visible'])
            assert not reopened['dismissal'], reopened
            record('panel-reopens-current-background-restoration', reopened)
            failed_write = control('startup-abort', failWrite=True)
            assert not failed_write['startupAbortPending'] and not failed_write['startupAbortReceipt'], failed_write
            record('abort-write-failure-keeps-restoration-running-and-action-retryable', failed_write)
            control('reopen-startup')
            until(lambda state: state['focusLabel'] == 'Abort restoration')
            control('native-key', key='Return')
            requested = until(lambda state: state['startupAbortPending'])
            record('native-return-activates-startup-abort', requested)
            repeated = control('startup-abort')
            assert requested['startupAbortReceipt'] == repeated['startupAbortReceipt'], repeated
            assert repeated['startupAbortPending'] and repeated['startupAbortReceipt']['operation_context'] == startup['context'], repeated
            record('repeated-abort-clicks-write-one-exact-context-receipt', repeated)
            control('publish', mode='startup', operation='b' * 32, state='degraded', cancelled=True,
                    operationState='cancelled', **first_context(startup))
            stopped = until(lambda state: state['titleText'] == 'Restoration stopped')
            assert not stopped['startupAbortPending'], stopped
            record('backend-cancelled-startup-is-stopped', stopped)

            (output / 'final-hud.json').write_text(json.dumps(control(), indent=2))
    except Exception as error:
        results.append({'case': 'integration-error', 'passed': False, 'error': f'{type(error).__name__}: {error}'})
        return_code = 1
    else:
        return_code = 0
    finally:
        for process in reversed(processes):
            stop(process)
        messages = (output / 'gnome-shell.log').read_text().splitlines()
        diagnostic_terms = ('needs an allocation', 'st_widget_get_theme_node',
                            'invalid (NULL) pointer instance', 'g_signal_connect_object: assertion', 'JS ERROR')
        diagnostics = {term: sum(term in line for line in messages) for term in diagnostic_terms}
        rendering_ok = not any(diagnostics.values())
        results.append({'case': 'hud-native-rendering-diagnostics', 'passed': rendering_ok,
                        'message_counts': diagnostics})
        if not rendering_ok:
            return_code = 1
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
        for name in ('extension.js', 'reports.js', 'hudView.js', 'stylesheet.css', 'metadata.json', 'buildInfo.js'):
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
