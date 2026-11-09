from plexmind.app import model_advisor


def test_3060_prefers_the_default_gemma(monkeypatch):
    monkeypatch.setenv("LLAMA_CPP_MODEL", "qwen3-4b-q4_k_m")
    result = model_advisor.recommendations({
        "vendor": "nvidia", "name": "NVIDIA GeForce RTX 3060",
        "pct": 2, "memory_total_mb": 12288, "memory_free_mb": 11500,
    }, 8192)
    assert result["mode"] == "advisory-only"
    assert result["recommendations"][0]["id"] == "gemma-4-12b-q5_k_m"
    assert result["recommendations"][0]["fits"] is True
    assert result["recommendations"][0]["estimated_vram_gb"] == 8.68
    assert result["recommendations"][1]["id"] == "qwen3-8b-q6_k"


def test_a_measured_size_only_applies_at_the_context_it_was_measured(monkeypatch):
    monkeypatch.setenv("LLAMA_CPP_MODEL", "x")
    result = model_advisor.recommendations({"vendor": "nvidia", "memory_total_mb": 12288}, 16384)
    gemma = next(r for r in result["recommendations"] if r["id"] == "gemma-4-12b-q5_k_m")
    assert gemma["estimated_vram_gb"] > 8.68


def test_active_model_is_shown_first(monkeypatch):
    monkeypatch.setenv("LLAMA_CPP_MODEL", "qwen3.5-9b-q5_k_m")
    result = model_advisor.recommendations({"vendor": "nvidia", "memory_total_mb": 12288}, 8192)
    assert result["recommendations"][0]["id"] == "qwen3.5-9b-q5_k_m"
    assert result["recommendations"][0]["active"] is True


def test_unknown_vram_reports_unknown_fit():
    result = model_advisor.recommendations({"vendor": None, "pct": None}, 8192)
    assert all(item["fits"] is None for item in result["recommendations"])
