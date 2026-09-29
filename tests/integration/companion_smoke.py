"""Opt-in real application companions, callable only by the private GNOME harness."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import select
import signal
import subprocess
import sys
import time

REPOSITORY = Path(__file__).resolve().parents[2]


class SkipCompanion(RuntimeError):
    pass


def wait_for(function, seconds=20):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            result = function()
            if result:
                return result
        except (OSError, RuntimeError) as error:
            last = str(error)
        time.sleep(.1)
    raise RuntimeError(f'companion readiness deadline: {last}')


def run(root: Path, output: Path) -> list[dict]:
    if (os.environ.get('WAYLAND_DISPLAY') != 'wsctl-integration'
            or Path(os.environ.get('XDG_RUNTIME_DIR', '')) != root / 'runtime'
            or str(root / 'runtime') not in os.environ.get('DBUS_SESSION_BUS_ADDRESS', '')
            or not root.name.startswith('wsctl-headless-')):
        raise RuntimeError('refusing companion smoke outside the private headless desktop')
    # Editor-terminal routing variables can override normal CLI profile selection.
    for key in list(os.environ):
        if key.startswith('VSCODE_') or key in {'ELECTRON_RUN_AS_NODE', 'CHROME_USER_DATA_DIR', 'CHROME_CONFIG_HOME', 'CHROME_LOG_FILE'}:
            os.environ.pop(key, None)
    sys.path.insert(0, str(REPOSITORY / 'src'))
    binary = root / 'bin'
    binary.mkdir(exist_ok=True)
    # Source checkout transport, still pointed exclusively at the private bus.
    winctl = binary / 'gnome-winctl'
    winctl.write_text(f'#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(REPOSITORY.parent / "gnome-winctl/src")!r})\nfrom gnome_winctl.cli import main\nraise SystemExit(main())\n')
    winctl.chmod(0o700)
    os.environ['PATH'] = str(binary) + os.pathsep + os.environ['PATH']
    results = []
    (output / 'companion-results.json').write_text('[]')
    for name, function in [('chrome', chrome), ('vscode', code), ('nemo', nemo)]:
        try:
            details = function(root, output)
            results.append({'case': name + '-companion', 'passed': True, **details})
        except SkipCompanion as error:
            results.append({'case': name + '-companion', 'skipped': True, 'reason': str(error)})
        except Exception as error:
            results.append({'case': name + '-companion', 'passed': False, 'error': f'{type(error).__name__}: {error}'})
        (output / 'companion-results.json').write_text(json.dumps(results, indent=2))
    return results


def launch(command, output, name):
    # Inherit the outer private process group, so its hard deadline covers
    # renderer and extension-host descendants even when a CLI launcher exits.
    log = (output / (name + '.log')).open('w')
    try:
        return subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
    finally:
        log.close()


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def chrome(root, output):
    from workspace_state import browser, desktop, deployment
    executable = shutil.which('google-chrome')
    if not executable:
        raise SkipCompanion('google-chrome is not installed')
    profile = root / 'chrome-profile'
    profile.mkdir()
    (profile / 'Default').mkdir()
    (profile / 'Default/Preferences').write_text(json.dumps({
        'extensions': {'ui': {'developer_mode': True}},
    }))
    extension = root / 'data/workspace-state/chrome-extension'
    extension.parent.mkdir(parents=True, exist_ok=True)
    initial_release, upgraded_release = root / 'chrome-release-old', root / 'chrome-release-new'
    current_release = root / 'chrome-current'
    shutil.copytree(REPOSITORY / 'chrome-extension', initial_release)
    current_release.symlink_to(initial_release)
    extension.mkdir()
    for asset in initial_release.iterdir():
        (extension / asset.name).symlink_to(current_release / asset.name)
    (extension / 'build-info.json').symlink_to(current_release / 'build-info.json')
    initial_revision, upgraded_revision = 'r-' + '1' * 24, 'r-' + '2' * 24
    (extension / 'buildInfo.js').write_text(f"globalThis.WSCTL_BUILD_REVISION = {initial_revision!r};\n")
    (extension / 'build-info.json').write_text(json.dumps({'revision': initial_revision}))
    worker_spec = {'manifest': 'manifest.json', 'source': 'service-worker.js'}
    deployment._package_chrome_worker(initial_release, worker_spec, initial_revision)
    initial_worker = f'service-worker-{initial_revision}.js'
    (extension / initial_worker).symlink_to(current_release / initial_worker)
    (output / 'native-host.log').write_text('')
    host = root / 'bin/native-host'
    host.write_text(f'''#!{sys.executable}
import os, sys
from pathlib import Path
root = Path({str(root)!r})
if os.environ.get('XDG_RUNTIME_DIR') != str(root / 'runtime') or (root / 'host-disabled').exists():
    raise SystemExit(1)
sys.stderr = open({str(output / 'native-host.log')!r}, 'a', buffering=1)
(root / 'native-host.pid').write_text(str(os.getpid()))
sys.path.insert(0, {str(REPOSITORY / 'src')!r})
from workspace_state.native_host import serve
print('private native host started', os.getpid(), file=sys.stderr)
result = serve()
print('private native host exited', result, file=sys.stderr)
raise SystemExit(result)
''')
    host.chmod(0o700)
    manifest = json.loads((REPOSITORY / 'native-messaging/org.sagecat.workspace_state.json.in').read_text())
    manifest['path'] = str(host)
    for directory in (profile / 'NativeMessagingHosts', root / 'config/google-chrome/NativeMessagingHosts'):
        directory.mkdir(parents=True)
        (directory / 'org.sagecat.workspace_state.json').write_text(json.dumps(manifest))
    page = root / 'duplicate-title.html'
    page.write_text('<!doctype html><title>Identical integration title</title><p>Private file fixture</p>')
    command = [executable, f'--user-data-dir={profile}', '--profile-directory=Default',
               '--ozone-platform=wayland', '--no-first-run', '--no-default-browser-check',
               '--password-store=basic', '--disable-background-networking', '--disable-component-update',
               '--disable-sync', '--metrics-recording-only', '--disable-default-apps',
               '--proxy-server=http://127.0.0.1:9', '--proxy-bypass-list=<-loopback>',
               '--disable-features=MediaRouter,OptimizationHints', '--enable-logging=stderr',
               '--new-window', page.as_uri()]
    # Private inherited pipes expose no TCP debugging listener. The installed
    # Chrome build may forbid both CLI and DevTools unpacked loading; that is
    # an explicit unsupported capability, never permission to use a real profile.
    input_read, input_write = os.pipe()
    output_read, output_write = os.pipe()
    trampoline = "import os,sys; i=os.dup(int(sys.argv[1])); o=os.dup(int(sys.argv[2])); os.dup2(i,3); os.dup2(o,4); os.set_inheritable(3,True); os.set_inheritable(4,True); os.close(i) if i>4 else None; os.close(o) if o>4 else None; os.execv(sys.argv[3],sys.argv[3:])"
    with (output / 'chrome.log').open('w') as log:
        process = subprocess.Popen([sys.executable, '-c', trampoline, str(input_read), str(output_write),
                                    *command, '--remote-debugging-pipe', '--enable-unsafe-extension-debugging'],
                                   pass_fds=(input_read, output_write), stdout=log, stderr=subprocess.STDOUT)
    os.close(input_read)
    os.close(output_write)
    try:
        os.write(input_write, json.dumps({'id': 1, 'method': 'Extensions.loadUnpacked',
                                         'params': {'path': str(extension)}}).encode() + b'\0')
        deadline = time.monotonic() + 10
        received = bytearray()
        loaded = None
        while time.monotonic() < deadline and loaded is None:
            if not select.select([output_read], [], [], max(0, deadline - time.monotonic()))[0]:
                break
            chunk = os.read(output_read, 65536)
            if not chunk:
                break
            received.extend(chunk)
            if len(received) > 4 * 1024 * 1024:
                raise RuntimeError('oversized private DevTools reply')
            while b'\0' in received:
                line, _, remainder = received.partition(b'\0')
                received = bytearray(remainder)
                reply = json.loads(line)
                if reply.get('id') == 1:
                    loaded = reply
                    break
        (output / 'chrome-extension-load.json').write_text(json.dumps(loaded, indent=2))
        if loaded and loaded.get('error'):
            error = loaded['error']
            reason = str(error)
            if error.get('code') == -32601 or any(text in reason.lower() for text in
                    ('not supported', 'not allowed', 'only supported', 'enable-unsafe-extension-debugging')):
                raise SkipCompanion('Installed Chrome cannot load the temporary extension: ' + reason)
            raise RuntimeError('Temporary extension failed to load: ' + reason)
        if loaded is None:
            raise RuntimeError('Chrome private DevTools pipe did not answer; see chrome.log')
        try:
            ping = wait_for(lambda: browser.request_browser('ping', profile='Default', timeout=.5), seconds=12)
        except RuntimeError:
            (output / 'chrome-private-profile.json').write_text(json.dumps({
                'host_started': (root / 'native-host.pid').exists(),
                'runtime_files': [str(path.relative_to(root)) for path in (root / 'runtime').rglob('*')],
            }, indent=2))
            raise
        assert ping['build']['revision'] == initial_revision, ping
        second = subprocess.run(command, capture_output=True, text=True, timeout=5)
        assert second.returncode == 0, second.stderr
        windows = wait_for(lambda: (items if len(items) == 2 else None)
                           if (items := browser.request_browser('list_windows', profile='Default', timeout=1)) else None)
        # Keep the registered path and running worker. Change both its imported
        # stamp and main script, then reconnect the private native host: a cached
        # activation-aware worker must reload itself without touching user tabs.
        before_upgrade = browser.request_browser('capture', profile='Default', timeout=1)
        shutil.copytree(initial_release, upgraded_release)
        (upgraded_release / initial_worker).unlink()
        worker = upgraded_release / 'service-worker.js'
        worker.write_text(worker.read_text().replace("'installed_build_activation',",
                                                    "'installed_build_activation', 'upgrade_fixture',"))
        (upgraded_release / 'buildInfo.js').write_text(f"globalThis.WSCTL_BUILD_REVISION = {upgraded_revision!r};\n")
        (upgraded_release / 'build-info.json').write_text(json.dumps({'revision': upgraded_revision}))
        deployment._package_chrome_worker(upgraded_release, worker_spec, upgraded_revision)
        upgraded_worker = f'service-worker-{upgraded_revision}.js'
        (extension / upgraded_worker).symlink_to(current_release / upgraded_worker)
        replacement = root / 'chrome-next'
        replacement.symlink_to(upgraded_release)
        replacement.replace(current_release)
        cached = browser.request_browser('ping', profile='Default', timeout=1)
        assert cached['build']['revision'] == initial_revision, cached
        assert 'upgrade_fixture' not in cached['capabilities'], cached
        pid = int((root / 'native-host.pid').read_text())
        descriptor = os.pidfd_open(pid)
        try:
            assert f'XDG_RUNTIME_DIR={root / "runtime"}'.encode() in Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        finally:
            os.close(descriptor)
        def upgraded():
            result = browser.request_browser('ping', profile='Default', timeout=.5)
            return result if result['build']['revision'] == upgraded_revision else None
        ping = wait_for(upgraded, seconds=20)
        assert 'upgrade_fixture' in ping['capabilities'], ping
        after_upgrade = browser.request_browser('capture', profile='Default', timeout=1)
        def tab_identity(capture):
            return [(window['runtime_window_id'], [
                {key: tab[key] for key in ('url', 'pinned', 'active', 'group')}
                for tab in window['tabs']]) for window in capture['windows']]
        assert tab_identity(after_upgrade) == tab_identity(before_upgrade), (before_upgrade, after_upgrade)
        native = wait_for(lambda: (items if len(items) == 2 and len({item['title'] for item in items}) == 1 else None)
                          if (items := browser._shell_browser_windows(desktop.capture_shell(), 'google-chrome')) else None)
        assert 'Identical integration title' in native[0]['title'], native
        mapping = {}
        for window in windows:
            native_id, lease = browser._identify_native_window(profile='Default', chrome_window_id=window['id'],
                                                               app_id='google-chrome', timeout=3, preserve_focus=True)
            try:
                mapping[str(window['id'])] = native_id
            finally:
                browser.request_browser('release_window_identification', {**lease, 'focus': False}, profile='Default', timeout=1)
        assert len(set(mapping.values())) == 2, mapping
        assert set(mapping.values()) == {item['id'] for item in native}, (mapping, native)
        # Only the wrapper in this private profile can write this PID receipt.
        # Disable reconnection, then kill that exact process through a pidfd.
        (root / 'host-disabled').touch()
        pid = int((root / 'native-host.pid').read_text())
        descriptor = os.pidfd_open(pid)
        try:
            process_env = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
            assert f'XDG_RUNTIME_DIR={root / "runtime"}'.encode() in process_env
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        finally:
            os.close(descriptor)
        try:
            browser.request_browser('identify_window', {'window_id': windows[0]['id'], 'token': 'lost-companion'}, profile='Default', timeout=1)
        except browser.BrowserUnavailable:
            pass
        else:
            raise AssertionError('lost companion falsely acknowledged identity')
        after_loss = desktop.capture_shell()
        loss_windows = browser._shell_browser_windows(after_loss, 'google-chrome')
        assert {item['id'] for item in loss_windows} == set(mapping.values()), loss_windows
        try:
            browser.capture_browser(shell=after_loss, names=[item['name'] for item in after_loss['workspaces']])
        except browser.BrowserUnavailable as error:
            assert 'exact companion identity; checkpoint preserved' in str(error), str(error)
        else:
            raise AssertionError('capture accepted native Chrome windows without companion identity')
        # Refusal must preserve the existing windows, without replacing them or
        # moving them to manufacture a capture match after companion loss.
        observed = browser._shell_browser_windows(desktop.capture_shell(), 'google-chrome')
        keys = ('id', 'workspace', 'monitor', 'state', 'geometry')
        before = {item['id']: {key: item.get(key) for key in keys} for item in loss_windows}
        after = {item['id']: {key: item.get(key) for key in keys} for item in observed}
        assert after == before, (before, after)
        return {'duplicate_title_native_mapping': mapping, 'companion_loss_refused': True,
                'native_windows_preserved': True, 'build': ping['build'],
                'cached_worker_same_path_upgrade': True, 'upgrade_tabs_preserved': True}
    finally:
        os.close(input_write)
        os.close(output_read)
        stop(process)


def code(root, output):
    from workspace_state import vscode
    executable = shutil.which('code')
    if not executable:
        raise SkipCompanion('VS Code is not installed')
    profile = root / 'code-profile'
    (profile / 'User').mkdir(parents=True)
    (profile / 'User/settings.json').write_text(json.dumps({
        'telemetry.telemetryLevel': 'off', 'update.mode': 'none', 'update.showReleaseNotes': False,
        'extensions.autoUpdate': False, 'extensions.autoCheckUpdates': False,
        'workbench.startupEditor': 'none', 'workbench.enableExperiments': False,
        'security.workspace.trust.enabled': False, 'git.enabled': False,
        'http.proxy': 'http://127.0.0.1:9', 'http.proxySupport': 'override',
        'files.hotExit': 'onExitAndWindowClose', 'window.restoreWindows': 'none',
    }))
    extensions = root / 'code-extensions'
    extensions.mkdir()
    companion = extensions / 'sagecat.workspace-state-companion-0.1.0'
    shutil.copytree(REPOSITORY / 'vscode-extension', companion)
    (companion / 'build-info.json').write_text(json.dumps({'revision': 'headless-companion'}))
    project = root / 'disposable-project'
    project.mkdir()
    editor = project / 'smoke.txt'
    editor.write_text('Disposable metadata-only editor fixture.\n')
    process = launch([executable, '--user-data-dir', str(profile), '--extensions-dir', str(extensions),
                      '--new-window', '--wait', '--ozone-platform=wayland', '--password-store=basic',
                      '--proxy-server=http://127.0.0.1:9', '--proxy-bypass-list=<-loopback>',
                      '--skip-welcome', '--skip-release-notes', '--disable-workspace-trust',
                      str(project), str(editor)], output, 'vscode')
    try:
        states = wait_for(lambda: vscode._states(time.monotonic() + 1), seconds=30)
        assert len(states) == 1, states
        state = states[0]
        assert state['user_data_dir'] == str(profile), state
        assert state['profile'] == {'id': 'default', 'name': 'Default'}, state
        assert [item['uri'] for item in state['folders']] == [project.as_uri()], state
        wait_for(lambda: editor.as_uri() in vscode._request(state['endpoint'], 'state')['editor_uris'])
        assert state['build']['revision'] == 'headless-companion', state
        live = vscode._LiveWindows().get(time.monotonic() + 5)
        assert len(live) == 1 and live[0]['user_data_dir'] == str(profile), live
        assert vscode._request(state['endpoint'], 'probe')['ready'] is True
        return {'profile': state['profile'], 'editor_uri': editor.as_uri(), 'native_window_id': live[0]['shell']['id'], 'build': state['build']}
    finally:
        stop(process)


def nemo(root, output):
    from workspace_state import file_manager, desktop
    executable = shutil.which('nemo')
    if not executable:
        raise SkipCompanion('Nemo is not installed')
    system_python = Path('/usr/share/nemo-python/extensions')
    if system_python.exists() and any(system_python.glob('*.py')):
        raise SkipCompanion('System Nemo Python extensions cannot be excluded from this temporary profile')
    native_extensions = Path('/usr/lib/x86_64-linux-gnu/nemo/extensions-3.0')
    if any(path.name not in {'libnemo-python.so', 'libnemo-fileroller.so'} for path in native_extensions.glob('*.so')):
        raise SkipCompanion('Unreviewed system Nemo native extensions prevent isolated smoke launch')
    installed = root / 'data/nemo-python/extensions'
    installed.mkdir(parents=True)
    shutil.copy2(REPOSITORY / 'nemo-extension/wsctl_nemo_bridge.py', installed)
    (installed / 'build-info.json').write_text(json.dumps({'revision': 'headless-companion'}))
    folders = [root / 'nemo-one', root / 'nemo-two']
    for folder in folders:
        folder.mkdir()
    process = launch([executable, '--no-default-window', '--tabs', *map(str, folders)], output, 'nemo')
    try:
        def ready():
            state = file_manager._bridge('GetState')
            return state if state.get('windows') and all(item['complete'] for item in state['windows']) else None
        state = wait_for(ready, seconds=15)
        assert state['pid'] == process.pid, state
        assert state['build']['revision'] == 'headless-companion', state
        assert len(state['windows']) == 1, state
        assert state['windows'][0]['locations'] == [folder.as_uri() for folder in folders], state
        captured = file_manager.capture_file_manager(desktop.capture_shell())
        assert len(captured['windows']) == 1, captured
        assert captured['windows'][0]['locations'] == [folder.as_uri() for folder in folders], captured
        return {'tab_uris': captured['windows'][0]['locations'], 'build': state['build']}
    finally:
        stop(process)
