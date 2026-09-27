"""Small, fail-closed Nemo extension used by workspace-state.

The extension deliberately observes only widgets installed by ``get_widget``;
it does not scrape Nemo's labels or invoke private C APIs.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import weakref
from pathlib import Path
from dataclasses import dataclass
from typing import Callable

try:  # Nemo imports this module in its GTK process; unit tests may mock GI.
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Nemo", "3.0")
    from gi.repository import Gio, GLib, GObject, Gtk, Nemo
except Exception:  # pragma: no cover - exercised by environments without GI
    Gio = GLib = GObject = Gtk = Nemo = None


try:
    # Resolve the loaded bridge file once; the current release pointer may change later.
    BUILD_REVISION = json.loads(Path(__file__).resolve().with_name("build-info.json").read_text())["revision"]
except (OSError, ValueError, KeyError):
    BUILD_REVISION = "development"


BUS_NAME = "org.sagecat.WorkspaceState.Nemo1"
OBJECT_PATH = "/org/sagecat/WorkspaceState/Nemo1"
IFACE = BUS_NAME
_XML = """<node><interface name='org.sagecat.WorkspaceState.Nemo1'>
<method name='GetState'><arg name='result' type='s' direction='out'/></method>
<method name='Identify'><arg name='lease' type='s' direction='in'/><arg name='result' type='s' direction='out'/></method>
<method name='Release'><arg name='lease' type='s' direction='in'/><arg name='result' type='b' direction='out'/></method>
<method name='SelectTab'><arg name='id' type='u' direction='in'/><arg name='index' type='u' direction='in'/><arg name='result' type='b' direction='out'/></method>
<method name='CloseWindow'><arg name='id' type='u' direction='in'/><arg name='expected' type='s' direction='in'/><arg name='result' type='b' direction='out'/></method>
</interface></node>"""


def _children(widget):
    try:
        return list(widget.get_children())
    except Exception:
        return []


def _visible(widget):
    try:
        return bool(widget.get_visible())
    except Exception:
        return False


def _walk(widget):
    yield widget
    for child in _children(widget):
        yield from _walk(child)


def _window_id(window):
    try:
        return int(window.get_id())
    except Exception:
        return None


@dataclass
class _Window:
    ref: Callable
    widgets: weakref.WeakSet
    original_title: str | None = None
    tagged_title: str | None = None


class NemoBridge:
    """State and lease logic; all methods must run on Nemo's GTK thread."""

    def __init__(self):
        self._windows: dict[int, _Window] = {}
        self._lease: str | None = None
        self._timer = None
        self._lock = threading.RLock()

    def get_widget(self, uri, window):
        if Gtk is None or _window_id(window) is None or not self._is_nemo_window(window):
            return None
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        box.set_size_request(0, 0)
        box.set_no_show_all(True)
        box.hide()
        # Plain Python attributes are intentional: no Nemo ABI assumptions.
        box.wsctl_nemo_uri = str(uri)
        box.wsctl_nemo_provider = True
        wid = _window_id(window)
        entry = self._windows.get(wid)
        if entry is None:
            # A weak reference to an otherwise unowned PyGObject wrapper dies
            # even while its C GtkWindow is alive. Hold it until destroy.
            entry = _Window(lambda: window, weakref.WeakSet())
            self._windows[wid] = entry
            try:
                window.connect("destroy", lambda *_: self._windows.pop(wid, None))
            except Exception:
                pass
        entry.widgets.add(box)
        return box

    def _destroyed(self, window, *args):
        wid = _window_id(window)
        if wid is not None:
            self._windows.pop(wid, None)

    def _is_nemo_window(self, window):
        try:
            return window.__gtype__.name == "NemoWindow"
        except Exception:
            return False

    def _notebooks(self, window):
        if Gtk is None:
            return []
        return [w for w in _walk(window)
                if isinstance(w, Gtk.Notebook) and _visible(w)
                and any(self._is_slot(w.get_nth_page(i))
                        for i in range(w.get_n_pages()))]

    def _is_slot(self, page):
        try:
            return page.__gtype__.name == "NemoWindowSlot"
        except Exception:
            return False

    def _record(self, wid, entry):
        window = entry.ref()
        if window is None:
            return None
        books = self._notebooks(window)
        if len(books) != 1:
            return {"id": wid, "marker": entry.tagged_title or "",
                    "locations": [], "active_tab": -1, "complete": False}
        notebook = books[0]
        locations = []
        complete = True
        for i in range(notebook.get_n_pages()):
            page = notebook.get_nth_page(i)
            if not self._is_slot(page):
                complete = False
                locations.append(None)
                continue
            found = [getattr(w, "wsctl_nemo_uri") for w in _walk(page)
                     if getattr(w, "wsctl_nemo_provider", False)
                     and getattr(w, "wsctl_nemo_uri", None) is not None]
            if len(found) != 1:
                complete = False
                locations.append(found[0] if len(found) == 1 else None)
            else:
                locations.append(found[0])
        try:
            active = int(notebook.get_current_page())
        except Exception:
            active = -1
            complete = False
        if not locations or active < 0 or active >= len(locations) or any(x is None for x in locations):
            complete = False
        return {"id": wid, "marker": entry.tagged_title or "",
                "locations": locations, "active_tab": active,
                "complete": complete}

    def state(self):
        with self._lock:
            result = []
            for wid, entry in list(self._windows.items()):
                if entry.ref() is None:
                    self._windows.pop(wid, None)
                    continue
                item = self._record(wid, entry)
                if item is not None:
                    result.append(item)
            return {"pid": os.getpid(), "windows": result, "build": {"revision": BUILD_REVISION},
                    "capabilities": ["runtime_build"]}

    def identify(self, lease):
        lease = str(lease)
        if not re.fullmatch(r"[0-9a-f]{32}", lease):
            return {"ok": False, "error": "invalid-lease"}
        with self._lock:
            if self._lease is not None and self._lease != lease:
                return {"ok": False, "error": "lease-conflict"}
            self._lease = lease
            for wid, entry in self._windows.items():
                window = entry.ref()
                if window is None:
                    continue
                try:
                    title = window.get_title() or ""
                    marker = f"[wsctl-nemo:{lease}:{wid}]"
                    if entry.tagged_title == title:
                        pass  # idempotent retry; do not nest markers
                    else:
                        entry.original_title = title
                        entry.tagged_title = f"{title} {marker}"
                        window.set_title(entry.tagged_title)
                except Exception:
                    entry.original_title = entry.tagged_title = None
            if GLib is not None:
                if self._timer:
                    GLib.source_remove(self._timer)
                self._timer = GLib.timeout_add(3000, self._timeout_release, lease)
            return {"ok": True, "lease": lease, "pid": os.getpid(),
                    "windows": self.state()["windows"]}

    def _timeout_release(self, lease):
        self._timer = None
        self.release(lease)
        return False

    def release(self, lease):
        with self._lock:
            if self._lease != str(lease):
                return False
            for entry in self._windows.values():
                window = entry.ref()
                if window is None or entry.tagged_title is None:
                    continue
                try:
                    if window.get_title() == entry.tagged_title:
                        window.set_title(entry.original_title or "")
                except Exception:
                    pass
                entry.original_title = entry.tagged_title = None
            self._lease = None
            if self._timer and GLib is not None:
                GLib.source_remove(self._timer)
            self._timer = None
            return True

    def select_tab(self, wid, index):
        entry = self._windows.get(int(wid)); window = entry.ref() if entry else None
        books = self._notebooks(window) if window else []
        if len(books) != 1 or not (0 <= int(index) < books[0].get_n_pages()):
            return False
        books[0].set_current_page(int(index)); return True

    def close_window(self, wid, expected):
        entry = self._windows.get(int(wid)); window = entry.ref() if entry else None
        if window is None:
            return False
        try:
            expected = json.loads(expected)
        except Exception:
            return False
        current = self._record(int(wid), entry)
        if not current or not current["complete"] or current["locations"] != expected:
            return False
        try:
            window.close(); return True
        except Exception:
            return False


if GObject is not None and Nemo is not None:
    _ProviderBase = (GObject.GObject, Nemo.LocationWidgetProvider)
else:  # keeps pure logic importable by tests on hosts without Nemo GI
    _ProviderBase = (object,)


class LocationWidgetProvider(*_ProviderBase):
    """Nemo's Python extension entry point and session-bus adapter."""
    def __init__(self):
        if GObject is not None:
            GObject.GObject.__init__(self)
        self.bridge = NemoBridge()
        self._bus = None
        self._registration = None
        self._owner = None
        if Gio is not None:
            try:
                info = Gio.DBusNodeInfo.new_for_xml(_XML).interfaces[0]
                self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
                self._owner = Gio.bus_own_name_on_connection(
                    self._bus, BUS_NAME, Gio.BusNameOwnerFlags.NONE, None, None)
                self._registration = self._bus.register_object(
                    OBJECT_PATH, info, self._call, None, None)
            except Exception as error:
                print(f"workspace-state Nemo bridge registration failed: {error}", file=sys.stderr)
                self._bus = self._registration = None

    def get_widget(self, uri, window):
        return self.bridge.get_widget(uri, window)

    def _call(self, conn, sender, path, iface, method, params, invocation):
        try:
            if method == "GetState": value = json.dumps(self.bridge.state(), separators=(",", ":")); invocation.return_value(GLib.Variant("(s)", (value,)))
            elif method == "Identify": value = json.dumps(self.bridge.identify(params.unpack()[0]), separators=(",", ":")); invocation.return_value(GLib.Variant("(s)", (value,)))
            elif method == "Release": invocation.return_value(GLib.Variant("(b)", (self.bridge.release(params.unpack()[0]),)))
            elif method == "SelectTab": invocation.return_value(GLib.Variant("(b)", (self.bridge.select_tab(*params.unpack()),)))
            elif method == "CloseWindow": invocation.return_value(GLib.Variant("(b)", (self.bridge.close_window(*params.unpack()),)))
            else: invocation.return_dbus_error("org.sagecat.WorkspaceState.Nemo1.Error", "unknown method")
        except Exception as exc:
            invocation.return_dbus_error("org.sagecat.WorkspaceState.Nemo1.Error", str(exc))
