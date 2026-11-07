#!/usr/bin/env python3
"""
Whether a video already has a full subtitle in a language that Plex can show.

- Counts subtitle tracks embedded in the file (ffprobe), and every subtitle
  Plex lists for the item, which includes ones Plex downloaded itself (for
  example from OpenSubtitles) and external files.
- Forced tracks never count: Plex's forced flag, or "forced" in the track
  title ("English [ForcedNarrative]").
- Languages match on their base, so es-MX matches any Spanish subtitle and zh
  any Chinese one.
- Results are cached per file in DATA_DIR/subtitle_inventory.json, keyed by
  size and mtime and refreshed after SUBTITLE_INVENTORY_TTL_HOURS, so a
  subtitle Plex downloads later is seen. Plex's file -> item map is cached in
  DATA_DIR/plex_part_index.json. Without Plex (no PLEX_URL/PLEX_TOKEN, or it
  is unreachable) only embedded tracks count, and nothing is cached.

Usage: subtitle_inventory.py has LANG VIDEO
       Exit 0 and print where (embedded / Plex) if VIDEO has one, else exit 1.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
CACHE_FILE = DATA_DIR / "subtitle_inventory.json"
INDEX_FILE = DATA_DIR / "plex_part_index.json"
TTL_SECONDS = float(os.getenv("SUBTITLE_INVENTORY_TTL_HOURS", "24")) * 3600
INDEX_TTL_SECONDS = 6 * 3600
PLEX_URL = os.getenv("PLEX_URL", "").rstrip("/")
PLEX_TOKEN = os.getenv("PLEX_TOKEN", "")
# PlexMind's own mount of each kind of Plex library section.
ROOTS = {"movie": os.getenv("MOVIE_DIR", "/media/movies").rstrip("/"),
         "show": os.getenv("TV_DIR", "/media/tv").rstrip("/")}

ISO_639_2 = {
    "eng": "en", "spa": "es", "chi": "zh", "zho": "zh", "cmn": "zh", "yue": "zh",
    "fre": "fr", "fra": "fr", "ger": "de", "deu": "de", "ita": "it", "jpn": "ja",
    "kor": "ko", "por": "pt", "rus": "ru", "ara": "ar", "dut": "nl", "nld": "nl",
    "swe": "sv", "nor": "no", "nob": "no", "nno": "no", "dan": "da", "fin": "fi",
    "pol": "pl", "tur": "tr", "hin": "hi", "heb": "he", "tha": "th", "vie": "vi",
    "ind": "id", "ukr": "uk", "cze": "cs", "ces": "cs", "gre": "el", "ell": "el",
    "hun": "hu", "rum": "ro", "ron": "ro", "msa": "ms", "may": "ms",
}
UNDEFINED = {"", "und", "unk", "mis", "zxx", "mul"}


def base_lang(code):
    """Base ISO 639-1 language of a tag such as eng, es-MX, zh-Hant or spa."""
    code = str(code or "").strip().lower().replace("_", "-").split("-")[0]
    return "" if code in UNDEFINED else ISO_639_2.get(code, code)


def is_forced(flag, title):
    return str(flag).lower() in ("1", "true") or "forced" in str(title or "").lower()


def embedded_langs(video, run=subprocess.run):
    """Languages of the full subtitle tracks inside VIDEO."""
    try:
        result = run(["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries",
                      "stream_tags=language,title:stream_disposition=forced", "-of", "json", video],
                     capture_output=True, text=True, timeout=120)
        streams = json.loads(result.stdout or "{}").get("streams") or []
    except (OSError, subprocess.SubprocessError, ValueError):
        return set()
    langs = set()
    for stream in streams:
        tags = stream.get("tags") or {}
        if not is_forced((stream.get("disposition") or {}).get("forced"), tags.get("title")):
            langs.add(base_lang(tags.get("language")))
    return langs - {""}


def _plex_get(path):
    request = urllib.request.Request(f"{PLEX_URL}{path}", headers={
        "X-Plex-Token": PLEX_TOKEN, "Accept": "application/xml"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return ET.fromstring(response.read())


def _load(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(value, handle)
    os.replace(handle.name, path)


def map_part(file, locations, root):
    """PlexMind's path for a Plex part FILE in a section at LOCATIONS mounted at ROOT."""
    for location in locations:
        location = location.rstrip("/")
        if location and file.startswith(location + "/"):
            return root + file[len(location):]
    return None


def build_index(get=None):
    """Map each video's PlexMind path to its Plex ratingKey."""
    get = get or _plex_get
    parts = {}
    for section in get("/library/sections").findall("Directory"):
        kind = section.get("type")
        if kind not in ROOTS:
            continue
        locations = [location.get("path", "") for location in section.findall("Location")]
        item_type = 1 if kind == "movie" else 4  # movies, or episodes
        start = 0
        while True:
            page = get(f"/library/sections/{section.get('key')}/all?type={item_type}"
                       f"&X-Plex-Container-Start={start}&X-Plex-Container-Size=1000")
            videos = page.findall("Video")
            for video in videos:
                for part in video.findall("Media/Part"):
                    path = map_part(part.get("file", ""), locations, ROOTS[kind])
                    if path:
                        parts[path] = video.get("ratingKey")
            start += len(videos)
            if not videos or start >= int(page.get("totalSize") or start):
                break
    return parts


def plex_index():
    cached = _load(INDEX_FILE)
    if cached.get("built", 0) > time.time() - INDEX_TTL_SECONDS:
        return cached.get("parts") or {}
    parts = build_index()
    _save(INDEX_FILE, {"built": time.time(), "parts": parts})
    return parts


def plex_langs(video, get=None, index=None):
    """Languages of the full subtitles Plex lists for VIDEO; None if Plex could
    not be asked (so the answer is not cached)."""
    if get is None and not (PLEX_URL and PLEX_TOKEN):
        return None
    try:
        rating_key = (index if index is not None else plex_index()).get(video)
        if not rating_key:
            return set()  # Plex has not scanned this file yet
        metadata = (get or _plex_get)(f"/library/metadata/{rating_key}")
    except Exception:
        return None
    langs = set()
    for stream in metadata.iter("Stream"):
        if stream.get("streamType") != "3":
            continue
        if not is_forced(stream.get("forced"), stream.get("title") or stream.get("extendedDisplayTitle")):
            langs.add(base_lang(stream.get("languageTag") or stream.get("languageCode")))
    return langs - {""}


def subtitle_langs(video):
    """(embedded languages, Plex languages) for VIDEO, cached."""
    stat = os.stat(video)
    signature = f"{stat.st_size}:{int(stat.st_mtime)}"
    cache = _load(CACHE_FILE)
    entry = cache.get(video)
    if entry and entry.get("sig") == signature and entry.get("checked", 0) > time.time() - TTL_SECONDS:
        return set(entry.get("embedded") or []), set(entry.get("plex") or [])
    embedded, plex = embedded_langs(video), plex_langs(video)
    if plex is not None:
        cache[video] = {"sig": signature, "checked": time.time(),
                        "embedded": sorted(embedded), "plex": sorted(plex)}
        _save(CACHE_FILE, cache)
    return embedded, plex or set()


def main(argv):
    if len(argv) != 4 or argv[1] != "has":
        print("usage: subtitle_inventory.py has LANG VIDEO", file=sys.stderr)
        return 2
    lang, video = base_lang(argv[2]), argv[3]
    try:
        embedded, plex = subtitle_langs(video)
    except OSError:
        return 1
    if lang in embedded:
        print("embedded")
        return 0
    if lang in plex:
        print("Plex")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
