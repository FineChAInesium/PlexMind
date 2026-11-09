"""
llama.cpp client targeting the local OpenAI-compatible server.
Handles JSON parsing, fence stripping, variable-assignment prefix removal, and retries.
"""
import json
import logging
import os
import re
import tempfile

import httpx

from app import llama_lifecycle

log = logging.getLogger("plexmind.llm")
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

LLAMA_CPP_URL = os.getenv("LLAMA_CPP_URL", "http://localhost:11435")
LLAMA_CPP_MODEL = (
    os.getenv("LLAMA_CPP_MODEL")
    or os.getenv("LLAMA_CPP_MODEL_ALIAS")
    or "gemma-4-12b-it-Q5_K_M"
)
REQUEST_TIMEOUT = 180  # seconds
_log = logging.getLogger("plexmind.llm")


def _data_file(name: str) -> Path:
    return Path(os.getenv("DATA_DIR", "/app/data")) / name


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump(value, stream)
        os.chmod(temporary, 0o644)   # translate.sh reads this as another user
        os.replace(temporary, path)
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        raise


def current_model() -> str:
    """The model last chosen in the dashboard, or the configured default."""
    saved = _read_json(_data_file("llm_model.json"))
    model = saved.get("model") if isinstance(saved, dict) else None
    return model if isinstance(model, str) and model else LLAMA_CPP_MODEL


def save_model(model: str) -> None:
    _write_json(_data_file("llm_model.json"), {"model": model})


async def list_models() -> dict:
    """Models llama.cpp offers. While it is on standby, the list from when it
    last ran, so opening the dashboard never has to wake the GPU."""
    seen_file = _data_file("llm_models_seen.json")
    models, live = None, False
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{LLAMA_CPP_URL.rstrip('/')}/v1/models")
            resp.raise_for_status()
        models = [{"id": m["id"], "loaded": (m.get("status") or {}).get("value") in (None, "loaded")}
                  for m in resp.json().get("data", []) if m.get("id")]
        live = True
        _write_json(seen_file, [m["id"] for m in models])
    except (httpx.HTTPError, ValueError, KeyError, TypeError, OSError):
        models = None
    if models is None:
        seen = _read_json(seen_file)
        models = [{"id": m, "loaded": False} for m in seen] if isinstance(seen, list) else []
    current = current_model()
    ids = [m["id"] for m in models]
    if live and ids and current not in ids and current != LLAMA_CPP_MODEL:
        # The file was removed: fall back rather than fail every request.
        _log.warning("Selected model %s is no longer available; using the default.", current)
        _data_file("llm_model.json").unlink(missing_ok=True)
        current = LLAMA_CPP_MODEL
    if current not in ids:
        models.insert(0, {"id": current, "loaded": False})
    return {"current": current, "default": LLAMA_CPP_MODEL, "live": live, "models": models}
MAX_TOKENS = int(os.getenv("LLAMA_CPP_MAX_TOKENS", "768"))
MAX_RETRIES = 2


def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` or ``` ... ``` fences."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json(text: str) -> str:
    """
    Pull the first valid JSON array or object out of the response,
    handling common LLM quirks:
      - markdown fences
      - thinking tags (<think>...</think>)
      - variable assignments: `result = [...]` or `json_response = {...}`
      - leading prose before the JSON
    """
    # Strip thinking blocks, which some models emit even when asked not to
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    text = _strip_fences(text)

    # Strip variable assignment prefix: `varname = [` or `varname = {`
    text = re.sub(r"^\s*\w+\s*=\s*", "", text).strip()

    # Try direct parse
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass

    # Find the first JSON array (greedy from first `[` to last `]`)
    start = text.find("[")
    end = text.rfind("]")

    # Handle truncated arrays — LLM ran out of tokens before closing `]`
    if start != -1 and (end == -1 or end <= start):
        # Try to close the truncated array
        truncated = text[start:]
        # Keep the complete objects: cut after the last `}` first, so a
        # `null` inside the half-written object after it is not a cut point.
        # Then try after the last `null`, for arrays written without braces.
        last_null = truncated.rfind("null")
        last_brace = truncated.rfind("}")
        cuts = [last_brace + 1 if last_brace != -1 else -1, last_null + 4 if last_null != -1 else -1]
        for cut_at in cuts:
            if cut_at <= 0:
                continue
            closed = truncated[:cut_at].rstrip(", \n\t") + "]"
            try:
                json.loads(closed)
                return closed
            except json.JSONDecodeError:
                pass
        if max(cuts) > 0:
            # Will fall through to repair logic below
            text = truncated[:max(cuts)].rstrip(", \n\t") + "]"
            end = len(text) - 1

    if start != -1 and end > start:
        candidate = text[start:end + 1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

        # Repair: LLM sometimes omits {} braces around array elements.
        # Detect pattern: [ "title": ..., "poster_url": null, "title": ... ]
        inner = candidate.strip("[] \n\t")
        if '"title"' in inner and '{"title"' not in inner:
            log.info("Attempting brace repair on %d-char response", len(inner))
            # Split between objects: each starts with "title":
            # Use a regex that matches the boundary between poster_url:null and next title
            parts = re.split(r'(?:"poster_url"\s*:\s*null)\s*,?\s*(?="title")', inner)
            objects = []
            for i, part in enumerate(parts):
                part = part.strip().rstrip(",").strip()
                if i > 0:
                    # Re-add the poster_url field that was consumed by the split
                    part = '"title"' + part if not part.startswith('"title"') else part
                if i < len(parts) - 1:
                    part = part + ', "poster_url": null'
                if part:
                    obj = "{" + part + "}"
                    objects.append(obj)
            if objects:
                repaired = "[" + ", ".join(objects) + "]"
                try:
                    json.loads(repaired)
                    log.info("Brace repair succeeded — %d objects recovered", len(objects))
                    return repaired
                except json.JSONDecodeError as e:
                    log.warning("Brace repair failed: %s\nFirst 200 chars: %s", e, repaired[:200])

            # Fallback: simpler approach — split on "title" and wrap
            parts2 = re.split(r',?\s*\n\s*(?="title"\s*:)', inner)
            objects2 = []
            for part in parts2:
                part = part.strip()
                while part.endswith(","):
                    part = part[:-1].strip()
                if part:
                    objects2.append("{" + part + "}")
            if objects2:
                repaired2 = "[" + ", ".join(objects2) + "]"
                try:
                    json.loads(repaired2)
                    log.info("Fallback brace repair succeeded — %d objects", len(objects2))
                    return repaired2
                except json.JSONDecodeError as e:
                    log.warning("Fallback repair failed: %s\nFirst 200: %s", e, repaired2[:200])

    # Handle a truncated single object before falling back to retry.
    obj_start = text.find("{")
    obj_end = text.rfind("}")
    if obj_start != -1 and (obj_end == -1 or obj_end <= obj_start):
        truncated = text[obj_start:].rstrip(", \n\t")
        missing = truncated.count("{") - truncated.count("}")
        if missing > 0:
            repaired_object = truncated + ("}" * missing)
            try:
                json.loads(repaired_object)
                return repaired_object
            except json.JSONDecodeError:
                pass

    # Find the first JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidate = text[start:end + 1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    return text


def _is_model_output_error(exc: httpx.HTTPStatusError) -> bool:
    """llama.cpp answers HTTP 500 when its chat parser rejects the model's output.

    That is a bad sample ("does not match the expected peg-native format"),
    not an outage, so it is resampled like unparseable JSON.
    """
    if exc.response.status_code != 500:
        return False
    try:
        message = str(exc.response.json().get("error", {}).get("message", ""))
    except (ValueError, AttributeError):
        return False
    return "peg-native" in message or "does not match the expected" in message


async def generate(prompt: str, system: str | None = None, *,
                   temperature: float = 0.2, seed: int | None = None) -> str:
    """Send a prompt to llama.cpp and return the raw text response."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": "/no_think\n" + prompt})

    payload: dict = {
        "model": current_model(),
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "top_p": 0.9,
        "max_tokens": MAX_TOKENS,
        # Some models think by default; models that do not ignore this setting.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if seed is not None:
        payload["seed"] = seed

    async with llama_lifecycle.in_use(), httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(f"{LLAMA_CPP_URL.rstrip('/')}/v1/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content", "")


async def generate_json(prompt: str, system: str | None = None) -> list | dict:
    """
    Generate a response and parse it as JSON.
    Retries up to MAX_RETRIES times with a stricter reminder if parsing fails.
    """
    last_error: Exception | None = None
    raw = ""

    for attempt in range(1, MAX_RETRIES + 2):
        retry_suffix = ""
        temperature, seed = 0.2, None
        if attempt > 1:
            retry_suffix = (
                '\n\nIMPORTANT: Your previous response could not be parsed as JSON. '
                'Return ONLY a raw JSON array. Each element MUST be wrapped in curly braces {}. '
                'Example: [{"title":"X","year":2024,"type":"movie","reason":"Because...","poster_url":null}]'
            )
            # Resample (new seed, warmer) instead of replaying the same reply.
            temperature, seed = round(min(0.2 + 0.1 * (attempt - 1), 0.5), 1), attempt

        try:
            raw = await generate(prompt + retry_suffix, system=system, temperature=temperature, seed=seed)
        except httpx.HTTPStatusError as exc:
            if not _is_model_output_error(exc):
                raise
            log.warning("llama.cpp could not parse the model output on attempt %d/%d; resampling.",
                        attempt, MAX_RETRIES + 1)
            last_error = exc
            continue
        extracted = _extract_json(raw)
        try:
            return json.loads(extracted)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue

    raise ValueError(
        f"LLM did not return valid JSON after {MAX_RETRIES + 1} attempts.\n"
        f"Last raw response:\n{raw}"
    ) from last_error


async def health_check() -> bool:
    """Return True if llama.cpp is reachable and exposes a model endpoint."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{LLAMA_CPP_URL.rstrip('/')}/v1/models")
            resp.raise_for_status()
            models = [m.get("id", "") for m in resp.json().get("data", [])]
            return not models or current_model() in models
    except Exception:
        return False
