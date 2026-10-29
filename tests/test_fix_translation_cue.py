import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("translation_cue_fix_under_test", ROOT / "scripts" / "translation_cue.py")
cue = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cue)

# What hand-made containers without WATERMARK_TEXT wrote (the old
# ${WATERMARK_TEXT:-...} default), and the same after watermark.sh's old
# printf %b turned "\a" into BEL.
LEGACY = "{\\an8<i>Brought to you by PlexMind</i>}"
LEGACY_BEL = "{\an8<i>Brought to you by PlexMind</i>}"
LEGACY_BEL_CLOSED = "{\an8}<i>Brought to you by PlexMind</i>"


@pytest.fixture
def default_env():
    """WATERMARK_SEARCH=PlexMind and WATERMARK_TEXT unset, as on the server."""
    with patch.dict(os.environ, {"WATERMARK_SEARCH": "PlexMind"}):
        os.environ.pop("WATERMARK_TEXT", None)
        yield


@pytest.mark.parametrize("source", [
    LEGACY,
    LEGACY_BEL,
    LEGACY_BEL_CLOSED,
    "{\\an8}<i>Brought to you by PlexMind</i>",
    "X<i>Brought to you by PlexMind</i>}",
    "Brought to you by Plex{\\b1}Mind",
])
def test_legacy_watermarks_are_passed_through(default_env, source):
    assert cue.is_passthrough(source)


@pytest.mark.parametrize("source", ["", "<i></i>", "{}", "{\\an8}", "Where are you going with this?"])
def test_non_watermark_cues_are_translated(default_env, source):
    assert not cue.is_passthrough(source)


def test_empty_watermark_search_falls_back_like_lib_sh():
    # lib.sh uses ${WATERMARK_SEARCH:-PlexMind}, so an empty template field
    # still means "PlexMind".
    with patch.dict(os.environ, {"WATERMARK_SEARCH": "", "WATERMARK_TEXT": ""}):
        assert cue.is_passthrough(LEGACY)
        assert not cue.is_passthrough("<i></i>")
        assert not cue.is_passthrough("Hello there, how are you?")


def test_classify_and_passthrough_cli_on_legacy_watermark(default_env, tmp_path, capsys):
    paths = []
    for index, (number, dialogue) in enumerate([("0", LEGACY), ("1", "Are you sure about this?"), ("2", LEGACY_BEL)]):
        path = tmp_path / f"chunk_{index:05d}.txt"
        path.write_bytes(f"{number}\n00:00:0{index},000 --> 00:00:0{index},500\n{dialogue}\n\n".encode("utf-8"))
        paths.append(str(path))
    assert cue.main(["x", "classify", *paths]) == 0
    assert capsys.readouterr().out.split() == ["passthrough", "translate", "passthrough"]
    assert cue.main(["x", "passthrough", LEGACY]) == 0
    assert cue.main(["x", "passthrough", "<i></i>"]) == 1
