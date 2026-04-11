"""
Plex client — per-user watch history, token management, in-progress detection.
"""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv
from plexapi.server import PlexServer

load_dotenv()

PLEX_URL = os.getenv("PLEX_URL", "http://localhost:32400")
PLEX_TOKEN = os.getenv("PLEX_TOKEN", "")
MIN_WATCH_PCT = float(os.getenv("MIN_WATCH_PCT", "0.70"))


@dataclass
class WatchedItem:
    title: str
    year: int | None
    media_type: str                    # "movie" | "show"
    genres: list[str] = field(default_factory=list)
    rating: float | None = None
    tmdb_id: int | None = None
    plex_key: str = ""
    viewed_at: float | None = None     # Unix timestamp of last watch
    view_percent: float | None = None  # 0.0–1.0 if known


def _get_server() -> PlexServer:
    return PlexServer(PLEX_URL, PLEX_TOKEN)


def get_users() -> list[dict]:
    """Return list of managed users + admin. Each dict has id and username."""
    server = _get_server()
    users = []
    account = server.myPlexAccount()
    users.append({"id": "admin", "username": account.username, "type": "admin"})
    try:
        for u in account.users():
            users.append({"id": str(u.id), "username": u.title, "type": "managed"})
    except Exception:
        pass
    return users


def get_user_token(user_id: str) -> str | None:
    """
    Return a Plex auth token for a managed user, or None if unavailable.
    Admin always returns the server token.
    """
    if user_id == "admin":
        return PLEX_TOKEN
    try:
        server = _get_server()
        account = server.myPlexAccount()
        managed = {str(u.id): u for u in account.users()}
        if user_id in managed:
            return account.user(managed[user_id].title).get_token(server.machineIdentifier)
    except Exception:
        pass
    return None


def get_watch_history(user_id: str) -> list[WatchedItem]:
    """
    Return deduplicated watch history for the given user.
    TV shows are deduplicated to one entry per show.
    Movies watched less than MIN_WATCH_PCT are excluded.
    """
    server = _get_server()

    if user_id != "admin":
        try:
            account = server.myPlexAccount()
            managed = {str(u.id): u for u in account.users()}
            if user_id in managed:
                token = account.user(managed[user_id].title).get_token(server.machineIdentifier)
                server = PlexServer(PLEX_URL, token)
        except Exception as exc:
            raise RuntimeError(f"Cannot obtain token for user {user_id}: {exc}") from exc

    seen_shows: set[str] = set()
    items: list[WatchedItem] = []

    try:
        history = server.library.history(maxresults=500)
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch Plex history: {exc}") from exc

    for entry in history:
        media_type = entry.type

        # Skip non-video entries (music tracks, photos)
        if media_type not in ("movie", "episode"):
            continue

        # viewedAt is a datetime; convert to Unix timestamp
        viewed_at_dt = getattr(entry, "viewedAt", None)
        viewed_at = viewed_at_dt.timestamp() if viewed_at_dt else None

        if media_type == "movie":
            # Partial-watch filter using viewOffset vs duration on library item
            view_pct = None
            try:
                lib_item = server.fetchItem(entry.key)
                dur = getattr(lib_item, "duration", None)
                off = getattr(lib_item, "viewOffset", None)
                if dur and dur > 0 and off is not None:
                    view_pct = (off / dur) if off > 0 else (1.0 if getattr(lib_item, "viewCount", 0) > 0 else 0.0)
                    # If currently paused mid-way AND viewCount == 0 → partial watch → skip
                    view_count = getattr(lib_item, "viewCount", 1)
                    if view_count == 0 and view_pct < MIN_WATCH_PCT:
                        continue
            except Exception:
                pass

            items.append(WatchedItem(
                title=entry.title,
                year=getattr(entry, "year", None),
                media_type="movie",
                genres=[g.tag for g in getattr(entry, "genres", [])],
                rating=getattr(entry, "audienceRating", None),
                plex_key=entry.key,
                viewed_at=viewed_at,
                view_percent=view_pct,
            ))

        elif media_type == "episode":
            show_title = getattr(entry, "grandparentTitle", None) or entry.title
            if show_title in seen_shows:
                continue
            seen_shows.add(show_title)
            try:
                show = entry.show() if getattr(entry, "grandparentKey", None) else None
            except Exception:
                show = None
            genres = [g.tag for g in getattr(show, "genres", [])] if show else []
            items.append(WatchedItem(
                title=show_title,
                year=getattr(show, "year", None) if show else None,
                media_type="show",
                genres=genres,
                rating=getattr(show, "audienceRating", None) if show else None,
                plex_key=getattr(show, "key", entry.key),
                viewed_at=viewed_at,
            ))

    # Deduplicate movies by title (keep most recent)
    seen: dict[tuple, WatchedItem] = {}
    for item in items:
        key = (item.title.lower(), item.media_type)
        existing = seen.get(key)
        if not existing or (item.viewed_at or 0) > (existing.viewed_at or 0):
            seen[key] = item

    return list(seen.values())


def get_in_progress_titles(user_id: str) -> set[str]:
    """
    Return lowercase titles of items currently in progress (started but not finished).
    These are excluded from the candidate pool so we don't re-recommend them.
    """
    server = _get_server()
    if user_id != "admin":
        token = get_user_token(user_id)
        if token:
            try:
                server = PlexServer(PLEX_URL, token)
            except Exception:
                pass
    try:
        on_deck = server.library.onDeck()
        return {getattr(i, "title", "").lower() for i in on_deck}
    except Exception:
        return set()
