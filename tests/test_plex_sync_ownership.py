import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app import plex_client, plex_sync


def _item(rating_key, title):
    return SimpleNamespace(ratingKey=rating_key, title=title, type="movie", year=2020)


@contextmanager
def _track_file(initial):
    with tempfile.TemporaryDirectory() as tmp:
        track = Path(tmp) / "watchlist_track.json"
        track.write_text(json.dumps(initial), encoding="utf-8")
        with patch.object(plex_sync, "WATCHLIST_TRACK_FILE", str(track)), \
                patch.object(plex_sync, "_track_file_lock", f"{track}.lock"):
            yield track


class FakeAccount:
    def __init__(self, on_watchlist, lock_state):
        self.on = set(on_watchlist)
        self.lock_state = lock_state
        self.added = []
        self.removed = []

    def onWatchlist(self, item):
        assert self.lock_state["held"], "watchlist read outside the ownership lock"
        return item.ratingKey in self.on

    def addToWatchlist(self, item):
        assert self.lock_state["held"], "watchlist write outside the ownership lock"
        if item.ratingKey in self.on:
            raise RuntimeError("already on the watchlist")  # plexapi raises BadRequest
        self.on.add(item.ratingKey)
        self.added.append(item.ratingKey)

    def removeFromWatchlist(self, item):
        assert self.lock_state["held"], "watchlist write outside the ownership lock"
        self.on.discard(item.ratingKey)
        self.removed.append(item.ratingKey)


def test_rerecommended_title_stays_on_watchlist_and_user_items_are_not_claimed():
    kept, dropped, new, users_own = _item(1, "Kept"), _item(2, "Dropped"), _item(3, "New"), _item(4, "Mine")
    index = {f"rating:{i.ratingKey}": i for i in (kept, dropped, new, users_own)}
    lock_state = {"held": False}

    @contextmanager
    def tracking_lock():
        lock_state["held"] = True
        try:
            yield
        finally:
            lock_state["held"] = False

    # Kept + Dropped were added by PlexMind last run; Mine was added by the user.
    account = FakeAccount(on_watchlist={1, 2, 4}, lock_state=lock_state)
    recs = [
        {"title": "Kept", "_rating_key": 1},
        {"title": "New", "_rating_key": 3},
        {"title": "Mine", "_rating_key": 4},
    ]
    with _track_file({"admin": ["rating:1", "rating:2"]}) as track, \
            patch.object(plex_sync, "PlexServer"), \
            patch.object(plex_sync, "_get_account", return_value=account), \
            patch.object(plex_sync, "_build_index", return_value=index), \
            patch.object(plex_sync, "_ownership_lock", tracking_lock):
        result = plex_sync._sync_watchlist("admin-token", "admin", recs)
        saved = json.loads(track.read_text(encoding="utf-8"))

    assert result["mode"] == "watchlist"
    assert result["matched"] == 3
    assert account.added == [3]
    assert account.removed == [2]  # never the re-recommended title, never the user's own
    assert account.on == {1, 3, 4}
    assert saved["admin"] == ["rating:1", "rating:3"]


def test_remove_collection_refuses_admin_token_fallback_for_other_users():
    server = MagicMock()
    with _track_file({"42": ["some title"]}), \
            patch.object(plex_client, "get_user_token", return_value=None), \
            patch.object(plex_sync, "PlexServer", server), \
            patch.object(plex_sync, "_get_account") as get_account:
        with pytest.raises(RuntimeError, match="refusing to act with the admin token"):
            plex_sync.remove_collection("42", "friend")
    server.assert_not_called()
    get_account.assert_not_called()


def test_remove_collection_refuses_when_token_lookup_fails():
    server = MagicMock()
    with _track_file({}), \
            patch.object(plex_client, "get_user_token", side_effect=OSError("plex.tv down")), \
            patch.object(plex_sync, "PlexServer", server):
        with pytest.raises(RuntimeError, match="could not obtain a Plex token"):
            plex_sync.remove_collection("42", "friend")
    server.assert_not_called()


def test_remove_collection_for_friend_uses_only_their_token_and_skips_watchlist():
    playlist = MagicMock()
    playlist.title = plex_sync.PLAYLIST_MOVIES
    other = MagicMock()
    other.title = "Their own playlist"
    server = MagicMock()
    server.return_value.playlists.return_value = [playlist, other]
    with _track_file({"42": ["some title"], "admin": ["rating:9"]}) as track, \
            patch.object(plex_client, "get_user_token", return_value="friend-token"), \
            patch.object(plex_sync, "PlexServer", server), \
            patch.object(plex_sync, "_get_account") as get_account:
        plex_sync.remove_collection("42", "friend")
        saved = json.loads(track.read_text(encoding="utf-8"))

    assert {call.args[1] for call in server.call_args_list} == {"friend-token"}
    get_account.assert_not_called()
    playlist.delete.assert_called_once()
    other.delete.assert_not_called()
    assert saved == {"admin": ["rating:9"]}
