const NATIVE_HOST = 'org.sagecat.workspace_state';
const PROTOCOL_VERSION = 2;
const IDENTIFY_PAGE = chrome.runtime.getURL('identify.html');
const DEFAULT_CONFIG = {
    profile: 'Default',
    profileDirectory: 'Default',
    appId: 'google-chrome',
};

let nativePort = null;
let nativeConnectionPending = false;
let reconnectTimer = null;
let restoreQueue = Promise.resolve();

async function configuration() {
    const stored = await chrome.storage.local.get(null);
    return {
        ...DEFAULT_CONFIG,
        ...stored,
        profileConfigured: Boolean(stored.profileConfigured),
    };
}

async function profileEmail() {
    try {
        const identity = await chrome.identity.getProfileUserInfo({accountStatus: 'ANY'});
        return identity.email ?? '';
    } catch (_error) {
        return '';
    }
}

function normalizeWindowState(state) {
    return state === 'locked-fullscreen' ? 'fullscreen' : state;
}

function tabSiteKey(tab) {
    const value = tab.pendingUrl ?? tab.url ?? 'chrome://newtab/';
    try {
        const url = new URL(value);
        if (url.host)
            return `${url.protocol}//${url.host}`;
        return `${url.protocol}${url.pathname}`;
    } catch (_error) {
        return value;
    }
}

function windowSiteSignature(window) {
    const tabs = [...(window.tabs ?? [])].sort(
        (left, right) => (left.index ?? 0) - (right.index ?? 0),
    );
    return JSON.stringify({
        type: window.type === 'popup' ? 'popup' : 'normal',
        incognito: Boolean(window.incognito),
        tabs: tabs.map(tab => [tabSiteKey(tab), Boolean(tab.pinned)]),
    });
}

function windowFullSignature(window) {
    const tabs = [...(window.tabs ?? [])].sort(
        (left, right) => (left.index ?? 0) - (right.index ?? 0),
    );
    return JSON.stringify({
        type: window.type === 'popup' ? 'popup' : 'normal',
        incognito: Boolean(window.incognito),
        tabs: tabs.map(tab => [
            tab.pendingUrl ?? tab.url ?? 'chrome://newtab/',
            Boolean(tab.pinned),
        ]),
    });
}

function restoreRecord(value) {
    if (Number.isInteger(value))
        return {windowId: value, created: true};
    if (value && Number.isInteger(value.windowId)) {
        return {
            windowId: value.windowId,
            created: Boolean(value.created),
        };
    }
    return null;
}

async function rememberRestoredWindow(restoreToken, windowId, created) {
    if (restoreToken) {
        await chrome.storage.session.set({
            [restoreToken]: {windowId, created: Boolean(created)},
        });
    }
}

async function claimedWindowIds(restoreToken) {
    const stored = await chrome.storage.session.get(null);
    const scope = String(restoreToken ?? '').split(':', 1)[0];
    return new Set(Object.entries(stored)
        .filter(([key]) => key !== restoreToken && (!scope || key.startsWith(`${scope}:`)))
        .map(([_key, value]) => restoreRecord(value)?.windowId)
        .filter(Number.isInteger));
}

async function matchingOpenWindow(windowState, restoreToken) {
    const signature = windowSiteSignature(windowState);
    const claimed = await claimedWindowIds(restoreToken);
    const windows = await chrome.windows.getAll({
        populate: true,
        windowTypes: ['normal', 'popup'],
    });
    const available = windows.filter(window => !claimed.has(window.id));
    const exactSignature = windowFullSignature(windowState);
    return available.find(window => windowFullSignature(window) === exactSignature) ??
        available.find(window => windowSiteSignature(window) === signature) ?? null;
}

function browserWindowSummary(window) {
    const activeTab = (window.tabs ?? []).find(tab => tab.active);
    return {
        id: window.id,
        signature: windowSiteSignature(window),
        full_signature: windowFullSignature(window),
        tabs: (window.tabs ?? []).map(tab => tab.pendingUrl ?? tab.url ?? 'chrome://newtab/'),
        active_title: activeTab?.title ?? '',
        focused: Boolean(window.focused),
        state: normalizeWindowState(window.state),
        bounds: {
            left: window.left ?? 0,
            top: window.top ?? 0,
            width: window.width ?? 0,
            height: window.height ?? 0,
        },
    };
}

async function listBrowserWindows() {
    const windows = await chrome.windows.getAll({
        populate: true,
        windowTypes: ['normal', 'popup'],
    });
    return windows.map(browserWindowSummary);
}

async function captureWindow(window, ordinal) {
    const groups = await chrome.tabGroups.query({windowId: window.id});
    groups.sort((left, right) => left.id - right.id);
    const groupIds = new Map(groups.map((group, index) => [group.id, `group-${index + 1}`]));
    const tabs = [...(window.tabs ?? [])].sort((left, right) => left.index - right.index);
    return {
        id: `window-${ordinal + 1}`,
        type: window.type,
        state: normalizeWindowState(window.state),
        focused: window.focused,
        incognito: window.incognito,
        always_on_top: window.alwaysOnTop,
        bounds: {
            left: window.left ?? 0,
            top: window.top ?? 0,
            width: window.width ?? 1000,
            height: window.height ?? 700,
        },
        groups: groups.map((group, index) => ({
            id: `group-${index + 1}`,
            title: group.title ?? '',
            color: group.color,
            collapsed: group.collapsed,
        })),
        tabs: tabs.map(tab => ({
            url: tab.pendingUrl ?? tab.url ?? 'chrome://newtab/',
            title: tab.title ?? '',
            pinned: Boolean(tab.pinned),
            active: Boolean(tab.active),
            group: groupIds.get(tab.groupId) ?? null,
        })),
    };
}

async function captureBrowser() {
    const config = await configuration();
    const windows = await chrome.windows.getAll({
        populate: true,
        windowTypes: ['normal', 'popup'],
    });
    windows.sort((left, right) => left.id - right.id);
    return {
        profile: config.profile,
        profile_directory: config.profileDirectory,
        app_id: config.appId,
        windows: await Promise.all(windows.map(captureWindow)),
    };
}

async function setTabUrl(tabId, url, warnings) {
    try {
        return await chrome.tabs.update(tabId, {url}) ?? chrome.tabs.get(tabId);
    } catch (error) {
        warnings.push(`Could not restore ${url}: ${error.message}`);
        return chrome.tabs.get(tabId);
    }
}

async function pinTab(tab, warnings) {
    try {
        return await chrome.tabs.update(tab.id, {pinned: true}) ?? tab;
    } catch (error) {
        warnings.push(`Could not pin ${tab.url ?? 'tab'}: ${error.message}`);
        return tab;
    }
}

async function createTab(windowId, tabState, warnings) {
    let tab;
    try {
        tab = await chrome.tabs.create({
            windowId,
            url: tabState.url,
            active: false,
        });
    } catch (error) {
        warnings.push(`Could not restore ${tabState.url}: ${error.message}`);
        tab = await chrome.tabs.create({windowId, url: 'chrome://newtab/', active: false});
    }
    return tabState.pinned ? pinTab(tab, warnings) : tab;
}

async function restoreGroups(windowState, restoredTabs, warnings) {
    for (const groupState of windowState.groups ?? []) {
        const tabIds = (windowState.tabs ?? [])
            .map((tab, index) => tab.group === groupState.id && !tab.pinned ? restoredTabs[index]?.id : null)
            .filter(id => id != null);
        if (!tabIds.length)
            continue;
        try {
            const groupId = await chrome.tabs.group({tabIds});
            await chrome.tabGroups.update(groupId, {
                title: groupState.title,
                color: groupState.color,
                collapsed: Boolean(groupState.collapsed),
            });
        } catch (error) {
            warnings.push(`Could not restore tab group ${groupState.title || groupState.id}: ${error.message}`);
        }
    }
}

async function restoreWindow(payload) {
    const windowState = payload.window;
    const savedTabs = windowState.tabs ?? [];
    const warnings = [];
    const creationToken = String(payload.creation_token ?? '');
    const createData = {
        url: creationToken
            ? `${IDENTIFY_PAGE}?mode=create&token=${encodeURIComponent(creationToken)}`
            : 'about:blank',
        focused: true,
        incognito: Boolean(windowState.incognito),
        type: windowState.type === 'popup' ? 'popup' : 'normal',
        state: 'normal',
    };
    if (!payload.place_expected) {
        const bounds = windowState.geometry ?? windowState.bounds;
        if (bounds) {
            createData.left = bounds.x ?? bounds.left;
            createData.top = bounds.y ?? bounds.top;
            createData.width = bounds.width;
            createData.height = bounds.height;
        }
    }
    const restoreToken = payload.restore_token;
    if (restoreToken) {
        const stored = await chrome.storage.session.get(restoreToken);
        const record = restoreRecord(stored[restoreToken]);
        if (record) {
            try {
                await chrome.windows.get(record.windowId);
                return {
                    window_id: record.windowId,
                    warnings: [],
                    reused: true,
                    created: record.created,
                };
            } catch (_error) {
                await chrome.storage.session.remove(restoreToken);
            }
        }
    }

    const existingWindow = await matchingOpenWindow(windowState, restoreToken);
    if (existingWindow) {
        await rememberRestoredWindow(restoreToken, existingWindow.id, false);
        return {
            window_id: existingWindow.id,
            warnings: [],
            reused: true,
            created: false,
        };
    }

    let createdWindow = null;
    try {
        createdWindow = await chrome.windows.create(createData);
        if (!createdWindow?.id)
            throw new Error('Chrome did not return the newly created window');
        const populatedWindow = await chrome.windows.get(createdWindow.id, {populate: true});
        const restoredTabs = [];
        const placeholder = populatedWindow.tabs?.[0];
        if (!placeholder?.id)
            throw new Error('Chrome did not create the initial tab');
        if (creationToken) {
            for (let attempt = 0; attempt < 50; attempt += 1) {
                const current = await chrome.tabs.get(placeholder.id);
                if ((current.title ?? '').includes(creationToken))
                    break;
                await new Promise(resolve => setTimeout(resolve, 20));
            }
            // Give Mutter a chance to observe the unique creation title before
            // the placeholder navigates to the first saved URL. Exact-ID
            // placement remains the fallback if the expectation is still pending.
            await new Promise(resolve => setTimeout(resolve, 100));
        }

        if (savedTabs.length) {
            const first = savedTabs[0];
            let restored = await setTabUrl(placeholder.id, first.url, warnings);
            if (first.pinned)
                restored = await pinTab(restored, warnings);
            restoredTabs.push(restored);
            for (const tabState of savedTabs.slice(1))
                restoredTabs.push(await createTab(createdWindow.id, tabState, warnings));
        } else {
            restoredTabs.push(placeholder);
        }

        await restoreGroups(windowState, restoredTabs, warnings);
        const activeIndex = savedTabs.findIndex(tab => tab.active);
        if (activeIndex >= 0 && restoredTabs[activeIndex]) {
            try {
                await chrome.tabs.update(restoredTabs[activeIndex].id, {active: true});
            } catch (error) {
                warnings.push(`Could not activate saved tab: ${error.message}`);
            }
        }
        if (!payload.place_expected && windowState.state && windowState.state !== 'normal') {
            try {
                await chrome.windows.update(createdWindow.id, {state: normalizeWindowState(windowState.state)});
            } catch (error) {
                warnings.push(`Could not restore window state: ${error.message}`);
            }
        }
        await rememberRestoredWindow(restoreToken, createdWindow.id, true);
        return {window_id: createdWindow.id, warnings, created: true};
    } catch (error) {
        if (createdWindow?.id) {
            try {
                await chrome.windows.remove(createdWindow.id);
            } catch (_closeError) {
                // The window may already have closed; preserve the original failure.
            }
        }
        if (restoreToken)
            await chrome.storage.session.remove(restoreToken);
        throw error;
    }
}

async function focusWindow(payload) {
    const windowId = Number(payload.window_id);
    if (!Number.isInteger(windowId))
        throw new Error('window_id is required');
    await chrome.windows.update(windowId, {focused: true});
    const window = await chrome.windows.get(windowId, {populate: true});
    return browserWindowSummary(window);
}

async function removeIdentificationTabs(windowId) {
    const tabs = await chrome.tabs.query({windowId});
    const staleIds = tabs
        .filter(tab => (tab.pendingUrl ?? tab.url ?? '').startsWith(IDENTIFY_PAGE))
        .map(tab => tab.id)
        .filter(Number.isInteger);
    if (staleIds.length)
        await chrome.tabs.remove(staleIds);
}

async function identifyWindow(payload) {
    const windowId = Number(payload.window_id);
    const token = String(payload.token ?? '');
    if (!Number.isInteger(windowId) || !token)
        throw new Error('window_id and token are required');
    const before = await chrome.windows.get(windowId, {populate: true});
    const previousActiveTab = (before.tabs ?? []).find(tab => tab.active);
    if (before.type === 'popup') {
        await chrome.windows.update(windowId, {focused: true});
        return {
            window_id: windowId,
            marker_tab_id: null,
            previous_active_tab_id: null,
            token,
            strategy: 'focused_popup',
            active_title: previousActiveTab?.title ?? '',
        };
    }
    await removeIdentificationTabs(windowId);
    const markerTab = await chrome.tabs.create({
        windowId,
        url: `${IDENTIFY_PAGE}?token=${encodeURIComponent(token)}`,
        active: true,
    });
    if (!Number.isInteger(markerTab.id))
        throw new Error('Chrome did not create the identification tab');
    if (markerTab.windowId !== windowId) {
        await chrome.tabs.remove(markerTab.id).catch(() => {});
        if (Number.isInteger(previousActiveTab?.id))
            await chrome.tabs.update(previousActiveTab.id, {active: true}).catch(() => {});
        throw new Error('Chrome created the identification tab in a different window');
    }
    await chrome.windows.update(windowId, {focused: true});
    for (let attempt = 0; attempt < 50; attempt += 1) {
        const current = await chrome.tabs.get(markerTab.id);
        if ((current.title ?? '').includes(token)) {
            return {
                window_id: windowId,
                marker_tab_id: markerTab.id,
                previous_active_tab_id: previousActiveTab?.id ?? null,
                token,
            };
        }
        await new Promise(resolve => setTimeout(resolve, 20));
    }
    await chrome.tabs.remove(markerTab.id).catch(() => {});
    if (Number.isInteger(previousActiveTab?.id))
        await chrome.tabs.update(previousActiveTab.id, {active: true}).catch(() => {});
    throw new Error('Chrome identification tab did not become ready');
}

async function releaseWindowIdentification(payload) {
    const markerTabId = payload.marker_tab_id;
    const previousActiveTabId = payload.previous_active_tab_id;
    if (Number.isInteger(markerTabId)) {
        try {
            const tab = await chrome.tabs.get(markerTabId);
            const url = tab.pendingUrl ?? tab.url ?? '';
            if (url.startsWith(IDENTIFY_PAGE))
                await chrome.tabs.remove(markerTabId);
        } catch (_error) {
            // Cleanup is idempotent when an interrupted attempt already removed it.
        }
    }
    if (Number.isInteger(previousActiveTabId)) {
        try {
            await chrome.tabs.update(previousActiveTabId, {active: true});
        } catch (_error) {
            // The formerly active tab may have closed while restoration ran.
        }
    }
    if (payload.focus && Number.isInteger(payload.window_id))
        await chrome.windows.update(payload.window_id, {focused: true});
    return {released: true};
}

async function closeRestoredWindow(payload) {
    let created = Boolean(payload.created);
    if (payload.restore_token) {
        const stored = await chrome.storage.session.get(payload.restore_token);
        const record = restoreRecord(stored[payload.restore_token]);
        if (record && record.windowId === payload.window_id)
            created = record.created;
    }
    if (created) {
        try {
            await chrome.windows.remove(payload.window_id);
        } catch (_error) {
            // Retry cleanup is idempotent.
        }
    }
    if (payload.restore_token)
        await chrome.storage.session.remove(payload.restore_token);
    return {closed: created, released: !created};
}

async function restoredWindowStatus(payload) {
    const restoreToken = payload.restore_token;
    if (!restoreToken)
        throw new Error('restore_token is required');
    const stored = await chrome.storage.session.get(restoreToken);
    const record = restoreRecord(stored[restoreToken]);
    if (!record)
        return {exists: false};
    try {
        await chrome.windows.get(record.windowId);
        return {
            exists: true,
            window_id: record.windowId,
            created: record.created,
        };
    } catch (_error) {
        await chrome.storage.session.remove(restoreToken);
        return {exists: false};
    }
}

async function dispatch(message) {
    switch (message.action) {
    case 'ping': {
        const config = await configuration();
        return {
            profile: config.profile,
            protocol_version: PROTOCOL_VERSION,
            capabilities: [
                'capture',
                'list_windows',
                'restore_window',
                'restore_status',
                'identify_window',
                'focus_window',
                'release_window_identification',
                'close_restored_window',
                'scoped_creation_marker',
            ],
        };
    }
    case 'capture':
        return captureBrowser();
    case 'list_windows':
        return listBrowserWindows();
    case 'restore_window': {
        const task = restoreQueue.then(() => restoreWindow(message.payload ?? {}));
        restoreQueue = task.catch(() => {});
        return task;
    }
    case 'restore_status':
        return restoredWindowStatus(message.payload ?? {});
    case 'identify_window':
        return identifyWindow(message.payload ?? {});
    case 'focus_window':
        return focusWindow(message.payload ?? {});
    case 'release_window_identification':
        return releaseWindowIdentification(message.payload ?? {});
    case 'close_restored_window':
        return closeRestoredWindow(message.payload ?? {});
    default:
        throw new Error(`Unknown wsctl action: ${message.action}`);
    }
}

async function onNativeMessage(message) {
    if (message.type === 'hello') {
        const config = await configuration();
        if (message.ok && !config.profileConfigured && message.profileDirectory) {
            await chrome.storage.local.set({
                profile: message.profile ?? message.profileDirectory,
                profileDirectory: message.profileDirectory,
            });
        }
        return;
    }
    try {
        const result = await dispatch(message);
        nativePort?.postMessage({id: message.id, ok: true, result});
    } catch (error) {
        nativePort?.postMessage({id: message.id, ok: false, error: error.message});
    }
}

async function connectNativeHost() {
    if (nativePort || nativeConnectionPending)
        return;
    nativeConnectionPending = true;
    try {
        const config = await configuration();
        const port = chrome.runtime.connectNative(NATIVE_HOST);
        nativePort = port;
        port.onMessage.addListener(onNativeMessage);
        port.onDisconnect.addListener(() => {
            void chrome.runtime.lastError;
            if (nativePort !== port)
                return;
            nativePort = null;
            clearTimeout(reconnectTimer);
            reconnectTimer = setTimeout(connectNativeHost, 5000);
        });
        port.postMessage({
            type: 'hello',
            profile: config.profile,
            profileDirectory: config.profileDirectory,
            profileConfigured: config.profileConfigured,
            profileEmail: await profileEmail(),
        });
    } catch (_error) {
        nativePort = null;
        clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connectNativeHost, 5000);
    } finally {
        nativeConnectionPending = false;
    }
}

chrome.runtime.onInstalled.addListener(connectNativeHost);
chrome.runtime.onStartup.addListener(connectNativeHost);
chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName !== 'local')
        return;
    const connectionKeys = ['profile', 'profileDirectory', 'profileConfigured'];
    if (connectionKeys.some(key => Object.hasOwn(changes, key)))
        nativePort?.disconnect();
});
connectNativeHost();
