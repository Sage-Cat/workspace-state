import assert from 'node:assert/strict';
import Module from 'node:module';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';

const testRuntime = fs.mkdtempSync(path.join(os.tmpdir(), 'wsctl-vscode-node-'));
process.env.XDG_RUNTIME_DIR = testRuntime;

const panels = new Map();
const vscode = {
  ViewColumn: {Active: -1}, env: {remoteName: 'ssh-remote'},
  workspace: {
    workspaceFile: {toString: () => 'untitled:workspace.code-workspace'},
    workspaceFolders: [{uri: {toString: () => 'file:///tmp/project'}, name: 'project'}],
    textDocuments: [],
    getConfiguration() { return {get() { return 'on'; }}; },
    fs: {stat: async () => ({type: 2})},
  },
  window: {
    state: {focused: true}, visibleTextEditors: [], activeTextEditor: null,
    tabGroups: {all: []},
    createWebviewPanel(_type, title, showOptions) {
      const p = {title, showOptions, webview: {}, reveal(...args) { p.revealed = args; },
        onDidDispose(fn) { p.disposeListener = fn; }, dispose() { p.disposeListener?.(); }};
      panels.set(title, p); return p;
    }, showTextDocument() { throw new Error('must not reopen a closed editor'); },
  }, commands: {registerCommand() { return {dispose() {}}; }},
};
const originalLoad = Module._load;
Module._load = function(request, parent, isMain) { return request === 'vscode' ? vscode : request === './build-info.json' ? {revision: 'test-release'} : originalLoad(request, parent, isMain); };
const ext = await import('../vscode-extension/extension.js');
Module._load = originalLoad;
const api = ext.default?._test || ext._test;

const context = {globalStorageUri: {fsPath: '/tmp/wsctl-test/User/globalStorage/sagecat.workspace-state'}, workspaceState: {get() { return null; }, update() { return Promise.resolve(); }}, subscriptions: []};
ext.activate(context);
await new Promise(resolve => setTimeout(resolve, 20));
vscode.window.activeTextEditor = {document: {uri: {toString: () => 'file:///closed'}, isDirty: true}, viewColumn: 1};
const state = api.workspaceState.call(null);
assert.equal(state.profile.id, 'default');
assert.equal(state.user_data_dir, '/tmp/wsctl-test');
assert.equal(state.remote_name, 'ssh-remote');
assert.equal(state.empty, false);
assert.equal(state.build.revision, 'test-release');
assert.ok(state.capabilities.includes('runtime_build'));

const identified = api.handle({version: 1, method: 'identify', token: 'a'.repeat(32)});
assert.equal(identified.ok, true);
assert.equal(identified.result.token, 'a'.repeat(32));
assert.equal(panels.get(identified.result.title).showOptions.preserveFocus, true);
assert.deepEqual(api.handle({version: 1, method: 'release', token: 'a'.repeat(32)}).result, {released: true});
assert.equal(api.handle({version: 1, method: 'state', token: 'not-hex'}).ok, false);
assert.equal(api.validRequest({version: 1, method: 'state'}), true);
assert.equal(api.validRequest({version: 2, method: 'state'}), false);

const endpoint = path.join(testRuntime, 'workspace-state', 'vscode', `${state.instance}.sock`);
assert.equal(fs.statSync(endpoint).mode & 0o777, 0o600);
async function request(value) {
  return new Promise((resolve, reject) => {
    const client = net.createConnection(endpoint);
    let received = '';
    client.setTimeout(2000, () => { client.destroy(); reject(new Error('timeout')); });
    client.on('connect', () => client.write(JSON.stringify(value) + '\n'));
    client.on('data', chunk => { received += chunk; });
    client.on('error', reject);
    client.on('end', () => resolve(JSON.parse(received)));
  });
}
assert.equal((await request({version: 1, method: 'state'})).result.instance, state.instance);
assert.equal((await request({version: 1, method: 'probe'})).result.ready, true);
vscode.workspace.fs.stat = async () => { throw new Error('remote offline'); };
assert.equal((await request({version: 1, method: 'probe'})).result.ready, false);
vscode.workspace.fs.stat = async () => ({type: 1});
vscode.FileType = {File: 1};
vscode.Uri = {parse(value) { return {scheme: new URL(value).protocol.slice(0, -1), toString: () => value}; }};
vscode.TabInputText = class { constructor(uri) { this.uri = uri; } };
const opened = [];
vscode.workspace.openTextDocument = async uri => ({uri, isDirty: false});
vscode.window.showTextDocument = async (document, options) => {
  assert.deepEqual(options, {preview: false, preserveFocus: true});
  opened.push(document.uri.toString());
  vscode.window.tabGroups.all = [{tabs: [{input: new vscode.TabInputText(document.uri)}]}];
};
const reopen = {version: 1, method: 'restore-editors', instance: state.instance,
  project: JSON.stringify([state.user_data_dir, state.profile.id, state.remote_name,
    state.workspace_file, state.folders.map(folder => folder.uri)]),
  editors: ['file:///example/project/readme.md']};
assert.equal((await request({...reopen, instance: 'e'.repeat(32)})).ok, false);
assert.deepEqual(opened, []);
assert.equal((await request(reopen)).ok, true);
assert.deepEqual(opened, reopen.editors);
vscode.window.tabGroups.all[0].tabs[0].isDirty = true;
assert.equal((await request({...reopen, editors: ['file:///example/project/other.md']})).ok, false);
assert.deepEqual(opened, reopen.editors);
const markerToken = 'c'.repeat(32);
const markerPath = path.join(testRuntime, 'workspace-state', 'workspace-launches', `vscode-${markerToken}.txt`);
const markerUri = {scheme: 'file', authority: '', fsPath: markerPath, toString: () => `file://${markerPath}`};
const markerTab = {input: new vscode.TabInputText(markerUri), isDirty: true};
const documentTab = vscode.window.tabGroups.all[0].tabs[0];
documentTab.isDirty = false;
vscode.window.tabGroups.all[0].tabs.push(markerTab);
vscode.window.tabGroups.close = async (tab, preserveFocus) => {
  assert.equal(tab, markerTab);
  assert.equal(preserveFocus, true);
  vscode.window.tabGroups.all[0].tabs.pop();
  return true;
};
const drop = {version: 1, method: 'drop-launch-marker', instance: state.instance,
  project: reopen.project, token: markerToken};
assert.deepEqual((await request({version: 1, method: 'state'})).result.workspace_launch_markers, [markerToken]);
assert.equal((await request(drop)).ok, false);
markerTab.isDirty = false;
assert.equal((await request(drop)).result.removed, true);
assert.deepEqual((await request({version: 1, method: 'state'})).result.editor_uris, reopen.editors);

// Switch policy: only unsaved tabs, untitled content and running terminals block.
const fileUri = value => ({scheme: 'file', authority: '', path: value, fsPath: value, toString: () => `file://${value}`});
vscode.TabInputTextDiff = class { constructor(original, modified) { this.original = original; this.modified = modified; } };
vscode.TabInputWebview = class { constructor(viewType) { this.viewType = viewType; } };
vscode.TabInputCustom = class { constructor(uri) { this.uri = uri; } };
const textTab = (value, extra = {}) => ({label: path.basename(value), input: new vscode.TabInputText(fileUri(value)), isDirty: false, ...extra});
const mixed = [
  textTab('/example/project/a.md'),
  {label: 'a.md (Working Tree)', input: new vscode.TabInputTextDiff(fileUri('/example/project/old.md'), fileUri('/example/project/new.md')), isDirty: false},
  {label: 'Settings', input: new vscode.TabInputWebview('workbench.editor.settings2'), isDirty: false},
  {label: 'Chat', input: new vscode.TabInputWebview('chat.view'), isDirty: false},
  {label: 'Preview', input: new vscode.TabInputCustom(fileUri('/example/project/image.png')), isDirty: false},
];
vscode.window.tabGroups.all = [{tabs: mixed}];
let mixedState = (await request({version: 1, method: 'state'})).result;
// editor_uris keeps its original meaning; the reopenable text files are reported separately.
assert.deepEqual(mixedState.editor_uris, ['file:///example/project/a.md', 'file:///example/project/old.md',
  'file:///example/project/new.md', 'file:///example/project/image.png']);
assert.deepEqual(mixedState.text_editor_uris, ['file:///example/project/a.md']);
assert.deepEqual(mixedState.switch_blockers, []);
assert.equal(mixedState.unsupported_editors, 4);
assert.ok(mixedState.capabilities.includes('switch_policy'));
const mixedRestore = {version: 1, method: 'restore-editors', instance: state.instance, project: reopen.project,
  editors: ['file:///example/project/b.md']};
opened.length = 0;
vscode.window.tabGroups.all = [{tabs: mixed}];
assert.equal((await request(mixedRestore)).ok, true);
assert.deepEqual(opened, mixedRestore.editors);

vscode.window.tabGroups.all = [{tabs: [textTab('/example/project/dirty.md', {isDirty: true})]}];
assert.deepEqual((await request({version: 1, method: 'state'})).result.switch_blockers,
  [{kind: 'unsaved', label: 'dirty.md'}]);
const untitledUri = {scheme: 'untitled', authority: '', path: 'Untitled-1', toString: () => 'untitled:Untitled-1'};
const untitledTab = {label: 'Untitled-1', input: new vscode.TabInputText(untitledUri), isDirty: false};
vscode.window.tabGroups.all = [{tabs: [untitledTab]}];
vscode.workspace.textDocuments = [{uri: untitledUri, isDirty: false, getText: () => ''}];
assert.deepEqual((await request({version: 1, method: 'state'})).result.switch_blockers, []);
vscode.workspace.textDocuments = [{uri: untitledUri, isDirty: true, getText: () => 'x'}];
assert.deepEqual((await request({version: 1, method: 'state'})).result.switch_blockers,
  [{kind: 'untitled', label: 'Untitled-1'}]);
vscode.workspace.textDocuments = [];
vscode.window.tabGroups.all = [];

// Terminals: a live process anywhere under the shell, a non-shell root and an
// unverifiable terminal all block; shell integration events add to that.
const handlers = {};
for (const name of ['onDidOpenTerminal', 'onDidStartTerminalShellExecution', 'onDidEndTerminalShellExecution']) {
  vscode.window[name] = listener => { handlers[name] = listener; return {dispose() {}}; };
}
const terminal = (name, pid, extra = {}) => ({name, exitStatus: undefined, shellIntegration: {}, processId: Promise.resolve(pid), ...extra});
const idle = terminal('idle', 1);
const job = terminal('job', 2);          // background job or stopped editor under an idle prompt
const ssh = terminal('ssh', 3);          // root process is not a shell, no children
const plain = terminal('plain', 4, {shellIntegration: undefined});
const extensionTerminal = terminal('task', undefined);
const running = terminal('running', 5);  // execution event, no visible child
vscode.window.terminals = [idle, job, ssh, plain, extensionTerminal, running];
api.watchTerminals({subscriptions: []});
await new Promise(resolve => setImmediate(resolve));
const blockers = () => api.workspaceState.call(null).switch_blockers.filter(item => item.kind === 'terminal')
  .map(item => `${item.label}:${item.reason}`);
const remote = vscode.env.remoteName;
vscode.env.remoteName = undefined;
const comm = new Map([[1, 'zsh\n'], [2, '-bash\n'], [3, 'ssh\n'], [4, 'fish\n'], [5, 'dash\n']]);
const children = new Map([[1, ''], [2, '9001 9002\n'], [3, ''], [4, ''], [5, '']]);
api.setCommReader(pid => { if (!comm.has(pid)) throw new Error('gone'); return comm.get(pid); });
api.setChildReader(pid => { if (!children.has(pid)) throw new Error('gone'); return children.get(pid); });
assert.deepEqual(blockers(), ['job:command running', 'ssh:running process', 'task:activity unverified']);
handlers.onDidStartTerminalShellExecution({terminal: running});
assert.deepEqual(blockers(), ['job:command running', 'ssh:running process', 'task:activity unverified', 'running:command running']);
handlers.onDidEndTerminalShellExecution({terminal: running});
children.set(4, '77');
children.set(2, '');
assert.deepEqual(blockers(), ['ssh:running process', 'plain:command running', 'task:activity unverified']);
comm.delete(4);  // an unreadable process is unverified, never idle
assert.ok(blockers().includes('plain:activity unverified'));
vscode.env.remoteName = 'ssh-remote';
assert.equal(blockers().length, 6);
vscode.env.remoteName = remote;
// A tmux client, as the root or under the shell, is only a view of a session that outlives the window.
for (const item of vscode.window.terminals) item.exitStatus = {code: 0};
vscode.env.remoteName = undefined;
const tmuxRoot = terminal('tmux-root', 6);
const tmuxChild = terminal('tmux-child', 7);
const tmuxAndVim = terminal('tmux-vim', 8);
vscode.window.terminals = [tmuxRoot, tmuxChild, tmuxAndVim];
for (const item of vscode.window.terminals) handlers.onDidOpenTerminal(item);
await new Promise(resolve => setImmediate(resolve));
comm.set(6, 'tmux: client\n'); comm.set(7, 'zsh\n'); comm.set(8, 'zsh\n');
comm.set(70, 'tmux: client\n'); comm.set(80, 'tmux\n'); comm.set(81, 'vim\n');
children.set(6, ''); children.set(7, '70'); children.set(8, '80 81');
handlers.onDidStartTerminalShellExecution({terminal: tmuxChild});
assert.deepEqual(blockers(), ['tmux-vim:command running']);
for (const item of vscode.window.terminals) item.exitStatus = {code: 0};
vscode.env.remoteName = remote;

assert.deepEqual(blockers(), []);
vscode.window.terminals = [];
vscode.window.tabGroups.all = [];
api.handle({version: 1, method: 'identify', token: 'b'.repeat(32)});
assert.throws(() => api.handle({version: 1, method: 'identify', token: 'c'.repeat(32)}), /in progress/);
await new Promise(resolve => setTimeout(resolve, 3100));
assert.equal(api.handle({version: 1, method: 'release', token: 'b'.repeat(32)}).result.released, false);

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'wsctl-vscode-'));
fs.mkdirSync(path.join(root, 'vscode'), {mode: 0o700});
const sock = path.join(root, 'vscode', 'x.sock');
fs.symlinkSync('/tmp', sock);
assert.throws(() => api.secureSocket(sock));
assert.throws(() => api.secureDir(sock));
console.log('vscode extension mocked tests: ok');
ext.deactivate();
