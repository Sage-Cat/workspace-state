'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const session = {};
let windows = [];
const removedWindows = [];
const chrome = {
    runtime: {
        getURL: name => `chrome-extension://test/${name}`,
    },
    storage: {
        local: {get: async () => ({})},
        session: {
            get: async key => {
                if (key === null)
                    return {...session};
                return Object.hasOwn(session, key) ? {[key]: session[key]} : {};
            },
            set: async values => Object.assign(session, values),
            remove: async key => delete session[key],
        },
    },
    windows: {
        getAll: async () => windows,
        get: async id => windows.find(window => window.id === id),
        update: async (id, changes) => {
            const window = windows.find(item => item.id === id);
            Object.assign(window, changes);
            return window;
        },
        remove: async id => removedWindows.push(id),
    },
};

const workerPath = path.join(__dirname, '..', 'chrome-extension', 'service-worker.js');
const workerSource = fs.readFileSync(workerPath, 'utf8');
const definitions = workerSource.split('\nchrome.runtime.onInstalled.addListener')[0];
const context = vm.createContext({chrome, console, URL, setTimeout, clearTimeout});
vm.runInContext(definitions, context, {filename: workerPath});

function chromeWindow(id, urls) {
    return {
        id,
        type: 'normal',
        incognito: false,
        tabs: urls.map((url, index) => ({url, index, pinned: false})),
    };
}

async function main() {
    const saved = chromeWindow('saved', ['https://example.com/exact']);
    windows = [
        chromeWindow(1, ['https://example.com/different']),
        chromeWindow(2, ['https://example.com/exact']),
    ];
    let match = await context.matchingOpenWindow(saved, 'current-token');
    assert.equal(match.id, 2, 'exact URL fingerprint must outrank site fallback');

    session['current:another-token'] = {windowId: 2, created: false};
    match = await context.matchingOpenWindow(saved, 'current:this-token');
    assert.equal(match.id, 1, 'a window claimed by another saved item must be skipped');

    session['older:window-token'] = {windowId: 1, created: false};
    match = await context.matchingOpenWindow(saved, 'newer:this-token');
    assert.equal(match.id, 2, 'claims from an older restore transaction must not hide live windows');

    for (const key of Object.keys(session))
        delete session[key];
    windows = [
        chromeWindow(21, ['https://same.example/window']),
        chromeWindow(22, ['https://same.example/window']),
    ];
    const concurrentSaved = chromeWindow('saved', ['https://same.example/window']);
    const claims = await Promise.all([
        context.dispatch({
            action: 'restore_window',
            payload: {window: concurrentSaved, restore_token: 'same:saved-one'},
        }),
        context.dispatch({
            action: 'restore_window',
            payload: {window: concurrentSaved, restore_token: 'same:saved-two'},
        }),
    ]);
    assert.deepEqual(
        claims.map(result => result.window_id),
        [21, 22],
        'concurrent restore requests must claim different live windows',
    );

    session.reused = {windowId: 7, created: false};
    await context.closeRestoredWindow({
        window_id: 7,
        restore_token: 'reused',
        created: true,
    });
    assert.deepEqual(removedWindows, [], 'a Chrome-restored window must never be closed');
    assert.equal(Object.hasOwn(session, 'reused'), false);

    session.created = {windowId: 8, created: true};
    await context.closeRestoredWindow({
        window_id: 8,
        restore_token: 'created',
        created: true,
    });
    assert.deepEqual(removedWindows, [8], 'wsctl may clean up only its own new window');

    assert.deepEqual(
        {...context.restoreRecord(9)},
        {windowId: 9, created: true},
        'legacy restore records remain compatible',
    );

    windows = [{
        ...chromeWindow(30, ['blob:https://private.example/document']),
        type: 'popup',
        focused: false,
        tabs: [{
            id: 301,
            windowId: 30,
            url: 'blob:https://private.example/document',
            title: 'Private document',
            index: 0,
            pinned: false,
            active: true,
        }],
    }];
    const popupIdentification = await context.identifyWindow({
        window_id: 30,
        token: 'popup-token',
    });
    assert.equal(popupIdentification.strategy, 'focused_popup');
    assert.equal(popupIdentification.marker_tab_id, null);
    assert.equal(windows[0].focused, true);
    console.log('Chrome extension protocol tests passed');
}

main().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
