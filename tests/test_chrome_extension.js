'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const session = {};
const localStorage = {};
let windows = [];
let nativeGroups = [];
let nextWindowId = 100;
let nextTabId = 1000;
let onSleep = null;
let onNavigate = null;
let onActivate = null;
let onMove = null;
let onCreate = null;
let onWindowUpdate = null;
let onRemoveTabs = null;
let onGetTab = null;
let onGetWindow = null;
let leaseClock = 1000000;
const leaseTimers = new Map();
let nextTimerId = 0;
const movedTabs = [];
let failUrl = null;
let sleepCount = 0;
const removedWindows = [];
const navigations = [];
const createdWindows = [];
const activations = [];
const groupMutations = [];

function rejectGroupMutation(action, args) {
    groupMutations.push({action, args});
    throw new Error(`Group mutation is forbidden during restore: ${action}`);
}

function findWindow(id) {
    const window = windows.find(item => item.id === id);
    if (!window)
        throw new Error(`No window with id ${id}`);
    return window;
}

function findTab(id) {
    const tab = windows.flatMap(window => window.tabs).find(item => item.id === id);
    if (!tab)
        throw new Error(`No tab with id ${id}`);
    return tab;
}

function navigate(tab, url) {
    navigations.push({id: tab.id, url});
    if (url === failUrl)
        throw new Error('Navigation rejected');
    Object.assign(tab, {url, status: 'complete'});
    delete tab.pendingUrl;
    onNavigate?.(tab, url);
    return tab;
}

function chromeWindow(id, urls) {
    return {
        id, type: 'normal', incognito: false,
        tabs: urls.map((url, index) => ({
            id: nextTabId++, windowId: id, url, index, pinned: false,
            status: 'complete', active: index === 0,
        })),
    };
}

const chrome = {
    runtime: {getURL: name => `chrome-extension://test/${name}`},
    storage: {
        local: {
            get: async key => key === null ? structuredClone(localStorage) :
                (Object.hasOwn(localStorage, key) ? {[key]: structuredClone(localStorage[key])} : {}),
            set: async values => Object.assign(localStorage, structuredClone(values)),
        },
        session: {
            get: async key => key === null ? structuredClone(session) :
                (Object.hasOwn(session, key) ? {[key]: structuredClone(session[key])} : {}),
            set: async values => Object.assign(session, structuredClone(values)),
            remove: async key => delete session[key],
        },
    },
    windows: {
        getAll: async () => structuredClone(windows),
        get: async id => {
            onGetWindow?.(id);
            const window = findWindow(id);
            return structuredClone(window);
        },
        create: async data => {
            const window = {...chromeWindow(nextWindowId++, [data.url]), ...data};
            windows.push(window);
            createdWindows.push(window.id);
            return window;
        },
        update: async (id, changes) => {
            onWindowUpdate?.(id, changes);
            return Object.assign(findWindow(id), changes);
        },
        remove: async id => {
            removedWindows.push(id);
            windows = windows.filter(window => window.id !== id);
        },
    },
    tabGroups: {
        query: async ({windowId}) => nativeGroups.filter(group => group.windowId === windowId),
        update: async (...args) => rejectGroupMutation('tabGroups.update', args),
        move: async (...args) => rejectGroupMutation('tabGroups.move', args),
    },
    tabs: {
        move: async (id, changes) => {
            const tab = findTab(id);
            const from = findWindow(tab.windowId);
            const to = findWindow(changes.windowId);
            from.tabs.splice(from.tabs.indexOf(tab), 1);
            to.tabs.splice(changes.index < 0 ? to.tabs.length : changes.index, 0, tab);
            tab.windowId = to.id;
            windows.forEach(window => window.tabs.forEach((item, index) => { item.index = index; }));
            movedTabs.push({id, ...changes});
            onMove?.(tab);
            return tab;
        },
        group: async (...args) => rejectGroupMutation('tabs.group', args),
        ungroup: async (...args) => rejectGroupMutation('tabs.ungroup', args),
        query: async ({windowId}) => findWindow(windowId).tabs,
        remove: async ids => {
            onRemoveTabs?.(ids);
            const removed = new Set(Array.isArray(ids) ? ids : [ids]);
            windows.forEach(window => { window.tabs = window.tabs.filter(tab => !removed.has(tab.id)); });
        },
        get: async id => { onGetTab?.(id); return structuredClone(findTab(id)); },
        update: async (id, changes) => {
            const tab = findTab(id);
            if (changes.active) {
                activations.push(id);
                findWindow(tab.windowId).tabs.forEach(item => { item.active = item.id === id; });
                if (tab.discarded || tab.status === 'unloaded')
                    Object.assign(tab, {discarded: false, status: 'loading'});
                onActivate?.(tab);
            }
            if (Object.hasOwn(changes, 'url'))
                navigate(tab, changes.url);
            const {url, ...otherChanges} = changes;
            return Object.assign(tab, otherChanges);
        },
        create: async data => {
            const window = findWindow(data.windowId);
            const tab = {...chromeWindow(data.windowId, ['about:blank']).tabs[0],
                index: window.tabs.length, active: false};
            navigate(tab, data.url);
            if (data.url.includes("identify.html?token="))
                tab.title = new URL(data.url).searchParams.get("token");
            if (data.active) {
                window.tabs.forEach(item => { item.active = false; });
                tab.active = true;
            }
            window.tabs.push(tab);
            onCreate?.(tab);
            return tab;
        },
    },
};

const workerPath = path.join(__dirname, '..', 'chrome-extension', 'service-worker.js');
const definitions = fs.readFileSync(workerPath, 'utf8').split('\nchrome.runtime.onInstalled.addListener')[0];
const context = vm.createContext({
    chrome, console, URL, Date: {now: () => leaseClock}, WSCTL_BUILD_REVISION: 'test-release',
    clearTimeout: id => leaseTimers.delete(id),
    setTimeout: (callback, delay) => {
        if (delay >= 1000) {
            const id = ++nextTimerId;
            leaseTimers.set(id, () => { leaseTimers.delete(id); return callback(); });
            return id;
        }
        sleepCount += 1;
        onSleep?.();
        callback();
    },
});
vm.runInContext(definitions, context, {filename: workerPath});

function reset() {
    for (const key of Object.keys(session))
        delete session[key];
    for (const key of Object.keys(localStorage))
        delete localStorage[key];
    windows = [];
    nativeGroups = [];
    groupMutations.length = 0;
    removedWindows.length = 0;
    createdWindows.length = 0;
    navigations.length = 0;
    activations.length = 0;
    onSleep = onNavigate = onActivate = failUrl = null;
    sleepCount = 0;
    onMove = onCreate = null;
    onWindowUpdate = onRemoveTabs = onGetTab = onGetWindow = null;
    leaseClock = 1000000;
    leaseTimers.clear();
    movedTabs.length = 0;
}

async function restore(saved, token = 'current:saved') {
    return context.dispatch({action: 'restore_window', payload: {window: saved, restore_token: token}});
}

async function repair(payload) {
    return context.dispatch({action: 'repair_restored_tabs', payload});
}

async function testIdentificationLeases() {
    const identify = payload => context.dispatch({action: 'identify_window', payload});
    const release = payload => context.dispatch({action: 'release_window_identification', payload});
    const expire = () => context.dispatch({action: 'release_expired_identifications'});
    const ping = () => context.dispatch({action: 'ping'});
    const setup = () => {
        reset();
        windows = [chromeWindow(71, ['https://example.com/original', 'https://example.com/other'])];
        return windows[0].tabs.map(tab => tab.id);
    };
    for (const failure of ['focus', 'title', 'create-reply']) {
        const ids = setup();
        if (failure === 'focus') onWindowUpdate = () => { throw new Error('Focus failed'); };
        if (failure === 'title') onGetTab = id => {
            if (!ids.includes(id)) { onGetTab = null; throw new Error('Title read failed'); }
        };
        if (failure === 'create-reply') onCreate = () => { throw new Error('Create reply failed'); };
        await assert.rejects(identify({window_id: 71, token: 'failed-identify'}), /failed/);
        assert.deepEqual(windows[0].tabs.map(tab => tab.id), ids,
            `${failure} failure must not orphan the created marker`);
        assert.equal(windows[0].tabs[0].active, true);
    }

    let ids = setup();
    let marker = await identify({window_id: 71, token: 'lost-caller', focus: false});
    assert.ok(Number.isFinite(marker.expires_at));
    await assert.rejects(identify({window_id: 71, token: 'competing-caller'}), /identification/);
    assert.equal((await ping()).active_identifications, 1, 'a second caller cannot discard a live lease');
    let cleanup = await expire();
    assert.equal(cleanup.released, 0);
    assert.equal(cleanup.live_identifications, 1, 'shutdown cleanup defers a live lease');
    leaseClock = marker.expires_at;
    cleanup = await expire();
    assert.equal(cleanup.released, 1, 'a cancelled caller cannot hold an expired lease forever');
    assert.equal(cleanup.active_identifications, 0);
    assert.deepEqual(windows[0].tabs.map(tab => tab.id), ids);
    assert.equal(windows[0].tabs[0].active, true);

    ids = setup();
    marker = await identify({window_id: 71, token: 'failed-release', focus: false});
    onRemoveTabs = () => { throw new Error('Removal rejected'); };
    assert.equal((await release(marker)).released, false, 'removal failure cannot report successful release');
    assert.equal((await ping()).active_identifications, 1);
    onRemoveTabs = null;
    assert.equal((await release(marker)).released, true);
    assert.equal((await release(marker)).released, true, 'a lost release reply is safely retryable');
    assert.deepEqual(windows[0].tabs.map(tab => tab.id), ids);

    setup();
    marker = await identify({window_id: 71, token: 'renewed', focus: false});
    const originalExpiry = marker.expires_at;
    leaseClock += 30000;
    const renewed = await context.dispatch({action: 'renew_window_identification', payload: marker});
    assert.ok(renewed.expires_at > originalExpiry);
    leaseClock = originalExpiry;
    assert.equal((await expire()).released, 0, 'renewed live identification cannot expire at its old deadline');
    leaseClock = renewed.expires_at;
    await assert.rejects(context.dispatch({action: 'renew_window_identification', payload: marker}), /Live matching/);
    const timers = [...leaseTimers.values()];
    assert.equal(timers.length, 1, 'renewal replaces the original cleanup timer');
    await timers[0]();
    assert.equal((await ping()).active_identifications, 0, 'timer cleans an orphan without a shutdown request');

    for (const failure of ['removal', 'sole-tab']) {
        setup();
        marker = await identify({window_id: 71, token: 'bounded-cleanup', focus: false});
        let removalAttempts = 0;
        if (failure === 'removal') onRemoveTabs = () => { removalAttempts += 1; throw new Error('Removal rejected'); };
        else windows[0].tabs = [findTab(marker.marker_tab_id)];
        leaseClock = marker.expires_at;
        await [...leaseTimers.values()][0]();
        assert.equal(leaseTimers.size, 0, `${failure} cannot schedule an endless expiry retry`);
        await context.dispatch({action: 'release_expired_identifications', payload: {automatic: true}});
        assert.equal(removalAttempts, failure === 'removal' ? 1 : 0);
        assert.equal((await ping()).expired_identifications, 1);
        if (failure === 'removal') {
            onRemoveTabs = null;
            assert.equal((await expire()).released, 1, 'explicit cleanup can retry after automatic failure');
        }
    }

    for (const change of ['active', 'url', 'pending', 'sole-tab']) {
        ids = setup();
        marker = await identify({window_id: 71, token: 'user-edit', focus: false});
        const tab = findTab(marker.marker_tab_id);
        if (change === 'active') windows[0].tabs.forEach(item => { item.active = item.id === ids[1]; });
        if (change === 'url') tab.url = 'https://example.com/user-page';
        if (change === 'pending') tab.pendingUrl = 'https://example.com/user-page';
        if (change === 'sole-tab') windows[0].tabs = [tab];
        leaseClock = marker.expires_at;
        cleanup = await expire();
        if (change === 'active') {
            assert.deepEqual(windows[0].tabs.map(item => item.id), ids);
            assert.equal(findTab(ids[1]).active, true, 'expiry must preserve the user-selected tab');
        } else {
            assert.ok(windows[0].tabs.some(item => item.id === marker.marker_tab_id),
                `${change}: cleanup must not close real content or the last tab`);
        }
        if (change === 'sole-tab') {
            assert.equal(cleanup.active_identifications, 1);
            assert.equal(cleanup.released, 0);
        }
    }

    ids = setup();
    marker = await identify({window_id: 71, token: 'wrong-token', focus: false});
    assert.equal((await release({...marker, token: 'other-token'})).released, false);
    assert.equal((await ping()).active_identifications, 1);
    assert.equal((await release({window_id: 71, token: marker.token})).released, true,
        'cancellation can release its known token even when the create reply was lost');

    for (const browserRestart of [false, true]) {
        setup();
        marker = await identify({window_id: 71, token: 'restart', focus: false});
        if (browserRestart) {
            for (const key of Object.keys(session)) delete session[key];
            windows[0].id = 72;
            windows[0].tabs.forEach(tab => { tab.windowId = 72; tab.id = nextTabId++; });
        }
        leaseTimers.clear(); // The old service worker no longer owns a timer.
        const restarted = vm.createContext({chrome, console, URL, Date: {now: () => leaseClock},
            setTimeout: callback => { const id = ++nextTimerId; leaseTimers.set(id, callback); return id; },
            clearTimeout: id => leaseTimers.delete(id)});
        vm.runInContext(definitions, restarted);
        leaseClock = marker.expires_at;
        cleanup = await restarted.dispatch({action: 'release_expired_identifications'});
        assert.equal(cleanup.released, 1, 'owned orphan remains recoverable after worker/browser restart');
        assert.equal(cleanup.active_identifications, 0);
        assert.equal(windows[0].tabs.length, 2);
        if (!browserRestart)
            assert.equal(windows[0].tabs[0].active, true, 'same-session cleanup restores the original active tab');
    }

    setup();
    marker = await identify({window_id: 71, token: 'duplicate-after-crash', focus: false});
    for (const key of Object.keys(session)) delete session[key];
    windows.push(chromeWindow(72, ['https://example.com/real', findTab(marker.marker_tab_id).url]));
    leaseClock = marker.expires_at;
    cleanup = await expire();
    assert.equal(cleanup.released, 0, 'durable journal cannot choose between duplicated restored markers');
    assert.equal(cleanup.active_identifications, 2);
    assert.equal(windows[0].tabs.length, 3);
    assert.equal(windows[1].tabs.length, 2);

    setup();
    marker = await identify({window_id: 71, token: 'lost-session-write', focus: false});
    // Model termination after durable commit but before the corresponding
    // session write: the old session map exists, but lacks the new lease.
    session.__wsctl_identification_leases = {};
    leaseClock = marker.expires_at;
    assert.equal((await expire()).released, 1, 'an older session map cannot hide durable ownership');
    assert.equal(windows[0].tabs.length, 2);

    setup();
    marker = await identify({window_id: 71, token: 'invalid-journal', focus: false});
    localStorage.__wsctl_identification_leases[marker.token].marker_url = 'https://example.com/original';
    localStorage.__wsctl_identification_leases[marker.token].marker_tab_id = windows[0].tabs[0].id;
    leaseClock = marker.expires_at;
    cleanup = await expire();
    assert.equal(cleanup.released, 0, 'journal entries cannot authorize closure of non-marker URLs');
    assert.equal(windows[0].tabs.length, 3);

    setup();
    windows[0].tabs.push({...chromeWindow(71, ['chrome-extension://test/identify.html?token=legacy']).tabs[0], index: 2});
    cleanup = await expire();
    assert.equal(cleanup.released, 0, 'unknown legacy marker has no proven expiry');
    assert.equal(cleanup.unowned_identifications, 1);

    setup();
    windows[0].type = 'popup';
    marker = await identify({window_id: 71, token: 'popup'});
    assert.equal(marker.marker_tab_id, null);
    assert.equal((await ping()).active_identifications, 0, 'popup focus creates no marker lease');
    assert.equal((await release(marker)).released, true);
    assert.equal(windows[0].tabs.length, 2);
}

async function testNativeReconnect() {
    const ports = [];
    const timers = new Map();
    const config = {};
    let timerId = 0;
    let worker;
    let writes = 0;
    const mockChrome = {
        runtime: {
            getURL: name => `chrome-extension://test/${name}`,
            connectNative: () => {
                const port = {messages: [], disconnects: 0,
                    onMessage: {addListener: callback => { port.receive = callback; }},
                    onDisconnect: {addListener: callback => { port.disconnected = callback; }},
                    postMessage: message => port.messages.push(message),
                    // Chrome does not promise an initiator-side event here.
                    disconnect: () => { port.disconnects += 1; },
                };
                ports.push(port);
                return port;
            },
        },
        storage: {session: {get: async () => ({})}, local: {
            get: async () => ({...config}),
            set: async values => {
                writes += 1;
                const changes = Object.fromEntries(Object.entries(values).map(([key, value]) => [key, {oldValue: config[key], newValue: value}]));
                Object.assign(config, values);
                worker.onStorageChanged(changes, 'local');
            },
        }},
    };
    worker = vm.createContext({chrome: mockChrome, URL, console,
        setTimeout: callback => { const id = ++timerId; timers.set(id, callback); return id; },
        clearTimeout: id => timers.delete(id),
    });
    vm.runInContext(definitions, worker);
    await worker.connectNativeHost();
    assert.equal(ports.length, 1);
    await ports[0].receive({type: 'hello', ok: true, profile: 'Default', profileDirectory: 'Default'});
    assert.equal(writes, 0, 'unchanged hello defaults must not disconnect the first connection');
    await ports[0].receive({type: 'hello', ok: true, profile: 'Profile 1', profileDirectory: 'Profile 1'});
    assert.equal(ports[0].disconnects, 1);
    assert.equal(timers.size, 1, 'explicit local disconnect must schedule its own reconnect');
    const [id, reconnect] = [...timers][0];
    timers.delete(id);
    await reconnect();
    assert.equal(ports.length, 2);
    assert.equal(ports[1].messages[0].profileDirectory, 'Profile 1');
    ports[0].disconnected();
    assert.equal(timers.size, 0, 'late old-port disconnect must not clear the replacement');
    await worker.connectNativeHost();
    assert.equal(ports.length, 2, 'replacement port remains owned');
    await ports[1].receive({type: 'hello', ok: true, profile: 'Profile 1', profileDirectory: 'Profile 1'});
    assert.equal(ports[1].disconnects, 0, 'unchanged hello must not create a reconnect loop');
    worker.onStorageChanged({unrelated: {newValue: true}}, 'local');
    assert.equal(ports[1].disconnects, 0);
    ports[1].disconnected();
    assert.equal(timers.size, 1, 'remote disconnect retains bounded automatic retry');
}

async function testInstalledBuildActivation() {
    const oldRevision = 'r-' + '1'.repeat(24);
    const newRevision = 'r-' + '2'.repeat(24);
    const local = {};
    let claims = {};
    let open = [];
    let reloads = 0;
    function worker() {
        const mockChrome = {
            runtime: {getURL: name => `chrome-extension://test/${name}`, reload: () => {
                reloads += 1;
                claims = {}; // Chrome clears session storage on extension reload.
            }},
            storage: {
                local: {get: async () => ({...local}), set: async value => Object.assign(local, value)},
                session: {get: async () => ({...claims})},
            },
            windows: {getAll: async () => open},
        };
        const result = vm.createContext({chrome: mockChrome, URL, console,
            WSCTL_BUILD_REVISION: oldRevision, setTimeout, clearTimeout});
        vm.runInContext(definitions, result);
        return result;
    }
    let current = worker();
    await current.activateInstalledBuild({revision: oldRevision}, null);
    await current.activateInstalledBuild({revision: 'development'}, null);
    assert.equal(reloads, 0, 'same or unknown installed build never reloads');
    claims = {'restore:one': {windowId: 7, created: false}};
    await current.activateInstalledBuild({revision: newRevision}, null);
    assert.equal(reloads, 0, 'restore ownership must survive mismatch');
    await current.activateInstalledBuild({revision: oldRevision}, null);
    assert.equal(vm.runInContext('activationPending', current), false, 'rollback to the loaded build clears pending activation');
    await current.activateInstalledBuild({revision: newRevision}, null);
    await assert.rejects(current.dispatch({action: 'restore_window'}), /activation is pending/);
    claims = {};
    open = [chromeWindow(7, ['chrome-extension://test/identify.html?token=held'])];
    await current.activateInstalledBuild({revision: newRevision}, null);
    assert.equal(reloads, 0, 'identification lease must survive mismatch');
    open = [];
    vm.runInContext('activeNativeMutations = 1', current);
    await current.activateInstalledBuild({revision: newRevision}, null);
    assert.equal(reloads, 0, 'in-flight work must survive mismatch');
    vm.runInContext('activeNativeMutations = 0', current);
    await current.activateInstalledBuild({revision: newRevision}, null);
    assert.equal(reloads, 1);
    assert.equal(local.activationAttempt, newRevision);
    current = worker(); // Even a failed reload with the old worker cannot loop.
    await current.activateInstalledBuild({revision: newRevision}, null);
    assert.equal(reloads, 1, 'local guard survives extension reload and worker replacement');
}

async function testNativeGroupReuseOnly() {
    const urls = ['one', 'two', 'three', 'four', 'five'].map(name => `https://example.com/${name}`);
    const saved = chromeWindow('saved-groups', urls);
    saved.groups = [
        {id: 'saved-a', title: '', color: 'blue', collapsed: true},
        {id: 'saved-b', title: '', color: 'blue', collapsed: false},
    ];
    saved.tabs.forEach((tab, index) => { tab.group = index < 2 ? 'saved-a' : index < 4 ? 'saved-b' : null; });
    const token = 'groups:saved';
    const status = () => context.dispatch({action: 'restore_status', payload: {restore_token: token, window: saved}});
    function assertGroups(result, reused, missing) {
        assert.equal(result.groups_reused, reused);
        assert.equal(result.group_warnings.length, missing);
        assert.ok(result.group_warnings.every(warning => typeof warning === 'string' && warning.length > 0));
        assert.ok(result.group_warnings.every(warning => result.warnings.includes(warning)),
            'group warnings must reach the existing user-visible warning channel');
        assert.equal(groupMutations.length, 0, 'restoration cannot create, ungroup, move or rewrite any native group');
    }
    function setNativeGroups(membership) {
        windows = [chromeWindow(70, urls)];
        windows[0].tabs.forEach((tab, index) => { tab.groupId = membership[index]; });
        nativeGroups = [...new Set(membership.filter(id => id >= 0))].map(id => ({
            id, windowId: 70, title: '', color: 'blue', collapsed: false,
        }));
    }

    reset();
    setNativeGroups([501, 501, 502, 502, -1]);
    const nativeBefore = JSON.stringify(nativeGroups);
    const tabsBefore = windows[0].tabs.map(tab => [tab.id, tab.groupId]);
    for (const result of [await restore(saved, token), await restore(saved, token), await status()]) {
        assertGroups(result, 2, 0);
        assert.equal(result.window_id, 70);
        assert.equal(result.urls_restored, true);
    }
    assert.equal(JSON.stringify(nativeGroups), nativeBefore, 'blank-title, identical-color groups retain native metadata');
    assert.deepEqual(windows[0].tabs.map(tab => [tab.id, tab.groupId]), tabsBefore);
    assert.equal(createdWindows.length, 0);
    assert.equal(navigations.length, 0);
    // Chrome group IDs can change independently of saved groups or restore claims.
    // Recheck live tab membership instead of persisting the first matching IDs.
    windows[0].tabs.forEach(tab => { if (tab.groupId >= 0) tab.groupId += 100; });
    nativeGroups.forEach(group => { group.id += 100; });
    assertGroups(await restore(saved, token), 2, 0);
    assertGroups(await status(), 2, 0);

    reset();
    setNativeGroups([501, 501, 502, 502, -1]);
    windows.unshift(chromeWindow(69, urls));
    const candidatesBefore = JSON.stringify({groups: nativeGroups, windows});
    const preferred = await restore(saved, token);
    assert.equal(preferred.window_id, 70, 'prefer the original grouped window over an earlier ungrouped URL lookalike');
    assertGroups(preferred, 2, 0);
    assert.equal(createdWindows.length, 0);
    assert.equal(JSON.stringify({groups: nativeGroups, windows}), candidatesBefore,
        'candidate selection preserves both windows, tab IDs and native group IDs');

    reset();
    await assert.rejects(() => restore(saved, token), /no replacement window created/);
    assert.equal(createdWindows.length, 0, 'missing originals cannot produce ungrouped replacement windows');
    assert.equal(navigations.length, 0);
    assert.equal(Object.keys(session).length, 0);
    // A partial original group window must also survive without duplication.
    windows = [chromeWindow(70, urls.slice(0, 2))];
    windows[0].tabs.forEach(tab => { tab.groupId = 501; });
    const partialBefore = JSON.stringify(windows);
    await assert.rejects(() => restore(saved, token), /no replacement window created/);
    assert.equal(JSON.stringify(windows), partialBefore);
    assert.equal(createdWindows.length, 0);

    for (const [membership, reused, missing] of [
        [[501, 502, 501, 502, -1], 0, 2], // Equal titles/colors cannot hide split membership.
        [[501, 501, 502, 502, 501], 1, 1], // A superset is not the saved group.
    ]) {
        reset();
        setNativeGroups(membership);
        const original = JSON.stringify({groups: nativeGroups, tabs: windows[0].tabs});
        assertGroups(await restore(saved, token), reused, missing);
        assertGroups(await restore(saved, token), reused, missing);
        assertGroups(await status(), reused, missing);
        assert.equal(JSON.stringify({groups: nativeGroups, tabs: windows[0].tabs}), original,
            'conflicting native group membership and metadata must remain unchanged');
        assert.equal(createdWindows.length, 0);
        assert.equal(navigations.length, 0);
    }
}

async function testOriginalWindowRecovery() {
    const url = name => `https://example.com/${name}`;
    const token = 'recovery:saved';
    function setup({partial = false, adopt = false} = {}) {
        reset();
        const saved = chromeWindow('saved-original', [url('one'), url('two'), url('three'), url('four'), url('five')]);
        saved.groups = [{id: 'group-a', title: '', color: 'green'}];
        saved.tabs.forEach((tab, index) => { tab.group = index < 3 ? 'group-a' : null; });
        const original = chromeWindow(50, partial ? [url('one'), url('two'), url('three')] :
            [url('one'), url('two-current'), url('three'), url('extra'), url('five')]);
        original.tabs.forEach((tab, index) => { tab.groupId = index < 3 ? 700 : -1; });
        const duplicate = chromeWindow(60, saved.tabs.map(tab => tab.url));
        windows = [original, duplicate];
        nativeGroups = [{id: 700, windowId: 50, title: '', color: 'green', collapsed: false}];
        if (!adopt)
            session[token] = {windowId: duplicate.id, created: true, windowState: saved};
        const payload = {restore_token: token, original_window_id: original.id,
            ...(adopt ? {adopt_created_window: true, created_window_id: duplicate.id, window: saved} : {})};
        return {saved, original, duplicate, payload};
    }
    const inspect = payload => context.dispatch({action: 'inspect_original_window', payload});
    const recover = payload => context.dispatch({action: 'recover_original_window', payload});

    for (const options of [{}, {partial: true}, {adopt: true}]) {
        const {saved, original, duplicate, payload} = setup(options);
        const groupIds = original.tabs.slice(0, 3).map(tab => tab.id);
        const groupMetadata = JSON.stringify(nativeGroups);
        const originalExtraId = !options.partial ? original.tabs[3].id : null;
        const before = JSON.stringify({windows, session});
        const inspection = await inspect(payload);
        assert.equal(JSON.stringify({windows, session}), before, 'inspection has no native or storage mutations');
        const request = {...payload, expected_plan: inspection.expected_plan};
        const result = await recover(request);
        assert.equal(result.recovery_complete, true);
        assert.equal(result.urls_restored, true);
        assert.equal(result.groups_reused, 1);
        assert.equal(result.window_id, original.id);
        assert.equal(result.recovery_window_id, duplicate.id);
        assert.deepEqual(original.tabs.map(tab => tab.url), saved.tabs.map(tab => tab.url));
        assert.deepEqual(original.tabs.slice(0, 3).map(tab => tab.id), groupIds,
            'same original member tabs and native group object survive recovery');
        assert.ok(original.tabs.slice(0, 3).every(tab => tab.groupId === 700));
        assert.equal(JSON.stringify(nativeGroups), groupMetadata);
        if (originalExtraId) {
            assert.equal(findTab(originalExtraId).windowId, duplicate.id, 'extra original page is moved intact into recovery window');
            assert.ok(duplicate.tabs.some(tab => tab.url === url('two-current')),
                'divergent group page is retained before its original tab navigates');
        }
        assert.equal(session[token].windowId, original.id);
        assert.equal(session[token].created, false);
        const after = JSON.stringify({windows, moves: movedTabs, navigations});
        assert.equal((await recover(request)).recovery_complete, true);
        assert.equal(JSON.stringify({windows, moves: movedTabs, navigations}), after,
            'completed journal retries do not repeat copying, moving or navigation');
        assert.equal(groupMutations.length, 0);
        assert.equal(removedWindows.length, 0);
        assert.equal(createdWindows.length, 0);
    }

    for (const change of ['url', 'groupId', 'order', 'claim']) {
        const {original, payload} = setup();
        const inspection = await inspect(payload);
        if (change === 'url') original.tabs[0].url = url('new-user-page');
        if (change === 'groupId') original.tabs[0].groupId = 999;
        if (change === 'order') [original.tabs[0].index, original.tabs[1].index] = [1, 0];
        if (change === 'claim') session['other:claim'] = {windowId: original.id, created: false};
        const before = JSON.stringify(windows);
        await assert.rejects(() => recover({...payload, expected_plan: inspection.expected_plan}));
        assert.equal(JSON.stringify(windows), before, `changed ${change} is rejected before recovery mutation`);
        assert.equal(movedTabs.length, 0);
        assert.equal(navigations.length, 0);
    }

    {
        const {original, payload} = setup();
        const lookalike = chromeWindow(51, original.tabs.map(tab => tab.url));
        lookalike.tabs.forEach((tab, index) => { tab.groupId = index < 3 ? 701 : -1; });
        windows.push(lookalike);
        await assert.rejects(() => inspect(payload), /More than one original/);
    }
    {
        const {payload} = setup();
        session[token].created = false;
        await assert.rejects(() => inspect(payload), /known created duplicate/);
    }
    {
        const {original, payload} = setup();
        original.tabs[0].url = url('unrelated-one');
        original.tabs[2].url = url('unrelated-three');
        await assert.rejects(() => inspect(payload), /membership is missing or ambiguous/);
    }
    {
        const {original, payload} = setup();
        original.tabs[3].groupId = 800;
        await assert.rejects(() => inspect(payload), /additional original group/);
    }
    {
        const {original, payload} = setup();
        const inspection = await inspect(payload);
        const groupPageId = original.tabs[1].id;
        onNavigate = tab => { if (tab.id !== groupPageId) tab.url = url('redirect'); };
        await assert.rejects(() => recover({...payload, expected_plan: inspection.expected_plan}), /preserve the divergent original page/);
        assert.equal(findTab(groupPageId).url, url('two-current'), 'failed copy leaves original group page intact');
        assert.equal(movedTabs.length, 0);
    }
    {
        const {payload} = setup({partial: true});
        const inspection = await inspect(payload);
        const request = {...payload, expected_plan: inspection.expected_plan};
        onMove = () => { onMove = null; throw new Error('Lost move reply'); };
        await assert.rejects(() => recover(request), /Lost move reply/);
        assert.equal(movedTabs.length, 1);
        assert.equal((await recover(request)).recovery_complete, true);
        assert.equal(movedTabs.length, 2, 'journal recognizes a completed move after a lost API reply');
    }
    {
        const {original, duplicate, payload} = setup();
        const inspection = await inspect(payload);
        const request = {...payload, expected_plan: inspection.expected_plan};
        onCreate = () => { onCreate = null; throw new Error('Lost copy reply'); };
        await assert.rejects(() => recover(request), /Lost copy reply/);
        const copyCount = duplicate.tabs.length;
        await assert.rejects(() => recover(request), /Recovery tabs changed/);
        assert.equal(duplicate.tabs.length, copyCount, 'uncertain copy completion never repeats a blind copy');
        assert.equal(original.tabs[1].url, url('two-current'));
    }
    {
        const {original, payload} = setup();
        const inspection = await inspect(payload);
        const request = {...payload, expected_plan: inspection.expected_plan};
        onCreate = tab => { tab.status = 'loading'; };
        onSleep = () => { original.tabs[0].url = url('changed-during-copy'); };
        await assert.rejects(() => recover(request), /has not loaded exactly/);
        assert.equal(original.tabs[1].url, url('two-current'));
        assert.equal(groupMutations.length, 0);
    }
}

async function testLateUrlCompletion() {
    const url = 'https://example.com/saved?view=detail#message';
    const saved = chromeWindow('saved', [url]);
    for (const completeSamples of [[40], [38, 40], [39, 40]]) {
        reset();
        windows = [chromeWindow(39, ['about:blank'])];
        const tab = windows[0].tabs[0];
        Object.assign(tab, {pendingUrl: url, status: 'loading'});
        onSleep = () => {
            if (completeSamples.includes(sleepCount + 1)) {
                Object.assign(tab, {url, status: 'complete'});
                delete tab.pendingUrl;
            } else {
                tab.status = 'loading';
            }
        };
        const result = await restore(saved);
        const stable = completeSamples.includes(39);
        assert.equal(sleepCount, 39, 'verification keeps its fixed sample bound');
        assert.equal(result.urls_restored, stable,
            'only consecutive complete observations establish stability');
        if (!stable) {
            assert.equal(result.urls_pending, true,
                'final-sample completion must remain observable until stability is confirmed');
            assert.match(result.url_errors.join(' '), /stability/);
            tab.status = 'loading';
            const loading = await context.restoredWindowStatus({restore_token: 'current:saved'});
            assert.equal(loading.urls_restored, false);
            assert.equal(loading.urls_pending, true,
                'exact committed URLs that resume loading remain pending');
            tab.status = 'complete';
        }
        const status = await context.restoredWindowStatus({restore_token: 'current:saved'});
        assert.equal(status.urls_restored, true, 'later status observes exact completion');
        assert.equal(status.window_id, result.window_id);
        assert.equal(createdWindows.length, 0);
        assert.equal(navigations.length, 0);
        assert.equal(activations.length, 0);
    }
}

async function testRestoreClaimRetrySafety() {
    const urls = ['https://example.test/one', 'https://example.test/two'];
    const saved = chromeWindow('saved', urls);
    const token = 'retry:saved';

    for (const action of ['restore_window', 'restore_status']) {
        reset();
        windows = [chromeWindow(80, urls)];
        await restore(saved, token);
        windows[0].tabs[0].url = 'https://example.test/user-navigation';
        onGetWindow = () => { throw new Error('Transient window lookup failure'); };
        await assert.rejects(() => context.dispatch({action, payload: {window: saved, restore_token: token}}),
            /Transient window lookup failure/);
        assert.equal(session[token].windowId, 80, `${action} cannot discard an unproven stale claim`);
        const getAll = chrome.windows.getAll;
        try {
            for (const lookup of [async () => { throw new Error('Window list unavailable'); }, async () => null]) {
                chrome.windows.getAll = lookup;
                await assert.rejects(() => context.dispatch({action, payload: {window: saved, restore_token: token}}));
                assert.equal(session[token].windowId, 80, 'an unavailable window list never proves absence');
            }
        } finally {
            chrome.windows.getAll = getAll;
        }
        onGetWindow = null;
        const retry = await restore(saved, token);
        assert.equal(retry.window_id, 80);
        assert.equal(retry.urls_restored, false);
        assert.equal(createdWindows.length, 0);
        assert.equal(navigations.length, 0);
    }

    reset();
    onNavigate = tab => {
        const window = findWindow(tab.windowId);
        const userTab = chromeWindow(window.id, ['https://example.test/opened-during-restore']).tabs[0];
        userTab.index = window.tabs.length;
        window.tabs.push(userTab);
    };
    const createTab = chrome.tabs.create;
    chrome.tabs.create = async () => { throw new Error('Tab creation interrupted'); };
    try {
        await assert.rejects(() => restore(saved, token), /Tab creation interrupted/);
    } finally {
        chrome.tabs.create = createTab;
    }
    assert.equal(windows.length, 1, 'partial creation remains inspectable after an API failure');
    const partialId = windows[0].id;
    assert.equal(session[token].windowId, partialId);
    assert.equal(windows[0].tabs[1].url, 'https://example.test/opened-during-restore',
        'failure preserves a tab the user opened while restoration was active');
    windows[0].tabs[0].url = 'https://example.test/user-kept-tab';
    const partialRetry = await restore(saved, token);
    assert.equal(partialRetry.window_id, partialId);
    assert.equal(partialRetry.urls_restored, false);
    assert.equal(createdWindows.length, 1, 'partial creation retry never opens another window');
    assert.equal(removedWindows.length, 0, 'an interrupted operation never closes user tabs');
    assert.equal(windows[0].tabs[0].url, 'https://example.test/user-kept-tab');

    // Model a caller cancelled after dispatch: the worker finishes its request
    // and a new caller retries the same intent while the old request is queued.
    reset();
    windows = [chromeWindow(81, urls)];
    Object.assign(windows[0].tabs[1], {url: 'about:blank', pendingUrl: urls[1], status: 'loading'});
    onSleep = () => {
        Object.assign(windows[0].tabs[1], {url: urls[1], status: 'complete'});
        delete windows[0].tabs[1].pendingUrl;
    };
    const abandoned = restore(saved, token);
    const retried = restore(saved, token);
    const results = await Promise.all([abandoned, retried]);
    assert.ok(results.every(result => result.window_id === 81 && result.urls_restored));
    assert.equal(createdWindows.length, 0);
    assert.equal(navigations.length, 0);
}

async function testNativeIdentityAfterBrowserRestart() {
    const urls = ['https://example.test/one', 'https://example.test/two'];
    const saved = chromeWindow('saved', urls);
    saved.runtime_window_id = 17; // A stale numeric ID must never authorize reuse.
    saved.groups = [{id: 'group-1', title: '', color: 'blue'}];
    saved.tabs.forEach(tab => { tab.group = 'group-1'; });
    const token = 'native:saved';

    reset();
    windows = [chromeWindow(17, ['https://example.test/unrelated']), chromeWindow(82, urls)];
    windows[1].tabs.forEach(tab => { tab.groupId = 501; });
    Object.assign(windows[1].tabs[1], {url: 'about:blank', pendingUrl: urls[1], status: 'loading'});
    const tabIds = windows[1].tabs.map(tab => tab.id);
    onSleep = () => {
        Object.assign(windows[1].tabs[1], {url: urls[1], status: 'complete'});
        delete windows[1].tabs[1].pendingUrl;
    };
    const restored = await restore(saved, token);
    assert.equal(restored.window_id, 82);
    assert.equal(restored.groups_reused, 1);
    assert.equal(restored.urls_restored, true);
    assert.deepEqual(windows[1].tabs.map(tab => tab.id), tabIds);
    windows[1].tabs[0].url = 'https://example.test/changed';
    const retry = await restore(saved, token);
    assert.equal(retry.window_id, 82, 'a prior exact claim survives changed live URLs');
    assert.equal(retry.urls_restored, false);
    assert.equal(retry.groups_reused, 1);
    assert.equal(navigations.length, 0);
    assert.equal(createdWindows.length, 0);
    assert.equal(groupMutations.length, 0);

    // A genuinely new browser session has no valid numeric claim. Changed
    // contents cannot be identified by a matching group title/color alone.
    delete session[token];
    const before = JSON.stringify(windows);
    await assert.rejects(() => restore(saved, token), /no replacement window created/);
    assert.equal(JSON.stringify(windows), before);
    assert.equal(createdWindows.length, 0);
    assert.equal(groupMutations.length, 0);

    reset();
    windows = [chromeWindow(83, urls), chromeWindow(84, urls)];
    windows.forEach((window, index) => window.tabs.forEach(tab => { tab.groupId = 510 + index; }));
    const ambiguousBefore = JSON.stringify(windows);
    for (let attempt = 0; attempt < 2; attempt += 1)
        await assert.rejects(() => restore(saved, token), /identity is ambiguous/);
    assert.equal(JSON.stringify(windows), ambiguousBefore);
    assert.equal(Object.hasOwn(session, token), false);
    assert.equal(createdWindows.length, 0);
    assert.equal(groupMutations.length, 0);
    session[token] = {windowId: 84, created: false, windowState: saved};
    assert.equal((await restore(saved, token)).window_id, 84,
        'an existing exact claim remains authoritative despite another lookalike');
}

async function main() {
    const url = 'https://chatgpt.com/c/12345678?model=example#message';
    const saved = chromeWindow('saved', [url]);
    for (const wrong of [
        'https://chatgpt.com/',
        'https://chatgpt.com/c/different?model=example#message',
        'https://chatgpt.com/c/12345678?model=other#message',
        'https://chatgpt.com/c/12345678?model=example#different',
    ]) {
        reset();
        windows = [chromeWindow(1, [wrong])];
        assert.equal(await context.matchingOpenWindow(saved, 'current:saved'), null);
        const result = await restore(saved);
        assert.equal(result.urls_restored, true);
        assert.equal(result.created, true);
        assert.equal(windows[0].tabs[0].url, wrong, 'unrelated same-origin tabs stay untouched');
        assert.ok(navigations.every(item => item.id !== windows[0].tabs[0].id));
    }

    reset();
    windows = [chromeWindow(1, ['https://chatgpt.com/']), chromeWindow(2, [url])];
    assert.equal((await context.matchingOpenWindow(saved, 'current:saved')).id, 2);
    session['current:other'] = {windowId: 2, created: false};
    assert.equal(await context.matchingOpenWindow(saved, 'current:saved'), null,
        'claimed exact window cannot fall back to wrong same-origin window');
    session['older:other'] = {windowId: 1, created: false};
    assert.equal((await context.matchingOpenWindow(saved, 'newer:saved')).id, 2);
    assert.equal(context.browserWindowSummary(windows[1]).urls_loaded, true);
    assert.equal(context.browserWindowSummary(windows[0]).signature,
        context.browserWindowSummary(windows[1]).signature,
        'origin-only signatures remain available for GNOME window identification');
    assert.notEqual(context.browserWindowSummary(windows[0]).full_signature,
        context.browserWindowSummary(windows[1]).full_signature);

    reset();
    windows = [chromeWindow(21, [url]), chromeWindow(22, [url])];
    await assert.rejects(() => restore(saved, 'same:one'), /identity is ambiguous/);
    assert.equal(createdWindows.length, 0);
    assert.equal(Object.keys(session).length, 0, 'ambiguous originals cannot be assigned by array order');
    session['same:one'] = {windowId: 21, created: false, windowState: saved};
    const claims = await Promise.all([restore(saved, 'same:one'), restore(saved, 'same:two')]);
    assert.deepEqual(claims.map(result => result.window_id), [21, 22]);
    assert.ok(claims.every(result => result.urls_restored && result.reused && !result.created));
    assert.equal(navigations.length, 0);

    reset();
    windows = [chromeWindow(30, ['https://chatgpt.com/'])];
    session['current:saved'] = {windowId: 30, created: false};
    let result = await restore(saved);
    assert.equal(result.window_id, 30);
    assert.equal(result.urls_restored, false, 'live resume token must recheck exact URLs');
    assert.match(result.url_errors.join(' '), /URL mismatch/);
    assert.equal((await restore(saved)).window_id, 30);
    assert.equal(createdWindows.length, 0, 'failed resume must retain ID to avoid duplicates');
    assert.equal(navigations.length, 0, 'failed resume must not overwrite user navigation');
    let status = await context.restoredWindowStatus({restore_token: 'current:saved', window: saved});
    assert.equal(status.exists, true);
    assert.equal(status.urls_restored, false);
    assert.equal(status.window_id, 30);

    reset();
    session['current:saved'] = {windowId: 404, created: true};
    windows = [chromeWindow(31, [url])];
    result = await restore(saved);
    assert.equal(result.window_id, 31, 'closed stale token may claim an exact existing window');
    assert.equal(result.urls_restored, true);
    assert.equal(result.created, false);
    status = await context.restoredWindowStatus({restore_token: 'current:saved'});
    assert.equal(status.urls_restored, true, 'new records persist expected URLs for status checks');
    windows[0].tabs[0].url = 'https://chatgpt.com/';
    assert.equal((await context.restoredWindowStatus({restore_token: 'current:saved'})).urls_restored, false);

    reset();
    windows = [chromeWindow(32, [url])];
    session['current:saved'] = {windowId: 32, created: false};
    session['current:other'] = {windowId: 32, created: false};
    result = await restore(saved);
    assert.equal(result.urls_restored, false, 'conflicting persisted claims cannot both succeed');
    assert.match(result.url_errors.join(' '), /claimed/);

    reset();
    windows = [chromeWindow(33, ['https://chatgpt.com/'])];
    Object.assign(windows[0].tabs[0], {pendingUrl: url, status: 'loading'});
    assert.equal(context.browserWindowSummary(windows[0]).urls_loaded, false);
    onSleep = () => {
        windows[0].tabs[0].status = 'complete';
        delete windows[0].tabs[0].pendingUrl;
    };
    result = await restore(saved);
    assert.equal(result.window_id, 33);
    assert.equal(result.urls_restored, false, 'pending saved URL followed by homepage redirect must fail');
    assert.equal(result.urls_pending, false, 'completed redirect cannot remain pending');
    assert.equal(createdWindows.length, 0);

    reset();
    windows = [chromeWindow(38, ['about:blank'])];
    Object.assign(windows[0].tabs[0], {pendingUrl: url, status: 'loading'});
    onSleep = () => {
        Object.assign(windows[0].tabs[0], {url, status: 'complete'});
        delete windows[0].tabs[0].pendingUrl;
    };
    result = await restore(saved);
    assert.equal(result.window_id, 38);
    assert.equal(result.urls_restored, true, 'pending reuse succeeds only after exact committed completion');
    assert.equal(sleepCount, 2, 'completion must be observed twice');

    reset();
    windows = [chromeWindow(34, [url])];
    onSleep = () => { windows[0].tabs[0].url = 'https://chatgpt.com/'; };
    result = await restore(saved);
    assert.equal(result.urls_restored, false, 'immediate post-load redirect must fail');

    reset();
    onNavigate = (tab, requested) => {
        if (requested === url)
            Object.assign(tab, {url: 'about:blank', pendingUrl: url, status: 'loading'});
    };
    onSleep = () => {
        const tab = windows[0].tabs[0];
        Object.assign(tab, {url: 'https://chatgpt.com/', status: 'complete'});
        delete tab.pendingUrl;
    };
    result = await restore(saved);
    assert.equal(result.created, true);
    assert.equal(result.urls_restored, false, 'new window redirects must fail');
    const failedId = result.window_id;
    result = await restore(saved);
    assert.equal(result.window_id, failedId);
    assert.equal(createdWindows.length, 1, 'failed new window retains token and does not duplicate on retry');
    assert.deepEqual(removedWindows, []);

    reset();
    windows = [chromeWindow(35, [url])];
    Object.assign(windows[0].tabs[0], {pendingUrl: url, status: 'loading'});
    result = await restore(saved);
    assert.equal(result.urls_restored, false, 'permanently pending URL must time out');
    assert.equal(result.urls_pending, true, 'exact saved navigation remains observable after bounded wait');
    assert.ok(sleepCount > 1 && sleepCount <= 40, 'verification has a fixed total bound');
    assert.match(result.url_errors.join(' '), /not finished/);
    const loadingWindowId = result.window_id;
    let loadingStatus = await context.restoredWindowStatus({restore_token: 'current:saved'});
    assert.equal(loadingStatus.urls_pending, true);
    windows[0].tabs[0].status = 'complete';
    delete windows[0].tabs[0].pendingUrl;
    loadingStatus = await context.restoredWindowStatus({restore_token: 'current:saved'});
    assert.equal(loadingStatus.urls_restored, true, 'read-only status observes later exact completion');
    assert.equal(loadingStatus.window_id, loadingWindowId);
    assert.equal(createdWindows.length, 0);
    assert.equal(navigations.length, 0);

    reset();
    windows = [chromeWindow(35, [url])];
    Object.assign(windows[0].tabs[0], {pendingUrl: 'https://example.com/other', status: 'loading'});
    session['current:saved'] = {windowId: 35, created: false, windowState: saved};
    result = await restore(saved);
    assert.equal(result.urls_restored, false);
    assert.equal(result.urls_pending, false, 'different pending URL cannot claim saved navigation progress');
    assert.equal(createdWindows.length, 0);

    reset();
    windows = [chromeWindow(36, [url])];
    Object.assign(windows[0].tabs[0], {discarded: true, status: 'unloaded'});
    assert.equal(context.browserWindowSummary(windows[0]).urls_loaded, false);
    result = await restore(saved);
    assert.equal(result.urls_restored, false, 'discarded tab is not proof of loaded URL');
    assert.match(result.url_errors.join(' '), /not finished/);
    assert.equal(navigations.length, 0);
    assert.equal(activations.length, 1, 'discarded exact tab is activated once');
    assert.equal(sleepCount, 39, 'activation still waits for bounded completion');

    for (const lazyState of [{discarded: true}, {status: 'unloaded', discarded: false}]) {
        reset();
        const urls = [url, 'https://example.com/second', 'https://example.com/third'];
        const lazySaved = chromeWindow('saved', urls);
        windows = [chromeWindow(42, urls)];
        Object.assign(windows[0].tabs[1], lazyState);
        Object.assign(windows[0].tabs[2], lazyState);
        const originalActive = windows[0].tabs[0].id;
        session['current:saved'] = {windowId: 42, created: false, windowState: lazySaved};
        assert.equal((await context.restoredWindowStatus({restore_token: 'current:saved'})).urls_restored, false);
        assert.equal(activations.length, 0, 'status checks never load lazy tabs');
        onSleep = () => windows[0].tabs.forEach(tab => { tab.status = 'complete'; });
        result = await restore(lazySaved);
        assert.equal(result.urls_restored, true);
        assert.deepEqual(activations, [windows[0].tabs[1].id, windows[0].tabs[2].id, originalActive]);
        assert.equal(windows[0].tabs.find(tab => tab.active).id, originalActive);
        assert.equal(navigations.length, 0, 'activation must never issue URL updates');
        assert.equal(createdWindows.length, 0);
    }

    for (const navigation of ['fragment', 'pending']) {
        reset();
        const urls = ['https://example.com/active', 'https://example.com/mail#sent/message',
            'https://example.com/remaining'];
        const saved = chromeWindow('saved', urls);
        saved.groups = [{id: 'original', title: 'Original', color: 'green'}];
        saved.tabs.forEach(tab => { tab.group = 'original'; });
        windows = [chromeWindow(42, urls)];
        windows[0].tabs.forEach(tab => { tab.groupId = 77; });
        windows[0].tabs.slice(1).forEach(tab => { tab.status = 'unloaded'; });
        const tabIds = windows[0].tabs.map(tab => tab.id);
        onActivate = tab => {
            if (tab.index === 1) {
                if (navigation === 'fragment')
                    Object.assign(tab, {url: 'https://example.com/mail#inbox', status: 'complete'});
                else
                    Object.assign(tab, {url: 'about:blank', pendingUrl: urls[1], status: 'loading'});
            }
        };
        onSleep = () => {
            windows[0].tabs.forEach(tab => { tab.status = 'complete'; });
            if (navigation === 'pending') {
                windows[0].tabs[1].url = urls[1];
                delete windows[0].tabs[1].pendingUrl;
            }
        };
        result = await restore(saved);
        assert.deepEqual(activations, [tabIds[1], tabIds[2], tabIds[0]],
            'navigation from an activated lazy tab must not strand other unchanged lazy tabs');
        assert.equal(result.urls_restored, navigation === 'pending');
        if (navigation === 'fragment') {
            assert.match(result.url_errors.join(' '), /Tab 2 URL mismatch/);
            assert.doesNotMatch(result.url_errors.join(' '), /Window tabs changed/);
            assert.equal(windows[0].tabs[1].url, 'https://example.com/mail#inbox',
                'self-navigation is preserved and remains an exact URL verification failure');
        }
        assert.equal(result.groups_reused, 1);
        assert.deepEqual(windows[0].tabs.map(tab => [tab.id, tab.groupId]), tabIds.map(id => [id, 77]));
        assert.equal(windows[0].tabs[0].active, true);
        assert.equal(navigations.length, 0, 'lazy loading never rewrites page URLs');
        assert.equal(groupMutations.length, 0);
        assert.equal(createdWindows.length, 0);
    }

    reset();
    const lazyUrls = [url, 'https://example.com/second', 'https://example.com/third'];
    const lazySaved = chromeWindow('saved', lazyUrls);
    windows = [chromeWindow(43, lazyUrls)];
    windows[0].tabs[1].status = 'unloaded';
    onActivate = tab => {
        if (tab.index === 1) {
            tab.url = 'https://example.com/redirect';
            tab.status = 'complete';
        }
    };
    result = await restore(lazySaved);
    assert.equal(result.urls_restored, false, 'redirect after lazy activation fails');
    assert.equal(windows[0].tabs[0].active, true, 'restore original active tab after redirect');

    for (const change of ['replace', 'order', 'pin', 'group', 'url', 'pending']) {
        reset();
        windows = [chromeWindow(44, lazyUrls)];
        windows[0].tabs[1].status = 'unloaded';
        windows[0].tabs[2].status = 'unloaded';
        const tabIds = windows[0].tabs.map(tab => tab.id);
        let changedTabs;
        onActivate = tab => {
            if (tab.id !== tabIds[1])
                return;
            const remaining = windows[0].tabs[2];
            if (change === 'replace') remaining.id = nextTabId++;
            if (change === 'order') [tab.index, remaining.index] = [remaining.index, tab.index];
            if (change === 'pin') remaining.pinned = true;
            if (change === 'group') remaining.groupId = 78;
            if (change === 'url') remaining.url = 'https://example.com/user-page';
            if (change === 'pending') remaining.pendingUrl = 'https://example.com/user-page';
            changedTabs = windows[0].tabs.map(({active, ...other}) => ({...other}));
        };
        result = await restore(lazySaved);
        assert.equal(result.urls_restored, false);
        assert.match(result.url_errors.join(' '), /(?:Window tabs|Untouched tab URLs) changed/);
        assert.deepEqual(activations, [tabIds[1], tabIds[0]],
            `${change} during loading must prevent activation of the changed remaining tab`);
        assert.deepEqual(windows[0].tabs.map(({active, ...other}) => ({...other})), changedTabs,
            `${change} during loading is preserved`);
        assert.equal(windows[0].tabs[0].active, true);
        assert.equal(navigations.length, 0);
        assert.equal(groupMutations.length, 0);
    }

    reset();
    windows = [chromeWindow(45, lazyUrls)];
    windows[0].tabs[1].status = 'unloaded';
    onActivate = tab => {
        if (tab.index === 1)
            throw new Error('Activation rejected');
    };
    result = await restore(lazySaved);
    assert.equal(result.urls_restored, false);
    assert.match(result.url_errors.join(' '), /Activation rejected/);
    assert.equal(windows[0].tabs[0].active, true, 'finally restores original active tab after API failure');

    reset();
    windows = [chromeWindow(46, ['https://chatgpt.com/'])];
    windows[0].tabs[0].status = 'unloaded';
    session['current:saved'] = {windowId: 46, created: false};
    result = await restore(saved);
    assert.equal(result.urls_restored, false);
    assert.equal(activations.length, 0, 'mismatched token window must not activate lazy tabs');
    windows[0].tabs[0].url = url;
    windows[0].tabs[0].pendingUrl = 'https://chatgpt.com/';
    result = await restore(saved);
    assert.equal(result.urls_restored, false);
    assert.equal(activations.length, 0, 'conflicting pending navigation must not activate lazy tabs');
    assert.equal(sleepCount, 39, 'pending unloaded state receives the bounded wait');

    reset();
    windows = [chromeWindow(47, [url, 'about:blank'])];
    windows[0].tabs[0].status = 'unloaded';
    Object.assign(windows[0].tabs[1], {pendingUrl: lazyUrls[1], status: 'loading'});
    onSleep = () => {
        Object.assign(windows[0].tabs[1], {url: lazyUrls[1], status: 'complete'});
        delete windows[0].tabs[1].pendingUrl;
        if (activations.length)
            windows[0].tabs[0].status = 'complete';
    };
    result = await restore(chromeWindow('saved', lazyUrls.slice(0, 2)));
    assert.equal(result.urls_restored, true, 'lazy activation waits for other pending URLs to commit exactly');
    assert.equal(activations.length, 1);

    reset();
    failUrl = url;
    result = await restore(saved);
    assert.equal(result.urls_restored, false, 'navigation API failure cannot succeed as a warning');
    assert.match(result.warnings.join(' '), /Navigation rejected/);
    assert.equal(session['current:saved'].windowId, result.window_id);

    reset();
    failUrl = url;
    const twoTabs = chromeWindow('saved', ['https://example.com/first', url]);
    result = await restore(twoTabs);
    assert.equal(result.urls_restored, false, 'failed tab creation with fallback must fail URL verification');
    assert.match(result.warnings.join(' '), /Navigation rejected/);

    reset();
    windows = [chromeWindow(37, [url])];
    session.legacy = 37;
    status = await context.restoredWindowStatus({restore_token: 'legacy'});
    assert.equal(status.exists, true);
    assert.equal(status.urls_restored, false, 'legacy token without expected URLs cannot verify success');
    assert.equal((await context.restoredWindowStatus({restore_token: 'legacy', window: saved})).urls_restored, true);
    session.missing = {windowId: 404, created: true};
    assert.equal((await context.restoredWindowStatus({restore_token: 'missing', window: saved})).exists, false);
    assert.equal(Object.hasOwn(session, 'missing'), false);

    reset();
    windows = [chromeWindow(39, [url])];
    session['current:saved'] = {windowId: 39, created: false};
    windows[0].tabs[0].pinned = true;
    assert.equal((await restore(saved)).urls_restored, false);
    windows[0].tabs[0].pinned = false;
    windows[0].tabs.push({...windows[0].tabs[0], id: nextTabId++, index: 1});
    assert.equal((await restore(saved)).urls_restored, false);
    windows[0].tabs.pop();
    windows[0].incognito = true;
    assert.equal((await restore(saved)).urls_restored, false);
    assert.equal(navigations.length, 0, 'mismatched resume structure never rewrites live tabs');

    reset();
    session.reused = {windowId: 7, created: false};
    await context.closeRestoredWindow({window_id: 7, restore_token: 'reused', created: true});
    assert.deepEqual(removedWindows, [], 'a Chrome-restored window must never be closed');
    assert.equal(Object.hasOwn(session, 'reused'), false);
    session.created = {windowId: 8, created: true};
    await context.closeRestoredWindow({window_id: 8, restore_token: 'created', created: true});
    assert.deepEqual(removedWindows, [8]);
    assert.deepEqual({...context.restoreRecord(9)}, {windowId: 9, created: true});

    reset();
    windows = [chromeWindow(41, [url])];
    session.cleanup = {windowId: 41, created: true};
    const expectedSignature = context.browserWindowSummary(windows[0]).full_signature;
    const cleanup = {
        window_id: 41, restore_token: 'cleanup', created: true,
        expected_full_signature: expectedSignature,
    };
    windows[0].tabs[0].url = 'https://chatgpt.com/';
    assert.equal((await context.closeRestoredWindow(cleanup)).closed, false,
        'cleanup must recheck exact URLs immediately before closing');
    assert.ok(Object.hasOwn(session, 'cleanup'), 'refused cleanup preserves its token');
    assert.deepEqual(removedWindows, []);
    windows[0].tabs[0].url = url;
    windows[0].tabs[0].pendingUrl = url;
    assert.equal((await context.closeRestoredWindow(cleanup)).closed, false,
        'cleanup cannot close a loading window even if pending URLs match');
    delete windows[0].tabs[0].pendingUrl;
    windows[0].tabs[0].discarded = true;
    assert.equal((await context.closeRestoredWindow(cleanup)).closed, false);
    windows[0].tabs[0].discarded = false;
    assert.equal((await context.closeRestoredWindow(cleanup)).closed, true);
    assert.deepEqual(removedWindows, [41]);
    assert.equal(Object.hasOwn(session, 'cleanup'), false);

    reset();
    windows = [chromeWindow(41, [url]), chromeWindow(42, [url])];
    session.cleanup = {windowId: 42, created: false};
    assert.deepEqual({...await context.dispatch({action: 'close_restored_window', payload: cleanup})},
        {closed: false, released: false});
    assert.deepEqual(removedWindows, [], 'obsolete duplicate cleanup cannot use a migrated original claim');
    assert.equal(session.cleanup.windowId, 42, 'the migrated original-window token remains intact');

    const originalGetWindow = chrome.windows.get;
    session.cleanup = {windowId: 41, created: true};
    chrome.windows.get = async (...args) => {
        const window = await originalGetWindow(...args);
        session.cleanup = {windowId: 42, created: false};
        return window;
    };
    try {
        assert.equal((await context.closeRestoredWindow(cleanup)).closed, false);
        assert.deepEqual(removedWindows, [], 'migration during URL verification prevents closing the old target');
        assert.equal(session.cleanup.windowId, 42);
    } finally {
        chrome.windows.get = originalGetWindow;
    }

    const originalRemoveWindow = chrome.windows.remove;
    session.cleanup = {windowId: 41, created: true};
    chrome.windows.remove = async id => {
        await originalRemoveWindow(id);
        session.cleanup = {windowId: 42, created: false};
    };
    try {
        assert.deepEqual({...await context.closeRestoredWindow(cleanup)}, {closed: true, released: false});
        assert.equal(session.cleanup.windowId, 42, 'post-close cleanup preserves a concurrently migrated claim');
    } finally {
        chrome.windows.remove = originalRemoveWindow;
    }

    for (const rejected of [true, false]) {
        reset();
        windows = [chromeWindow(41, [url])];
        session.cleanup = {windowId: 41, created: true};
        let closeAttempts = 0;
        chrome.windows.remove = async () => {
            closeAttempts += 1;
            if (rejected)
                throw new Error('Close rejected');
        };
        try {
            const failedClose = await context.closeRestoredWindow(cleanup);
            assert.equal(failedClose.closed, false);
            assert.equal(failedClose.released, false);
            assert.equal(failedClose.retryable, true);
            assert.match(failedClose.reason, /still open/);
            if (rejected)
                assert.match(failedClose.reason, /Close rejected/);
            assert.equal(session.cleanup.windowId, 41, 'unverified closure preserves the original claim');
            assert.equal(closeAttempts, 1, 'bounded observation never repeats the close mutation');
            assert.ok(sleepCount > 0 && sleepCount < 30, 'close observation is bounded');
        } finally {
            chrome.windows.remove = originalRemoveWindow;
        }
    }

    reset();
    windows = [chromeWindow(41, [url])];
    session.cleanup = {windowId: 41, created: true};
    chrome.windows.remove = async () => {};
    onSleep = () => { if (sleepCount === 2) windows = []; };
    try {
        assert.equal((await context.closeRestoredWindow(cleanup)).closed, true,
            'accepted close waits until the target actually disappears');
        assert.equal(sleepCount, 2);
        assert.equal(Object.hasOwn(session, 'cleanup'), false);
    } finally {
        chrome.windows.remove = originalRemoveWindow;
    }

    reset();
    windows = [chromeWindow(41, [url])];
    session.cleanup = {windowId: 41, created: true};
    const originalGetAllWindows = chrome.windows.getAll;
    chrome.windows.getAll = async () => { throw new Error('Observation unavailable'); };
    try {
        const unknownClose = await context.closeRestoredWindow(cleanup);
        assert.equal(unknownClose.closed, false, 'failed window observation cannot prove closure');
        assert.equal(unknownClose.retryable, true);
        assert.match(unknownClose.reason, /Observation unavailable/);
        assert.equal(session.cleanup.windowId, 41);
    } finally {
        chrome.windows.getAll = originalGetAllWindows;
    }

    reset();
    session.cleanup = {windowId: 41, created: true};
    chrome.windows.remove = async () => { throw new Error('Window is already gone'); };
    try {
        assert.equal((await context.closeRestoredWindow({window_id: 41,
            restore_token: 'cleanup', created: true})).closed, true,
        'an absent target makes an unguarded retry idempotent despite a remove error');
        assert.equal(Object.hasOwn(session, 'cleanup'), false);
    } finally {
        chrome.windows.remove = originalRemoveWindow;
    }

    // All claim-changing restore/repair/cleanup actions share the restore queue.
    // A cleanup requested while recovery is running must observe its final claim.
    reset();
    windows = [chromeWindow(41, [url]), chromeWindow(42, [url])];
    session.cleanup = {windowId: 41, created: true};
    const realRecovery = context.recoverOriginalWindow;
    let finishRecovery;
    context.recoverOriginalWindow = () => new Promise(resolve => { finishRecovery = resolve; });
    try {
        const recovery = context.dispatch({action: 'recover_original_window', payload: {}});
        const queuedCleanup = context.dispatch({action: 'close_restored_window', payload: cleanup});
        await Promise.resolve();
        assert.deepEqual(removedWindows, [], 'cleanup waits for the in-flight original-window recovery');
        session.cleanup = {windowId: 42, created: false};
        finishRecovery({});
        await recovery;
        assert.equal((await queuedCleanup).closed, false);
        assert.deepEqual(removedWindows, []);
        assert.equal(session.cleanup.windowId, 42);
    } finally {
        context.recoverOriginalWindow = realRecovery;
    }

    const repairSaved = chromeWindow('saved', [
        'https://mail.proton.me/u/1/inbox#category=primary',
        'https://mail.google.com/mail/u/0/#inbox',
    ]);
    const socialUrl = 'https://mail.proton.me/u/1/inbox#category=social';
    function setupRepair(register = true) {
        reset();
        windows = [chromeWindow(50, [socialUrl, repairSaved.tabs[1].url])];
        if (register)
            session['manual:saved'] = {windowId: 50, created: false, windowState: repairSaved};
        return {
            restore_token: 'manual:saved', window_id: 50,
            expected_full_signature: context.browserWindowSummary(windows[0]).full_signature,
        };
    }

    let repairPayload = setupRepair();
    windows[0].tabs[1].status = 'unloaded';
    const repairedId = windows[0].tabs[0].id;
    onSleep = () => windows[0].tabs.forEach(tab => { tab.status = 'complete'; });
    result = await repair(repairPayload);
    assert.equal(result.urls_restored, true);
    assert.deepEqual(navigations, [{id: repairedId, url: repairSaved.tabs[0].url}],
        'manual repair updates only the mismatched saved URL');
    assert.deepEqual([...result.repaired_tab_ids], [repairedId]);
    assert.equal(windows[0].tabs[0].active, true);
    assert.equal(createdWindows.length, 0);

    repairPayload = setupRepair();
    windows[0].tabs[0].url = 'https://example.com/changed-after-inspection';
    result = await repair(repairPayload);
    assert.equal(result.urls_restored, false);
    assert.equal(navigations.length, 0, 'stale inspection signature must reject repair');

    repairPayload = setupRepair();
    windows[0].tabs[1].pinned = true;
    repairPayload.expected_full_signature = context.browserWindowSummary(windows[0]).full_signature;
    result = await repair(repairPayload);
    assert.equal(result.urls_restored, false);
    assert.equal(navigations.length, 0, 'even a fresh signature cannot authorize changed saved structure');

    for (const change of [
        window => { window.type = 'popup'; },
        window => { window.incognito = true; },
        window => { window.tabs.pop(); },
    ]) {
        repairPayload = setupRepair();
        change(windows[0]);
        repairPayload.expected_full_signature = context.browserWindowSummary(windows[0]).full_signature;
        assert.equal((await repair(repairPayload)).urls_restored, false);
        assert.equal(navigations.length, 0);
    }

    repairPayload = setupRepair();
    repairPayload.window_id = 51;
    assert.equal((await repair(repairPayload)).urls_restored, false);
    assert.equal(navigations.length, 0, 'repair cannot switch a token to another window');

    repairPayload = setupRepair();
    repairPayload.window = chromeWindow('other', [socialUrl, repairSaved.tabs[1].url]);
    assert.equal((await repair(repairPayload)).urls_restored, false);
    assert.equal(navigations.length, 0, 'existing saved record remains authoritative');

    repairPayload = setupRepair();
    onNavigate = () => { windows[0].tabs[1].id = nextTabId++; };
    result = await repair(repairPayload);
    assert.equal(result.urls_restored, false);
    assert.match(result.url_errors.join(' '), /changed during manual repair/);
    assert.equal(navigations.length, 1);

    repairPayload = setupRepair();
    windows[0].tabs[1].url = 'https://mail.google.com/mail/u/0/#social';
    repairPayload.expected_full_signature = context.browserWindowSummary(windows[0]).full_signature;
    onNavigate = tab => { tab.url = socialUrl; };
    result = await repair(repairPayload);
    assert.equal(result.urls_restored, false);
    assert.equal(navigations.length, 1, 'redirect after first repair stops before mutating another tab');

    repairPayload = setupRepair();
    onNavigate = tab => {
        windows[0].tabs[0].active = false;
        windows[0].tabs[1].active = true;
        tab.url = socialUrl;
    };
    result = await repair(repairPayload);
    assert.equal(result.urls_restored, false);
    assert.equal(windows[0].tabs[0].active, true,
        'repair finally restores the original active tab even after a failed navigation');

    repairPayload = setupRepair();
    failUrl = repairSaved.tabs[0].url;
    result = await repair(repairPayload);
    assert.equal(result.urls_restored, false);
    assert.match(result.url_errors.join(' '), /Navigation rejected/);
    assert.equal(session['manual:saved'].windowId, 50);

    repairPayload = setupRepair();
    windows[0].tabs[0].pendingUrl = repairSaved.tabs[0].url;
    repairPayload.expected_full_signature = context.browserWindowSummary(windows[0]).full_signature;
    result = await repair(repairPayload);
    assert.equal(result.urls_restored, false);
    assert.equal(navigations.length, 0, 'repair rejects conflicting committed and pending URLs');

    repairPayload = setupRepair();
    onNavigate = tab => {
        tab.pendingUrl = tab.url;
        tab.url = socialUrl;
        tab.status = 'loading';
    };
    onSleep = () => {
        const tab = windows[0].tabs[0];
        tab.url = tab.pendingUrl;
        tab.status = 'complete';
        delete tab.pendingUrl;
        onSleep = null;
    };
    result = await repair(repairPayload);
    assert.equal(result.urls_restored, true, 'repair waits for intended navigation to commit and complete');

    repairPayload = setupRepair(false);
    assert.equal((await repair(repairPayload)).urls_restored, false,
        'missing records cannot be silently adopted');
    assert.equal(navigations.length, 0);
    result = await repair({...repairPayload, adopt_known_window: true, window: repairSaved});
    assert.equal(result.urls_restored, true, 'explicit inspected adoption can recover a record after reload');
    assert.equal(session['manual:saved'].created, false);
    assert.equal(session['manual:saved'].windowId, 50);
    assert.equal(session['manual:saved'].windowState.tabs[0].url, repairSaved.tabs[0].url);

    repairPayload = setupRepair(false);
    session['old:claimed'] = {windowId: 50, created: false};
    result = await repair({...repairPayload, adopt_known_window: true, window: repairSaved});
    assert.equal(result.urls_restored, false, 'manual adoption cannot reuse another token claim, even older scopes');
    assert.equal(Object.hasOwn(session, 'manual:saved'), false);
    assert.equal(navigations.length, 0);

    repairPayload = setupRepair(false);
    windows[0].tabs[0].url = 'https://example.com/changed';
    result = await repair({...repairPayload, adopt_known_window: true, window: repairSaved});
    assert.equal(result.urls_restored, false);
    assert.equal(Object.hasOwn(session, 'manual:saved'), false,
        'failed adoption guards must not register a restore token');

    repairPayload = setupRepair();
    session['manual:saved'].windowId = 999;
    result = await repair({...repairPayload, adopt_known_window: true, window: repairSaved});
    assert.equal(result.urls_restored, false);
    assert.equal(session['manual:saved'].windowId, 999, 'adoption never replaces a conflicting record');

    reset();
    windows = [{...chromeWindow(40, ['blob:https://private.example/document']), type: 'popup'}];
    const popupIdentification = await context.identifyWindow({window_id: 40, token: 'popup-token'});
    assert.equal(popupIdentification.strategy, 'focused_popup');
    assert.equal(popupIdentification.marker_tab_id, null);
    assert.equal(windows[0].focused, true);
    const capabilities = (await context.dispatch({action: 'ping'})).capabilities;
    assert.ok(capabilities.includes('exact_url_restore'));
    assert.ok(capabilities.includes('exact_url_pending'));
    assert.ok(capabilities.includes('installed_build_activation'));
    assert.ok(capabilities.includes('lazy_tab_restore'));
    assert.ok(capabilities.includes('repair_restored_tabs'));
    assert.ok(capabilities.includes('exact_capture_identity'));
    assert.ok(capabilities.includes('runtime_build'));
    context.WSCTL_BUILD_REVISION = 'new-installed-release';
    assert.equal((await context.dispatch({action: 'ping'})).build.revision, 'test-release',
        'runtime identity remains the build loaded into this worker');
    const captured = await context.captureWindow(windows[0], 0);
    assert.equal(captured.runtime_window_id, 40);
    assert.equal(captured.id, 'window-1');

    reset();
    windows = [chromeWindow(41, ['https://example.com/a', 'https://example.com/b'])];
    windows[0].focused = false;
    const originalActive = windows[0].tabs[0].id;
    const marker = await context.identifyWindow({window_id: 41, token: 'capture-identity', focus: false});
    assert.equal(windows[0].focused, false, 'capture identification must preserve desktop focus');
    assert.equal((await context.dispatch({action: 'ping'})).active_identifications, 1);
    await assert.rejects(() => context.dispatch({action: 'capture'}), /identification/);
    await context.releaseWindowIdentification({...marker, focus: false});
    assert.equal(windows[0].tabs.length, 2);
    assert.equal(windows[0].tabs.find(tab => tab.active).id, originalActive);
    assert.equal(windows[0].focused, false);
    assert.equal(context.browserWindowSummary(windows[0]).tab_states[0].status, 'complete');
    const realRestoreWindow = context.restoreWindow;
    const finish = [];
    context.restoreWindow = () => new Promise(resolve => finish.push(resolve));
    const firstMutation = context.dispatch({action: 'restore_window', payload: {}});
    const secondMutation = context.dispatch({action: 'restore_window', payload: {}});
    let mutationStatus = await context.dispatch({action: 'ping'});
    assert.ok(mutationStatus.capabilities.includes('native_mutation_status'));
    assert.equal(mutationStatus.active_mutations, 2, 'queued and running native mutations both count');
    await assert.rejects(() => context.dispatch({action: 'capture'}), /mutations/);
    finish.shift()({});
    await firstMutation;
    mutationStatus = await context.dispatch({action: 'ping'});
    assert.equal(mutationStatus.active_mutations, 1);
    finish.shift()({});
    await secondMutation;
    assert.equal((await context.dispatch({action: 'ping'})).active_mutations, 0);
    context.restoreWindow = realRestoreWindow;
    await testNativeGroupReuseOnly();
    await testOriginalWindowRecovery();
    await testLateUrlCompletion();
    await testNativeReconnect();
    await testRestoreClaimRetrySafety();
    await testNativeIdentityAfterBrowserRestart();
    await testInstalledBuildActivation();
    await testIdentificationLeases();
    console.log('Chrome extension protocol tests passed');
}

main().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
