from collections import Counter
from types import SimpleNamespace
from unittest.mock import patch

from app import plex_sync, recommender


def test_plex_index_refuses_ambiguous_title_fallback():
    first = SimpleNamespace(title="The Thing", year=1982, type="movie", ratingKey="1")
    second = SimpleNamespace(title="The Thing", year=2011, type="movie", ratingKey="2")
    section = SimpleNamespace(type="movie", all=lambda: [first, second])
    server = SimpleNamespace(library=SimpleNamespace(sections=lambda: [section]))
    index = plex_sync._build_index(server)
    assert "the thing" not in index
    assert plex_sync._resolve(index, {"title": "The Thing", "year": 1982, "type": "movie"}) is first
    assert plex_sync._resolve(index, {"title": "The Thing", "type": "movie"}) is None


def test_suppression_applies_even_when_candidate_pool_is_small():
    candidates = [
        {"title": "Shown", "media_type": "movie", "genres": [], "keywords": []},
        {"title": "Fresh", "media_type": "movie", "genres": [], "keywords": []},
    ]
    history = [SimpleNamespace(viewed_at=None)]
    history_meta = [{"media_type": "movie", "title": "History", "genres": [], "keywords": []}]
    with patch.object(recommender.cache, "get_user_feedback", return_value=[]):
        result = recommender._prefilter(
            candidates, history_meta, history, "u", set(), {"shown": 1.0}, pool_size=40, n=1
        )
    assert [item["title"] for item in result] == ["Fresh"]


def test_omdb_uses_tls():
    from app import imdb_client
    assert imdb_client.OMDB_BASE.startswith("https://")


def test_playlist_swap_restores_active_title_when_promotion_fails():
    class Playlist:
        def __init__(self, title, fail_final=False):
            self.title = title
            self.fail_final = fail_final
            self.deleted = False

        def editTitle(self, title):
            if self.fail_final and title == plex_sync.PLAYLIST_MOVIES:
                raise RuntimeError("rename failed")
            self.title = title

        def editSummary(self, _summary):
            return None

        def delete(self):
            self.deleted = True

    old = Playlist(plex_sync.PLAYLIST_MOVIES)
    pending = Playlist("pending", fail_final=True)
    item = SimpleNamespace(title="Movie", year=2024, type="movie", ratingKey="42")
    server = SimpleNamespace(
        playlists=lambda: [old],
        createPlaylist=lambda _title, items: pending,
    )
    with patch.object(plex_sync, "PlexServer", return_value=server), \
            patch.object(plex_sync, "_build_index", return_value={"rating:42": item}), \
            patch.object(plex_sync, "_load_track", return_value={}), \
            patch.object(plex_sync, "_save_track"):
        try:
            plex_sync._sync_playlist("token", "user", [{"title": "Movie", "year": 2024, "type": "movie", "_rating_key": "42"}])
        except RuntimeError:
            pass
        else:
            raise AssertionError("promotion failure was not propagated")
    assert old.title == plex_sync.PLAYLIST_MOVIES
    assert not old.deleted
    assert pending.deleted
