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
import tempfile
import uuid
import fcntl
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from dotenv import load_dotenv
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer

load_dotenv()

PLEX_URL = os.getenv("PLEX_URL", "http://localhost:32400")
PLEX_TOKEN = os.getenv("PLEX_TOKEN", "")
WATCHLIST_TRACK_FILE = os.getenv(
    "WATCHLIST_TRACK_FILE",
    str(Path(os.getenv("DATA_DIR", "/app/data")) / "watchlist_track.json"),
)
PLAYLIST_MOVIES = "PlexMind Movies"
PLAYLIST_TV = "PlexMind TV Pilot"

log = logging.getLogger("plexmind.plex_sync")
_track_lock = RLock()
_track_file_lock = f"{WATCHLIST_TRACK_FILE}.lock"


@contextmanager
def _ownership_lock():
    directory = os.path.dirname(_track_file_lock) or "."
    os.makedirs(directory, exist_ok=True)
    with open(_track_file_lock, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


# ---------------------------------------------------------------------------
# Tracking helpers — remember what PlexMind added to each user's watchlist
# ---------------------------------------------------------------------------

def _load_track() -> dict:
    try:
        with open(WATCHLIST_TRACK_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Watchlist ownership state is unreadable: {exc}") from exc


def _save_track(data: dict) -> None:
    directory = os.path.dirname(WATCHLIST_TRACK_FILE) or "."
    os.makedirs(directory, exist_ok=True)
    with _track_lock, tempfile.NamedTemporaryFile("w", dir=directory, delete=False) as f:
        tmp = f.name
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, WATCHLIST_TRACK_FILE)


# ---------------------------------------------------------------------------
# Library index
# ---------------------------------------------------------------------------

def _build_index(server: PlexServer) -> dict[str, object]:
    items: list[object] = []
    for section in server.library.sections():
        if getattr(section, "type", "") not in {"movie", "show"}:
            continue
        try:
            for item in section.all():
                items.append(item)
        except Exception as exc:
            log.warning("Could not index Plex section %s: %s", getattr(section, "title", "unknown"), exc)
    index: dict[str, object] = {}
    counts: dict[str, int] = {}
    for item in items:
        title = item.title.casefold().strip()
        counts[title] = counts.get(title, 0) + 1
        media_type = getattr(item, "type", "")
        year = getattr(item, "year", None) or ""
        index[f"{title}|{year}|{media_type}"] = item
        rating_key = getattr(item, "ratingKey", None)
        if rating_key:
            index[f"rating:{rating_key}"] = item
    for item in items:
        title = item.title.casefold().strip()
        if counts[title] == 1:
            index[title] = item
    return index


def _resolve(index: dict[str, object], rec: dict) -> object | None:
    rating_key = rec.get("_rating_key") or rec.get("rating_key")
    if rating_key:
        resolved = index.get(f"rating:{rating_key}")
        if resolved is not None:
            return resolved
    title = str(rec.get("title", "")).casefold().strip()
    year = rec.get("year") or ""
    media_type = "show" if rec.get("type") in ("tv", "show") else "movie"
    return index.get(f"{title}|{year}|{media_type}") or index.get(title)


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
    with _ownership_lock():
        track = _load_track()
    previous = set(track.get(user_key, []))

    # Add new recommendations to watchlist
    matched: list = []
    unmatched: list[str] = []
    new_identities: list[str] = []

    for rec in recs:
        item = _resolve(index, rec)
        if item:
            try:
                account.addToWatchlist(item)
                matched.append(rec["title"])
                rating_key = getattr(item, "ratingKey", None)
                new_identities.append(f"rating:{rating_key}" if rating_key else rec.get("title", "").casefold().strip())
            except Exception as exc:
                log.debug("Watchlist add failed for %s: %s", rec["title"], exc)
                unmatched.append(rec["title"])
        else:
            unmatched.append(rec.get("title", ""))

    if not new_identities and previous:
        return {"mode": "watchlist_error", "error": "replacement resolved no items; previous set retained"}

    # Remove the old set only after at least one replacement was added.
    owned_identities = set(new_identities)
    cleanup_failed: list[str] = []
    for identity in previous - owned_identities:
        item = index.get(identity)
        if item:
            try:
                account.removeFromWatchlist(item)
            except Exception as exc:
                log.warning("Could not remove obsolete watchlist item %s: %s", identity, exc)
                owned_identities.add(identity)
                cleanup_failed.append(identity)
        else:
            owned_identities.add(identity)
            cleanup_failed.append(identity)

    with _ownership_lock():
        track = _load_track()
        track[user_key] = sorted(owned_identities)
        _save_track(track)

    return {
        "mode": "watchlist_partial" if unmatched or cleanup_failed else "watchlist",
        "matched": len(matched),
        "unmatched": unmatched,
        "cleanup_failed": cleanup_failed,
    }


def _sync_playlist(token: str, user_key: str, recs: list[dict]) -> dict:
    """Friends / managed users: replace server-side playlists with current recs.

    Creates two playlists:
      - PlexMind Movies — movie recommendations
      - PlexMind TV Pilot — TV show recommendations (S01E01 of each show)
    """
    server = PlexServer(PLEX_URL, token)
    index = _build_index(server)

    all_playlists = list(server.playlists())
    for final_title in (PLAYLIST_MOVIES, PLAYLIST_TV):
        active = [pl for pl in all_playlists if pl.title == final_title]
        backups = [
            pl for pl in all_playlists
            if pl.title.startswith(f"{final_title} (PlexMind backup ")
        ]
        if not active and backups:
            backups[-1].editTitle(final_title)
    all_playlists = list(server.playlists())
    existing = [pl for pl in all_playlists
                if pl.title in (PLAYLIST_MOVIES, PLAYLIST_TV, "PlexMind Picks")]
    stale_pending = [
        pl for pl in all_playlists
        if " (PlexMind pending " in pl.title
    ]

    # Resolve recs into movie items and TV pilot episodes
    movie_items = []
    tv_items = []
    unmatched: list[str] = []
    matched_titles: list[str] = []

    for rec in recs:
        title_lower = rec.get("title", "").lower()
        item = _resolve(index, rec)
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

    if not movie_items and not tv_items:
        return {"mode": "playlist_error", "error": "replacement resolved no items; previous playlists retained"}

    created = []
    transaction = uuid.uuid4().hex[:8]
    suffix = f" (PlexMind pending {transaction})"
    # Create and verify temporary replacements before deleting the active playlists.
    if movie_items:
        pl = server.createPlaylist(PLAYLIST_MOVIES + suffix, items=movie_items)
        created.append((pl, PLAYLIST_MOVIES))
        try:
            pl.editSummary("Movie picks from PlexMind — updated monthly.")
        except Exception:
            pass

    # Create TV pilot playlist
    if tv_items:
        pl = server.createPlaylist(PLAYLIST_TV + suffix, items=tv_items)
        created.append((pl, PLAYLIST_TV))
        try:
            pl.editSummary("TV show picks from PlexMind — pilot episodes to get you started.")
        except Exception:
            pass

    backups = []
    try:
        final_titles = {final_title for _, final_title in created}
        for pl in existing:
            if pl.title in final_titles:
                original = pl.title
                pl.editTitle(f"{original} (PlexMind backup {transaction})")
                backups.append((pl, original))
        for pl, final_title in created:
            pl.editTitle(final_title)
        for pl, _ in backups:
            pl.delete()
        for pl in existing:
            if pl.title == "PlexMind Picks" or (
                pl.title in (PLAYLIST_MOVIES, PLAYLIST_TV)
                and pl.title not in final_titles
            ):
                pl.delete()
        for pl in stale_pending:
            pl.delete()
    except Exception:
        for pl, original in backups:
            try:
                pl.editTitle(original)
            except Exception:
                pass
        for pl, _ in created:
            try:
                pl.delete()
            except Exception:
                pass
        raise

    # Track for cleanup
    with _ownership_lock():
        track = _load_track()
        track[user_key] = matched_titles
        _save_track(track)

    return {
        "mode": "playlist_partial" if unmatched else "playlist",
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
    except Exception as exc:
        log.warning("Could not verify recommendation engagement for %s; retaining current set: %s", user_id, exc)
        return False


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
        for section in server.library.sections():
            if getattr(section, "type", "") not in {"movie", "show"}:
                continue
            try:
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
            except Exception as exc:
                log.warning("Could not purge Plex section %s: %s", getattr(section, "title", "unknown"), exc)
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
    failures = []
    with _ownership_lock():
        track = _load_track()
        previous = set(track.get(str(user_id), []))
        remaining = set()
        if previous:
            try:
                server = PlexServer(PLEX_URL, token)
                index = _build_index(server)
                account = _get_account(token)
                if not account:
                    raise RuntimeError("could not authenticate watchlist account")
                for identity in previous:
                    item = index.get(identity)
                    if not item:
                        continue
                    try:
                        account.removeFromWatchlist(item)
                    except Exception as exc:
                        failures.append(f"watchlist {identity}: {exc}")
                        remaining.add(identity)
            except Exception as exc:
                failures.append(f"watchlist cleanup: {exc}")
                remaining = previous
        if remaining:
            track[str(user_id)] = sorted(remaining)
        else:
            track.pop(str(user_id), None)
        _save_track(track)

    # Remove all PlexMind playlists (current + legacy)
    try:
        user_server = PlexServer(PLEX_URL, token)
        for pl in user_server.playlists():
            if pl.title in (PLAYLIST_MOVIES, PLAYLIST_TV, "PlexMind Picks"):
                try:
                    pl.delete()
                except Exception as exc:
                    failures.append(f"playlist {pl.title}: {exc}")
    except Exception as exc:
        failures.append(f"playlist cleanup: {exc}")
    if failures:
        raise RuntimeError("; ".join(failures))
