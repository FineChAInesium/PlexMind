import json
import logging
from unittest.mock import patch

import pytest

from app import scheduler


@pytest.fixture
def state_path(tmp_path):
    path = tmp_path / "scheduler_state.json"
    with patch.object(scheduler, "SCHEDULE_STATE_PATH", path):
        yield path


def test_valid_state_round_trips(state_path):
    state = {"monthly": {"day": 15, "hour": 4, "minute": 30}, "window_receipts": {"translate": "2026-10-27"}}
    scheduler._save_schedule_state(state)
    assert scheduler._load_schedule_state() == state


def test_missing_state_is_empty_and_quiet(state_path, caplog):
    with caplog.at_level(logging.WARNING, logger="plexmind.scheduler"):
        assert scheduler._load_schedule_state() == {}
    assert caplog.records == []


@pytest.mark.parametrize("content", [
    '{"monthly": {"day": 15, "hour"',
    '["not", "an", "object"]',
    '{"monthly": {"day": 42, "hour": 3, "minute": 0}}',
    '{"monthly": {"day": 15}}',
    '{"window_receipts": "2026-10-27"}',
])
def test_corrupt_state_is_kept_aside_and_reported_loudly(state_path, caplog, content):
    state_path.write_text(content)
    with caplog.at_level(logging.ERROR, logger="plexmind.scheduler"):
        assert scheduler._load_schedule_state() == {}
    kept = list(state_path.parent.glob("scheduler_state.json.corrupt-*"))
    assert len(kept) == 1
    assert kept[0].read_text() == content
    assert not state_path.exists()
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    message = record.getMessage()
    assert str(kept[0]) in message
    assert "monthly recommendation run reverts to the default (day 1, 03:00 UTC)" in message


def test_saving_after_corruption_writes_clean_state(state_path):
    state_path.write_text("{oops")
    state = scheduler._load_schedule_state()
    state["window_receipts"] = {"translate": "2026-10-27"}
    scheduler._save_schedule_state(state)
    assert json.loads(state_path.read_text()) == {"window_receipts": {"translate": "2026-10-27"}}
