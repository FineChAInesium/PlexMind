import asyncio
import json
import time
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import cache, llm_client, plex_client, recommendation_jobs, recommendation_worker, recommender


def obj(title):
    return {"title": title, "year": None, "type": "movie", "reason": "r", "poster_url": None}


def test_cut_off_reply_keeps_every_complete_object():
    text = json.dumps([obj("A"), obj("B")])[:-1] + ', {"title": "C", "year": null, "type": "mo'
    assert [o["title"] for o in json.loads(llm_client._extract_json(text))] == ["A", "B"]


def test_shown_titles_stop_being_suppressed_after_the_window(tmp_path):
    old = time.time() - (cache.SUPPRESSION_DAYS + 1) * 86400
    with patch.object(cache, "SHOWN_RECS_FILE", tmp_path / "shown.json"):
        cache._save_shown({"u": {"old": old, "new": time.time()}})
        assert set(cache.get_shown_recs("u")) == {"new"}


def candidate(title, media_type="movie"):
    return {"title": title, "media_type": media_type, "genres": [], "keywords": []}


def prefilter(candidates, shown=None, feedback=(), n=1, history_types=("movie",)):
    history_meta = [candidate(f"H{i}", t) for i, t in enumerate(history_types)]
    history = [SimpleNamespace(viewed_at=None) for _ in history_types]
    with patch.object(recommender.cache, "get_user_feedback", return_value=list(feedback)):
        return recommender._prefilter(candidates, history_meta, history, "u", set(), shown or {}, 40, n)


def test_watched_and_disliked_feedback_is_never_a_candidate():
    feedback = [{"title": "Seen Elsewhere", "rating": "watched"}, {"title": "Nope", "rating": "dislike"}]
    result = prefilter([candidate("Seen Elsewhere"), candidate("Nope"), candidate("Fresh")], feedback=feedback)
    assert [c["title"] for c in result] == ["Fresh"]


def test_short_pool_brings_back_the_longest_ago_shown_titles_only():
    shown = {"recent": 300.0, "oldest": 100.0, "middle": 200.0}
    result = prefilter([candidate("Recent"), candidate("Oldest"), candidate("Middle")], shown=shown, n=2)
    assert {c["title"] for c in result} == {"Oldest", "Middle"}


def test_prompt_keeps_tv_for_a_movie_heavy_user():
    pool = [candidate(f"M{i}") for i in range(40)] + [candidate(f"S{i}", "show") for i in range(10)]
    result = prefilter(pool, n=9, history_types=("movie",) * 7 + ("show",) * 3)
    shown_in_prompt = result[:recommender.MAX_CANDIDATE_PROMPT_ITEMS]
    assert any(c["media_type"] == "show" for c in shown_in_prompt)


def test_unusable_llm_picks_are_topped_up_and_suffixed_titles_match():
    metas = [{"title": t, "year": 1995, "media_type": "movie", "rating_key": t} for t in ("Heat", "Ronin", "Thief", "Collateral")]
    shortlist = [{"title": m["title"], "year": 1995, "media_type": "movie", "rating_key": m["title"]} for m in metas]
    set_cache = MagicMock()
    with patch.object(recommender, "MAX_RECS", 4), \
            patch.object(cache, "cache_get", return_value=None), \
            patch.object(plex_client, "get_watch_history", return_value=[]), \
            patch.object(plex_client, "get_in_progress_titles", return_value=set()), \
            patch.object(recommender, "_get_unwatched_library", return_value=shortlist), \
            patch.object(recommender, "_prescore_by_plex_genres", return_value=shortlist), \
            patch.object(recommender.tmdb_client, "get_trending", AsyncMock(return_value=[])), \
            patch.object(recommender, "_enrich_all", AsyncMock(return_value=metas)), \
            patch.object(cache, "get_shown_recs", return_value={}), \
            patch.object(cache, "get_user_feedback", return_value=[]), \
            patch.object(recommender, "_prefilter", return_value=metas), \
            patch.object(recommender, "_build_fingerprint", return_value=(Counter(),) * 5), \
            patch.object(recommender, "_format_history", return_value=""), \
            patch.object(recommender.llm_client, "generate_json",
                         AsyncMock(return_value=[{"title": "Ronin (1998)", "type": "movie"}, {"title": "Invented"}])), \
            patch.object(recommender, "_disliked_genres", return_value=set()), \
            patch.object(recommender, "_pick_deep_cut", return_value=None), \
            patch.object(cache, "cache_set", set_cache), \
            patch.object(cache, "mark_shown_recs", MagicMock()):
        recs = asyncio.run(recommender.get_recommendations("admin", force=True))
    # 4 slots, one reserved for the deep cut: the LLM's one valid pick plus two top-ups.
    assert [r["title"] for r in recs] == ["Ronin", "Heat", "Thief"]
    set_cache.assert_called_once()


def test_a_run_where_every_tried_user_failed_is_reported_failed():
    finish = MagicMock()
    summary = {"ok": 0, "skipped": 1, "errors": 2, "total": 3}
    with patch.object(recommendation_worker.scheduler, "run_all_users", AsyncMock(return_value={"summary": summary})), \
            patch.object(recommendation_jobs, "finish", finish):
        asyncio.run(recommendation_worker.run_job("j1", {"triggered_by": "monthly_cron"}))
    assert finish.call_args.args[:2] == ("j1", "failed")
    finish.reset_mock()
    with patch.object(recommendation_worker.scheduler, "run_all_users",
                      AsyncMock(return_value={"summary": {**summary, "ok": 1}})), \
            patch.object(recommendation_jobs, "finish", finish):
        asyncio.run(recommendation_worker.run_job("j2", {}))
    assert finish.call_args.args[:2] == ("j2", "completed")


def test_unknown_user_does_not_get_the_admin_history():
    account = SimpleNamespace(users=lambda: [])
    server = SimpleNamespace(myPlexAccount=lambda: account, library=SimpleNamespace(history=MagicMock()))
    with patch.object(plex_client, "_get_server", return_value=server):
        with pytest.raises(RuntimeError, match="Cannot obtain token"):
            plex_client.get_watch_history("12345")
    server.library.history.assert_not_called()
