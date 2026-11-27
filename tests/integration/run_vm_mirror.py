#!/usr/bin/python3
"""Mirror a described desktop layout, with synthetic content only, in the disposable guest.

Run inside the explicitly named guest (hostname `wsctl-validation`, KVM) and never on a
working desktop. The layout is a JSON file passed with `--layout`; it carries only names,
counts and placements (monitors, workspaces, terminal sessions with their windows, Chrome
windows with tab and group sizes, VS Code windows with editor counts, file-manager and
social-application windows, Agent Deck groups). Every page, file, history line and
conversation is generated here. Real applications and companions are used; conversation
clients and Agent Deck sessions are labeled local stand-ins. See docs/testing.md.

Phases (all need `--layout FILE`):
  seed     monitors, configure, deck, terminals, chrome, applications, place, history, checkpoint
  verify   compare the live desktop with the layout and print a JSON report
  vscode-extras   open the synthetic extra editors in one VS Code window (`--extras-workspace NAME`)
  monitors write ~/.config/monitors.xml for the layout and apply it live
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_vm_scale as scale  # noqa: E402  (shared fixture primitives)

ROOT = scale.ROOT
MIRROR = Path.home() / '.local/state/wsctl-mirror'
PAGES = 'http://127.0.0.1:18765/scale/mirror'
PROFILE = 'mirror'
HISTORY_COMMANDS = 3
COLORS = ['blue', 'red', 'green', 'yellow', 'purple', 'cyan', 'orange', 'pink']


def run(*args, **kwargs):
    return scale.run(*args, **kwargs)


def wait_for(function, *, timeout=45):
    return scale.wait_for(function, timeout=timeout)


def load(path):
    return json.loads(Path(path).read_text())


def save(name, value):
    MIRROR.mkdir(parents=True, mode=0o700, exist_ok=True)
    destination = MIRROR / (name + '.json')
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(destination)


def environment():
    """Reproduce the graphical login environment for commands started over SSH."""
    for line in run('systemctl', '--user', 'show-environment').splitlines():
        key, separator, value = line.partition('=')
        if separator and key in {'DISPLAY', 'XAUTHORITY'}:
            os.environ[key] = value
    os.environ.update({'PATH': str(Path.home() / '.local/bin') + ':' + os.environ['PATH'],
                       'XDG_RUNTIME_DIR': f'/run/user/{os.getuid()}',
                       'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{os.getuid()}/bus',
                       'WAYLAND_DISPLAY': 'wayland-0', 'XDG_SESSION_TYPE': 'wayland',
                       'AGENTDECK_SKIP_UPDATE_CHECK': '1'})
    data_dirs = os.environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share').split(':')
    if '/var/lib/snapd/desktop' not in data_dirs:
        data_dirs.append('/var/lib/snapd/desktop')
    os.environ['XDG_DATA_DIRS'] = ':'.join(data_dirs)
    release = (Path.home() / '.local/bin/wsctl').resolve().parent.parent
    sys.path.insert(0, str(release / 'src'))


# ---------------------------------------------------------------- layout helpers

def monitor_index(layout, connector):
    """Index of the live monitor that has the geometry of the named layout monitor."""
    want = next(m for m in layout['monitors'] if m['connector'] == connector)
    for monitor in scale.shell()['monitors']:
        if (monitor['x'], monitor['y'], monitor['width'], monitor['height']) == (
                want['x'], want['y'], want['width'], want['height']):
            return monitor['index']
    raise RuntimeError(f'No live monitor has the geometry of {connector}')


def target_of(layout, placement):
    target = {'workspace': layout['workspaces'].index(placement['workspace']),
              'monitor': monitor_index(layout, placement['monitor']),
              'state': placement['state'], 'coordinate_space': 'monitor',
              'geometry': {key: placement['frame'][key] for key in ('x', 'y', 'width', 'height')}}
    if placement['state'] != 'normal':
        # A maximized frame is the compositor's work area, not a saved rectangle.
        target.pop('geometry')
    return target


def place_window(window_id, target):
    """Stage on the active workspace, then hand off to the saved one (GNOME owns the handoff)."""
    from workspace_state.desktop import move_window_result
    from workspace_state.provider_results import placement_accepted, placement_frame_matches
    for placement in ({**target, 'workspace': scale.shell()['active_workspace']}, target):
        result = move_window_result(window_id, placement)
        if not placement_accepted(result):
            raise RuntimeError(f'Placement rejected: {result}')
        resolved = result.get('resolved_target') or placement
        wait_for(lambda: any(w['id'] == window_id and placement_frame_matches(w, resolved)
                             for w in scale.shell()['windows']), timeout=20)


def conversation_id(session, index):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'wsctl-mirror/{session}/{index}'))


# ---------------------------------------------------------------- monitors

def monitors(layout):
    """Persist the layout in monitors.xml and apply it live (offsets are ignored by SetUIInfo)."""
    import gi
    gi.require_version('Gio', '2.0')
    from gi.repository import Gio, GLib
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def call(method, args=None):
        return bus.call_sync('org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig',
                             'org.gnome.Mutter.DisplayConfig', method, args, None,
                             Gio.DBusCallFlags.NONE, -1, None)
    serial, current, _logical, _props = call('GetCurrentState').unpack()
    connectors = [m[0][0] for m in sorted(current, key=lambda m: m[0][0])]
    logical, xml = [], ['<monitors version="2">', '  <configuration>', '    <layoutmode>logical</layoutmode>']
    for wanted, connector in zip(layout['monitors'], connectors):
        monitor = next(m for m in current if m[0][0] == connector)
        mode = next(m for m in monitor[1] if (m[1], m[2]) == (wanted['width'], wanted['height']))
        logical.append((wanted['x'], wanted['y'], float(wanted['scale']), 0, bool(wanted['primary']),
                        [(connector, mode[0], {})]))
        xml += ['    <logicalmonitor>', f'      <x>{wanted["x"]}</x>', f'      <y>{wanted["y"]}</y>',
                f'      <scale>{int(wanted["scale"])}</scale>']
        if wanted['primary']:
            xml.append('      <primary>yes</primary>')
        spec = monitor[0]
        xml += ['      <monitor>', '        <monitorspec>', f'          <connector>{spec[0]}</connector>',
                f'          <vendor>{spec[1]}</vendor>', f'          <product>{spec[2]}</product>',
                f'          <serial>{spec[3]}</serial>', '        </monitorspec>', '        <mode>',
                f'          <width>{mode[1]}</width>', f'          <height>{mode[2]}</height>',
                f'          <rate>{mode[3]!r}</rate>', '        </mode>', '      </monitor>',
                '    </logicalmonitor>']
    xml += ['  </configuration>', '</monitors>', '']
    (Path.home() / '.config/monitors.xml').write_text('\n'.join(xml))
    # Temporary apply: the persistent method waits for a confirmation dialog and reverts.
    call('ApplyMonitorsConfig', GLib.Variant('(uua(iiduba(ssa{sv}))a{sv})', (serial, 1, logical, {})))
    save('monitor-map', {connector: want['connector'] for connector, want in
                         zip(connectors, layout['monitors'])})


# ---------------------------------------------------------------- configuration

def configure(layout):
    ROOT.mkdir(parents=True, mode=0o700, exist_ok=True)
    scale.write('social-mode', {'real': True})
    scale.configure()
    scale.services()
    names = layout['workspaces']
    run('gsettings', 'set', 'org.gnome.mutter', 'dynamic-workspaces', 'false')
    run('gsettings', 'set', 'org.gnome.desktop.wm.preferences', 'num-workspaces', str(len(names)))
    run('gsettings', 'set', 'org.gnome.desktop.wm.preferences', 'workspace-names', repr(names))
    # The synthetic viewer of the scale fixture is not part of a mirrored desktop.
    (Path.home() / '.config/autostart/wsctl-scale-viewer.desktop').unlink(missing_ok=True)
    (Path.home() / '.config/alacritty/alacritty.toml').write_text('[window]\ndynamic_title = true\n')
    tmux_config = Path.home() / '.tmux.conf'
    marker = '# mirror baseline options'
    text = tmux_config.read_text().split(marker)[0]
    tmux_config.write_text(text + marker + '\nset -g default-shell /usr/bin/zsh\nset -g base-index 1\n'
                           'setw -g pane-base-index 1\nset -g renumber-windows on\n')
    (Path.home() / '.zshrc').write_text('HISTFILE=~/.zsh_history\nHISTSIZE=50000\nSAVEHIST=50000\n'
                                        'setopt INC_APPEND_HISTORY\nPROMPT=\'%~ %# \'\n')
    binary = Path.home() / '.local/bin'
    fixture = ROOT / 'fixture.py'
    for name in ('claude', 'codex'):
        (binary / name).write_text(
            '#!/bin/sh\n# SYNTHETIC stand-in for a conversation client: no network, no model\n'
            f'if [ "$1" = resume ]; then exec /usr/bin/python3 {shlex.quote(str(fixture))} conversation "$@"; fi\n'
            'echo "SYNTHETIC stand-in for a conversation client"\nexec sleep 100000\n')
        (binary / name).chmod(0o700)
    scale.configure_pages()
    for folder in ('projects', 'code', 'folders'):
        (MIRROR / folder).mkdir(parents=True, exist_ok=True)
    save('configured', {'workspaces': names, 'time': time.time()})


# ---------------------------------------------------------------- Agent Deck

def deck(layout):
    """Synthetic profile with the layout's groups and sessions; no host configuration is read."""
    deck_binary = shutil.which('agent-deck')
    if not deck_binary:
        raise RuntimeError('Copy the static agent-deck executable to ~/.local/bin first')
    base = ['agent-deck', '-p', PROFILE]
    config = Path.home() / '.config/agent-deck/config.toml'
    config.parent.mkdir(parents=True, exist_ok=True)
    # Several scoped dashboards run at once, one per terminal session.
    config.write_text('# synthetic mirror configuration\n[instances]\nallow_multiple = true\n')
    existing = json.loads(run('agent-deck', 'profile', 'list', '--json')).get('profiles') or []
    if PROFILE not in [p['name'] for p in existing]:
        run('agent-deck', 'profile', 'create', PROFILE)
    made = []
    for group in sorted({row['group'] for row in layout['agent_deck']}):
        run(*base, 'group', 'create', group, check=False)
    for row in layout['agent_deck']:
        for number in range(row['count']):
            title = f'{row["group"]}-{row["provider"]}-{number + 1}'
            directory = MIRROR / 'projects' / row['group'] / title
            directory.mkdir(parents=True, exist_ok=True)
            record = json.loads(run(*base, 'add', directory, '-t', title, '-g', row['group'],
                                    '-c', row['provider'], '-json'))
            if row['open']:
                run(*base, 'session', 'start', record['id'], '-no-wait', '-q', timeout=60, check=False)
            made.append({'id': record['id'], 'title': title, 'group': row['group'],
                         'provider': row['provider'], 'open': row['open']})
    save('deck-sessions', made)


# ---------------------------------------------------------------- terminals

def terminals(layout):
    names = {term['session'] for term in layout['terminals']}
    if names & set(run('tmux', 'list-sessions', '-F', '#{session_name}', check=False).split()):
        raise RuntimeError('Seed requires a guest tmux server without the layout sessions')
    histories, identities = {}, []
    for term in layout['terminals']:
        name = term['session']
        directory = MIRROR / 'projects' / name
        directory.mkdir(parents=True, exist_ok=True)
        first = True
        for window in term['windows']:
            command = None
            if window['kind'] == 'deck':
                command = 'exec env AGENT_DECK_ALLOW_OUTER_TMUX=1 agent-deck -p ' + PROFILE + (
                    ' -g ' + shlex.quote(window['group']) if window.get('group') else '')
            elif window['kind'] == 'conversation':
                identity = conversation_id(name, window['index'])
                identities.append(identity)
                command = f'exec {Path.home()}/.local/bin/codex resume {identity}'
            if first:
                args = ['new-session', '-d', '-s', name, '-n', window['name'], '-c', directory]
                first = False
            else:
                args = ['new-window', '-d', '-t', f'={name}:{window["index"]}', '-n', window['name'], '-c', directory]
            run('tmux', *args, *([command] if command else []))
            run('tmux', 'set-window-option', '-t', f'={name}:{window["index"]}', 'automatic-rename', 'off')
        live = run('tmux', 'list-windows', '-t', f'={name}:', '-F', '#{window_index}').split()
        if live != [str(w['index']) for w in term['windows']]:
            raise RuntimeError(f'Window indices differ in {name}: {live}')
        placement = target_of(layout, term['placement'])
        scale.launch(['/usr/bin/alacritty', '--title', name, '-o', 'window.dynamic_title=false',
                      '-e', 'tmux', 'attach-session', '-t', '=' + name], 'alacritty-' + name)
        time.sleep(.5)
        histories[name] = {}
        for window in term['windows']:
            if window['kind'] == 'shell':
                histories[name][str(window['index'])] = [
                    f'echo mirror-{name}-{window["index"]}-{n + 1}' for n in range(HISTORY_COMMANDS)]
    save('conversation-identities', identities)
    save('expected-history', histories)
    wait_for(lambda: len([w for w in scale.shell()['windows'] if w.get('wm_class') == 'Alacritty'])
             == len(layout['terminals']), timeout=60)
    time.sleep(2)
    for name, windows in histories.items():
        for index, commands in windows.items():
            for command in commands:
                run('tmux', 'send-keys', '-t', f'={name}:{index}', command, 'Enter')
                time.sleep(.3)


# ---------------------------------------------------------------- Chrome

def chrome_spec(layout):
    return [{'tabs': w['tabs'], 'groups': w['group_sizes']} for w in layout['chrome']]


def chrome(layout, layout_path):
    for pid in {window['pid'] for window in scale.shell()['windows'] if 'google-chrome' in str(window.get('app_ids'))}:
        if str(ROOT / 'chrome-profile').encode() not in Path(f'/proc/{pid}/cmdline').read_bytes():
            raise RuntimeError('Refusing to replace a non-fixture Chrome process')
        os.kill(pid, 15)
    profile = ROOT / 'chrome-profile'
    if profile.exists():
        archived = ROOT / 'seeds' / ('chrome-' + uuid.uuid4().hex)
        archived.parent.mkdir(exist_ok=True)
        profile.rename(archived)
    (ROOT / 'chrome-ready.json').unlink(missing_ok=True)
    scale.write('chrome-expected', {'windows': len(layout['chrome']), 'tabs': sum(w['tabs'] for w in layout['chrome']),
                                    'groups': sum(len(w['group_sizes']) for w in layout['chrome'])})
    scale.launch([sys.executable, HERE / 'run_vm_mirror.py', '--disposable-guest', '--layout', layout_path,
                  'chrome-host'], 'chrome-host')
    wait_for(lambda: (ROOT / 'chrome-ready.json').exists(), timeout=180)


def chrome_host(layout):
    scale.configure_pages()
    pipe = scale.ChromePipe()
    extension = Path.home() / '.local/share/workspace-state/chrome-extension'
    loaded = pipe.call('Extensions.loadUnpacked', {'path': str(extension)})
    pipe.extension_id = loaded['id']
    scale.write('chrome-extension-load', loaded)
    target = wait_for(lambda: next((t for t in pipe.call('Target.getTargets')['targetInfos']
                                    if t['type'] == 'service_worker'
                                    and t['url'].startswith('chrome-extension://' + loaded['id'] + '/')), None))
    session = pipe.call('Target.attachToTarget', {'targetId': target['targetId'], 'flatten': True})['sessionId']
    expression = '''(async () => {
      const spec = %s, colors = %s, initial = await chrome.windows.getAll(), result = [];
      for (let i = 0; i < spec.length; i++) {
        const w = await chrome.windows.create({url: '%s/' + i + '-tab-0', focused: false});
        const ids = [w.tabs[0].id];
        for (let t = 1; t < spec[i].tabs; t++)
          ids.push((await chrome.tabs.create({windowId: w.id, url: '%s/' + i + '-tab-' + t})).id);
        let at = 0; const groups = [];
        for (let g = 0; g < spec[i].groups.length; g++) {
          const slice = ids.slice(at, at + spec[i].groups[g]); at += spec[i].groups[g];
          const id = await chrome.tabs.group({tabIds: slice, createProperties: {windowId: w.id}});
          await chrome.tabGroups.update(id, {title: 'mirror-group-' + i + '-' + g,
                                             color: colors[g %% colors.length], collapsed: false});
          groups.push(id);
        }
        result.push({window: w.id, groups: groups, tabs: spec[i].tabs});
      }
      for (const w of initial) await chrome.windows.remove(w.id);
      return result;
    })()''' % (json.dumps(chrome_spec(layout)), json.dumps(COLORS), PAGES, PAGES)
    result = pipe.call('Runtime.evaluate', {'expression': expression, 'awaitPromise': True,
                                           'returnByValue': True}, session)
    if 'exceptionDetails' in result:
        raise RuntimeError(str(result))
    scale.write('chrome-original-groups', result['result']['value'])
    scale.write('chrome-ready', {'pid': pipe.process.pid, 'controller': os.getpid()})
    pipe.serve()


def place_chrome(layout):
    from workspace_state import browser
    created = json.loads((ROOT / 'chrome-original-groups.json').read_text())
    live = {w['runtime_window_id']: w for w in browser.request_browser('capture', profile='Default')['windows']}
    for entry, spec in zip(created, layout['chrome']):
        if entry['window'] not in live:
            raise RuntimeError('Chrome window missing after seeding')
        target = {'workspace': layout['workspaces'].index(spec['workspace']),
                  'monitor': monitor_index(layout, spec['monitor']), 'state': spec['state'],
                  'coordinate_space': 'monitor',
                  'geometry': {'x': 80, 'y': 60, 'width': 960, 'height': 600}}
        try:
            placed = browser._place_browser_window(profile='Default', chrome_window_id=entry['window'],
                                                   app_id='google-chrome', placement=target)
        except browser.BrowserPlacementPending as pending:
            placed = scale.await_placement_handoff(pending.request_id)
        if not placed:
            raise RuntimeError('Chrome placement did not verify')


# ---------------------------------------------------------------- applications

FIXTURE_EXTENSION = r'''
const vscode = require('vscode');
const fs = require('fs');
const path = require('path');
exports.activate = function (context) {
  const folders = vscode.workspace.workspaceFolders || [];
  if (!folders.length) return;
  const trigger = path.join(folders[0].uri.fsPath, '.mirror-actions.json');
  const timer = setInterval(async () => {
    if (!fs.existsSync(trigger)) return;
    let actions;
    try { actions = JSON.parse(fs.readFileSync(trigger, 'utf8')); } catch (e) { return; }
    fs.renameSync(trigger, trigger + '.done');
    for (const a of actions) {
      const uri = a.file ? vscode.Uri.file(path.join(folders[0].uri.fsPath, a.file)) : undefined;
      try {
        if (a.cmd === 'markdown') await vscode.commands.executeCommand('markdown.showPreview', uri);
        else if (a.cmd === 'settings') await vscode.commands.executeCommand('workbench.action.openSettings');
        else if (a.cmd === 'git-diff') await vscode.commands.executeCommand('git.openChange', uri);
        else if (a.cmd === 'untitled') {
          const doc = await vscode.workspace.openTextDocument({content: a.text, language: 'plaintext'});
          await vscode.window.showTextDocument(doc, {preview: false});
        } else if (a.cmd === 'dump') {
          const tabs = [];
          for (const group of vscode.window.tabGroups.all)
            for (const tab of group.tabs)
              tabs.push({label: tab.label, input: tab.input ? tab.input.constructor.name : null,
                         dirty: tab.isDirty, preview: tab.isPreview});
          fs.writeFileSync(path.join(folders[0].uri.fsPath, a.out || '.mirror-tabs.json'),
                           JSON.stringify({tabs, terminals: vscode.window.terminals.map(t => t.name)}, null, 1));
        } else if (a.cmd === 'close-window') {
          await vscode.commands.executeCommand('workbench.action.closeWindow');
        } else if (a.cmd === 'dirty') {
          const doc = await vscode.workspace.openTextDocument(uri);
          const editor = await vscode.window.showTextDocument(doc, {preview: false});
          await editor.edit(b => b.insert(new vscode.Position(0, 0), 'unsaved synthetic edit\n'));
        } else if (a.cmd === 'terminal-sleep') {
          const terminal = vscode.window.createTerminal({name: 'mirror-sleep'});
          terminal.show(); terminal.sendText('sleep 300');
        } else if (a.cmd === 'terminal') {
          const terminal = vscode.window.createTerminal({name: 'mirror-terminal'});
          terminal.show(); terminal.sendText('echo mirror-terminal');
        }
      } catch (e) { fs.appendFileSync(trigger + '.errors', String(e) + '\n'); }
    }
  }, 700);
  context.subscriptions.push({dispose: () => clearInterval(timer)});
};
'''


def code_settings():
    user = Path.home() / '.config/Code/User'
    user.mkdir(parents=True, exist_ok=True)
    (user / 'settings.json').write_text(json.dumps({
        'telemetry.telemetryLevel': 'off', 'update.mode': 'none', 'update.showReleaseNotes': False,
        'extensions.autoUpdate': False, 'extensions.autoCheckUpdates': False,
        'workbench.startupEditor': 'none', 'window.title': '${rootName}${separator}${activeEditorShort}', 'workbench.enableExperiments': False,
        'security.workspace.trust.enabled': False, 'git.enabled': True, 'git.openRepositoryInParentFolders': 'never',
        'http.proxy': 'http://127.0.0.1:9', 'http.proxySupport': 'override'}))
    extension = Path.home() / '.vscode/extensions/mirror-fixture-0.0.1'
    extension.mkdir(parents=True, exist_ok=True)
    (extension / 'package.json').write_text(json.dumps({
        'name': 'mirror-fixture', 'publisher': 'synthetic', 'version': '0.0.1', 'main': 'extension.js',
        'engines': {'vscode': '^1.80.0'}, 'activationEvents': ['onStartupFinished']}))
    (extension / 'extension.js').write_text(FIXTURE_EXTENSION)


def code_project(layout, index):
    entry = layout['vscode'][index]
    name = f'{index + 1}-{entry["placement"]["workspace"].lower()}'
    project = MIRROR / 'code' / name
    project.mkdir(parents=True, exist_ok=True)
    files = []
    for number in range(entry['editors']):
        path = project / f'file-{number + 1}.txt'
        path.write_text(f'Synthetic editor content {name} {number + 1}\n')
        files.append(path)
    return project, files


def git_repository(project):
    """A tiny repository whose working tree differs from HEAD, for a git diff editor."""
    env = {**os.environ, 'GIT_AUTHOR_NAME': 'mirror', 'GIT_AUTHOR_EMAIL': 'mirror@example.invalid',
           'GIT_COMMITTER_NAME': 'mirror', 'GIT_COMMITTER_EMAIL': 'mirror@example.invalid'}
    shutil.rmtree(project / '.git', ignore_errors=True)
    (project / 'changed.txt').write_text('original line\n')
    (project / 'notes.md').write_text('# Synthetic notes\n\nMarkdown preview fixture.\n')
    for command in (['init', '-q'], ['add', '.'], ['commit', '-q', '-m', 'synthetic base']):
        subprocess.run(['git', '-C', project, *command], check=True, env=env, capture_output=True)
    (project / 'changed.txt').write_text('original line\nchanged line\n')


def vscode_actions(project, actions):
    (project / '.mirror-actions.json').write_text(json.dumps(actions))
    wait_for(lambda: (project / '.mirror-actions.json.done').exists(), timeout=60)
    (project / '.mirror-actions.json.done').unlink(missing_ok=True)


def vscode_experiment():
    """What VS Code restores by itself: open a folder with assorted editors, close the window, reopen.

    Phase 1 leaves the dirty untitled editor in place (default settings); the result records
    the tabs before closing. Run `vscode-experiment-reopen` after the window was closed.
    """
    project = MIRROR / 'vscode-experiment'
    shutil.rmtree(project, ignore_errors=True)
    project.mkdir(parents=True)
    (project / 'file-a.txt').write_text('synthetic file tab\n')
    git_repository(project)
    code_settings()
    scale.launch(['code', '--new-window', '--ozone-platform=wayland', '--password-store=basic',
                  '--skip-welcome', '--skip-release-notes', '--disable-workspace-trust', project,
                  project / 'file-a.txt'], 'vscode-experiment')
    wait_for(lambda: any(project.name in w['title'] for w in scale.shell()['windows']
                         if w.get('wm_class') == 'com.microsoft.VSCode'), timeout=90)
    time.sleep(6)
    vscode_actions(project, [{'cmd': 'git-diff', 'file': 'changed.txt'}, {'cmd': 'markdown', 'file': 'notes.md'},
                             {'cmd': 'settings'}, {'cmd': 'terminal'},
                             {'cmd': 'untitled', 'text': 'synthetic untitled text\n'}])
    time.sleep(4)
    vscode_actions(project, [{'cmd': 'dump', 'out': 'tabs-before-close.json'}])
    save('vscode-experiment-before', json.loads((project / 'tabs-before-close.json').read_text()))


def vscode_experiment_reopen():
    project = MIRROR / 'vscode-experiment'
    scale.launch(['code', '--new-window', '--ozone-platform=wayland', '--password-store=basic',
                  '--skip-welcome', '--skip-release-notes', '--disable-workspace-trust', project], 'vscode-experiment')
    wait_for(lambda: any(project.name in w['title'] for w in scale.shell()['windows']
                         if w.get('wm_class') == 'com.microsoft.VSCode'), timeout=90)
    time.sleep(10)
    vscode_actions(project, [{'cmd': 'dump', 'out': 'tabs-after-reopen.json'}])
    save('vscode-experiment-after', json.loads((project / 'tabs-after-reopen.json').read_text()))


def vscode_extras(layout, workspace, untitled=False):
    """Diff, Markdown preview, Settings UI, untitled text and a terminal in one window."""
    index = next(i for i, e in enumerate(layout['vscode']) if e['placement']['workspace'] == workspace)
    project, _files = code_project(layout, index)
    vscode_actions(project, [{'cmd': 'git-diff', 'file': 'changed.txt'}, {'cmd': 'markdown', 'file': 'notes.md'},
                             {'cmd': 'settings'}, {'cmd': 'terminal'},
                             *([{'cmd': 'untitled', 'text': 'synthetic untitled text\n'}] if untitled else [])])


def vscode_windows(layout, extras_workspace):
    from workspace_state import vscode
    code_settings()
    argv = Path.home() / '.vscode/argv.json'
    argv.parent.mkdir(exist_ok=True)
    argv.write_text(json.dumps({'password-store': 'basic'}) + '\n')
    for index, entry in enumerate(layout['vscode']):
        project, files = code_project(layout, index)
        if entry['placement']['workspace'] == extras_workspace:
            git_repository(project)
        before = len(vscode._states(time.monotonic() + 2))
        scale.launch(['code', '--new-window', '--ozone-platform=wayland', '--password-store=basic',
                      '--skip-welcome', '--skip-release-notes', '--disable-workspace-trust',
                      project, *files], f'vscode-{index}')
        wait_for(lambda: len(vscode._states(time.monotonic() + 2)) == before + 1, timeout=90)
    if extras_workspace:
        time.sleep(5)
        vscode_extras(layout, extras_workspace)


def applications(layout, extras_workspace):
    from workspace_state import file_manager, vscode
    from workspace_state.social_apps import APPS, desktop_id, matching_windows
    wanted = {app['app'] for app in layout['social']}
    labels = {'slack': 'Slack', 'discord': 'Discord', 'telegram': 'Telegram', 'viber': 'Viber'}
    for app in APPS:
        if app.label.lower() in wanted and app.label in labels.values() and not matching_windows(app, scale.shell()):
            scale.launch(['gtk-launch', desktop_id(app)], 'social-' + app.label.lower())
    for number in range(len(layout['nemo'])):
        folder = MIRROR / 'folders' / f'folder-{number + 1}'
        folder.mkdir(parents=True, exist_ok=True)
        scale.launch(['nemo', '--no-default-window', folder], f'nemo-{number}')
        time.sleep(.5)
    wait_for(lambda: len(file_manager._bridge('GetState').get('windows', [])) == len(layout['nemo']))
    vscode_windows(layout, extras_workspace)
    wait_for(lambda: all(len(matching_windows(app, scale.shell())) == 1 for app in APPS
                         if app.label.lower() in wanted and app.label in labels.values()), timeout=120)


def place(layout, only=None):
    """Place every window of the layout; `only` limits it to one family."""
    from workspace_state.social_apps import APPS, matching_windows
    state = scale.shell()
    if only in (None, 'terminals'):
        for term in layout['terminals']:
            window = next(w for w in state['windows'] if w.get('wm_class') == 'Alacritty' and w['title'] == term['session'])
            place_window(window['id'], target_of(layout, term['placement']))
    if only in (None, 'nemo'):
        nemo = [w for w in state['windows'] if w.get('wm_class') == 'nemo']
        for window, entry in zip(sorted(nemo, key=lambda w: w['id']), layout['nemo']):
            place_window(window['id'], target_of(layout, entry['placement']))
    if only in (None, 'vscode'):
        code = sorted((w for w in state['windows'] if w.get('wm_class') == 'com.microsoft.VSCode'), key=lambda w: w['id'])
        for index, entry in enumerate(layout['vscode']):
            project, _files = code_project(layout, index)
            match = next(w for w in code if project.name in w['title'])
            place_window(match['id'], target_of(layout, entry['placement']))
    if only in (None, 'social'):
        for entry in layout['social']:
            for app in APPS:
                if app.label.lower() == entry['app']:
                    for window in matching_windows(app, scale.shell()):
                        place_window(window['id'], target_of(layout, entry['placement']))
    if only in (None, 'chrome'):
        place_chrome(layout)
    save('seed-desktop', scale.shell())


def sample(duration):
    """Record state and frame changes of VS Code, Chrome and terminal windows after login."""
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()[:8]
    directory = MIRROR / 'samples'
    directory.mkdir(parents=True, exist_ok=True)
    out = (directory / f'{time.strftime("%Y%m%dT%H%M%S")}-{boot}.jsonl').open('w')
    last, start = {}, time.time()
    while time.time() - start < duration:
        try:
            windows = json.loads(run('gnome-winctl', 'windows', '--json', timeout=10))
        except Exception:  # noqa: BLE001 - the shell may be busy during login
            time.sleep(.2)
            continue
        for window in windows:
            if window.get('wm_class') not in ('com.microsoft.VSCode', 'Alacritty') and 'google-chrome' not in str(window.get('app_ids')):
                continue
            item = {'workspace': window['workspace'], 'monitor': window['monitor'], 'state': window['state'],
                    'geometry': window['geometry'], 'title': window['title'][:40], 'class': window.get('wm_class')}
            if last.get(window['id']) != item:
                last[window['id']] = item
                out.write(json.dumps({'t': round(time.time() - start, 2), 'wall': time.time(), 'id': window['id'], **item}) + '\n')
                out.flush()
        time.sleep(.1)


def chrome_rows():
    from workspace_state.cli import _capture_all
    rows = []
    for profile in _capture_all()['browsers']['google_chrome']['profiles']:
        for window in profile['windows']:
            bounds, frame = window['bounds'], window.get('geometry') or {}
            rows.append({'state': window['state'], 'bounds': bounds, 'frame': frame,
                         'delta': {key: bounds[source] - frame[target] for key, source, target in (
                             ('x', 'left', 'x'), ('y', 'top', 'y'), ('width', 'width', 'width'),
                             ('height', 'height', 'height'))} if frame else None})
    return rows


def measure_chrome(layout):
    """Extension bounds against Mutter's frame rectangle, for maximized and one normal window."""
    result = {'maximized': chrome_rows()}
    window = next(w for w in scale.shell()['windows'] if 'google-chrome' in str(w.get('app_ids')))
    monitor = scale.shell()['monitors'][window['monitor']]
    target = {'workspace': scale.shell()['active_workspace'], 'monitor': window['monitor'], 'state': 'normal',
              'coordinate_space': 'monitor', 'geometry': {'x': 200, 'y': 200, 'width': 1200, 'height': 800}}
    place_window(window['id'], target)
    time.sleep(2)
    result['one_normal_window'] = chrome_rows()
    save('chrome-measure', result)
    print(json.dumps(result, indent=1))


def measure_vscode_checkpoint():
    """What the next periodic checkpoint recorded for the VS Code windows."""
    run('systemctl', '--user', 'start', 'wsctl-checkpoint.service', timeout=240)
    snapshot = json.loads((Path.home() / '.local/share/workspace-state/snapshots/current.json').read_text())
    windows = [{'workspace': w.get('placement', {}).get('workspace_name'), 'placement': w.get('placement'),
                'project': (w.get('folder_uri') or w.get('uri'))} for w in snapshot.get('vscode', {}).get('windows', [])]
    save('vscode-checkpoint', windows)
    print(json.dumps(windows, indent=1)[:4000])


def checkpoint(layout):
    result = subprocess.run(['wsctl', 'save'], text=True, capture_output=True, timeout=240)
    (MIRROR / 'checkpoint.log').write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError('Checkpoint failed; see wsctl-mirror/checkpoint.log')
    run('systemctl', '--user', 'enable', 'wsctl-gnome-session.service', check=False)
    save('checkpoint-done', {'time': time.time(), 'release': (Path.home() / '.local/share/workspace-state/desktop-releases/current').resolve().name})


# ---------------------------------------------------------------- verification

def window_summary(window, layout):
    names = layout['workspaces']
    return {'workspace': names[window['workspace']] if 0 <= window['workspace'] < len(names) else window['workspace'],
            'monitor': monitor_name(layout, window['monitor']), 'state': window['state'],
            'frame': {key: window['geometry_relative'][key] for key in ('x', 'y', 'width', 'height')}}


def monitor_name(layout, index):
    monitor = scale.shell()['monitors'][index]
    for entry in layout['monitors']:
        if (monitor['x'], monitor['y'], monitor['width'], monitor['height']) == (
                entry['x'], entry['y'], entry['width'], entry['height']):
            return entry['connector']
    return f'unknown-{index}'


def same_place(actual, wanted, *, frame):
    base = (actual['workspace'], actual['monitor'], actual['state']) == (
        wanted['workspace'], wanted['monitor'], wanted['state'])
    return base and (not frame or all(abs(actual['frame'][k] - wanted['frame'][k]) <= 2 for k in actual['frame']))



def descendants(pid):
    """PIDs of a process and everything below it, from /proc."""
    children = {}
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            try:
                fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
                children.setdefault(int(fields[1]), []).append(int(entry.name))
            except (OSError, IndexError):
                continue
    found, queue = [], [int(pid)]
    while queue:
        current = queue.pop()
        found.append(current)
        queue.extend(children.get(current, []))
    return found


def deck_recipe(pane_pid):
    """Profile and group of an Agent Deck dashboard running anywhere below a pane's shell.

    tmux reports the wrapper shell as the pane's command when the dashboard shares its
    process group, so the process tree is the honest observation.
    """
    for pid in descendants(pane_pid):
        try:
            argv = Path(f'/proc/{pid}/cmdline').read_bytes().rstrip(b'\0').decode().split('\0')
        except OSError:
            continue
        if argv and Path(argv[0]).name == 'agent-deck':
            fields = {'profile': '', 'group': ''}
            for flag, value in zip(argv[1:], argv[2:]):
                if flag in ('-p', '--profile'):
                    fields['profile'] = value
                if flag in ('-g', '--group'):
                    fields['group'] = value
            return fields
    return None


def pane_pids(session, index):
    return [int(v) for v in run('tmux', 'list-panes', '-t', f'={session}:{index}', '-F', '#{pane_pid}', check=False).split()]


def environ_path(pid):
    try:
        raw = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
    except OSError:
        return None
    return next((v[5:].decode() for v in raw if v.startswith(b'PATH=')), None)


def history(layout):
    """After a login with the per-tab history files: record each shell tab's id and commands."""
    expected, problems = {}, []
    tabs = re.compile(r'^[0-9a-f]{32}$')
    for term in layout['terminals']:
        name = term['session']
        for window in term['windows']:
            if window['kind'] != 'shell':
                continue
            target = f'={name}:{window["index"]}'
            tab = run('tmux', 'show-options', '-pqv', '-t', target, '@wsctl_tab', check=False).strip()
            if not tabs.match(tab):
                problems.append(f'{name}:{window["index"]} has no valid @wsctl_tab: {tab!r}')
                tab = None
            commands = [f'echo tabhist-{name}-{window["index"]}-{n + 1}' for n in range(HISTORY_COMMANDS)]
            for command in commands:
                run('tmux', 'send-keys', '-t', target, command, 'Enter')
                time.sleep(.4)
            expected[f'{name}:{window["index"]}'] = {'tab': tab, 'commands': commands}
    time.sleep(2)
    save('tab-history', expected)
    result = subprocess.run(['wsctl', 'save'], text=True, capture_output=True, timeout=240)
    if result.returncode:
        raise RuntimeError('Checkpoint failed after history seeding')
    snapshot = json.loads((Path.home() / '.local/share/workspace-state/snapshots/current.json').read_text())
    saved = {}
    for session in snapshot['sessions']:
        for window in session['windows']:
            for pane in window['panes']:
                saved[f'{session["name"]}:{window["index"]}'] = pane.get('tab_id')
    mismatched = {key: {'live': value['tab'], 'snapshot': saved.get(key)} for key, value in expected.items()
                  if value['tab'] != saved.get(key)}
    save('tab-history-seed', {'problems': problems, 'snapshot_tab_id_mismatch': mismatched, 'tabs': len(expected)})
    print(json.dumps({'problems': problems, 'snapshot_tab_id_mismatch': mismatched, 'tabs': len(expected)}, indent=1))


def history_exact(check):
    path = MIRROR / 'tab-history.json'
    if not path.exists():
        return
    expected = load(path)
    directory = Path.home() / '.local/state/zsh/tabs'
    shared = Path.home() / '.zsh_history'
    shared_text = shared.read_text(errors='replace') if shared.exists() else ''
    everything = {c for e in expected.values() for c in e['commands']}
    detail = {}
    for key, entry in expected.items():
        session, index = key.split(':')
        live = run('tmux', 'show-options', '-pqv', '-t', f'={session}:{index}', '@wsctl_tab', check=False).strip()
        file = directory / f'{entry["tab"]}.history'
        text = file.read_text(errors='replace') if entry['tab'] and file.exists() else ''
        output = Path(f'/tmp/mirror-fc-{session}-{index}.txt')
        output.unlink(missing_ok=True)
        # A leading space keeps this probe out of the history (hist_ignore_space).
        run('tmux', 'send-keys', '-t', f'={session}:{index}', f' fc -ln 1 > {output}', 'Enter', check=False)
        for _ in range(30):
            if output.exists() and output.stat().st_size:
                break
            time.sleep(.2)
        listed = output.read_text(errors='replace') if output.exists() else ''
        wrong = entry['commands']
        detail[key] = {
            'same_tab': live == entry['tab'] and bool(live),
            'file_missing': [c for c in wrong if c not in text],
            'file_foreign': sorted(c for c in everything - set(wrong) if c in text),
            'shared_missing': [c for c in wrong if c not in shared_text],
            'fc_missing': [c for c in wrong if c not in listed],
            'fc_foreign': sorted(c for c in everything - set(wrong) if c in listed),
        }
    bad = {k: v for k, v in detail.items() if not v['same_tab'] or any(v[f] for f in v if f != 'same_tab')}
    check('tab-history-exact', not bad, {'tabs': len(detail), 'bad': bad})


def verify(layout, extras_workspace):
    from workspace_state import file_manager, vscode
    from workspace_state.social_apps import APPS, matching_windows
    report = {'time': time.time(), 'checks': {}, 'failures': []}

    def check(name, ok, detail=None):
        report['checks'][name] = {'ok': bool(ok), 'detail': detail}
        if not ok:
            report['failures'].append(name)
    state = scale.shell()
    windows = state['windows']
    report['release'] = (Path.home() / '.local/share/workspace-state/desktop-releases/current').resolve().name
    # monitors
    live = sorted((m['x'], m['y'], m['width'], m['height'], bool(m['primary'])) for m in state['monitors'])
    want = sorted((m['x'], m['y'], m['width'], m['height'], bool(m['primary'])) for m in layout['monitors'])
    check('monitors', live == want, {'live': live, 'wanted': want})
    # terminals: names on windows, placement, tmux structure
    sessions = {}
    for line in run('tmux', 'list-windows', '-a', '-F', '#{session_name}\t#{window_index}\t#{window_name}', check=False).splitlines():
        name, index, window_name = line.split('\t')
        sessions.setdefault(name, []).append((int(index), window_name))
    alacritty = [w for w in windows if w.get('wm_class') == 'Alacritty']
    report['alacritty_titles'] = sorted(w['title'] for w in alacritty)
    for term in layout['terminals']:
        name = term['session']
        check(f'tmux-structure:{name}', sorted(sessions.get(name, [])) == sorted(
            (w['index'], w['name']) for w in term['windows']), {'live': sorted(sessions.get(name, []))})
        match = [w for w in alacritty if w['title'] == name]
        check(f'alacritty-name:{name}', len(match) == 1, {'titles': [w['title'] for w in alacritty]})
        if match:
            check(f'alacritty-place:{name}', same_place(window_summary(match[0], layout), term['placement'], frame=False),
                  window_summary(match[0], layout))
        for window in term['windows']:
            if window['kind'] == 'deck':
                recipes = [deck_recipe(pid) for pid in pane_pids(name, window['index'])]
                check(f'deck:{name}:{window["index"]}',
                      {'profile': PROFILE, 'group': window.get('group', '')} in recipes, recipes)
            for pid in pane_pids(name, window['index']):
                value = environ_path(pid) or ''
                check(f'path:{name}:{window["index"]}', 'desktop-releases/' not in value, value[:160])
            if window['kind'] == 'shell':
                pane = run('tmux', 'list-panes', '-t', f'={name}:{window["index"]}', '-F', '#{pane_current_command}', check=False)
                check(f'shell:{name}:{window["index"]}', pane.strip() == 'zsh', pane)
    check('alacritty-count', len(alacritty) == len(layout['terminals']), len(alacritty))
    # zsh history
    history = Path.home() / '.zsh_history'
    text = history.read_text(errors='replace') if history.exists() else ''
    expected = load(MIRROR / 'expected-history.json') if (MIRROR / 'expected-history.json').exists() else {}
    missing = [c for s in expected.values() for cs in s.values() for c in cs if c not in text]
    tab_files = list((Path.home() / '.local/state/zsh/tabs').glob('*.history')) if (Path.home() / '.local/state/zsh/tabs').is_dir() else []
    tab_text = ''.join(f.read_text(errors='replace') for f in tab_files)
    still = [c for c in missing if c not in tab_text]
    check('zsh-history', not still, {'missing': still, 'shared_file': history.exists(), 'tab_files': len(tab_files)})
    # Chrome
    from workspace_state import browser
    try:
        capture = browser.request_browser('capture', profile='Default')['windows']
    except Exception as error:  # noqa: BLE001 - report, never abort the whole report
        capture, report['chrome_error'] = [], str(error)
    def group_sizes(window):
        counts = {}
        for tab in window['tabs']:
            if tab.get('group'):
                counts[tab['group']] = counts.get(tab['group'], 0) + 1
        return tuple(sorted(counts.values()))
    wanted_chrome = sorted((w['tabs'], tuple(sorted(w['group_sizes']))) for w in layout['chrome'])
    live_chrome = sorted((len(w['tabs']), group_sizes(w)) for w in capture)
    check('chrome-windows', len(capture) == len(layout['chrome']), len(capture))
    check('chrome-tabs-groups', live_chrome == wanted_chrome, {'live': live_chrome, 'wanted': wanted_chrome})
    chrome_native = [w for w in windows if 'google-chrome' in str(w.get('app_ids'))]
    live_places = sorted(json.dumps({k: v for k, v in window_summary(w, layout).items() if k != 'frame'}, sort_keys=True) for w in chrome_native)
    want_places = sorted(json.dumps({k: spec[k] for k in ('workspace', 'monitor', 'state')}, sort_keys=True) for spec in layout['chrome'])
    check('chrome-placement', live_places == want_places, {'live': live_places, 'wanted': want_places})
    # VS Code
    code = [w for w in windows if w.get('wm_class') == 'com.microsoft.VSCode']
    live_code = sorted(json.dumps({k: v for k, v in window_summary(w, layout).items() if k != 'frame'}, sort_keys=True) for w in code)
    want_code = sorted(json.dumps({k: e['placement'][k] for k in ('workspace', 'monitor', 'state')}, sort_keys=True) for e in layout['vscode'])
    check('vscode-windows', live_code == want_code, {'live': live_code, 'wanted': want_code})
    # Nemo
    nemo = [w for w in windows if w.get('wm_class') == 'nemo']
    check('nemo-windows', len(nemo) == len(layout['nemo']) and all(
        any(same_place(window_summary(w, layout), e['placement'], frame=False) for w in nemo) for e in layout['nemo']), len(nemo))
    # social
    for entry in layout['social']:
        for app in APPS:
            if app.label.lower() == entry['app']:
                found = matching_windows(app, scale.shell())
                check(f'social:{entry["app"]}:{entry["placement"]["workspace"]}',
                      any(same_place(window_summary(w, layout), entry['placement'], frame=False) for w in found),
                      [window_summary(w, layout) for w in found])
    history_exact(check)
    # HUD
    status = Path(f'/run/user/{os.getuid()}/workspace-state/login-hud-status.json')
    hud = json.loads(status.read_text()) if status.exists() else {}
    report['hud'] = {'operation_state': hud.get('operation_state'), 'overall_state': hud.get('overall_state'),
                     'overall_message': hud.get('overall_message'),
                     'stages': {s['id']: s['state'] for s in hud.get('stages', [])}}
    check('hud-exists', bool(hud), report['hud']['operation_state'])
    # A dashboard window that is not running the dashboard must never leave the terminal stage verified.
    broken = [name for name, ok in ((n, c['ok']) for n, c in report['checks'].items() if n.startswith('deck:')) if not ok]
    if broken:
        check('hud-no-false-verified', report['hud']['stages'].get('terminals') != 'ready', {'broken_decks': broken})
    report['deck_dashboards'] = [line for line in run('ps', '-u', str(os.getuid()), '-o', 'pid=,args=').splitlines()
                                 if 'agent-deck -p' in line]
    report['agent_deck_sessions'] = len(json.loads(run('agent-deck', '-p', PROFILE, 'list', '--json', check=False) or '[]'))
    print(json.dumps(report, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--disposable-guest', action='store_true', required=True)
    parser.add_argument('--layout', required=True, help='private JSON layout (names, counts, placements)')
    parser.add_argument('--extras-workspace', help='workspace whose VS Code window gets diff, preview, settings and terminal')
    parser.add_argument('phase', choices=['seed', 'monitors', 'configure', 'deck', 'terminals', 'chrome', 'chrome-host',
                                          'applications', 'vscode', 'place', 'checkpoint', 'vscode-extras', 'vscode-experiment',
                                          'vscode-experiment-reopen', 'history', 'vscode-act', 'code-extension', 'sample', 'measure-chrome', 'measure-vscode', 'verify'])
    parser.add_argument('--duration', type=float, default=150)
    parser.add_argument('--actions', help='vscode-act: JSON list of fixture-extension actions for the extras workspace window')
    parser.add_argument('--only', choices=['terminals', 'nemo', 'vscode', 'social', 'chrome'], help='place: one family only')
    parser.add_argument('--with-untitled', action='store_true', help='vscode-extras: also open an untitled editor with text')
    args = parser.parse_args()
    if socket.gethostname() != 'wsctl-validation' or run('systemd-detect-virt') != 'kvm':
        raise SystemExit('Refusing outside wsctl-validation KVM guest')
    environment()
    layout = load(args.layout)
    MIRROR.mkdir(parents=True, mode=0o700, exist_ok=True)
    scale.ROOT.mkdir(parents=True, mode=0o700, exist_ok=True)
    phases = {'monitors': lambda: monitors(layout), 'configure': lambda: configure(layout),
              'deck': lambda: deck(layout), 'terminals': lambda: terminals(layout),
              'chrome': lambda: chrome(layout, args.layout), 'chrome-host': lambda: chrome_host(layout),
              'applications': lambda: applications(layout, args.extras_workspace), 'place': lambda: place(layout, args.only),
              'checkpoint': lambda: checkpoint(layout), 'verify': lambda: verify(layout, args.extras_workspace),
              'vscode-extras': lambda: vscode_extras(layout, args.extras_workspace, args.with_untitled),
              'history': lambda: history(layout), 'code-extension': code_settings, 'measure-chrome': lambda: measure_chrome(layout),
              'measure-vscode': measure_vscode_checkpoint, 'sample': lambda: sample(args.duration),
              'vscode-act': lambda: vscode_actions(code_project(layout, next(i for i, e in enumerate(layout['vscode']) if e['placement']['workspace'] == args.extras_workspace))[0], json.loads(args.actions)), 'vscode-experiment': vscode_experiment, 'vscode-experiment-reopen': vscode_experiment_reopen,
              'vscode': lambda: vscode_windows(layout, args.extras_workspace)}
    try:
        if args.phase == 'seed':
            for stage in ('monitors', 'configure', 'deck', 'terminals', 'chrome', 'applications', 'place', 'checkpoint'):
                save('seed-progress', {'stage': stage, 'time': time.time()})
                print('Mirror seed: ' + stage, flush=True)
                phases[stage]()
        else:
            phases[args.phase]()
    except Exception as error:
        save('failure-' + args.phase, {'error': type(error).__name__ + ': ' + str(error), 'time': time.time()})
        raise


if __name__ == '__main__':
    main()
