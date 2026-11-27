'use strict';

const vscode = require('vscode');
const fs = require('fs');
const os = require('os');
const path = require('path');
const net = require('net');
const crypto = require('crypto');

const BUILD_REVISION = (() => {
  try { return require('./build-info.json').revision; } catch (_) { return 'development'; }
})();
const VERSION = 1;
const MAX_LINE = 1024 * 1024;
const HEX32 = /^[0-9a-f]{32}$/;
const TTL_MS = 3000;
let server;
let socketPath;
let contextRef;
let panelRecords = new Map();
let windowKey = null;
let stopping = false;
// Terminal activity.  Shell integration events (VS Code 1.93+) report only the
// commands that start after this extension subscribes, so a terminal that
// existed earlier is trusted only after one of its executions was observed.
const executions = new WeakMap();
const trustedTerminals = new WeakSet();
const terminalPids = new WeakMap();
let childReader = pid => fs.readFileSync(`/proc/${pid}/task/${pid}/children`, 'utf8');

function randomToken() { return crypto.randomBytes(16).toString('hex'); }
function runtimeDir() {
  const base = process.env.XDG_RUNTIME_DIR || `/run/user/${process.getuid()}`;
  return path.join(base, 'workspace-state', 'vscode');
}
function secureDir(dir) {
  try { const before = fs.lstatSync(dir); if (!before.isDirectory() || before.uid !== process.getuid()) throw new Error('invalid runtime directory'); } catch (e) { if (e.code !== 'ENOENT') throw e; }
  fs.mkdirSync(dir, {recursive: true, mode: 0o700});
  const st = fs.lstatSync(dir);
  fs.chmodSync(dir, 0o700);
  if (!st.isDirectory() || st.uid !== process.getuid()) throw new Error('invalid runtime directory');
}
function secureSocket(p) {
  const st = fs.lstatSync(p);
  if (!st.isSocket() || st.uid !== process.getuid() || (st.mode & 0o777) !== 0o600) throw new Error('invalid socket');
}
function uriString(uri) { return uri ? uri.toString() : null; }
function userDataDir() {
  const p = contextRef && contextRef.globalStorageUri && contextRef.globalStorageUri.fsPath;
  if (!p) return null;
  const marker = `${path.sep}User${path.sep}`;
  const i = p.indexOf(marker);
  return i >= 0 ? p.slice(0, i) : path.dirname(path.dirname(p));
}
function profileInfo() {
  // VS Code has no public current-profile API.  The storage layout reliably
  // identifies the default profile without reading settings or credentials.
  const root = userDataDir();
  if (!root) return null;
  const p = contextRef.globalStorageUri.fsPath;
  const m = p.match(`${path.sep}User${path.sep}(?:profiles${path.sep}([^${path.sep}]+)${path.sep})?globalStorage`);
  let id = m && m[1] ? m[1] : 'default';
  // Profile IDs are not CLI names. Never invent a name: --profile would create
  // a new, empty profile. Read only the profile registry, not editor databases.
  for (const filename of [path.join(root, 'User', 'globalStorage', 'storage.json'), path.join(root, 'storage.json')]) {
    try {
      const info = fs.statSync(filename);
      if (!info.isFile() || info.size > MAX_LINE * 8) continue;
      const registry = JSON.parse(fs.readFileSync(filename, 'utf8'));
      const profiles = registry.userDataProfiles;
      const project = uriString(vscode.workspace.workspaceFile || vscode.workspace.workspaceFolders?.[0]?.uri);
      const associated = registry.profileAssociations?.workspaces?.[project];
      // Profiles may deliberately share the default global state directory.
      // In that case its path alone is not a reliable current-profile identity.
      if (id === 'default' && associated && !['default', '__default__profile__'].includes(associated)) id = associated;
      if (id === 'default') {
        if (!project && Array.isArray(profiles) && profiles.some(p => p.useDefaultFlags?.globalState)) return null;
        return {id, name: 'Default'};
      }
      const profile = Array.isArray(profiles) && profiles.find(p => {
        const location = typeof p.location === 'object' ? p.location.path : p.location;
        const locationId = typeof location === 'string' ? path.basename(location) : null;
        return p.id === id || locationId === id;
      });
      if (profile && typeof profile.name === 'string' && profile.name) return {id, name: profile.name};
    } catch (_) { /* Unknown profile must be reported, not guessed. */ }
  }
  return {id, name: id === 'default' ? 'Default' : null};
}
function editorTabs() {
  return (vscode.window.tabGroups?.all || []).flatMap(group => group.tabs || []);
}

function workspaceLaunchMarker(tab) {
  if (!vscode.TabInputText || !(tab.input instanceof vscode.TabInputText)) return null;
  const uri = tab.input.uri;
  const directory = path.join(path.dirname(runtimeDir()), 'workspace-launches');
  if (uri.scheme !== 'file' || uri.authority || !uri.fsPath || path.dirname(uri.fsPath) !== directory) return null;
  return /^vscode-([0-9a-f]{32})\.txt$/.exec(path.basename(uri.fsPath))?.[1] || null;
}

function label(value) {
  return String(value ?? '').replace(/[\p{Cc}\p{Cf}\s]+/gu, ' ').trim().slice(0, 60);
}
function isTextTab(tab) {
  return Boolean(vscode.TabInputText && tab.input instanceof vscode.TabInputText &&
    ['file', 'vscode-remote'].includes(tab.input.uri?.scheme));
}
function trackTerminal(terminal) {
  Promise.resolve(terminal.processId).then(pid => { if (Number.isInteger(pid)) terminalPids.set(terminal, pid); }, () => {});
}
// Null means the terminal is idle; a string says why it blocks closing the window.
function terminalBlock(terminal) {
  if (terminal.exitStatus !== undefined) return null;
  if (terminal.shellIntegration && trustedTerminals.has(terminal)) {
    return (executions.get(terminal) || 0) > 0 ? 'command running' : null;
  }
  // Without shell integration only a local terminal's own child processes can
  // show a foreground command.  Anything else is blocked, never guessed idle.
  const pid = terminalPids.get(terminal);
  if (vscode.env.remoteName || !Number.isInteger(pid)) return 'activity unverified';
  try { return childReader(pid).trim() ? 'command running' : null; } catch (_) { return 'activity unverified'; }
}
function switchBlockers(tabs, docs) {
  const blockers = [];
  const covered = new Set();
  for (const tab of tabs) {
    const uri = tab.input?.uri;
    if (uri) covered.add(uriString(uri));
    if (uri?.scheme === 'untitled') {
      const document = docs.find(doc => uriString(doc.uri) === uriString(uri));
      // Only the length is read; document text never leaves the editor.
      if (document ? document.getText().length > 0 : Boolean(tab.isDirty)) blockers.push({kind: 'untitled', label: label(tab.label)});
    } else if (tab.isDirty) {
      blockers.push({kind: 'unsaved', label: label(tab.label)});
    }
  }
  for (const document of docs) {
    if (document.isDirty && ['file', 'vscode-remote'].includes(document.uri?.scheme) && !covered.has(uriString(document.uri))) {
      blockers.push({kind: 'unsaved', label: label(path.basename(document.uri.path || document.fileName || ''))});
    }
  }
  for (const terminal of vscode.window.terminals || []) {
    const reason = terminalBlock(terminal);
    if (reason) blockers.push({kind: 'terminal', label: label(terminal.name), reason});
  }
  return blockers.slice(0, 64);
}

function workspaceState() {
  const folders = (vscode.workspace.workspaceFolders || []).map(f => ({uri: uriString(f.uri), name: f.name}));
  const docs = vscode.workspace.textDocuments || [];
  const identificationTab = tab => vscode.TabInputWebview && tab.input instanceof vscode.TabInputWebview &&
    /(?:^|[.-])workspaceState\.identify$/.test(tab.input.viewType) &&
    /^wsctl-identify-[0-9a-f]{32}$/.test(tab.label);
  const tabs = editorTabs().filter(tab => !identificationTab(tab));
  // Older clients block on every tab that is not a plain text file, so this
  // count keeps its meaning.  switch_blockers is the current policy.
  const editorUris = tabs.filter(tab => isTextTab(tab) && !workspaceLaunchMarker(tab)).map(tab => uriString(tab.input.uri));
  return {
    version: VERSION, instance: instanceId, pid: process.pid,
    build: {revision: BUILD_REVISION}, capabilities: ['runtime_build', 'workspace_switch', 'switch_policy'],
    workspace_file: uriString(vscode.workspace.workspaceFile), folders,
    window_key: windowKey,
    editor_uris: [...new Set(editorUris)],
    workspace_launch_markers: tabs.map(workspaceLaunchMarker).filter(Boolean),
    remote_name: vscode.env.remoteName || null, profile: profileInfo(),
    user_data_dir: userDataDir(),
    hot_exit: String(vscode.workspace.getConfiguration?.('files')?.get?.('hotExit') ?? 'unknown'),
    dirty_count: Math.max(docs.filter(document => document.isDirty).length, tabs.filter(tab => tab.isDirty).length),
    unsupported_editors: tabs.filter(tab => !isTextTab(tab)).length,
    switch_blockers: switchBlockers(tabs, docs),
    focused: Boolean(vscode.window.state && vscode.window.state.focused),
    empty: !vscode.workspace.workspaceFile && folders.length === 0,
  };
}
function reply(socket, value) { socket.write(JSON.stringify(value) + '\n'); }
function validRequest(req) {
  return req && req.version === VERSION && typeof req.method === 'string' &&
    ['state', 'identify', 'release', 'probe', 'restore-editors', 'drop-launch-marker'].includes(req.method) &&
    (req.method !== 'restore-editors' || validEditorRestore(req)) &&
    (req.method !== 'drop-launch-marker' || (validWindowRequest(req) && HEX32.test(req.token || ''))) &&
    (req.token === undefined || (typeof req.token === 'string' && HEX32.test(req.token)));
}
function identify(token) {
  if (stopping) throw new Error('companion is stopping');
  if (panelRecords.size) throw new Error('window identification is already in progress');
  const title = `wsctl-identify-${token}`;
  const original = vscode.window.activeTextEditor;
  const record = {panel: null, original, timer: null};
  const panel = vscode.window.createWebviewPanel('workspaceState.identify', title,
    {viewColumn: vscode.ViewColumn.Active, preserveFocus: true}, {enableScripts: false, retainContextWhenHidden: false});
  record.panel = panel;
  panel.webview.html = '<!doctype html><html><body></body></html>';
  panel.onDidDispose(() => {
    if (record.timer) clearTimeout(record.timer);
    panelRecords.delete(token);
    // Disposing this panel returns to the editor selected by VS Code's own
    // history. Do not call showTextDocument: it can reopen a tab the user closed
    // meanwhile, change a preview tab, or replace an active notebook/diff editor.
  });
  panelRecords.set(token, record);
  record.timer = setTimeout(() => panel.dispose(), TTL_MS);
  return {token, title};
}
function release(token) {
  if (!token || !panelRecords.has(token)) return {released: false};
  panelRecords.get(token).panel.dispose();
  return {released: true};
}
function handle(req) {
  if (!validRequest(req)) return {ok: false, error: 'invalid request'};
  if (req.method === 'state') return {ok: true, result: workspaceState()};
  if (req.method === 'identify') return {ok: true, result: identify(req.token || randomToken())};
  return {ok: true, result: release(req.token)};
}
async function probe() {
  const uris = (vscode.workspace.workspaceFolders || []).map(f => f.uri);
  if (vscode.workspace.workspaceFile && vscode.workspace.workspaceFile.scheme !== 'untitled') uris.push(vscode.workspace.workspaceFile);
  let timer;
  try {
    await Promise.race([
      Promise.all(uris.map(uri => vscode.workspace.fs.stat(uri))),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('timeout')), 1000); }),
    ]);
    return {ok: true, result: {ready: true}};
  } catch (_) {
    return {ok: true, result: {ready: false, error: 'Project storage or remote connection is unavailable'}};
  } finally { clearTimeout(timer); }
}
function validWindowRequest(req) {
  return typeof req.instance === 'string' && HEX32.test(req.instance) &&
    typeof req.project === 'string' && req.project.length <= MAX_LINE;
}
function validEditorRestore(req) {
  return validWindowRequest(req) && Array.isArray(req.editors) && req.editors.length <= 512 &&
    req.editors.every(uri => typeof uri === 'string' && uri.length <= 8192);
}

function projectIdentity(state) {
  return JSON.stringify([state.user_data_dir, state.profile?.id, state.remote_name,
    state.workspace_file, state.folders.map(folder => folder.uri)]);
}

async function dropLaunchMarker(req) {
  const state = workspaceState();
  if (stopping || req.instance !== state.instance || req.project !== projectIdentity(state)) {
    throw new Error('VS Code project/window changed');
  }
  const matches = editorTabs().filter(tab => workspaceLaunchMarker(tab) === req.token);
  if (matches.length !== 1 || matches[0].isDirty) throw new Error('The scoped launch tab changed');
  const removed = await vscode.window.tabGroups.close(matches[0], true);
  return {ok: true, result: {instance: state.instance, pid: state.pid, removed}};
}

async function restoreEditors(req, connected) {
  const authorized = () => {
    const state = workspaceState();
    return connected() && !stopping && req.instance === state.instance && req.project === projectIdentity(state) &&
      state.dirty_count === 0 && state.switch_blockers.every(blocker => blocker.kind === 'terminal');
  };
  if (!authorized()) throw new Error('VS Code window or editor state changed');
  const resources = req.editors.map(value => {
    const uri = vscode.Uri.parse(value, true);
    if (!['file', 'vscode-remote'].includes(uri.scheme) || uri.query || uri.fragment ||
        (uri.scheme === 'file' && uri.authority && uri.authority !== 'localhost')) {
      throw new Error('Saved editor URI is unsupported');
    }
    return uri;
  });
  for (const uri of resources) {
    if (!authorized()) throw new Error('VS Code window or editor state changed');
    if (workspaceState().editor_uris.includes(uriString(uri))) continue;
    const stat = await vscode.workspace.fs.stat(uri);
    if (!(stat.type & vscode.FileType.File)) throw new Error('A saved editor is no longer a file');
    const document = await vscode.workspace.openTextDocument(uri);
    if (!authorized() || document.isDirty) throw new Error('VS Code editor changed while reopening');
    await vscode.window.showTextDocument(document, {preview: false, preserveFocus: true});
  }
  const state = workspaceState();
  return {ok: true, result: {instance: state.instance, pid: state.pid,
    editor_uris: state.editor_uris, dirty_count: state.dirty_count}};
}

function onConnection(socket) {
  let data = ''; let closed = false;
  socket.setTimeout(2000, () => socket.destroy());
  socket.on('error', () => { closed = true; });
  socket.on('data', chunk => {
    if (closed) return;
    data += chunk.toString('utf8');
    if (Buffer.byteLength(data) > MAX_LINE) { closed = true; reply(socket, {ok: false, error: 'request too large'}); socket.destroy(); return; }
    const i = data.indexOf('\n');
    if (i < 0) return;
    const line = data.slice(0, i); closed = true;
    try {
      const request = JSON.parse(line);
      if (validRequest(request) && request.method === 'probe') {
        probe().then(result => { if (!socket.destroyed) socket.end(JSON.stringify(result) + '\n'); });
        return;
      }
      if (validRequest(request) && ['restore-editors', 'drop-launch-marker'].includes(request.method)) {
        const operation = request.method === 'restore-editors'
          ? restoreEditors(request, () => !socket.destroyed) : dropLaunchMarker(request);
        operation.then(result => { if (!socket.destroyed) socket.end(JSON.stringify(result) + '\n'); })
          .catch(() => { if (!socket.destroyed) socket.end(JSON.stringify({ok: false, error: 'Could not complete the scoped VS Code editor operation'}) + '\n'); });
        return;
      }
      reply(socket, handle(request));
    } catch (_) { reply(socket, {ok: false, error: 'Invalid request or identification is busy'}); }
    socket.end();
  });
}
let instanceId;
function start(context) {
  contextRef = context; instanceId = randomToken(); stopping = false;
  const persistent = Boolean(vscode.workspace.workspaceFile || (vscode.workspace.workspaceFolders || []).length);
  if (persistent) {
    windowKey = context.workspaceState.get('wsctl.windowKey');
    if (typeof windowKey !== 'string' || !HEX32.test(windowKey)) {
      windowKey = randomToken();
      context.workspaceState.update('wsctl.windowKey', windowKey).catch(() => {});
    }
  }
  const dir = runtimeDir();
  // Validate the runtime parent before creating children; never follow an
  // attacker-controlled directory symlink or change another directory's mode.
  const runtime = path.dirname(path.dirname(dir));
  const baseInfo = fs.lstatSync(runtime);
  if (!baseInfo.isDirectory() || baseInfo.uid !== process.getuid() || (baseInfo.mode & 0o077)) throw new Error('invalid user runtime directory');
  secureDir(path.dirname(dir)); secureDir(dir);
  socketPath = path.join(dir, `${instanceId}.sock`);
  server = net.createServer(onConnection);
  server.on('error', () => stop());
  server.listen(socketPath, () => {
    try { fs.chmodSync(socketPath, 0o600); secureSocket(socketPath); }
    catch (_) { stop(); }
  });
  context.subscriptions.push({dispose: stop});
}
function stop() {
  if (stopping && !server && !socketPath) return;
  stopping = true;
  for (const r of panelRecords.values()) r.panel.dispose(); panelRecords.clear();
  if (server) { try { server.close(); } catch (_) {} server = null; }
  if (socketPath) { try { fs.unlinkSync(socketPath); } catch (_) {} socketPath = null; }
}
function watchTerminals(context) {
  const window = vscode.window;
  for (const terminal of window.terminals || []) trackTerminal(terminal);
  const add = (event, listener) => { if (typeof event === 'function') context.subscriptions.push(event(listener)); };
  add(window.onDidOpenTerminal, terminal => { trustedTerminals.add(terminal); trackTerminal(terminal); });
  // These events need VS Code 1.93; older versions use the process fallback.
  add(window.onDidStartTerminalShellExecution, event => {
    trustedTerminals.add(event.terminal);
    executions.set(event.terminal, (executions.get(event.terminal) || 0) + 1);
  });
  add(window.onDidEndTerminalShellExecution, event => {
    trustedTerminals.add(event.terminal);
    executions.set(event.terminal, Math.max(0, (executions.get(event.terminal) || 0) - 1));
  });
}
function activate(context) {
  start(context); watchTerminals(context);
  context.subscriptions.push(vscode.commands.registerCommand('workspaceState.identify', () => identify(randomToken())));
}
function deactivate() { stopping = true; stop(); }
module.exports = {activate, deactivate, _test: {watchTerminals, setChildReader(reader) { childReader = reader; }, trackTerminal, workspaceState, handle, runtimeDir, secureDir, secureSocket, validRequest, MAX_LINE}};
