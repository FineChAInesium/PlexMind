#!/usr/bin/env python3
"""
What to translate first, and what the model should know about each title.

  translation_plan.py order
      Reads NUL-separated subtitle or video paths on stdin and writes them back
      most wanted first. A one-line summary goes to stderr. Anything that goes
      wrong with Plex still returns every path, in natural name order.
  translation_plan.py context VIDEO
      Prints a short description of VIDEO from Plex: show, genres, synopsis
      and character names. Prints nothing if Plex has nothing to offer.

The order, across every user on the server:
  1. the next few episodes after where each active viewer has got to
  2. the rest of the shows people are watching now
  3. recently added titles
  4. anything else that has been watched
  5. everything else
"""
import os
import re
import sys
import time

import subtitle_inventory as inv

ACTIVE_DAYS = 45
RECENTLY_ADDED_DAYS = 45
NEXT_UP_EPISODES = 5
HISTORY_ENTRIES = 5000
MAX_SYNOPSIS_CHARS = 300
MAX_CHARACTERS = 10
_DAY = 86400
_SOURCE_SUFFIX = re.compile(r"(\.(hi|sdh))?\.[a-z]{2,3}(\.(hi|sdh))?\.srt$", re.I)
_VIDEO_SUFFIX = re.compile(r"\.(mkv|mp4|avi|mov|wmv|m4v)$", re.I)
TIER_NAMES = ("next up", "being watched", "recently added", "watched before", "the rest")


def natural_key(text):
    """Sort so that E2 comes before E10."""
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", text)]


def base_of(path):
    """The lowercase path without its video or subtitle suffix."""
    return _VIDEO_SUFFIX.sub("", _SOURCE_SUFFIX.sub("", path)).lower()


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def library_catalog(get=None):
    """Every movie and episode: {lowercase base path: details}."""
    catalog = {}
    for video, path in inv.library_videos(get):
        show = video.get("grandparentRatingKey")
        catalog[base_of(path)] = {
            "key": video.get("ratingKey"), "show": show,
            "season": _int(video.get("parentIndex")), "episode": _int(video.get("index")),
            "added": _int(video.get("addedAt")),
        }
    return catalog


def view_history(get=None):
    """Newest-first (rating key, show key, account, when) for all users' plays."""
    page = (get or inv._plex_get)("/status/sessions/history/all?sort=viewedAt:desc"
                                  f"&X-Plex-Container-Start=0&X-Plex-Container-Size={HISTORY_ENTRIES}")
    rows = []
    for entry in page:
        grandparent = (entry.get("grandparentKey") or "").rsplit("/", 1)[-1] or None
        rows.append((entry.get("ratingKey"), grandparent, entry.get("accountID"), _int(entry.get("viewedAt"))))
    return rows


def rank(paths, catalog, history, now=None):
    """(ordered paths, count per tier)."""
    now = now or time.time()
    by_key = {item["key"]: item for item in catalog.values()}
    episodes: dict = {}
    show_added: dict = {}
    for item in catalog.values():
        if item["show"]:
            episodes.setdefault(item["show"], []).append((item["season"], item["episode"]))
            show_added[item["show"]] = max(show_added.get(item["show"], 0), item["added"])
    for positions in episodes.values():
        positions.sort()

    last_view: dict = {}          # show or movie key -> newest play by anyone
    next_up: dict = {}            # (show, season, episode) -> newest play that makes it next
    seen_viewers = set()
    for rating_key, show, account, when in history:
        item = by_key.get(rating_key)
        show = show or (item or {}).get("show")
        target = show or rating_key
        last_view[target] = max(last_view.get(target, 0), when)
        if not (item and show) or (account, show) in seen_viewers:
            continue
        seen_viewers.add((account, show))   # newest first, so this is where they are now
        if now - when > ACTIVE_DAYS * _DAY:
            continue
        position = (item["season"], item["episode"])
        for season, episode in [p for p in episodes.get(show, []) if p > position][:NEXT_UP_EPISODES]:
            key = (show, season, episode)
            next_up[key] = max(next_up.get(key, 0), when)

    def sort_key(path):
        item = catalog.get(base_of(path))
        if not item:
            return (4, 0, natural_key(path))
        show, position = item["show"], (item["season"], item["episode"])
        when = last_view.get(show or item["key"], 0)
        if show and (show, *position) in next_up:
            return (0, -next_up[(show, *position)], position)
        if show and now - when <= ACTIVE_DAYS * _DAY:
            return (1, -when, show, position)
        added = show_added.get(show, 0) if show else item["added"]
        if now - added <= RECENTLY_ADDED_DAYS * _DAY:
            return (2, -added, show or "", position)
        if when:
            return (3, -when, show or item["key"], position)
        return (4, 0, natural_key(path))

    keyed = [(sort_key(path), path) for path in paths]
    # Tuples of one tier can differ in shape only between tiers, which the first element settles.
    keyed.sort(key=lambda pair: pair[0])
    counts = [0] * len(TIER_NAMES)
    for key, _ in keyed:
        counts[key[0]] += 1
    return [path for _, path in keyed], counts


def order(paths, get=None):
    """(ordered paths, summary). Plex trouble only costs the priority."""
    if get is None and not (inv.PLEX_URL and inv.PLEX_TOKEN):
        return sorted(paths, key=natural_key), f"{len(paths)} items in name order (Plex not configured)"
    try:
        ordered, counts = rank(paths, library_catalog(get), view_history(get))
    except Exception as exc:
        return sorted(paths, key=natural_key), f"{len(paths)} items in name order (Plex unavailable: {type(exc).__name__})"
    return ordered, ", ".join(f"{count} {name}" for count, name in zip(counts, TIER_NAMES))


def _short(text, limit=MAX_SYNOPSIS_CHARS):
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "..."


def _first(container):
    return next(iter(container), None)


def _tags(element, name):
    return [child.get("tag") for child in element.findall(name) if child.get("tag")]


def context_text(video, get=None, index=None):
    """A few lines describing VIDEO, or "" if Plex cannot say."""
    if get is None and not (inv.PLEX_URL and inv.PLEX_TOKEN):
        return ""
    try:
        fetch = get or inv._plex_get
        rating_key = (index if index is not None else inv.plex_index()).get(video)
        item = _first(fetch(f"/library/metadata/{rating_key}")) if rating_key else None
        if item is None:
            return ""
        lines = []
        if item.get("type") == "episode":
            show = _first(fetch(f"/library/metadata/{item.get('grandparentRatingKey')}"))
            subject = show if show is not None else item
            lines.append(f"Series: {item.get('grandparentTitle') or subject.get('title') or 'unknown'}")
            lines.append(f"Episode: S{_int(item.get('parentIndex')):02d}E{_int(item.get('index')):02d} "
                         f"\"{item.get('title') or ''}\"")
            if subject is not item and subject.get("summary"):
                lines.append(f"About the series: {_short(subject.get('summary'))}")
            if item.get("summary"):
                lines.append(f"This episode: {_short(item.get('summary'))}")
        else:
            subject = item
            lines.append(f"Film: {item.get('title') or 'unknown'}" + (f" ({item.get('year')})" if item.get("year") else ""))
            if item.get("summary"):
                lines.append(f"Plot: {_short(item.get('summary'))}")
        genres = _tags(subject, "Genre")
        if genres:
            lines.append(f"Genres: {', '.join(genres[:4])}")
        characters = list(dict.fromkeys(
            role.get("role") for role in subject.findall("Role") if role.get("role")))[:MAX_CHARACTERS]
        if characters:
            lines.append(f"Characters: {'; '.join(characters)}")
        return "\n".join(lines)
    except Exception:
        return ""


def main(argv):
    if argv[1:] == ["order"]:
        paths = [p.decode("utf-8", "surrogateescape") for p in sys.stdin.buffer.read().split(b"\0") if p]
        ordered, summary = order(paths)
        sys.stdout.buffer.write(b"".join(p.encode("utf-8", "surrogateescape") + b"\0" for p in ordered))
        sys.stdout.buffer.flush()
        print(summary, file=sys.stderr)
        return 0
    if len(argv) == 3 and argv[1] == "context":
        text = context_text(argv[2])
        if text:
            print(text)
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
