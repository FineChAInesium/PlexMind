"""
Match library titles against the individual titles a user has watched.

A candidate is scored from the few watched titles it most resembles, not from
an average of everything, so someone who watches horror and reality TV gets
horror judged against their horror. Similarity uses rarity-weighted genres,
themes and people, plus TMDB's own "similar" and "recommended" lists. Each
watched title counts by how much the user cared: how recently, how much of
it, whether they rewatched it, and how they rated it.
"""
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field

TOP_MATCHES = 5
MATCH_DECAY = 0.7           # each further match counts this much of the one before
MIN_SIMILARITY = 0.12       # below this, a watched title says nothing about a candidate
GENRE_SHARE = 0.25
THEME_SHARE = 0.30
PEOPLE_SHARE = 0.10
LINK_SHARE = 0.35           # TMDB lists this title alongside the watched one
RECENCY_HALF_LIFE_DAYS = 180
RECENCY_FLOOR = 0.25
LIKED_WEIGHT = 1.5          # a thumbs-up
TRENDING_FACTOR = 1.05
LANGUAGE_FACTOR = 1.10
DISLIKE_PENALTY = 1.5       # per unit of similarity to something disliked
MAX_DISLIKE_CUT = 0.8
DISLIKED_RATING = 4         # Plex star rating, out of 10, at or below which a watch is a dislike
VARIETY_PENALTY = 0.6       # how much resembling an already chosen pick costs

_GENRE_ALIASES = {
    "sci-fi": "science fiction", "scifi": "science fiction", "sci fi": "science fiction",
    "reality-tv": "reality", "talk-show": "talk", "game-show": "game show",
    "film-noir": "film noir", "biography": "biographical", "kids": "family", "children": "family",
}


def normalise_title(title: str) -> str:
    """Strip region suffixes and trailing years, for comparing titles."""
    text = str(title).lower().strip()
    text = re.sub(r"\s*\([a-z]{2}\)$", "", text)
    text = re.sub(r"\s*\(\d{4}\)$", "", text)
    return text.strip()


def genre_tokens(genres) -> frozenset:
    """Comparable genres. TMDB's "Sci-Fi & Fantasy" counts as both, and the
    different spellings of TMDB, TVDB, OMDb and Plex agree."""
    tokens = set()
    for genre in genres or []:
        for part in re.split(r"\s*[&/]\s*", str(genre).lower().strip()):
            if part:
                tokens.add(_GENRE_ALIASES.get(part, part))
    return frozenset(tokens)


def engagement(item, now: float | None = None) -> float:
    """How much one watched title should count."""
    viewed_at = getattr(item, "viewed_at", None)
    if viewed_at:
        days = max(0.0, ((now or time.time()) - viewed_at) / 86400)
        recency = RECENCY_FLOOR + (1 - RECENCY_FLOOR) * 0.5 ** (days / RECENCY_HALF_LIFE_DAYS)
    else:
        recency = 0.5

    episodes = getattr(item, "episodes_watched", None)
    if episodes:
        total = getattr(item, "episode_total", None)
        commitment = max(min(episodes / total, 1.0) if total else 0.0, min(episodes, 10) / 10)
        depth = 0.4 + 1.1 * math.sqrt(commitment)
    else:
        depth = 1.0 + 0.3 * min(max((getattr(item, "plays", 1) or 1) - 1, 0), 2)

    rating = getattr(item, "user_rating", None)
    if rating is None:
        opinion = 1.0
    elif rating >= 8:
        opinion = 1.4
    elif rating >= 6:
        opinion = 1.1
    else:
        opinion = 0.7
    return recency * depth * opinion


@dataclass
class Profile:
    title: str
    year: int | None
    genres: frozenset = frozenset()
    themes: frozenset = frozenset()
    people: frozenset = frozenset()
    links: dict = field(default_factory=dict)   # normalised title -> years TMDB gave
    weight: float = 1.0

    @property
    def key(self) -> str:
        return normalise_title(self.title)

    def links_to(self, title_key: str, year: int | None) -> bool:
        years = self.links.get(title_key)
        if years is None:
            return False
        known = {y for y in years if y}
        return not year or not known or any(abs(y - year) <= 1 for y in known)


def profile(meta: dict, weight: float = 1.0) -> Profile:
    people = {str(p).lower() for p in (meta.get("cast") or [])[:5]}
    if meta.get("director"):
        people.add(str(meta["director"]).lower())
    links: dict = {}
    for title, year in meta.get("related") or []:
        links.setdefault(normalise_title(title), set()).add(year)
    return Profile(
        title=meta["title"], year=meta.get("year"),
        genres=genre_tokens(meta.get("genres")),
        themes=frozenset(str(k).lower() for k in meta.get("keywords") or []),
        people=frozenset(people), links=links, weight=weight,
    )


def build_profiles(history_meta, history, feedback=(), feedback_meta=None) -> tuple[list, list]:
    """(what the user liked, what they disliked) as weighted profiles."""
    feedback_meta = feedback_meta or {}
    liked = {fb["title"].lower() for fb in feedback if fb.get("rating") == "like"}
    disliked_titles = {fb["title"].lower() for fb in feedback if fb.get("rating") == "dislike"}

    watched: list[Profile] = []
    disliked: list[Profile] = []
    seen = set()
    for meta, item in zip(history_meta, history):
        title = meta["title"].lower()
        seen.add(title)
        rating = getattr(item, "user_rating", None)
        if title in disliked_titles or (rating is not None and rating <= DISLIKED_RATING):
            disliked.append(profile(meta))
            continue
        watched.append(profile(meta, engagement(item) * (LIKED_WEIGHT if title in liked else 1.0)))
    for title, meta in feedback_meta.items():
        if title in seen:
            continue
        if title in liked:
            watched.append(profile(meta, LIKED_WEIGHT))
        elif title in disliked_titles:
            disliked.append(profile(meta))
    return watched, disliked


def related_items(library_items: list[dict], watched: list[Profile], limit: int) -> list[dict]:
    """Library items that TMDB lists alongside watched titles, most supported first."""
    if not any(p.links for p in watched):
        return []
    scored = []
    for item in library_items:
        key, year = normalise_title(item["title"]), item.get("year")
        support = sum(p.weight for p in watched if p.links_to(key, year))
        if support > 0:
            scored.append((support, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:limit]]


class Matcher:
    """Scores candidates against watched titles, over one set of rarity weights."""

    def __init__(self, watched: list[Profile], disliked: list[Profile], candidates: list[dict]):
        self.watched = watched
        self.disliked = disliked
        self.profiles = {id(c): profile(c) for c in candidates}
        self.total = len(watched) + len(disliked) + len(candidates)
        self.frequency: Counter = Counter()
        for p in [*watched, *disliked, *self.profiles.values()]:
            self.frequency.update(p.genres)
            self.frequency.update(p.themes)
            self.frequency.update(p.people)

    def _rarity(self, feature: str) -> float:
        return math.log((self.total + 1) / (self.frequency[feature] + 1)) + 1

    def _overlap(self, a: frozenset, b: frozenset) -> float:
        shared = a & b
        if not shared:
            return 0.0
        top = sum(self._rarity(f) ** 2 for f in shared)
        size_a = sum(self._rarity(f) ** 2 for f in a)
        size_b = sum(self._rarity(f) ** 2 for f in b)
        return top / math.sqrt(size_a * size_b)

    def similarity(self, a: Profile, b: Profile) -> float:
        linked = a.links_to(b.key, b.year) or b.links_to(a.key, a.year)
        return (GENRE_SHARE * self._overlap(a.genres, b.genres)
                + THEME_SHARE * self._overlap(a.themes, b.themes)
                + PEOPLE_SHARE * self._overlap(a.people, b.people)
                + (LINK_SHARE if linked else 0.0))

    def _shared(self, a: Profile, b: Profile) -> list[str]:
        """What two titles have in common, most distinctive first."""
        for shared in (a.themes & b.themes, a.genres & b.genres):
            if shared:
                return sorted(shared, key=self._rarity, reverse=True)[:2]
        return []

    def rank(self, candidates: list[dict], trending=frozenset(), languages=frozenset()) -> list[dict]:
        """Best match first. Sets _score, _matches and _shared on each candidate."""
        for candidate in candidates:
            own = self.profiles[id(candidate)]
            similar = ((self.similarity(own, w), w) for w in self.watched)
            matches = sorted(((s * w.weight, w) for s, w in similar if s >= MIN_SIMILARITY),
                             key=lambda pair: pair[0], reverse=True)[:TOP_MATCHES]
            score = sum(value * MATCH_DECAY ** i for i, (value, _) in enumerate(matches))

            imdb = candidate.get("imdb_rating")
            if imdb:
                score *= 0.85 + 0.3 * min(float(imdb) / 10, 1.0)
            if candidate.get("original_language") in languages:
                score *= LANGUAGE_FACTOR
            if candidate["title"].lower() in trending:
                score *= TRENDING_FACTOR
            if self.disliked:
                closest = max(self.similarity(own, d) for d in self.disliked)
                score *= 1 - min(MAX_DISLIKE_CUT, closest * DISLIKE_PENALTY)

            candidate["_score"] = score
            candidate["_matches"] = [w.title for _, w in matches]
            candidate["_shared"] = self._shared(own, matches[0][1]) if matches else []
        return sorted(candidates, key=lambda c: c["_score"], reverse=True)

    def varied(self, ranked: list[dict], k: int) -> list[dict]:
        """The best K of RANKED, trading a little score for variety between picks."""
        best = ranked[0]["_score"] if ranked else 0
        if not best:
            return ranked[:k]
        remaining, chosen = list(ranked), []
        while remaining and len(chosen) < k:
            def value(candidate):
                own = self.profiles[id(candidate)]
                closest = max((self.similarity(own, self.profiles[id(c)]) for c in chosen), default=0.0)
                return candidate["_score"] / best - VARIETY_PENALTY * closest
            pick = max(remaining, key=value)
            remaining.remove(pick)
            chosen.append(pick)
        return chosen


def explain(meta: dict) -> str:
    """The reason for a pick, from what it actually matched."""
    names = meta["_matches"][:3]
    listed = f"{', '.join(names[:-1])} and {names[-1]}" if len(names) > 1 else names[0]
    shared = meta.get("_shared") or []
    if shared:
        return f"Because you watched {listed}, which share {' and '.join(shared)} with this."
    return f"Because you watched {listed}."


def titles_named(text: str, titles) -> set[str]:
    """Which of TITLES appear in TEXT as whole words. Case-sensitive, so the
    film "It" is not found in every sentence."""
    return {t for t in titles if t and re.search(rf"(?<!\w){re.escape(t)}(?!\w)", text)}
