import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "srt_encoding.py"
_spec = importlib.util.spec_from_file_location("srt_encoding_under_test", SCRIPT)
enc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(enc)

CUES = "".join(f"{i}\n00:00:0{i},000 --> 00:00:0{i},500\nLine number {i} is here.\n\n" for i in range(1, 6))


class SrtEncodingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.backups = self.root / "backups"
        self.subtitle = self.root / "Movie.en.srt"

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, data):
        self.subtitle.write_bytes(data)
        os.chmod(self.subtitle, 0o640)
        return data

    def backup_bytes(self):
        backups = list(self.backups.iterdir())
        self.assertEqual(len(backups), 1)
        return backups[0].read_bytes()

    def test_valid_utf8_is_never_rewritten(self):
        original = self.write((CUES + "Café crème\n").encode("utf-8"))
        before = self.subtitle.stat().st_mtime_ns
        self.assertEqual(enc.repair(self.subtitle, self.backups), ("ok", ""))
        self.assertEqual(self.subtitle.read_bytes(), original)
        self.assertEqual(self.subtitle.stat().st_mtime_ns, before)
        self.assertFalse(self.backups.exists())

    def test_utf8_bom_is_stripped_and_original_backed_up(self):
        original = self.write(b"\xef\xbb\xbf" + CUES.encode("utf-8"))
        status, detail = enc.repair(self.subtitle, self.backups)
        self.assertEqual(status, "converted", detail)
        self.assertEqual(self.subtitle.read_bytes(), CUES.encode("utf-8"))
        self.assertEqual(self.backup_bytes(), original)
        if os.name == "posix":
            self.assertEqual(self.subtitle.stat().st_mode & 0o777, 0o640)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.endswith(".enc_tmp")], [])

    def test_utf16_with_bom_is_reencoded(self):
        self.write((CUES + "Привет\n").encode("utf-16"))
        status, detail = enc.repair(self.subtitle, self.backups)
        self.assertEqual(status, "converted", detail)
        self.assertEqual(self.subtitle.read_text(encoding="utf-8"), CUES + "Привет\n")

    def test_latin1_is_converted_only_from_a_known_charset(self):
        original = self.write((CUES + "Café crème\n").encode("latin-1"))
        status, detail = enc.repair(self.subtitle, self.backups, charset="unknown-8bit")
        self.assertEqual(status, "failed")
        self.assertIn("unrecognised encoding", detail)
        self.assertEqual(self.subtitle.read_bytes(), original)

        status, detail = enc.repair(self.subtitle, self.backups, charset="iso-8859-1")
        self.assertEqual(status, "converted", detail)
        self.assertEqual(self.subtitle.read_text(encoding="utf-8"), CUES + "Café crème\n")
        self.assertEqual(self.backup_bytes(), original)

    def test_bytes_that_do_not_decode_are_never_dropped(self):
        original = self.write(b"\xef\xbb\xbf" + CUES.encode("utf-8") + b"Broken \xff byte\n")
        status, detail = enc.repair(self.subtitle, self.backups)
        self.assertEqual(status, "failed")
        self.assertIn("does not decode cleanly", detail)
        self.assertEqual(self.subtitle.read_bytes(), original)
        self.assertFalse(self.backups.exists())

    def test_cyrillic_labelled_latin1_is_refused(self):
        original = self.write((CUES + "Привет, как дела? Всё хорошо.\n" * 8).encode("cp1251"))
        status, detail = enc.repair(self.subtitle, self.backups, charset="iso-8859-1")
        self.assertEqual(status, "failed")
        self.assertIn("ambiguous", detail)
        self.assertEqual(self.subtitle.read_bytes(), original)

    def test_without_file_command_non_utf8_is_kept(self):
        original = self.write((CUES + "Café\n").encode("latin-1"))
        with patch.object(enc.subprocess, "run", side_effect=FileNotFoundError("file")):
            self.assertEqual(enc.repair(self.subtitle, self.backups)[0], "failed")
        self.assertEqual(self.subtitle.read_bytes(), original)

    def test_cli_repair_and_check(self):
        self.write((CUES + "Café\n").encode("latin-1"))
        clean = self.root / "Clean.en.srt"
        clean.write_text(CUES, encoding="utf-8")
        bom = self.root / "Bom.en.srt"
        bom.write_bytes(b"\xef\xbb\xbf" + CUES.encode("utf-8"))

        listing = b"\0".join(os.fsencode(p) for p in (self.subtitle, clean, bom)) + b"\0"
        result = subprocess.run([sys.executable, str(SCRIPT), "check", "--limit", "2"],
                                input=listing, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(b"\0"),
                         [b"non-utf-8|" + os.fsencode(self.subtitle), b"checked|2", b""])

        result = subprocess.run(
            [sys.executable, str(SCRIPT), "repair", "--backup-dir", str(self.backups),
             "--charset", "iso-8859-1", str(self.subtitle)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("converted|from iso8859-1; original saved as "))


if __name__ == "__main__":
    unittest.main()
