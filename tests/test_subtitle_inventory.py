import importlib.util
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("subtitle_inventory", ROOT / "scripts" / "subtitle_inventory.py")
inv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inv)


def ffprobe(*streams):
    """A fake subprocess.run returning these subtitle streams."""
    payload = json.dumps({"streams": [
        {"tags": {"language": lang, **({"title": title} if title else {})}, "disposition": {"forced": forced}}
        for lang, title, forced in streams]})
    return lambda *_a, **_k: SimpleNamespace(stdout=payload)


class LanguageTests(unittest.TestCase):
    def test_base_language_of_plex_and_container_tags(self):
        self.assertEqual(inv.base_lang("eng"), "en")
        self.assertEqual(inv.base_lang("es-MX"), "es")
        self.assertEqual(inv.base_lang("spa"), "es")
        self.assertEqual(inv.base_lang("zh-Hant"), "zh")
        self.assertEqual(inv.base_lang("chi"), "zh")
        self.assertEqual(inv.base_lang("und"), "")

    def test_forced_by_flag_or_track_title(self):
        self.assertTrue(inv.is_forced("1", None))
        self.assertTrue(inv.is_forced(None, "English [ForcedNarrative]"))
        self.assertFalse(inv.is_forced("0", "English [CC]"))

    def test_embedded_tracks_skip_forced_and_untagged(self):
        run = ffprobe(("eng", "SDH", 0), ("spa", "Forced", 0), ("fre", None, 1), ("und", None, 0), ("chi", None, 0))
        self.assertEqual(inv.embedded_langs("video.mkv", run=run), {"en", "zh"})


class PlexTests(unittest.TestCase):
    def test_part_paths_map_to_plexminds_mount(self):
        self.assertEqual(inv.map_part("/data/TV Shows/Show/S01/E1.mkv", ["/data/TV Shows"], "/media/tv"),
                         "/media/tv/Show/S01/E1.mkv")
        self.assertIsNone(inv.map_part("/elsewhere/E1.mkv", ["/data/TV Shows"], "/media/tv"))

    def test_index_covers_movies_and_episodes_across_pages(self):
        pages = {
            "/library/sections": '<MediaContainer><Directory key="1" type="movie"><Location path="/data/Movies"/></Directory>'
                                 '<Directory key="2" type="show"><Location path="/data/TV Shows"/></Directory>'
                                 '<Directory key="3" type="artist"><Location path="/music"/></Directory></MediaContainer>',
        }

        def get(path):
            if path in pages:
                return ET.fromstring(pages[path])
            section, start = path.split("/")[3], int(path.split("Start=")[1].split("&")[0])
            if section == "1" and start == 0:
                return ET.fromstring('<MediaContainer totalSize="1"><Video ratingKey="10"><Media>'
                                     '<Part file="/data/Movies/A (2000)/A.mkv"/></Media></Video></MediaContainer>')
            if section == "2" and start == 0:
                return ET.fromstring('<MediaContainer totalSize="2"><Video ratingKey="20"><Media>'
                                     '<Part file="/data/TV Shows/S/E1.mkv"/></Media></Video></MediaContainer>')
            if section == "2" and start == 1:
                return ET.fromstring('<MediaContainer totalSize="2"><Video ratingKey="21"><Media>'
                                     '<Part file="/data/TV Shows/S/E2.mkv"/></Media></Video></MediaContainer>')
            raise AssertionError(path)

        with patch.dict(inv.ROOTS, {"movie": "/media/movies", "show": "/media/tv"}):
            self.assertEqual(inv.build_index(get), {"/media/movies/A (2000)/A.mkv": "10",
                                                    "/media/tv/S/E1.mkv": "20", "/media/tv/S/E2.mkv": "21"})

    def test_plex_listed_subtitles_count_except_forced(self):
        xml = ('<MediaContainer><Video><Media><Part>'
               '<Stream streamType="2" languageCode="jpn"/>'
               '<Stream streamType="3" languageCode="spa" languageTag="es"/>'
               '<Stream streamType="3" languageCode="eng" title="English [ForcedNarrative]"/>'
               '<Stream streamType="3" languageCode="chi" forced="1"/>'
               '</Part></Media></Video></MediaContainer>')
        langs = inv.plex_langs("/media/tv/S/E1.mkv", get=lambda _p: ET.fromstring(xml), index={"/media/tv/S/E1.mkv": "20"})
        self.assertEqual(langs, {"es"})
        self.assertEqual(inv.plex_langs("/media/tv/new.mkv", get=lambda _p: None, index={}), set())

    def test_plex_errors_are_not_answers(self):
        def broken(_path):
            raise OSError("Plex down")
        self.assertIsNone(inv.plex_langs("/media/tv/S/E1.mkv", get=broken, index={"/media/tv/S/E1.mkv": "20"}))


class CommandTests(unittest.TestCase):
    def test_has_reports_where_and_caches_the_answer(self):
        with tempfile.TemporaryDirectory() as raw:
            video = Path(raw) / "E1.mkv"
            video.write_bytes(b"video")
            calls = []

            def plex(path):
                calls.append(path)
                return {"es"}

            with patch.object(inv, "CACHE_FILE", Path(raw) / "cache.json"), \
                    patch.object(inv, "embedded_langs", return_value={"en"}), \
                    patch.object(inv, "plex_langs", side_effect=plex), \
                    patch("builtins.print") as out:
                self.assertEqual(inv.main(["x", "has", "en", str(video)]), 0)
                out.assert_called_with("embedded")
                self.assertEqual(inv.main(["x", "has", "es-MX", str(video)]), 0)
                out.assert_called_with("Plex")
                self.assertEqual(inv.main(["x", "has", "zh", str(video)]), 1)
            self.assertEqual(len(calls), 1)  # answered from the cache after the first call

    def test_missing_video_is_not_covered(self):
        self.assertEqual(inv.main(["x", "has", "en", "/no/such/video.mkv"]), 1)


def srt(cues):
    return "".join(f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},500\nLine {i}.\n\n" for i in range(1, cues + 1))


def plex_xml(*streams):
    return ET.fromstring("<MediaContainer><Video><Media><Part>"
                         + "".join(f"<Stream {s}/>" for s in streams)
                         + "</Part></Media></Video></MediaContainer>")


class TranslationSourceTests(unittest.TestCase):
    def probe(self, *streams):
        payload = json.dumps({"streams": [
            {"codec_name": codec, "tags": {"language": lang, "title": title},
             "disposition": {"forced": forced, "hearing_impaired": sdh}}
            for codec, lang, title, forced, sdh in streams]})
        return lambda *_a, **_k: SimpleNamespace(stdout=payload)

    def test_embedded_track_must_be_full_dialogue_text_in_the_language(self):
        run = self.probe(("hdmv_pgs_subtitle", "eng", "", 0, 0), ("subrip", "spa", "", 0, 0),
                         ("subrip", "eng", "Director Commentary", 0, 0), ("subrip", "eng", "", 1, 0))
        self.assertIsNone(inv.embedded_text_track("v.mkv", "en", run=run))

    def test_embedded_track_position_prefers_plain_over_sdh(self):
        run = self.probe(("hdmv_pgs_subtitle", "eng", "", 0, 0), ("subrip", "eng", "SDH", 0, 0),
                         ("ass", "eng", "English", 0, 0))
        self.assertEqual(inv.embedded_text_track("v.mkv", "en", run=run), 2)
        run = self.probe(("subrip", "eng", "English", 0, 1))
        self.assertEqual(inv.embedded_text_track("v.mkv", "en", run=run), 0)

    def test_plex_stream_needs_a_downloadable_text_file(self):
        xml = plex_xml('streamType="3" languageCode="eng" codec="pgs" key="/library/streams/1"',
                       'streamType="3" languageCode="eng" codec="srt"',  # embedded: no key
                       'streamType="3" languageCode="eng" codec="srt" key="/somewhere/else"',
                       'streamType="3" languageCode="eng" codec="srt" key="/library/streams/2" forced="1"',
                       'streamType="3" languageCode="eng" codec="srt" key="/library/streams/3" hearingImpaired="1"',
                       'streamType="3" languageCode="eng" codec="srt" key="/library/streams/4"',
                       'streamType="3" languageCode="spa" codec="srt" key="/library/streams/5"')
        found = inv.plex_text_stream("v.mkv", "en", get=lambda _p: xml, index={"v.mkv": "1"})
        self.assertEqual(found, {"key": "/library/streams/4", "codec": "srt"})
        self.assertEqual(inv.plex_text_stream("v.mkv", "fr", get=lambda _p: xml, index={"v.mkv": "1"}), {})

    def _video(self, raw):
        video = Path(raw) / "E1.mkv"
        video.write_bytes(b"video")
        return str(video)

    def test_an_unreachable_plex_is_not_remembered_as_no_subtitle(self):
        with tempfile.TemporaryDirectory() as raw:
            video = self._video(raw)
            with patch.object(inv, "SOURCES_FILE", Path(raw) / "s.json"), \
                    patch.object(inv, "embedded_text_track", return_value=None), \
                    patch.object(inv, "plex_text_stream", side_effect=[None, {"key": "/library/streams/9", "codec": "srt"}]) as plex:
                self.assertIsNone(inv.find_source(video, "en"))
                self.assertEqual(inv.find_source(video, "en"), {"key": "/library/streams/9", "codec": "srt"})
                self.assertEqual(inv.find_source(video, "en"), {"key": "/library/streams/9", "codec": "srt"})
            self.assertEqual(plex.call_count, 2)  # the third call came from the cache

    def test_extract_writes_a_plex_download_and_says_where_it_came_from(self):
        with tempfile.TemporaryDirectory() as raw:
            video, output = self._video(raw), Path(raw) / "out" / "E1.en.srt"
            with patch.object(inv, "find_source", return_value={"key": "/library/streams/9", "codec": "srt"}):
                origin = inv.extract(video, "en", output, fetch=lambda _key: srt(25).encode())
            self.assertEqual(origin, "Plex")
            self.assertEqual(output.read_text(), srt(25))
            self.assertEqual([p.name for p in output.parent.iterdir()], ["E1.en.srt"])

    def test_extract_passes_the_track_position_to_ffmpeg(self):
        calls = []

        def run(command, **_kwargs):
            calls.append(command)
            Path(command[-1]).write_text(srt(25))
            return SimpleNamespace(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as raw:
            video, output = self._video(raw), Path(raw) / "E1.en.srt"
            with patch.object(inv, "find_source", return_value={"track": 3}):
                self.assertEqual(inv.extract(video, "en", output, run=run), "embedded")
            self.assertTrue(output.exists())
        self.assertIn("0:s:3", calls[0])

    def test_extract_refuses_a_short_subtitle_and_leaves_nothing_behind(self):
        with tempfile.TemporaryDirectory() as raw:
            video, output = self._video(raw), Path(raw) / "out" / "E1.en.srt"
            with patch.object(inv, "find_source", return_value={"key": "/library/streams/9", "codec": "srt"}):
                with self.assertRaisesRegex(ValueError, "only 3 cues"):
                    inv.extract(video, "en", output, fetch=lambda _key: srt(3).encode())
            self.assertEqual(list(output.parent.iterdir()), [])

    def test_extract_command_prints_why_it_failed(self):
        with patch.object(inv, "extract", side_effect=ValueError("no text subtitle in en")), \
                patch("builtins.print") as out:
            self.assertEqual(inv.main(["x", "extract", "en", "v.mkv", "out.srt"]), 1)
        self.assertEqual(str(out.call_args.args[0]), "no text subtitle in en")


if __name__ == "__main__":
    unittest.main()
