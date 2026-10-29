import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


ordering = load_module("ordering_encoding_under_test", ROOT / "scripts" / "fix_srt_ordering.py")

BOM = "\N{ZERO WIDTH NO-BREAK SPACE}"
OUT_OF_ORDER = (
    "2\n00:00:05,000 --> 00:00:06,000\n{second}\n\n"
    "1\n00:00:01,000 --> 00:00:02,000\n{first}\n"
)


class OrderingEncodingTests(unittest.TestCase):
    def _assert_left_unchanged(self, name, raw):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backups = root / "backups"
            subtitle = root / name
            subtitle.write_bytes(raw)
            with patch.object(ordering, "BACKUP_DIR", backups):
                status, detail = ordering.fix_file(subtitle)
            self.assertEqual(status, "error")
            self.assertIn("UTF-8", detail)
            self.assertEqual(subtitle.read_bytes(), raw)
            self.assertFalse(backups.exists())

    def test_gbk_chinese_file_is_not_rewritten(self):
        raw = OUT_OF_ORDER.format(second="第二句台词", first="第一句台词").encode("gbk")
        self._assert_left_unchanged("Movie.zh.srt", raw)

    def test_cp1252_spanish_file_is_not_rewritten(self):
        raw = OUT_OF_ORDER.format(second="¿Qué pasó?", first="Señor, mañana").encode("cp1252")
        self._assert_left_unchanged("Movie.es-MX.srt", raw)

    def test_non_utf8_file_is_reported_as_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            movies = root / "movies"
            movies.mkdir()
            raw = OUT_OF_ORDER.format(second="第二句", first="第一句").encode("gbk")
            (movies / "Movie.zh.srt").write_bytes(raw)
            with patch.object(ordering, "MEDIA_DIRS", [movies]), \
                    patch.object(ordering, "BACKUP_DIR", root / "backups"), \
                    redirect_stdout(io.StringIO()) as out:
                status = ordering.run()
            output = out.getvalue()
            self.assertEqual(status, 1, output)
            self.assertIn("ERROR: Movie.zh.srt", output)
            self.assertIn("not valid UTF-8", output)
            self.assertIn("Errors: 1", output)
            self.assertEqual((movies / "Movie.zh.srt").read_bytes(), raw)

    def test_bom_prefixed_file_in_order_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            subtitle = Path(tmp) / "Movie.zh.srt"
            raw = (BOM + "1\n00:00:01,000 --> 00:00:02,000\n第一句\n").encode("utf-8")
            subtitle.write_bytes(raw)
            self.assertEqual(ordering.fix_file(subtitle), ("ok", 0))
            self.assertEqual(subtitle.read_bytes(), raw)

    def test_bom_prefixed_file_out_of_order_is_fixed_without_losing_cue_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subtitle = root / "Movie.zh.srt"
            subtitle.write_bytes((BOM + OUT_OF_ORDER.format(second="第二句", first="第一句")).encode("utf-8"))
            with patch.object(ordering, "BACKUP_DIR", root / "backups"):
                status, count = ordering.fix_file(subtitle)
            self.assertEqual((status, count), ("fixed", 2))
            self.assertEqual(
                subtitle.read_text(encoding="utf-8"),
                "1\n00:00:01,000 --> 00:00:02,000\n第一句\n\n"
                "2\n00:00:05,000 --> 00:00:06,000\n第二句\n\n",
            )


class OrderingPathModeTests(unittest.TestCase):
    """translate.sh passes the files it wrote; only those are checked."""

    def _library(self, root):
        movies = root / "movies"
        movies.mkdir()
        written = movies / "X.fr.srt"
        written.write_text(OUT_OF_ORDER.format(second="Deuxième", first="Première"), encoding="utf-8")
        other = movies / "Y.es-MX.srt"
        other_raw = OUT_OF_ORDER.format(second="Segunda", first="Primera").encode("utf-8")
        other.write_bytes(other_raw)
        return movies, written, other, other_raw

    def test_only_the_given_files_are_checked_whatever_their_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            movies, written, other, other_raw = self._library(root)
            with patch.object(ordering, "MEDIA_DIRS", [movies]), \
                    patch.object(ordering, "BACKUP_DIR", root / "backups"), \
                    patch.dict(os.environ, {"PLEXMIND_MEDIA_LOCK_HELD": "1"}), \
                    redirect_stdout(io.StringIO()) as out:
                status = ordering.main([str(written), str(movies / "Missing.fr.srt")])
            output = out.getvalue()
            self.assertEqual(status, 0, output)
            self.assertEqual(written.read_text(encoding="utf-8").splitlines()[:3],
                             ["1", "00:00:01,000 --> 00:00:02,000", "Première"])
            self.assertEqual(other.read_bytes(), other_raw)
            self.assertIn("FIXED (2 order issues): X.fr.srt", output)
            self.assertNotIn("Y.es-MX.srt", output)
            self.assertNotIn("Found", output)
            self.assertIn("Done. Fixed: 1 | Already OK: 0 | Errors: 0", output)

    def test_command_line_paths_reach_path_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            movies, written, other, other_raw = self._library(root)
            env = {**os.environ, "PLEXMIND_MEDIA_LOCK_HELD": "1", "MOVIE_DIR": str(movies),
                   "TV_DIR": str(root / "tv"), "DATA_DIR": str(root / "data")}
            result = subprocess.run([sys.executable, str(ROOT / "scripts" / "fix_srt_ordering.py"), str(written)],
                                    env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("FIXED (2 order issues): X.fr.srt", result.stdout)
            self.assertEqual(other.read_bytes(), other_raw)


if __name__ == "__main__":
    unittest.main()
