import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const BUS_NAME = 'org.sagecat.WorkspaceState';
const OBJECT_PATH = '/org/sagecat/WorkspaceState';
const IFACE = `
<node>
  <interface name="org.sagecat.WorkspaceState">
    <method name="Capture">
      <arg type="s" direction="out" name="state"/>
    </method>
    <method name="ListWindows">
      <arg type="s" direction="out" name="windows"/>
    </method>
    <method name="PlaceNextWindow">
      <arg type="s" direction="in" name="app_id"/>
      <arg type="u" direction="in" name="workspace"/>
      <arg type="u" direction="in" name="monitor"/>
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
      <arg type="i" direction="in" name="width"/>
      <arg type="i" direction="in" name="height"/>
      <arg type="s" direction="in" name="state"/>
      <arg type="s" direction="out" name="expectation_id"/>
    </method>
    <method name="PlacementStatus">
      <arg type="s" direction="in" name="expectation_id"/>
      <arg type="s" direction="out" name="status"/>
    </method>
    <method name="CancelPlacement">
      <arg type="s" direction="in" name="expectation_id"/>
      <arg type="b" direction="out" name="cancelled"/>
    </method>
    <method name="MoveWindow">
      <arg type="u" direction="in" name="window_id"/>
      <arg type="u" direction="in" name="workspace"/>
      <arg type="u" direction="in" name="monitor"/>
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
      <arg type="i" direction="in" name="width"/>
      <arg type="i" direction="in" name="height"/>
      <arg type="s" direction="in" name="state"/>
      <arg type="b" direction="out" name="moved"/>
    </method>
    <method name="PlaceByTitle">
      <arg type="s" direction="in" name="title"/>
      <arg type="u" direction="in" name="workspace"/>
      <arg type="u" direction="in" name="monitor"/>
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
      <arg type="i" direction="in" name="width"/>
      <arg type="i" direction="in" name="height"/>
      <arg type="u" direction="in" name="maximized"/>
      <arg type="b" direction="out" name="placed"/>
    </method>
  </interface>
</node>`;

const EXPECTATION_LIFETIME_US = 20 * GLib.USEC_PER_SEC;

function windows() {
    return global.get_window_actors()
        .map(actor => actor.meta_window)
        .filter(window => window && window.get_window_type() === Meta.WindowType.NORMAL);
}

function normalizeAppId(value) {
    const normalized = (value ?? '').trim().toLowerCase();
    return normalized.endsWith('.desktop') ? normalized.slice(0, -8) : normalized;
}

function appIds(window) {
    const values = [
        window.get_gtk_application_id?.(),
        window.get_wm_class?.(),
        window.get_wm_class_instance?.(),
        window.get_sandboxed_app_id?.(),
    ];
    return values.filter(Boolean).map(normalizeAppId);
}

function windowState(window) {
    if (window.is_fullscreen())
        return 'fullscreen';
    if (window.minimized)
        return 'minimized';
    if (window.get_maximized() !== 0)
        return 'maximized';
    return 'normal';
}

export default class WorkspaceStateExtension extends Extension {
    enable() {
        this._expectations = [];
        this._statuses = new Map();
        this._retrySources = new Set();
        this._nextExpectation = 1;
        this._windowCreatedId = global.display.connect(
            'window-created',
            (_display, window) => this._windowCreated(window),
        );
        this._exported = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._exported.export(Gio.DBus.session, OBJECT_PATH);
        this._ownerId = Gio.bus_own_name_on_connection(
            Gio.DBus.session,
            BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            null,
            null,
        );
    }

    disable() {
        if (this._windowCreatedId)
            global.display.disconnect(this._windowCreatedId);
        this._windowCreatedId = 0;
        for (const source of this._retrySources)
            GLib.source_remove(source);
        this._retrySources.clear();
        this._expectations = [];
        this._statuses.clear();
        if (this._ownerId)
            Gio.bus_unown_name(this._ownerId);
        this._ownerId = 0;
        if (this._exported)
            this._exported.unexport();
        this._exported = null;
    }

    _monitors() {
        return Main.layoutManager.monitors.map(monitor => ({
            index: monitor.index,
            x: monitor.x,
            y: monitor.y,
            width: monitor.width,
            height: monitor.height,
            scale: global.display.get_monitor_scale(monitor.index),
            primary: monitor.index === Main.layoutManager.primaryIndex,
        }));
    }

    _windows(monitors) {
        return windows().map(window => {
            const rect = window.get_frame_rect();
            const monitor = window.get_monitor();
            return {
                id: window.get_stable_sequence(),
                pid: window.get_pid(),
                title: window.get_title() ?? '',
                wm_class: window.get_wm_class() ?? '',
                wm_class_instance: window.get_wm_class_instance?.() ?? '',
                app_id: window.get_gtk_application_id?.() ?? '',
                sandboxed_app_id: window.get_sandboxed_app_id?.() ?? '',
                workspace: window.get_workspace()?.index() ?? 0,
                monitor,
                monitor_geometry: monitors.find(item => item.index === monitor) ?? null,
                geometry: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                state: windowState(window),
                maximized: window.get_maximized(),
                fullscreen: window.is_fullscreen(),
                active: window.has_focus(),
            };
        });
    }

    Capture() {
        const monitors = this._monitors();
        const captured = this._windows(monitors);
        const activeWorkspace = global.workspace_manager.get_active_workspace_index();
        return JSON.stringify({monitors, windows: captured, active_workspace: activeWorkspace});
    }

    ListWindows() {
        return JSON.stringify(this._windows(this._monitors()));
    }

    _applyPlacement(window, workspaceIndex, monitor, x, y, width, height, state) {
        const workspaceManager = global.workspace_manager;
        const monitorCount = Main.layoutManager.monitors.length;
        if (workspaceManager.n_workspaces < 1 || monitorCount < 1)
            return false;
        const safeWorkspace = Math.min(workspaceIndex, workspaceManager.n_workspaces - 1);
        const safeMonitor = Math.min(monitor, monitorCount - 1);
        window.unminimize();
        if (window.is_fullscreen())
            window.unmake_fullscreen();
        window.unmaximize(Meta.MaximizeFlags.BOTH);
        window.change_workspace(workspaceManager.get_workspace_by_index(safeWorkspace));
        window.move_to_monitor(safeMonitor);
        window.move_resize_frame(true, x, y, width, height);
        if (state === 'maximized')
            window.maximize(Meta.MaximizeFlags.BOTH);
        else if (state === 'fullscreen')
            window.make_fullscreen();
        else if (state === 'minimized')
            window.minimize();
        return true;
    }

    _expirePlacements() {
        const now = GLib.get_monotonic_time();
        for (const expectation of this._expectations) {
            if (now - expectation.createdAt > EXPECTATION_LIFETIME_US)
                this._statuses.set(expectation.id, 'expired');
        }
        this._expectations = this._expectations.filter(
            expectation => this._statuses.get(expectation.id) === 'pending',
        );
    }

    _windowCreated(window) {
        let attempts = 0;
        const source = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 50, () => {
            attempts += 1;
            this._expirePlacements();
            if (!this._expectations.length || window.get_window_type() !== Meta.WindowType.NORMAL) {
                this._retrySources.delete(source);
                return GLib.SOURCE_REMOVE;
            }
            const ids = appIds(window);
            const index = this._expectations.findIndex(expectation => ids.includes(expectation.appId));
            if (index < 0 && attempts < 60)
                return GLib.SOURCE_CONTINUE;
            if (index < 0) {
                this._retrySources.delete(source);
                return GLib.SOURCE_REMOVE;
            }
            const [expectation] = this._expectations.splice(index, 1);
            const placed = this._applyPlacement(
                window,
                expectation.workspace,
                expectation.monitor,
                expectation.x,
                expectation.y,
                expectation.width,
                expectation.height,
                expectation.state,
            );
            this._statuses.set(expectation.id, placed ? 'placed' : 'failed');
            this._retrySources.delete(source);
            return GLib.SOURCE_REMOVE;
        });
        this._retrySources.add(source);
    }

    PlaceNextWindow(appId, workspace, monitor, x, y, width, height, state) {
        this._expirePlacements();
        const id = String(this._nextExpectation++);
        this._expectations.push({
            id,
            appId: normalizeAppId(appId),
            workspace,
            monitor,
            x,
            y,
            width,
            height,
            state,
            createdAt: GLib.get_monotonic_time(),
        });
        this._statuses.set(id, 'pending');
        return id;
    }

    PlacementStatus(id) {
        this._expirePlacements();
        return this._statuses.get(id) ?? 'unknown';
    }

    CancelPlacement(id) {
        const index = this._expectations.findIndex(expectation => expectation.id === id);
        if (index < 0)
            return false;
        this._expectations.splice(index, 1);
        this._statuses.set(id, 'cancelled');
        return true;
    }

    MoveWindow(windowId, workspace, monitor, x, y, width, height, state) {
        const window = windows().find(candidate => candidate.get_stable_sequence() === windowId);
        if (!window)
            return false;
        return this._applyPlacement(window, workspace, monitor, x, y, width, height, state);
    }

    PlaceByTitle(title, workspace, monitor, x, y, width, height, maximized) {
        const window = windows().find(candidate => candidate.get_title() === title);
        if (!window)
            return false;
        const state = maximized ? 'maximized' : 'normal';
        return this._applyPlacement(window, workspace, monitor, x, y, width, height, state);
    }
}
