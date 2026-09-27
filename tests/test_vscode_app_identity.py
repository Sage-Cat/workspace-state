import unittest

from workspace_state.vscode import matching_windows


class VSCodeAppIdentityTests(unittest.TestCase):
    def test_current_native_wayland_app_id_is_discovered(self):
        code = {"id": 26, "app_id": "", "app_ids": ["com.microsoft.vscode"],
                "wm_class": "com.microsoft.VSCode", "wm_class_instance": "com.microsoft.VSCode"}
        unrelated = {"id": 27, "title": "Visual Studio Code", "app_id": "other"}
        self.assertEqual(matching_windows({"windows": [code, unrelated]}), [code])
