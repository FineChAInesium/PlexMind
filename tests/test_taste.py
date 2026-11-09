import asyncio
import time
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app import cache, plex_client, recommender, taste, tmdb_client

NOW = 1_800_000_000.0


def meta(title, genres=(), themes=(), people=(), related=(), year=2000, media_type="movie", **extra):
    return {"title": title, "year": year, "media_type": media_type, "genres": list(genres),
            "keywords": list(themes), "cast": list(people), "related": list(related), **extra}


def watched(**fields):
    return SimpleNamespace(**{"viewed_at": NOW, **fields})


def rank(candidates, watched_metas, disliked_metas=()):
    profiles = [taste.profile(m) for m in watched_metas]
    matcher = taste.Matcher(profiles, [taste.profile(m) for m in disliked_metas], candidates)
    return matcher.rank(candidates)


HORROR = meta("Hereditary", ["Horror"], ["grief", "cult", "haunting"], ["Toni Collette"])
REALITY = meta("Love Island", ["Reality"], ["dating", "villa", "competition"], ["Maya Jama"])


def test_genre_spellings_from_different_sources_agree():
    assert taste.genre_tokens(["Sci-Fi & Fantasy"]) == {"science fiction", "fantasy"}
    assert taste.genre_tokens(["sci-fi", "Reality-TV"]) == {"science fiction", "reality"}


def test_a_candidate_is_judged_against_the_titles_it_resembles_not_an_average():
    horror_pick = meta("The Witch", ["Horror"], ["cult", "haunting"])
    reality_pick = meta("Too Hot to Handle", ["Reality"], ["dating", "competition"])
    blend = meta("Bland", ["Drama"], ["family"])
    result = rank([blend, horror_pick, reality_pick], [HORROR, REALITY])
    assert result[-1]["title"] == "Bland"
    assert {r["title"] for r in result[:2]} == {"The Witch", "Too Hot to Handle"}
    assert horror_pick["_matches"] == ["Hereditary"]
    assert reality_pick["_matches"] == ["Love Island"]


def test_several_supporting_titles_beat_one():
    a = meta("A", ["Thriller"], ["heist", "con"])
    b = meta("B", ["Thriller"], ["heist", "crew"])
    c = meta("C", ["Western"], ["duel", "frontier"])
    strong = meta("Many", ["Thriller"], ["heist", "con", "crew"])
    single = meta("One", ["Western"], ["duel", "frontier"])
    result = rank([single, strong], [a, b, c])
    assert result[0]["title"] == "Many"
    assert len(result[0]["_matches"]) == 2


def test_tmdb_link_counts_in_either_direction_and_respects_the_year():
    linked = meta("The Thing", year=1982)
    wrong_film = meta("The Thing", year=2011)
    source = meta("Alien", related=[("The Thing", 1982)])
    rank([linked, wrong_film], [source])
    assert linked["_matches"] == ["Alien"]
    assert wrong_film["_matches"] == []
    reverse = meta("Candidate", related=[("Alien", 2000)])
    rank([reverse], [meta("Alien")])
    assert reverse["_matches"] == ["Alien"]


def test_something_close_to_a_dislike_drops():
    fine = meta("Fine", ["Horror"], ["cult"])
    close = meta("Close", ["Horror"], ["cult", "clown"])
    disliked = meta("Hated", ["Comedy"], ["clown", "cult"])
    result = rank([close, fine], [HORROR], [disliked])
    assert result[0]["title"] == "Fine"


def test_a_binged_show_outweighs_a_sampled_one():
    binged = watched(episodes_watched=40, episode_total=40)
    sampled = watched(episodes_watched=1, episode_total=40)
    assert taste.engagement(binged, NOW) > 1.5 * taste.engagement(sampled, NOW)


def test_rewatches_ratings_and_recency_all_count():
    once = taste.engagement(watched(), NOW)
    assert taste.engagement(watched(plays=3), NOW) > once
    assert taste.engagement(watched(user_rating=10), NOW) > once
    assert taste.engagement(watched(viewed_at=NOW - 720 * 86400), NOW) < once


def test_a_low_star_rating_or_thumbs_down_counts_as_a_dislike_and_a_thumbs_up_counts_extra():
    metas = [meta("Rated Low"), meta("Thumbed Down"), meta("Liked")]
    items = [watched(user_rating=2), watched(), watched()]
    feedback = [{"title": "Thumbed Down", "rating": "dislike"}, {"title": "Liked", "rating": "like"},
                {"title": "Unwatched Like", "rating": "like"}]
    good, bad = taste.build_profiles(metas, items, feedback, {"unwatched like": meta("Unwatched Like")})
    assert {p.title for p in bad} == {"Rated Low", "Thumbed Down"}
    assert {p.title for p in good} == {"Liked", "Unwatched Like"}


def test_related_items_are_found_and_ordered_by_support():
    library = [{"title": "Once", "year": 2000}, {"title": "Twice", "year": 2000}, {"title": "Never", "year": 2000}]
    profiles = [taste.profile(meta("A", related=[("Once", 2000), ("Twice", 2000)])),
                taste.profile(meta("B", related=[("Twice", 2000)]))]
    assert [i["title"] for i in taste.related_items(library, profiles, 10)] == ["Twice", "Once"]


def test_shortlist_keeps_room_for_genre_matches_and_stays_within_size():
    related = [{"title": f"R{i}", "rating_key": f"r{i}"} for i in range(10)]
    by_genre = [{"title": f"G{i}", "rating_key": f"g{i}"} for i in range(10)]
    result = recommender._merge_shortlist(related, by_genre, 6)
    assert [i["title"] for i in result] == ["R0", "R1", "R2", "R3", "G0", "G1"]


def test_genre_prescore_does_not_reward_extra_tags():
    history = [SimpleNamespace(viewed_at=NOW, genres=["Horror"])]
    focused = {"title": "Focused", "plex_genres": ["Horror"]}
    padded = {"title": "Padded", "plex_genres": ["Horror", "Comedy", "Drama", "Romance", "War"]}
    others = [{"title": f"O{i}", "plex_genres": ["Drama"]} for i in range(5)]
    assert recommender._prescore_by_plex_genres([padded, focused, *others], history, 1)[0]["title"] == "Focused"


def test_varied_pool_gives_a_second_taste_a_place():
    clones = [meta(f"Clone {i}", ["Horror"], ["cult", "haunting"]) for i in range(4)]
    other = meta("Other", ["Reality"], ["dating", "villa"])
    profiles = [taste.profile(HORROR), taste.profile(REALITY)]
    candidates = [*clones, other]
    matcher = taste.Matcher(profiles, [], candidates)
    assert "Other" in [c["title"] for c in matcher.varied(matcher.rank(candidates), 2)]


def test_explanations_come_from_the_real_matches():
    item = {"_matches": ["Dark", "Severance", "Lost", "Fringe"], "_shared": ["time travel"]}
    assert taste.explain(item) == "Because you watched Dark, Severance and Lost, which share time travel with this."
    assert taste.explain({"_matches": ["Dark"], "_shared": []}) == "Because you watched Dark."


def test_title_names_are_whole_and_case_sensitive():
    assert taste.titles_named("Because you watched Heat, it fits.", {"Heat", "It", "Heathers"}) == {"Heat"}


def _run(candidates, reply, history=()):
    """get_recommendations with everything external stubbed; returns the recs."""
    metas = list(candidates)
    shortlist = [{"title": m["title"], "year": m["year"], "media_type": "movie", "rating_key": m["title"]} for m in metas]
    with patch.object(recommender, "MAX_RECS", len(metas) + 1), \
            patch.object(cache, "cache_get", return_value=None), \
            patch.object(plex_client, "get_watch_history", return_value=list(history)), \
            patch.object(plex_client, "get_in_progress_titles", return_value=set()), \
            patch.object(plex_client, "get_watched_rating_keys", return_value=set()), \
            patch.object(recommender, "_get_unwatched_library", return_value=shortlist), \
            patch.object(recommender, "_prescore_by_plex_genres", return_value=shortlist), \
            patch.object(recommender, "_merge_shortlist", return_value=shortlist), \
            patch.object(recommender.taste, "build_profiles", return_value=([], [])), \
            patch.object(recommender.tmdb_client, "get_trending", AsyncMock(return_value=[])), \
            patch.object(recommender, "_enrich_all", AsyncMock(return_value=metas)), \
            patch.object(cache, "get_shown_recs", return_value={}), \
            patch.object(cache, "get_user_feedback", return_value=[]), \
            patch.object(recommender, "_prefilter", return_value=metas), \
            patch.object(recommender, "_build_fingerprint", return_value=(Counter(), Counter())), \
            patch.object(recommender, "_format_history", return_value=""), \
            patch.object(recommender.llm_client, "generate_json", AsyncMock(return_value=reply)), \
            patch.object(recommender, "_pick_deep_cut", return_value=None), \
            patch.object(cache, "cache_set", MagicMock()), \
            patch.object(cache, "mark_shown_recs", MagicMock()):
        return asyncio.run(recommender.get_recommendations("admin", force=True))


HISTORY = [plex_client.WatchedItem(title=t, year=2000, media_type="movie") for t in ("Dark", "Heat")]


def picked(title, matches):
    return meta(title, _matches=matches, _shared=[], _score=1.0)


def reply(title, reason):
    return {"title": title, "year": 2000, "type": "movie", "reason": reason}


def test_a_reason_naming_a_real_match_is_kept():
    recs = _run([picked("A", ["Dark"])], [reply("A", "Because you watched Dark, this fits.")], HISTORY)
    assert recs[0]["reason"] == "Because you watched Dark, this fits."


def test_a_reason_naming_some_other_watched_title_is_rewritten():
    recs = _run([picked("A", ["Dark"])], [reply("A", "Because you watched Heat, this fits.")], HISTORY)
    assert recs[0]["reason"] == "Because you watched Dark."


def test_a_reason_naming_nothing_is_rewritten():
    recs = _run([picked("A", ["Dark"])], [reply("A", "You will love it.")], HISTORY)
    assert recs[0]["reason"] == "Because you watched Dark."


def test_picks_sharing_one_match_are_capped_unless_the_list_would_run_short():
    same = [picked(f"S{i}", ["Dark"]) for i in range(5)]
    other = picked("O", ["Heat"])
    answer = [reply(f"S{i}", "Because you watched Dark, yes.") for i in range(5)] + [reply("O", "x")]
    titles = [r["title"] for r in _run([*same, other], answer, HISTORY)]
    assert titles[:4] == ["S0", "S1", "S2", "O"]
    assert titles[4] == "S3"  # the list would otherwise run short


def test_deep_cut_needs_a_real_match():
    unmatched = meta("Obscure", vote_count=100, tmdb_rating=7.5, _matches=[], _score=9.0)
    matched = meta("Gem", vote_count=100, tmdb_rating=7.5, _matches=["Dark"], _score=1.0)
    assert recommender._pick_deep_cut([unmatched, matched], set())["title"] == "Gem"
    assert recommender._pick_deep_cut([unmatched], set()) is None


def test_tmdb_related_merges_both_lists_without_repeats():
    detail = {"recommendations": {"results": [{"title": "A", "release_date": "1999-01-01"}]},
              "similar": {"results": [{"title": "A", "release_date": "1999-05-05"}, {"title": "B", "release_date": ""}]}}
    assert tmdb_client._related(detail, "title", "release_date") == [("A", 1999), ("B", None)]


def test_history_counts_episodes_plays_and_the_latest_watch():
    show = SimpleNamespace(genres=[], year=2010, key="/s", audienceRating=None, leafCount=20, userRating=9.0)

    def episode(key, when):
        return SimpleNamespace(type="episode", title=f"E{key}", grandparentTitle="Dark", grandparentKey="/s",
                               ratingKey=key, key=f"/e{key}", viewedAt=SimpleNamespace(timestamp=lambda: when), show=lambda: show)

    def movie(when):
        return SimpleNamespace(type="movie", title="Heat", key="/m", year=1995, genres=[], audienceRating=None,
                               viewedAt=SimpleNamespace(timestamp=lambda: when))

    feed = [episode(1, 100.0), episode(2, 300.0), episode(1, 200.0), movie(50.0), movie(60.0)]
    item = SimpleNamespace(duration=100, viewOffset=0, viewCount=2, userRating=8.0)
    server = SimpleNamespace(library=SimpleNamespace(history=lambda maxresults: feed), fetchItem=lambda key: item)
    with patch.object(plex_client, "_get_server", return_value=server):
        result = {i.title: i for i in plex_client.get_watch_history("admin")}
    assert (result["Dark"].episodes_watched, result["Dark"].episode_total, result["Dark"].user_rating) == (2, 20, 9.0)
    assert result["Dark"].viewed_at == 300.0
    assert (result["Heat"].plays, result["Heat"].user_rating) == (2, 8.0)


def test_engagement_handles_items_without_new_fields():
    assert taste.engagement(SimpleNamespace(viewed_at=None)) == 0.5
    assert taste.engagement(SimpleNamespace(viewed_at=time.time())) > 0.9
