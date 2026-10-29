import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "srt_encoding.py"
_spec = importlib.util.spec_from_file_location("srt_encoding_fix_under_test", SCRIPT)
enc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(enc)

CUES = "".join(f"{i}\n00:00:0{i},000 --> 00:00:0{i},500\nLine number {i} is here.\n\n" for i in range(1, 6))


class MislabelledLatin1Tests(unittest.TestCase):
    """`file` calls any 8-bit text without C1 bytes iso-8859-1."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.backups = self.root / "backups"

    def tearDown(self):
        self._tmp.cleanup()

    def assert_refused(self, name, data, reason):
        subtitle = self.root / name
        subtitle.write_bytes(data)
        status, detail = enc.repair(subtitle, self.backups, charset="iso-8859-1")
        self.assertEqual(status, "failed", detail)
        self.assertIn(reason, detail)
        self.assertEqual(subtitle.read_bytes(), data)
        self.assertFalse(self.backups.exists())

    def assert_converted(self, name, data, text, codec):
        subtitle = self.root / name
        subtitle.write_bytes(data)
        status, detail = enc.repair(subtitle, self.backups, charset="iso-8859-1")
        self.assertEqual(status, "converted", detail)
        self.assertTrue(detail.startswith(f"from {codec}; "), detail)
        self.assertEqual(subtitle.read_text(encoding="utf-8"), text)
        self.assertEqual([p.read_bytes() for p in self.backups.iterdir()], [data])

    def test_utf8_with_one_stray_latin1_byte_is_refused(self):
        data = (CUES + "¿Qué pasó? Mañana.\n").encode("utf-8") + b"Caf\xe9\n"
        self.assert_refused("Movie.es.srt", data, "damaged UTF-8")

    def test_central_european_and_turkish_code_pages_are_refused(self):
        samples = {
            "Movie.pl.srt": ("Zażółć gęślą jaźń. Proszę, chodź tutaj.\n", "iso-8859-2"),
            "Movie.tr.srt": ("Günaydın, nasılsın? Şimdi çıkıyorum.\n", "cp1254"),
            "Movie.hu.srt": ("Árvíztűrő tükörfúrógép.\n", "cp1250"),
        }
        for name, (line, codec) in samples.items():
            with self.subTest(name=name):
                self.assert_refused(name, (CUES + line).encode(codec), "Western European")

    def test_untagged_latin1_is_refused(self):
        self.assert_refused("Movie.srt", (CUES + "Café crème\n").encode("latin-1"), "Western European")

    def test_bilingual_gb2312_is_refused(self):
        data = (CUES + "你好，世界\nHello, world\n").encode("gb2312")
        self.assert_refused("Movie.zh.srt", data, "Western European")

    # Each sample contains one accidental valid UTF-8 pair (É+NBSP, ß+«).
    def test_tagged_french_latin1_is_converted(self):
        text = CUES + "Déjà vu\xa0! C'est l'ÉTÉ\xa0!\n"
        self.assert_converted("Movie.fr.forced.srt", text.encode("latin-1"), text, "iso8859-1")

    def test_tagged_german_latin1_is_converted(self):
        text = CUES + "Er sagte »Die Straße ist groß« und ging. Grüße!\n"
        self.assert_converted("Movie.sdh.de.srt", text.encode("latin-1"), text, "iso8859-1")

    def test_c1_bytes_past_the_sample_are_read_as_windows_1252(self):
        text = CUES + "“Café,” she said… it’s fine.\n"
        self.assert_converted("Movie.en.hi.srt", text.encode("cp1252"), text, "cp1252")

    def test_c1_bytes_undefined_in_windows_1252_are_refused(self):
        self.assert_refused("Movie.en.srt", (CUES + "Café\n").encode("latin-1") + b"\x81\n",
                            "does not decode cleanly as cp1252")

    def test_language_tag(self):
        cases = {
            "Movie.en.srt": "en",
            "Movie.hi.en.srt": "en",
            "Movie.en.forced.srt": "en",
            "Movie.Part.One.pt-BR.srt": "pt",
            "Movie.de.temp": "de",
            "Movie.srt": "",
            "Movie.forced.srt": "",
            "Movie.translate_tmp.srt": "",
            "en.srt": "",
        }
        self.assertEqual({name: enc.language_tag(name) for name in cases}, cases)

    def test_cli_reports_refusal_for_lib_sh(self):
        subtitle = self.root / "Movie.pl.srt"
        data = (CUES + "Zażółć gęślą jaźń.\n").encode("iso-8859-2")
        subtitle.write_bytes(data)
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "repair", "--backup-dir", str(self.backups),
             "--charset", "iso-8859-1", str(subtitle)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout,
                         "failed|is labelled iso-8859-1 but is not tagged with a Western European language\n")
        self.assertEqual(subtitle.read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
