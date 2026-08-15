"""Discover Whisper models that are already present in the local cache."""

import os
from pathlib import Path


_MODEL_ORDER = (
    "tiny", "tiny.en", "base", "base.en", "small", "small.en",
    "medium", "medium.en", "large-v1", "large-v2", "large-v3", "turbo",
)
_MODEL_NAMES = set(_MODEL_ORDER)


def _public_name(cache_name: str) -> str | None:
    name = cache_name.removesuffix(".pt")
    if name == "large-v3-turbo":
        return "turbo"
    return name if name in _MODEL_NAMES else None


def discover(cache_dir: str | Path | None = None) -> dict:
    root = Path(cache_dir or os.getenv("WHISPER_MODEL_CACHE_DIR", "/whisper-cache"))
    found: set[str] = set()
    if root.is_dir():
        for artifact in root.rglob("*.pt"):
            if artifact.stat().st_size < 1024 * 1024:
                continue
            model = _public_name(artifact.name.lower())
            if model:
                found.add(model)
        for artifact in root.rglob("model.bin"):
            if artifact.stat().st_size < 1024 * 1024:
                continue
            for parent in artifact.parents:
                marker = "faster-whisper-"
                if marker in parent.name:
                    model = _public_name(parent.name.split(marker, 1)[1].lower())
                    if model:
                        found.add(model)
                    break
    models = [name for name in _MODEL_ORDER if name in found]
    configured = os.getenv("WHISPER_MODEL", "turbo")
    return {
        "models": models,
        "configured_model": configured if configured in found else None,
        "cache_dir": str(root),
        "cache_available": root.is_dir(),
    }
