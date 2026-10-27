import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("translation_cue_under_test", ROOT / "scripts" / "translation_cue.py")
cue = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cue)


# Real cues that failed whole files on the server (2026-10-26 diagnostics).
@pytest.mark.parametrize("source, output", [
    ("Detective Watts, Louise Cherry, Toronto Gazette.", "¡Detective Watts, Louise Cherry, Toronto Gazette!"),
    ("No, no, no, no, no, no.", "No, no, no, no, no, no."),
    ("Louise Cherry, Toronto Gazette.", "Louise Cherry, Toronto Gazette."),
    ("Frida Kahlo.", "Frida Kahlo."),
])
def test_names_and_shared_words_are_not_untranslated(source, output):
    assert cue.validate(source, output, "es-MX") is None


def test_real_english_echo_is_still_rejected():
    assert cue.validate("Sir, are you sure about this?", "Sir, are you sure about this?", "es-MX") == "substantial_dialogue_unchanged"
    assert cue.validate("Sir, are you sure about this?", "Sir, are you sure about this?", "zh") == "missing_target_script"


def test_proper_translations_pass():
    assert cue.validate("Sir, are you sure about this?", "Señor, ¿está seguro de esto?", "es-MX") is None
    assert cue.validate("Sir, are you sure about this?", "長官，你確定嗎？", "zh") is None


def test_zh_names_only_cue_may_stay_in_latin_script():
    assert cue.validate("Detective Watts, Louise Cherry.", "Detective Watts, Louise Cherry.", "zh") is None


@pytest.mark.parametrize("output, reason", [
    ("   ", "empty_output"),
    ("00:00:01,000 --> 00:00:02,000", "unexpected_formatting"),
    ("<think>hmm</think>Hola", "unexpected_formatting"),
    ("2\nHola", "cue_number_in_output"),
])
def test_envelope_problems_are_rejected(output, reason):
    assert cue.validate("Where are you going with this?", output, "es-MX") == reason


def test_watermark_cue_is_passed_through():
    env = {"WATERMARK_SEARCH": "ChAInesium", "WATERMARK_TEXT": "{\\an8}<i>Brought to you by Fine ChAInesium</i>"}
    with patch.dict(os.environ, env):
        assert cue.is_passthrough("{n8}<i>Brought to you by Fine ChAInesium</i>")
        assert cue.is_passthrough("<i>Brought to you by Fine ChAInesium</i>")
        assert not cue.is_passthrough("Where are you going with this?")
        assert not cue.is_passthrough("<i></i>")


def test_watermark_exact_text_matches_even_if_search_differs():
    env = {"WATERMARK_SEARCH": "PlexMind", "WATERMARK_TEXT": "{\\an8}<i>Brought to you by Fine ChAInesium</i>"}
    with patch.dict(os.environ, env):
        assert cue.is_passthrough("{n8}<i>Brought to you by Fine ChAInesium</i>")


def test_cli_validate_and_passthrough(tmp_path):
    out = tmp_path / "out.txt"
    out.write_text("No, no.", encoding="utf-8")
    assert cue.main(["x", "validate", "No, no.", str(out), "es-MX"]) == 0
    out.write_text("Are you sure about this?", encoding="utf-8")
    assert cue.main(["x", "validate", "Are you sure about this?", str(out), "es-MX"]) == 1
    with patch.dict(os.environ, {"WATERMARK_SEARCH": "PlexMind"}):
        assert cue.main(["x", "passthrough", "<i>Brought to you by PlexMind</i>"]) == 0
        assert cue.main(["x", "passthrough", "Hello"]) == 1
