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
function workspaceState() {
  const folders = (vscode.workspace.workspaceFolders || []).map(f => ({uri: uriString(f.uri), name: f.name}));
  const docs = vscode.workspace.textDocuments || [];
  const editorUris = (vscode.window.tabGroups?.all || []).flatMap(g => (g.tabs || [])
    .flatMap(t => [t.input?.uri, t.input?.original, t.input?.modified])
    .filter(uri => uri && ['file', 'untitled', 'vscode-remote'].includes(uri.scheme || String(uri).split(':')[0]))
    .map(uriString).filter(Boolean));
  return {
    version: VERSION, instance: instanceId, pid: process.pid,
    build: {revision: BUILD_REVISION}, capabilities: ['runtime_build'],
    workspace_file: uriString(vscode.workspace.workspaceFile), folders,
    window_key: windowKey,
    editor_uris: [...new Set(editorUris)],
    remote_name: vscode.env.remoteName || null, profile: profileInfo(),
    user_data_dir: userDataDir(),
    hot_exit: String(vscode.workspace.getConfiguration?.('files')?.get?.('hotExit') ?? 'unknown'), dirty_count: docs.filter(d => d.isDirty).length,
    focused: Boolean(vscode.window.state && vscode.window.state.focused),
    empty: !vscode.workspace.workspaceFile && folders.length === 0,
  };
}
function reply(socket, value) { socket.write(JSON.stringify(value) + '\n'); }
function validRequest(req) {
  return req && req.version === VERSION && typeof req.method === 'string' &&
    ['state', 'identify', 'release', 'probe'].includes(req.method) &&
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
function activate(context) { start(context); context.subscriptions.push(vscode.commands.registerCommand('workspaceState.identify', () => identify(randomToken()))); }
function deactivate() { stopping = true; stop(); }
module.exports = {activate, deactivate, _test: {workspaceState, handle, runtimeDir, secureDir, secureSocket, validRequest, MAX_LINE}};
