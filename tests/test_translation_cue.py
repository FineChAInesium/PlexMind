import importlib.util
import json
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


def write_chunks(tmp_path, *cues):
    """Write single-cue chunk files the way translate.sh's splitter does."""
    paths = []
    for index, (number, dialogue) in enumerate(cues):
        path = tmp_path / f"chunk_{index:05d}.txt"
        path.write_bytes(f"{number}\n00:00:0{index},000 --> 00:00:0{index},500\n{dialogue}\n\n".encode("utf-8"))
        paths.append(str(path))
    return paths


def test_read_cue_matches_the_shell_split(tmp_path):
    [path] = write_chunks(tmp_path, ("7", "<i>Two</i>\n- lines"))
    assert cue.read_cue(path) == ("7", "00:00:00,000 --> 00:00:00,500", "<i>Two</i>\n- lines")


def test_classify_and_batch_sources_cli(tmp_path, capsys):
    paths = write_chunks(tmp_path, ("1", "Où est-il?\nHere."), ("2", "{\\an8}<i>Brought to you by PlexMind</i>"))
    with patch.dict(os.environ, {"WATERMARK_SEARCH": "PlexMind"}):
        assert cue.main(["x", "classify", *paths]) == 0
    assert capsys.readouterr().out.split() == ["translate", "passthrough"]
    assert cue.main(["x", "batch-sources", paths[0]]) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed) == ["Où est-il?\nHere."]
    assert "Où" in printed and "\n" not in printed.rstrip("\n")


@pytest.mark.parametrize("content, reason", [
    ("Hola", "invalid_json"),
    ('{"translations": ["Hola", "Adiós"]}', "not_a_string_array"),
    ('["Hola", 2]', "not_a_string_array"),
    ('["Hola"]', "item_count_1_of_2"),
    ('["Hola", "Adiós", "Extra"]', "item_count_3_of_2"),
])
def test_unusable_batch_replies(content, reason):
    assert cue.validate_batch(["Where are you going?", "Are you sure about this?"], content, "es-MX") == (None, reason)


def test_batch_items_are_validated_one_by_one():
    sources = ["Where are you going with this?", "Are you sure about this?", "Frida Kahlo."]
    content = '```json\n["¿A dónde vas con esto?", "Are you sure about this?", "Frida Kahlo."]\n```'
    items, reasons = cue.validate_batch(sources, content, "es-MX")
    assert items == ["¿A dónde vas con esto?", "Are you sure about this?", "Frida Kahlo."]
    assert reasons == [None, "substantial_dialogue_unchanged", None]


def test_batch_items_keep_line_breaks_but_not_blank_lines():
    items, reasons = cue.validate_batch(["Hi.\nBye."], '["\\nHola.\\n\\n<i>Adiós.</i>\\n"]', "es-MX")
    assert items == ["Hola.\n<i>Adiós.</i>"]
    assert reasons == [None]


def test_batch_validate_cli_writes_only_accepted_cues(tmp_path, capsys):
    paths = write_chunks(tmp_path, ("4", "Where are you going with this?"), ("5", "Are you sure about this?\nReally?"))
    out_dir = tmp_path / "batch"
    out_dir.mkdir()
    content = tmp_path / "content.txt"
    content.write_text(json.dumps(["¿A dónde vas con esto?", "Are you sure about this?\nReally?"]), encoding="utf-8")
    assert cue.main(["x", "batch-validate", str(content), "es-MX", str(out_dir), *paths]) == 1
    assert capsys.readouterr().out.strip() == "1/2 accepted; rejected cue(s) 5 (substantial_dialogue_unchanged)"
    assert (out_dir / "0.cue").read_bytes() == "4\n00:00:00,000 --> 00:00:00,500\n¿A dónde vas con esto?\n\n".encode("utf-8")
    assert not (out_dir / "1.cue").exists()

    content.write_text('["¿A dónde vas con esto?", "¿Estás seguro?\\n¿De verdad?"]', encoding="utf-8")
    assert cue.main(["x", "batch-validate", str(content), "es-MX", str(out_dir), *paths]) == 0
    assert capsys.readouterr().out == ""
    assert (out_dir / "1.cue").read_bytes() == "5\n00:00:01,000 --> 00:00:01,500\n¿Estás seguro?\n¿De verdad?\n\n".encode("utf-8")

    (out_dir / "0.cue").unlink()
    (out_dir / "1.cue").unlink()
    content.write_text('["Solo uno"]', encoding="utf-8")
    assert cue.main(["x", "batch-validate", str(content), "es-MX", str(out_dir), *paths]) == 3
    assert capsys.readouterr().out.strip() == "item_count_1_of_2"
    assert list(out_dir.iterdir()) == []


def test_batch_validate_rejects_an_unencodable_item_and_keeps_the_rest(tmp_path, capsys):
    # json.loads turns the model's "\ud83c" escape into a lone surrogate.
    paths = write_chunks(tmp_path, ("1", "Where are you going?"), ("2", "Who are you?"), ("3", "Why not?"))
    out_dir = tmp_path / "batch"
    out_dir.mkdir()
    content = tmp_path / "content.txt"
    content.write_text('["¿A dónde vas?", "\\ud83c", "¿Por qué no?"]', encoding="utf-8")
    assert cue.main(["x", "batch-validate", str(content), "es-MX", str(out_dir), *paths]) == 1
    assert capsys.readouterr().out.strip() == "2/3 accepted; rejected cue(s) 2 (invalid_unicode)"
    assert sorted(path.name for path in out_dir.iterdir()) == ["0.cue", "2.cue"]
    assert (out_dir / "2.cue").read_bytes() == "3\n00:00:02,000 --> 00:00:02,500\n¿Por qué no?\n\n".encode("utf-8")
