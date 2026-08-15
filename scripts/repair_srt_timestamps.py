#!/usr/bin/env python3
"""Repair non-positive SRT cue durations while preserving dialogue and originals."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

STAMP = re.compile(
    r"^(?P<prefix>\s*)(?P<start>\d{2}:\d{2}:\d{2},\d{3})"
    r"(?P<arrow>\s+-->\s+)(?P<end>\d{2}:\d{2}:\d{2},\d{3})(?P<suffix>.*)$"
)


def to_ms(value: str) -> int:
    hours, minutes, rest = value.replace(",", ".").split(":")
    return round((int(hours) * 3600 + int(minutes) * 60 + float(rest)) * 1000)


def from_ms(value: int) -> str:
    value = max(0, value)
    hours, rem = divmod(value, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def repair(path: Path, backup_root: Path) -> tuple[int, Path | None]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    newline = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    parsed = [(index, match) for index, line in enumerate(lines) if (match := STAMP.match(line))]
    fixes = 0
    for position, (line_index, match) in enumerate(parsed):
        start, end = to_ms(match.group("start")), to_ms(match.group("end"))
        if end > start:
            continue
        next_start = None
        if position + 1 < len(parsed):
            next_start = to_ms(parsed[position + 1][1].group("start"))
        proposed = start + 2000
        if next_start is not None and next_start > start:
            proposed = min(proposed, max(start + 1, next_start - 1))
        lines[line_index] = (
            f"{match.group('prefix')}{match.group('start')}{match.group('arrow')}"
            f"{from_ms(proposed)}{match.group('suffix')}"
        )
        fixes += 1
    if not fixes:
        return 0, None

    digest = hashlib.sha256(os.fsencode(path)).hexdigest()[:16]
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = backup_root / f"{digest}-{path.name}"
    if not backup.exists():
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)

    repaired = newline.join(lines)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(repaired)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, path.stat().st_mode & 0o777)
    os.replace(temporary, path)
    return fixes, backup


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup-root", required=True, type=Path)
    parser.add_argument("--audit-report", type=Path)
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args()
    paths = args.paths
    if args.audit_report:
        report = args.audit_report.read_text(encoding="utf-8", errors="replace")
        container_paths = re.findall(r"^  \[INVALID[^]]*\] (/media/.*\.srt)$", report, re.MULTILINE)
        paths.extend(
            Path(value.replace("/media/movies", "/mnt/user/data/media/Movies")
                       .replace("/media/tv", "/mnt/user/data/media/TV Shows"))
            for value in container_paths
        )
    if not paths:
        paths = [Path(os.fsdecode(value)) for value in sys.stdin.buffer.read().split(b"\0") if value]
    total = 0
    for path in paths:
        fixes, backup = repair(path, args.backup_root)
        if fixes:
            total += fixes
            print(f"REPAIRED fixes={fixes} backup={backup} file={path}")
    print(f"TOTAL_REPAIRS={total}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main())
