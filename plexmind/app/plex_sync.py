"""
Plex sync — per-user recommendation delivery.

- Admin: watchlist (plex.tv API — only admin has a usable plex.tv token)
- Friends / managed users: server-side playlist (server token works fine)

We track what PlexMind added (watchlist_track.json) so we can cleanly
replace the previous set on each run without touching items the user
added themselves.
"""
import json
import logging
import os

from dotenv import load_dotenv
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer

load_dotenv()

PLEX_URL = os.getenv("PLEX_URL", "http://localhost:32400")
PLEX_TOKEN = os.getenv("PLEX_TOKEN", "")
MOVIES_SECTION = "Movies"
TV_SECTION = "TV Shows"
WATCHLIST_TRACK_FILE = os.getenv("WATCHLIST_TRACK_FILE", "data/watchlist_track.json")
PLAYLIST_MOVIES = "PlexMind Movies"
PLAYLIST_TV = "PlexMind TV Pilot"

log = logging.getLogger("plexmind.plex_sync")


# ---------------------------------------------------------------------------
# Tracking helpers — remember what PlexMind added to each user's watchlist
# ---------------------------------------------------------------------------

def _load_track() -> dict:
    try:
        with open(WATCHLIST_TRACK_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_track(data: dict) -> None:
    os.makedirs(os.path.dirname(WATCHLIST_TRACK_FILE) or ".", exist_ok=True)
    with open(WATCHLIST_TRACK_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Library index
# ---------------------------------------------------------------------------

def _build_index(server: PlexServer) -> dict[str, object]:
    index: dict[str, object] = {}
    for section_name in (MOVIES_SECTION, TV_SECTION):
        try:
            for item in server.library.section(section_name).all():
                index[item.title.lower()] = item
        except Exception:
            pass
    return index


# ---------------------------------------------------------------------------
# Watchlist sync
# ---------------------------------------------------------------------------

def _get_account(token: str) -> MyPlexAccount | None:
    try:
        return MyPlexAccount(token=token)
    except Exception:
        return None


def _sync_watchlist(token: str, user_key: str, recs: list[dict]) -> dict:
    """Admin-only: add recs to plex.tv Watchlist."""
    server = PlexServer(PLEX_URL, token)
    account = _get_account(token)
    if not account:
        return {"mode": "watchlist_error", "error": "could not authenticate with plex.tv"}

    index = _build_index(server)
    track = _load_track()
    prev_titles = set(track.get(user_key, []))

    # Remove previously PlexMind-added items from watchlist
    for title_lower in prev_titles:
        item = index.get(title_lower)
        if item:
            try:
                account.removeFromWatchlist(item)
            except Exception:
                pass

    # Add new recommendations to watchlist
    matched: list = []
    unmatched: list[str] = []
    new_titles: list[str] = []

    for rec in recs:
        title_lower = rec.get("title", "").lower()
        item = index.get(title_lower)
        if item:
            try:
                account.addToWatchlist(item)
                matched.append(rec["title"])
                new_titles.append(title_lower)
            except Exception as exc:
                log.debug("Watchlist add failed for %s: %s", rec["title"], exc)
                unmatched.append(rec["title"])
        else:
            unmatched.append(rec.get("title", ""))

    track[user_key] = new_titles
    _save_track(track)

    return {
        "mode": "watchlist",
        "matched": len(matched),
        "unmatched": unmatched,
    }


def _sync_playlist(token: str, user_key: str, recs: list[dict]) -> dict:
    """Friends / managed users: replace server-side playlists with current recs.

    Creates two playlists:
      - PlexMind Movies — movie recommendations
      - PlexMind TV Pilot — TV show recommendations (S01E01 of each show)
    """
    server = PlexServer(PLEX_URL, token)
    index = _build_index(server)

    # Delete existing PlexMind playlists for this user
    for pl in server.playlists():
        if pl.title in (PLAYLIST_MOVIES, PLAYLIST_TV, "PlexMind Picks"):
            try:
                pl.delete()
            except Exception:
                pass

    # Resolve recs into movie items and TV pilot episodes
    movie_items = []
    tv_items = []
    unmatched: list[str] = []
    matched_titles: list[str] = []

    for rec in recs:
        title_lower = rec.get("title", "").lower()
        item = index.get(title_lower)
        if item:
            if item.type == "show":
                try:
                    ep = item.episodes()[0]
                    tv_items.append(ep)
                except Exception:
                    continue
            else:
                movie_items.append(item)
            matched_titles.append(title_lower)
        else:
            unmatched.append(rec.get("title", ""))

    # Create movie playlist
    if movie_items:
        pl = server.createPlaylist(PLAYLIST_MOVIES, items=movie_items)
        try:
            pl.editSummary("Movie picks from PlexMind — updated monthly.")
        except Exception:
            pass

    # Create TV pilot playlist
    if tv_items:
        pl = server.createPlaylist(PLAYLIST_TV, items=tv_items)
        try:
            pl.editSummary("TV show picks from PlexMind — pilot episodes to get you started.")
        except Exception:
            pass

    # Track for cleanup
    track = _load_track()
    track[user_key] = matched_titles
    _save_track(track)

    return {
        "mode": "playlist",
        "matched": len(movie_items) + len(tv_items),
        "movies": len(movie_items),
        "tv": len(tv_items),
        "unmatched": unmatched,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def user_has_engaged_with_recs(user_id: str, user_token: str | None = None) -> bool:
    """Check if the user has watched ANY item from their current PlexMind playlists.

    Returns True if they've engaged (meaning we should refresh their recs),
    or True if they have no existing playlists (first run).
    Returns False if playlists exist but nothing has been watched.
    """
    token = PLEX_TOKEN if user_id == "admin" else (user_token or PLEX_TOKEN)
    try:
        server = PlexServer(PLEX_URL, token)
        playlists = [pl for pl in server.playlists()
                     if pl.title in (PLAYLIST_MOVIES, PLAYLIST_TV, "PlexMind Picks")]
        if not playlists:
            return True  # No existing playlists — first run, proceed

        for pl in playlists:
            for item in pl.items():
                # viewCount > 0 means the user watched it
                if getattr(item, "viewCount", 0) > 0:
                    return True
        return False  # Playlists exist but nothing watched
    except Exception:
        return True  # On error, proceed with refresh to be safe


def sync_to_plex(user_id: str, username: str, recs: list[dict], user_token: str | None = None) -> dict:
    """
    Deliver recommendations:
      - Admin → plex.tv Watchlist (admin has a full plex.tv token)
      - Everyone else → server-side playlist (server token is sufficient)
    """
    if not recs:
        return {"mode": "noop", "reason": "no recommendations"}

    token = PLEX_TOKEN if user_id == "admin" else (user_token or PLEX_TOKEN)

    if user_id == "admin":
        return _sync_watchlist(token, str(user_id), recs)

    if not user_token:
        return {"mode": "playlist_error", "error": "no server token for user"}
    return _sync_playlist(user_token, str(user_id), recs)


def purge_all_plexmind_collections() -> None:
    """Remove every PlexMind Collection from all library sections (legacy cleanup)."""
    try:
        server = PlexServer(PLEX_URL, PLEX_TOKEN)
        for section_name in (MOVIES_SECTION, TV_SECTION):
            try:
                section = server.library.section(section_name)
                for col in section.collections():
                    if "PlexMind" in col.title:
                        try:
                            col.visibility().updateVisibility(home=False, recommended=False, shared=False)
                        except Exception:
                            pass
                        try:
                            col.delete()
                        except Exception:
                            pass
            except Exception:
                pass
    except Exception:
        pass


def migrate_picks_to_split_playlists() -> dict:
    """One-time migration: split existing 'PlexMind Picks' into Movies + TV Pilot playlists."""
    from app import plex_client
    results = []

    for user in plex_client.get_users():
        uid = user["id"]
        username = user["username"]
        token = plex_client.get_user_token(uid)
        if not token:
            continue
        try:
            server = PlexServer(PLEX_URL, token)
            old_pl = None
            for pl in server.playlists():
                if pl.title == "PlexMind Picks":
                    old_pl = pl
                    break
            if not old_pl:
                continue

            items = old_pl.items()
            movie_items = [i for i in items if i.type == "movie"]
            tv_items = [i for i in items if i.type == "episode"]

            if movie_items:
                new_pl = server.createPlaylist(PLAYLIST_MOVIES, items=movie_items)
                try:
                    new_pl.editSummary("Movie picks from PlexMind — updated monthly.")
                except Exception:
                    pass
            if tv_items:
                new_pl = server.createPlaylist(PLAYLIST_TV, items=tv_items)
                try:
                    new_pl.editSummary("TV show picks from PlexMind — pilot episodes to get you started.")
                except Exception:
                    pass

            old_pl.delete()
            results.append({"user": username, "movies": len(movie_items), "tv": len(tv_items)})
            log.info("Migrated %s: %d movies, %d tv pilots", username, len(movie_items), len(tv_items))
        except Exception as exc:
            log.warning("Migration failed for %s: %s", username, exc)
            results.append({"user": username, "error": str(exc)})

    return {"migrated": len(results), "details": results}


def purge_all_plexmind_playlists() -> None:
    """Remove ALL PlexMind playlists for all users. Use sparingly — this wipes active playlists."""
    # Admin
    try:
        server = PlexServer(PLEX_URL, PLEX_TOKEN)
        for pl in server.playlists():
            if "PlexMind" in pl.title:
                try:
                    pl.delete()
                except Exception:
                    pass
    except Exception:
        pass

    # Managed users
    try:
        from app import plex_client
        for user in plex_client.get_users():
            try:
                token = plex_client.get_user_token(user["id"])
                if token and token != PLEX_TOKEN:
                    user_server = PlexServer(PLEX_URL, token)
                    for pl in user_server.playlists():
                        if "PlexMind" in pl.title:
                            try:
                                pl.delete()
                            except Exception:
                                pass
            except Exception:
                pass
    except Exception:
        pass


def remove_collection(user_id: str, username: str) -> None:
    """Remove PlexMind watchlist entries and any legacy collections/playlists for this user."""
    token = PLEX_TOKEN
    if user_id != "admin":
        try:
            from app import plex_client
            t = plex_client.get_user_token(user_id)
            if t and t != PLEX_TOKEN:
                token = t
        except Exception:
            pass

    # Clear watchlist entries we added
    track = _load_track()
    prev_titles = set(track.get(str(user_id), []))
    if prev_titles:
        try:
            server = PlexServer(PLEX_URL, token)
            index = _build_index(server)
            account = _get_account(token)
            if account:
                for title_lower in prev_titles:
                    item = index.get(title_lower)
                    if item:
                        try:
                            account.removeFromWatchlist(item)
                        except Exception:
                            pass
        except Exception:
            pass
        track.pop(str(user_id), None)
        _save_track(track)

    # Remove all PlexMind playlists (current + legacy)
    try:
        user_server = PlexServer(PLEX_URL, token)
        for pl in user_server.playlists():
            if pl.title in (PLAYLIST_MOVIES, PLAYLIST_TV, "PlexMind Picks"):
                try:
                    pl.delete()
                except Exception:
                    pass
    except Exception:
        pass
