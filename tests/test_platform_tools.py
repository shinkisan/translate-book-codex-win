import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import platform_tools  # noqa: E402


class PlatformToolDiscoveryTests(unittest.TestCase):
    def test_calibre_environment_override_wins(self):
        override = r"D:\Portable Apps\Calibre\ebook-convert.exe"
        with mock.patch.dict(os.environ, {"CALIBRE_EBOOK_CONVERT": override}), mock.patch(
            "platform_tools.shutil.which",
            side_effect=lambda candidate: override if candidate == override else None,
        ):
            self.assertEqual(platform_tools.find_calibre_convert(), override)

    def test_finds_standard_windows_calibre_install(self):
        with tempfile.TemporaryDirectory() as program_files:
            expected = Path(program_files) / "Calibre2" / "ebook-convert.exe"
            expected.parent.mkdir()
            expected.touch()
            with mock.patch.dict(os.environ, {"ProgramFiles": program_files}, clear=False), mock.patch(
                "platform_tools.shutil.which", return_value=None
            ):
                self.assertEqual(platform_tools.find_calibre_convert(), str(expected))

    def test_pandoc_environment_override_wins(self):
        override = r"D:\Portable Apps\Pandoc\pandoc.exe"
        with mock.patch.dict(os.environ, {"PANDOC": override}), mock.patch(
            "platform_tools.shutil.which",
            side_effect=lambda candidate: override if candidate == override else None,
        ):
            self.assertEqual(platform_tools.find_pandoc(), override)


if __name__ == "__main__":
    unittest.main()
