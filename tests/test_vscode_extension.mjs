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
assert.deepEqual(mixedState.editor_uris, ['file:///example/project/a.md']);
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

// Terminals: shell-integration events, then the process fallback.
const handlers = {};
for (const name of ['onDidOpenTerminal', 'onDidStartTerminalShellExecution', 'onDidEndTerminalShellExecution']) {
  vscode.window[name] = listener => { handlers[name] = listener; return {dispose() {}}; };
}
const live = {name: 'integrated', exitStatus: undefined, shellIntegration: {}, processId: Promise.resolve(4242)};
const legacy = {name: 'plain', exitStatus: undefined, shellIntegration: undefined, processId: Promise.resolve(4343)};
const extensionTerminal = {name: 'task', exitStatus: undefined, shellIntegration: undefined, processId: Promise.resolve(undefined)};
vscode.window.terminals = [legacy, extensionTerminal];
api.watchTerminals({subscriptions: []});
handlers.onDidOpenTerminal(live);
vscode.window.terminals = [live, legacy, extensionTerminal];
await new Promise(resolve => setImmediate(resolve));
const blockers = () => api.workspaceState.call(null).switch_blockers.filter(item => item.kind === 'terminal');
const remote = vscode.env.remoteName;
vscode.env.remoteName = undefined;
const children = new Map([[4343, '']]);
api.setChildReader(pid => { if (!children.has(pid)) throw new Error('no such process'); return children.get(pid); });
assert.deepEqual(blockers(), [{kind: 'terminal', label: 'task', reason: 'activity unverified'}]);
handlers.onDidStartTerminalShellExecution({terminal: live});
children.set(4343, '9001 ');
assert.deepEqual(blockers().map(item => [item.label, item.reason]),
  [['integrated', 'command running'], ['plain', 'command running'], ['task', 'activity unverified']]);
handlers.onDidEndTerminalShellExecution({terminal: live});
children.set(4343, '\n');
assert.deepEqual(blockers().map(item => item.label), ['task']);
children.delete(4343);
assert.deepEqual(blockers().map(item => [item.label, item.reason]),
  [['plain', 'activity unverified'], ['task', 'activity unverified']]);
vscode.env.remoteName = 'ssh-remote';
assert.deepEqual(blockers().map(item => item.label), ['plain', 'task']);
vscode.env.remoteName = remote;
live.exitStatus = {code: 0}; legacy.exitStatus = {code: 0}; extensionTerminal.exitStatus = {code: 0};
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
