// Capture build identity once when this worker loads; never read the install pointer.
if (globalThis.WSCTL_BUILD_REVISION === undefined && typeof importScripts === 'function') {
    try { importScripts('buildInfo.js'); } catch (_) { /* explicit development checkout */ }
}
const BUILD_REVISION = globalThis.WSCTL_BUILD_REVISION ?? 'development';
const NATIVE_HOST = 'org.sagecat.workspace_state';
const PROTOCOL_VERSION = 2;
const IDENTIFY_PAGE = chrome.runtime.getURL('identify.html');
// A native browser page keeps its origin after session restore, so extension
// reload cannot close it as an extension-owned tab.
const WORKSPACE_MARKER_PREFIX = 'chrome://version/#wsctl-workspace-';
const URL_CHECK_INTERVAL_MS = 250;
const URL_CHECK_ATTEMPTS = 40;
const IDENTIFICATION_LEASE_KEY = '__wsctl_identification_leases';
const IDENTIFICATION_LEASE_MS = 60000;
const ABORTED_RESTORES_KEY = '__wsctl_aborted_restores';
const FOCUS_LEASE_KEY = '__wsctl_focus_leases';
const abortedRestores = new Set();
const pendingRestoreVerifications = new Map();

function operationKey(context) {
    if (context === undefined)
        return null;
    const keys = ['boot_id', 'login_generation', 'operation_id', 'mode', 'attempt', 'deadline'];
    if (!context || typeof context !== 'object' || Object.keys(context).length !== keys.length ||
        keys.some(key => !Object.hasOwn(context, key)) ||
        keys.slice(0, 3).some(key => typeof context[key] !== 'string' || !context[key]) ||
        !['startup', 'shutdown'].includes(context.mode) || !Number.isInteger(context.attempt) ||
        context.attempt < 1 || !Number.isFinite(context.deadline) || context.deadline <= 0)
        throw new Error('A complete valid operation_context is required');
    return JSON.stringify(keys.map(key => context[key]));
}

function requireRestoreActive(context) {
    const key = operationKey(context);
    if (key && abortedRestores.has(key))
        throw new Error('Chrome restore operation was aborted; existing windows and claims preserved');
}

async function loadRestoreAbort(context) {
    const key = operationKey(context);
    if (key) {
        const stored = await chrome.storage.session.get(ABORTED_RESTORES_KEY);
        if ((stored[ABORTED_RESTORES_KEY] ?? []).includes(key))
            abortedRestores.add(key);
    }
    requireRestoreActive(context);
}

async function abortRestore(payload) {
    const key = operationKey(payload.operation_context);
    if (!key)
        throw new Error('A complete valid operation_context is required');
    // Revoke in memory before awaiting storage, including queued work. This
    // action must bypass the mutation queue to interrupt a polling restore.
    abortedRestores.add(key);
    const stored = await chrome.storage.session.get(ABORTED_RESTORES_KEY);
    const keys = new Set([...(stored[ABORTED_RESTORES_KEY] ?? []), ...abortedRestores]);
    await chrome.storage.session.set({[ABORTED_RESTORES_KEY]: [...keys]});
    return {aborted: true, operation_context: payload.operation_context, preserved: true};
}

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
let identificationQueue = Promise.resolve();
let identificationTimer = null;
const MUTATING_ACTIONS = new Set([
    'restore_window', 'recreate_window', 'identify_window', 'focus_window',
    'release_window_identification', 'release_expired_identifications',
    'renew_window_identification',
    'prepare_workspace_window', 'cancel_workspace_window', 'confirm_workspace_placement',
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
            workspacePlaced: value.workspacePlaced === true,
            ...(value.workspaceMarker ? {workspaceMarker: value.workspaceMarker} : {}),
            ...(value.windowState ? {windowState: value.windowState} : {}),
            ...(value.operation_context ? {operation_context: value.operation_context} : {}),
            ...(Array.isArray(value.preservedCatalog) ? {preservedCatalog: value.preservedCatalog} : {}),
        };
    }
    return null;
}

async function rememberRestoredWindow(restoreToken, windowId, created, windowState, operationContext, workspaceMarker) {
    if (restoreToken) {
        await chrome.storage.session.set({
            [restoreToken]: {windowId, created: Boolean(created), windowState,
                ...(operationContext ? {operation_context: operationContext} : {}),
                ...(workspaceMarker ? {workspaceMarker} : {})},
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

async function claimedWindowExists(windowId) {
    try {
        await chrome.windows.get(windowId);
        return true;
    } catch (error) {
        // A failed API request is not proof that the original window closed.
        // Dropping its claim here would let a retry create a duplicate after
        // a transient error or after the user navigated its tabs elsewhere.
        const windows = await chrome.windows.getAll({populate: false});
        if (!Array.isArray(windows) || windows.some(window => window.id === windowId))
            throw error;
        return false;
    }
}

async function matchingOpenWindow(windowState, restoreToken, catalog) {
    const claimed = await claimedWindowIds(restoreToken);
    const windows = await chrome.windows.getAll({
        populate: true,
        windowTypes: ['normal', 'popup'],
    });
    const available = windows.filter(window => !claimed.has(window.id));
    const exactSignature = windowFullSignature(windowState);
    let matches = available.filter(window => windowFullSignature(window) === exactSignature);
    if (windowState.groups?.length) {
        const grouped = [];
        for (const window of matches) {
            const groups = await verifyRestoredGroups(window.id, windowState, window);
            if (!groups.group_warnings.length)
                grouped.push(window);
        }
        if (grouped.length)
            matches = grouped;
    }
    if (matches.length > 1) {
        // Identical content and identical saved placement need no original-ID
        // guess. Claim each existing window once; never change its tabs/groups.
        const placement = window => JSON.stringify([
            window.workspace, window.workspace_index, window.monitor,
            window.geometry, window.geometry_relative, window.state, monitorIntent(window),
            window.groups, (window.tabs ?? []).map(tab => tab.group ?? null),
        ]);
        const equivalents = (Array.isArray(catalog) ? catalog : []).filter(entry =>
            windowFullSignature(entry.window ?? {}) === exactSignature);
        if (!equivalents.some(entry => entry.restore_token === restoreToken) ||
            equivalents.length < matches.length ||
            equivalents.some(entry => placement(entry.window) !== placement(windowState)))
            throw new Error('More than one Chrome window matches the saved tabs and groups; original window identity is ambiguous. No replacement window created. Inspect the original windows before retrying.');
        matches.sort((left, right) => left.id - right.id);
    }
    return matches[0] ?? null;
}

function pristineStartupWindow(window) {
    const tabs = window.tabs ?? [];
    return tabs.length === 1 && !tabs[0].pinned && !(tabs[0].groupId >= 0) &&
        !tabs[0].pendingUrl && !tabs[0].discarded && tabs[0].status === 'complete' &&
        ['about:blank', 'chrome://newtab/', 'chrome://new-tab-page/'].includes(tabs[0].url);
}

function exactGroupMembership(window, saved) {
    const tabs = [...(window.tabs ?? [])].sort((a, b) => a.index - b.index);
    const expected = saved.tabs ?? [];
    if (tabs.length !== expected.length)
        return false;
    const nativeBySaved = new Map();
    const savedByNative = new Map();
    return expected.every((tab, index) => {
        const native = tabs[index].groupId >= 0 ? tabs[index].groupId : null;
        const group = tab.group ?? null;
        if (group === null || native === null)
            return group === native;
        if ((nativeBySaved.has(group) && nativeBySaved.get(group) !== native) ||
            (savedByNative.has(native) && savedByNative.get(native) !== group))
            return false;
        nativeBySaved.set(group, native);
        savedByNative.set(native, group);
        return true;
    });
}

async function requireAbsentOriginal(windowState, restoreToken, catalog, recreating = false) {
    // Neither an ordinal capture label nor a previous browser session's numeric
    // ID identifies a changed native window. A failed exact match is therefore
    // not proof of absence, even for a single tab with no surviving URL overlap.
    const windows = await chrome.windows.getAll({populate: true, windowTypes: ['normal', 'popup']});
    const inventory = values => JSON.stringify([...values].sort((a, b) => a.id - b.id).map(window =>
        [window.id, windowFullSignature(window), (window.tabs ?? []).map(tab =>
            [tab.id, tab.groupId, tab.status, tab.discarded, tab.pendingUrl])]));
    const unchanged = async () => {
        const current = await chrome.windows.getAll({populate: true, windowTypes: ['normal', 'popup']});
        if (recreating) {
            // An earlier claim accounts for a keeper's identity, even if it has
            // since navigated. Check both committed and pending URLs on every
            // keeper immediately before creating the separate window.
            const saved = new Set(windowState.tabs.map(tab => navigationUrl(tab.url)));
            if (current.some(window => (window.tabs ?? []).some(tab =>
                [tab.url, tab.pendingUrl].some(url => url && saved.has(navigationUrl(url))))))
                throw new Error('A saved Chrome tab URL is present in a live window; no replacement window created.');
        }
        if (inventory(current) !== inventory(windows))
            throw new Error('Chrome windows changed while checking the missing original; no replacement window created. Retry after native restoration settles.');
    };
    let unresolved = windows.filter(window =>
        (window.type === 'popup') === (windowState.type === 'popup') &&
        Boolean(window.incognito) === Boolean(windowState.incognito) &&
        !pristineStartupWindow(window));
    if (!unresolved.length)
        return unchanged();

    const fail = () => {
        throw new Error('Original Chrome window identity is unresolved: existing tabs may have changed. No replacement window created. Inspect the live windows or capture their current state before retrying.');
    };
    if (!Array.isArray(catalog) || !restoreToken)
        fail();
    const tokens = catalog.map(entry => entry?.restore_token);
    const own = catalog.filter(entry => entry?.restore_token === restoreToken);
    if (tokens.some(token => typeof token !== 'string' || !token) || new Set(tokens).size !== tokens.length ||
        own.length !== 1 || windowFullSignature(own[0].window ?? {}) !== windowFullSignature(windowState))
        fail();
    const stored = await chrome.storage.session.get(null);
    const liveIds = new Set(windows.map(window => window.id));
    const claimsByWindow = new Map();
    for (const [key, value] of Object.entries(stored)) {
        const id = restoreRecord(value)?.windowId;
        if (liveIds.has(id))
            claimsByWindow.set(id, [...(claimsByWindow.get(id) ?? []), key]);
    }
    const accountedClaims = new Set();
    for (const entry of catalog.filter(entry => entry.restore_token !== restoreToken && entry.window)) {
        const record = restoreRecord(stored[entry.restore_token]);
        if (record?.windowState && claimsByWindow.get(record.windowId)?.length === 1 &&
            windowFullSignature(record.windowState) === windowFullSignature(entry.window) &&
            JSON.stringify((record.windowState.tabs ?? []).map(tab => tab.group ?? null)) ===
                JSON.stringify((entry.window.tabs ?? []).map(tab => tab.group ?? null)))
            accountedClaims.add(record.windowId);
    }
    // A window that the caller paired with another recipe a moment ago, by its
    // native window ID in this browser session, accounts for that recipe
    // whatever its tabs show now.
    for (const entry of catalog)
        if (entry?.window?._preserve_current_content === true && liveIds.has(entry.window._reconcile_window_id))
            accountedClaims.add(entry.window._reconcile_window_id);
    // A foreign/legacy claim proves ownership, not absence of this original.
    // Only a unique claim for another exact current-catalog recipe can account
    // for a live window, including one the user subsequently navigated.
    unresolved = unresolved.filter(window => !accountedClaims.has(window.id));
    if (unresolved.some(window => claimsByWindow.has(window.id)))
        fail();
    const others = catalog.filter(entry => entry.restore_token !== restoreToken && entry.window &&
        !liveIds.has(restoreRecord(stored[entry.restore_token])?.windowId));
    // This assignment proves only that the other exact saved recipes account
    // for the live inventory. It never claims, moves or rewrites those windows.
    // Considering the full catalog avoids making absence depend on loop order.
    const candidates = unresolved.map(window => others.flatMap((entry, index) =>
        windowFullSignature(window) === windowFullSignature(entry.window) &&
        exactGroupMembership(window, entry.window) ? [index] : []));
    const assigned = new Map();
    function assign(index, visited) {
        for (const slot of candidates[index]) {
            if (visited.has(slot))
                continue;
            visited.add(slot);
            if (!assigned.has(slot) || assign(assigned.get(slot), visited)) {
                assigned.set(slot, index);
                return true;
            }
        }
        return false;
    }
    if (!unresolved.every((_window, index) => assign(index, new Set())))
        fail();
    await unchanged();
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
        // Chrome keeps restored background tabs parked until they are selected.
        // Their committed URL and tab/group identity are the workspace state;
        // opening them merely to verify a network load can change that state.
        if (tab.pendingUrl || (!isLazyTab(tab) && tab.status !== 'complete')) {
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
        (window.tabs ?? []).every(tab => isLazyTab(tab) ||
            tab.status === 'complete' || tab.status === 'loading') &&
        (window.tabs ?? []).some(tab => tab.pendingUrl || tab.status === 'loading'));
}

function isLazyTab(tab) {
    return tab.discarded || tab.status === 'unloaded';
}

async function nativeIdentificationProtected() {
    // A marker lease remains a fence until exact cleanup is confirmed, including
    // preserve-focus captures and failed cleanup after expiry. Popup focus
    // receipts have no marker and use a bounded, exact-token session lease.
    if (Object.values(await identificationLeases()).some(ownedIdentification))
        return true;
    const stored = await chrome.storage.session.get(FOCUS_LEASE_KEY);
    return Object.values(stored[FOCUS_LEASE_KEY] ?? {}).some(lease =>
        Number.isInteger(lease.window_id) && typeof lease.token === 'string' &&
        lease.expires_at > Date.now());
}

function identificationDeferredVerification() {
    return {urls_restored: false, urls_pending: true, verification_deferred: true,
        url_errors: ['Chrome native identification is active; exact verification deferred']};
}

async function verifyRestoredUrls(windowId, windowState, wait = true, operationContext, workspaceMarker) {
    let errors = [];
    let window;
    let completeChecks = 0;
    let identificationWaits = 0;
    const attempts = wait ? URL_CHECK_ATTEMPTS : 1;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
        if (wait) requireRestoreActive(operationContext);
        // Sample inside the same queue as identification, so a marker cannot be
        // created between checking its fence and reading this window's tabs.
        let observation;
        try {
            observation = operationContext ? await identificationTask(async () =>
                await nativeIdentificationProtected() ? null : chrome.windows.get(windowId, {populate: true})) :
                undefined;
        } catch (error) {
            return {urls_restored: false, url_errors: [`Could not verify restored window: ${error.message}`]};
        }
        if (observation === null) {
            completeChecks = 0;
            if (!wait || ++identificationWaits >= URL_CHECK_ATTEMPTS * 6)
                return identificationDeferredVerification();
            await new Promise(resolve => setTimeout(resolve, URL_CHECK_INTERVAL_MS));
            attempt -= 1; // Identification time does not consume URL load observations.
            continue;
        }
        try {
            window = observation ?? await chrome.windows.get(windowId, {populate: true});
        } catch (error) {
            return {urls_restored: false, url_errors: [`Could not verify restored window: ${error.message}`]};
        }
        if (workspaceMarker)
            window = workspaceContent(window, workspaceMarker);
        errors = restoredUrlErrors(window, windowState);
        completeChecks = errors.length ? 0 : completeChecks + 1;
        // Observe twice so a page still loading cannot count as stable. Parked
        // tabs remain parked; their exact committed URLs are checked in place.
        if (!errors.length && (!wait || completeChecks >= 2))
            return {urls_restored: true, url_errors: [], native_window: window};
        const loading = (window.tabs ?? []).some(tab => tab.pendingUrl || tab.status === 'loading');
        if (errors.length && !loading)
            break;
        if (attempt + 1 < attempts)
            await new Promise(resolve => setTimeout(resolve, URL_CHECK_INTERVAL_MS));
    }
    return {
        urls_restored: false,
        native_window: window,
        // A first clean sample at the deadline still needs confirmation. Keep
        // observing that window instead of treating late completion as failure.
        urls_pending: completeChecks > 0 || restoredUrlsPending(window, windowState),
        url_errors: errors.length ? errors : ['Restored URLs are complete; waiting for stability confirmation'],
    };
}

async function restoredWindowResult(windowId, windowState,
    {created, reused = false, warnings = [], operationContext, workspaceMarker}) {
    const {native_window: sample, ...verification} = await verifyRestoredUrls(
        windowId, windowState, true, operationContext, workspaceMarker);
    requireRestoreActive(operationContext);
    const groups = sample ? await verifyRestoredGroups(windowId, windowState, sample, Boolean(workspaceMarker)) :
        {groups_reused: 0, group_warnings: verification.verification_deferred ? [] :
            ['Restored window sample unavailable for group verification']};
    return {
        window_id: windowId,
        created,
        reused,
        ...verification,
        ...groups,
        warnings: [...warnings, ...verification.url_errors, ...groups.group_warnings],
    };
}

async function ownedStartupWindowResult(payload, windowId, windowState, options) {
    if (payload.operation_context?.mode !== 'startup')
        return restoredWindowResult(windowId, windowState, options);
    requireRestoreActive(payload.operation_context);
    const token = payload.restore_token;
    if (!token)
        throw new Error('Owned startup restoration requires restore_token');
    await rememberRestoredWindow(token, windowId, options.created, windowState, payload.operation_context);
    const key = `${token}:${operationKey(payload.operation_context)}`;
    if (!pendingRestoreVerifications.has(key)) {
        // Yield the ownership receipt before slow URL polls. Only lazy-tab
        // activation enters the mutation queue; observation cannot block claims.
        const task = Promise.resolve().then(() => restoredWindowResult(windowId, windowState,
            {...options, operationContext: payload.operation_context}));
        pendingRestoreVerifications.set(key, task);
        task.catch(() => {}).finally(() => {
            if (pendingRestoreVerifications.get(key) === task)
                pendingRestoreVerifications.delete(key);
        });
    }
    return {window_id: windowId, ...options, operation_context: payload.operation_context,
        urls_restored: false, urls_pending: true, verification_pending: true,
        url_errors: ['Exact loaded URL and original group verification is pending'],
        groups_reused: 0, group_warnings: [], warnings: options.warnings ?? []};
}

function browserWindowSummary(window) {
    window = workspaceContent(window);
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

async function captureWindow(window, ordinal, runtimeIdentity = false) {
    window = workspaceContent(window);
    const groups = await chrome.tabGroups.query({windowId: window.id});
    groups.sort((left, right) => left.id - right.id);
    const groupIds = new Map(groups.map((group, index) => [group.id, `group-${index + 1}`]));
    const tabs = [...(window.tabs ?? [])].sort((left, right) => left.index - right.index);
    return {
        id: `window-${ordinal + 1}`,
        runtime_window_id: window.id,
        ...(runtimeIdentity ? {runtime_identity: {
            tabs: tabs.map(tab => [tab.id, tab.groupId ?? -1]),
            groups: groups.map(group => group.id),
        }} : {}),
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

async function captureBrowser(payload = {}) {
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
        windows: await Promise.all(windows.map((window, index) =>
            captureWindow(window, index, payload.runtime_identity === true))),
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

async function createTab(windowId, tabState, warnings, operationContext) {
    let tab;
    try {
        tab = await chrome.tabs.create({
            windowId,
            url: tabState.url,
            active: false,
        });
    } catch (error) {
        warnings.push(`Could not restore ${tabState.url}: ${error.message}`);
        requireRestoreActive(operationContext);
        tab = await chrome.tabs.create({windowId, url: 'chrome://newtab/', active: false});
    }
    requireRestoreActive(operationContext);
    return tabState.pinned ? pinTab(tab, warnings) : tab;
}

async function verifyRestoredGroups(windowId, windowState, sample, strict = false) {
    const savedGroups = windowState?.groups ?? [];
    const result = {groups_reused: 0, group_warnings: []};
    if (!strict && !savedGroups.length)
        return result;
    const window = sample ?? await chrome.windows.get(windowId, {populate: true});
    if (strict) {
        if (reconciliationSignature(await captureWindow(window, 0)) !== reconciliationSignature(windowState))
            result.group_warnings.push('Restored Chrome groups or their member tabs differ from the archived window');
        else
            result.groups_reused = savedGroups.length;
        return result;
    }
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

function reconciliationSignature(window) {
    const tabs = window.tabs ?? [];
    const groups = new Map((window.groups ?? []).map(group => [group.id, [
        tabs.flatMap((tab, index) => tab.group === group.id ? [index] : []),
        group.title ?? '', group.color, Boolean(group.collapsed),
    ]]));
    return JSON.stringify([window.type, Boolean(window.incognito), tabs.map(tab => [
        tab.url, Boolean(tab.pinned), groups.get(tab.group) ?? null,
    ])]);
}

function navigationUrl(url) {
    try {
        const parsed = new URL(url);
        if (/^https?:\/\//i.test(url) && ['http:', 'https:'].includes(parsed.protocol) && parsed.host)
            return url.split('#', 1)[0];
    } catch (_) { /* Non-HTTP or invalid URLs keep their exact identity. */ }
    return url;
}

function navigationSignature(window) {
    return reconciliationSignature({...window, tabs: (window.tabs ?? []).map(tab =>
        ({...tab, url: navigationUrl(tab.url)}))});
}

function monitorIntent(window) {
    const intent = window.monitor_intent;
    return intent && Object.keys(intent).length ? intent : null;
}

function savedPlacementSignature(window) {
    const canonical = value => Array.isArray(value) ? value.map(canonical) :
        value && typeof value === 'object' ? Object.fromEntries(
            Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;
    const placement = Object.fromEntries([
        'workspace', 'workspace_index', 'monitor', 'geometry', 'geometry_relative', 'state',
    ].map(key => [key, window[key] ?? null]));
    placement.monitor_intent = monitorIntent(window);
    return JSON.stringify(canonical(placement));
}

function runtimeIdentitySignature(identity) {
    return JSON.stringify([identity?.tabs, identity?.groups]);
}

// A preservation claim binds a window ID, kind and saved destination only. Tab
// and group content, and runtime IDs, may differ between runs of one session.
function preservedBindingMatches(state, saved) {
    return Boolean(state) && state.type === saved.type &&
        Boolean(state.incognito) === Boolean(saved.incognito) &&
        savedPlacementSignature(state) === savedPlacementSignature(saved);
}

async function verifyPreservedCatalog(catalog, ownToken) {
    const fail = () => { throw new Error('Browser reconciliation needs review: the paired native window is missing or changed type, or claims/destinations conflict; no replacement created'); };
    if (!Array.isArray(catalog) || !catalog.length || !ownToken ||
        catalog.some(entry => !entry?.window?._preserve_current_content || !entry.restore_token) ||
        new Set(catalog.map(entry => entry.restore_token)).size !== catalog.length ||
        new Set(catalog.map(entry => entry.window._reconcile_window_id)).size !== catalog.length ||
        catalog.filter(entry => entry.restore_token === ownToken).length !== 1)
        fail();
    // Chrome window IDs are unique within a browser session. After pairing, the
    // own item binds only to that window still existing with the same kind;
    // tab/group content, other windows and the inventory size may change freely.
    const windows = await chrome.windows.getAll({windowTypes: ['normal', 'popup']});
    const claims = await chrome.storage.session.get(null);
    const destinations = new Map();
    for (const entry of catalog) {
        const saved = entry.window;
        const windowId = saved._reconcile_window_id;
        const signature = navigationSignature(saved);
        const placement = savedPlacementSignature(saved);
        if (entry.restore_token === ownToken) {
            const live = windows.find(window => window.id === windowId);
            if (!live || live.type !== saved.type || Boolean(live.incognito) !== Boolean(saved.incognito))
                fail();
        }
        if (destinations.has(signature) && destinations.get(signature) !== placement)
            fail();
        destinations.set(signature, placement);
        for (const [token, value] of Object.entries(claims)) {
            const record = restoreRecord(value);
            if (!record)
                continue;
            const own = token === entry.restore_token;
            if (!own && record.windowId === windowId)
                fail();  // Another token already claims this window.
            // A window that an explicit reopen created holds a plain claim with
            // created: true; it binds to the same native window ID and destination
            // as a preservation claim. A claim on a window Chrome restored does not.
            // A reuse-only claim (retained observation) on the same native window records
            // that window ID in its saved window; it binds the same way.
            const reuseClaim = record.windowState?._reconcile_window_id === windowId &&
                !record.windowState._preserve_current_content;
            if (own && entry.restore_token === ownToken &&
                (record.windowId !== windowId ||
                 (!record.preservedCatalog && record.created !== true && !reuseClaim) ||
                 !preservedBindingMatches(record.windowState, saved)))
                fail();
        }
    }
}

function preservedWindowResult(windowId, saved) {
    return {window_id: windowId, created: false, reused: true,
        content_preserved: true, content_basis: 'current-native-session',
        urls_restored: false, urls_pending: false, url_errors: [], warnings: [],
        groups_reused: (saved.groups ?? []).length, group_warnings: []};
}

async function reusePreservedWindow(payload) {
    await verifyPreservedCatalog(payload.restore_catalog, payload.restore_token);
    const own = payload.restore_catalog.filter(entry => entry.restore_token === payload.restore_token);
    if (own.length !== 1 || own[0].window._reconcile_window_id !== payload.expected_window_id ||
        JSON.stringify(own[0].window) !== JSON.stringify(payload.window))
        throw new Error('Browser reconciliation needs review: preservation binding differs; no replacement created');
    // This claim authorizes placement only. In particular do not call
    // restoredWindowResult: its lazy-tab activation would load user content.
    await chrome.storage.session.set({[payload.restore_token]: {
        windowId: payload.expected_window_id, created: false, windowState: payload.window,
        preservedCatalog: payload.restore_catalog,
    }});
    await verifyPreservedCatalog(payload.restore_catalog, payload.restore_token);
    return preservedWindowResult(payload.expected_window_id, payload.window);
}

async function reuseReconciledWindow(payload) {
    const id = payload.expected_window_id;
    if (!Number.isInteger(id) || !payload.restore_token)
        throw new Error('Browser reconciliation needs review: missing exact native identity; no replacement created');
    const catalog = payload.restore_catalog;
    const windows = await chrome.windows.getAll({populate: true, windowTypes: ['normal', 'popup']});
    if (!Array.isArray(catalog) || catalog.length !== windows.length ||
        new Set(catalog.map(entry => entry.window?._reconcile_window_id)).size !== windows.length)
        throw new Error('Browser reconciliation needs review: complete native inventory changed; no replacement created');
    for (const entry of catalog) {
        const original = windows.find(candidate => candidate.id === entry.window?._reconcile_window_id);
        if (!original || !hasExactCommittedUrls(original, entry.window) ||
            reconciliationSignature(await captureWindow(original, 0)) !== reconciliationSignature(entry.window))
            throw new Error('Browser reconciliation needs review: native catalog changed; no replacement created');
    }
    const window = await chrome.windows.get(id, {populate: true});
    const captured = await captureWindow(window, 0);
    if (!hasExactCommittedUrls(window, payload.window) ||
        reconciliationSignature(captured) !== reconciliationSignature(payload.window) ||
        (await claimedWindowIds(payload.restore_token)).has(id))
        throw new Error('Browser reconciliation needs review: original window changed or is already claimed; no replacement created');
    const prior = restoreRecord((await chrome.storage.session.get(payload.restore_token))[payload.restore_token]);
    if (prior && prior.windowId !== id)
        throw new Error('Browser reconciliation needs review: existing claim differs; no replacement created');
    await rememberRestoredWindow(payload.restore_token, id, false, payload.window);
    return ownedStartupWindowResult(payload, id, payload.window, {created: false, reused: true});
}

function workspaceMarkerUrl(marker) {
    if (typeof marker !== 'string' || !/^[a-f0-9]{64}$/.test(marker))
        throw new Error('Invalid workspace marker');
    return WORKSPACE_MARKER_PREFIX + marker;
}

function workspaceMarkerAddress(address) {
    if (typeof address !== 'string' || !address.startsWith(WORKSPACE_MARKER_PREFIX))
        return null;
    const parsed = /^([a-f0-9]{64})(?:-(rebuild|ready|placed|(?:rebuild-)?group-(?:0|[1-9][0-9]*)))?$/
        .exec(address.slice(WORKSPACE_MARKER_PREFIX.length));
    return parsed ? {url: WORKSPACE_MARKER_PREFIX + parsed[1], phase: parsed[2] ?? 'parked'} : null;
}

function workspaceMarkers(window) {
    return (window.tabs ?? []).flatMap(tab => {
        const pending = workspaceMarkerAddress(tab.pendingUrl);
        const committed = workspaceMarkerAddress(tab.url);
        const marker = pending ?? committed;
        if (!marker)
            return [];
        return [{...marker, tab, committed, unsafeNavigation: Boolean(tab.pendingUrl &&
            (!pending || (committed && committed.url !== pending.url)))}];
    });
}

function disposableWorkspaceMarker(window, marker, allowRebuildPlaceholder = false) {
    const {tab, phase, committed, unsafeNavigation} = marker;
    const placeholder = allowRebuildPlaceholder && phase.startsWith('rebuild');
    return window.type === 'normal' && !window.incognito && !unsafeNavigation &&
        !tab.pinned && !(tab.groupId >= 0) &&
        ((placeholder && (committed || !tab.url || tab.url === 'about:blank')) ||
            (committed && !tab.active && window.tabs.length > 1));
}

function workspaceContent(window, url) {
    const markers = workspaceMarkers(window);
    if (markers.length === 1 && (!url || markers[0].url === url) && disposableWorkspaceMarker(window, markers[0]))
        return {...window, tabs: window.tabs.filter(tab => tab.id !== markers[0].tab.id)};
    return window;
}

function requireWorkspaceMarker(window, url, allowRebuildPlaceholder = false) {
    const markers = workspaceMarkers(window);
    if (markers.length !== 1 || markers[0].url !== url ||
        !disposableWorkspaceMarker(window, markers[0], allowRebuildPlaceholder))
        throw new Error('Workspace marker is absent, ambiguous or no longer a disposable tab');
    return markers[0];
}

function markedWorkspaceWindows(windows, url) {
    return windows.filter(window => workspaceMarkers(window).some(marker =>
        marker.url === url || marker.committed?.url === url));
}

async function updateWorkspaceMarker(windowId, url, phase, nextPhase) {
    const current = await chrome.windows.get(windowId, {populate: true});
    const marker = requireWorkspaceMarker(current, url, true);
    if (marker.phase !== phase)
        throw new Error('Workspace marker changed before its state could be recorded');
    const address = nextPhase === 'parked' ? url : url + '-' + nextPhase;
    await chrome.tabs.update(marker.tab.id, {url: address});
    await waitForWorkspaceMarker(marker.tab.id, windowId, address, marker.tab.url);
}

async function waitForWorkspaceMarker(tabId, windowId, address, previous) {
    for (let attempt = 0; attempt < 50; attempt += 1) {
        const tab = await chrome.tabs.get(tabId);
        const observed = tab.pendingUrl ?? tab.url;
        if (tab.windowId !== windowId || (observed !== address && (tab.pendingUrl || observed !== previous)) ||
            tab.pinned || tab.groupId >= 0)
            throw new Error('Chrome workspace marker changed before it could be saved');
        if (!tab.pendingUrl && tab.url === address)
            return;
        await new Promise(resolve => setTimeout(resolve, 20));
    }
    throw new Error('Chrome workspace marker navigation did not commit');
}

async function prepareWorkspaceWindow(payload) {
    const url = workspaceMarkerUrl(payload.marker);
    const window = await chrome.windows.get(payload.window_id, {populate: true});
    if (window.incognito || window.type !== 'normal')
        throw new Error('Only regular Chrome windows can be parked');
    const existing = workspaceMarkers(window);
    if (existing.length && requireWorkspaceMarker(window, url).phase !== 'parked')
        throw new Error('Chrome window already belongs to an incomplete workspace restore');
    const original = workspaceContent(window, url);
    if (reconciliationSignature(await captureWindow(original, 0)) !== reconciliationSignature(payload.window))
        throw new Error('Chrome tabs or groups changed after the workspace was saved');
    if (!existing.length) {
        const tab = await chrome.tabs.create({windowId: window.id, url, active: false});
        await waitForWorkspaceMarker(tab.id, window.id, url);
    }
    return {prepared: true};
}

async function cancelWorkspaceWindow(payload) {
    const url = workspaceMarkerUrl(payload.marker);
    const windows = markedWorkspaceWindows(await chrome.windows.getAll({populate: true}), url);
    if (windows.length > 1 || (windows.length && Number.isInteger(payload.window_id) &&
        windows[0].id !== payload.window_id))
        throw new Error('Workspace marker does not identify exactly the expected window');
    if (windows.length) {
        const window = await chrome.windows.get(windows[0].id, {populate: true});
        const marker = requireWorkspaceMarker(window, url);
        await chrome.tabs.remove(marker.tab.id);
    }
    await chrome.storage.local.remove(workspacePlacementResetKey(url));
    return {cancelled: true};
}

function workspacePlacementResetKey(url) {
    return 'workspace-placement-reset:' + url.slice(WORKSPACE_MARKER_PREFIX.length);
}

async function workspacePlacementResetPending(url) {
    const key = workspacePlacementResetKey(url);
    return (await chrome.storage.local.get(key))[key] === true;
}

async function resetReopenedWorkspacePlacement(windowId, url) {
    if (!await workspacePlacementResetPending(url))
        return;
    const native = await chrome.windows.get(windowId, {populate: true});
    const marker = requireWorkspaceMarker(native, url, true);
    if (marker.phase === 'placed')
        await updateWorkspaceMarker(windowId, url, 'placed', 'ready');
    else if (marker.phase === 'ready')
        await waitForWorkspaceMarker(marker.tab.id, windowId, url + '-ready');
    await chrome.storage.local.remove(workspacePlacementResetKey(url));
}

async function confirmWorkspacePlacement(payload) {
    const key = payload.restore_token;
    const id = payload.window_id;
    const stored = (await chrome.storage.session.get(key))[key];
    const url = payload.marker === undefined ? stored?.workspaceMarker : workspaceMarkerUrl(payload.marker);
    if (!key || !Number.isInteger(id) || !url ||
        (stored && (!stored.created || stored.windowId !== id || stored.workspaceMarker !== url)))
        throw new Error('Chrome workspace placement claim changed');
    if (await workspacePlacementResetPending(url))
        throw new Error('The reopened Chrome workspace window still needs placement');
    const windows = markedWorkspaceWindows(await chrome.windows.getAll({populate: true}), url);
    if (windows.length !== 1 || windows[0].id !== id)
        throw new Error('Chrome workspace placement marker changed');
    const marker = requireWorkspaceMarker(windows[0], url);
    if (marker.phase === 'ready')
        await updateWorkspaceMarker(id, url, 'ready', 'placed');
    else if (marker.phase !== 'placed')
        throw new Error('Chrome workspace content has not finished restoring');
    if (stored)
        await chrome.storage.session.set({[key]: {...stored, workspacePlaced: true}});
    return {confirmed: true};
}

async function reopenParkedWindow(payload, url) {
    const live = markedWorkspaceWindows(await chrome.windows.getAll({populate: true}), url);
    if (live.length > 1)
        throw new Error('More than one open Chrome window carries the workspace marker');
    let window = live[0];
    if (!window) {
        const recent = await chrome.sessions.getRecentlyClosed();
        const closed = recent.filter(entry =>
            entry.window?.sessionId && markedWorkspaceWindows([entry.window], url).length);
        if (closed.length > 1 || (closed.length && workspaceMarkers(closed[0].window).length !== 1))
            throw new Error('More than one closed Chrome window or tab carries the workspace marker');
        if (closed.length) {
            if (workspaceMarkers(closed[0].window)[0].phase === 'placed') {
                // The historical marker describes the old compositor window.
                // Keep this intent across a worker reload after sessions.restore.
                await chrome.storage.local.set({[workspacePlacementResetKey(url)]: true});
            }
            const restored = await chrome.sessions.restore(closed[0].window.sessionId);
            if (!Number.isInteger(restored?.window?.id))
                throw new Error('Chrome did not reopen the marked window');
            window = restored.window;
        } else {
            if (recent.some(entry => entry.tab && workspaceMarkers({tabs: [entry.tab]})
                .some(marker => marker.url === url || marker.committed?.url === url)))
                throw new Error('Chrome closed the workspace marker separately; the original window may still be open');
            window = await chrome.windows.create({url: url + '-rebuild', focused: false, type: 'normal'});
        }
    }
    await rememberRestoredWindow(payload.restore_token, window.id, true, payload.window, undefined, url);
    return completeWorkspaceRestore(payload, window.id, url, Boolean(live.length));
}

async function completeWorkspaceRestore(payload, windowId, url, reused) {
    await resetReopenedWorkspacePlacement(windowId, url);
    const phase = await finishNativeWorkspaceRestore(windowId, payload.window, url);
    const result = await ownedStartupWindowResult(payload, windowId, payload.window, {
        created: true, reused, workspaceMarker: url,
    });
    return {...result, workspace_placed: phase === 'placed'};
}

// The marker survives an extension reload. Once it records assembled content,
// retries observe the same window without changing any of its original tabs.
async function finishNativeWorkspaceRestore(windowId, saved, url) {
    const native = await chrome.windows.get(windowId, {populate: true});
    const marker = requireWorkspaceMarker(native, url, true);
    if (marker.phase === 'ready' || marker.phase === 'placed')
        return marker.phase;
    const rebuilding = marker.phase.startsWith('rebuild');
    const live = native.tabs.filter(tab => tab.id !== marker.tab.id);
    const matches = (tab, recipe) => (tab.pendingUrl || tab.url) === recipe.url &&
        Boolean(tab.pinned) === Boolean(recipe.pinned);
    let survivors;
    if (saved.tabs.length === live.length && saved.tabs.every((tab, index) => matches(live[index], tab))) {
        survivors = saved.tabs;
    } else if (rebuilding && live.every((tab, index) => saved.tabs[index] && matches(tab, saved.tabs[index]))) {
        survivors = saved.tabs.slice(0, live.length);
    } else {
        // Missing native group members are inserted in archive order. A retry
        // may already have restored a prefix of those members.
        let restoredGroups = live.length - saved.tabs.filter(tab => !tab.group).length;
        survivors = saved.tabs.filter(tab => !tab.group || restoredGroups-- > 0);
        if (survivors.length !== live.length || !survivors.every((tab, index) => matches(live[index], tab)))
            throw new Error('Reopened Chrome window differs from its archived tabs; window preserved');
    }

    let index = 0;
    const present = new Set(survivors);
    const restored = [];
    for (const [position, tab] of saved.tabs.entries()) {
        if (present.has(tab))
            restored.push(live[index++]);
        else
            restored.push(await chrome.tabs.create({windowId, url: tab.url, pinned: Boolean(tab.pinned),
                index: position, active: false}));
    }
    const phase = await restoreWorkspaceGroups(windowId, saved, restored, url, marker.phase, rebuilding);
    const selected = restored[saved.tabs.findIndex(tab => tab.active)] ?? restored[0];
    if (!selected?.active)
        await chrome.tabs.update(selected.id, {active: true});
    await updateWorkspaceMarker(windowId, url, phase, 'ready');
    return 'ready';
}

async function restoreWorkspaceGroups(windowId, saved, restoredTabs, url, phase, rebuilding) {
    const base = rebuilding ? 'rebuild' : 'parked';
    for (const [index, group] of (saved.groups ?? []).entries()) {
        const groupPhase = (rebuilding ? 'rebuild-' : '') + 'group-' + index;
        const members = saved.tabs.flatMap((tab, index) => tab.group === group.id ? [restoredTabs[index].id] : []);
        if (!members.length)
            throw new Error('Archived tab group has no members');
        const current = await chrome.windows.get(windowId, {populate: true});
        const tabs = current.tabs.filter(tab => members.includes(tab.id));
        if (tabs.length !== members.length || tabs.some(tab => tab.pinned))
            throw new Error('Workspace tabs changed before grouping');
        let id = tabs[0].groupId;
        const exactGroup = id >= 0 && tabs.every(tab => tab.groupId === id) &&
            current.tabs.filter(tab => tab.groupId === id).length === members.length;
        if (!exactGroup) {
            if (tabs.some(tab => tab.groupId >= 0) || (phase !== base && phase !== groupPhase))
                throw new Error('Reopened window has a conflicting group; native group preserved');
            if (phase === base) {
                await updateWorkspaceMarker(windowId, url, base, groupPhase);
                phase = groupPhase;
            }
            id = await chrome.tabs.group({createProperties: {windowId}, tabIds: members});
        }
        if (phase === groupPhase) {
            await chrome.tabGroups.update(id, {title: group.title ?? '', color: group.color, collapsed: Boolean(group.collapsed)});
            await updateWorkspaceMarker(windowId, url, groupPhase, base);
            phase = base;
        }
    }
    if (phase !== base)
        throw new Error('The interrupted group does not belong to the archived window');
    return phase;
}

async function restoreWindow(payload, originalClosed = false) {
    await loadRestoreAbort(payload.operation_context);
    if (payload.preserve_current_content === true && payload.reuse_only === true)
        return reusePreservedWindow(payload);
    if (payload.window?._preserve_current_content && payload.reuse_only === true)
        throw new Error('Browser preservation requires its explicit placement-only mode');
    if (payload.reuse_only === true)
        return reuseReconciledWindow(payload);
    const windowState = payload.window;
    const scoped = payload.workspace_switch === true;
    const recreating = payload.recreate_window === true;
    if (recreating && (scoped || !payload.restore_token || windowState?.type !== 'normal' ||
        windowState.incognito !== false || !Array.isArray(windowState.groups) || windowState.groups.length ||
        !windowState.tabs?.length || windowState.tabs.some(tab => !tab.url || tab.group != null)))
        throw new Error('Explicit recreation requires a saved ungrouped regular Chrome window');
    if (scoped && (!payload.restore_token || !payload.place_expected || !windowState?.tabs?.length || windowState.incognito ||
        windowState?.type !== 'normal'))
        throw new Error('Invalid saved workspace window');
    const marker = scoped ? workspaceMarkerUrl(payload.marker) : null;
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
            if (scoped && (!record.created || record.workspaceMarker !== marker || !record.windowState ||
                reconciliationSignature(record.windowState) !== reconciliationSignature(windowState)))
                throw new Error('Workspace window claim conflicts with the archived tabs');
            if (record.preservedCatalog) {
                if (('_reconcile_window_id' in windowState && windowState._reconcile_window_id !== record.windowId) ||
                    !preservedBindingMatches(windowState, record.windowState))
                    throw new Error('Browser preservation claim conflicts with the saved recipe; no replacement created');
                await verifyPreservedCatalog(record.preservedCatalog, restoreToken);
                return preservedWindowResult(record.windowId, record.windowState);
            }
            if (!await claimedWindowExists(record.windowId)) {
                await chrome.storage.session.remove(restoreToken);
                return restoreWindow(payload, true);
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
            if (scoped)
                return completeWorkspaceRestore(payload, record.windowId, marker, true);
            return ownedStartupWindowResult(payload, record.windowId, windowState, {
                reused: true,
                created: record.created,
            });
        }
    }

    // Unrelated open windows with matching URLs belong to other activities.
    if (scoped)
        return reopenParkedWindow(payload, marker);
    const existingWindow = await matchingOpenWindow(windowState, restoreToken, payload.restore_catalog);
    if (existingWindow) {
        requireRestoreActive(payload.operation_context);
        await rememberRestoredWindow(restoreToken, existingWindow.id, false, windowState, payload.operation_context);
        return ownedStartupWindowResult(payload, existingWindow.id, windowState, {
            reused: true,
            created: false,
        });
    }

    if (windowState.groups?.length)
        throw new Error('Original grouped Chrome window does not match the saved tabs; no replacement window created. Recover the original window and its existing groups.');

    if (!originalClosed || recreating)
        await requireAbsentOriginal(windowState, restoreToken, payload.restore_catalog, recreating);

    let createdWindow = null;
    try {
        requireRestoreActive(payload.operation_context);
        createdWindow = await chrome.windows.create(createData);
        if (!createdWindow?.id)
            throw new Error('Chrome did not return the newly created window');
        await rememberRestoredWindow(restoreToken, createdWindow.id, true, windowState, payload.operation_context);
        requireRestoreActive(payload.operation_context);
        const populatedWindow = await chrome.windows.get(createdWindow.id, {populate: true});
        const restoredTabs = [];
        const placeholder = populatedWindow.tabs?.[0];
        if (!placeholder?.id)
            throw new Error('Chrome did not create the initial tab');
        if (creationToken) {
            for (let attempt = 0; attempt < 50; attempt += 1) {
                requireRestoreActive(payload.operation_context);
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
            requireRestoreActive(payload.operation_context);
            let restored = await setTabUrl(placeholder.id, first.url, warnings);
            if (first.pinned) {
                requireRestoreActive(payload.operation_context);
                restored = await pinTab(restored, warnings);
            }
            restoredTabs.push(restored);
            for (const tabState of savedTabs.slice(1)) {
                requireRestoreActive(payload.operation_context);
                restoredTabs.push(await createTab(createdWindow.id, tabState, warnings, payload.operation_context));
            }
        } else {
            restoredTabs.push(placeholder);
        }

        const activeIndex = savedTabs.findIndex(tab => tab.active);
        if (activeIndex >= 0 && restoredTabs[activeIndex]) {
            try {
                requireRestoreActive(payload.operation_context);
                await chrome.tabs.update(restoredTabs[activeIndex].id, {active: true});
            } catch (error) {
                warnings.push(`Could not activate saved tab: ${error.message}`);
            }
        }
        if (!payload.place_expected && windowState.state && windowState.state !== 'normal') {
            try {
                requireRestoreActive(payload.operation_context);
                await chrome.windows.update(createdWindow.id, {state: normalizeWindowState(windowState.state)});
            } catch (error) {
                warnings.push(`Could not restore window state: ${error.message}`);
            }
        }
        return await ownedStartupWindowResult(payload, createdWindow.id, windowState, {warnings, created: true});
    } catch (error) {
        // The caller can disappear while Chrome is still opening tabs. Keep
        // any claimed window for inspection/retry, including partial creation.
        // Closing it here could discard user edits made during the operation;
        // deleting its claim could make the next attempt create a duplicate.
        if (createdWindow?.id)
            throw new Error(`Chrome window ${createdWindow.id} preserved for inspection after interrupted restore: ${error.message}`);
        throw error;
    }
}

async function focusWindow(payload) {
    const windowId = Number(payload.window_id);
    if (!Number.isInteger(windowId))
        throw new Error('window_id is required');
    return identificationTask(async () => {
        if (payload.token !== undefined) {
            if (typeof payload.token !== 'string' || !payload.token)
                throw new Error('A nonempty identification token is required for scoped focus');
            const markers = await identificationLeases();
            const marker = markers[payload.token];
            if (marker && (!ownedIdentification(marker) || marker.window_id !== windowId))
                throw new Error('Focus token belongs to another identification window');
            if (!marker) {
                const stored = await chrome.storage.session.get(FOCUS_LEASE_KEY);
                const leases = stored[FOCUS_LEASE_KEY] ?? {};
                if (leases[payload.token] && leases[payload.token].window_id !== windowId)
                    throw new Error('Focus token belongs to another native window');
                leases[payload.token] = {window_id: windowId, token: payload.token,
                    expires_at: Date.now() + IDENTIFICATION_LEASE_MS};
                await chrome.storage.session.set({[FOCUS_LEASE_KEY]: leases});
            }
        }
        await chrome.windows.update(windowId, {focused: true});
        const window = await chrome.windows.get(windowId, {populate: true});
        return browserWindowSummary(window);
    });
}

function isIdentificationPage(value) {
    try {
        const url = new URL(value);
        return `${url.protocol}//${url.host}${url.pathname}` === IDENTIFY_PAGE;
    } catch (_) {
        return false;
    }
}

function hasIdentificationPage(tab) {
    return isIdentificationPage(tab.url) || isIdentificationPage(tab.pendingUrl);
}

function identificationTask(callback) {
    const task = identificationQueue.then(callback);
    identificationQueue = task.catch(() => {});
    return task;
}

async function identificationLeases() {
    const stored = await chrome.storage.session.get(IDENTIFICATION_LEASE_KEY);
    const current = stored[IDENTIFICATION_LEASE_KEY] ?? {};
    // Chrome can restore the marker after a browser crash while session
    // storage (and every numeric tab/window identity) has been replaced.
    // Only a unique exact URL from our durable ownership journal can be
    // adopted; never infer ownership from the identify.html path alone.
    const journal = await chrome.storage.local.get(IDENTIFICATION_LEASE_KEY);
    const leases = journal[IDENTIFICATION_LEASE_KEY] ?? {};
    if (!Object.values(leases).some(ownedIdentification))
        return leases;
    const windows = await chrome.windows.getAll({populate: true, windowTypes: ['normal', 'popup']});
    return Object.fromEntries(Object.entries(leases).map(([token, lease]) => {
        if (!ownedIdentification(lease))
            return [token, lease];
        // The durable journal is authoritative even if a session-storage write
        // was interrupted. Session data only corroborates this browser's IDs.
        const observed = current[token];
        if (ownedIdentification(observed) && observed.window_id === lease.window_id &&
            observed.marker_tab_id === lease.marker_tab_id &&
            observed.previous_active_tab_id === lease.previous_active_tab_id)
            return [token, lease];
        const matches = windows.flatMap(window => (window.tabs ?? [])
            .filter(tab => exactIdentificationTab(tab, lease)).map(tab => ({window, tab})));
        if (matches.length === 1) {
            const {window, tab} = matches[0];
            return [token, {...lease, window_id: window.id, marker_tab_id: tab.id,
                previous_active_tab_id: null}];
        }
        return [token, {...lease, previous_active_tab_id: null, ambiguous: matches.length > 1}];
    }));
}

function ownedIdentification(lease) {
    return lease?.kind === 'identification-lease-v1' && Number.isInteger(lease.window_id) &&
        typeof lease.token === 'string' && Boolean(lease.token) && Number.isFinite(lease.expires_at) &&
        lease.marker_url === `${IDENTIFY_PAGE}?token=${encodeURIComponent(lease.token)}` &&
        (lease.marker_tab_id === null || Number.isInteger(lease.marker_tab_id));
}

function exactIdentificationTab(tab, lease) {
    return tab && (tab.url === lease.marker_url || (!tab.url && tab.pendingUrl === lease.marker_url)) &&
        (!tab.pendingUrl || tab.pendingUrl === lease.marker_url);
}

function scheduleIdentificationCleanup(leases) {
    clearTimeout(identificationTimer);
    identificationTimer = null;
    const deadlines = Object.values(leases).filter(lease => ownedIdentification(lease) &&
        lease.automatic_cleanup_expiry !== lease.expires_at).map(lease => lease.expires_at);
    if (deadlines.length) {
        // The native connection normally keeps this worker alive. Session
        // storage and explicit expiry cleanup cover worker suspension/restart.
        identificationTimer = setTimeout(() => {
            identificationTimer = null;
            return dispatch({action: 'release_expired_identifications', payload: {automatic: true}}).catch(() => {});
        }, Math.max(5000, Math.min(...deadlines) - Date.now()));
    }
}

async function saveIdentificationLeases(leases) {
    await chrome.storage.local.set({[IDENTIFICATION_LEASE_KEY]: leases});
    await chrome.storage.session.set({[IDENTIFICATION_LEASE_KEY]: leases});
    scheduleIdentificationCleanup(leases);
}

async function identificationStatus(windows, leases = null) {
    leases ??= await identificationLeases();
    const result = {active_identifications: 0, live_identifications: 0,
        expired_identifications: 0, unowned_identifications: 0};
    for (const window of windows) {
        for (const tab of (window.tabs ?? []).filter(hasIdentificationPage)) {
            result.active_identifications += 1;
            const lease = Object.values(leases).find(item => ownedIdentification(item) &&
                item.window_id === window.id && item.marker_tab_id === tab.id && exactIdentificationTab(tab, item));
            if (!lease)
                result.unowned_identifications += 1;
            else if (lease.expires_at > Date.now())
                result.live_identifications += 1;
            else
                result.expired_identifications += 1;
        }
    }
    return result;
}

async function removeOwnedIdentification(lease) {
    // A missing reply is not proof that a tab closed. Query the window again,
    // retain failed cleanup for retry, and never remove navigated or sole tabs.
    if (lease.ambiguous)
        return {released: false, reason: 'Restored identification marker is ambiguous'};
    const windows = await chrome.windows.getAll({populate: true, windowTypes: ['normal', 'popup']});
    const window = windows.find(item => item.id === lease.window_id);
    if (!window)
        return {released: true};
    const candidates = (window.tabs ?? []).filter(tab => Number.isInteger(lease.marker_tab_id)
        ? tab.id === lease.marker_tab_id : exactIdentificationTab(tab, lease));
    if (candidates.length > 1)
        return {released: false, reason: 'Identification marker is ambiguous'};
    const marker = candidates[0];
    if (!exactIdentificationTab(marker, lease))
        return {released: true}; // The user navigated or closed it; preserve their page/selection.
    if (window.tabs.length <= 1 || marker.pinned || (marker.groupId ?? -1) >= 0)
        return {released: false, reason: 'Identification marker is the last tab, pinned or grouped'};
    if (marker.active && Number.isInteger(lease.previous_active_tab_id)) {
        let previous;
        try { previous = await chrome.tabs.get(lease.previous_active_tab_id); }
        catch (_) { /* The previously active tab may have closed. */ }
        if (previous && previous.id !== marker.id) {
            const current = await chrome.tabs.get(marker.id);
            if (previous.windowId === lease.window_id && current.windowId === lease.window_id &&
                current.active && exactIdentificationTab(current, lease))
                await chrome.tabs.update(previous.id, {active: true});
        }
    }
    const currentTabs = await chrome.tabs.query({windowId: lease.window_id});
    const current = currentTabs.find(tab => tab.id === marker.id);
    if (!exactIdentificationTab(current, lease))
        return {released: true};
    if (currentTabs.length <= 1 || current.pinned || (current.groupId ?? -1) >= 0)
        return {released: false, reason: 'Identification marker is the last tab, pinned or grouped'};
    let removalError = null;
    try { await chrome.tabs.remove(marker.id); } catch (error) { removalError = error.message; }
    const after = await chrome.windows.getAll({populate: true, windowTypes: ['normal', 'popup']});
    if (after.some(item => (item.tabs ?? []).some(tab => tab.id === marker.id)))
        return {released: false, reason: removalError ?? 'Chrome still has the identification marker'};
    return {released: true};
}

async function identifyWindow(payload) {
    return identificationTask(async () => {
        const windowId = Number(payload.window_id);
        const token = String(payload.token ?? '');
        if (!Number.isInteger(windowId) || !token)
            throw new Error('window_id and token are required');
        const before = await chrome.windows.get(windowId, {populate: true});
        const previousActiveTab = (workspaceContent(before).tabs ?? []).find(tab => tab.active);
        if (before.type === 'popup') {
            await chrome.windows.update(windowId, {focused: true});
            return {window_id: windowId, marker_tab_id: null, previous_active_tab_id: null,
                token, strategy: 'focused_popup', active_title: previousActiveTab?.title ?? ''};
        }
        const leases = await identificationLeases();
        if (leases[token] || (before.tabs ?? []).some(hasIdentificationPage) ||
            Object.values(leases).some(item => ownedIdentification(item) && item.window_id === windowId))
            throw new Error('Window already has an identification lease; release it before identifying again');
        const lease = {kind: 'identification-lease-v1', window_id: windowId, token,
            marker_tab_id: null, previous_active_tab_id: previousActiveTab?.id ?? null,
            marker_url: `${IDENTIFY_PAGE}?token=${encodeURIComponent(token)}`,
            expires_at: Date.now() + IDENTIFICATION_LEASE_MS};
        leases[token] = lease;
        // Record intent first, so cancellation or a lost tabs.create reply can
        // still release this exact token without guessing a user's tab ID.
        await saveIdentificationLeases(leases);
        try {
            const marker = await chrome.tabs.create({windowId, url: lease.marker_url, active: true});
            if (!Number.isInteger(marker.id))
                throw new Error('Chrome did not create the identification tab in the requested window');
            lease.marker_tab_id = marker.id;
            if (marker.windowId !== windowId) {
                // Keep ownership of a misplaced marker without ever restoring
                // a previous-active ID from the originally requested window.
                lease.window_id = marker.windowId;
                lease.previous_active_tab_id = null;
                await saveIdentificationLeases(leases);
                throw new Error('Chrome did not create the identification tab in the requested window');
            }
            await saveIdentificationLeases(leases);
            if (payload.focus !== false)
                await chrome.windows.update(windowId, {focused: true});
            for (let attempt = 0; attempt < 50; attempt += 1) {
                const current = await chrome.tabs.get(marker.id);
                if (exactIdentificationTab(current, lease) && (current.title ?? '').includes(token)) {
                    lease.expires_at = Date.now() + IDENTIFICATION_LEASE_MS;
                    await saveIdentificationLeases(leases);
                    return {window_id: windowId, marker_tab_id: marker.id,
                        previous_active_tab_id: lease.previous_active_tab_id, token, expires_at: lease.expires_at};
                }
                await new Promise(resolve => setTimeout(resolve, 20));
            }
            throw new Error('Chrome identification tab did not become ready');
        } catch (error) {
            try {
                if ((await removeOwnedIdentification(lease)).released)
                    delete leases[token];
            } finally {
                await saveIdentificationLeases(leases);
            }
            throw error;
        }
    });
}

async function releaseWindowIdentification(payload) {
    return identificationTask(async () => {
        if (payload.strategy === 'focused_popup' && payload.marker_tab_id == null) {
            if (!Number.isInteger(payload.window_id) || typeof payload.token !== 'string' || !payload.token)
                return {released: false, reason: 'Exact window and focus token are required'};
            const stored = await chrome.storage.session.get(FOCUS_LEASE_KEY);
            const leases = stored[FOCUS_LEASE_KEY] ?? {};
            const lease = leases[payload.token];
            if (lease && lease.window_id !== payload.window_id)
                return {released: false, reason: 'Focus lease does not match the requested window'};
            delete leases[payload.token];
            await chrome.storage.session.set({[FOCUS_LEASE_KEY]: leases});
            return {released: true};
        }
        if (!Number.isInteger(payload.window_id) || typeof payload.token !== 'string' || !payload.token)
            return {released: false, reason: 'Exact window and identification token are required'};
        const leases = await identificationLeases();
        let lease = leases[payload.token];
        if (lease && (!ownedIdentification(lease) || lease.window_id !== payload.window_id ||
            (Number.isInteger(payload.marker_tab_id) && lease.marker_tab_id !== payload.marker_tab_id)))
            return {released: false, reason: 'Identification lease does not match the requested marker'};
        if (!lease) {
            if (!Number.isInteger(payload.marker_tab_id))
                return {released: true};
            // Explicit legacy cleanup requires the exact returned token and tab.
            // Never trust an unrecorded previous-active ID to change selection.
            lease = {window_id: payload.window_id, marker_tab_id: payload.marker_tab_id,
                marker_url: `${IDENTIFY_PAGE}?token=${encodeURIComponent(payload.token)}`};
            const tabs = await chrome.tabs.query({windowId: payload.window_id});
            const tab = tabs.find(item => item.id === payload.marker_tab_id);
            if (tab && !exactIdentificationTab(tab, lease))
                return {released: false, reason: 'Identification token no longer matches that tab'};
        }
        let result;
        try { result = await removeOwnedIdentification(lease); }
        catch (error) { result = {released: false, reason: error.message}; }
        if (result.released)
            delete leases[payload.token];
        await saveIdentificationLeases(leases);
        return result;
    });
}

async function renewWindowIdentification(payload) {
    return identificationTask(async () => {
        const leases = await identificationLeases();
        const lease = leases[payload.token];
        if (!ownedIdentification(lease) || lease.window_id !== payload.window_id ||
            lease.marker_tab_id !== payload.marker_tab_id || lease.expires_at <= Date.now())
            throw new Error('Live matching identification lease is required for renewal');
        const tab = await chrome.tabs.get(lease.marker_tab_id);
        if (tab.windowId !== lease.window_id || !exactIdentificationTab(tab, lease))
            throw new Error('Identification marker changed before renewal');
        lease.expires_at = Date.now() + IDENTIFICATION_LEASE_MS;
        await saveIdentificationLeases(leases);
        return {renewed: true, expires_at: lease.expires_at};
    });
}

async function releaseExpiredIdentifications({automatic = false} = {}) {
    return identificationTask(async () => {
        const leases = await identificationLeases();
        let released = 0;
        const errors = [];
        for (const [token, lease] of Object.entries(leases)) {
            if (!ownedIdentification(lease) || lease.expires_at > Date.now())
                continue;
            if (automatic && lease.automatic_cleanup_expiry === lease.expires_at)
                continue;
            try {
                if (automatic) {
                    // Try once per expiry, including across worker restarts.
                    // Persistent errors remain visible for explicit cleanup;
                    // do not run an endless background mutation loop.
                    lease.automatic_cleanup_expiry = lease.expires_at;
                    await saveIdentificationLeases(leases);
                }
                const result = await removeOwnedIdentification(lease);
                if (result.released) {
                    delete leases[token];
                    released += 1;
                } else {
                    errors.push(result.reason);
                }
            } catch (error) { errors.push(error.message); }
        }
        await saveIdentificationLeases(leases);
        const windows = await chrome.windows.getAll({populate: true, windowTypes: ['normal', 'popup']});
        return {released, errors, ...await identificationStatus(windows, leases)};
    });
}

async function restoredWindowStatus(payload) {
    const restoreToken = payload.restore_token;
    if (!restoreToken)
        throw new Error('restore_token is required');
    const stored = await chrome.storage.session.get(restoreToken);
    let record = restoreRecord(stored[restoreToken]);
    if (payload.marker !== undefined) {
        const url = workspaceMarkerUrl(payload.marker);
        if (record && record.workspaceMarker !== url)
            throw new Error('Workspace status does not match the claimed window');
        if (!record) {
            const windows = markedWorkspaceWindows(await chrome.windows.getAll({populate: true}), url);
            if (windows.length > 1)
                throw new Error('More than one open Chrome window carries the workspace marker');
            if (windows.length && payload.window) {
                const marker = requireWorkspaceMarker(windows[0], url, true);
                record = {windowId: windows[0].id, created: true, windowState: payload.window,
                    workspaceMarker: url, workspacePlaced: marker.phase === 'placed'};
            }
        }
    }
    if (!record)
        return {exists: false};
    if (!await claimedWindowExists(record.windowId)) {
        if (record.preservedCatalog)
            throw new Error('Browser reconciliation needs review: preserved native window is missing; no replacement created');
        await chrome.storage.session.remove(restoreToken);
        return {exists: false};
    }
    if (record.preservedCatalog) {
        if (payload.window && (('_reconcile_window_id' in payload.window && payload.window._reconcile_window_id !== record.windowId) ||
                               !preservedBindingMatches(payload.window, record.windowState)))
            throw new Error('Browser preservation status conflicts with the saved placement recipe');
        await verifyPreservedCatalog(record.preservedCatalog, restoreToken);
        return {exists: true, ...preservedWindowResult(record.windowId, record.windowState)};
    }
    const {native_window: sample, ...verification} = await verifyRestoredUrls(
        record.windowId, payload.window ?? record.windowState, false, record.operation_context, record.workspaceMarker,
    );
    if ((await claimedWindowIds(restoreToken)).has(record.windowId)) {
        verification.urls_restored = false;
        verification.urls_pending = false;
        verification.url_errors.push('Restored window is also claimed by another saved window');
    }
    const groups = sample ? await verifyRestoredGroups(
        record.windowId, payload.window ?? record.windowState, sample, Boolean(record.workspaceMarker)) :
        {groups_reused: 0, group_warnings: verification.verification_deferred ? [] :
            ['Restored window sample unavailable for group verification']};
    const key = `${restoreToken}:${operationKey(record.operation_context)}`;
    const abortState = await chrome.storage.session.get(ABORTED_RESTORES_KEY);
    for (const abortedKey of abortState[ABORTED_RESTORES_KEY] ?? [])
        abortedRestores.add(abortedKey);
    const aborted = abortedRestores.has(operationKey(record.operation_context));
    const pending = pendingRestoreVerifications.has(key) && !aborted;
    if (pending && verification.urls_restored) {
        verification.urls_restored = false;
        verification.urls_pending = true;
    }
    if (aborted) {
        verification.urls_restored = false;
        verification.urls_pending = false;
        verification.url_errors.push('Chrome restore operation was aborted; window preserved');
    }
    return {
        exists: true,
        ...(record.operation_context ? {operation_context: record.operation_context, aborted, verification_pending: pending} : {}),
        window_id: record.windowId,
        created: record.created,
        ...(record.workspaceMarker ? {
            workspace_placed: record.workspacePlaced && !await workspacePlacementResetPending(record.workspaceMarker),
        } : {}),
        ...verification,
        ...groups,
        warnings: [...verification.url_errors, ...groups.group_warnings],
    };
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
            ...await identificationStatus(windows),
            window_count: windows.length,
            build: {revision: BUILD_REVISION},
            activation_pending: activationPending,
            capabilities: [
                'capture',
                'list_windows',
                'restore_window',
                'recreate_window',
                'restore_status',
                'identify_window',
                'focus_window',
                'release_window_identification',
                'scoped_creation_marker',
                'workspace_switch',
                'exact_url_restore',
                'exact_url_pending',
                'reuse_only_groups',
                'installed_build_activation',
                'lazy_tab_restore',
                'original_groups_required',
                'unclaimed_original_guard',
                'exact_capture_identity',
                'runtime_build',
                'native_mutation_status',
                'reconciliation_reuse_only',
                'preserve_native_fragment_navigation',
                'identification_lease_lifecycle',
                'asynchronous_restore_verification',
                'cooperative_restore_abort',
            ],
        };
    }
    case 'capture':
        return captureBrowser(message.payload ?? {});
    case 'list_windows':
        return listBrowserWindows();
    case 'confirm_workspace_placement':
    case 'prepare_workspace_window':
    case 'cancel_workspace_window':
    case 'restore_window':
    case 'recreate_window': {
        const handler = {
            confirm_workspace_placement: confirmWorkspacePlacement,
            prepare_workspace_window: prepareWorkspaceWindow,
            cancel_workspace_window: cancelWorkspaceWindow,
            restore_window: restoreWindow,
            recreate_window: payload => restoreWindow({...payload, recreate_window: true}),
        }[message.action];
        const task = restoreQueue.then(() => handler(message.payload ?? {}));
        restoreQueue = task.catch(() => {});
        return task;
    }
    case 'abort_restore':
        return abortRestore(message.payload ?? {});
    case 'restore_status':
        return restoredWindowStatus(message.payload ?? {});
    case 'identify_window':
        return identifyWindow(message.payload ?? {});
    case 'focus_window':
        return focusWindow(message.payload ?? {});
    case 'release_window_identification':
        return releaseWindowIdentification(message.payload ?? {});
    case 'release_expired_identifications':
        return releaseExpiredIdentifications(message.payload ?? {});
    case 'renew_window_identification':
        return renewWindowIdentification(message.payload ?? {});
    default:
        throw new Error(`Unknown wsctl action: ${message.action}`);
    }
}

async function dispatch(message) {
    const mutating = MUTATING_ACTIONS.has(message.action);
    if (mutating && activationPending && !['release_window_identification', 'release_expired_identifications',
        'renew_window_identification'].includes(message.action))
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
        scheduleIdentificationCleanup(await identificationLeases());
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
