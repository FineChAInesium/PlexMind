import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


ordering = load_module("ordering_under_test", ROOT / "scripts" / "fix_srt_ordering.py")


class SubtitleToolTests(unittest.TestCase):
    def test_ordering_repair_is_atomic_backed_up_and_mode_preserving(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ordering.BACKUP_DIR = root / "backups"
            subtitle = root / "sample.es-MX.srt"
            subtitle.write_text(
                "2\n00:00:05,000 --> 00:00:06,000\nSecond\n\n"
                "1\n00:00:01,000 --> 00:00:02,000\nFirst\n",
                encoding="utf-8",
            )
            os.chmod(subtitle, 0o640)
            status, count = ordering.fix_file(subtitle)
            self.assertEqual((status, count), ("fixed", 2))
            self.assertIn("00:00:01,000", subtitle.read_text().splitlines()[1])
            self.assertEqual(subtitle.stat().st_mode & 0o777, 0o640)
            self.assertEqual(len(list(ordering.BACKUP_DIR.iterdir())), 1)

    def test_ordering_repair_refuses_to_drop_unparsed_blocks(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ordering.BACKUP_DIR = root / "backups"
            subtitle = root / "sample.zh.srt"
            original = (
                "2\n00:00:05,000 --> 00:00:06,000\nSecond\n\n"
                "NOTE THAT MUST SURVIVE\n\n"
                "1\n00:00:01,000 --> 00:00:02,000\nFirst\n"
            )
            subtitle.write_text(original, encoding="utf-8")
            status, detail = ordering.fix_file(subtitle)
            self.assertEqual(status, "error")
            self.assertIn("lossy", detail)
            self.assertEqual(subtitle.read_text(), original)

    def test_audit_repair_uses_configured_host_roots(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            movies, tv = root / "Movies", root / "TV Shows"
            movies.mkdir(); tv.mkdir()
            subtitle = movies / "broken.srt"
            subtitle.write_text("1\n00:00:01,000 --> 00:00:01,000\nText\n", encoding="utf-8")
            report = root / "audit.txt"
            report.write_text("  [INVALID reason:non_positive_timestamp cues:1 size:1b] /media/movies/broken.srt\n")
            result = subprocess.run(
                [
                    sys.executable, str(ROOT / "scripts" / "repair_srt_timestamps.py"),
                    "--backup-root", str(root / "backups"), "--audit-report", str(report),
                    "--movies-root", str(movies), "--tv-root", str(tv),
                    "--lock-file", str(root / "media.lock"),
                ],
                text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("TOTAL_REPAIRS=1", result.stdout)


if __name__ == "__main__":
    unittest.main()
