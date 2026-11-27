"""The public tree names no owner home directory; examples are synthetic."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_HOMES = {"example", "user", "test", "tester", "u"}
HOME = re.compile(r"/home/([A-Za-z0-9_.-]+)")


def tracked():
    try:
        listing = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [path for path in (ROOT / name.decode() for name in listing.split(b"\0") if name)
            if path.is_file() and path != Path(__file__).resolve()]


class PublicTreeTests(unittest.TestCase):
    def texts(self):
        files = tracked()
        if not files:
            self.skipTest('not a Git checkout')
        self.assertGreater(len(files), 50)
        for path in files:
            try:
                yield path, path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue

    def test_no_tracked_file_names_a_real_home_directory(self):
        found = [f"{path.relative_to(ROOT)}: /home/{match[1]}" for path, text in self.texts()
                 for match in HOME.finditer(text) if match[1] not in SYNTHETIC_HOMES]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
