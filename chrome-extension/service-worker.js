// Capture build identity once when this worker loads; never read the install pointer.
if (globalThis.WSCTL_BUILD_REVISION === undefined && typeof importScripts === 'function') {
    try { importScripts('buildInfo.js'); } catch (_) { /* explicit development checkout */ }
}
const BUILD_REVISION = globalThis.WSCTL_BUILD_REVISION ?? 'development';
const NATIVE_HOST = 'org.sagecat.workspace_state';
const PROTOCOL_VERSION = 2;
const IDENTIFY_PAGE = chrome.runtime.getURL('identify.html');
const URL_CHECK_INTERVAL_MS = 250;
const URL_CHECK_ATTEMPTS = 40;
const WINDOW_CLOSE_CHECK_INTERVAL_MS = 100;
const WINDOW_CLOSE_CHECK_ATTEMPTS = 20;
const DEFAULT_CONFIG = {
    profile: 'Default',
    profileDirectory: 'Default',
    appId: 'google-chrome',
};

let nativePort = null;
let nativeConnectionPending = false;
let nativeConnectionGeneration = 0;
let reconnectTimer = null;
let restoreQueue = Promise.resolve();
let activeNativeMutations = 0;
let activationPending = false;
const MUTATING_ACTIONS = new Set([
    'restore_window', 'repair_restored_tabs', 'identify_window', 'focus_window',
    'release_window_identification', 'close_restored_window', 'recover_original_window',
]);

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

function windowFullSignature(window, includePending = true) {
    const tabs = [...(window.tabs ?? [])].sort(
        (left, right) => (left.index ?? 0) - (right.index ?? 0),
    );
    return JSON.stringify({
        type: window.type === 'popup' ? 'popup' : 'normal',
        incognito: Boolean(window.incognito),
        tabs: tabs.map(tab => [
            (includePending ? tab.pendingUrl : null) ?? tab.url ?? 'chrome://newtab/',
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
            ...(value.windowState ? {windowState: value.windowState} : {}),
        };
    }
    return null;
}

async function rememberRestoredWindow(restoreToken, windowId, created, windowState) {
    if (restoreToken) {
        await chrome.storage.session.set({
            [restoreToken]: {windowId, created: Boolean(created), windowState},
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
    const claimed = await claimedWindowIds(restoreToken);
    const windows = await chrome.windows.getAll({
        populate: true,
        windowTypes: ['normal', 'popup'],
    });
    const available = windows.filter(window => !claimed.has(window.id));
    const exactSignature = windowFullSignature(windowState);
    const matches = available.filter(window => windowFullSignature(window) === exactSignature);
    if (windowState.groups?.length) {
        for (const window of matches) {
            const groups = await verifyRestoredGroups(window.id, windowState);
            if (!groups.group_warnings.length)
                return window;
        }
    }
    return matches[0] ?? null;
}

function restoredUrlErrors(window, windowState) {
    if (!windowState)
        return ['Cannot verify restored URLs without the saved window state'];
    const tabs = [...(window.tabs ?? [])].sort((left, right) => left.index - right.index);
    const savedTabs = windowState.tabs ?? [];
    const errors = [];
    if (window.type !== (windowState.type === 'popup' ? 'popup' : 'normal') ||
        Boolean(window.incognito) !== Boolean(windowState.incognito))
        errors.push('Restored window type or incognito mode differs from the saved window');
    if (tabs.length !== savedTabs.length)
        errors.push(`Restored window has ${tabs.length} tabs; expected ${savedTabs.length}`);
    savedTabs.forEach((saved, index) => {
        const tab = tabs[index];
        if (!tab) {
            errors.push(`Tab ${index + 1} is missing: expected ${saved.url}`);
            return;
        }
        if (tab.discarded) {
            errors.push(`Tab ${index + 1} is discarded; cannot verify loaded URL ${saved.url}`);
        } else if (tab.pendingUrl || tab.status !== 'complete') {
            errors.push(`Tab ${index + 1} has not finished loading ${saved.url}`);
        }
        // pendingUrl is an intention, not evidence that navigation succeeded.
        if (tab.url !== saved.url)
            errors.push(`Tab ${index + 1} URL mismatch: expected ${saved.url}; found ${tab.url ?? '(missing URL)'}`);
        if (Boolean(tab.pinned) !== Boolean(saved.pinned))
            errors.push(`Tab ${index + 1} pinned state differs from the saved tab`);
    });
    return errors;
}

function hasExactCommittedUrls(window, windowState) {
    return windowState &&
        (window.tabs ?? []).every(tab => typeof tab.url === 'string') &&
        windowFullSignature(window, false) === windowFullSignature(windowState) &&
        windowFullSignature(window) === windowFullSignature(windowState);
}

function restoredUrlsPending(window, windowState) {
    // A matching pending URL proves only that the saved navigation is still
    // underway. Keep observing its existing window without creating or loading
    // anything; redirects, missing tabs and conflicting claims remain failures.
    return Boolean(windowState && window &&
        windowFullSignature(window) === windowFullSignature(windowState) &&
        (window.tabs ?? []).every(tab => !isLazyTab(tab) &&
            (tab.status === 'complete' || tab.status === 'loading')) &&
        (window.tabs ?? []).some(tab => tab.pendingUrl || tab.status === 'loading'));
}

function isLazyTab(tab) {
    return tab.discarded || tab.status === 'unloaded';
}

async function activateLazyTabs(window, windowState) {
    const tabs = [...(window.tabs ?? [])].sort((left, right) => left.index - right.index);
    const tabIds = tabs.map(tab => tab.id);
    const previousActiveId = tabs.find(tab => tab.active)?.id;
    const activatedIds = new Set();
    const errors = [];
    if (!hasExactCommittedUrls(window, windowState) ||
        !tabIds.every(Number.isInteger) || !Number.isInteger(previousActiveId))
        return {activatedIds, errors};
    try {
        for (const tab of tabs.filter(isLazyTab)) {
            const current = await chrome.windows.get(window.id, {populate: true});
            const currentTabs = [...(current.tabs ?? [])].sort((left, right) => left.index - right.index);
            if (!hasExactCommittedUrls(current, windowState) ||
                JSON.stringify(currentTabs.map(item => item.id)) !== JSON.stringify(tabIds))
                throw new Error('Window tabs changed while loading lazy saved tabs');
            if (!isLazyTab(currentTabs.find(item => item.id === tab.id)))
                continue;
            // Activating a discarded/unloaded tab loads its existing URL.
            // Never rewrite URLs or reload tabs that are already in memory.
            activatedIds.add(tab.id);
            await chrome.tabs.update(tab.id, {active: true});
        }
    } catch (error) {
        errors.push(`Could not load lazy saved tabs: ${error.message}`);
    } finally {
        if (activatedIds.size) {
            try {
                const previous = await chrome.tabs.get(previousActiveId);
                if (previous.windowId !== window.id)
                    throw new Error('Previously active tab moved to another window');
                if (!previous.active)
                    await chrome.tabs.update(previousActiveId, {active: true});
            } catch (error) {
                errors.push(`Could not restore previously active tab: ${error.message}`);
            }
        }
    }
    return {activatedIds, errors};
}

async function verifyRestoredUrls(windowId, windowState, wait = true) {
    let errors = [];
    let window;
    let completeChecks = 0;
    let activationAttempted = false;
    let activatedIds = new Set();
    const attempts = wait ? URL_CHECK_ATTEMPTS : 1;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
        try {
            window = await chrome.windows.get(windowId, {populate: true});
        } catch (error) {
            return {urls_restored: false, url_errors: [`Could not verify restored window: ${error.message}`]};
        }
        if (wait && !activationAttempted && hasExactCommittedUrls(window, windowState) &&
            (window.tabs ?? []).some(isLazyTab)) {
            activationAttempted = true;
            const activation = await activateLazyTabs(window, windowState);
            activatedIds = activation.activatedIds;
            if (activation.errors.length)
                return {urls_restored: false, url_errors: activation.errors};
            try {
                window = await chrome.windows.get(windowId, {populate: true});
            } catch (error) {
                return {urls_restored: false, url_errors: [`Could not verify restored window: ${error.message}`]};
            }
        }
        errors = restoredUrlErrors(window, windowState);
        completeChecks = errors.length ? 0 : completeChecks + 1;
        // Observe completion twice so pending URLs and immediate post-load
        // redirects cannot count as successfully restored saved pages.
        if (!errors.length && (!wait || completeChecks >= 2))
            return {urls_restored: true, url_errors: []};
        const loading = (window.tabs ?? []).some(tab =>
            tab.pendingUrl || tab.status === 'loading' || (isLazyTab(tab) && activatedIds.has(tab.id)));
        if (errors.length && !loading)
            break;
        if (attempt + 1 < attempts)
            await new Promise(resolve => setTimeout(resolve, URL_CHECK_INTERVAL_MS));
    }
    return {
        urls_restored: false,
        urls_pending: restoredUrlsPending(window, windowState),
        url_errors: errors.length ? errors : ['Restored URLs did not remain complete within the verification timeout'],
    };
}

async function restoredWindowResult(windowId, windowState, {created, reused = false, warnings = []}) {
    const verification = await verifyRestoredUrls(windowId, windowState);
    const groups = await verifyRestoredGroups(windowId, windowState);
    return {
        window_id: windowId,
        created,
        reused,
        ...verification,
        ...groups,
        warnings: [...warnings, ...verification.url_errors, ...groups.group_warnings],
    };
}

function browserWindowSummary(window) {
    const activeTab = (window.tabs ?? []).find(tab => tab.active);
    return {
        id: window.id,
        signature: windowSiteSignature(window),
        full_signature: windowFullSignature(window),
        urls_loaded: (window.tabs ?? []).every(tab =>
            tab.status === 'complete' && !tab.pendingUrl && !tab.discarded),
        tabs: (window.tabs ?? []).map(tab => tab.pendingUrl ?? tab.url ?? 'chrome://newtab/'),
        tab_states: (window.tabs ?? []).map(tab => ({
            status: tab.status, discarded: Boolean(tab.discarded), pending: Boolean(tab.pendingUrl),
        })),
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
        runtime_window_id: window.id,
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
    if (activeNativeMutations)
        throw new Error('Chrome native mutations are still active; checkpoint preserved');
    const config = await configuration();
    const windows = await chrome.windows.getAll({
        populate: true,
        windowTypes: ['normal', 'popup'],
    });
    if (activeNativeMutations || windows.some(window => (window.tabs ?? []).some(tab =>
        (tab.pendingUrl ?? tab.url ?? '').startsWith(IDENTIFY_PAGE))))
        throw new Error('Chrome identification or native mutations are still active; checkpoint preserved');
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

async function verifyRestoredGroups(windowId, windowState) {
    const savedGroups = windowState?.groups ?? [];
    const result = {groups_reused: 0, group_warnings: []};
    if (!savedGroups.length)
        return result;
    const window = await chrome.windows.get(windowId, {populate: true});
    const tabs = [...(window.tabs ?? [])].sort((left, right) => left.index - right.index);
    const reused = new Set();
    for (const group of savedGroups) {
        const indices = (windowState.tabs ?? []).flatMap((tab, index) =>
            tab.group === group.id && !tab.pinned ? [index] : []);
        if (!indices.length)
            continue;
        const members = indices.map(index => tabs[index]);
        const groupId = members[0]?.groupId;
        const ids = new Set(members.map(tab => tab?.id));
        // Runtime IDs are session-local. Match actual member tabs, never saved
        // numeric IDs, titles or colors (unnamed groups commonly look alike).
        if (Number.isInteger(groupId) && groupId >= 0 && !reused.has(groupId) &&
            members.every(tab => tab && !tab.pinned && tab.groupId === groupId) &&
            tabs.filter(tab => tab.groupId === groupId).every(tab => ids.has(tab.id))) {
            reused.add(groupId);
            result.groups_reused += 1;
        } else {
            // Chrome does not expose closed saved groups to extensions. Creating
            // a replacement would add another saved group to the bookmarks bar.
            result.group_warnings.push(`Original tab group ${group.title || group.id} is not available with its saved tabs; no replacement created. Open the original saved group in Chrome.`);
        }
    }
    return result;
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
            } catch (_error) {
                await chrome.storage.session.remove(restoreToken);
                return restoreWindow(payload);
            }
            // Keep the claim even if its URLs changed, so a retry reports the
            // same failed window instead of opening duplicates or navigating
            // a window the user may now be using for something else.
            if ((await claimedWindowIds(restoreToken)).has(record.windowId)) {
                const errors = ['Restored window is also claimed by another saved window'];
                return {
                    window_id: record.windowId, created: record.created, reused: true,
                    urls_restored: false, url_errors: errors, warnings: errors,
                };
            }
            return restoredWindowResult(record.windowId, windowState, {
                reused: true,
                created: record.created,
            });
        }
    }

    const existingWindow = await matchingOpenWindow(windowState, restoreToken);
    if (existingWindow) {
        await rememberRestoredWindow(restoreToken, existingWindow.id, false, windowState);
        return restoredWindowResult(existingWindow.id, windowState, {
            reused: true,
            created: false,
        });
    }

    if (windowState.groups?.length)
        throw new Error('Original grouped Chrome window does not match the saved tabs; no replacement window created. Recover the original window and its existing groups.');

    let createdWindow = null;
    try {
        createdWindow = await chrome.windows.create(createData);
        if (!createdWindow?.id)
            throw new Error('Chrome did not return the newly created window');
        await rememberRestoredWindow(restoreToken, createdWindow.id, true, windowState);
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
        return await restoredWindowResult(createdWindow.id, windowState, {warnings, created: true});
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
    if (payload.focus !== false)
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
    let claimSnapshot = null;
    if (payload.restore_token) {
        const stored = await chrome.storage.session.get(payload.restore_token);
        const record = restoreRecord(stored[payload.restore_token]);
        if (Object.hasOwn(stored, payload.restore_token)) {
            // An original-window recovery can migrate this token away from the
            // old duplicate. Neither that claim nor either window is ours to
            // clean up using the obsolete token/window pair.
            if (!record || record.windowId !== payload.window_id)
                return {closed: false, released: false};
            created = record.created;
            claimSnapshot = JSON.stringify(stored[payload.restore_token]);
        }
    }
    if (created) {
        if (Object.hasOwn(payload, 'expected_full_signature')) {
            try {
                const window = await chrome.windows.get(payload.window_id, {populate: true});
                const summary = browserWindowSummary(window);
                if (summary.full_signature !== payload.expected_full_signature || !summary.urls_loaded)
                    return {closed: false, released: false};
            } catch (_error) {
                // Without current URL evidence, leave both the window and
                // its claim alone. Unguarded cleanup remains idempotent.
                return {closed: false, released: false};
            }
        }
        if (payload.restore_token && claimSnapshot !== null) {
            const current = await chrome.storage.session.get(payload.restore_token);
            if (JSON.stringify(current[payload.restore_token]) !== claimSnapshot)
                return {closed: false, released: false};
        }
        let closeError = null;
        try {
            await chrome.windows.remove(payload.window_id);
        } catch (error) {
            closeError = error.message ?? String(error);
        }
        // Chrome can acknowledge a close before the native window disappears.
        // A rejected API call can also mean it was already closed. Observe the
        // window list rather than treating either outcome as proof of closure.
        let closed = false;
        let observationError = null;
        for (let attempt = 0; attempt < WINDOW_CLOSE_CHECK_ATTEMPTS; attempt += 1) {
            try {
                const windows = await chrome.windows.getAll({populate: false});
                if (!Array.isArray(windows))
                    throw new Error('Chrome returned an invalid window list');
                observationError = null;
                closed = !windows.some(window => window.id === payload.window_id);
                if (closed)
                    break;
            } catch (error) {
                observationError = error.message ?? String(error);
            }
            if (attempt + 1 < WINDOW_CLOSE_CHECK_ATTEMPTS)
                await new Promise(resolve => setTimeout(resolve, WINDOW_CLOSE_CHECK_INTERVAL_MS));
        }
        if (!closed) {
            const reason = observationError
                ? `Could not verify Chrome window closure: ${observationError}`
                : `Chrome window ${payload.window_id} is still open after its close request`;
            return {closed: false, released: false, retryable: true,
                reason: reason + (closeError ? `; remove failed: ${closeError}` : '')};
        }
    }
    if (payload.restore_token && claimSnapshot !== null) {
        const current = await chrome.storage.session.get(payload.restore_token);
        if (JSON.stringify(current[payload.restore_token]) !== claimSnapshot)
            return {closed: created, released: false};
        await chrome.storage.session.remove(payload.restore_token);
    }
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
    } catch (_error) {
        await chrome.storage.session.remove(restoreToken);
        return {exists: false};
    }
    const verification = await verifyRestoredUrls(
        record.windowId, payload.window ?? record.windowState, false,
    );
    if ((await claimedWindowIds(restoreToken)).has(record.windowId)) {
        verification.urls_restored = false;
        verification.urls_pending = false;
        verification.url_errors.push('Restored window is also claimed by another saved window');
    }
    const groups = await verifyRestoredGroups(record.windowId, payload.window ?? record.windowState);
    return {
        exists: true,
        window_id: record.windowId,
        created: record.created,
        ...verification,
        ...groups,
        warnings: [...verification.url_errors, ...groups.group_warnings],
    };
}

function windowStructureSignature(window) {
    const tabs = [...(window.tabs ?? [])].sort((left, right) => left.index - right.index);
    return JSON.stringify({
        type: window.type === 'popup' ? 'popup' : 'normal',
        incognito: Boolean(window.incognito),
        pinned: tabs.map(tab => Boolean(tab.pinned)),
    });
}

// This RPC is an explicit manual repair. Normal startup must never use it to
// overwrite a live window that no longer matches its recorded snapshot URLs.
async function repairRestoredTabs(payload) {
    let record;
    let previousActiveId;
    let mutationAttempted = false;
    let result;
    const repairedTabIds = [];
    try {
        if (!payload.restore_token || !Number.isInteger(payload.window_id) ||
            typeof payload.expected_full_signature !== 'string')
            throw new Error('restore_token, window_id and expected_full_signature are required');
        const stored = await chrome.storage.session.get(payload.restore_token);
        record = restoreRecord(stored[payload.restore_token]);
        const adopt = !record && !Object.hasOwn(stored, payload.restore_token) &&
            payload.adopt_known_window === true && payload.window;
        if (adopt)
            record = {windowId: payload.window_id, created: false, windowState: payload.window};
        if (!record || record.windowId !== payload.window_id || !record.windowState)
            throw new Error('Repair requires the matching restore record and saved window state');
        if (payload.window && windowFullSignature(payload.window) !== windowFullSignature(record.windowState))
            throw new Error('Repair payload conflicts with the recorded saved window');
        const claims = await chrome.storage.session.get(null);
        if (Object.entries(claims).some(([key, value]) => key !== payload.restore_token &&
            restoreRecord(value)?.windowId === record.windowId))
            throw new Error('Restored window is also claimed by another saved window');
        const before = await chrome.windows.get(record.windowId, {populate: true});
        if (windowFullSignature(before) !== payload.expected_full_signature ||
            windowFullSignature(before, false) !== payload.expected_full_signature)
            throw new Error('Window URLs changed or navigation is pending since repair inspection');
        const saved = record.windowState;
        const structure = windowStructureSignature(saved);
        if (windowStructureSignature(before) !== structure)
            throw new Error('Window tab count, type, incognito or pinned state differs from the saved window');
        const tabs = [...(before.tabs ?? [])].sort((left, right) => left.index - right.index);
        const tabIds = tabs.map(tab => tab.id);
        previousActiveId = tabs.find(tab => tab.active)?.id;
        if (!tabIds.every(Number.isInteger) || !Number.isInteger(previousActiveId) ||
            !(saved.tabs ?? []).every(tab => typeof tab.url === 'string'))
            throw new Error('Window tab identity or saved URL is missing');
        const expected = {...before, tabs: tabs.map(tab => ({...tab}))};
        const checkCurrent = async () => {
            const current = await chrome.windows.get(record.windowId, {populate: true});
            const currentTabs = [...(current.tabs ?? [])].sort((left, right) => left.index - right.index);
            if (windowStructureSignature(current) !== structure ||
                JSON.stringify(currentTabs.map(tab => tab.id)) !== JSON.stringify(tabIds) ||
                windowFullSignature(current) !== windowFullSignature(expected))
                throw new Error('Window tabs or URLs changed during manual repair');
        };
        await checkCurrent();
        if (adopt)
            await rememberRestoredWindow(payload.restore_token, record.windowId, false, saved);
        for (let index = 0; index < tabs.length; index += 1) {
            if (tabs[index].url === saved.tabs[index].url)
                continue;
            await checkCurrent();
            mutationAttempted = true;
            await chrome.tabs.update(tabIds[index], {url: saved.tabs[index].url});
            repairedTabIds.push(tabIds[index]);
            // Pending navigation to the requested saved URL is allowed while
            // repairing the next tab; a redirect or other edit is not.
            expected.tabs[index].url = saved.tabs[index].url;
            delete expected.tabs[index].pendingUrl;
        }
        await checkCurrent();
        result = await restoredWindowResult(record.windowId, saved, {
            created: record.created, reused: true,
        });
    } catch (error) {
        const errors = [`Could not repair saved tabs: ${error.message}`];
        result = {
            window_id: payload.window_id, created: record?.created ?? false, reused: true,
            urls_restored: false, url_errors: errors, warnings: [...errors],
        };
    } finally {
        if (mutationAttempted && Number.isInteger(previousActiveId)) {
            try {
                const previous = await chrome.tabs.get(previousActiveId);
                if (previous.windowId !== payload.window_id)
                    throw new Error('Previously active tab moved to another window');
                if (!previous.active)
                    await chrome.tabs.update(previousActiveId, {active: true});
            } catch (error) {
                const message = `Could not restore previously active tab: ${error.message}`;
                result.urls_restored = false;
                result.url_errors.push(message);
                result.warnings.push(message);
            }
        }
    }
    return {...result, repaired_tab_ids: repairedTabIds};
}

// Explicit manual recovery only. Normal startup never infers ownership from a
// partial URL match or navigates an existing native group to an older snapshot.
function recoveryWindowState(window) {
    return {
        id: window.id, type: window.type, incognito: Boolean(window.incognito),
        tabs: [...(window.tabs ?? [])].sort((a, b) => a.index - b.index).map(tab => ({
            id: tab.id, url: tab.pendingUrl ?? tab.url, pinned: Boolean(tab.pinned),
            groupId: Number.isInteger(tab.groupId) ? tab.groupId : -1,
        })),
    };
}

function originalRecoveryMapping(original, saved) {
    const tabs = recoveryWindowState(original).tabs;
    if (tabs.some(tab => tab.pinned) || (saved.tabs ?? []).some(tab => tab.pinned))
        throw new Error('Original-group recovery does not move pinned tabs');
    const mapping = new Map();
    const used = new Set();
    for (const group of saved.groups ?? []) {
        const members = (saved.tabs ?? []).flatMap((tab, index) => tab.group === group.id ? [index] : []);
        if (!members.length)
            continue;
        const candidates = [...new Set(tabs.filter(tab => tab.groupId >= 0).map(tab => tab.groupId))]
            .map(id => tabs.filter(tab => tab.groupId === id))
            .filter(native => native.length === members.length && native.every(tab => !used.has(tab.id)))
            .filter(native => {
                const exact = members.filter((index, offset) => saved.tabs[index].url === native[offset].url).length;
                return exact === members.length || (exact >= 2 && exact * 2 > members.length);
            });
        if (candidates.length !== 1)
            throw new Error('Original group membership is missing or ambiguous');
        members.forEach((index, offset) => {
            const tab = candidates[0][offset];
            used.add(tab.id);
            mapping.set(index, tab.id);
        });
    }
    if (!used.size)
        throw new Error('Recovery requires an identifiable existing original group');
    for (const [index, savedTab] of (saved.tabs ?? []).entries()) {
        if (mapping.has(index))
            continue;
        if (savedTab.group)
            throw new Error('Saved tab refers to an unknown original group');
        const matches = tabs.filter(tab => !used.has(tab.id) && tab.groupId < 0 && tab.url === savedTab.url);
        if (matches.length > 1)
            throw new Error('Ungrouped saved tab identity is ambiguous');
        if (matches.length) {
            mapping.set(index, matches[0].id);
            used.add(matches[0].id);
        }
    }
    const extras = tabs.filter(tab => !used.has(tab.id));
    if (extras.some(tab => tab.groupId >= 0))
        throw new Error('Recovery would split an additional original group');
    const ordered = [...mapping].sort(([a], [b]) => a - b).map(([_index, id]) => id);
    if (JSON.stringify(tabs.filter(tab => used.has(tab.id)).map(tab => tab.id)) !== JSON.stringify(ordered))
        throw new Error('Original saved tabs have a conflicting order');
    return {mapping, extras};
}

async function inspectOriginalWindow(payload) {
    if (!payload.restore_token || !Number.isInteger(payload.original_window_id))
        throw new Error('restore_token and original_window_id are required');
    const stored = await chrome.storage.session.get(null);
    const record = restoreRecord(stored[payload.restore_token]);
    const adopted = !record && payload.adopt_created_window === true && payload.window &&
        Number.isInteger(payload.created_window_id);
    const createdId = record?.windowId ?? (adopted ? payload.created_window_id : null);
    const saved = record?.windowState ?? (adopted ? payload.window : null);
    if (!saved || !(record?.created || adopted) || createdId === payload.original_window_id)
        throw new Error('Recovery requires a distinct known created duplicate and its saved window');
    const claims = Object.entries(stored).filter(([key]) => key !== payload.restore_token)
        .map(([_key, value]) => restoreRecord(value)?.windowId);
    if (claims.includes(createdId) || claims.includes(payload.original_window_id))
        throw new Error('Recovery window is claimed by another saved window');
    const original = await chrome.windows.get(payload.original_window_id, {populate: true});
    const duplicate = await chrome.windows.get(createdId, {populate: true});
    if (!hasExactCommittedUrls(duplicate, saved) ||
        (duplicate.tabs ?? []).some(tab => tab.pinned || tab.groupId >= 0) ||
        (original.tabs ?? []).some(tab => tab.pendingUrl) ||
        original.type !== duplicate.type || Boolean(original.incognito) !== Boolean(duplicate.incognito))
        throw new Error('Recovery windows changed, have pending navigation, or lack an exact ungrouped duplicate');
    const {mapping, extras} = originalRecoveryMapping(original, saved);
    const all = await chrome.windows.getAll({populate: true, windowTypes: ['normal', 'popup']});
    for (const other of all.filter(window => window.id !== original.id && window.id !== duplicate.id &&
        !claims.includes(window.id) && window.type === original.type &&
        Boolean(window.incognito) === Boolean(original.incognito))) {
        let matches = false;
        try { originalRecoveryMapping(other, saved); matches = true; } catch (_) { /* not a candidate */ }
        if (matches)
            throw new Error('More than one original window matches the saved groups');
    }
    const originalState = recoveryWindowState(original);
    const duplicateState = recoveryWindowState(duplicate);
    const steps = [];
    for (const [index, id] of mapping) {
        const tab = originalState.tabs.find(item => item.id === id);
        if (tab.url !== saved.tabs[index].url) {
            if (typeof tab.url !== 'string' || !tab.url)
                throw new Error('Original group tab URL is unavailable');
            steps.push({kind: 'copy', tabId: id, url: tab.url});
            steps.push({kind: 'navigate', tabId: id, url: saved.tabs[index].url});
        }
    }
    for (const tab of extras)
        steps.push({kind: 'move', tabId: tab.id, from: original.id, to: duplicate.id, index: -1});
    for (const [index, _tab] of saved.tabs.entries()) {
        if (!mapping.has(index))
            steps.push({kind: 'move', tabId: duplicateState.tabs[index].id,
                from: duplicate.id, to: original.id, index});
    }
    const plan = {
        restore_token: payload.restore_token, original_window_id: original.id,
        recovery_window_id: duplicate.id, window: saved,
        expected: [originalState, duplicateState], steps,
    };
    return {plan, expected_plan: JSON.stringify(plan)};
}

async function recoverOriginalWindow(payload) {
    if (!payload.restore_token || typeof payload.expected_plan !== 'string')
        throw new Error('A reviewed original-window inspection plan is required');
    const journalKey = `original-recovery:${payload.restore_token}`;
    const stored = await chrome.storage.session.get(journalKey);
    let journal = stored[journalKey];
    if (!journal) {
        const inspected = await inspectOriginalWindow(payload);
        if (inspected.expected_plan !== payload.expected_plan)
            throw new Error('Recovery windows or claims changed since inspection');
        journal = {plan: inspected.plan, fingerprint: inspected.expected_plan,
            expected: inspected.plan.expected, next: 0, copies: {}, pending: null};
        await chrome.storage.session.set({
            [journalKey]: journal,
            [payload.restore_token]: {windowId: journal.plan.recovery_window_id, created: true, windowState: journal.plan.window},
        });
    } else if (journal.fingerprint !== payload.expected_plan) {
        throw new Error('Another original-window recovery plan is already journaled');
    }
    const plan = journal.plan;
    const readCurrent = async () => Promise.all(journal.expected.map(async window =>
        recoveryWindowState(await chrome.windows.get(window.id, {populate: true}))));
    const writeJournal = () => chrome.storage.session.set({[journalKey]: journal});
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    const requireClaims = async () => {
        const claims = await chrome.storage.session.get(null);
        const record = restoreRecord(claims[plan.restore_token]);
        const owner = journal.complete ? plan.original_window_id : plan.recovery_window_id;
        if (!record || record.windowId !== owner || Object.entries(claims).some(([key, value]) =>
            key !== plan.restore_token && [plan.original_window_id, plan.recovery_window_id].includes(restoreRecord(value)?.windowId)))
            throw new Error('Original-window recovery claims changed');
    };
    await requireClaims();
    while (journal.next < plan.steps.length) {
        const step = plan.steps[journal.next];
        let current = await readCurrent();
        if (journal.pending?.after && same(current, journal.pending.after)) {
            journal.expected = journal.pending.after;
            journal.pending = null;
            journal.next += 1;
            await writeJournal();
            continue;
        }
        if (!same(current, journal.expected))
            throw new Error('Recovery tabs changed; preserving both windows for inspection');
        await requireClaims();
        const after = JSON.parse(JSON.stringify(journal.expected));
        if (step.kind === 'copy') {
            // A crash after a copy but before its returned ID was journaled must
            // stop on the changed fingerprint, never create another blind copy.
            journal.pending = {after: null};
            await writeJournal();
            const copy = await chrome.tabs.create({windowId: plan.recovery_window_id, url: step.url, active: false});
            if (!Number.isInteger(copy?.id) || (copy.pendingUrl ?? copy.url) !== step.url)
                throw new Error('Could not preserve the divergent original page; original tab is unchanged');
            after.find(window => window.id === plan.recovery_window_id).tabs.push({
                id: copy.id, url: step.url, pinned: false, groupId: -1,
            });
            journal.copies[step.tabId] = copy.id;
        } else {
            if (step.kind === 'navigate') {
                // Do not replace the original page until its preserved copy has
                // actually loaded the inspected URL in the recovery window.
                const copyId = journal.copies[step.tabId];
                const expectedCopy = journal.expected.flatMap(window => window.tabs).find(tab => tab.id === copyId);
                let loaded = false;
                for (let attempt = 0; attempt < URL_CHECK_ATTEMPTS; attempt += 1) {
                    const copy = await chrome.tabs.get(copyId);
                    if (copy.url === expectedCopy?.url && !copy.pendingUrl && copy.status === 'complete') {
                        loaded = true;
                        break;
                    }
                    if ((copy.pendingUrl ?? copy.url) !== expectedCopy?.url)
                        break;
                    await new Promise(resolve => setTimeout(resolve, URL_CHECK_INTERVAL_MS));
                }
                if (!loaded)
                    throw new Error('Preserved original page has not loaded exactly; original tab is unchanged');
                after.flatMap(window => window.tabs).find(tab => tab.id === step.tabId).url = step.url;
            } else {
                const from = after.find(window => window.id === step.from).tabs;
                const to = after.find(window => window.id === step.to).tabs;
                const [tab] = from.splice(from.findIndex(item => item.id === step.tabId), 1);
                to.splice(step.index < 0 ? to.length : step.index, 0, tab);
            }
            if (!same(await readCurrent(), journal.expected))
                throw new Error('Recovery tabs changed before mutation; preserving both windows');
            journal.pending = {after};
            await writeJournal();
            if (step.kind === 'navigate')
                await chrome.tabs.update(step.tabId, {url: step.url});
            else
                await chrome.tabs.move(step.tabId, {windowId: step.to, index: step.index});
        }
        current = await readCurrent();
        if (!same(current, after))
            throw new Error('Recovery mutation did not preserve the inspected tab identities');
        journal.expected = after;
        journal.pending = null;
        journal.next += 1;
        await writeJournal();
    }
    if (!same(await readCurrent(), journal.expected))
        throw new Error('Recovered windows changed; refusing to migrate the restore claim');
    const result = await restoredWindowResult(plan.original_window_id, plan.window, {created: false, reused: true});
    if (result.urls_restored && !result.group_warnings.length) {
        await requireClaims();
        if (!same(await readCurrent(), journal.expected))
            throw new Error('Recovery windows changed during verification; preserving the existing claim');
        journal.complete = true;
        await chrome.storage.session.set({
            [journalKey]: journal,
            [plan.restore_token]: {windowId: plan.original_window_id, created: false, windowState: plan.window},
        });
    }
    return {...result, recovery_window_id: plan.recovery_window_id,
        preserved_extra_tab_ids: plan.steps.filter(step => step.kind === 'move' && step.from === plan.original_window_id).map(step => step.tabId),
        preserved_page_tab_ids: Object.values(journal.copies), recovery_complete: Boolean(journal.complete)};
}

async function dispatchAction(message) {
    switch (message.action) {
    case 'ping': {
        const config = await configuration();
        const windows = await chrome.windows.getAll({populate: true, windowTypes: ['normal', 'popup']});
        return {
            profile: config.profile,
            protocol_version: PROTOCOL_VERSION,
            active_mutations: activeNativeMutations,
            active_identifications: windows.reduce((count, window) => count + (window.tabs ?? []).filter(tab =>
                (tab.pendingUrl ?? tab.url ?? '').startsWith(IDENTIFY_PAGE)).length, 0),
            window_count: windows.length,
            build: {revision: BUILD_REVISION},
            activation_pending: activationPending,
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
                'exact_url_restore',
                'exact_url_pending',
                'reuse_only_groups',
                'installed_build_activation',
                'lazy_tab_restore',
                'repair_restored_tabs',
                'recover_original_window',
                'inspect_original_window',
                'original_groups_required',
                'exact_capture_identity',
                'runtime_build',
                'native_mutation_status',
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
    case 'repair_restored_tabs': {
        const task = restoreQueue.then(() => repairRestoredTabs(message.payload ?? {}));
        restoreQueue = task.catch(() => {});
        return task;
    }
    case 'inspect_original_window':
        return inspectOriginalWindow(message.payload ?? {});
    case 'recover_original_window': {
        const task = restoreQueue.then(() => recoverOriginalWindow(message.payload ?? {}));
        restoreQueue = task.catch(() => {});
        return task;
    }
    case 'identify_window':
        return identifyWindow(message.payload ?? {});
    case 'focus_window':
        return focusWindow(message.payload ?? {});
    case 'release_window_identification':
        return releaseWindowIdentification(message.payload ?? {});
    case 'close_restored_window': {
        const task = restoreQueue.then(() => closeRestoredWindow(message.payload ?? {}));
        restoreQueue = task.catch(() => {});
        return task;
    }
    default:
        throw new Error(`Unknown wsctl action: ${message.action}`);
    }
}

async function dispatch(message) {
    const mutating = MUTATING_ACTIONS.has(message.action);
    if (mutating && activationPending && message.action !== 'release_window_identification')
        throw new Error('Chrome companion activation is pending; reload the companion when restoration is idle');
    if (mutating)
        activeNativeMutations += 1;
    try {
        return await dispatchAction(message);
    } finally {
        if (mutating)
            activeNativeMutations -= 1;
    }
}

function scheduleNativeReconnect() {
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        return connectNativeHost();
    }, 5000);
}

function resetNativeConnection() {
    nativeConnectionGeneration += 1;
    const port = nativePort;
    // Explicit disconnect need not emit onDisconnect on the initiating side.
    // Release our ownership first so a late event cannot clear a newer port.
    nativePort = null;
    try { port?.disconnect(); } catch (_) { /* already disconnected */ }
    scheduleNativeReconnect();
}

async function activateInstalledBuild(build, port) {
    const revision = build?.revision;
    if (revision === BUILD_REVISION) {
        activationPending = false;
        return;
    }
    if (!/^r-[a-f0-9]{24}$/.test(revision ?? '') || BUILD_REVISION === 'development')
        return;
    activationPending = true;
    // Reload clears session storage, including restore ownership. Preserve every
    // live claim and lease; a fresh browser session can activate before restoring.
    const [session, stored, windows] = await Promise.all([
        chrome.storage.session.get(null), chrome.storage.local.get('activationAttempt'),
        chrome.windows.getAll({populate: true, windowTypes: ['normal', 'popup']}),
    ]);
    if (port !== nativePort || activeNativeMutations || Object.values(session).some(restoreRecord) ||
        windows.some(window => (window.tabs ?? []).some(tab =>
            (tab.pendingUrl ?? tab.url ?? '').startsWith(IDENTIFY_PAGE))))
        return;
    // local storage survives runtime.reload(), unlike session storage. A failed
    // reload must stay observable instead of repeatedly clearing the connection.
    if (stored.activationAttempt === revision)
        return;
    await chrome.storage.local.set({activationAttempt: revision});
    if (port === nativePort && !activeNativeMutations)
        chrome.runtime.reload();
}

async function onNativeMessage(message, port = nativePort) {
    if (port !== nativePort)
        return;
    if (message.type === 'hello') {
        if (message.ok)
            await activateInstalledBuild(message.installed_companion_build, port);
        const config = await configuration();
        if (port !== nativePort)
            return;
        const profile = message.profile ?? message.profileDirectory;
        if (message.ok && !config.profileConfigured && message.profileDirectory &&
            (config.profile !== profile || config.profileDirectory !== message.profileDirectory)) {
            await chrome.storage.local.set({profile, profileDirectory: message.profileDirectory});
        }
        return;
    }
    try {
        const result = await dispatch(message);
        if (nativePort === port)
            port?.postMessage({id: message.id, ok: true, result});
    } catch (error) {
        if (nativePort === port)
            port?.postMessage({id: message.id, ok: false, error: error.message});
    }
}

async function connectNativeHost() {
    if (nativePort || nativeConnectionPending)
        return;
    nativeConnectionPending = true;
    const generation = nativeConnectionGeneration;
    let port = null;
    try {
        const config = await configuration();
        if (generation !== nativeConnectionGeneration)
            return;
        port = chrome.runtime.connectNative(NATIVE_HOST);
        nativePort = port;
        port.onMessage.addListener(message => onNativeMessage(message, port));
        port.onDisconnect.addListener(() => {
            void chrome.runtime.lastError;
            if (nativePort !== port)
                return;
            nativePort = null;
            scheduleNativeReconnect();
        });
        const email = await profileEmail();
        if (nativePort !== port)
            return;
        port.postMessage({
            type: 'hello',
            profile: config.profile,
            profileDirectory: config.profileDirectory,
            profileConfigured: config.profileConfigured,
            profileEmail: email,
        });
    } catch (_error) {
        if (nativePort === port)
            nativePort = null;
        try { port?.disconnect(); } catch (_) { /* already disconnected */ }
    } finally {
        nativeConnectionPending = false;
        if (!nativePort)
            scheduleNativeReconnect();
    }
}

function onStorageChanged(changes, areaName) {
    if (areaName !== 'local')
        return;
    const connectionKeys = ['profile', 'profileDirectory', 'profileConfigured'];
    if (connectionKeys.some(key => Object.hasOwn(changes, key)))
        resetNativeConnection();
}

chrome.runtime.onInstalled.addListener(connectNativeHost);
chrome.runtime.onStartup.addListener(connectNativeHost);
chrome.storage.onChanged.addListener(onStorageChanged);
connectNativeHost();
