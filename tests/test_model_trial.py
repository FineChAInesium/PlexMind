import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import llm_client, main, model_trial

ROOT = Path(__file__).resolve().parents[1]


def _translate_sh_prompt(function: str, lang: str) -> str:
    script = (ROOT / "scripts" / "translate.sh").read_text(encoding="utf-8")
    body = script.split(f"{function}() {{", 1)[1].split("\n}", 1)[0]
    pattern = r'\*\)' if lang == "*" else re.escape(f'"{lang}")')
    return re.search(pattern + r'\s+echo "(.*?)" ;;', body).group(1)


def test_trial_prompts_are_the_ones_translate_sh_sends():
    for lang in ("zh", "es-MX"):
        assert model_trial.batch_system_prompt(lang) == _translate_sh_prompt("get_batch_system_prompt", lang)
    generic = _translate_sh_prompt("get_batch_system_prompt", "*").replace("$lang", "fr")
    assert model_trial.batch_system_prompt("fr") == generic


def test_trial_batches_fit_the_translation_batch_size():
    assert all(len(batch) == 8 for batch in model_trial.BATCHES)
    assert all(model_trial.recommendation_prompt().count(c["title"]) >= 1 for c in model_trial.CANDIDATES)


def test_a_clean_traditional_chinese_batch_scores_clean():
    sources = ["Hold still, you big baby.", "There. You look like a million bucks."]
    content = json.dumps(["別動，你這個愛哭鬼。", "好了。你看起來帥呆了。"], ensure_ascii=False)
    scored = model_trial.score_batch("zh", sources, content)
    assert scored["ok"] and scored["rejected"] == 0 and scored["literal"] == [] and scored["simplified"] == 0


def test_word_for_word_idioms_and_simplified_characters_are_caught():
    sources = ["Break a leg out there tonight.", "It's raining cats and dogs out there."]
    content = json.dumps(["今晚去外面斷腿吧。", "外面下着猫和狗，这样不行。"], ensure_ascii=False)
    scored = model_trial.score_batch("zh", sources, content)
    assert set(scored["literal"]) == {"Break a leg", "cats and dogs"}
    assert scored["simplified"] >= 2
    es = model_trial.score_batch("es-MX", sources, json.dumps(["Rómpete una pierna esta noche.", "Llueve a cántaros allá afuera."]))
    assert es["literal"] == ["Break a leg"]


def test_a_reply_with_the_wrong_number_of_items_is_unusable():
    scored = model_trial.score_batch("es-MX", ["One.", "Two."], json.dumps(["Uno."]))
    assert not scored["ok"] and scored["rejected"] == 2


def test_recommendations_must_come_from_the_library():
    good = [{"title": c["title"], "reason": "Because you watched Alien, ..."} for c in model_trial.CANDIDATES[:5]]
    assert model_trial.score_recommendations(json.dumps(good)) == {"ok": True, "reason": None, "picks": [g["title"] for g in good]}
    invented = good[:4] + [{"title": "Galaxy Quest 2", "reason": "Because you watched Alien"}]
    scored = model_trial.score_recommendations(json.dumps(invented))
    assert not scored["ok"] and "Galaxy Quest 2" in scored["reason"]
    assert not model_trial.score_recommendations("I recommend Aliens!")["ok"]


def _run(**overrides):
    base = {"load_error": None, "fit": {"fraction": 0.74}, "tokens_per_second": 30.0,
            "translation": {"zh": {"batches": 4, "batches_ok": 4, "cues": 32, "rejected": 0, "literal": [], "simplified": 0}},
            "recommendations": {"ok": True, "reason": None, "picks": []}}
    base.update(overrides)
    return base


def test_verdict_passes_a_good_run_and_fails_each_kind_of_problem():
    assert model_trial.verdict(_run())["pass"]
    assert not model_trial.verdict(_run(fit={"fraction": 0.97}))["pass"]
    assert not model_trial.verdict(_run(tokens_per_second=2.0))["pass"]
    assert not model_trial.verdict(_run(load_error="HTTP 500: out of memory"))["pass"]
    broken = {"zh": {"batches": 4, "batches_ok": 2, "cues": 32, "rejected": 16, "literal": [], "simplified": 0}}
    assert not model_trial.verdict(_run(translation=broken))["pass"]
    assert not model_trial.verdict(_run(recommendations={"ok": False, "reason": "not JSON", "picks": []}))["pass"]
    literal = {"zh": {"batches": 4, "batches_ok": 4, "cues": 32, "rejected": 1, "literal": ["Break a leg"], "simplified": 0}}
    judged = model_trial.verdict(_run(translation=literal))
    assert judged["pass"] and any("word for word" in w for w in judged["warnings"])


def test_run_model_uses_the_named_model_and_reads_llama_timings():
    sent = []

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    async def fake_post(_self, _url, json=None):
        sent.append(json)
        if "response_format" in json:
            user = json["messages"][1]["content"]
            sources = loads(user[user.index("["):])
            if "繁體" in json["messages"][0]["content"]:
                content = json_dumps(["中" * max(2, int(len(s) * 0.4)) for s in sources])
            else:
                content = json_dumps([" ".join(["palabra"] * max(1, len(s) // 8)) for s in sources])
        else:
            content = json_dumps([{"title": c["title"], "reason": "Because you watched Alien"} for c in model_trial.CANDIDATES[:5]])
        return FakeResponse({"choices": [{"message": {"content": content}}],
                             "timings": {"predicted_n": 100, "predicted_per_second": 25.0}})

    json_dumps = lambda value: json.dumps(value, ensure_ascii=False)
    loads = json.loads
    with patch("httpx.AsyncClient.post", fake_post), \
            patch.object(model_trial, "_gpu_memory", lambda: {"used_mb": 9000, "total_mb": 12288, "fraction": 0.732}):
        result = asyncio.run(model_trial.run_model("candidate-Q4", ["zh", "es-MX"]))
    assert {payload["model"] for payload in sent} == {"candidate-Q4"}
    assert len(sent) == 2 * len(model_trial.BATCHES) + 1
    assert result["tokens_per_second"] == 25.0
    assert result["translation"]["zh"]["batches_ok"] == len(model_trial.BATCHES)
    assert result["recommendations"]["ok"]
    assert model_trial.verdict(result)["pass"]


def test_a_model_that_failed_its_trial_cannot_be_selected():
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"DATA_DIR": tmp}), \
            patch.object(main, "_API_KEY", "k"):
        (Path(tmp) / "model_trials.json").write_text(json.dumps({
            "big-Q8": {"verdict": {"pass": False, "failures": ["Uses 98% of GPU memory (limit 92%)"]}},
            "small-Q4": {"verdict": {"pass": True, "failures": []}},
        }))

        async def models():
            return {"current": "small-Q4", "default": "small-Q4", "live": True,
                    "models": [{"id": "big-Q8", "loaded": False}, {"id": "small-Q4", "loaded": True}]}

        with patch.object(llm_client, "list_models", models):
            client = TestClient(main.app)
            refused = client.post("/api/models", json={"model": "big-Q8"}, headers={"X-API-Key": "k"})
            assert refused.status_code == 409 and "98%" in refused.json()["detail"]
            assert client.post("/api/models", json={"model": "small-Q4"}, headers={"X-API-Key": "k"}).status_code == 200
