"""
Core recommendation engine — all 9 features:

  1. Recency-weighted genre fingerprint (recent watches count 3×)
  2. Partial-watch + in-progress exclusion from candidate pool
  3. Feedback genre penalties (disliked items' genres reduce candidate scores)
  4. Original-language awareness (dominant language(s) in history get a boost)
  5. "Because you watched X" reason format
  6. Trending boost (TMDB weekly trending titles score higher)
  7. Re-recommendation suppression (SUPPRESSION_DAYS window)
  8. Candidate pre-filter (top CANDIDATE_POOL_SIZE by composite score before LLM)
  9. Plex collection sort by LLM rank order + IMDB rating (in plex_sync)

Metadata sources (concurrent): TMDB + TVDB + OMDB/IMDB
"""
import asyncio
import os
import re
import time
from collections import Counter

from dotenv import load_dotenv

from app import cache, imdb_client, llm_client, plex_client, plex_sync, tmdb_client, tvdb_client

load_dotenv()

MAX_RECS = int(os.getenv("MAX_RECOMMENDATIONS", "10"))
CANDIDATE_POOL_SIZE = int(os.getenv("CANDIDATE_POOL_SIZE", "40"))
RECENCY_HOT_DAYS = 90    # watched within this window → 3× weight
RECENCY_WARM_DAYS = 180  # within this → 2× weight
TRENDING_BOOST = 0.08
LANGUAGE_BOOST = 0.06
RATING_BOOST_MAX = 0.04  # scaled by IMDB rating / 10

# Deep cut: obscure but genre-matched hidden gem (like Spotify's low-key picks)
DEEP_CUT_MAX_VOTES = 8000   # under this TMDB vote_count = not widely seen
DEEP_CUT_MIN_RATING = 6.3   # but still has to be decent

SYSTEM_PROMPT = """\
You are a personalised media recommendation engine. Select titles from an AVAILABLE
LIBRARY that a specific user will genuinely enjoy based on their personal watch history.
Match the user's ACTUAL taste — if they watch reality TV, recommend reality TV; if they
watch horror, recommend horror. Never substitute "prestige" picks for what the user
actually enjoys. Respect every genre equally.
Respond ONLY with a valid JSON array — no prose, no markdown, no explanation.
Each element must have exactly these keys:
  "title"      — exact match to the AVAILABLE LIBRARY list
  "year"       — integer or null
  "type"       — "movie" or "tv"
  "reason"     — one sentence beginning with "Because you watched [specific title from history],"
  "poster_url" — null (filled in later)\
"""


# ---------------------------------------------------------------------------
# Title normalisation
# ---------------------------------------------------------------------------

def _normalise(title: str) -> str:
    """Strip region suffixes and trailing years for fuzzy dedup."""
    t = title.lower().strip()
    t = re.sub(r"\s*\([a-z]{2}\)$", "", t)
    t = re.sub(r"\s*\(\d{4}\)$", "", t)
    return t.strip()


# ---------------------------------------------------------------------------
# Library helpers
# ---------------------------------------------------------------------------

def _get_unwatched_library(
    watched_titles: set[str],
    in_progress: set[str],
) -> list[dict]:
    """
    Return dicts with title, year, media_type, and plex_genres for library items
    the user hasn't watched and isn't currently in the middle of watching.
    Plex genres are used for the lightweight pre-score before API enrichment.
    """
    from plexapi.server import PlexServer
    server = PlexServer(plex_client.PLEX_URL, plex_client.PLEX_TOKEN)
    normalised_watched = {_normalise(t) for t in watched_titles | in_progress}
    items: list[dict] = []
    for section_name, media_type in [("Movies", "movie"), ("TV Shows", "show")]:
        try:
            section = server.library.section(section_name)
            for item in section.all():
                if _normalise(item.title) not in normalised_watched:
                    items.append({
                        "title": item.title,
                        "year": getattr(item, "year", None),
                        "media_type": media_type,
                        "plex_genres": [g.tag for g in getattr(item, "genres", [])],
                    })
        except Exception:
            pass
    return items


# ---------------------------------------------------------------------------
# Metadata enrichment
# ---------------------------------------------------------------------------

async def _enrich_all(items: list[tuple[str, int | None, str]]) -> list[dict]:
    """Enrich (title, year, media_type) list from TMDB + TVDB + OMDB concurrently."""
    if not items:
        return []

    tv_items = [(t, y) for t, y, mt in items if mt in ("show", "tv")]

    tmdb_metas, tvdb_metas, omdb_metas = await asyncio.gather(
        tmdb_client.enrich_batch(items),
        tvdb_client.enrich_batch_tv(tv_items) if tv_items else asyncio.sleep(0, result=[]),
        imdb_client.enrich_batch([(t, y, mt) for t, y, mt in items]),
    )

    tvdb_by_title: dict[str, dict] = {}
    for (t, _), meta in zip(tv_items, tvdb_metas or []):
        if meta:
            tvdb_by_title[t.lower()] = meta

    merged: list[dict] = []
    for i, (title, year, media_type) in enumerate(items):
        tmdb = tmdb_metas[i] if i < len(tmdb_metas) else None
        omdb = omdb_metas[i] if i < len(omdb_metas) else None
        tvdb = tvdb_by_title.get(title.lower()) if media_type in ("show", "tv") else None

        genres: list[str] = []
        if tmdb:
            genres += tmdb.genres
        if tvdb and tvdb.get("genres"):
            genres += tvdb["genres"]
        if omdb and omdb.get("genre_tags"):
            genres += omdb["genre_tags"]

        entry: dict = {
            "title": title,
            "year": year,
            "media_type": media_type,
            "poster_url": tmdb.poster_url if tmdb else None,
            "genres": list(dict.fromkeys(genres)),
            "keywords": tmdb.keywords[:10] if tmdb else [],
            "cast": tmdb.cast[:5] if tmdb else [],
            "director": tmdb.director if tmdb else None,
            "original_language": tmdb.original_language if tmdb else None,
            "tmdb_rating": tmdb.vote_average if tmdb else None,
            "vote_count": tmdb.vote_count if tmdb else None,
            "imdb_rating": omdb.get("imdb_rating") if omdb else None,
            "metascore": omdb.get("metascore") if omdb else None,
            "rt_score": omdb.get("rt_score") if omdb else None,
            "awards": omdb.get("awards") if omdb else None,
            "overview": (tmdb.overview if tmdb else None) or (omdb.get("plot") if omdb else None) or "",
            "similar": tmdb.similar[:4] if tmdb else [],
            "tv_status": tvdb.get("status") if tvdb else None,
            "network": tvdb.get("network") if tvdb else None,
        }
        merged.append(entry)

    return merged


# ---------------------------------------------------------------------------
# Recency weighting
# ---------------------------------------------------------------------------

def _recency_weight(viewed_at: float | None) -> float:
    if not viewed_at:
        return 1.0
    days_ago = (time.time() - viewed_at) / 86400
    if days_ago <= RECENCY_HOT_DAYS:
        return 3.0
    if days_ago <= RECENCY_WARM_DAYS:
        return 2.0
    return 1.0


def _build_fingerprint(
    history_meta: list[dict],
    history_items: list[plex_client.WatchedItem],
) -> tuple[Counter, Counter, Counter]:
    """
    Build recency-weighted genre, keyword, and language Counters from watch history.
    Returns (genre_weights, kw_weights, lang_counts).
    """
    genre_weights: Counter = Counter()
    kw_weights: Counter = Counter()
    lang_counts: Counter = Counter()

    for entry, watched in zip(history_meta, history_items):
        w = _recency_weight(watched.viewed_at)
        for g in entry.get("genres", []):
            genre_weights[g.lower()] += w
        for k in entry.get("keywords", []):
            kw_weights[k.lower()] += w
        lang = entry.get("original_language")
        if lang and lang != "en":  # non-English languages are a strong signal
            lang_counts[lang] += w

    return genre_weights, kw_weights, lang_counts


def _disliked_genres(user_id: str, meta_by_title: dict[str, dict]) -> set[str]:
    """Return genre set built from all titles the user has disliked."""
    genres: set[str] = set()
    for fb in cache.get_user_feedback(user_id):
        if fb["rating"] == "dislike":
            meta = meta_by_title.get(fb["title"].lower(), {})
            for g in meta.get("genres", []):
                genres.add(g.lower())
    return genres


# ---------------------------------------------------------------------------
# Lightweight pre-score (Plex genres only — no API calls)
# ---------------------------------------------------------------------------

PRESCORE_POOL = int(os.getenv("PRESCORE_POOL_SIZE", "100"))


def _prescore_by_plex_genres(
    library_items: list[dict],
    history: list[plex_client.WatchedItem],
    pool_size: int = PRESCORE_POOL,
) -> list[dict]:
    """
    Fast genre-only scoring using Plex's built-in genre tags.
    Returns the top `pool_size` items — these are the only ones we'll enrich via API.
    """
    if len(library_items) <= pool_size:
        return library_items

    # Build genre weights from watch history (using Plex genres on WatchedItems)
    genre_w: Counter = Counter()
    for item in history:
        w = _recency_weight(item.viewed_at)
        for g in item.genres:
            genre_w[g.lower()] += w

    total = max(sum(genre_w.values()), 1)

    def _score(item: dict) -> float:
        genres = {g.lower() for g in item.get("plex_genres", [])}
        return sum(genre_w.get(g, 0) for g in genres) / total

    scored = sorted(library_items, key=_score, reverse=True)
    return scored[:pool_size]


# ---------------------------------------------------------------------------
# Candidate pre-filter (full enriched metadata)
# ---------------------------------------------------------------------------

def _score_candidate(
    candidate: dict,
    genre_weights: Counter,
    kw_weights: Counter,
    lang_counts: Counter,
    disliked_genres: set[str],
    trending_titles: set[str],
    dominant_langs: set[str],
) -> float:
    total_genre_w = max(sum(genre_weights.values()), 1)
    total_kw_w = max(sum(kw_weights.values()), 1)

    cand_genres = {g.lower() for g in candidate.get("genres", [])}
    cand_kws = {k.lower() for k in candidate.get("keywords", [])}

    genre_score = sum(genre_weights.get(g, 0) for g in cand_genres) / total_genre_w
    kw_score = sum(kw_weights.get(k, 0) for k in cand_kws) / total_kw_w

    # Language boost
    lang = candidate.get("original_language")
    lang_boost = LANGUAGE_BOOST if lang in dominant_langs else 0.0

    # Trending boost
    trending_boost = TRENDING_BOOST if candidate["title"].lower() in trending_titles else 0.0

    # Rating boost (scaled, capped)
    imdb = candidate.get("imdb_rating") or 0
    rating_boost = RATING_BOOST_MAX * min(float(imdb) / 10.0, 1.0)

    # Dislike genre penalty — proportional to overlap
    penalty = 0.25 * len(cand_genres & disliked_genres)

    # Small baseline so items with no enrichment data aren't zeroed out entirely
    baseline = 0.01

    return max(0.0, baseline + genre_score * 0.5 + kw_score * 0.3
               + lang_boost + trending_boost + rating_boost - penalty)


def _prefilter(
    candidates: list[dict],
    history_meta: list[dict],
    history_items: list[plex_client.WatchedItem],
    user_id: str,
    trending_titles: set[str],
    shown_recs: dict[str, float],
    pool_size: int,
    n: int,
) -> list[dict]:
    """
    Score candidates, exclude suppressed titles, preserve movie/TV ratio,
    return top `pool_size` items.
    """
    if len(candidates) <= pool_size:
        return candidates

    genre_weights, kw_weights, lang_counts = _build_fingerprint(history_meta, history_items)
    dominant_langs = {lang for lang, _ in lang_counts.most_common(3)} - {"en"}
    meta_by_title = {e["title"].lower(): e for e in history_meta}
    bad_genres = _disliked_genres(user_id, meta_by_title)

    # Suppression: exclude titles shown recently that have no positive feedback
    feedback_liked = {fb["title"].lower() for fb in cache.get_user_feedback(user_id)
                      if fb["rating"] in ("like", "watched")}
    suppressed = {t for t, ts in shown_recs.items() if t not in feedback_liked}

    active = [c for c in candidates if c["title"].lower() not in suppressed]
    if len(active) < n:
        active = candidates  # fall back if suppression would leave too few

    # Preserve proportional movie/TV ratio from history
    history_types = [h["media_type"] for h in history_meta]
    movie_ratio = history_types.count("movie") / max(len(history_types), 1)
    n_movies = max(1, round(pool_size * movie_ratio))
    n_shows = pool_size - n_movies

    movies = [c for c in active if c["media_type"] == "movie"]
    shows = [c for c in active if c["media_type"] != "movie"]

    def _top(items: list[dict], k: int) -> list[dict]:
        scored = sorted(
            items,
            key=lambda c: _score_candidate(
                c, genre_weights, kw_weights, lang_counts,
                bad_genres, trending_titles, dominant_langs,
            ),
            reverse=True,
        )
        return scored[:k]

    return _top(movies, n_movies) + _top(shows, n_shows)


# ---------------------------------------------------------------------------
# Deep cut (hidden gem) picker
# ---------------------------------------------------------------------------

def _pick_deep_cut(
    library_meta: list[dict],
    genre_weights: Counter,
    kw_weights: Counter,
    lang_counts: Counter,
    disliked_genres: set[str],
    trending_titles: set[str],
    dominant_langs: set[str],
    exclude_titles: set[str],
) -> dict | None:
    """
    Find one 'deep cut': a library item that strongly matches the user's taste
    but is largely unknown (low TMDB vote_count).  Like Spotify's low-key gem.
    Returns a rec dict with deep_cut=True, or None if nothing qualifies.
    """
    scored: list[tuple[float, dict]] = []
    for item in library_meta:
        title_lower = item["title"].lower()
        if title_lower in exclude_titles:
            continue
        vote_count = item.get("vote_count")
        # Must be obscure (or unknown — None means no TMDB data, skip)
        if vote_count is None or vote_count > DEEP_CUT_MAX_VOTES:
            continue
        # Must clear a minimum quality bar
        rating = float(item.get("tmdb_rating") or item.get("imdb_rating") or 0)
        if rating < DEEP_CUT_MIN_RATING:
            continue
        score = _score_candidate(
            item, genre_weights, kw_weights, lang_counts,
            disliked_genres, trending_titles, dominant_langs,
        )
        scored.append((score, item))

    if not scored:
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    best = scored[0][1]

    # Build a reason that highlights the top genre match
    top_genre = next(
        (g for g, _ in genre_weights.most_common(5) if g in {g2.lower() for g2 in best.get("genres", [])}),
        None,
    )
    genre_note = f"its {top_genre} elements" if top_genre else "its themes"
    reason = (
        f"A deep cut: only {best.get('vote_count', '?')} ratings on TMDB, "
        f"but {genre_note} closely match your taste — a hidden gem worth discovering."
    )

    return {
        "title": best["title"],
        "year": best.get("year"),
        "type": "movie" if best.get("media_type") == "movie" else "tv",
        "reason": reason,
        "poster_url": best.get("poster_url"),
        "deep_cut": True,
        "_imdb_rating": best.get("imdb_rating"),
        "_tmdb_rating": best.get("tmdb_rating"),
    }


# ---------------------------------------------------------------------------
# Prompt formatting
# ---------------------------------------------------------------------------

def _fmt_meta(entry: dict, weight: float = 1.0) -> str:
    recency = " [recent]" if weight >= 3.0 else (" [this year]" if weight >= 2.0 else "")
    parts = [f"- {entry['title']} ({entry['year'] or '?'}) [{entry['media_type']}]{recency}"]
    if entry.get("genres"):
        parts.append(f"genres: {', '.join(entry['genres'][:5])}")
    if entry.get("keywords"):
        parts.append(f"themes: {', '.join(entry['keywords'][:6])}")
    if entry.get("cast"):
        parts.append(f"cast: {', '.join(entry['cast'])}")
    if entry.get("director"):
        parts.append(f"director/creator: {entry['director']}")
    if entry.get("original_language") and entry["original_language"] != "en":
        parts.append(f"language: {entry['original_language']}")

    ratings = []
    if entry.get("imdb_rating"):
        ratings.append(f"IMDB {entry['imdb_rating']}/10")
    if entry.get("tmdb_rating"):
        ratings.append(f"TMDB {entry['tmdb_rating']:.1f}/10")
    if entry.get("metascore"):
        ratings.append(f"Metascore {int(entry['metascore'])}")
    if entry.get("rt_score"):
        ratings.append(f"RT {entry['rt_score']}")
    if ratings:
        parts.append(f"ratings: {' | '.join(ratings)}")

    if entry.get("tv_status"):
        status = entry["tv_status"]
        if entry.get("network"):
            status = f"{entry['network']} ({status})"
        parts.append(f"status: {status}")
    if entry.get("awards"):
        parts.append(f"awards: {entry['awards'][:80]}")
    return " | ".join(parts)


def _format_history(
    history_meta: list[dict],
    history_items: list[plex_client.WatchedItem],
) -> str:
    lines = []
    paired = sorted(
        zip(history_meta, history_items),
        key=lambda x: _recency_weight(x[1].viewed_at),
        reverse=True,
    )
    for meta, watched in paired:
        w = _recency_weight(watched.viewed_at)
        lines.append(_fmt_meta(meta, weight=w))
    return "\n".join(lines) if lines else "No history."


def _fmt_candidate(entry: dict) -> str:
    """
    Format a candidate for the AVAILABLE LIBRARY section.
    Title is on its own at the start so the LLM copies it exactly.
    """
    parts = [f'"{entry["title"]}" ({entry["year"] or "?"}) [{entry["media_type"]}]']
    if entry.get("genres"):
        parts.append(f"genres: {', '.join(entry['genres'][:5])}")
    if entry.get("keywords"):
        parts.append(f"themes: {', '.join(entry['keywords'][:5])}")
    ratings = []
    if entry.get("imdb_rating"):
        ratings.append(f"IMDB {entry['imdb_rating']}/10")
    if entry.get("tmdb_rating"):
        ratings.append(f"TMDB {entry['tmdb_rating']:.1f}/10")
    if ratings:
        parts.append(f"ratings: {' | '.join(ratings)}")
    if entry.get("original_language") and entry["original_language"] != "en":
        parts.append(f"language: {entry['original_language']}")
    return " | ".join(parts)


def _format_candidates(candidates: list[dict]) -> str:
    return "\n".join(_fmt_candidate(e) for e in candidates) if candidates else "None."


def _format_feedback(feedback: list[dict]) -> str:
    if not feedback:
        return "No feedback recorded yet."
    lines = []
    for fb in feedback[-30:]:
        emoji = {"like": "👍", "dislike": "👎", "watched": "✓"}.get(fb["rating"], "?")
        note = f' — "{fb["note"]}"' if fb.get("note") else ""
        lines.append(f"  {emoji} {fb['title']}{note}")
    return "\n".join(lines)


def _build_prompt(history_text: str, candidates_text: str, feedback_text: str,
                  n: int, top_genres: str = "") -> str:
    genre_line = f"\nUSER'S TOP GENRES (by watch frequency): {top_genres}\n" if top_genres else ""
    return f"""\
USER WATCH HISTORY (sorted most-recent first; [recent] = watched within 90 days):
{history_text}
{genre_line}
USER FEEDBACK ON PAST RECOMMENDATIONS:
{feedback_text}

AVAILABLE LIBRARY — select ONLY from these titles the user has not yet watched:
{candidates_text}

TASK:
Choose exactly {n} titles from AVAILABLE LIBRARY that best match this user's taste.
Prioritise genre alignment with the user's top genres above all else.
Weight: genre/tone alignment, thematic fit, cast/director overlap, then critical ratings.
Do NOT invent titles. Match movie/TV ratio to the user's viewing habits.

Return a JSON array of exactly {n} objects. Each object must be wrapped in curly braces {{}}.
Example format (DO NOT copy these titles — use only titles from AVAILABLE LIBRARY):
[
  {{"title": "Example Movie", "year": 2024, "type": "movie", "reason": "Because you watched X, ...", "poster_url": null}},
  {{"title": "Example Show", "year": 2023, "type": "tv", "reason": "Because you watched Y, ...", "poster_url": null}}
]

Rules:
- "title": exact match from AVAILABLE LIBRARY — no year, no [type] suffix
- "year": integer or null
- "type": "movie" or "tv"
- "reason": one sentence starting with "Because you watched [specific title from history],"
- "poster_url": always null

Raw JSON array only. No variable names, no prose, no markdown.\
"""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def get_recommendations(user_id: str, force: bool = False) -> list[dict]:
    if not force:
        cached = cache.cache_get(user_id)
        if cached is not None:
            return cached

    # 1. Watch history (per-user, with timestamps)
    history = plex_client.get_watch_history(user_id)
    watched_titles = {item.title.lower() for item in history}

    # 2. In-progress titles (exclude from candidates)
    in_progress = plex_client.get_in_progress_titles(user_id)

    # 3. Unwatched library candidates (excludes watched + in-progress)
    library_items = _get_unwatched_library(watched_titles, in_progress)
    if not library_items:
        return []

    # 4. Lightweight pre-score by Plex genres — avoids enriching entire library
    shortlist = _prescore_by_plex_genres(library_items, history)

    # 5. Trending titles for boost signal (fetch concurrently with enrichment)
    async def _get_trending_titles() -> set[str]:
        try:
            trending = await tmdb_client.get_trending("all", "week")
            return {m.title.lower() for m in trending}
        except Exception:
            return set()

    history_batch = [(i.title, i.year, i.media_type) for i in history]
    enrich_batch = [(d["title"], d["year"], d["media_type"]) for d in shortlist]
    (history_meta, library_meta, trending_titles) = await asyncio.gather(
        _enrich_all(history_batch) if history_batch else asyncio.sleep(0, result=[]),
        _enrich_all(enrich_batch),
        _get_trending_titles(),
    )

    # 6. Shown-rec suppression data
    shown_recs = cache.get_shown_recs(user_id)

    # 7. Pre-filter enriched candidates to focused pool
    feedback = cache.get_user_feedback(user_id)
    n = min(MAX_RECS, len(shortlist))
    # Reserve one slot for the deep cut; LLM fills n-1
    llm_n = max(1, n - 1)
    candidates = _prefilter(
        library_meta, history_meta, history,
        user_id, trending_titles, shown_recs, CANDIDATE_POOL_SIZE, llm_n,
    )

    # 8. Build prompt with recency-ordered history + top genres
    genre_weights, kw_weights, lang_counts = _build_fingerprint(history_meta, history)
    top_genres_str = ", ".join(g for g, _ in genre_weights.most_common(8))
    history_text = _format_history(history_meta, history)
    candidates_text = _format_candidates(candidates)
    feedback_text = _format_feedback(feedback)
    prompt = _build_prompt(history_text, candidates_text, feedback_text, llm_n,
                           top_genres=top_genres_str)

    # 9. Call LLM
    raw_recs: list | dict = await llm_client.generate_json(prompt, system=SYSTEM_PROMPT)
    if isinstance(raw_recs, dict):
        raw_recs = raw_recs.get("recommendations", list(raw_recs.values())[0] if raw_recs else [])

    # 10. Post-process LLM recs
    meta_by_title = {e["title"].lower(): e for e in library_meta}
    disliked = {fb["title"].lower() for fb in feedback if fb["rating"] == "dislike"}
    recs: list[dict] = []
    seen: set[str] = set()

    for rec in raw_recs:
        if not isinstance(rec, dict):
            continue
        title_lower = rec.get("title", "").lower()
        if title_lower in seen or title_lower in disliked:
            continue
        seen.add(title_lower)
        meta = meta_by_title.get(title_lower)
        if meta:
            if not rec.get("poster_url"):
                rec["poster_url"] = meta.get("poster_url")
            if not rec.get("year"):
                rec["year"] = meta.get("year")
            rec["_imdb_rating"] = meta.get("imdb_rating")
            rec["_tmdb_rating"] = meta.get("tmdb_rating")
        recs.append(rec)

    # 10b. Deep cut — pick one hidden gem the LLM wouldn't normally choose
    dominant_langs = {lang for lang, _ in lang_counts.most_common(3)} - {"en"}
    bad_genres = _disliked_genres(user_id, {e["title"].lower(): e for e in history_meta})
    deep_cut = _pick_deep_cut(
        library_meta, genre_weights, kw_weights, lang_counts,
        bad_genres, trending_titles, dominant_langs,
        exclude_titles=seen | {t for t, _ in shown_recs.items()},
    )
    if deep_cut:
        recs.append(deep_cut)

    cache.cache_set(user_id, recs)

    # 11. Mark shown (for future suppression)
    cache.mark_shown_recs(user_id, [r["title"] for r in recs])

    # 12. Sync to Plex (per-user isolation: collection for admin, playlist for others)
    try:
        users = plex_client.get_users()
        username = next((u["username"] for u in users if str(u["id"]) == str(user_id)), str(user_id))
        user_token = plex_client.get_user_token(user_id)
        plex_sync.sync_to_plex(user_id, username, recs, user_token=user_token)
    except Exception as exc:
        print(f"[plex_sync] Warning: could not sync for {user_id}: {exc}")

    return recs
