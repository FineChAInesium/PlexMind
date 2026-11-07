import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("ordering_under_test", ROOT / "scripts" / "fix_srt_ordering.py")
ordering = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ordering)
REPAIR = ROOT / "scripts" / "repair_srt_timestamps.py"


class OrderingTests(unittest.TestCase):
    def test_whitespace_separator_and_position_suffix_survive_a_reorder(self):
        content = (
            "1\n00:00:05,000 --> 00:00:06,000 X1:100 X2:200 Y1:10 Y2:20\nSecond\n"
            " \n"  # a "blank" line holding a space
            "2\n00:00:01,000 --> 00:00:02,000\nFirst\n"
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "Movie.zh.srt"
            path.write_text(content, encoding="utf-8")
            with patch.object(ordering, "BACKUP_DIR", Path(raw) / "backups"):
                status, _detail = ordering.fix_file(path)
            self.assertEqual(status, "fixed", _detail)
            out = path.read_text(encoding="utf-8")
        self.assertLess(out.index("First"), out.index("Second"))
        self.assertIn("00:00:06,000 X1:100 X2:200 Y1:10 Y2:20", out)


class TimestampRepairTests(unittest.TestCase):
    def run_repair(self, *args, env=None):
        return subprocess.run([sys.executable, str(REPAIR), *args], text=True, capture_output=True,
                              timeout=10, env={**os.environ, **(env or {})})

    def test_non_utf8_file_is_skipped_not_mangled(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "Movie.fr.srt"
            original = "1\n00:00:01,000 --> 00:00:01,000\nCafé crème\n".encode("cp1252")
            path.write_bytes(original)
            result = self.run_repair("--backup-root", str(root / "b"), "--lock-file", str(root / "m.lock"), str(path))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("SKIPPED not UTF-8", result.stdout)
            self.assertEqual(path.read_bytes(), original)

    def test_a_private_lock_is_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            env = {k: v for k, v in os.environ.items() if k != "DATA_DIR"}
            result = subprocess.run([sys.executable, str(REPAIR), "--backup-root", raw, "x.srt"],
                                    text=True, capture_output=True, timeout=10, env=env)
            self.assertEqual(result.returncode, 2)
            self.assertIn("--lock-file", result.stderr)


if __name__ == "__main__":
    unittest.main()
