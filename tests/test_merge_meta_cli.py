import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class MergeMetaCliTests(unittest.TestCase):
    def test_apply_merge_accepts_utf8_input_file_option(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "merge_meta.py"
        with tempfile.TemporaryDirectory(prefix="translate book ") as temp_dir:
            decisions = Path(temp_dir) / "merge decisions.json"
            decisions.write_text("not json", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "apply-merge",
                    temp_dir,
                    "--input",
                    str(decisions),
                ],
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn(str(decisions), result.stderr)
        self.assertIn("is not valid", result.stderr)


if __name__ == "__main__":
    unittest.main()
