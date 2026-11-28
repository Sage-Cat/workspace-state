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
  seed     monitors, configure, deck, terminals, chrome, applications, place, checkpoint
  history  after a login with the per-tab history files: record every zsh tab's id and commands, then save
  verify   compare the live desktop with the layout, print a JSON report, exit 1 unless `failures` is empty
  vscode-extras   open the synthetic extra editors in one VS Code window (`--extras-workspace NAME`)
  monitors write ~/.config/monitors.xml for the layout and apply it live
Other phases are listed by `--help`; the verdict is documented in docs/testing.md.
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
from collections import Counter
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
    # run_vm_scale reads the expected Chrome content from here only when this variable names it.
    os.environ['WSCTL_CHROME_EXPECTATION'] = str(MIRROR / 'chrome-expected.json')


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
                    echo_command(f'mirror-{name}-{window["index"]}-{n + 1}') for n in range(HISTORY_COMMANDS)]
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
        process = Path(f'/proc/{pid}/cmdline')
        if str(ROOT / 'chrome-profile').encode() not in process.read_bytes():
            raise RuntimeError('Refusing to replace a non-fixture Chrome process')
        os.kill(pid, 15)
        wait_for(lambda: not process.exists(), timeout=15)
    ready = ROOT / 'chrome-ready.json'
    if ready.exists():
        prior = json.loads(ready.read_text())
        controller = Path(f'/proc/{prior["pid"]}/cmdline')
        if controller.exists():
            if str(ROOT / 'chrome-profile').encode() not in controller.read_bytes():
                raise RuntimeError('Old Chrome PID no longer belongs to this fixture')
            os.kill(prior['pid'], 15)
            wait_for(lambda: not controller.exists(), timeout=10)
        ready.unlink()
    profile = ROOT / 'chrome-profile'
    if profile.exists():
        archived = ROOT / 'seeds' / ('chrome-' + uuid.uuid4().hex)
        archived.parent.mkdir(exist_ok=True)
        profile.rename(archived)
    (ROOT / 'chrome-ready.json').unlink(missing_ok=True)
    # Kept beside the mirror state, never in the scale fixture's directory (run_vm_scale reads it from there).
    save('chrome-expected', {'windows': len(layout['chrome']), 'tabs': sum(w['tabs'] for w in layout['chrome']),
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
        } else if (a.cmd === 'chat') {
          await vscode.commands.executeCommand('workbench.action.chat.open');
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
        subprocess.run(['git', '-C', project, *command], check=True, env=env, capture_output=True, timeout=30)
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


def applications(layout, extras_workspace, skip_apps=()):
    from workspace_state import file_manager, vscode
    from workspace_state.social_apps import APPS, desktop_id, matching_windows
    wanted = {app['app'] for app in layout['social']}
    for name in sorted(wanted - set(SOCIAL_APPS)):
        if f'app:{name}' not in skip_apps:
            raise RuntimeError(f'unsupported app {name} (exclude it explicitly with --expect-without app:{name})')
    for app in APPS:
        if app.label.lower() in wanted and app.label.lower() in SOCIAL_APPS and not matching_windows(app, scale.shell()):
            scale.launch(['gtk-launch', desktop_id(app)], 'social-' + app.label.lower())
    for number in range(len(layout['nemo'])):
        folder = MIRROR / 'folders' / f'folder-{number + 1}'
        folder.mkdir(parents=True, exist_ok=True)
        scale.launch(['nemo', '--no-default-window', folder], f'nemo-{number}')
        time.sleep(.5)
    wait_for(lambda: len(file_manager._bridge('GetState').get('windows', [])) == len(layout['nemo']))
    vscode_windows(layout, extras_workspace)
    wait_for(lambda: all(len(matching_windows(app, scale.shell())) == sum(1 for e in layout['social'] if e['app'] == app.label.lower())
                         for app in APPS if app.label.lower() in wanted and app.label.lower() in SOCIAL_APPS), timeout=120)


def place(layout, only=None, skip_apps=()):
    """Place every window of the layout; `only` limits it to one family."""
    from workspace_state.social_apps import APPS, matching_windows
    state = scale.shell()
    if only in (None, 'terminals'):
        for term in layout['terminals']:
            window = next(w for w in state['windows'] if w.get('wm_class') == 'Alacritty' and terminal_name(w['title']) == term['session'])
            place_window(window['id'], target_of(layout, term['placement']))
    if only in (None, 'nemo'):
        nemo = [w for w in state['windows'] if w.get('wm_class') == 'nemo']
        if len(nemo) != len(layout['nemo']):
            raise RuntimeError(f'Expected {len(layout["nemo"])} file-manager windows, found {len(nemo)}')
        for window, entry in zip(sorted(nemo, key=lambda w: w['id']), layout['nemo']):
            place_window(window['id'], target_of(layout, entry['placement']))
    if only in (None, 'vscode'):
        code = sorted((w for w in state['windows'] if w.get('wm_class') == 'com.microsoft.VSCode'), key=lambda w: w['id'])
        for index, entry in enumerate(layout['vscode']):
            project, _files = code_project(layout, index)
            match = next(w for w in code if w['title'] == project.name or w['title'].startswith(project.name + ' - '))
            place_window(match['id'], target_of(layout, entry['placement']))
    if only in (None, 'social'):
        by_app = {}
        for entry in layout['social']:
            by_app.setdefault(entry['app'], []).append(entry)
        for name, entries in by_app.items():
            if name not in SOCIAL_APPS:
                if f'app:{name}' in skip_apps:
                    continue
                raise RuntimeError(f'unsupported app {name}')
            app = next(a for a in APPS if a.label.lower() == name)
            windows = sorted(matching_windows(app, scale.shell()), key=lambda w: w['id'])
            if len(windows) != len(entries):
                raise RuntimeError(f'{name}: layout has {len(entries)} windows, found {len(windows)}')
            for window, entry in zip(windows, entries):
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


def saved_placement(snapshot, provider, item):
    """The saved placement of the window a failed HUD item was supposed to reach, from the snapshot."""
    name = item.split('/')[0]
    try:
        if provider == 'alacritty':
            return next((x.get('placement') for x in snapshot['sessions'] if x['name'] == name), None)
        if provider == 'vscode':
            return snapshot['vscode']['windows'][int(item) - 1].get('placement')
        if provider in ('file-manager', 'nemo'):
            number = int(item.split(':')[-1]) if item.split(':')[-1].isdigit() else 1
            return snapshot['file_manager']['windows'][number - 1].get('placement')
        if '/window-' in item:
            for profile in snapshot['browsers']['google_chrome']['profiles']:
                for window in profile['windows']:
                    if window['id'] == item.split('/')[1]:
                        return {key: window.get(key) for key in ('workspace', 'workspace_index', 'monitor', 'state', 'bounds', 'geometry')}
            return None
        if ':' in item:
            app, number = item.split(':')
            return snapshot['social_apps'][app]['windows'][int(number) - 1]
    except (KeyError, IndexError, ValueError, TypeError):
        return None
    return None


def placement_failures():
    """JSON lines, one per failed placement in the login status: raw request record, saved target, live windows."""
    status = json.loads(Path(f'/run/user/{os.getuid()}/workspace-state/login-hud-status.json').read_text())
    snapshot = json.loads((Path.home() / '.local/share/workspace-state/snapshots/current.json').read_text())
    windows = json.loads(run('gnome-winctl', 'windows', '--json', timeout=30))
    classes = {'alacritty': 'Alacritty', 'chrome': 'google-chrome', 'vscode': 'com.microsoft.VSCode',
               'nemo': 'nemo', 'file-manager': 'nemo'}
    for stage in status.get('stages', []):
        for result in stage.get('provider_results') or []:
            placement = result.get('placement') or {}
            if placement.get('state') not in ('failed', 'expired', 'cancelled', 'rejected'):
                continue
            request = placement.get('request_id') or ''
            token = request.split(':', 1)[1] if ':' in request else request
            raw = subprocess.run(['gnome-winctl', 'expectation', token], text=True, capture_output=True, timeout=30) if token else None
            try:
                record = json.loads(raw.stdout) if raw and raw.stdout.strip() else None
            except ValueError:
                record = None
            provider, item = result.get('provider', ''), result.get('item_id', '')
            wanted = classes.get(provider)
            live = [w for w in windows if (wanted and (w.get('wm_class') == wanted or wanted in str(w.get('app_ids'))))
                    or (provider in item.split(':')[0] or item.split(':')[0] in (w.get('wm_class') or ''))]
            print(json.dumps({'stage': stage.get('id'), 'provider': provider, 'item_id': item, 'result': result,
                              'request_id': request, 'request_record': record,
                              'request_stdout': (raw.stdout if raw else None), 'request_stderr': (raw.stderr if raw else None),
                              'saved_placement': saved_placement(snapshot, provider, item), 'windows_now': live,
                              'time': time.time()}))


def ticket_watch(duration, tag):
    """After login: when each placement request is first seen, when it resolves and how (HUD state alongside)."""
    directory = MIRROR / 'ticket-watch'
    directory.mkdir(parents=True, exist_ok=True)
    out = (directory / f'{tag}.jsonl').open('w')
    status_path = Path(f'/run/user/{os.getuid()}/workspace-state/login-hud-status.json')
    seen, done, start = {}, {}, time.time()
    while time.time() - start < duration:
        try:
            status = json.loads(status_path.read_text())
        except (OSError, ValueError):
            time.sleep(.5)
            continue
        for stage in status.get('stages', []):
            for result in stage.get('provider_results') or []:
                placement = result.get('placement') or {}
                request = placement.get('request_id') or ''
                if not request:
                    continue
                token = request.split(':', 1)[1] if ':' in request else request
                hud_state = placement.get('state')
                if token in done and done[token] == hud_state:
                    continue
                now = time.time()
                seen.setdefault(token, now)
                try:
                    raw = subprocess.run(['gnome-winctl', 'expectation', token], text=True, capture_output=True, timeout=20)
                    record = json.loads(raw.stdout)
                except (subprocess.TimeoutExpired, ValueError):
                    record = {}
                verification = record.get('verification') or {}
                key = (record.get('status'), hud_state)
                if seen.get(('last', token)) != key:
                    seen[('last', token)] = key
                    out.write(json.dumps({'wall': now, 't': round(now - start, 2), 'first_seen': seen[token], 'token': token,
                                          'stage': stage.get('id'), 'item_id': result.get('item_id'), 'hud_placement': hud_state,
                                          'status': record.get('status'), 'placed': record.get('placed'),
                                          'message': record.get('message'), 'elapsed_us': verification.get('elapsed_us'),
                                          'attempts': verification.get('attempts'), 'stable_us': verification.get('stable_us')}) + '\n')
                    out.flush()
                if record.get('status') in ('verified', 'failed', 'expired', 'cancelled') and hud_state not in ('waiting', 'pending', None):
                    done[token] = hud_state
        time.sleep(.5)


def checkpoint(layout):
    result = subprocess.run(['wsctl', 'save'], text=True, capture_output=True, timeout=240)
    (MIRROR / 'checkpoint.log').write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError('Checkpoint failed; see wsctl-mirror/checkpoint.log')
    run('systemctl', '--user', 'enable', 'wsctl-gnome-session.service', check=False)
    save('checkpoint-done', {'time': time.time(), 'release': (Path.home() / '.local/share/workspace-state/desktop-releases/current').resolve().name})


# ---------------------------------------------------------------- verification

SOCIAL_APPS = {'slack': 'Slack', 'discord': 'Discord', 'telegram': 'Telegram', 'viber': 'Viber'}
# Window managers round frames to decoration and grid steps; 8 px absorbs that and still catches a stalled resize
# (the defects seen in the guest are 20 px or more).
FRAME_TOLERANCE = 8
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def echo_command(text):
    """A typed history command; the text is quoted so a layout value can never inject shell syntax."""
    return 'echo ' + shlex.quote(text)


def vscode_project_name(layout, index):
    return f'{index + 1}-{layout["vscode"][index]["placement"]["workspace"].lower()}'


def monitor_name_of(monitors, layout, index):
    """Layout connector of the live monitor at `index`, by geometry; 'unknown-N' when none matches."""
    if not 0 <= index < len(monitors):
        return f'unknown-{index}'
    monitor = monitors[index]
    for entry in layout['monitors']:
        if (monitor['x'], monitor['y'], monitor['width'], monitor['height']) == (
                entry['x'], entry['y'], entry['width'], entry['height']):
            return entry['connector']
    return f'unknown-{index}'


def summarize_window(window, monitors, layout):
    names = layout['workspaces']
    return {'workspace': names[window['workspace']] if 0 <= window['workspace'] < len(names) else window['workspace'],
            'monitor': monitor_name_of(monitors, layout, window['monitor']), 'state': window['state'],
            'frame': {key: window['geometry_relative'][key] for key in ('x', 'y', 'width', 'height')}}


def place_key(summary):
    return (summary['workspace'], summary['monitor'], summary['state'])


def same_place(actual, wanted, *, frame):
    base = place_key(actual) == place_key(wanted)
    return base and (not frame or all(abs(actual['frame'][k] - wanted['frame'][k]) <= FRAME_TOLERANCE for k in actual['frame']))


def terminal_name(title):
    """Session name shown by a terminal window; a static wsctl:<name>:<hex> header names it too."""
    found = re.fullmatch(r'wsctl:(.+):[0-9a-f]{8}', title)
    return found.group(1) if found else title


def window_key(window, summary):
    if window.get('wm_class') == 'Alacritty':
        return ('Alacritty', terminal_name(window['title']))
    return (window.get('wm_class'), summary['workspace'], summary['monitor'])


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


def cmdline(pid):
    try:
        return Path(f'/proc/{pid}/cmdline').read_bytes().rstrip(b'\0').decode().split('\0')
    except OSError:
        return []


def deck_recipe(pane_pid):
    """Profile and group of an Agent Deck dashboard running anywhere below a pane's shell.

    tmux reports the wrapper shell as the pane's command when the dashboard shares its
    process group, so the process tree is the honest observation.
    """
    for pid in descendants(pane_pid):
        argv = cmdline(pid)
        if argv and Path(argv[0]).name == 'agent-deck':
            fields = {'profile': '', 'group': ''}
            for flag, value in zip(argv[1:], argv[2:]):
                if flag in ('-p', '--profile'):
                    fields['profile'] = value
                if flag in ('-g', '--group'):
                    fields['group'] = value
            return fields
    return None


def conversation_uuids(pane_pid):
    """UUIDs of conversation clients (`... resume <uuid>`) running below a pane's shell."""
    found = set()
    for pid in descendants(pane_pid):
        argv = cmdline(pid)
        for flag, value in zip(argv, argv[1:]):
            if flag == 'resume' and UUID.fullmatch(value):
                found.add(value)
    return found


def pane_rows(session, index):
    rows = []
    for line in run('tmux', 'list-panes', '-t', f'={session}:{index}', '-F', '#{pane_pid}\t#{pane_current_command}',
                    check=False).splitlines():
        pid, _, command = line.partition('\t')
        if pid.isdigit():
            rows.append({'pid': int(pid), 'command': command})
    return rows


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
            commands = [echo_command(f'tabhist-{name}-{window["index"]}-{n + 1}') for n in range(HISTORY_COMMANDS)]
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


def observe_tab_history(without):
    """Live evidence for every recorded shell tab: id, own history file, shared file and `fc -ln`."""
    path = MIRROR / 'tab-history.json'
    if not path.exists():
        return None
    expected = load(path)
    directory = Path.home() / '.local/state/zsh/tabs'
    shared = Path.home() / '.zsh_history'
    live = {}
    for key, entry in expected.items():
        if key in without:
            continue
        session, index = key.split(':')
        tab = run('tmux', 'show-options', '-pqv', '-t', f'={session}:{index}', '@wsctl_tab', check=False).strip()
        file = directory / f'{entry["tab"]}.history'
        output = Path(f'/tmp/mirror-fc-{session}-{index}.txt')
        output.unlink(missing_ok=True)
        # A leading space keeps this probe out of the history (hist_ignore_space).
        run('tmux', 'send-keys', '-t', f'={session}:{index}', f' fc -ln 1 > {shlex.quote(str(output))}', 'Enter', check=False)
        for _ in range(30):
            if output.exists() and output.stat().st_size:
                break
            time.sleep(.2)
        live[key] = {'tab': tab, 'file_text': file.read_text(errors='replace') if entry['tab'] and file.exists() else '',
                     'fc_text': output.read_text(errors='replace') if output.exists() else ''}
    return {'expected': expected, 'live': live,
            'shared_text': shared.read_text(errors='replace') if shared.exists() else ''}


def observe_chrome(layout, monitors):
    """One record per Chrome window with its content and its native placement, from the same capture."""
    from workspace_state.cli import _capture_all
    rows = []
    for profile in _capture_all()['browsers']['google_chrome']['profiles']:
        for window in profile['windows']:
            counts = {}
            for tab in window['tabs']:
                if tab.get('group'):
                    counts[tab['group']] = counts.get(tab['group'], 0) + 1
            geometry = (window.get('monitor') or {}).get('geometry') or {}
            monitor = next((e['connector'] for e in layout['monitors'] if (
                geometry.get('x'), geometry.get('y'), geometry.get('width'), geometry.get('height')) == (
                e['x'], e['y'], e['width'], e['height'])), 'unknown')
            rows.append({'tabs': len(window['tabs']), 'groups': tuple(sorted(counts.values())),
                         'workspace': window.get('workspace'), 'monitor': monitor, 'state': window.get('state')})
    return rows


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def session_started():
    """Epoch of the graphical session's start, from logind."""
    session = run('loginctl', 'show-user', str(os.getuid()), '-p', 'Display', '--value', check=False).strip()
    stamp = run('loginctl', 'show-session', session, '-p', 'Timestamp', '--value', check=False).strip() if session else ''
    if not stamp:
        return None
    return float(run('date', '-d', stamp, '+%s', check=False) or 0) or None


def _reference_frames(path):
    """Earlier frames, or None when the file is absent or was cut short by a power loss."""
    if not path:
        return None
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return None
    if not text.strip():
        return None
    return json.loads(text)


def _write_durably(path, text):
    """Atomic and synced: an abrupt power-off right after must not leave an empty file."""
    destination = Path(path)
    temporary = destination.with_name(destination.name + '.tmp')
    with open(temporary, 'w') as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(destination)
    directory = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def observe(layout, without, before_path=None):
    state = scale.shell()
    tmux = {}
    for line in run('tmux', 'list-windows', '-a', '-F', '#{session_name}\t#{window_index}\t#{window_name}', check=False).splitlines():
        name, index, window_name = line.split('\t')
        tmux.setdefault(name, []).append((int(index), window_name))
    panes = {}
    for term in layout['terminals']:
        for window in term['windows']:
            key = f'{term["session"]}:{window["index"]}'
            rows = pane_rows(term['session'], window['index'])
            for row in rows:
                row['deck'] = deck_recipe(row['pid'])
                row['uuids'] = sorted(conversation_uuids(row['pid']))
                row['path'] = environ_path(row['pid'])
            panes[key] = rows
    history_file = Path.home() / '.zsh_history'
    expected_history = load(MIRROR / 'expected-history.json') if (MIRROR / 'expected-history.json').exists() else None
    tab_dir = Path.home() / '.local/state/zsh/tabs'
    status = Path(f'/run/user/{os.getuid()}/workspace-state/login-hud-status.json')
    try:
        chrome_rows = observe_chrome(layout, state['monitors'])
        chrome_error = None
    except Exception as error:  # noqa: BLE001 - reported as a failed check, not an abort
        chrome_rows, chrome_error = [], str(error)
    return {
        'monitors': state['monitors'], 'windows': state['windows'], 'tmux': tmux, 'panes': panes,
        'chrome': chrome_rows, 'chrome_error': chrome_error,
        'expected_history': expected_history,
        'shared_history_text': history_file.read_text(errors='replace') if history_file.exists() else '',
        'tab_history_text': ''.join(f.read_text(errors='replace') for f in tab_dir.glob('*.history')) if tab_dir.is_dir() else '',
        'tab_history': observe_tab_history(set(without)),
        'conversation_ids': load(MIRROR / 'conversation-identities.json') if (MIRROR / 'conversation-identities.json').exists() else None,
        'hud': json.loads(status.read_text()) if status.exists() else {},
        'boot_id': boot_id(), 'session_started': session_started(),
        'supported_apps': sorted(SOCIAL_APPS),
        'before': _reference_frames(before_path),
    }


def window_snapshot(obs, layout):
    """Frames of the live windows, keyed so that the same window is found again after a cycle."""
    out = []
    for w in obs['windows']:
        summary = summarize_window(w, obs['monitors'], layout)
        out.append({'key': list(window_key(w, summary)), 'state': summary['state'], 'frame': summary['frame']})
    return out


def evaluate(obs, layout, without=(), extras=None):
    """Pure verdict over one observation; every check has a unique name and `failures` is the only verdict."""
    without = set(without)
    report = {'time': time.time(), 'checks': {}, 'failures': []}

    def check(name, ok, detail=None):
        base, number = name, 1
        while name in report['checks']:
            number += 1
            name = f'{base}#{number}'
        report['checks'][name] = {'ok': bool(ok), 'detail': detail}
        if not ok:
            report['failures'].append(name)
    monitors, windows = obs['monitors'], obs['windows']
    summaries = {id(w): summarize_window(w, monitors, layout) for w in windows}
    live = sorted((m['x'], m['y'], m['width'], m['height'], bool(m['primary'])) for m in monitors)
    want = sorted((m['x'], m['y'], m['width'], m['height'], bool(m['primary'])) for m in layout['monitors'])
    check('monitors', live == want, {'live': live, 'wanted': want})
    # terminals
    alacritty = [w for w in windows if w.get('wm_class') == 'Alacritty']
    report['alacritty_titles'] = sorted(w['title'] for w in alacritty)
    expected_sessions = [t for t in layout['terminals']]
    for term in expected_sessions:
        name = term['session']
        present = [w for w in term['windows'] if f'{name}:{w["index"]}' not in without]
        check(f'tmux-structure:{name}', sorted(obs['tmux'].get(name, [])) == sorted((w['index'], w['name']) for w in present),
              {'live': sorted(obs['tmux'].get(name, []))})
        match = [w for w in alacritty if terminal_name(w['title']) == name]
        exact = [w for w in alacritty if w['title'] == name]
        check(f'alacritty-name:{name}', len(exact) == 1, {'titles': [w['title'] for w in alacritty]})
        check(f'alacritty-window:{name}', len(match) == 1, {'matching': len(match)})
        if len(match) == 1:
            check(f'alacritty-place:{name}', same_place(summaries[id(match[0])], term['placement'], frame=False), summaries[id(match[0])])
        for window in present:
            key = f'{name}:{window["index"]}'
            rows = obs['panes'].get(key, [])
            if not rows:
                check(f'pane:{key}', False, 'no pane observed')
            for position, row in enumerate(rows):
                path = row.get('path')
                check(f'path:{key}:{position}', path is not None and 'desktop-releases/' not in path,
                      'environment unreadable' if path is None else path[:160])
                if window['kind'] == 'shell':
                    check(f'shell:{key}:{position}', row['command'].strip() == 'zsh', row['command'])
            if window['kind'] == 'deck':
                recipes = [row.get('deck') for row in rows]
                check(f'deck:{key}', {'profile': PROFILE, 'group': window.get('group', '')} in recipes, recipes)
    check('alacritty-count', len(alacritty) == len(expected_sessions), len(alacritty))
    # conversations: every expected identity runs as a client in its own window (not a shell, not another UUID)
    ids = obs.get('conversation_ids')
    if ids is None:
        check('conversations', False, 'not checked: conversation-identities.json missing')
    else:
        for term in layout['terminals']:
            for window in term['windows']:
                key = f'{term["session"]}:{window["index"]}'
                if window['kind'] != 'conversation' or key in without:
                    continue
                wanted = conversation_id(term['session'], window['index'])
                running = {u for row in obs['panes'].get(key, []) for u in row.get('uuids', [])}
                check(f'conversation:{key}', wanted in ids and wanted in running,
                      {'wanted': wanted, 'recorded': wanted in ids, 'running': sorted(running)})
        excluded = {conversation_id(t['session'], w['index']) for t in layout['terminals'] for w in t['windows']
                    if f'{t["session"]}:{w["index"]}' in without}
        known = {conversation_id(t['session'], w['index']) for t in layout['terminals'] for w in t['windows']
                 if w['kind'] == 'conversation'}
        check('conversations', set(ids) - excluded <= known, {'unexpected_identities': sorted(set(ids) - excluded - known)})
    # zsh history
    expected = obs.get('expected_history')
    if expected is None:
        check('zsh-history', False, 'not checked: expected-history.json missing')
    else:
        missing = [c for s in expected.values() for cs in s.values() for c in cs
                   if c not in obs['shared_history_text'] and c not in obs['tab_history_text']]
        check('zsh-history', not missing, {'missing': missing})
    tab = obs.get('tab_history')
    if tab is None:
        check('tab-history-exact', False, 'not checked: tab-history.json missing')
    else:
        everything = {c for k, e in tab['expected'].items() if k not in without for c in e['commands']}
        bad = {}
        for key, entry in tab['expected'].items():
            if key in without:
                continue
            seen = tab['live'].get(key)
            if seen is None:
                bad[key] = 'no live observation'
                continue
            commands = entry['commands']
            detail = {
                'same_tab': seen['tab'] == entry['tab'] and bool(seen['tab']),
                'file_missing': [c for c in commands if c not in seen['file_text']],
                'file_foreign': sorted(c for c in everything - set(commands) if c in seen['file_text']),
                'shared_missing': [c for c in commands if c not in tab['shared_text']],
                'fc_missing': [c for c in commands if c not in seen['fc_text']],
                'fc_foreign': sorted(c for c in everything - set(commands) if c in seen['fc_text'])}
            if not detail['same_tab'] or any(detail[f] for f in detail if f != 'same_tab'):
                bad[key] = detail
        check('tab-history-exact', not bad, {'tabs': len(tab['live']), 'bad': bad})
    # Chrome: content and placement together, as one multiset
    if obs.get('chrome_error'):
        check('chrome-capture', False, obs['chrome_error'])
    wanted_chrome = sorted((w['tabs'], tuple(sorted(w['group_sizes'])), w['workspace'], w['monitor'], w['state']) for w in layout['chrome'])
    live_chrome = sorted((c['tabs'], tuple(c['groups']), c['workspace'], c['monitor'], c['state']) for c in obs['chrome'])
    check('chrome-windows', live_chrome == wanted_chrome, {'live': live_chrome, 'wanted': wanted_chrome})
    # VS Code: each layout window is found by its project name in the title
    code = [w for w in windows if w.get('wm_class') == 'com.microsoft.VSCode']
    for index, entry in enumerate(layout['vscode']):
        name = vscode_project_name(layout, index)
        found = [w for w in code if w['title'] == name or w['title'].startswith(name + ' - ')]
        check(f'vscode-window:{name}', len(found) == 1 and same_place(summaries[id(found[0])], entry['placement'], frame=False),
              [summaries[id(w)] for w in found])
    check('vscode-count', len(code) == len(layout['vscode']), len(code))
    # maximized windows must fill the work area; no evidence is a failure
    maximized = [w for w in windows if w['state'] == 'maximized']
    no_area = [w.get('wm_class') for w in maximized if not w.get('monitor_work_area')]
    wrong = []
    for w in maximized:
        area = w.get('monitor_work_area')
        if area and (w['geometry']['width'], w['geometry']['height'], w['geometry']['x'], w['geometry']['y']) != (
                area['width'], area['height'], area['x'], area['y']):
            wrong.append({'class': w.get('wm_class'), 'title': w['title'][:30], 'frame': w['geometry'], 'work_area': area})
    layout_maximized = sum(1 for _ in _layout_placements(layout) if _['state'] == 'maximized')
    if no_area:
        check('maximized-frames', False, {'no_monitor_work_area': no_area})
    elif layout_maximized and not maximized:
        check('maximized-frames', False, 'no maximized window observed although the layout has maximized windows')
    else:
        check('maximized-frames', not wrong, wrong)
    # Nemo, one multiset
    nemo = [summaries[id(w)] for w in windows if w.get('wm_class') == 'nemo']
    check('nemo-windows', sorted(map(place_key, nemo)) == sorted(place_key(e['placement']) for e in layout['nemo']),
          {'live': sorted(map(place_key, nemo)), 'wanted': sorted(place_key(e['placement']) for e in layout['nemo'])})
    # social: unsupported apps fail loudly unless excluded explicitly (`--expect-without app:NAME`)
    wanted_by_app = {}
    for entry in layout['social']:
        wanted_by_app.setdefault(entry['app'], []).append(entry['placement'])
    for app, placements in sorted(wanted_by_app.items()):
        if f'app:{app}' in without:
            check(f'social-skipped:{app}', True, 'explicitly excluded; not verified')
            continue
        if app not in SOCIAL_APPS:
            check(f'social-unsupported:{app}', False, f'unsupported app {app}')
            continue
        label = SOCIAL_APPS[app]
        found = [summaries[id(w)] for w in windows if _social_matches(w, app, label)]
        check(f'social:{app}', sorted(map(place_key, found)) == sorted(map(place_key, placements)),
              {'live': sorted(map(place_key, found)), 'wanted': sorted(map(place_key, placements))})
    # frames of normal windows against the same window before the cycle
    before = obs.get('before')
    if before is None:
        check('normal-frames', False, 'not checked: no before-windows reference (use --before-windows FILE)')
    else:
        reference = {}
        for item in before:
            if item['state'] == 'normal':
                reference.setdefault(tuple(item['key']), []).append(item['frame'])
        problems = []
        for w in windows:
            summary = summaries[id(w)]
            if summary['state'] != 'normal':
                continue
            key = window_key(w, summary)
            if key not in reference:
                problems.append({'window': list(key), 'problem': 'no reference frame before the cycle'})
                continue
            frames = reference[key]
            if not any(same_place({**summary, 'frame': f}, summary, frame=True) for f in frames):
                problems.append({'window': list(key), 'frame': summary['frame'], 'before': frames})
        check('normal-frames', not problems, problems)
    # HUD: the document must belong to this boot and to this login
    hud = obs.get('hud') or {}
    context = hud.get('operation_context') or {}
    report['hud'] = {'operation_state': hud.get('operation_state'), 'overall_state': hud.get('overall_state'),
                     'overall_message': hud.get('overall_message'),
                     'stages': {s['id']: s['state'] for s in hud.get('stages', [])}}
    started = hud.get('started_at')
    fresh = bool(hud) and context.get('boot_id') == obs.get('boot_id')
    stale_reason = None if fresh else 'status missing or from another boot'
    if fresh:
        if not obs.get('session_started') or not started:
            fresh, stale_reason = False, 'cannot prove the status belongs to this login: session start or started_at missing'
        else:
            try:
                from datetime import datetime
                begun = datetime.fromisoformat(started).timestamp()
                if begun < obs['session_started'] - 2:
                    fresh, stale_reason = False, 'status started before this login'
            except ValueError:
                fresh, stale_reason = False, 'unreadable started_at'
    check('hud-exists', fresh, {'operation_state': hud.get('operation_state'), 'reason': stale_reason})
    broken = [n for n, c in report['checks'].items() if n.startswith('deck:') and not c['ok']]
    if broken:
        check('hud-no-false-verified', report['hud']['stages'].get('terminals') != 'ready', {'broken_decks': broken})
    return report


def _layout_placements(layout):
    for key in ('terminals', 'vscode', 'nemo', 'social', 'chrome'):
        for entry in layout.get(key, []):
            yield entry['placement'] if 'placement' in entry else entry


def _social_matches(window, app, label):
    cls = (window.get('wm_class') or '').lower()
    ids = ' '.join(map(str, window.get('app_ids') or [])).lower()
    return app in cls or app in ids


def exit_code(report):
    """The verdict: 0 only when no check failed."""
    return 1 if report['failures'] else 0


def verify(layout, extras_workspace, without=(), save_windows=None, before_windows=None):
    report_obs = observe(layout, without, before_windows)
    report = evaluate(report_obs, layout, without, extras_workspace)
    report['release'] = (Path.home() / '.local/share/workspace-state/desktop-releases/current').resolve().name
    if save_windows:
        _write_durably(save_windows, json.dumps(window_snapshot(report_obs, layout)))
    report['deck_dashboards'] = [line for line in run('ps', '-u', str(os.getuid()), '-o', 'pid=,args=', check=False).splitlines()
                                 if 'agent-deck -p' in line]
    try:
        listing = json.loads(run('agent-deck', '-p', PROFILE, 'list', '--json', check=False))
        report['agent_deck_sessions'] = len(listing)
        statuses = Counter(row.get('status') for row in listing if not row.get('archived'))
        report['agent_deck_statuses'] = dict(statuses)
        # A session that died with the previous boot must read as stopped, never as a crash.
        errored = sorted(row.get('title') or row.get('id') for row in listing
                         if not row.get('archived') and row.get('status') == 'error')
        report['checks']['agent-deck-status'] = {
            'ok': not errored, 'detail': f'error: {errored}' if errored else dict(statuses)}
        if errored:
            report['failures'].append('agent-deck-status')
    except (ValueError, OSError, subprocess.SubprocessError):
        report['agent_deck_sessions'] = None
        report['checks']['agent-deck-list'] = {'ok': False, 'detail': 'agent-deck list did not return JSON'}
        report['failures'].append('agent-deck-list')
    print(json.dumps(report, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--disposable-guest', action='store_true', required=True)
    parser.add_argument('--layout', required=True, help='private JSON layout (names, counts, placements)')
    parser.add_argument('--extras-workspace', help='workspace whose VS Code window gets diff, preview, settings and terminal')
    parser.add_argument('phase', choices=['seed', 'monitors', 'configure', 'deck', 'terminals', 'chrome', 'chrome-host',
                                          'applications', 'vscode', 'place', 'checkpoint', 'vscode-extras', 'vscode-experiment',
                                          'vscode-experiment-reopen', 'history', 'vscode-act', 'code-extension', 'sample', 'measure-chrome', 'measure-vscode', 'placement-failures', 'ticket-watch', 'verify'])
    parser.add_argument('--tag', default='login')
    parser.add_argument('--expect-without', action='append', metavar='SESSION:INDEX', help='verify: this saved window is intentionally absent')
    parser.add_argument('--save-windows', metavar='FILE', help='verify: write the observed window frames for a later --before-windows')
    parser.add_argument('--before-windows', metavar='FILE', help='verify: frames of normal windows must match this earlier observation')
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
              'applications': lambda: applications(layout, args.extras_workspace, tuple(args.expect_without or ())), 'place': lambda: place(layout, args.only, tuple(args.expect_without or ())),
              'checkpoint': lambda: checkpoint(layout), 'verify': lambda: verify(layout, args.extras_workspace, tuple(args.expect_without or ()), args.save_windows, args.before_windows),
              'vscode-extras': lambda: vscode_extras(layout, args.extras_workspace, args.with_untitled),
              'history': lambda: history(layout), 'code-extension': code_settings, 'placement-failures': placement_failures, 'ticket-watch': lambda: ticket_watch(args.duration, args.tag), 'measure-chrome': lambda: measure_chrome(layout),
              'measure-vscode': measure_vscode_checkpoint, 'sample': lambda: sample(args.duration),
              'vscode-act': lambda: vscode_actions(code_project(layout, next(i for i, e in enumerate(layout['vscode']) if e['placement']['workspace'] == args.extras_workspace))[0], json.loads(args.actions)), 'vscode-experiment': vscode_experiment, 'vscode-experiment-reopen': vscode_experiment_reopen,
              'vscode': lambda: vscode_windows(layout, args.extras_workspace)}
    try:
        if args.phase == 'seed':
            for stage in ('monitors', 'configure', 'deck', 'terminals', 'chrome', 'applications', 'place', 'checkpoint'):
                save('seed-progress', {'stage': stage, 'time': time.time()})
                print('Mirror seed: ' + stage, flush=True)
                phases[stage]()
        elif args.phase == 'verify':
            raise SystemExit(exit_code(phases['verify']()))
        else:
            phases[args.phase]()
    except SystemExit:
        raise
    except Exception as error:
        save('failure-' + args.phase, {'error': type(error).__name__ + ': ' + str(error), 'time': time.time()})
        raise


if __name__ == '__main__':
    main()
