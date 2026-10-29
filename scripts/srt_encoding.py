#!/usr/bin/env python3
"""
Check and repair the text encoding of subtitle files without losing data.

- Valid UTF-8 without a BOM is never rewritten.
- A UTF-8 BOM is stripped and BOM-marked UTF-16 is re-encoded as UTF-8.
- Other text is converted only from the charset `file` reports, and only
  when every byte decodes (never `iconv -c`). Text labelled Latin-1 whose
  letters are mostly non-ASCII is refused: it is far more likely Cyrillic,
  Greek or Hebrew in a Windows code page than Western European.
- `file` calls any 8-bit text without C1 bytes iso-8859-1, so that label is
  trusted only for files tagged with a Western European language
  (Title.xx[.forced|.sdh].srt), and C1 bytes past its sample are read as
  Windows-1252. Text that is mostly valid UTF-8 with stray bytes is damaged
  UTF-8, not another charset, and is refused.
- The original is copied to the backup directory first, then replaced
  atomically keeping its mode and, where permitted, its ownership.

Usage:
  srt_encoding.py repair --backup-dir DIR [--charset NAME] FILE
  srt_encoding.py check [--limit N] < NUL-separated paths
"""

import argparse
import codecs
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Charsets `file` reports that say nothing about how to decode the bytes.
UNUSABLE_CHARSETS = {"", "unknown", "unknown-8bit", "binary", "us-ascii", "utf-8"}
WESTERN_CODECS = {"iso8859-1", "cp1252"}
MAX_NON_ASCII_LETTERS = 0.3
# Languages Latin-1/Windows-1252 actually covers. Anything else labelled
# iso-8859-1 (Polish, Czech, Turkish...) is in a code page `file` cannot name.
WESTERN_LANGUAGES = {
    "en", "eng", "fr", "fre", "fra", "de", "ger", "deu", "es", "spa", "it", "ita",
    "pt", "por", "nl", "dut", "nld", "sv", "swe", "da", "dan", "no", "nor", "nb",
    "nob", "nn", "nno", "fi", "fin", "is", "ice", "isl", "ca", "cat", "gl", "glg",
    "eu", "baq", "eus", "ga", "gle", "af", "afr",
}
LANG_RE = re.compile(r"^([a-z]{2,3})(-[a-z0-9]{2,8})?$")
# Track-kind tags that may follow or precede the language code.
KIND_TAGS = {"forced", "hi", "sdh", "cc"}


def classify(data):
    """Return ok, utf-8-bom, utf-16 or non-utf-8 for raw subtitle bytes."""
    if data.startswith(codecs.BOM_UTF8):
        return "utf-8-bom"
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    if b"\0" not in data:
        try:
            data.decode("utf-8")
            return "ok"
        except UnicodeDecodeError:
            pass
    return "non-utf-8"


def sniff_charset(path):
    """Ask libmagic (`file`) for the charset; empty when it is unavailable."""
    try:
        result = subprocess.run(
            ["file", "-b", "--mime-encoding", "--", str(path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip().lower() if result.returncode == 0 else ""


def _mostly_non_ascii(text):
    letters = [char for char in text if char.isalpha()]
    return bool(letters) and sum(ord(char) > 127 for char in letters) > MAX_NON_ASCII_LETTERS * len(letters)


def _mostly_utf8(data):
    """True when valid multibyte UTF-8 characters at least match invalid bytes."""
    probe = data.decode("utf-8", "surrogateescape")
    bad = sum("\udc80" <= char <= "\udcff" for char in probe)
    good = sum(ord(char) > 127 for char in probe) - bad
    return good > 0 and good >= bad


def _codec_name(charset):
    try:
        return codecs.lookup(charset).name
    except LookupError:
        return charset


def language_tag(name):
    """Return the language code of Title.xx[.forced|.sdh].srt, or ''."""
    parts = Path(name).stem.lower().split(".")
    # Peel kind tags and at most one language code off the end only.
    while len(parts) > 1:
        tail = parts.pop()
        if tail not in KIND_TAGS:
            match = LANG_RE.match(tail)
            return match.group(1) if match else ""
    return ""


def decode(data, charset="", name=""):
    """Return (text, codec) for subtitle bytes that are not plain UTF-8."""
    kind = classify(data)
    if kind == "utf-8-bom":
        codec = "utf-8-sig"
    elif kind == "utf-16":
        codec = "utf-16"
    elif charset in UNUSABLE_CHARSETS:
        raise ValueError(f"has an unrecognised encoding ({charset or 'unknown'})")
    elif _mostly_utf8(data):
        raise ValueError("looks like damaged UTF-8 rather than another charset")
    else:
        codec = charset
        if _codec_name(codec) == "iso8859-1":
            if language_tag(name) not in WESTERN_LANGUAGES:
                raise ValueError("is labelled iso-8859-1 but is not tagged with a Western European language")
            if re.search(rb"[\x80-\x9f]", data):
                # `file` samples only the start; C1 bytes past it mean Windows-1252.
                codec = "cp1252"
    try:
        text = data.decode(codec)
        codec = codecs.lookup(codec).name
    except (LookupError, UnicodeDecodeError) as exc:
        raise ValueError(f"does not decode cleanly as {codec}") from exc
    if "\0" in text:
        raise ValueError(f"contains NUL characters as {codec}")
    if codec in WESTERN_CODECS and _mostly_non_ascii(text):
        raise ValueError(f"is mostly non-ASCII as {codec}, so its real encoding is ambiguous")
    return text, codec


def backup_path(path, backup_dir):
    digest = hashlib.sha256(os.fsencode(path.resolve())).hexdigest()[:16]
    backup = Path(backup_dir) / f"{digest}-{path.name}"
    if backup.exists():
        backup = backup.with_name(f"{backup.name}.{time.time_ns()}")
    return backup


def repair(path, backup_dir, charset=None):
    """Return (status, detail) where status is ok, converted or failed."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        return "failed", f"is unreadable ({exc.strerror or exc})"
    kind = classify(data)
    if kind == "ok":
        return "ok", ""
    if charset is None:
        charset = sniff_charset(path) if kind == "non-utf-8" else ""
    try:
        text, codec = decode(data, charset.strip().lower(), path.name)
    except ValueError as exc:
        return "failed", str(exc)

    temporary = None
    try:
        stat = path.stat()
        backup = backup_path(path, backup_dir)
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
        with tempfile.NamedTemporaryFile(
            "wb", dir=path.parent, prefix=f".{path.name}.", suffix=".enc_tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.st_mode & 0o777)
        if hasattr(os, "chown"):  # absent on Windows test hosts
            try:
                os.chown(temporary, stat.st_uid, stat.st_gid)
            except PermissionError:
                pass
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        return "failed", f"could not be rewritten ({exc})"
    return "converted", f"from {codec}; original saved as {backup}"


def check(raw_paths, limit, out):
    """Write problem|path records (NUL-terminated) and a final checked|N."""
    checked = 0
    for raw in raw_paths:
        if not raw:
            continue
        if checked >= limit:
            break
        checked += 1
        try:
            problem = classify(Path(os.fsdecode(raw)).read_bytes())
        except OSError:
            problem = "unreadable"
        if problem != "ok":
            out.write(problem.encode() + b"|" + raw + b"\0")
    out.write(f"checked|{checked}".encode() + b"\0")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check or repair subtitle text encoding.")
    commands = parser.add_subparsers(dest="command", required=True)
    repair_cmd = commands.add_parser("repair", help="convert FILE to UTF-8 if needed; prints status|detail")
    repair_cmd.add_argument("--backup-dir", required=True)
    repair_cmd.add_argument("--charset", help="charset hint (default: ask `file`)")
    repair_cmd.add_argument("file")
    check_cmd = commands.add_parser("check", help="classify NUL-separated paths read from stdin")
    check_cmd.add_argument("--limit", type=int, default=200)
    args = parser.parse_args(argv)

    if args.command == "repair":
        status, detail = repair(args.file, args.backup_dir, args.charset)
        print(f"{status}|{detail}")
        return 1 if status == "failed" else 0

    check(sys.stdin.buffer.read().split(b"\0"), args.limit, sys.stdout.buffer)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
