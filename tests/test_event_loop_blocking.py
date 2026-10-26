import asyncio
import threading
import time
from unittest.mock import patch

from app import cache, plex_client, recommender


def test_recommendation_plex_io_runs_off_the_event_loop():
    loop_thread = {}
    calls = {}

    def blocking(name, result):
        def call(*_args, **_kwargs):
            calls[name] = threading.current_thread()
            time.sleep(0.2)  # stands in for a slow synchronous plexapi call
            return result
        return call

    async def scenario():
        loop_thread["thread"] = threading.current_thread()
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        tick_task = asyncio.create_task(ticker())
        result = await recommender.get_recommendations("admin")
        tick_task.cancel()
        return result, ticks

    with patch.object(cache, "cache_get", side_effect=blocking("cache_get", None)), \
            patch.object(plex_client, "get_watch_history", side_effect=blocking("history", [])), \
            patch.object(plex_client, "get_in_progress_titles", side_effect=blocking("in_progress", set())), \
            patch.object(recommender, "_get_unwatched_library", side_effect=blocking("library", [])):
        result, ticks = asyncio.run(scenario())

    assert result == []
    assert set(calls) == {"cache_get", "history", "in_progress", "library"}
    assert all(thread is not loop_thread["thread"] for thread in calls.values())
    # 4 x 200ms of blocking work; the loop must keep serving other tasks meanwhile.
    assert ticks >= 20
