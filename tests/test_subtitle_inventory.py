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


if __name__ == "__main__":
    unittest.main()
