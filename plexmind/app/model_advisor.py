"""Read-only, deterministic model/quantization recommendations for llama.cpp."""
from __future__ import annotations

import os
from datetime import datetime, timezone


CATALOG = (
    {
        "id": "gemma-4-12b-q5_k_m",
        "model": "Gemma 4 12B",
        "quantization": "Q5_K_M",
        "weights_gb": 8.41,
        "measured_vram_gb": 8.68,   # on an RTX 3060 with an 8K context
        "provenance": "community-conversion",
        "license": "Apache-2.0",
        "tier": "production",
        "url": "https://huggingface.co/unsloth/gemma-4-12b-it-GGUF",
        "summary": "Default. Fewer translation errors than Qwen 3.5 9B in blind Chinese and Spanish tests, but about 25% slower.",
    },
    {
        "id": "qwen3-8b-q6_k",
        "model": "Qwen3 8B",
        "quantization": "Q6_K",
        "weights_gb": 6.73,
        "provenance": "official",
        "license": "Apache-2.0",
        "tier": "production",
        "url": "https://huggingface.co/Qwen/Qwen3-8B-GGUF",
        "summary": "Best quality/headroom balance for multilingual translation and structured recommendations.",
    },
    {
        "id": "qwen3.5-9b-q5_k_m",
        "model": "Qwen3.5 9B",
        "quantization": "Q5_K_M",
        "weights_gb": 6.6,
        "provenance": "community-conversion",
        "license": "Apache-2.0",
        "tier": "experimental",
        "url": "https://huggingface.co/Qwen/Qwen3.5-9B",
        "summary": "Newer high-quality candidate; benchmark locally and verify the GGUF revision and checksum.",
    },
    {
        "id": "ministral-3-8b-q5_k_m",
        "model": "Ministral 3 8B Instruct 2512",
        "quantization": "Q5_K_M",
        "weights_gb": 6.06,
        "provenance": "official",
        "license": "Apache-2.0",
        "tier": "alternative",
        "url": "https://huggingface.co/mistralai/Ministral-3-8B-Instruct-2512-GGUF",
        "summary": "Official, fast multilingual alternative.",
    },
    {
        "id": "qwen3-8b-q4_k_m",
        "model": "Qwen3 8B",
        "quantization": "Q4_K_M",
        "weights_gb": 5.03,
        "provenance": "official",
        "license": "Apache-2.0",
        "tier": "speed",
        "url": "https://huggingface.co/Qwen/Qwen3-8B-GGUF",
        "summary": "Maximum headroom and speed with a modest quality tradeoff.",
    },
)


def _same_model(catalog_id: str, name: str) -> bool:
    """Router mode names a model after its file: gemma-4-12b-it-Q5_K_M."""
    return catalog_id.lower() == name.lower().replace("-it-", "-")


def recommendations(gpu: dict, context_tokens: int | None = None, active: str | None = None) -> dict:
    """Rank known candidates against conservatively usable VRAM; never mutates runtime."""
    context = context_tokens or int(os.getenv("MODEL_ADVISOR_CONTEXT_TOKENS", "8192"))
    total_mb = gpu.get("memory_total_mb")
    free_mb = gpu.get("memory_free_mb")
    total_gb = round(float(total_mb) / 1024, 2) if total_mb is not None else None
    free_gb = round(float(free_mb) / 1024, 2) if free_mb is not None else None
    # Capacity fit must not fluctuate merely because the active model currently occupies VRAM.
    # Current free VRAM remains visible so the UI can warn about concurrent workloads.
    usable_gb = round(total_gb * 0.80, 2) if total_gb else None

    # Conservative 8K baseline: KV/cache + CUDA buffers scale with context, plus reserve.
    runtime_overhead = 1.35 + (max(context, 1) / 8192) * 0.75
    active_model = active or os.getenv("LLAMA_CPP_MODEL", "unknown")
    ranked = []
    for item in CATALOG:
        # A real measurement beats the estimate, at the context it was measured with.
        measured = item.get("measured_vram_gb") if context == 8192 else None
        # Catalog weights are file sizes in GB; VRAM and the measurement are GiB.
        required = round(measured or item["weights_gb"] * 1e9 / 2**30 + runtime_overhead, 2)
        fit = None if usable_gb is None else required <= usable_gb
        headroom = None if total_gb is None else round(total_gb - required, 2)
        ranked.append({**item, "estimated_vram_gb": required, "headroom_gb": headroom,
                       "fits": fit, "active": _same_model(item["id"], active_model)})

    tier_order = {"production": 0, "experimental": 1, "alternative": 2, "speed": 3}
    ranked.sort(key=lambda x: (not x["active"], x["fits"] is False, tier_order[x["tier"]]))
    return {
        "mode": "advisory-only",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "catalog_updated": "2026-11-09",
        "gpu": {**gpu, "memory_total_gb": total_gb, "memory_free_gb": free_gb, "usable_vram_gb": usable_gb},
        "active_model": active_model,
        "context_tokens": context,
        "whisper_concurrency_warning": "Fit estimates reserve runtime overhead but do not assume concurrent Whisper use.",
        "recommendations": ranked,
    }
