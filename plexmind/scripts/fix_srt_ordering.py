#!/usr/bin/env python3
"""
Fix timestamp ordering in script-generated SRT translations.
- Sorts blocks by start timestamp
- Re-numbers sequentially
- Skips files that are already in order
"""

import re
import sys
from pathlib import Path

MEDIA_DIRS = [
    Path("/mnt/user/data/media/Movies"),
    Path("/mnt/user/data/media/TV Shows"),
]

def parse_time_ms(ts):
    m = re.match(r'(\d{2}):(\d{2}):(\d{2}),(\d{3})', ts)
    if not m:
        return None
    h, mn, s, ms = map(int, m.groups())
    return h * 3600000 + mn * 60000 + s * 1000 + ms

def parse_srt(content):
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    blocks = []
    for raw in re.split(r'\n{2,}', content.strip()):
        lines = raw.strip().split('\n')
        if not lines:
            continue
        # Skip optional block number line
        idx = 1 if re.match(r'^\d+$', lines[0]) else 0
        if idx >= len(lines):
            continue
        m = re.match(
            r'(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})',
            lines[idx]
        )
        if not m:
            continue
        start_str, end_str = m.group(1), m.group(2)
        start_ms, end_ms = parse_time_ms(start_str), parse_time_ms(end_str)
        if start_ms is None or end_ms is None:
            continue
        text = '\n'.join(lines[idx + 1:]).strip()
        if not text:
            continue
        blocks.append((start_ms, end_ms, start_str, end_str, text))
    return blocks

def fix_file(path):
    try:
        content = path.read_text(encoding='utf-8', errors='replace')
    except Exception as e:
        return 'error', str(e)

    blocks = parse_srt(content)
    if not blocks:
        return 'skip', 'no blocks parsed'

    sorted_blocks = sorted(blocks, key=lambda b: b[0])
    issues = sum(1 for a, b in zip(blocks, sorted_blocks) if a[0] != b[0])

    if issues == 0:
        return 'ok', 0

    lines = []
    for i, (_, _, start_str, end_str, text) in enumerate(sorted_blocks, 1):
        lines.append(f"{i}\n{start_str} --> {end_str}\n{text}\n")

    try:
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    except Exception as e:
        return 'error', str(e)

    return 'fixed', issues

def find_translated_files():
    """
    Find all script-generated translated files:
      *.es-MX.srt  — all are ours
      *.zh.srt     — ours, excluding pre-existing *.TW.zh.srt / *.CHS.zh.srt / *.CHT.zh.srt
    """
    files = []
    for base in MEDIA_DIRS:
        if not base.exists():
            continue
        for p in base.rglob('*.srt'):
            name = p.name
            if name.endswith('.es-MX.srt'):
                files.append(p)
            elif name.endswith('.zh.srt') and not re.search(
                r'\.(TW|CHS|CHT|SC|TC)\.zh\.srt$', name, re.IGNORECASE
            ):
                files.append(p)
    return sorted(files)

def main():
    files = find_translated_files()
    print(f"Found {len(files)} translated SRT files to check.\n")

    total_fixed = 0
    total_ok = 0
    total_errors = 0

    for path in files:
        status, detail = fix_file(path)
        if status == 'fixed':
            print(f"  FIXED ({detail} order issues): {path.name}")
            total_fixed += 1
        elif status == 'error':
            print(f"  ERROR: {path.name} — {detail}")
            total_errors += 1
        else:
            total_ok += 1

    print(f"\nDone. Fixed: {total_fixed} | Already OK: {total_ok} | Errors: {total_errors}")

if __name__ == '__main__':
    main()
