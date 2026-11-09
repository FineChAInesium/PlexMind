import importlib.util
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan = load("translation_plan")
cue = load("translation_cue")

NOW = 1_800_000_000
DAY = 86400


def item(key, show=None, season=0, episode=0, added=NOW - 400 * DAY):
    return {"key": key, "show": show, "season": season, "episode": episode, "added": added}


def episodes(show, count, start=1, **extra):
    return {f"/tv/{show}/e{n}": item(f"{show}{n}", show, 1, n, **extra) for n in range(start, start + count)}


class OrderTests(unittest.TestCase):
    def paths(self, catalog):
        return [f"{base}.en.srt" for base in catalog]

    def test_next_episodes_after_each_viewer_come_first_newest_viewer_ahead(self):
        catalog = {**episodes("a", 8), **episodes("b", 4)}
        history = [("a3", "a", "1", NOW - DAY), ("b1", "b", "2", NOW - 2 * DAY)]
        ordered, counts = plan.rank(sorted(self.paths(catalog), reverse=True), catalog, history, NOW)
        self.assertEqual([p[4:-7] for p in ordered[:8]],
                         ["a/e4", "a/e5", "a/e6", "a/e7", "a/e8", "b/e2", "b/e3", "b/e4"])
        self.assertEqual(counts[0], 8)

    def test_each_viewer_gets_their_own_place_in_a_show(self):
        catalog = episodes("a", 20)
        history = [("a15", "a", "1", NOW - DAY), ("a2", "a", "2", NOW - 2 * DAY), ("a1", "a", "2", NOW - 3 * DAY)]
        ordered, _ = plan.rank(self.paths(catalog), catalog, history, NOW)
        self.assertEqual([p[4:-7] for p in ordered[:10]],
                         [f"a/e{n}" for n in (16, 17, 18, 19, 20, 3, 4, 5, 6, 7)])

    def test_tiers_run_active_then_new_then_old_watches_then_the_rest(self):
        catalog = {
            "/tv/active/e1": item("x1", "active", 1, 1),
            "/tv/new/e1": item("n1", "new", 1, 1, added=NOW - 3 * DAY),
            "/tv/old/e1": item("o1", "old", 1, 1),
            "/tv/other/e1": item("z1", "other", 1, 1),
        }
        history = [("x1", "active", "1", NOW - 3 * DAY), ("o1", "old", "1", NOW - 300 * DAY)]
        ordered, counts = plan.rank(sorted(self.paths(catalog)), catalog, history, NOW)
        self.assertEqual([p.split("/")[2] for p in ordered], ["active", "new", "old", "other"])
        self.assertEqual(counts, [0, 1, 1, 1, 1])

    def test_a_watched_movie_is_not_treated_as_being_watched(self):
        catalog = {"/m/seen": item("m1"), "/m/new": item("m2", added=NOW - DAY)}
        ordered, _ = plan.rank(self.paths(catalog), catalog, [("m1", None, "1", NOW - DAY)], NOW)
        self.assertEqual(ordered, ["/m/new.en.srt", "/m/seen.en.srt"])

    def test_videos_and_every_subtitle_name_find_their_title(self):
        for path in ("/tv/a/E1.mkv", "/tv/a/E1.en.srt", "/tv/a/E1.en.hi.srt", "/tv/a/E1.hi.en.srt", "/tv/a/e1.EN.srt"):
            self.assertEqual(plan.base_of(path), "/tv/a/e1")

    def test_unknown_files_stay_in_natural_order(self):
        ordered, _ = plan.rank(["/x/e10.en.srt", "/x/e2.en.srt"], {}, [], NOW)
        self.assertEqual(ordered, ["/x/e2.en.srt", "/x/e10.en.srt"])

    def test_plex_trouble_keeps_every_path(self):
        def broken(_path):
            raise OSError("down")
        ordered, summary = plan.order(["/x/b", "/x/a"], get=broken)
        self.assertEqual(ordered, ["/x/a", "/x/b"])
        self.assertIn("Plex unavailable", summary)

    def test_no_plex_configured_keeps_every_path(self):
        with patch.object(plan.inv, "PLEX_URL", ""), patch.object(plan.inv, "PLEX_TOKEN", ""):
            self.assertEqual(plan.order(["/x/e10", "/x/e2"])[0], ["/x/e2", "/x/e10"])

    def test_history_reads_the_show_from_its_key(self):
        xml = ('<MediaContainer><Video ratingKey="5" grandparentKey="/library/metadata/9" accountID="3" viewedAt="77"/>'
               '<Video ratingKey="6" accountID="1" viewedAt="50"/></MediaContainer>')
        self.assertEqual(plan.view_history(lambda _p: ET.fromstring(xml)),
                         [("5", "9", "3", 77), ("6", None, "1", 50)])


class ContextTests(unittest.TestCase):
    def fetcher(self, pages):
        return lambda path: ET.fromstring(pages[path])

    def test_episode_context_has_show_episode_genres_and_characters(self):
        pages = {
            "/library/metadata/5": '<MediaContainer><Video type="episode" title="Pilot" grandparentTitle="Dark" '
                                   'grandparentRatingKey="9" parentIndex="1" index="2" summary="A boy vanishes."/></MediaContainer>',
            "/library/metadata/9": '<MediaContainer><Directory type="show" title="Dark" summary="Four families."><Genre tag="Drama"/>'
                                   '<Role tag="Actor A" role="Jonas"/><Role tag="Actor B" role="Martha"/><Role tag="Actor C"/></Directory></MediaContainer>',
        }
        text = plan.context_text("/v", get=self.fetcher(pages), index={"/v": "5"})
        self.assertEqual(text.split("\n"), [
            "Series: Dark", 'Episode: S01E02 "Pilot"', "About the series: Four families.",
            "This episode: A boy vanishes.", "Genres: Drama", "Characters: Jonas; Martha"])

    def test_film_context_and_long_synopsis_is_cut_at_a_word(self):
        summary = "word " * 200
        pages = {"/library/metadata/5": f'<MediaContainer><Video type="movie" title="Heat" year="1995" summary="{summary}"/></MediaContainer>'}
        lines = plan.context_text("/v", get=self.fetcher(pages), index={"/v": "5"}).split("\n")
        self.assertEqual(lines[0], "Film: Heat (1995)")
        self.assertLessEqual(len(lines[1]), plan.MAX_SYNOPSIS_CHARS + 12)
        self.assertTrue(lines[1].endswith("word..."))

    def test_missing_or_failing_plex_gives_no_context(self):
        self.assertEqual(plan.context_text("/v", get=lambda _p: None, index={}), "")
        def broken(_path):
            raise OSError("down")
        self.assertEqual(plan.context_text("/v", get=broken, index={"/v": "5"}), "")


class CueTests(unittest.TestCase):
    def cue(self, text, start="00:00:01,000", end="00:00:02,000", number="1"):
        return (number, f"{start} --> {end}", text)

    def test_batches_may_end_at_sentences_and_long_pauses_only(self):
        cues = [self.cue("Where are you going?"), self.cue("I was thinking", "00:00:02,100", "00:00:03,000"),
                self.cue("that maybe...", "00:00:03,100", "00:00:04,000"), self.cue("<i>we should.</i>", "00:00:04,100", "00:00:05,000"),
                self.cue("And then", "00:00:05,100", "00:00:06,000"), self.cue("later on", "00:00:09,000", "00:00:10,000"),
                self.cue("the end", "00:00:10,100", "00:00:11,000")]
        self.assertEqual(cue.sentence_ends(cues), [True, False, False, True, True, False, True])

    def test_recent_pairs_match_by_number_and_skip_fallbacks(self):
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw) / "x.temp"
            temp.write_text("1\n00:00:01,000 --> 00:00:02,000\nHola\nAmigo\n\n2\n00:00:03,000 --> 00:00:04,000\nStill English\n\n", encoding="utf-8")
            cues = [self.cue("Hello\nFriend", number="1"), self.cue("Still English", number="2"), self.cue("Not done", number="3")]
            self.assertEqual(cue.recent_pairs(temp, cues).split("\n"), [
                "English: Hello / Friend", "Translation: Hola / Amigo", "English: Still English", "English: Not done"])

    def test_junk_from_a_broken_reply_is_rejected(self):
        self.assertEqual(cue.validate("Hello there my friend", "]`", "es-MX"), "unexpected_formatting")
        self.assertIsNone(cue.validate("[door slams]", "[portazo]", "es-MX"))

    def test_a_reply_with_a_shifted_line_is_redone_whole(self):
        sources = ["Hi.", "I think this is going to be a long night for all of us."]
        fine = '["Hola.", "Creo que va a ser una noche larga para todos nosotros."]'
        items, reasons = cue.validate_batch(sources, fine, "es-MX")
        self.assertEqual(reasons, [None, None])
        shifted = '["Hola. Creo que va a ser una noche larga para todos nosotros y más.", "Sí."]'
        self.assertEqual(cue.validate_batch(sources, shifted, "es-MX"), (None, "shifted_or_junk_items"))

    def test_shape_limits_only_apply_to_languages_they_fit(self):
        self.assertTrue(cue.looks_shifted("Okay.", "好的，我們現在馬上就走吧，不要再等了", "zh"))
        self.assertFalse(cue.looks_shifted("Okay.", "x" * 80, "ja"))

    def test_recent_pairs_survive_a_missing_file(self):
        self.assertEqual(cue.recent_pairs("/no/such/file", [self.cue("Hi")]), "English: Hi")


if __name__ == "__main__":
    unittest.main()
