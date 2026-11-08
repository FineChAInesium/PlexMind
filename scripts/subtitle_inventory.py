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

- A translation source must be text: picture formats (PGS, VobSub) cannot be
  read without OCR. A track inside the file is preferred to a Plex download,
  and a plain track to SDH. Commentary tracks are never used, and fewer than
  MIN_SOURCE_CUES cues is treated as signs or forced lines, not dialogue.

Usage: subtitle_inventory.py has LANG VIDEO
       Exit 0 and print where (embedded / Plex) if VIDEO has one, else exit 1.
       subtitle_inventory.py source LANG VIDEO
       The same, for a full subtitle in LANG that can be translated from.
       subtitle_inventory.py extract LANG VIDEO OUTPUT
       Write that subtitle to OUTPUT as SRT and print where it came from; on
       failure print why and exit 1.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
CACHE_FILE = DATA_DIR / "subtitle_inventory.json"
SOURCES_FILE = DATA_DIR / "subtitle_sources.json"
INDEX_FILE = DATA_DIR / "plex_part_index.json"
# Codecs of tracks inside a video, and of subtitles Plex holds as files.
EMBEDDED_TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
PLEX_TEXT_CODECS = {"srt", "ass", "ssa", "vtt", "webvtt"}
MIN_SOURCE_CUES = 20
MAX_PLEX_SUBTITLE_BYTES = 20 * 1024 * 1024
FFMPEG_TIMEOUT_SECONDS = 1800
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


def is_sdh(flag, title):
    return str(flag).lower() in ("1", "true") or bool(re.search(r"\b(sdh|cc)\b", str(title or "").lower()))


def usable_source(forced, title):
    return not is_forced(forced, title) and "commentary" not in str(title or "").lower()


def _best(found):
    """The value of the first (sdh, value) pair, preferring tracks without SDH."""
    return min(found, key=lambda item: item[0])[1] if found else None


def embedded_text_track(video, lang, run=subprocess.run):
    """Position of VIDEO's best text track in LANG among its subtitle tracks, or None."""
    try:
        result = run(["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries",
                      "stream=codec_name:stream_tags=language,title:stream_disposition=forced,hearing_impaired",
                      "-of", "json", video], capture_output=True, text=True, timeout=120)
        streams = json.loads(result.stdout or "{}").get("streams") or []
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    found = []
    for position, stream in enumerate(streams):
        tags = stream.get("tags") or {}
        disposition = stream.get("disposition") or {}
        if (stream.get("codec_name") in EMBEDDED_TEXT_CODECS and base_lang(tags.get("language")) == lang
                and usable_source(disposition.get("forced"), tags.get("title"))):
            found.append((is_sdh(disposition.get("hearing_impaired"), tags.get("title")), position))
    return _best(found)


def plex_text_stream(video, lang, get=None, index=None):
    """{key, codec} of the best subtitle file Plex holds for VIDEO in LANG; {} if
    it has none; None if Plex could not be asked. Tracks inside the file have no
    key: they are read from the file itself."""
    if get is None and not (PLEX_URL and PLEX_TOKEN):
        return None
    try:
        rating_key = (index if index is not None else plex_index()).get(video)
        if not rating_key:
            return {}
        metadata = (get or _plex_get)(f"/library/metadata/{rating_key}")
    except Exception:
        return None
    found = []
    for stream in metadata.iter("Stream"):
        key = stream.get("key") or ""
        codec = (stream.get("codec") or stream.get("format") or "").lower()
        title = stream.get("title") or stream.get("extendedDisplayTitle")
        if (stream.get("streamType") == "3" and key.startswith("/library/streams/") and codec in PLEX_TEXT_CODECS
                and base_lang(stream.get("languageTag") or stream.get("languageCode")) == lang
                and usable_source(stream.get("forced"), title)):
            found.append((is_sdh(stream.get("hearingImpaired"), title), {"key": key, "codec": codec}))
    return _best(found) or {}


def find_source(video, lang):
    """A dict saying where to read a translatable subtitle in LANG, or None. Cached."""
    stat = os.stat(video)
    signature = f"{stat.st_size}:{int(stat.st_mtime)}"
    cache = _load(SOURCES_FILE)
    entry = cache.get(video)
    if not (entry and entry.get("sig") == signature and entry.get("checked", 0) > time.time() - TTL_SECONDS):
        entry = {"sig": signature, "checked": time.time(), "found": {}}
    if lang in entry["found"]:
        return entry["found"][lang]
    track = embedded_text_track(video, lang)
    if track is not None:
        source = {"track": track}
    else:
        source = plex_text_stream(video, lang)
        if source is None:
            return None  # Plex did not answer, so this is not known
    entry["found"][lang] = source or None
    cache[video] = entry
    _save(SOURCES_FILE, cache)
    return source or None


def source_origin(source):
    return "embedded" if "track" in source else "Plex"


def _fetch_plex_file(key):
    request = urllib.request.Request(f"{PLEX_URL}{key}", headers={"X-Plex-Token": PLEX_TOKEN})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(MAX_PLEX_SUBTITLE_BYTES + 1)
    if len(data) > MAX_PLEX_SUBTITLE_BYTES:
        raise ValueError("Plex returned a subtitle over the size limit")
    return data


def _ffmpeg_to_srt(inputs, output, run=subprocess.run):
    result = run(["ffmpeg", "-nostdin", "-v", "error", "-y", *inputs, "-c:s", "srt", str(output)],
                 capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS)
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        raise ValueError(f"ffmpeg could not convert it ({detail[-1] if detail else result.returncode})")


def _utf8_from_plex(path):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import srt_encoding
    status, detail = srt_encoding.repair(path, path.parent / "backup")
    if status == "failed":
        raise ValueError(f"the subtitle from Plex {detail}")


def extract(video, lang, output, run=subprocess.run, fetch=_fetch_plex_file):
    """Write VIDEO's translatable subtitle in LANG to OUTPUT as SRT; return its origin."""
    source = find_source(video, lang)
    if not source:
        raise ValueError(f"no text subtitle in {lang}")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as raw:
        work = Path(raw) / f"source.{lang}.srt"
        if "track" in source:
            _ffmpeg_to_srt(["-i", video, "-map", f"0:s:{source['track']}"], work, run)
        else:
            downloaded = Path(raw) / f"source.{lang}.{source['codec']}"
            downloaded.write_bytes(fetch(source["key"]))
            _utf8_from_plex(downloaded)
            if source["codec"] == "srt":
                shutil.move(str(downloaded), work)
            else:
                _ffmpeg_to_srt(["-i", str(downloaded)], work, run)
        cues = work.read_text(encoding="utf-8", errors="replace").count("-->")
        if cues < MIN_SOURCE_CUES:
            raise ValueError(f"only {cues} cues, so not a full subtitle")
        os.chmod(work, 0o644)
        os.replace(work, output)
    return source_origin(source)


def main(argv):
    command = argv[1] if len(argv) > 1 else ""
    if command == "source" and len(argv) == 4:
        try:
            source = find_source(argv[3], base_lang(argv[2]))
        except OSError:
            return 1
        if not source:
            return 1
        print(source_origin(source))
        return 0
    if command == "extract" and len(argv) == 5:
        try:
            print(extract(argv[3], base_lang(argv[2]), argv[4]))
            return 0
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(exc)
            return 1
    if command != "has" or len(argv) != 4:
        print("usage: subtitle_inventory.py has|source LANG VIDEO, or extract LANG VIDEO OUTPUT", file=sys.stderr)
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
