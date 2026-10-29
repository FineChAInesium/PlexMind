import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("pgs_cleanup_under_test", ROOT / "scripts" / "pgs_cleanup.py")
pgs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pgs)

SRT = "1\n00:00:01,000 --> 00:00:02,000\nHello.\n"


class PgsCleanupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.media = self.root / "media" / "Movie"
        self.media.mkdir(parents=True)
        self.quarantine = self.root / "quarantine"

    def tearDown(self):
        self._tmp.cleanup()

    def add(self, *names, content="image\n"):
        for name in names:
            (self.media / name).write_text(content, encoding="utf-8")

    def cleanup(self, *flags):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = pgs.main([*flags, "--quarantine-dir", str(self.quarantine), str(self.root / "media")])
        return rc, out.getvalue()

    def remaining(self):
        return sorted(path.name for path in self.media.iterdir())

    def test_classify_reads_language_and_kind_from_the_end_only(self):
        self.assertEqual(pgs.classify("Movie.en"), ("movie", "en", ""))
        self.assertEqual(pgs.classify("Movie.en.forced"), ("movie", "en", "forced"))
        self.assertEqual(pgs.classify("Movie.forced.EN"), ("movie", "en", "forced"))
        self.assertEqual(pgs.classify("Movie.en.hi"), ("movie", "en", "sdh"))
        self.assertEqual(pgs.classify("Movie.cc.en"), ("movie", "en", "sdh"))
        self.assertEqual(pgs.classify("X.Part.One.en"), ("x.part.one", "en", ""))
        self.assertEqual(pgs.classify("Movie"), ("movie", None, ""))

    def test_forced_srt_never_replaces_a_full_track(self):
        self.add("Movie.en.sup", "Movie.en.forced.sup")
        self.add("Movie.en.forced.srt", content=SRT)
        rc, out = self.cleanup()
        self.assertEqual(rc, 0)
        self.assertEqual(self.remaining(), ["Movie.en.forced.srt", "Movie.en.sup"])
        self.assertTrue((self.quarantine / "Movie" / "Movie.en.forced.sup").is_file())
        self.assertIn("Movie.en.forced.sup", out)
        self.assertIn("TOTAL:1", out)

    def test_sdh_and_regular_tracks_only_replace_their_own_kind(self):
        self.add("Movie.en.sup", "Movie.en.sdh.sup", "Movie.en.forced.sup")
        self.add("Movie.en.hi.srt", content=SRT)
        self.cleanup()
        self.assertEqual(self.remaining(), ["Movie.en.forced.sup", "Movie.en.hi.srt", "Movie.en.sup"])

        (self.media / "Movie.en.hi.srt").unlink()
        self.add("Movie.en.srt", content=SRT)
        self.cleanup()
        self.assertEqual(self.remaining(), ["Movie.en.forced.sup", "Movie.en.srt"])

    def test_vobsub_pair_moves_together_and_matching_ignores_case(self):
        self.add("Movie.EN.sub", "Movie.EN.idx", "Movie.de.SUP")
        self.add("movie.en.srt", "Movie.DE.srt", content=SRT)
        rc, out = self.cleanup()
        self.assertEqual(rc, 0)
        self.assertEqual(self.remaining(), ["Movie.DE.srt", "movie.en.srt"])
        moved = sorted(path.name for path in (self.quarantine / "Movie").iterdir())
        self.assertEqual(moved, ["Movie.EN.idx", "Movie.EN.sub", "Movie.de.SUP"])
        self.assertIn("TOTAL:3", out)

    def test_partials_empty_and_other_language_srts_do_not_count(self):
        self.add("Movie.en.sup")
        self.add("Movie.ai.srt", "Movie.ai-en.srt", "Movie.en.ai.srt", "Movie.translate_tmp.srt",
                 "Movie.es.srt", content=SRT)
        self.add("Movie.en.srt", content="")
        rc, out = self.cleanup()
        self.assertEqual(rc, 0)
        self.assertIn("Movie.en.sup", self.remaining())
        self.assertFalse(self.quarantine.exists())
        self.assertIn("TOTAL:0", out)

    def test_unknown_language_needs_opt_in_and_still_respects_kind(self):
        self.add("Movie.sup", "Movie.forced.sup")
        self.add("Movie.en.srt", content=SRT)
        self.cleanup()
        self.assertEqual(self.remaining(), ["Movie.en.srt", "Movie.forced.sup", "Movie.sup"])
        self.cleanup("--allow-unknown")
        self.assertEqual(self.remaining(), ["Movie.en.srt", "Movie.forced.sup"])

    def test_different_titles_do_not_match(self):
        self.add("X.Part.One.en.sup")
        self.add("X.Part.Two.en.srt", content=SRT)
        self.cleanup()
        self.assertIn("X.Part.One.en.sup", self.remaining())

    def test_existing_quarantine_entry_is_never_overwritten(self):
        earlier = self.quarantine / "Movie" / "Movie.en.sup"
        earlier.parent.mkdir(parents=True)
        earlier.write_text("earlier\n", encoding="utf-8")
        self.add("Movie.en.sup")
        self.add("Movie.en.srt", content=SRT)
        rc, _out = self.cleanup()
        self.assertEqual(rc, 0)
        self.assertEqual(earlier.read_text(encoding="utf-8"), "earlier\n")
        self.assertEqual(len(list(earlier.parent.glob("Movie.en.sup*"))), 2)

    def test_list_mode_reports_without_moving(self):
        self.add("Movie.en.sup", "Movie.fr.sup")
        self.add("Movie.en.srt", content=SRT)
        out = io.TextIOWrapper(io.BytesIO())
        with contextlib.redirect_stdout(out):
            self.assertEqual(pgs.main(["--list", str(self.root / "media")]), 0)
        records = out.buffer.getvalue().split(b"\0")
        self.assertEqual(records, [str(self.media / "Movie.en.sup").encode(), b""])
        self.assertIn("Movie.en.sup", self.remaining())


if __name__ == "__main__":
    unittest.main()
