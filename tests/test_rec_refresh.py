from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app import plex_sync

NOW = datetime(2026, 11, 10, 12, 0)


def playlist(title, days_old, watched=False):
    item = SimpleNamespace(viewCount=1 if watched else 0)
    return SimpleNamespace(title=title, updatedAt=NOW - timedelta(days=days_old), items=lambda: [item])


def due(*playlists):
    server = SimpleNamespace(playlists=lambda: list(playlists))
    with patch.object(plex_sync, "PlexServer", return_value=server):
        return plex_sync.user_has_engaged_with_recs("123", user_token="t", now=NOW)


def test_first_run_is_due():
    assert due() is True


def test_a_recent_untouched_set_is_kept():
    assert due(playlist(plex_sync.PLAYLIST_MOVIES, 3), playlist(plex_sync.PLAYLIST_TV, 3)) is False


def test_a_watched_set_is_replaced():
    assert due(playlist(plex_sync.PLAYLIST_MOVIES, 3, watched=True)) is True


def test_an_old_ignored_set_is_replaced():
    assert due(playlist(plex_sync.PLAYLIST_MOVIES, 200), playlist(plex_sync.PLAYLIST_TV, 200)) is True


def test_age_follows_the_newest_playlist():
    assert due(playlist(plex_sync.PLAYLIST_MOVIES, 200), playlist(plex_sync.PLAYLIST_TV, 2)) is False


def test_other_playlists_are_ignored():
    assert due(playlist("Road trip", 400)) is True