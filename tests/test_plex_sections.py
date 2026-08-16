import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plexmind"))

try:
    from app import plex_sync, recommender
except ModuleNotFoundError as exc:  # Host-only checks may not have app dependencies installed.
    plex_sync = recommender = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


class Item:
    def __init__(self, title):
        self.title = title
        self.year = 2026
        self.genres = []


class Section:
    def __init__(self, title, section_type, items):
        self.title = title
        self.type = section_type
        self._items = items

    def all(self):
        return self._items


class Library:
    def __init__(self, sections):
        self._sections = sections

    def sections(self):
        return self._sections


class Server:
    def __init__(self, sections):
        self.library = Library(sections)


@unittest.skipIf(IMPORT_ERROR is not None, f"application dependencies unavailable: {IMPORT_ERROR}")
class PlexSectionDiscoveryTests(unittest.TestCase):
    def setUp(self):
        recommender._library_cache = None
        recommender._library_cache_ts = 0

    def test_recommender_discovers_sections_by_type_not_title(self):
        server = Server(
            [
                Section("Cinema Archive", "movie", [Item("Film")]),
                Section("Series Vault", "show", [Item("Series")]),
                Section("Audio", "artist", [Item("Album")]),
            ]
        )
        with patch("plexapi.server.PlexServer", return_value=server):
            items = recommender._fetch_full_library()
        self.assertEqual({item["title"] for item in items}, {"Film", "Series"})

    def test_sync_index_discovers_sections_by_type_not_title(self):
        server = Server([Section("My Films", "movie", [Item("Film")])])
        self.assertIn("film", plex_sync._build_index(server))


if __name__ == "__main__":
    unittest.main()
