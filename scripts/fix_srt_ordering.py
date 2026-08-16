#!/usr/bin/env python3
"""
Fix timestamp ordering in script-generated SRT translations.
- Sorts blocks by start timestamp
- Re-numbers sequentially
- Skips files that are already in order
"""

import os
import re
import fcntl
import hashlib
import shutil
import tempfile
from pathlib import Path

MEDIA_DIRS = [
    Path(os.getenv("MOVIE_DIR", "/media/movies")),
    Path(os.getenv("TV_DIR", "/media/tv")),
]
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
BACKUP_DIR = Path(os.getenv("ORDERING_BACKUP_DIR", str(DATA_DIR / "quarantine" / "srt-ordering-backups")))
LOCK_FILE = Path(os.getenv("MEDIA_MUTATION_LOCK", str(DATA_DIR / "plexmind_media_mutation.lock")))

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
    raw_blocks = [block for block in re.split(r'\n{2,}', content.replace('\r\n', '\n').replace('\r', '\n').strip()) if block.strip()]
    if len(blocks) != len(raw_blocks):
        return 'error', f'refusing lossy rewrite: parsed {len(blocks)} of {len(raw_blocks)} blocks'

    sorted_blocks = sorted(blocks, key=lambda b: b[0])
    issues = sum(1 for a, b in zip(blocks, sorted_blocks) if a[0] != b[0])

    if issues == 0:
        return 'ok', 0

    lines = []
    for i, (_, _, start_str, end_str, text) in enumerate(sorted_blocks, 1):
        lines.append(f"{i}\n{start_str} --> {end_str}\n{text}\n")

    try:
        stat = path.stat()
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(os.fsencode(path)).hexdigest()[:16]
        backup = BACKUP_DIR / f"{digest}-{path.name}"
        if not backup.exists():
            shutil.copy2(path, backup)
            os.chmod(backup, 0o600)
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write('\n'.join(lines) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.st_mode & 0o777)
        try:
            os.chown(temporary, stat.st_uid, stat.st_gid)
        except PermissionError:
            pass
        os.replace(temporary, path)
    except Exception as e:
        if 'temporary' in locals():
            temporary.unlink(missing_ok=True)
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
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"Another media mutation is running (lock: {LOCK_FILE}).")
            return 1
        return run()

def run():
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
    return 1 if total_errors else 0

if __name__ == '__main__':
    raise SystemExit(main())
