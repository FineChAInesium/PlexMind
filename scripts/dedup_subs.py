#!/usr/bin/env python3
"""
Detect SRT files that are true duplicates for the same title and language
(e.g. one downloaded + one from Whisper) and keep the one with more cues.

- Track kinds stay distinct: forced and SDH/HI/CC files never collide with a
  regular track of the same language.
- Only trailing dotted segments are read as language/tags, so title words
  such as "Part.One" are never mistaken for a language code.
- Inferior files are moved to a quarantine directory, never deleted.

Usage: dedup_subs.py SCAN_DIR
"""

import os
import re
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
QUARANTINE_DIR = Path(os.getenv("DEDUP_QUARANTINE_DIR", str(DATA_DIR / "quarantine" / "dedup")))

LANG_RE = re.compile(r"^[a-z]{2,3}(-[a-z]{2,4})?$")
# Provenance marker only: an older Whisper "Title.<lang>.ai.srt" is a finished
# track and a duplicate of a regular one.
PROVENANCE_TAGS = {"ai"}
# transcribe.sh's Title.ai-<lang>.srt (finished, not yet final) and the
# bilingual pass's temporary file belong to a job, not to the library.
WORK_FILE_RE = re.compile(r"\.(ai-[a-z]{2,3}(-[a-z0-9]{2,8})?|translate_tmp)\.srt$", re.IGNORECASE)
# Track-kind tags; hi/sdh/cc are the same kind of track.
KIND_TAGS = {"hi": "sdh", "sdh": "sdh", "cc": "sdh", "forced": "forced"}


def is_work_file(filename):
    """A job's file: Title.ai.srt (in progress, no language before "ai"),
    Title.ai-<lang>.srt or a translate_tmp. Dedup must never weigh these:
    an in-progress Title.ai.srt grouped with an untagged Title.srt, whatever
    its language, and one of the two was quarantined."""
    if WORK_FILE_RE.search(filename):
        return True
    parts = filename[:-4].split(".") if filename.lower().endswith(".srt") else filename.split(".")
    return len(parts) > 1 and parts[-1].lower() == "ai" and not (
        len(parts) > 2 and LANG_RE.match(parts[-2].lower()))


def classify(filename):
    """Return (base, lang, kind) for an .srt filename."""
    name = filename[:-4] if filename.lower().endswith(".srt") else filename
    parts = name.split(".")
    lang = None
    kinds = set()
    # Peel tags and at most one language code off the end only.
    while len(parts) > 1:
        tail = parts[-1].lower()
        if tail in PROVENANCE_TAGS:
            parts.pop()
        elif tail == "hi" and lang is None and not (len(parts) > 2 and LANG_RE.match(parts[-2].lower())):
            # "hi" is Hindi unless a language comes before it (Title.en.hi.srt).
            lang = tail
            parts.pop()
        elif tail in KIND_TAGS:
            kinds.add(KIND_TAGS[tail])
            parts.pop()
        elif lang is None and LANG_RE.match(tail):
            lang = tail
            parts.pop()
        else:
            break
    return ".".join(parts), lang or "unknown", ",".join(sorted(kinds))


def count_cues(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return 0, 0
    # A UTF-16 file read as UTF-8 counted 0 cues and always lost.
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"
    content = data.decode(encoding, errors="replace")
    return len(re.findall(r" --> ", content)), len(data)


def quarantine(path, scan_dir):
    relative = Path(path).resolve().relative_to(Path(scan_dir).resolve())
    target = QUARANTINE_DIR / relative
    if target.exists():
        target = target.with_name(f"{target.name}.{int(time.time())}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(path, target)
    return target


def main(scan_dir):
    groups = defaultdict(list)
    for root, _dirs, files in os.walk(scan_dir):
        for name in files:
            if not name.lower().endswith(".srt") or is_work_file(name):
                continue
            groups[(root, *classify(name))].append(os.path.join(root, name))

    moved = 0
    failed = 0
    for paths in groups.values():
        if len(paths) <= 1:
            continue
        scored = sorted(((*count_cues(p), p) for p in paths), reverse=True)
        best_cues, _best_size, best = scored[0]
        for cues, _size, path in scored[1:]:
            try:
                target = quarantine(path, scan_dir)
                moved += 1
                print(f"DEDUP: Quarantined {path} -> {target} (kept {os.path.basename(best)}, {best_cues} vs {cues} cues)")
            except Exception as exc:
                failed += 1
                print(f"DEDUP: Failed to quarantine {path}: {exc}")

    print(f"TOTAL:{moved}")
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: dedup_subs.py SCAN_DIR", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
