import importlib.util
import json
import pathlib
import sys
import unittest
import weakref
import gc

_path = pathlib.Path(__file__).parents[1] / "nemo-extension" / "wsctl_nemo_bridge.py"
_spec = importlib.util.spec_from_file_location("wsctl_nemo_bridge", _path)
bridge = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bridge
_spec.loader.exec_module(bridge)


class _Type:
    def __init__(self, name): self.name = name


class Window:
    def __init__(self, title="Files"):
        self.title = title
        self.closed = False
        self.__gtype__ = _Type("NemoWindow")
        self.destroy_callback = None

    def get_id(self): return 17
    def get_title(self): return self.title
    def set_title(self, value): self.title = value
    def close(self): self.closed = True
    def connect(self, signal, callback): self.destroy_callback = callback


class FakeBox:
    def __init__(self, *args, **kwargs): self.children = []
    def set_size_request(self, *args): pass
    def set_no_show_all(self, *args): pass
    def hide(self): pass
    def get_children(self): return list(self.children)


class FakeNotebook:
    def __init__(self, pages, active=0):
        self.pages, self.active, self.__gtype__ = pages, active, _Type("GtkNotebook")
    def get_n_pages(self): return len(self.pages)
    def get_nth_page(self, index): return self.pages[index]
    def get_current_page(self): return self.active
    def set_current_page(self, index): self.active = index
    def get_visible(self): return True


class FakeGtk:
    Notebook = FakeNotebook
    Box = FakeBox
    class Orientation: HORIZONTAL = 0


class FakeGLib:
    timer = 40
    removed = []
    @classmethod
    def timeout_add(cls, delay, callback, lease):
        cls.timer += 1; cls.callback = callback; cls.lease = lease; return cls.timer
    @classmethod
    def source_remove(cls, source): cls.removed.append(source)


def add_window(obj, window, locations):
    entry = bridge._Window(weakref.ref(window), weakref.WeakSet())
    obj._windows[bridge._window_id(window)] = entry
    obj._notebooks = lambda _window: []
    obj._record = lambda wid, ent: {
        "id": wid, "marker": ent.tagged_title or "",
        "locations": locations, "active_tab": 0, "complete": True,
    }
    return entry


class NemoBridgeTests(unittest.TestCase):
    def test_discovery_uses_exact_nemo_types(self):
        obj = bridge.NemoBridge()
        slot = type("Slot", (), {"__gtype__": _Type("NemoWindowSlot")})()
        other = type("Other", (), {"__gtype__": _Type("GtkBox")})()
        self.assertTrue(obj._is_slot(slot))
        self.assertFalse(obj._is_slot(other))
        self.assertTrue(obj._is_nemo_window(Window()))
        desktop = Window(); desktop.__gtype__ = _Type("NemoDesktopWindow")
        self.assertFalse(obj._is_nemo_window(desktop))

    def test_provider_has_gobject_and_nemo_bases(self):
        if bridge.GObject is None or bridge.Nemo is None:
            self.skipTest("Nemo GI unavailable")
        self.assertTrue(issubclass(bridge.LocationWidgetProvider, bridge.GObject.GObject))
        self.assertTrue(issubclass(bridge.LocationWidgetProvider,
                                   bridge.Nemo.LocationWidgetProvider))

    def test_same_lease_retry_is_idempotent_and_removes_timer(self):
        obj = bridge.NemoBridge(); window = Window(); add_window(obj, window, [])
        old_glib = bridge.GLib; bridge.GLib = FakeGLib
        try:
            lease = "e" * 32; obj.identify(lease); first = window.title
            obj.identify(lease)
            self.assertEqual(window.title, first)
            self.assertIn(41, FakeGLib.removed)
            self.assertFalse(FakeGLib.callback(lease))
        finally:
            bridge.GLib = old_glib

    def test_invalid_lease_rejected(self):
        obj = bridge.NemoBridge()
        self.assertEqual(obj.identify("not-a-lease"), {"ok": False, "error": "invalid-lease"})

    def test_record_walks_only_slot_pages_and_marker_widgets(self):
        class Slot:
            __gtype__ = _Type("NemoWindowSlot")
            def __init__(self, children): self.children = children
            def get_children(self): return self.children
        def marker(uri):
            box = FakeBox(); box.wsctl_nemo_provider = True; box.wsctl_nemo_uri = uri; return box
        notebook = FakeNotebook([Slot([marker("file:///a")]), Slot([marker("file:///b")])])
        obj = bridge.NemoBridge(); window = Window()
        entry = bridge._Window(weakref.ref(window), weakref.WeakSet())
        obj._windows[17] = entry
        obj._notebooks = lambda _window: [notebook]
        result = bridge.NemoBridge._record(obj, 17, entry)
        self.assertEqual(result["locations"], ["file:///a", "file:///b"])
        self.assertTrue(result["complete"])

    def test_record_fails_multi_pane_empty_and_bad_active(self):
        class Slot:
            __gtype__ = _Type("NemoWindowSlot")
            def get_children(self): return []
        obj = bridge.NemoBridge(); window = Window()
        entry = bridge._Window(weakref.ref(window), weakref.WeakSet())
        obj._windows[17] = entry
        one = FakeNotebook([Slot()]); two = FakeNotebook([Slot()])
        obj._notebooks = lambda _window: [one, two]
        self.assertFalse(bridge.NemoBridge._record(obj, 17, entry)["complete"])
        obj._notebooks = lambda _window: [FakeNotebook([], active=0)]
        self.assertFalse(bridge.NemoBridge._record(obj, 17, entry)["complete"])
        bad = FakeNotebook([Slot()], active=2); obj._notebooks = lambda _window: [bad]
        self.assertFalse(bridge.NemoBridge._record(obj, 17, entry)["complete"])

    def test_get_widget_holds_window_wrapper_until_destroy(self):
        old_gtk = bridge.Gtk; bridge.Gtk = FakeGtk
        try:
            obj = bridge.NemoBridge(); window = Window(); ref = weakref.ref(window)
            box = obj.get_widget("file:///a", window)
            del window; gc.collect()
            self.assertIsNotNone(ref())
            alive = ref(); alive.destroy_callback(alive); self.assertEqual(obj._windows, {})
            self.assertIsNotNone(box)
        finally:
            bridge.Gtk = old_gtk

    def test_identify_tags_and_release_restores_title(self):
        obj = bridge.NemoBridge(); window = Window()
        add_window(obj, window, ["file:///tmp/a"])
        lease = "a" * 32; result = obj.identify(lease)
        self.assertTrue(result["ok"])
        self.assertIn(f"[wsctl-nemo:{lease}:", window.title)
        self.assertTrue(obj.release(lease)); self.assertEqual(window.title, "Files")


    def test_release_does_not_overwrite_navigation_title(self):
        obj = bridge.NemoBridge(); window = Window(); add_window(obj, window, [])
        lease = "b" * 32; obj.identify(lease); window.set_title("Changed by navigation")
        self.assertTrue(obj.release(lease)); self.assertEqual(window.title, "Changed by navigation")


    def test_conflicting_lease_is_refused(self):
        obj = bridge.NemoBridge(); window = Window(); add_window(obj, window, [])
        self.assertTrue(obj.identify("c" * 32)["ok"])
        self.assertEqual(obj.identify("d" * 32), {"ok": False, "error": "lease-conflict"})


    def test_close_requires_exact_complete_location_list(self):
        obj = bridge.NemoBridge(); window = Window()
        add_window(obj, window, ["file:///tmp/a", "file:///tmp/b"]); wid = bridge._window_id(window)
        self.assertFalse(obj.close_window(wid, json.dumps(["file:///tmp/a"])))
        self.assertFalse(window.closed)
        self.assertTrue(obj.close_window(wid, json.dumps(["file:///tmp/a", "file:///tmp/b"])))
        self.assertTrue(window.closed)

    def test_runtime_build_is_loaded_once(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            code = root / 'bridge.py'
            code.write_text(_path.read_text())
            stamp = root / 'build-info.json'
            stamp.write_text('{"revision":"loaded-release"}')
            installed = root / 'installed.py'
            installed.symlink_to(code)
            spec = importlib.util.spec_from_file_location('wsctl_nemo_build_test', installed)
            module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, {spec.name: module}):
                spec.loader.exec_module(module)
                stamp.write_text('{"revision":"new-installed-release"}')
                self.assertEqual(module.NemoBridge().state()['build']['revision'], 'loaded-release')


if __name__ == "__main__":
    unittest.main()
