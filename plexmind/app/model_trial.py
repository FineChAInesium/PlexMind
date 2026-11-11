"""Model Trial: try a GGUF on this server's GPU before relying on it.

A trial loads the model through llama.cpp's router and runs PlexMind's real
work on a fixed sample: the subtitle batches translate.sh sends (same
prompts, same JSON schema, judged by translation_cue.py's own checks) for up
to two target languages, plus one recommendation request built by the real
recommender prompt from a fixed history. It records

  fit       GPU memory in use with the model loaded, against the card's total
  speed     generated tokens per second, from llama.cpp's own timings
  breakage  replies translate.sh would throw away (broken JSON, wrong item
            count, shifted lines, English left in), Simplified characters in
            Traditional Chinese, idioms translated word for word, and
            recommendation replies that are not JSON, invent titles or skip
            the "Because you watched" reason

and gives a verdict. A model that fails fit, speed or breakage cannot be
chosen in the dashboard. The current model is run on the same sample, so the
two can be compared side by side. Naturalness is not scored: that still needs
a person reading the output.
"""
from __future__ import annotations

import asyncio
import fcntl
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

from app import llama_lifecycle, llm_client, recommender, scheduler

FIT_MAX_FRACTION = float(os.getenv("TRIAL_FIT_MAX_FRACTION", "0.92"))
MIN_TOKENS_PER_SECOND = float(os.getenv("TRIAL_MIN_TOKENS_PER_SECOND", "4"))
MAX_CUE_REJECT_FRACTION = 0.10
MIN_BATCH_OK_FRACTION = 0.75
MAX_LANGUAGES = 2
REQUEST_TIMEOUT = 600
LOCK_WAIT_SECONDS = 150


def _translation_cue():
    """scripts/translation_cue.py: /app in the image, ../scripts in a checkout."""
    try:
        import translation_cue  # type: ignore
        return translation_cue
    except ImportError:
        path = Path(__file__).resolve().parents[2] / "scripts" / "translation_cue.py"
        spec = importlib.util.spec_from_file_location("translation_cue", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules["translation_cue"] = module
        return module


# --- the fixed sample ------------------------------------------------------
# Original lines written for this test. Each batch is sent as one request,
# like translate.sh does; several sentences run across cues on purpose.
BATCHES = [
    [
        "Break a leg out there tonight.",
        "Thanks. I'm so nervous\nI could throw up.",
        "Relax, you've got this in the bag.",
        "Says the guy who froze\nat his own wedding.",
        "<i>That was one time.</i>",
        "Okay, quit pulling my leg\nand help me with this tie.",
        "Hold still, you big baby.",
        "There. You look like a million bucks.",
    ],
    [
        "Look at this place. It's a dump.",
        "The landlord said it had character.",
        "Yeah, the character of a crime scene.",
        "Who in their right mind",
        "pays two grand a month",
        "for a closet with a sink?",
        "Me, apparently.",
        "Don't spill the beans to Mom, okay?",
    ],
    [
        "What the hell were you thinking?",
        "I wasn't. That's kind of my whole thing.",
        "You totally screwed us, man.",
        "Chill. I'll fix it.",
        "Like you fixed the car?\nIt's still on bricks!",
        "It's raining cats and dogs out there.",
        "Great. Perfect. Just peachy.",
        "♪ Nobody knows the trouble I've seen ♪",
    ],
    [
        "Agent Morales, you have a call on line two.",
        "Tell Dr. Whitfield I'll be there in five.",
        "She says it can't wait.",
        "Fine. Put her through.",
        "Morales, it's Whitfield. We have a problem.",
        "The samples from Building 9",
        "are gone. All of them.",
        "Lock it down. Nobody leaves.",
    ],
]

# Word-for-word renderings of the idioms above: their meaning is good luck,
# joking, telling a secret and heavy rain, not legs, beans, cats or dogs.
LITERAL_IDIOMS = {
    "zh": {
        "Break a leg": ("斷腿", "斷一條腿", "摔斷"),
        "pulling my leg": ("拉我的腿", "扯我的腿", "拉腿", "扯腿"),
        "spill the beans": ("豆子", "豆"),
        "cats and dogs": ("貓", "狗"),
    },
    "es": {
        "Break a leg": ("una pierna",),
        "pulling my leg": ("jalando la pierna", "jalar la pierna", "tirando de la pierna", "tirar de la pierna"),
        "spill the beans": ("frijoles", "judías", "habas"),
        "cats and dogs": ("gatos y perros",),
    },
}

# Characters only Simplified Chinese uses; Traditional output should have none.
SIMPLIFIED_ONLY = set("这说们个来时会为对没还么吗过让与当从现发经开关实见认问题样应该头两进种万东车门长话爱钱给里后觉气无谁听写飞")

# The prompts translate.sh sends (tests keep these identical to translate.sh).
BATCH_SYSTEM_PROMPTS = {
    "zh": "你是專業字幕翻譯員。輸入是一個 JSON 字串陣列，每個元素是一條英文字幕。將每個元素翻譯為精簡的繁體中文，只輸出一個 JSON 字串陣列：元素數量和順序與輸入完全相同，第 N 個元素只包含第 N 條字幕的譯文。俚語、雙關語和粗話要按真正的意思，譯成中文裡自然的說法，絕不逐字直譯。保留換行和 <i> 等格式標籤；每個元素只翻譯該元素本身的英文；即使句子延續到下一個元素，也絕不可把文字移到相鄰元素。不要合併、拆分或省略字幕，不要輸出編號、時間戳、解釋或 Markdown。",
    "es-MX": "Eres traductor profesional de subtítulos. La entrada es un arreglo JSON de cadenas; cada elemento es un subtítulo en inglés. Traduce cada elemento al español de México de forma breve y devuelve solamente un arreglo JSON de cadenas con la misma cantidad de elementos y en el mismo orden: el elemento N contiene solo la traducción del subtítulo N. Traduce la jerga, los dobles sentidos y las groserías por su significado real, con un equivalente natural en México; nunca palabra por palabra. Conserva los saltos de línea y las etiquetas de formato como <i>. Cada elemento traduce solo su propio texto: aunque la oración continúe en el siguiente, nunca muevas palabras a otro elemento. No combines, dividas ni omitas subtítulos, y no agregues números, marcas de tiempo, explicaciones ni Markdown.",
}
GENERIC_BATCH_PROMPT = "The input is a JSON array of strings, each one subtitle cue. Translate every element to {lang} and output only a JSON array of strings with the same number of elements in the same order: element N holds only the translation of cue N. Translate slang, innuendo and profanity by their real meaning, using a natural equivalent; never word for word. Keep line breaks and formatting tags such as <i>. Each element translates only its own text: even if a sentence continues into the next element, never move words to a neighbouring element. Do not merge, split, or drop cues, and do not add cue numbers, timestamps, explanations, or Markdown."
ZH_USER_MESSAGE = "將以下英文字幕翻譯成繁體中文，只輸出譯文（JSON 字串陣列，共 {count} 個元素）：\n{sources}"


def batch_system_prompt(lang: str) -> str:
    return BATCH_SYSTEM_PROMPTS.get(lang) or GENERIC_BATCH_PROMPT.format(lang=lang)


def batch_user_message(lang: str, sources: list[str]) -> str:
    text = json.dumps(sources, ensure_ascii=False)
    return ZH_USER_MESSAGE.format(count=len(sources), sources=text) if lang == "zh" else text


# A fixed viewer and library for the recommendation request.
HISTORY = [
    {"title": "The Office", "year": 2005, "media_type": "show", "genres": ["Comedy"], "keywords": ["workplace", "mockumentary"]},
    {"title": "Parks and Recreation", "year": 2009, "media_type": "show", "genres": ["Comedy"], "keywords": ["small town", "mockumentary"]},
    {"title": "Alien", "year": 1979, "media_type": "movie", "genres": ["Horror", "Science Fiction"], "keywords": ["space", "creature"]},
    {"title": "The Thing", "year": 1982, "media_type": "movie", "genres": ["Horror", "Science Fiction"], "keywords": ["paranoia", "antarctica"]},
    {"title": "Planet Earth", "year": 2006, "media_type": "show", "genres": ["Documentary"], "keywords": ["nature", "wildlife"]},
]
CANDIDATES = [
    {"title": "Brooklyn Nine-Nine", "year": 2013, "media_type": "tv", "genres": ["Comedy", "Crime"], "_matches": ["The Office", "Parks and Recreation"]},
    {"title": "Superstore", "year": 2015, "media_type": "tv", "genres": ["Comedy"], "_matches": ["The Office", "Parks and Recreation"]},
    {"title": "Abbott Elementary", "year": 2021, "media_type": "tv", "genres": ["Comedy"], "_matches": ["The Office"]},
    {"title": "Aliens", "year": 1986, "media_type": "movie", "genres": ["Action", "Science Fiction"], "_matches": ["Alien", "The Thing"]},
    {"title": "Event Horizon", "year": 1997, "media_type": "movie", "genres": ["Horror", "Science Fiction"], "_matches": ["Alien", "The Thing"]},
    {"title": "Annihilation", "year": 2018, "media_type": "movie", "genres": ["Science Fiction", "Horror"], "_matches": ["The Thing", "Alien"]},
    {"title": "Blue Planet II", "year": 2017, "media_type": "tv", "genres": ["Documentary"], "_matches": ["Planet Earth"]},
    {"title": "Our Planet", "year": 2019, "media_type": "tv", "genres": ["Documentary"], "_matches": ["Planet Earth"]},
    {"title": "The Notebook", "year": 2004, "media_type": "movie", "genres": ["Romance", "Drama"], "_matches": []},
    {"title": "Fast & Furious 6", "year": 2013, "media_type": "movie", "genres": ["Action"], "_matches": []},
]
REC_COUNT = 5


def recommendation_prompt() -> str:
    history_text = "\n".join(recommender._fmt_meta(item) for item in HISTORY)
    candidates_text = "\n".join(recommender._fmt_candidate(item) for item in CANDIDATES)
    return recommender._build_prompt(history_text, candidates_text, "No feedback recorded yet.", REC_COUNT,
                                     top_genres="Comedy, Horror, Science Fiction, Documentary")


# --- scoring ---------------------------------------------------------------

def literal_idioms(lang: str, sources: list[str], items: list[str]) -> list[str]:
    table = LITERAL_IDIOMS.get("zh" if lang == "zh" else "es" if lang.startswith("es") else "", {})
    found = []
    for source, item in zip(sources, items):
        for idiom, giveaways in table.items():
            if idiom.casefold() in source.casefold() and any(g.casefold() in item.casefold() for g in giveaways):
                found.append(idiom)
    return found


def score_batch(lang: str, sources: list[str], content: str) -> dict:
    cue = _translation_cue()
    items, reasons = cue.validate_batch(sources, content, lang)
    if items is None:
        return {"ok": False, "reason": reasons, "rejected": len(sources), "items": [], "literal": [], "simplified": 0}
    rejected = [r for r in reasons if r]
    simplified = sum(ch in SIMPLIFIED_ONLY for item in items for ch in item) if lang == "zh" else 0
    return {"ok": True, "reason": None, "rejected": len(rejected), "reasons": rejected, "items": items,
            "literal": literal_idioms(lang, sources, items), "simplified": simplified}


def score_recommendations(raw: str) -> dict:
    """Same parsing the recommender uses, then the rules its prompt sets."""
    try:
        picks = json.loads(llm_client._extract_json(raw))
    except (ValueError, TypeError):
        return {"ok": False, "reason": "not JSON", "picks": []}
    if isinstance(picks, dict):
        picks = picks.get("recommendations") or [picks]
    if not isinstance(picks, list) or not all(isinstance(p, dict) for p in picks):
        return {"ok": False, "reason": "not a list of objects", "picks": []}
    library = {c["title"].casefold() for c in CANDIDATES}
    titles = [str(p.get("title", "")) for p in picks]
    invented = [t for t in titles if t.casefold() not in library]
    no_reason = [t for t, p in zip(titles, picks) if not str(p.get("reason", "")).startswith("Because you watched")]
    dupes = len(titles) - len({t.casefold() for t in titles})
    problems = []
    if len(picks) != REC_COUNT:
        problems.append(f"{len(picks)} picks instead of {REC_COUNT}")
    if invented:
        problems.append("invented: " + ", ".join(invented))
    if dupes:
        problems.append(f"{dupes} duplicate picks")
    if no_reason:
        problems.append(f"{len(no_reason)} without a 'Because you watched' reason")
    return {"ok": not invented and not dupes and len(picks) >= REC_COUNT - 1,
            "reason": "; ".join(problems) or None, "picks": titles}


def verdict(result: dict) -> dict:
    """pass / fail with the reasons, from a finished model run."""
    failures, warnings = [], []
    if result.get("load_error"):
        failures.append(f"Did not load: {result['load_error']}")
    fit = result.get("fit") or {}
    if fit.get("fraction") is not None and fit["fraction"] > FIT_MAX_FRACTION:
        failures.append(f"Uses {fit['fraction']:.0%} of GPU memory (limit {FIT_MAX_FRACTION:.0%})")
    elif fit.get("fraction") is None and not result.get("load_error"):
        warnings.append("GPU memory could not be read")
    speed = result.get("tokens_per_second")
    if speed is not None and speed < MIN_TOKENS_PER_SECOND:
        failures.append(f"Too slow: {speed:.1f} tokens/s (minimum {MIN_TOKENS_PER_SECOND:g})")
    for lang, t in (result.get("translation") or {}).items():
        if t["batches"] and t["batches_ok"] / t["batches"] < MIN_BATCH_OK_FRACTION:
            failures.append(f"{lang}: {t['batches'] - t['batches_ok']} of {t['batches']} batches unusable")
        if t["cues"] and t["rejected"] / t["cues"] > MAX_CUE_REJECT_FRACTION:
            failures.append(f"{lang}: {t['rejected']} of {t['cues']} lines rejected")
        elif t["rejected"]:
            warnings.append(f"{lang}: {t['rejected']} of {t['cues']} lines rejected")
        if t["literal"]:
            warnings.append(f"{lang}: idioms translated word for word: {', '.join(sorted(set(t['literal'])))}")
        if t.get("simplified", 0) > 3:
            warnings.append(f"{lang}: {t['simplified']} Simplified characters in Traditional Chinese")
    recs = result.get("recommendations")
    if recs and not recs["ok"]:
        failures.append(f"Recommendations: {recs['reason']}")
    elif recs and recs.get("reason"):
        warnings.append(f"Recommendations: {recs['reason']}")
    return {"pass": not failures, "failures": failures, "warnings": warnings}


# --- running ---------------------------------------------------------------

def _trials_file() -> Path:
    return Path(os.getenv("DATA_DIR", "/app/data")) / "model_trials.json"


def saved_trials() -> dict:
    try:
        data = json.loads(_trials_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_trial(model: str, result: dict) -> None:
    data = saved_trials()
    data[model] = result
    llm_client._write_json(_trials_file(), data)


def failed(model: str) -> list[str]:
    """Why MODEL may not be chosen: its last trial's failures, else none."""
    trial = saved_trials().get(model) or {}
    return list((trial.get("verdict") or {}).get("failures") or [])


STATE: dict = {"status": "idle"}


def target_languages() -> list[str]:
    langs = [l.strip() for l in os.getenv("TARGET_LANGUAGES", "zh,es-MX").split(",") if l.strip()]
    return langs[:MAX_LANGUAGES] or ["zh"]


async def _chat(client: httpx.AsyncClient, payload: dict) -> tuple[str, dict, float]:
    started = time.monotonic()
    url = f"{llm_client.LLAMA_CPP_URL.rstrip('/')}/v1/chat/completions"
    resp = await client.post(url, json=payload)
    elapsed = time.monotonic() - started
    if resp.status_code == 500 and llm_client._is_model_output_error(httpx.HTTPStatusError("", request=resp.request, response=resp)):
        return "", {}, elapsed
    resp.raise_for_status()
    data = resp.json()
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    return content, data.get("timings") or {}, elapsed


def _gpu_memory() -> dict:
    info, _error = scheduler._nvidia_info_from_broker()
    if not info or not info.get("memory_total_mb"):
        return {"used_mb": None, "total_mb": None, "fraction": None}
    total = float(info["memory_total_mb"])
    used = total - float(info.get("memory_free_mb") or 0)
    return {"used_mb": round(used), "total_mb": round(total), "fraction": round(used / total, 3)}


async def run_model(model: str, langs: list[str], progress=lambda step: None) -> dict:
    """Run the sample on MODEL; llama.cpp's router loads it on the first request."""
    result: dict = {"model": model, "load_error": None, "fit": None, "tokens_per_second": None,
                    "first_reply_seconds": None, "translation": {}, "recommendations": None, "samples": {}}
    generated, gen_seconds = 0, 0.0

    def count(timings: dict) -> None:
        nonlocal generated, gen_seconds
        n, per_s = timings.get("predicted_n"), timings.get("predicted_per_second")
        if n and per_s:
            generated += n
            gen_seconds += n / per_s

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for lang in langs:
            totals = {"batches": 0, "batches_ok": 0, "cues": 0, "rejected": 0, "literal": [], "simplified": 0, "problems": []}
            for index, sources in enumerate(BATCHES, 1):
                progress(f"{model}: translating to {lang}, batch {index} of {len(BATCHES)}")
                payload = {
                    "model": model, "stream": False, "temperature": 0.1, "max_tokens": 256 * len(sources),
                    "chat_template_kwargs": {"enable_thinking": False},
                    "response_format": {"type": "json_schema", "json_schema": {"name": "subtitle_translations",
                        "schema": {"type": "array", "items": {"type": "string"},
                                   "minItems": len(sources), "maxItems": len(sources)}}},
                    "messages": [{"role": "system", "content": batch_system_prompt(lang)},
                                 {"role": "user", "content": batch_user_message(lang, sources)}],
                }
                try:
                    content, timings, elapsed = await _chat(client, payload)
                except httpx.HTTPError as exc:
                    if result["first_reply_seconds"] is None:
                        result["load_error"] = _http_error(exc)
                        return result
                    content, timings, elapsed = "", {}, 0.0
                if result["first_reply_seconds"] is None:
                    result["first_reply_seconds"] = round(elapsed, 1)
                    result["fit"] = _gpu_memory()
                count(timings)
                scored = score_batch(lang, sources, content)
                totals["batches"] += 1
                totals["batches_ok"] += scored["ok"]
                totals["cues"] += len(sources)
                totals["rejected"] += scored["rejected"]
                totals["literal"] += scored["literal"]
                totals["simplified"] += scored["simplified"]
                if not scored["ok"]:
                    totals["problems"].append(f"batch {index}: {scored['reason']}")
                if scored["items"]:
                    result["samples"].setdefault(lang, []).extend(
                        {"en": s, "out": o} for s, o in zip(sources, scored["items"]))
            result["translation"][lang] = totals

        progress(f"{model}: recommendations")
        payload = {
            "model": model, "stream": False, "temperature": 0.2, "top_p": 0.9, "max_tokens": llm_client.MAX_TOKENS,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": recommender.SYSTEM_PROMPT},
                         {"role": "user", "content": "/no_think\n" + recommendation_prompt()}],
        }
        try:
            content, timings, _elapsed = await _chat(client, payload)
            count(timings)
            result["recommendations"] = score_recommendations(content)
        except httpx.HTTPError as exc:
            result["recommendations"] = {"ok": False, "reason": _http_error(exc), "picks": []}

    if generated and gen_seconds:
        result["tokens_per_second"] = round(generated / gen_seconds, 1)
    latest = _gpu_memory()
    if latest["fraction"] is not None and (not result["fit"] or result["fit"]["fraction"] is None
                                           or latest["fraction"] > result["fit"]["fraction"]):
        result["fit"] = latest
    return result


def _http_error(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            message = exc.response.json().get("error", {}).get("message", "")
        except ValueError:
            message = ""
        return f"HTTP {exc.response.status_code}{': ' + str(message)[:200] if message else ''}"
    return type(exc).__name__


async def _gpu_lock():
    """The lock translation, transcription and the recommendation batch share."""
    path = Path(os.getenv("DATA_DIR", "/app/data")) / "plexmind_gpu.lock"
    handle = os.fdopen(os.open(path, os.O_RDONLY | os.O_CREAT, 0o644))
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if time.monotonic() > deadline:
                handle.close()
                raise RuntimeError("The GPU is busy (transcription or a recommendation batch); try again when it finishes")
            await asyncio.sleep(3)


async def run_trial(model: str, current: str, pause_translation=None, resume_translation=None) -> dict:
    """The whole trial: lock the GPU, run MODEL and the current model, save."""
    def progress(step: str) -> None:
        STATE["step"] = step

    STATE.update(status="running", model=model, step="Waiting for the GPU", started=time.time(), result=None, error=None)
    paused = False
    lock = None
    try:
        if pause_translation:
            paused = await pause_translation()
        lock = await _gpu_lock()
        langs = target_languages()
        async with llama_lifecycle.in_use():
            candidate = await run_model(model, langs, progress)
            baseline = None
            if current and current != model:
                baseline = await run_model(current, langs, progress)
        candidate["verdict"] = verdict(candidate)
        candidate["tested_at"] = time.time()
        if baseline:
            baseline["verdict"] = verdict(baseline)
            baseline["tested_at"] = candidate["tested_at"]
            _save_trial(current, baseline)
        _save_trial(model, candidate)
        result = {"model": candidate, "current": baseline}
        STATE.update(status="done", step="Finished", result=result)
        return result
    except Exception as exc:  # reported to the dashboard, never raised into the event loop
        STATE.update(status="error", error=str(exc) or type(exc).__name__)
        return {}
    finally:
        if lock:
            lock.close()
        if paused and resume_translation:
            try:
                await resume_translation()
            except Exception:
                STATE["error"] = (STATE.get("error") or "") + " (translation could not be restarted; start it from the dashboard)"
