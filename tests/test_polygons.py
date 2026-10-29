"""Полигоны: сценарная модель портфеля по УГТ, проверка гипотез, хранилище,
HTTP-слой и ИИ-фокус-группа (LLM подменяется заглушкой)."""

import asyncio
import json

import pytest

from api.runtime import polygons, projects
from api.runtime.config import get_settings

ADMIN_LOGIN = "a.ponomareva@csr-nw.ru"
ADMIN_PASSWORD = "1234"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    from api.routers import auth as auth_router

    monkeypatch.setenv("AIOS_ADMIN_LOGIN", ADMIN_LOGIN)
    monkeypatch.setenv("AIOS_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    caches = (get_settings, polygons.get_store, projects.get_store, auth_router.get_user_store,
              auth_router.get_token_store, auth_router.get_confirmation_store)
    for c in caches:
        c.cache_clear()
    yield
    for c in caches:
        c.cache_clear()


def _client():
    from fastapi.testclient import TestClient

    from api.main import app

    return TestClient(app)


def _token(client):
    resp = client.post("/api/auth/login", json={"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['token']}"}


# ---------- модель ----------


def test_simulation_is_deterministic_and_scenarios_differ():
    params = {"runs": 100, "horizon_years": 4, "initial": {"3": 10, "4": 10}, "intake_per_year": 5}
    scenarios = [{"id": "boost", "name": "Студия", "valley_boost": 80}]
    a = polygons.simulate(params, scenarios)
    b = polygons.simulate(params, scenarios)
    assert a["results"] == b["results"]  # тот же seed — тот же результат
    base, boost = a["results"]
    assert base["id"] == "base" and boost["id"] == "boost"
    assert boost["final"]["exits"]["mean"] > base["final"]["exits"]["mean"]
    assert len(base["series"]["startups"]) == 4
    s = base["final"]["cost"]
    assert s["p10"] <= s["mean"] <= s["p90"]


def test_hypothesis_probability():
    params = {"runs": 60, "initial": {"6": 20}, "exit_level": 7, "p_license": 0}
    sure = polygons.simulate(params, [], {"statement": "x", "metric": "startups", "op": ">=", "value": 1})
    never = polygons.simulate(params, [], {"statement": "x", "metric": "startups", "op": ">=", "value": 10_000})
    assert sure["results"][0]["hypothesis_probability"] == 1.0
    assert never["results"][0]["hypothesis_probability"] == 0.0


def test_budget_limits_progress():
    params = {"runs": 80, "initial": {"4": 30}, "levels": {"4": {"p_up": 0.6, "p_drop": 0, "cost": 1000}}}
    free = polygons.simulate(params)["results"][0]["final"]["exits"]["mean"]
    tight = polygons.simulate({**params, "annual_budget": 3000})["results"][0]["final"]["exits"]["mean"]
    assert tight < free


def test_params_are_clamped():
    p = polygons.normalize_params({"runs": 10**9, "horizon_years": -5, "levels": {"3": {"p_up": 0.9, "p_drop": 0.9}}})
    assert p["runs"] == polygons.MAX_RUNS and p["horizon_years"] == 1
    assert abs(p["levels"]["3"]["p_up"] + p["levels"]["3"]["p_drop"] - 1) < 1e-9
    with pytest.raises(polygons.PolygonError):
        polygons.normalize_params({"initial": {"1": 300, "2": 300}})


def test_examples_are_valid_and_run():
    examples = polygons.get_store().examples()
    assert {e["id"] for e in examples} >= {"pg-1a0e5c0201", "pg-1a0e5c0202"}
    for ex in examples:
        sim = polygons.simulate(ex["params"], ex["scenarios"], ex["hypothesis"])
        assert all(r["hypothesis_probability"] is not None for r in sim["results"])


def test_parse_opinion():
    role = {"id": "investor", "name": "Инвестор", "persona": ""}
    good = polygons.parse_opinion('Вот: {"score": 12, "verdict": "Поддержать", "pros": ["команда"], "cons": "рынок"}', role)
    assert good["score"] == 10 and good["verdict"] == "поддержать" and good["cons"] == ["рынок"]
    bad = polygons.parse_opinion("просто текст", role)
    assert bad["score"] is None and bad["text"] == "просто текст"
    summary = polygons.summarize_panel([good, {**good, "verdict": "доработать", "score": 6}])
    assert summary["average_score"] == 8.0


# ---------- HTTP ----------


def test_api_list_simulate_save_delete():
    client = _client()
    data = client.get("/api/polygons").json()
    assert data["items"] == [] and len(data["examples"]) >= 2
    ex = data["examples"][0]
    sim = client.post("/api/polygons/simulate", json={"params": ex["params"], "scenarios": ex["scenarios"],
                                                      "hypothesis": ex["hypothesis"]})
    assert sim.status_code == 200 and len(sim.json()["results"]) == len(ex["scenarios"]) + 1

    assert client.post("/api/polygons", json=ex).status_code == 401
    h = _token(client)
    saved = client.post("/api/polygons", json=ex, headers=h).json()
    assert saved["id"] != ex["id"] and saved["name"] == ex["name"]  # правка примера — свой полигон
    saved2 = client.post("/api/polygons", json={**saved, "name": "Мой"}, headers=h).json()
    assert saved2["id"] == saved["id"] and saved2["name"] == "Мой"
    assert [i["id"] for i in client.get("/api/polygons", headers=h).json()["items"]] == [saved["id"]]
    assert client.get(f"/api/polygons/{saved['id']}", headers=h).status_code == 200
    assert client.delete(f"/api/polygons/{saved['id']}", headers=h).status_code == 200
    assert client.get(f"/api/polygons/{saved['id']}", headers=h).status_code == 404


def test_panel_requires_llm(monkeypatch):
    from api.routers import chat

    monkeypatch.setattr(chat, "get_provider", lambda *a, **k: None)
    client = _client()
    h = _token(client)
    resp = client.post("/api/polygons/panel", json={"idea": "Запустить стартап-студию в вузе"}, headers=h)
    assert resp.status_code == 503


def test_panel_with_stub_provider(monkeypatch):
    from api.routers import chat

    class Stub:
        async def stream_chat(self, messages, tools, model):
            role = messages[0]["content"]
            score = 8 if "инвестор" in role.lower() else 5
            yield "text", json.dumps({"score": score, "verdict": "доработать", "pros": ["a"], "cons": ["b"],
                                      "condition": "c", "question": "q"}, ensure_ascii=False)

    monkeypatch.setattr(chat, "get_provider", lambda *a, **k: Stub())
    client = _client()
    h = _token(client)
    resp = client.post("/api/polygons/panel", json={"idea": "Запустить стартап-студию в вузе"}, headers=h)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["opinions"]) == len(polygons.DEFAULT_PANEL)
    assert body["summary"]["decision"] == "идею стоит доработать"


def test_run_polygon_tool():
    from api.runtime.tools import build_default_registry

    reg = build_default_registry(get_settings().tools_root)
    tool = reg._tools["run_polygon"]
    out = asyncio.run(tool.handler({"polygon_id": "pg-1a0e5c0202"}, None))
    assert out["ok"] and len(out["results"]) == 3
    assert out["results"][1]["гипотеза подтверждается, % прогонов"] is not None
