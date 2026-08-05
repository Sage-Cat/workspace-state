const NATIVE_HOST = 'org.sagecat.workspace_state';
const DEFAULT_CONFIG = {
    profile: 'Default',
    profileDirectory: 'Default',
    appId: 'google-chrome',
};

let nativePort = null;
let reconnectTimer = null;

async function configuration() {
    return {...DEFAULT_CONFIG, ...await chrome.storage.local.get(DEFAULT_CONFIG)};
}

function normalizeWindowState(state) {
    return state === 'locked-fullscreen' ? 'fullscreen' : state;
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
    const createData = {
        url: 'about:blank',
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
        const existingId = stored[restoreToken];
        if (existingId != null) {
            try {
                await chrome.windows.get(existingId);
                return {window_id: existingId, warnings: [], reused: true};
            } catch (_error) {
                await chrome.storage.session.remove(restoreToken);
            }
        }
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
        if (restoreToken)
            await chrome.storage.session.set({[restoreToken]: createdWindow.id});
        return {window_id: createdWindow.id, warnings};
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

async function closeRestoredWindow(payload) {
    try {
        await chrome.windows.remove(payload.window_id);
    } catch (_error) {
        // Retry cleanup is idempotent.
    }
    if (payload.restore_token)
        await chrome.storage.session.remove(payload.restore_token);
    return {closed: true};
}

async function dispatch(message) {
    switch (message.action) {
    case 'ping': {
        const config = await configuration();
        return {profile: config.profile};
    }
    case 'capture':
        return captureBrowser();
    case 'restore_window':
        return restoreWindow(message.payload ?? {});
    case 'close_restored_window':
        return closeRestoredWindow(message.payload ?? {});
    default:
        throw new Error(`Unknown wsctl action: ${message.action}`);
    }
}

async function onNativeMessage(message) {
    if (message.type === 'hello')
        return;
    try {
        const result = await dispatch(message);
        nativePort?.postMessage({id: message.id, ok: true, result});
    } catch (error) {
        nativePort?.postMessage({id: message.id, ok: false, error: error.message});
    }
}

async function connectNativeHost() {
    if (nativePort)
        return;
    const config = await configuration();
    try {
        nativePort = chrome.runtime.connectNative(NATIVE_HOST);
        nativePort.onMessage.addListener(onNativeMessage);
        nativePort.onDisconnect.addListener(() => {
            void chrome.runtime.lastError;
            nativePort = null;
            clearTimeout(reconnectTimer);
            reconnectTimer = setTimeout(connectNativeHost, 5000);
        });
        nativePort.postMessage({type: 'hello', profile: config.profile});
    } catch (_error) {
        nativePort = null;
        clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connectNativeHost, 5000);
    }
}

chrome.runtime.onInstalled.addListener(connectNativeHost);
chrome.runtime.onStartup.addListener(connectNativeHost);
chrome.storage.onChanged.addListener(() => {
    nativePort?.disconnect();
});
connectNativeHost();
