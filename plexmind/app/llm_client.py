"""
Ollama client targeting Gemma 3.
Handles JSON parsing, fence stripping, variable-assignment prefix removal, and retries.
"""
import json
import logging
import os
import re

import httpx

log = logging.getLogger("plexmind.llm")
from dotenv import load_dotenv

load_dotenv()

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:9b")
REQUEST_TIMEOUT = 180  # seconds
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
    # Strip thinking blocks (qwen3.5 sometimes emits these even with think:false)
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
        # Find last complete object (ends with `}` or `null`)
        last_null = truncated.rfind("null")
        last_brace = truncated.rfind("}")
        cut_at = max(last_null + 4 if last_null != -1 else -1,
                     last_brace + 1 if last_brace != -1 else -1)
        if cut_at > 0:
            truncated = truncated[:cut_at].rstrip(", \n\t") + "]"
            try:
                json.loads(truncated)
                return truncated
            except json.JSONDecodeError:
                # Will fall through to repair logic below
                text = truncated
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


async def generate(prompt: str, system: str | None = None) -> str:
    """Send a prompt to Ollama and return the raw text response."""
    payload: dict = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "think": False,   # disable Qwen3.5 thinking mode — we want direct JSON
        "options": {
            "temperature": 0.2,
            "top_p": 0.9,
            "num_predict": 8192,
        },
    }
    if system:
        payload["system"] = system

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
        resp.raise_for_status()
        return resp.json().get("response", "")


async def generate_json(prompt: str, system: str | None = None) -> list | dict:
    """
    Generate a response and parse it as JSON.
    Retries up to MAX_RETRIES times with a stricter reminder if parsing fails.
    """
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 2):
        retry_suffix = ""
        if attempt > 1:
            retry_suffix = (
                '\n\nIMPORTANT: Your previous response could not be parsed as JSON. '
                'Return ONLY a raw JSON array. Each element MUST be wrapped in curly braces {}. '
                'Example: [{"title":"X","year":2024,"type":"movie","reason":"Because...","poster_url":null}]'
            )

        raw = await generate(prompt + retry_suffix, system=system)
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
    """Return True if Ollama is reachable and the configured model is available."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            models = [m["name"] for m in resp.json().get("models", [])]
            base = OLLAMA_MODEL.split(":")[0]
            return any(m.startswith(base) for m in models)
    except Exception:
        return False
