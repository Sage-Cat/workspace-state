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
