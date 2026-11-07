#!/usr/bin/env python3
"""
Quarantine image-based subtitles (PGS .sup, VobSub .idx/.sub) that a text
subtitle next to them exactly replaces.

- A replacement is an .srt for the same title, language and track kind:
  forced only replaces forced, SDH/HI/CC only replaces SDH/HI/CC, and a
  regular track only replaces a regular track.
- transcribe.sh partials and temporary files never count. A regular or SDH
  replacement needs MIN_REPLACEMENT_CUES cues (a forced track, at least one).
- An .srt PlexMind generated (Whisper or LLM translation, recognised by the
  watermark) does not replace an authored image track unless
  --allow-generated is given.
- Unknown-language image subtitles are kept unless --allow-unknown is given;
  then a text track of the same kind in any language counts.
- Files are moved to the quarantine directory, never deleted.

Usage: pgs_cleanup.py [--allow-unknown] (--list | --quarantine-dir DIR) SCAN_DIR...
"""

import argparse
import os
import re
import shutil
import sys
import time
from collections import defaultdict

LANG_RE = re.compile(r"^[a-z]{2,3}(-[a-z]{2,4})?$")
# Track-kind tags; hi/sdh/cc are the same kind of track.
KIND_TAGS = {"hi": "sdh", "sdh": "sdh", "cc": "sdh", "forced": "forced"}
# Title.ai.srt (in progress), Title.ai-<lang>.srt (finished, not yet final)
# and the bilingual translate pass's temporary file.
PARTIAL_RE = re.compile(r"\.(ai|ai-[a-z]{2,3}(-[a-z0-9]{2,8})?|translate_tmp)\.srt$", re.IGNORECASE)
CUE_RE = re.compile(r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}")
MIN_REPLACEMENT_CUES = int(os.getenv("PGS_MIN_REPLACEMENT_CUES", "20"))
WATERMARK_SEARCH = os.getenv("WATERMARK_SEARCH", "PlexMind")


def classify(stem):
    """Return (title, language or None, kind) for a name without extension."""
    parts = stem.split(".")
    lang = None
    kinds = set()
    # Peel kind tags and at most one language code off the end only.
    while len(parts) > 1:
        tail = parts[-1].lower()
        if tail == "hi" and lang is None and not (len(parts) > 2 and LANG_RE.match(parts[-2].lower())):
            # "hi" is Hindi unless a language comes before it (Title.en.hi).
            lang = tail
        elif tail in KIND_TAGS:
            kinds.add(KIND_TAGS[tail])
        elif lang is None and LANG_RE.match(tail):
            lang = tail
        else:
            break
        parts.pop()
    return ".".join(parts).lower(), lang, ",".join(sorted(kinds))


def read_text(path):
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return ""
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"
    return data.decode(encoding, errors="replace")


def text_tracks(root, names, allow_generated=False):
    """Map (title, language, kind) to the .srt names in ROOT that can replace
    an image track of that kind."""
    tracks = defaultdict(list)
    for name in names:
        if not name.lower().endswith(".srt") or PARTIAL_RE.search(name):
            continue
        content = read_text(os.path.join(root, name))
        key = classify(name[:-4])
        if len(CUE_RE.findall(content)) < (1 if "forced" in key[2] else MIN_REPLACEMENT_CUES):
            continue
        if not allow_generated and WATERMARK_SEARCH and WATERMARK_SEARCH in "\n".join(content.splitlines()[:15]):
            continue
        tracks[key].append(name)
    return tracks


def image_tracks(root, names):
    """Yield (stem, file names) for each PGS track and complete VobSub pair."""
    by_lower = {name.lower(): name for name in names}
    for name in names:
        stem, ext = os.path.splitext(name)
        ext = ext.lower()
        if ext == ".sup":
            members = [name]
        elif ext == ".sub" and f"{stem}.idx".lower() in by_lower:
            members = [name, by_lower[f"{stem}.idx".lower()]]
        else:
            continue
        if not any(os.path.islink(os.path.join(root, member)) for member in members):
            yield stem, members


def find_cleanable(scan_dir, allow_unknown, allow_generated=False):
    """Yield (root, file names, replacement names) per replaced image track."""
    for root, dirs, names in os.walk(scan_dir):
        dirs.sort()
        images = list(image_tracks(root, sorted(names)))
        if not images:
            continue
        tracks = text_tracks(root, names, allow_generated)
        for stem, members in images:
            title, lang, kind = classify(stem)
            if lang is None and not allow_unknown:
                continue
            replacements = sorted(
                name
                for (srt_title, srt_lang, srt_kind), srt_names in tracks.items()
                if srt_title == title and srt_kind == kind and (lang is None or srt_lang == lang)
                for name in srt_names
            )
            if replacements:
                yield root, members, replacements


def quarantine(paths, scan_dir, quarantine_dir):
    """Move one track's files under QUARANTINE_DIR; yield (source, target)."""
    targets = [os.path.join(quarantine_dir, os.path.relpath(path, scan_dir)) for path in paths]
    if any(os.path.lexists(target) for target in targets):
        suffix = f".{time.time_ns()}"
        targets = [target + suffix for target in targets]
    for path, target in zip(paths, targets):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.move(path, target)
        yield path, target


def main(argv=None):
    parser = argparse.ArgumentParser(description="Quarantine image subtitles that an exact SRT replaces.")
    parser.add_argument("--allow-unknown", action="store_true",
                        help="also clean image subtitles without a language tag")
    parser.add_argument("--allow-generated", action="store_true",
                        help="let PlexMind's own Whisper/LLM subtitles replace image subtitles")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true",
                      help="print each replaceable track (NUL-separated) and change nothing")
    mode.add_argument("--quarantine-dir", help="directory that replaced image subtitles are moved to")
    parser.add_argument("scan_dirs", nargs="+")
    args = parser.parse_args(argv)

    moved = 0
    failed = 0
    for scan_dir in args.scan_dirs:
        for root, members, replacements in find_cleanable(scan_dir, args.allow_unknown, args.allow_generated):
            paths = [os.path.join(root, member) for member in members]
            if args.list:
                sys.stdout.buffer.write(os.fsencode(paths[0]) + b"\0")
                continue
            try:
                for path, target in quarantine(paths, scan_dir, args.quarantine_dir):
                    moved += 1
                    print(f"PGS CLEANUP: Quarantined {path} -> {target} (replaced by {', '.join(replacements)})")
            except OSError as exc:
                failed += 1
                print(f"PGS CLEANUP: Failed to quarantine {paths[0]}: {exc}")

    if args.list:
        sys.stdout.buffer.flush()
    else:
        print(f"TOTAL:{moved}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
