#!/usr/bin/env python3
"""
Per-cue helpers for translate.sh.

  translation_cue.py validate SOURCE_TEXT OUTPUT_PATH LANG
      Prints "ok" (exit 0) or a rejection reason (exit 1).
  translation_cue.py passthrough SOURCE_TEXT
      Exit 0 if the cue must be copied verbatim instead of translated
      (the PlexMind watermark cue), else exit 1.
"""
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
    # appears in the cue, or the cue is exactly WATERMARK_TEXT.
    text = _strip_tags(source).casefold()
    if not text:
        return False
    search = os.getenv("WATERMARK_SEARCH", "PlexMind").strip().casefold()
    watermark = _strip_tags(os.getenv("WATERMARK_TEXT", "")).casefold()
    return bool((search and search in text) or (watermark and text == watermark))


def main(argv):
    if len(argv) == 5 and argv[1] == "validate":
        with open(argv[3], encoding="utf-8", errors="strict") as handle:
            reason = validate(argv[2], handle.read(), argv[4])
        print(reason or "ok")
        return 1 if reason else 0
    if len(argv) == 3 and argv[1] == "passthrough":
        return 0 if is_passthrough(argv[2]) else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
