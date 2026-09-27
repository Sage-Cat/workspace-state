#!/usr/bin/env python3
"""Opt-in, private-bus headless GNOME placement integration and fault injection.

No host extension activation, real app profiles, Codex resume, or VM operations.
The test-only control method is injected into a temporary extension copy.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

UUID = 'gnome-winctl-v3@sagecat.local'
TEST_METHOD = '''
    TestControl(requestJson) {
        const request = JSON.parse(requestJson);
        if (request.action === 'ready')
            return JSON.stringify({ready: !Main.layoutManager._startingUp});
        if (request.action === 'setup') {
            Main.overview.hide();
            new Gio.Settings({schema_id: 'org.gnome.mutter'}).set_boolean('dynamic-workspaces', false);
            new Gio.Settings({schema_id: 'org.gnome.mutter'}).set_boolean('workspaces-only-on-primary', false);
            new Gio.Settings({schema_id: 'org.gnome.desktop.wm.preferences'}).set_int('num-workspaces', 3);
            while (global.workspace_manager.n_workspaces < 3)
                global.workspace_manager.append_new_workspace(false, global.get_current_time());
        } else if (request.action === 'activate')
            global.workspace_manager.get_workspace_by_index(request.workspace).activate(global.get_current_time());
        else if (request.action === 'fail-next') {
            const original = this._applyResolvedPlacement;
            this._applyResolvedPlacement = (window, target) => {
                this._applyResolvedPlacement = original;
                throw new Error('injected isolated compositor failure');
            };
        } else throw new Error('unsupported test control');
        return JSON.stringify({ok: true});
    }
'''


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def inside(root: Path, output: Path, companions: bool = False) -> int:
    if Path(os.environ['XDG_RUNTIME_DIR']) != root / 'runtime' or str(root / 'runtime') not in os.environ.get('DBUS_SESSION_BUS_ADDRESS', ''):
        raise RuntimeError('private runtime and private session bus are required')
    results = []
    processes = []

    def call(method, *arguments):
        command = ['gdbus', 'call', '--session', '--dest', 'org.sagecat.GnomeWinCtl1',
                   '--object-path', '/org/sagecat/GnomeWinCtl1', '--method', f'org.sagecat.GnomeWinCtl1.{method}', *arguments]
        result = subprocess.run(command, capture_output=True, text=True, timeout=3)
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
        import ast
        value = ast.literal_eval(result.stdout.replace('(true,)', '(True,)').replace('(false,)', '(False,)'))[0]
        return json.loads(value) if method not in {'ExpectWindow', 'CancelExpectation'} else value

    def until(function, *, timeout=20):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = function()
                if last:
                    return last
            except RuntimeError as error:
                last = str(error)
            time.sleep(.1)
        raise RuntimeError(f'condition not met: {last}')

    def state():
        return call('GetState')

    def status(token):
        return call('ExpectationStatus', token)

    def terminal(token, expected):
        value = status(token)
        if value['status'] == expected:
            return value
        if value['status'] in {'failed', 'cancelled', 'expired', 'unknown', 'verified'}:
            raise ValueError(f'expected {expected}: {value}')
        return None

    def place(identifier, workspace, monitor=0):
        return call('PlaceWindow', json.dumps({'id': identifier}), json.dumps({
            'workspace': workspace, 'monitor': monitor,
            'geometry': {'x': 40, 'y': 60, 'width': 480, 'height': 320},
            'coordinate_space': 'monitor', 'state': 'normal', 'clamp': True,
        }))

    try:
        with (output / 'gnome-shell.log').open('w') as log:
            shell = subprocess.Popen(['gnome-shell', '--headless', '--wayland', '--no-x11', '--sm-disable',
                '--wayland-display=wsctl-integration', '--virtual-monitor=1280x720',
                '--virtual-monitor=1024x768', '--virtual-monitor=800x600'], stdout=log, stderr=subprocess.STDOUT)
            processes.append(shell)
            desktop = until(lambda: state() if shell.poll() is None else (_ for _ in ()).throw(RuntimeError(f'Shell exited {shell.returncode}')), timeout=30)
            until(lambda: call('TestControl', json.dumps({'action': 'ready'}))['ready'], timeout=15)
            call('TestControl', json.dumps({'action': 'setup'}))
            desktop = until(lambda: (value if len(value['workspaces']) == 3 else None) if (value := state()) else None, timeout=10)
            assert len(desktop['monitors']) == 3, desktop['monitors']
            assert len(desktop['workspaces']) == 3, desktop['workspaces']
            assert desktop['build']['revision'] == 'headless-integration', desktop['build']
            results.append({'case': 'private-gnome-three-monitors', 'passed': True, 'build': desktop['build']})
            with (output / 'application.log').open('w') as app_log:
                app = subprocess.Popen([sys.executable, str(Path(__file__).with_name('window_fixture.py'))], stdout=app_log, stderr=subprocess.STDOUT)
                processes.append(app)
                window = until(lambda: next((item for item in state()['windows'] if item['title'] == 'Workspace integration fixture'), None))
                identifier = window['id']
                result = place(identifier, 0, 1)
                verified = until(lambda: terminal(result['token'], 'verified'))
                assert verified['window']['monitor'] == 1
                results.append({'case': 'native-window-verified-placement', 'passed': True})

                result = place(identifier, 1, 2)
                assert result['status'] == 'deferred' and result['placed'] is False, result
                assert state()['active_workspace'] == 0, 'placement stole workspace activation'
                call('TestControl', json.dumps({'action': 'activate', 'workspace': 1}))
                verified = until(lambda: terminal(result['token'], 'verified'))
                assert verified['window']['monitor'] == 2
                results.append({'case': 'deferred-placement-owned-through-activation', 'passed': True})

                result = place(identifier, 2)
                assert result['status'] == 'deferred', result
                call('TestControl', json.dumps({'action': 'fail-next'}))
                call('TestControl', json.dumps({'action': 'activate', 'workspace': 2}))
                failed = until(lambda: terminal(result['token'], 'failed'))
                assert 'injected' in failed['message'], failed
                results.append({'case': 'replay-failure-reaches-original-token', 'passed': True})

                result = place(identifier, 0)
                assert result['status'] == 'deferred', result
                before_cancel = next(item for item in state()['windows'] if item['id'] == identifier)
                assert call('CancelExpectation', result['token']) is True
                call('TestControl', json.dumps({'action': 'activate', 'workspace': 0}))
                time.sleep(.5)
                assert status(result['token'])['status'] == 'cancelled'
                after_cancel = next(item for item in state()['windows'] if item['id'] == identifier)
                assert after_cancel['monitor'] == before_cancel['monitor'], (before_cancel, after_cancel)
                assert after_cancel['geometry'] == before_cancel['geometry'], (before_cancel, after_cancel)
                results.append({'case': 'cancelled-placement-does-not-replay', 'passed': True})
                if companions:
                    from companion_smoke import run
                    results.extend(run(root, output))
                (output / 'final-desktop.json').write_text(json.dumps(state(), indent=2))
    except Exception as error:
        results.append({'case': 'integration-error', 'passed': False, 'error': f'{type(error).__name__}: {error}'})
        return_code = 1
    else:
        return_code = int(any(item.get('passed') is False for item in results))
    finally:
        for process in reversed(processes):
            stop(process)
        (output / 'results.json').write_text(json.dumps({'isolated': True, 'cases': results}, indent=2))
    return return_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='explicitly run the isolated headless desktop')
    parser.add_argument('--companions', action='store_true', help='also test installed apps with disposable profiles')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--inside', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inside:
        return inside(args.inside, args.output, args.companions)
    if not args.run:
        parser.error('--run is required; this is not part of ordinary unit discovery')
    for command in ('gnome-shell', 'dbus-run-session', 'gdbus', 'glib-compile-schemas'):
        if not shutil.which(command):
            raise RuntimeError(f'missing prerequisite: {command}')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.chmod(0o700)
    (output / 'results.json').write_text(json.dumps({'isolated': True, 'running': True, 'cases': []}))
    (output / 'final-desktop.json').unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix='wsctl-headless-', dir='/tmp') as temporary:
        root = Path(temporary)
        for name in ('runtime', 'config', 'data', 'cache', 'state', 'schemas'):
            (root / name).mkdir(mode=0o700)
        source = Path(__file__).resolve().parents[3] / 'gnome-winctl/extension' / UUID
        installed = root / 'data/gnome-shell/extensions' / UUID
        shutil.copytree(source, installed)
        extension = installed / 'extension.js'
        source_text = extension.read_text()
        assert source_text.count('<method name="GetCapabilities">') == 1
        assert source_text.count('    GetCapabilities() {') == 1
        text = source_text.replace('<method name="GetCapabilities">', '<method name="TestControl"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>\n    <method name="GetCapabilities">')
        extension.write_text(text.replace('    GetCapabilities() {', TEST_METHOD + '\n    GetCapabilities() {'))
        (installed / 'buildInfo.js').write_text("export const BUILD_REVISION = 'headless-integration';\n")
        for schema in Path('/usr/share/glib-2.0/schemas').iterdir():
            if schema.name.endswith(('.xml', '.override')):
                (root / 'schemas' / schema.name).symlink_to(schema)
        (root / 'schemas/zz-wsctl-integration.gschema.override').write_text(f'''[org.gnome.shell]
enabled-extensions=['{UUID}']
disable-user-extensions=false
[org.gnome.mutter]
dynamic-workspaces=false
[org.gnome.desktop.wm.preferences]
num-workspaces=3
workspace-names=['Integration One','Integration Two','Integration Three']
''')
        subprocess.run(['glib-compile-schemas', '--strict', str(root / 'schemas')], check=True, timeout=10)
        # No standard service directories: the private bus cannot autolaunch a
        # host-profile service or delegate service startup to the user manager.
        config = root / 'session-bus.conf'
        config.write_text(f'''<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN" "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig><type>session</type><listen>unix:tmpdir={root / 'runtime'}</listen><auth>EXTERNAL</auth>
<policy context="default"><allow send_destination="*"/><allow receive_sender="*"/><allow own="*"/></policy></busconfig>''')
        env = dict(os.environ, XDG_RUNTIME_DIR=str(root / 'runtime'), XDG_CONFIG_HOME=str(root / 'config'),
            XDG_DATA_HOME=str(root / 'data'), XDG_CACHE_HOME=str(root / 'cache'), XDG_STATE_HOME=str(root / 'state'),
            XDG_CONFIG_DIRS=str(root / 'config'), XDG_DATA_DIRS='/usr/local/share:/usr/share',
            WAYLAND_DISPLAY='wsctl-integration', DISPLAY='', GDK_BACKEND='wayland',
            GSETTINGS_BACKEND='memory', GSETTINGS_SCHEMA_DIR=str(root / 'schemas'),
            GIO_USE_VFS='local', GVFS_DISABLE_FUSE='1', NO_AT_BRIDGE='1', GTK_A11Y='none',
            IBUS_ADDRESS='unix:path=' + str(root / 'runtime/no-ibus'), GTK_IM_MODULE='gtk-im-context-simple',
            LIBGL_ALWAYS_SOFTWARE='1', GALLIUM_DRIVER='llvmpipe',
            GNOME_SHELL_SESSION_MODE='gnome', XDG_SESSION_TYPE='wayland', XDG_CURRENT_DESKTOP='GNOME')
        for key in ('DBUS_SESSION_BUS_ADDRESS', 'GNOME_SETUP_DISPLAY', 'SESSION_MANAGER', 'DESKTOP_AUTOSTART_ID'):
            env.pop(key, None)
        with (output / 'driver.log').open('w') as log:
            process = subprocess.Popen(['dbus-run-session', '--config-file', str(config), '--', sys.executable,
                str(Path(__file__).resolve()), '--inside', str(root), '--output', str(output),
                *(['--companions'] if args.companions else [])],
                env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                return_code = process.wait(timeout=180 if args.companions else 100)
            except subprocess.TimeoutExpired:
                return_code = 124
                (output / 'results.json').write_text(json.dumps({'isolated': True, 'cases': [
                    {'case': 'outer-deadline', 'passed': False, 'error': 'private desktop exceeded its outer deadline'},
                ]}, indent=2))
            finally:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
                # The bus leader can exit before another private child. Always
                # terminate any remaining members of this owned process group.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)
        return return_code


if __name__ == '__main__':
    raise SystemExit(main())
