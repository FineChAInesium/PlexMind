#!/usr/bin/env python3
"""
Per-cue and batch helpers for translate.sh.

  translation_cue.py validate SOURCE_TEXT OUTPUT_PATH LANG
      Prints "ok" (exit 0) or a rejection reason (exit 1).
  translation_cue.py passthrough SOURCE_TEXT
      Exit 0 if the cue must be copied verbatim instead of translated
      (the PlexMind watermark cue), else exit 1.
  translation_cue.py classify CHUNK_FILE...
      Prints "passthrough" or "translate" for each single-cue chunk file.
  translation_cue.py batch-sources CHUNK_FILE...
      Prints the chunk files' dialogue as one JSON array of strings.
  translation_cue.py batch-validate CONTENT_PATH LANG OUT_DIR CHUNK_FILE...
      Checks a batched reply (a JSON array with one translation per chunk
      file) and writes each accepted cue to OUT_DIR/<index>.cue. Exit 0 if
      every item was accepted, 1 if some were rejected (summary printed),
      3 if the reply is unusable (reason printed).
"""
import json
import os
import re
import sys

# English function words that are not also common Spanish words, so an
# "unchanged" or untranslated cue is only rejected when it is real English
# sentence text — not names, places, titles, or "No, no, no".
ENGLISH_MARKERS = frozenset("""
the and you your yours are is was were be been being what this that these those
with have has had it its of to for not do does did we they them their there here
about would could should will just know think want from my our his her him she
who why how when where which can cannot don doesn didn won isn aren wasn weren
i im ive ill id youre were theyre thats whats lets let get got go going come
okay yes yeah well right really very much sure please thank thanks sorry
""".split())

MIN_MARKERS = 2


def english_markers(text):
    words = re.findall(r"[a-z]+", text.casefold().replace("'", ""))
    return sum(1 for word in words if word in ENGLISH_MARKERS)


def validate(source, output, lang):
    """Return None if the translation is acceptable, else a rejection reason."""
    output = output.strip()
    if not output:
        return "empty_output"
    if "-->" in output or re.search(r"(?im)^\s*```|</?think>|\[(?:target|previous context)", output):
        return "unexpected_formatting"
    if re.search(r"(?m)^\s*\d+\s*$", output):
        return "cue_number_in_output"
    # Only treat a cue as untranslated when it is genuine English sentence
    # text. Names, acronyms, interjections and cognates legitimately survive.
    if english_markers(source) < MIN_MARKERS:
        return None
    if lang == "zh" and not re.search(r"[㐀-鿿]", output):
        return "missing_target_script"
    src_words = re.findall(r"[a-z]+", source.casefold())
    out_words = re.findall(r"[a-z]+", output.casefold())
    if src_words == out_words:
        return "substantial_dialogue_unchanged"
    return None


def _strip_tags(text):
    return re.sub(r"\{[^}]*\}|<[^>]+>", "", text).strip()


def is_passthrough(source):
    """The PlexMind watermark cue is copied verbatim, never sent to the LLM."""
    # Same rule lib.sh uses to find an existing watermark: WATERMARK_SEARCH
    # appears in the cue, or the cue is exactly WATERMARK_TEXT. The raw cue is
    # searched too (like lib.sh's grep -F): a legacy "{\an8<i>...</i>}"
    # watermark is a single override block, so its stripped text is empty.
    text = _strip_tags(source).casefold()
    search = (os.getenv("WATERMARK_SEARCH") or "PlexMind").strip().casefold()
    watermark = _strip_tags(os.getenv("WATERMARK_TEXT", "")).casefold()
    return bool((search and (search in source.casefold() or search in text))
                or (text and text == watermark))


def read_cue(path):
    """Return (number, timestamp, dialogue) of a chunk file written by translate.sh."""
    with open(path, encoding="utf-8", errors="strict") as handle:
        lines = handle.read().rstrip("\n").split("\n")
    return lines[0], (lines[1] if len(lines) > 1 else ""), "\n".join(lines[2:])


def normalize_item(text):
    """Drop blank lines: inside an SRT cue they would end the cue early."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line for line in lines if line.strip())


def parse_batch(content, count):
    """Return (items, None) for a JSON array of `count` strings, else (None, reason)."""
    text = re.sub(r"(?s)^\s*<think>.*?</think>", "", content).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        items = json.loads(text)
    except ValueError:
        return None, "invalid_json"
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        return None, "not_a_string_array"
    if len(items) != count:
        return None, f"item_count_{len(items)}_of_{count}"
    return items, None


def unicode_problem(text):
    """json.loads turns a "\\ud83c" escape into a lone surrogate, which has no
    UTF-8 form; such an item is rejected rather than written as '?'."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return "invalid_unicode"
    return None


def validate_batch(sources, content, lang):
    """Return (items, reasons) with reasons[i] None for each accepted item,
    or (None, reason) when the reply cannot be used at all."""
    items, reason = parse_batch(content, len(sources))
    if reason:
        return None, reason
    items = [normalize_item(item) for item in items]
    return items, [unicode_problem(item) or validate(source, item, lang) for source, item in zip(sources, items)]


def main(argv):
    if len(argv) == 5 and argv[1] == "validate":
        with open(argv[3], encoding="utf-8", errors="strict") as handle:
            reason = validate(argv[2], handle.read(), argv[4])
        print(reason or "ok")
        return 1 if reason else 0
    if len(argv) == 3 and argv[1] == "passthrough":
        return 0 if is_passthrough(argv[2]) else 1
    if len(argv) >= 3 and argv[1] == "classify":
        for path in argv[2:]:
            print("passthrough" if is_passthrough(read_cue(path)[2]) else "translate")
        return 0
    if len(argv) >= 3 and argv[1] == "batch-sources":
        print(json.dumps([read_cue(path)[2] for path in argv[2:]], ensure_ascii=False))
        return 0
    if len(argv) >= 6 and argv[1] == "batch-validate":
        content_path, lang, out_dir = argv[2:5]
        cues = [read_cue(path) for path in argv[5:]]
        with open(content_path, encoding="utf-8", errors="strict") as handle:
            items, reasons = validate_batch([cue[2] for cue in cues], handle.read(), lang)
        if items is None:
            print(reasons)
            return 3
        rejected = []
        for index, ((number, timestamp, _), item, reason) in enumerate(zip(cues, items, reasons)):
            if reason:
                rejected.append(f"{number} ({reason})")
                continue
            # Renamed into place, so a write that fails never leaves a partial cue.
            path = os.path.join(out_dir, f"{index}.cue")
            with open(f"{path}.tmp", "w", encoding="utf-8", newline="") as handle:
                handle.write(f"{number}\n{timestamp}\n{item}\n\n")
            os.replace(f"{path}.tmp", path)
        if not rejected:
            return 0
        print(f"{len(cues) - len(rejected)}/{len(cues)} accepted; rejected cue(s) {', '.join(rejected)}")
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
