import unittest

from workspace_state.provider_results import placement_frame_matches


class PlacementFrameTests(unittest.TestCase):
    def setUp(self):
        self.monitor = {"x": 1920, "y": 0, "width": 1920, "height": 1080}
        self.area = dict(self.monitor, y=30, height=1050)
        self.target = {"workspace": 2, "monitor": 1, "state": "maximized",
                       "monitor_geometry": self.monitor,
                       "geometry": {"x": 2000, "y": 80, "width": 600, "height": 400}}
        self.window = dict(self.target, geometry=self.area, monitor_work_area=self.area)

    def test_maximized_frame_uses_current_work_area_not_saved_normal_rectangle(self):
        self.assertTrue(placement_frame_matches(self.window, self.target))
        self.assertFalse(placement_frame_matches(dict(self.window, geometry=self.target["geometry"]), self.target))
        self.assertFalse(placement_frame_matches(dict(self.window, geometry=dict(self.area, height=2030)), self.target))

    def test_older_companion_allows_panels_but_rejects_frames_outside_monitor(self):
        self.window.pop("monitor_work_area")
        self.assertTrue(placement_frame_matches(self.window, self.target))
        for geometry in (dict(self.area, width=3840), dict(self.area, x=-1920), dict(self.area, height=2030)):
            with self.subTest(geometry=geometry):
                self.assertFalse(placement_frame_matches(dict(self.window, geometry=geometry), self.target))

    def test_fullscreen_frame_uses_physical_monitor_instead_of_work_area(self):
        target = dict(self.target, state="fullscreen")
        window = dict(self.window, state="fullscreen")
        self.assertFalse(placement_frame_matches(window, target))
        self.assertTrue(placement_frame_matches(dict(window, geometry=self.monitor), target))

    def test_missing_invalid_or_nonfinite_frame_never_verifies(self):
        for geometry in ({}, dict(self.area, width=0), dict(self.area, x=float("nan")), dict(self.area, height="1080")):
            with self.subTest(geometry=geometry):
                self.assertFalse(placement_frame_matches(dict(self.window, geometry=geometry), self.target))
        self.assertFalse(placement_frame_matches(
            {"workspace": 2, "monitor": 1, "state": "maximized", "geometry": self.area},
            {"workspace": 2, "monitor": 1, "state": "maximized"},
        ))

    def test_normal_frame_still_uses_saved_monitor_relative_geometry(self):
        target = dict(self.target, state="normal", coordinate_space="monitor",
                      geometry={"x": 10, "y": 20, "width": 600, "height": 400})
        window = dict(self.window, state="normal", geometry_relative=target["geometry"])
        self.assertTrue(placement_frame_matches(window, target))
        self.assertFalse(placement_frame_matches(dict(window, monitor=2), target))


if __name__ == "__main__":
    unittest.main()
