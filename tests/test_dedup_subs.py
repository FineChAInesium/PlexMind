import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("dedup_under_test", ROOT / "scripts" / "dedup_subs.py")
dedup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dedup)


def _srt(path: Path, cues: int) -> None:
    blocks = [f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},500\nLine {i}\n" for i in range(1, cues + 1)]
    path.write_text("\n".join(blocks), encoding="utf-8")


class DedupSubsTests(unittest.TestCase):
    def test_classify_reads_language_and_kind_from_the_end_only(self):
        self.assertEqual(dedup.classify("Movie.en.srt"), ("Movie", "en", ""))
        self.assertEqual(dedup.classify("Movie.en.forced.srt"), ("Movie", "en", "forced"))
        self.assertEqual(dedup.classify("Movie.en.sdh.srt"), ("Movie", "en", "sdh"))
        self.assertEqual(dedup.classify("Movie.hi.en.srt"), ("Movie", "en", "sdh"))
        self.assertEqual(dedup.classify("Movie.en.ai.srt"), ("Movie", "en", ""))
        self.assertEqual(dedup.classify("Movie.es-mx.srt"), ("Movie", "es-mx", ""))
        self.assertEqual(dedup.classify("X.Part.One.en.srt"), ("X.Part.One", "en", ""))
        self.assertEqual(dedup.classify("The.Fly.en.srt"), ("The.Fly", "en", ""))

    def test_forced_sdh_and_multi_part_tracks_are_not_duplicates(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            media = root / "media" / "Movie"
            media.mkdir(parents=True)
            names = {
                "Movie.en.srt": 50,
                "Movie.en.forced.srt": 3,
                "Movie.en.sdh.srt": 55,
                "X.Part.One.en.srt": 40,
                "X.Part.Two.en.srt": 10,
            }
            for name, cues in names.items():
                _srt(media / name, cues)
            with patch.object(dedup, "QUARANTINE_DIR", root / "quarantine"):
                self.assertEqual(dedup.main(str(root / "media")), 0)
            self.assertEqual(sorted(p.name for p in media.iterdir()), sorted(names))
            self.assertFalse((root / "quarantine").exists())

    def test_true_duplicate_is_quarantined_not_deleted(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            media = root / "media" / "Movie"
            media.mkdir(parents=True)
            _srt(media / "Movie.en.srt", 50)
            _srt(media / "Movie.en.ai.srt", 20)
            with patch.object(dedup, "QUARANTINE_DIR", root / "quarantine"):
                self.assertEqual(dedup.main(str(root / "media")), 0)
            self.assertEqual([p.name for p in media.iterdir()], ["Movie.en.srt"])
            moved = root / "quarantine" / "Movie" / "Movie.en.ai.srt"
            self.assertTrue(moved.exists())
            self.assertEqual(moved.read_text(encoding="utf-8").count(" --> "), 20)


if __name__ == "__main__":
    unittest.main()
