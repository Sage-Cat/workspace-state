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
        switch = None
        if 'workspace_switch' in ping['capabilities'] and 'close_window' in desktop.capture_shell()['capabilities']:
            from run_chrome_closed_sessions import Chrome
            cdp = Chrome(process, input_write, output_read)
            switch = chrome_workspace_switch(cdp, loaded['result']['id'], page.as_uri(), mapping, output)
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
        live_native_ids = set(switch['live_native_ids']) if switch else set(mapping.values())
        assert {item['id'] for item in loss_windows} == live_native_ids, loss_windows
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
                'cached_worker_same_path_upgrade': True, 'upgrade_tabs_preserved': True,
                'workspace_switch': switch or {'skipped': 'The workspace switch capabilities are unavailable'}}
    finally:
        os.close(input_write)
        os.close(output_read)
        stop(process)


def chrome_workspace_switch(cdp, extension_id, fixture_url, mapping, output):
    """Close the exact native Chrome window and restore it beside an identical sibling."""
    from workspace_state import browser, desktop

    def evaluate(body):
        targets = cdp.call('Target.getTargets')['targetInfos']
        worker = next(item for item in targets if item['type'] == 'service_worker'
                      and item['url'].startswith(f'chrome-extension://{extension_id}/'))
        cdp.session = cdp.call('Target.attachToTarget', {
            'targetId': worker['targetId'], 'flatten': True,
        })['sessionId']
        try:
            return cdp.evaluate(body)
        finally:
            cdp.call('Target.detachFromTarget', {'sessionId': cdp.session})

    evaluate(f"""(async () => {{
        const page = {json.dumps(fixture_url)};
        for (const windowId of {json.dumps([int(key) for key in mapping])}) {{
            const [original] = await chrome.tabs.query({{windowId}});
            await chrome.tabs.update(original.id, {{pinned: true, active: true}});
            const members = [];
            for (const anchor of ['#first', '#second']) {{
                const tab = await chrome.tabs.create({{windowId, url: page + anchor, active: false}});
                await wait(async () => {{
                    const current = await chrome.tabs.get(tab.id);
                    return current.url === page + anchor && current.status === 'complete';
                }});
                members.push(tab.id);
            }}
            const id = await chrome.tabs.group({{tabIds: members, createProperties: {{windowId}}}});
            await chrome.tabGroups.update(id, {{title: 'Synthetic group', color: 'cyan', collapsed: true}});
        }}
    }})()""")

    def shell_windows():
        shell = desktop.capture_shell()
        return {window['id']: window for window in browser._shell_browser_windows(shell, 'google-chrome')}

    def capture():
        return browser.capture_browser(include_native_ids=True)

    def signature(window):
        return {'groups': window['groups'], 'tabs': [
            {key: tab[key] for key in ('url', 'pinned', 'active', 'group')} for tab in window['tabs']
        ]}

    def chrome_windows():
        return {window['_native_id']: window for _, window in browser.browser_windows(capture())}

    def placement(window):
        return {key: window[key] for key in ('workspace', 'monitor', 'state', 'geometry')}

    def close(window, native):
        shell = desktop.capture_shell()
        desktop.close_window(native['id'], workspace=native['workspace'], name=window['workspace'],
                             pid=native['pid'], epoch=shell['enable_epoch'])
        wait_for(lambda: native['id'] not in shell_windows(), seconds=6)

    target_id, sibling_id = mapping.values()
    initial = capture()
    initial_windows = {window['_native_id']: window for _, window in browser.browser_windows(initial)}
    assert initial_windows[target_id]['_browser_window_id'] != initial_windows[sibling_id]['_browser_window_id'], initial
    assert signature(initial_windows[target_id]) == signature(initial_windows[sibling_id]), initial_windows
    native_target = shell_windows()[target_id]
    destination = (native_target['workspace'] + 1) % 3
    moved = desktop.move_window_result(target_id, {
        'workspace': destination, 'monitor': native_target['monitor'],
        'stage_before_handoff': True, 'state': 'maximized',
    })
    assert moved.get('token'), moved
    wait_for(lambda: desktop.expected_window_status(moved['token']) == 'placed', seconds=8)
    wait_for(lambda: shell_windows()[target_id]['workspace'] == destination, seconds=6)

    archived = capture()
    windows = {window['_native_id']: window for _, window in browser.browser_windows(archived)}
    target = windows[target_id]
    workspace = target['workspace']
    sibling = windows[sibling_id]
    target_before = placement(shell_windows()[target_id])
    sibling_before = placement(shell_windows()[sibling_id])
    sibling_browser_id = sibling['_browser_window_id']

    def sibling_tab_ids():
        live = browser.request_browser('capture', {'runtime_identity': True}, profile='Default')
        return next(item['runtime_identity'] for item in live['windows']
                    if item['runtime_window_id'] == sibling_browser_id)

    sibling_tabs_before = sibling_tab_ids()
    evidence = {'captured_native_ids': {key: item['_browser_window_id'] for key, item in windows.items()},
                'workspace': workspace, 'target': target_id, 'sibling': sibling_id}

    def record(stage, **values):
        evidence[stage] = values
        (output / 'chrome-workspace-switch.json').write_text(json.dumps(evidence, indent=2))

    def restore(prefix, expected_native=None):
        stage = prefix + ('-retry' if expected_native is not None else '')
        results = browser.restore_browser(archived, workspace=workspace, workspace_switch=True,
                                          restore_token_prefix=prefix)
        attempts = [str(item) for item in results]
        record(stage, results=attempts, current_native_ids=sorted(shell_windows()))
        provisional = results[0].evidence if len(results) == 1 else None
        handoff = provisional.placement.request_id if provisional else None
        if handoff and handoff.startswith('native-handoff:'):
            wait_for(lambda: desktop.expected_window_status(handoff.split(':', 1)[1]) == 'placed', seconds=10)
            results = browser.restore_browser(archived, workspace=workspace, workspace_switch=True,
                                              restore_token_prefix=prefix)
            attempts.extend(str(item) for item in results)
        record(stage, results=attempts, current_native_ids=sorted(shell_windows()))
        assert len(results) == 1 and results[0].success, results
        observed = chrome_windows()
        assert set(observed) == set(shell_windows()) and len(observed) == 2, observed
        assert observed[sibling_id]['_browser_window_id'] == sibling_browser_id, observed
        assert signature(observed[sibling_id]) == signature(sibling), observed
        assert sibling_tab_ids() == sibling_tabs_before, 'Sibling tab and group IDs must stay unchanged'
        assert placement(shell_windows()[sibling_id]) == sibling_before, shell_windows()
        restored_id = next(identifier for identifier in observed if identifier != sibling_id)
        if expected_native is not None:
            assert restored_id == expected_native, observed
        assert restored_id != target_id, observed
        actual = placement(shell_windows()[restored_id])
        assert {key: actual[key] for key in ('workspace', 'monitor', 'state')} == {
            key: target_before[key] for key in ('workspace', 'monitor', 'state')
        }, actual
        assert all(abs(value - actual['geometry'][key]) <= 3
                   for key, value in target_before['geometry'].items()), actual
        assert signature(observed[restored_id]) == signature(target), observed
        receipt = browser.request_browser('restore_status', {'restore_token': f'{prefix}:Default:{target["id"]}'},
                                          profile='Default')
        assert receipt['window_id'] == observed[restored_id]['_browser_window_id'], receipt
        return restored_id, observed[restored_id]

    prefix = 'fixture-native-restore'
    token = f'{prefix}:Default:{target["id"]}'
    marker = f'chrome-extension://{extension_id}/workspace.html#{browser._workspace_marker(token)}'

    def marked_sessions():
        return evaluate(f"""(async () => {{
            const sessions = await chrome.sessions.getRecentlyClosed();
            return sessions.filter(entry => entry.window?.tabs.some(tab =>
                tab.url === {json.dumps(marker)} && !tab.active
            )).map(entry => entry.window.sessionId);
        }})()""")

    record('before_close', target=target, sibling=sibling)
    browser.prepare_workspace_window('Default', target, token)
    close(target, shell_windows()[target_id])
    assert set(shell_windows()) == {sibling_id}, shell_windows()
    closed_sessions = wait_for(marked_sessions, seconds=5)
    assert len(closed_sessions) == 1, closed_sessions
    record('marked_session', session_id=closed_sessions[0])
    restored_id, restored = restore(prefix)
    assert not marked_sessions(), 'Exact native restore did not consume the marked session'
    assert restored['_browser_window_id'] != target['_browser_window_id'], restored
    retry_id, retry = restore(prefix, expected_native=restored_id)
    assert retry['_browser_window_id'] == restored['_browser_window_id'], retry
    record('native_restored', window_id=restored_id, browser_id=restored['_browser_window_id'], retry_id=retry_id)

    # Close without a marker to exercise the provider's reconstruction fallback.
    close(restored, shell_windows()[restored_id])
    assert set(shell_windows()) == {sibling_id}, shell_windows()
    fallback_id, fallback = restore('fixture-fallback-restore')
    assert fallback_id != restored_id, fallback
    record('fallback_restored', window_id=fallback_id, browser_id=fallback['_browser_window_id'])
    return {'native_restored': True, 'retry_reused_window': True, 'fallback_restored': True,
            'sibling_preserved': True, 'live_native_ids': sorted(shell_windows())}


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
