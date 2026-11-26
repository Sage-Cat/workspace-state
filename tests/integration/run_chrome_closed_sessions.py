#!/usr/bin/env python3
"""Probe Chrome restoration of closed windows and tabs in a disposable profile."""

from __future__ import annotations

import argparse
import hashlib
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time


# Chrome's private DevTools pipe reads fd 3 and writes fd 4.
PIPE_LAUNCHER = """import os, sys
from fcntl import fcntl, F_DUPFD
source_read, source_write = map(int, sys.argv[1:3])
incoming = fcntl(source_read, F_DUPFD, 5)
outgoing = fcntl(source_write, F_DUPFD, 5)
os.dup2(incoming, 3)
os.dup2(outgoing, 4)
for descriptor in {source_read, source_write, incoming, outgoing} - {3, 4}:
    os.close(descriptor)
os.execv(sys.argv[3], sys.argv[3:])
"""


class Chrome:
    def __init__(self, process, write_fd, read_fd):
        self.process = process
        self.write_fd = write_fd
        self.read_fd = read_fd
        self.number = 0
        self.buffer = bytearray()

    def call(self, method, params=None, session=None):
        self.number += 1
        request = {'id': self.number, 'method': method, 'params': params or {}}
        if session:
            request['sessionId'] = session
        deadline = time.monotonic() + 12
        command = memoryview(json.dumps(request).encode() + b'\0')
        while command:
            if not select.select([], [self.write_fd], [], max(0, deadline - time.monotonic()))[1]:
                raise RuntimeError(f'Chrome pipe write timed out: {method}')
            try:
                written = os.write(self.write_fd, command)
            except BlockingIOError:
                continue
            if not written:
                raise RuntimeError(f'Chrome pipe write made no progress: {method}')
            command = command[written:]
        while time.monotonic() < deadline:
            while b'\0' in self.buffer:
                raw, _, self.buffer = self.buffer.partition(b'\0')
                response = json.loads(raw)
                if response.get('id') != self.number:
                    continue
                if response.get('sessionId') != session or 'error' in response:
                    raise RuntimeError(f'{method}: {response}')
                return response['result']
            if not select.select([self.read_fd], [], [], max(0, deadline - time.monotonic()))[0]:
                break
            data = os.read(self.read_fd, 65536)
            if not data:
                raise EOFError(f'Chrome closed its pipe during {method}')
            self.buffer.extend(data)
            if len(self.buffer) > 4 * 1024 * 1024:
                raise RuntimeError('Chrome pipe response exceeds 4 MiB')
        raise RuntimeError(f'Chrome pipe read timed out: {method}')

    def connect(self, extension):
        extension_id = self.call('Extensions.loadUnpacked', {'path': str(extension)})['id']
        target = self.call('Target.createTarget', {
            'url': f'chrome-extension://{extension_id}/observer.html',
        })
        self.session = self.call('Target.attachToTarget', {
            'targetId': target['targetId'], 'flatten': True,
        })['sessionId']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if self.evaluate('typeof globalThis.chrome?.sessions?.getRecentlyClosed === "function"'):
                    return
            except RuntimeError as error:
                if 'Execution context was destroyed' not in str(error):
                    raise
            time.sleep(.1)
        raise RuntimeError('Temporary extension page did not expose chrome.sessions')

    def evaluate(self, body):
        result = self.call('Runtime.evaluate', {
            'expression': f'''(async () => {{
                const wait = async check => {{
                    for (let attempt = 0; attempt < 90; attempt++) {{
                        const result = await check();
                        if (result) return result;
                        await new Promise(resolve => setTimeout(resolve, 100));
                    }}
                    throw new Error('Fixture did not reach the expected browser state');
                }};
                const historyOf = async tabId => {{
                    const target = {{tabId}};
                    await chrome.debugger.attach(target, '1.3');
                    try {{
                        return await chrome.debugger.sendCommand(target, 'Page.getNavigationHistory');
                    }} finally {{
                        await chrome.debugger.detach(target);
                    }}
                }};
                return await ({body});
            }})()''',
            'awaitPromise': True, 'returnByValue': True,
        }, self.session)
        if 'exceptionDetails' in result:
            raise RuntimeError(result['exceptionDetails'].get('exception', {}).get('description', result))
        return result['result'].get('value')

    def close(self):
        try:
            self.call('Browser.close')
        except EOFError:
            pass
        code = self.process.wait(timeout=12)
        if code != 0:
            raise RuntimeError(f'Chrome failed to exit cleanly: {code}')


@contextmanager
def chrome(root, executable, log):
    home = root / 'home'
    home.mkdir(exist_ok=True)
    env = {
        'HOME': str(home), 'PATH': os.defpath, 'LANG': 'C.UTF-8',
        'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + str(root / 'no-session-bus'),
        'DBUS_SYSTEM_BUS_ADDRESS': 'unix:path=' + str(root / 'no-system-bus'),
    }
    for key in ('CONFIG', 'DATA', 'STATE', 'CACHE', 'RUNTIME'):
        directory = root / key.lower()
        directory.mkdir(mode=0o700, exist_ok=True)
        env[f'XDG_{key}_HOME' if key != 'RUNTIME' else 'XDG_RUNTIME_DIR'] = str(directory)
    argv = [executable, f'--user-data-dir={root / "profile"}', '--headless', '--disable-gpu',
            '--no-first-run', '--no-default-browser-check', '--password-store=basic',
            '--remote-debugging-pipe', '--enable-unsafe-extension-debugging', '--disable-sync',
            '--disable-background-networking', '--disable-component-update', '--disable-default-apps',
            '--proxy-server=http://127.0.0.1:9', 'about:blank']
    browser_read, driver_write = os.pipe()
    driver_read, browser_write = os.pipe()
    process = None
    try:
        process = subprocess.Popen([sys.executable, '-c', PIPE_LAUNCHER,
                                    str(browser_read), str(browser_write), *argv],
                                   env=env, pass_fds=(browser_read, browser_write),
                                   start_new_session=True, stdout=log, stderr=subprocess.STDOUT)
        os.close(browser_read)
        os.close(browser_write)
        browser_read = browser_write = -1
        os.set_blocking(driver_write, False)
        yield Chrome(process, driver_write, driver_read)
    finally:
        for descriptor in (browser_read, browser_write, driver_read, driver_write):
            if descriptor >= 0:
                os.close(descriptor)
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)


class FixturePage(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(b'<!doctype html><title>Closed window probe</title><p>Test page</p>')

    def log_message(self, *_args):
        pass


@contextmanager
def fixture_origin():
    with ThreadingHTTPServer(('127.0.0.1', 0), FixturePage) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}'
        finally:
            server.shutdown()
            worker.join(timeout=3)


def put_closed_window(browser, origin, token):
    return browser.evaluate(f"""(async () => {{
        const origin = {json.dumps(origin)};
        const markerUrl = 'chrome://version/#wsctl-workspace-' + {json.dumps(hashlib.sha256(token.encode()).hexdigest())};
        const window = await chrome.windows.create({{url: 'about:blank', focused: false}});
        const [seed] = window.tabs;
        const primary = await chrome.tabs.create({{windowId: window.id, url: origin + '/before', active: false}});
        const committed = (id, url) => wait(async () => {{
            const tab = await chrome.tabs.get(id);
            return tab.url === url && !tab.pendingUrl && tab.status === 'complete' && tab;
        }});
        await committed(primary.id, origin + '/before');
        await chrome.tabs.update(primary.id, {{active: true}});
        const target = {{tabId: primary.id}};
        await chrome.debugger.attach(target, '1.3');
        let originalHistory;
        try {{
            // Chrome's Back UI may skip entries that had no user interaction.
            await chrome.debugger.sendCommand(target, 'Runtime.evaluate', {{
                expression: 'location.assign(' + JSON.stringify(origin + '/after') + ')',
                userGesture: true,
            }});
            await committed(primary.id, origin + '/after');
            originalHistory = await chrome.debugger.sendCommand(target, 'Page.getNavigationHistory');
        }} finally {{
            await chrome.debugger.detach(target);
        }}
        if (!originalHistory.entries.some(entry => entry.url === origin + '/before'))
            throw new Error('Synthetic grouped tab has no prior navigation: ' + JSON.stringify(originalHistory));
        const sibling = await chrome.tabs.create({{windowId: window.id, url: origin + '/sibling', active: false}});
        const pinned = await chrome.tabs.create({{windowId: window.id, url: origin + '/before-pin', active: false, pinned: true}});
        const marker = await chrome.tabs.create({{windowId: window.id, url: markerUrl, active: false}});
        for (const [tab, url] of [[sibling, origin + '/sibling'], [pinned, origin + '/before-pin'], [marker, markerUrl]])
            await committed(tab.id, url);
        await chrome.tabs.update(pinned.id, {{url: origin + '/pinned'}});
        await committed(pinned.id, origin + '/pinned');
        const groupId = await chrome.tabs.group({{
            tabIds: [primary.id, sibling.id], createProperties: {{windowId: window.id}},
        }});
        await chrome.tabs.update(pinned.id, {{active: true}});
        await chrome.tabs.remove(seed.id);
        await chrome.tabGroups.update(groupId, {{title: 'Fixture group', color: 'cyan', collapsed: true}});
        const group = await chrome.tabGroups.get(groupId);
        const tabs = await chrome.tabs.query({{windowId: window.id}});
        if (group.windowId !== window.id || tabs.filter(tab => tab.groupId === groupId).length !== 2)
            throw new Error('Test group was not created in the target window');
        if (tabs.find(tab => tab.id === marker.id)?.active !== false)
            throw new Error('Marker must stay in the background');
        await chrome.windows.remove(window.id);
        const entries = await wait(async () => {{
            const entries = await chrome.sessions.getRecentlyClosed();
            return entries.some(entry => entry.window?.tabs.some(tab => tab.url === origin + '/pinned')) && entries;
        }});
        return {{group, tabs, markerUrl, entries, originalHistory}};
    }})()""")


def recently_closed(browser, pinned_url):
    return browser.evaluate(f"""wait(async () => {{
        const entries = await chrome.sessions.getRecentlyClosed();
        return entries.some(entry => entry.window?.tabs.some(tab => tab.url === {json.dumps(pinned_url)})) && entries;
    }})""")


def exact_entry(entries, marker):
    matches = [entry['window'] for entry in entries if 'window' in entry and any(
        tab.get('url') == marker or tab.get('pendingUrl') == marker
        for tab in entry['window']['tabs'])]
    if len(matches) != 1:
        raise RuntimeError(f'Expected exactly one closed window containing the marker, found {len(matches)}')
    return matches[0]


def reopen(browser, session_id, origin):
    return browser.evaluate(f"""(async () => {{
        const origin = {json.dumps(origin)};
        const restored = await chrome.sessions.restore({json.dumps(session_id)});
        const initial = await chrome.windows.get(restored.window.id, {{populate: true}});
        const window = await wait(async () => {{
            const window = await chrome.windows.get(restored.window.id, {{populate: true}});
            const urls = new Set(window.tabs.map(tab => tab.url));
            return ['/pinned', '/after', '/sibling'].every(path => urls.has(origin + path))
                && window.tabs[0].status === 'complete' && window;
        }});
        const groups = await chrome.tabGroups.query({{windowId: window.id}});
        const history = {{}};
        const navigations = {{}};
        for (const [current, previous] of [['/pinned', '/before-pin'], ['/after', '/before']]) {{
            const page = window.tabs.find(tab => [tab.url, tab.pendingUrl].includes(origin + current));
            if (!page)
                continue;
            await chrome.tabs.update(page.id, {{active: true}});
            await wait(async () => {{
                const tab = await chrome.tabs.get(page.id);
                return tab.status === 'complete' && !tab.pendingUrl;
            }});
            navigations[current] = await historyOf(page.id);
            try {{
                await chrome.tabs.goBack(page.id);
                const back = await wait(async () => {{
                    const tab = await chrome.tabs.get(page.id);
                    return tab.url === origin + previous && tab;
                }});
                history[current] = back.url;
            }} catch (error) {{
                history[current] = error.message;
            }}
        }}
        return {{initialTabs: initial.tabs, tabs: window.tabs, groups, history, navigations}};
    }})()""")


def verify_window(evidence, closed, origin):
    marker = evidence['markerUrl']
    expected_urls = [origin + route for route in ('/pinned', '/after', '/sibling')] + [marker]
    if [tab['url'] for tab in closed['tabs']] != expected_urls:
        raise RuntimeError('Unexpected recently closed tabs or missing committed marker URL')
    marker_tab = closed['tabs'][-1]
    if marker_tab['active'] or marker_tab.get('pendingUrl'):
        raise RuntimeError('Recently closed marker must be committed and inactive')
    restored = evidence['restored']
    tabs = restored['tabs']
    if [tab['url'] for tab in tabs] != expected_urls:
        raise RuntimeError('Exact window restore changed the tab order or contents')
    if [tab['pinned'] for tab in tabs] != [True, False, False, False]:
        raise RuntimeError('Exact window restore changed which tab is pinned')
    [group] = restored['groups']
    expected_group = evidence['group']
    for field in ('title', 'color', 'collapsed'):
        if group[field] != expected_group[field]:
            raise RuntimeError(f'Exact window restore changed group {field}')
    if [tab['groupId'] for tab in tabs] != [-1, group['id'], group['id'], -1]:
        raise RuntimeError('Exact window restore changed group membership')
    if restored['history'].get('/pinned') != origin + '/before-pin':
        raise RuntimeError('Exact window restore lost the pinned tab navigation history')
    entries = restored['navigations']['/after']['entries']
    return {
        'session_id': closed['sessionId'],
        'group_navigation_preserved': any(entry['url'] == origin + '/before' for entry in entries),
        'group_back': restored['history']['/after'],
    }


def run(root, executable, log, origin):
    extension = root / 'extension'
    extension.mkdir()
    (extension / 'manifest.json').write_text(json.dumps({
        'manifest_version': 3, 'name': 'Closed session probe', 'version': '1',
        'permissions': ['sessions', 'tabs', 'tabGroups', 'debugger'],
    }))
    (extension / 'observer.html').write_text('<!doctype html><title>Closed session fixture</title>')
    with chrome(root, executable, log) as browser:
        version = browser.call('Browser.getVersion')['product']
        browser.connect(extension)
        current = put_closed_window(browser, origin + '/current', 'fixture-current')
        restart = put_closed_window(browser, origin + '/restart', 'fixture-restart')
        closed = exact_entry(restart['entries'], current['markerUrl'])
        if restart['entries'][0]['window']['sessionId'] == closed['sessionId']:
            raise RuntimeError('The decoy window must have closed more recently than the target')
        current['restored'] = reopen(browser, closed['sessionId'], origin + '/current')
        current['verified'] = verify_window(current, closed, origin + '/current')
        individual = put_closed_window(browser, origin + '/individual', 'fixture-individual')
        individual_entry = exact_entry(individual['entries'], individual['markerUrl'])
        individual_tab = next(tab for tab in individual_entry['tabs'] if tab['url'] == origin + '/individual/after')
        individual['restored'] = browser.evaluate(f"""(async () => {{
            const restored = await chrome.sessions.restore({json.dumps(individual_tab['sessionId'])});
            const tab = restored.tab;
            const group = tab?.groupId >= 0 ? await chrome.tabGroups.get(tab.groupId) : null;
            const tabs = await chrome.tabs.query({{windowId: tab.windowId}});
            await wait(async () => (await chrome.tabs.get(tab.id)).status === 'complete');
            const navigation = await historyOf(tab.id);
            try {{
                await chrome.tabs.goBack(tab.id);
                const previous = await wait(async () => {{
                    const current = await chrome.tabs.get(tab.id);
                    return current.url === {json.dumps(origin + '/individual/before')} && current;
                }});
                return {{tab, group, tabs, navigation, back: previous.url}};
            }} catch (error) {{
                return {{tab, group, tabs, navigation, back: error.message}};
            }}
        }})()""")
        api = browser.evaluate("""({
            sessions: Object.keys(chrome.sessions).sort(),
            tabGroups: Object.keys(chrome.tabGroups).sort(),
        })""")
        browser.close()
    with chrome(root, executable, log) as browser:
        browser.connect(extension)
        restart['after_restart'] = recently_closed(browser, origin + '/restart/pinned')
        closed = exact_entry(restart['after_restart'], restart['markerUrl'])
        restart['restored'] = reopen(browser, closed['sessionId'], origin + '/restart')
        restart['verified'] = verify_window(restart, closed, origin + '/restart')
        browser.close()
    return {'chrome': version, 'current': current, 'restart': restart, 'individual': individual, 'api': api}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='launch two isolated Chrome processes')
    parser.add_argument('--output', type=Path, required=True, help='directory for synthetic evidence')
    args = parser.parse_args()
    if not args.run:
        parser.error('--run is required')
    executable = shutil.which('google-chrome')
    if not executable:
        parser.error('google-chrome is required')
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='wsctl-chrome-sessions-') as temporary, fixture_origin() as origin:
        with (args.output / 'chrome.log').open('w') as log:
            result = run(Path(temporary), executable, log, origin)
        (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
