import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {edgeTopology, warpTarget} from './edgePolicy.js';
import {BUILD_REVISION} from './buildInfo.js';

const XML = `<node><interface name="org.sagecat.LgEdgeWarp">
<method name="GetState"><arg type="s" direction="out"/></method>
</interface></node>`;

export default class LgEdgeWarpExtension extends Extension {
    enable() {
        const epoch = this._epoch = (this._epoch ?? 0) + 1;
        try {
            this._seat = Clutter.get_default_backend().get_default_seat();
            this._lastWarp = 0;
            const refresh = () => {
                if (this._epoch === epoch)
                    this._topology = edgeTopology(Main.layoutManager.monitors,
                        Main.layoutManager.primaryMonitor);
            };
            refresh();
            this._monitorsChanged = Main.layoutManager.connect('monitors-changed', refresh);
            this._dbus = Gio.DBusExportedObject.wrapJSObject(XML, this);
            this._dbus.export(Gio.DBus.session, '/org/sagecat/LgEdgeWarp');
            // Preserve the measured pointer responsiveness. Only topology
            // changes now allocate/sort geometry; idle polling remains tiny.
            this._pollSource = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 16, () => {
                if (this._epoch !== epoch)
                    return GLib.SOURCE_REMOVE;
                const [x, y] = global.get_pointer();
                const now = GLib.get_monotonic_time() / 1000;
                const target = warpTarget(this._topology, x, y, now - this._lastWarp);
                if (target && this._seat) {
                    this._seat.warp_pointer(...target);
                    this._lastWarp = now;
                }
                return GLib.SOURCE_CONTINUE;
            });
        } catch (error) {
            this.disable();
            throw error;
        }
    }

    GetState() {
        return JSON.stringify({enabled: Boolean(this._seat), poll_interval_ms: 16,
            topology_cached: Boolean(this._topology), build: {uuid: this.uuid,
                version: this.metadata.version, revision: BUILD_REVISION,
                sourceIdentityKnown: BUILD_REVISION !== 'development'}});
    }

    disable() {
        this._epoch = (this._epoch ?? 0) + 1;
        if (this._pollSource)
            GLib.source_remove(this._pollSource);
        this._pollSource = 0;
        if (this._monitorsChanged)
            Main.layoutManager.disconnect(this._monitorsChanged);
        this._monitorsChanged = 0;
        this._dbus?.unexport();
        this._dbus = null;
        this._topology = null;
        this._seat = null;
    }
}
