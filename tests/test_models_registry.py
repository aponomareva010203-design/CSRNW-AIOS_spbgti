"""Модели коллективов: ядро в песочнице, реестр, API страницы модели, инструменты чата.

Песочница в тестах — интерпретатор, в котором есть numpy/scipy/scikit-learn/
sympy/pandas: сначала тестовый, затем /opt/aios-sandbox/venv (на сервере).
Если ни в одном нет расчётных библиотек, тесты обучения пропускаются —
проверка спецификаций и прав работает без них.
"""

import asyncio
import csv
import io
import os
import subprocess
import sys

import numpy as np
import pytest

from api.runtime import compute_sandbox, model_engine, model_registry
from api.runtime.config import get_settings
from api.runtime.users import UserStore

ADMIN_LOGIN = "a.ponomareva@csr-nw.ru"
ADMIN_PASSWORD = "1234"


def _python_with_libs():
    for cand in (sys.executable, "/opt/aios-sandbox/venv/bin/python"):
        if not os.path.exists(cand):
            continue
        done = subprocess.run([cand, "-c", "import numpy, scipy, sklearn, sympy, pandas"],
                              capture_output=True, timeout=60)
        if done.returncode == 0:
            return cand
    return None


SANDBOX_PY = _python_with_libs()
needs_sandbox = pytest.mark.skipif(SANDBOX_PY is None, reason="нет интерпретатора с расчётными библиотеками")


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    from api.routers import auth as auth_router

    monkeypatch.setenv("AIOS_ADMIN_LOGIN", ADMIN_LOGIN)
    monkeypatch.setenv("AIOS_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("AIOS_SMTP_HOST", "")
    if SANDBOX_PY:
        monkeypatch.setattr(compute_sandbox, "SANDBOX_PYTHON", SANDBOX_PY)
        monkeypatch.setattr(compute_sandbox, "SANDBOX_ROOT", os.path.dirname(os.path.dirname(SANDBOX_PY)))
        compute_sandbox._isolation_mode.cache_clear()
    for fn in (get_settings, model_registry.get_store, auth_router.get_user_store,
               auth_router.get_token_store, auth_router.get_confirmation_store):
        fn.cache_clear()
    yield
    for fn in (get_settings, model_registry.get_store, auth_router.get_user_store,
               auth_router.get_token_store, auth_router.get_confirmation_store):
        fn.cache_clear()
    compute_sandbox._isolation_mode.cache_clear()


def _client():
    from fastapi.testclient import TestClient

    from api.main import app

    return TestClient(app)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def _register(client, login, name):
    from api.routers import auth as auth_router

    resp = client.post("/api/auth/register", json={"login": login, "password": "pass1234", "name": name,
                                                    "consent_pdn": True, "consent_policy": True})
    assert resp.status_code == 201, resp.text
    users = UserStore(get_settings().runtime_dir / "users")
    user_id = users.get_by_login(login)["id"]
    token = auth_router.get_confirmation_store().issue(user_id)
    client.get(f"/api/auth/confirm?token={token}", follow_redirects=True)
    resp = client.post("/api/auth/login", json={"login": login, "password": "pass1234"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"], user_id


def _admin(client):
    """Токен стартового администратора: раздел «Модели» закрыт ролью
    «Администратор» на уровне роутера (require_admin)."""
    resp = client.post("/api/auth/login", json={"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _pla_csv(n=16, seed=1) -> bytes:
    """Таблица опытов в «русском» CSV: точка с запятой, десятичные запятые."""
    rng = np.random.RandomState(seed)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Концентрация ПМК, мг/мл", "Соотношение фаз", "Скорость, мл/мин", "Стабилизатор", "Диаметр, нм"])
    for i in range(n):
        c, ratio, rate = rng.uniform(2, 20), rng.uniform(2, 10), rng.uniform(0.5, 5)
        stab = ["ПВС", "Tween"][i % 2]
        d = 80 + 9 * c + 60 / ratio - 4 * rate + (15 if stab == "ПВС" else 0) + rng.normal(0, 5)
        w.writerow([f"{c:.2f}".replace(".", ","), f"{ratio:.2f}".replace(".", ","),
                    f"{rate:.2f}".replace(".", ","), stab, f"{d:.1f}".replace(".", ",")])
    return buf.getvalue().encode("utf-8")


PLA_SPEC = {
    "inputs": [
        {"column": "Концентрация ПМК, мг/мл", "title": "Концентрация ПМК", "unit": "мг/мл"},
        {"column": "Соотношение фаз", "title": "Соотношение фаз"},
        {"column": "Скорость, мл/мин", "title": "Скорость введения", "unit": "мл/мин"},
        {"column": "Стабилизатор", "title": "Стабилизатор", "type": "category"},
    ],
    "outputs": [{"column": "Диаметр, нм", "title": "Диаметр", "unit": "нм"}],
    "threshold": {"output": "Диаметр", "max": 25},
}


# ---------------------------------------------------------------------------
# Спецификация — без песочницы


def test_normalize_spec_rejects_bad_input():
    with pytest.raises(model_engine.ModelEngineError):
        model_engine.normalize_spec({"inputs": [], "outputs": [{"column": "y"}]}, "table")
    with pytest.raises(model_engine.ModelEngineError):  # формуле нужно обозначение входа
        model_engine.normalize_spec({"inputs": [{"column": "Температура"}], "outputs": [{"column": "P"}],
                                     "formula": {"expression": "A*T"}}, "formula")
    with pytest.raises(model_engine.ModelEngineError):  # в формуле нет места коду
        model_engine.normalize_spec({"inputs": [{"column": "T", "symbol": "T"}], "outputs": [{"column": "P"}],
                                     "formula": {"expression": "__import__('os')"}}, "formula")
    spec = model_engine.normalize_spec(PLA_SPEC, "table")
    assert [i["name"] for i in spec["inputs"]] == ["x1", "x2", "x3", "x4"]
    assert spec["inputs"][3]["type"] == "category"
    assert spec["threshold"] == {"output": "y1", "metric": "mae", "max": 25.0}
    # входы можно передавать названием столбца или подписью
    assert model_engine.map_inputs(spec, {"Концентрация ПМК": 5, "скорость, мл/мин": 1, "лишнее": 3}) == \
        {"x1": 5, "x3": 1}


def test_draft_of_other_user_is_invisible():
    store = model_registry.get_store()
    card = store.create("aaaaaaaaaaaa", {"kind": "table", "name": "Чужая модель"})
    assert model_engine.level_for(card, {"id": "bbbbbbbbbbbb", "role": "Пользователь"}) is None
    assert model_engine.level_for(card, {"id": "aaaaaaaaaaaa"}) == model_engine.LEVEL_ADMIN
    card = dict(card, status=model_registry.STATUS_PUBLISHED)
    assert model_engine.level_for(card, {"id": "bbbbbbbbbbbb", "role": "Пользователь"}) == model_engine.LEVEL_USE
    assert model_engine.level_for(card, None) == model_engine.LEVEL_USE


def test_kernel_is_not_imported_by_api():
    """Ядро исполняется только в песочнице: API его не импортирует."""
    import api.main  # noqa: F401

    assert "api.runtime.model_kernel" not in sys.modules


# ---------------------------------------------------------------------------
# Полный цикл через API


@needs_sandbox
def test_table_model_full_cycle():
    client = _client()
    owner = _admin(client)
    other, _other_id = _register(client, "other@example.ru", "Посторонний Пётр Петрович")
    import json

    resp = client.post("/api/models/registry/create", headers=auth(owner),
                       data={"kind": "table", "name": "Диаметр частиц ПМК", "spec": json.dumps(PLA_SPEC)},
                       files={"file": ("опыты.csv", _pla_csv(), "text/csv")})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    mid = body["model"]["id"]
    assert body["made_current"] is True
    assert body["version"]["version"] == 1 and body["version"]["passed"] is True
    assert body["version"]["skill"] > 0.6

    view = client.get(f"/api/models/registry/{mid}", headers=auth(owner)).json()
    passport = view["passport"]
    assert passport["validation"]["per_output"]["y1"]["scheme"] == "loo"
    assert passport["validation"]["learning_curve"]["y1"][-1]["n"] == 16
    assert view["can"]["admin"] is True

    pred = client.post(f"/api/models/registry/{mid}/predict", headers=auth(owner),
                       json={"inputs": {"Концентрация ПМК": 10, "Соотношение фаз": 5, "Скорость введения": 2,
                                        "Стабилизатор": "ПВС"}}).json()
    item = pred["items"][0]
    assert 150 < item["outputs"]["y1"]["value"] < 230
    assert item["domain"]["status"] == "inside"
    assert pred["model"]["version"] == 1

    prof = client.post(f"/api/models/registry/{mid}/profile", headers=auth(owner),
                       json={"base": {"x1": 10, "x2": 5, "x3": 2, "x4": "ПВС"}, "vary": "x1", "points": 9}).json()
    assert len(prof["x"]) == 9 and prof["series"]["y1"]["y"][0] < prof["series"]["y1"]["y"][-1]

    opt = client.post(f"/api/models/registry/{mid}/optimize", headers=auth(owner),
                      json={"targets": {"Диаметр": {"value": 150, "tolerance": 15}}, "fixed": {"x4": "Tween"},
                            "count": 3}).json()
    assert opt["items"] and abs(opt["items"][0]["outputs"]["y1"]["value"] - 150) < 15

    # «прогноз → факт»
    fact = client.post(f"/api/models/registry/{mid}/facts", headers=auth(owner),
                       json={"inputs": {"x1": 10, "x2": 5, "x3": 2, "x4": "ПВС"}, "measured": {"Диаметр": 190}}).json()
    assert fact["ok"] and fact["stats"]["y1"]["n"] == 1

    resp = client.post(f"/api/models/registry/{mid}/publish", headers=auth(owner))
    assert resp.status_code == 200, resp.text
    assert client.get(f"/api/models/registry/{mid}", headers=auth(owner)).status_code == 200
    assert [m["id"] for m in client.get("/api/models/registry", headers=auth(owner)).json()["items"]] == [mid]

    # дообучение: +8 опытов → версия 2 становится текущей, если не хуже
    resp = client.post(f"/api/models/registry/{mid}/retrain", headers=auth(owner),
                       data={"mode": "append", "note": "добавлены опыты"},
                       files={"file": ("ещё.csv", _pla_csv(8, seed=7), "text/csv")})
    assert resp.status_code == 200, resp.text
    v2 = resp.json()
    assert v2["version"]["version"] == 2 and v2["version"]["n"] == 24
    data = client.get(f"/api/models/registry/{mid}/data", headers=auth(owner)).json()
    assert len(data["rows"]) == 24

    # подтверждение руководителем: нужно описание независимой проверки
    assert client.post(f"/api/models/registry/{mid}/verify", headers=auth(owner),
                       json={"evidence": "кор"}).status_code == 422
    resp = client.post(f"/api/models/registry/{mid}/verify", headers=auth(owner),
                       json={"evidence": "5 новых опытов сентября 2026, ошибка 12 нм"})
    assert resp.status_code == 200 and resp.json()["status"] == "verified"

    # новая версия проверенной модели не становится текущей сама
    resp = client.post(f"/api/models/registry/{mid}/retrain", headers=auth(owner),
                       data={"mode": "same", "spec_patch": json.dumps({"method": "linear"})})
    v3 = resp.json()
    assert v3["made_current"] is False and "подтверждена" in v3["message"]
    # откат/переключение версии снимает подтверждение
    card = client.post(f"/api/models/registry/{mid}/current", headers=auth(owner), json={"version": 3}).json()
    assert card["status"] == "published" and card["version"] == 3

    # раздел закрыт: обычный пользователь и гость получают 403/401
    # на любой эндпоинт, даже опубликованной модели
    assert client.get("/api/models/registry", headers=auth(other)).status_code == 403
    assert client.get(f"/api/models/registry/{mid}", headers=auth(other)).status_code == 403
    assert client.get("/api/models/registry").status_code == 401


@needs_sandbox
def test_publish_requires_threshold_and_minimum_rows():
    client = _client()
    owner = _admin(client)
    import json

    spec = dict(PLA_SPEC)
    spec.pop("threshold")
    resp = client.post("/api/models/registry/create", headers=auth(owner),
                       data={"kind": "table", "name": "Без порога", "spec": json.dumps(spec)},
                       files={"file": ("d.csv", _pla_csv(), "text/csv")})
    mid = resp.json()["model"]["id"]
    resp = client.post(f"/api/models/registry/{mid}/publish", headers=auth(owner))
    assert resp.status_code == 422 and "порог" in resp.json()["detail"]

    small = client.post("/api/models/registry/create", headers=auth(owner),
                        data={"kind": "table", "name": "Мало данных", "spec": json.dumps(PLA_SPEC)},
                        files={"file": ("d.csv", _pla_csv(6), "text/csv")})
    assert small.status_code == 422 and "минимум 8" in small.json()["detail"]


@needs_sandbox
def test_formula_model_antoine():
    client = _client()
    owner = _admin(client)
    import json

    rng = np.random.RandomState(2)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Температура, °C", "Давление пара, Па"])
    for T in np.linspace(20, 100, 12):
        w.writerow([round(T, 1), round(10 ** (10.196 - 1730.63 / (233.426 + T)) * np.exp(rng.normal(0, 0.02)), 1)])
    spec = {"inputs": [{"column": "Температура, °C", "symbol": "T", "title": "Температура", "unit": "°C"}],
            "outputs": [{"column": "Давление пара, Па", "title": "Давление пара", "unit": "Па"}],
            "formula": {"expression": "P = 10^(A - B/(C + T))", "fit_scale": "log",
                        "params": {"A": {"init": 10}, "B": {"init": 1500}, "C": {"init": 230}}},
            "threshold": {"max": 3000}}
    resp = client.post("/api/models/registry/create", headers=auth(owner),
                       data={"kind": "formula", "name": "Давление пара воды", "spec": json.dumps(spec)},
                       files={"file": ("antoine.csv", buf.getvalue().encode(), "text/csv")})
    assert resp.status_code == 200, resp.text
    mid = resp.json()["model"]["id"]
    view = client.get(f"/api/models/registry/{mid}", headers=auth(owner)).json()
    fit = view["passport"]["fit"]["y1"]
    assert "T" in fit["equation"] and [p["name"] for p in fit["params"]] == ["A", "B", "C"]
    pred = client.post(f"/api/models/registry/{mid}/predict", headers=auth(owner),
                       json={"inputs": [{"T": 50}, {"T": 150}]}).json()
    assert 10000 < pred["items"][0]["outputs"]["y1"]["value"] < 14500
    assert pred["items"][1]["domain"]["status"] == "outside"


def _integrate_cure(t, beta, A, E, n, R):
    """Степень отверждения при линейном нагреве: dy/dt = A*exp(-E/(R*T))*(1-y)^n.
    Метод Рунге-Кутты 4-го порядка на numpy: тесту не нужен scipy, которого нет в
    основном окружении (расчётные библиотеки живут в песочнице)."""
    def rhs(tt, y):
        return A * np.exp(-E / (R * (30 + beta * tt + 273.15))) * max(1 - y, 0) ** n

    ys, y = [0.0], 0.0
    for t0, t1 in zip(t[:-1], t[1:]):
        h = (t1 - t0) / 20
        tt = t0
        for _ in range(20):
            k1 = rhs(tt, y)
            k2 = rhs(tt + h / 2, y + h * k1 / 2)
            k3 = rhs(tt + h / 2, y + h * k2 / 2)
            k4 = rhs(tt + h, y + h * k3)
            y += h * (k1 + 2 * k2 + 2 * k3 + k4) / 6
            tt += h
        ys.append(y)
    return ys


@needs_sandbox
def test_kinetics_model_program():
    client = _client()
    owner = _admin(client)
    import json

    rng = np.random.RandomState(3)
    A, E, n, R = 1e7, 70000.0, 1.4, 8.314
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Опыт", "Время, мин", "Температура, °C", "Степень отверждения"])
    for beta in (2, 5, 10, 20):
        tend = 220.0 / beta
        t = np.linspace(0, tend, 80)
        ys = _integrate_cure(t, beta, A, E, n, R)
        for ti, yi in zip(t, ys):
            w.writerow([f"{beta} °C/мин", round(ti, 3), round(30 + beta * ti, 2),
                        round(min(max(yi + rng.normal(0, 0.004), 0), 1), 4)])
    spec = {"outputs": [{"column": "Степень отверждения", "title": "Степень отверждения"}],
            "kinetics": {"rate": "A*exp(-E/(R*T))*(1-a)^n", "state": "a",
                         "columns": {"run": "Опыт", "t": "Время, мин", "T": "Температура, °C",
                                     "y": "Степень отверждения"}},
            "threshold": {"max": 0.05}}
    resp = client.post("/api/models/registry/create", headers=auth(owner),
                       data={"kind": "kinetics", "name": "Отверждение ЭД-20", "spec": json.dumps(spec)},
                       files={"file": ("dsc.csv", buf.getvalue().encode(), "text/csv")})
    assert resp.status_code == 200, resp.text
    mid = resp.json()["model"]["id"]
    view = client.get(f"/api/models/registry/{mid}", headers=auth(owner)).json()
    params = {p["name"]: p for p in view["passport"]["fit"]["y1"]["params"]}
    assert 65000 < params["E"]["value"] < 75000
    assert view["passport"]["validation"]["per_output"]["y1"]["scheme"] == "loro"
    run = client.post(f"/api/models/registry/{mid}/predict", headers=auth(owner),
                      json={"T0": 25, "program": [{"kind": "ramp", "to": 150, "rate": 5},
                                                  {"kind": "hold", "time": 300}]}).json()
    # при 150 °C заложенная кинетика даёт около 0,96 за 300 мин выдержки
    assert 0.9 < run["final"] < 0.99 and run["reach"][0]["time"] is not None
    opt = client.post(f"/api/models/registry/{mid}/optimize", headers=auth(owner),
                      json={"kinetics": {"target": 0.9, "temperatures": [130, 160]}}).json()
    times = [i["time"] for i in opt["items"]]
    assert times[0] > times[1]


@needs_sandbox
def test_onnx_import():
    pytest.importorskip("skl2onnx")
    if subprocess.run([SANDBOX_PY, "-c", "import onnxruntime"], capture_output=True).returncode != 0:
        pytest.skip("в песочнице нет onnxruntime")
    from skl2onnx import to_onnx
    from sklearn.linear_model import LinearRegression

    client = _client()
    owner = _admin(client)
    import json

    rng = np.random.RandomState(4)
    X = rng.uniform(0, 10, size=(40, 2))
    y = 3 * X[:, 0] - 2 * X[:, 1] + 5
    blob = to_onnx(LinearRegression().fit(X, y), X[:1].astype(np.float32)).SerializeToString()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["x1", "x2", "y"])
    for a, b in rng.uniform(0, 10, size=(10, 2)):
        w.writerow([round(a, 3), round(b, 3), round(3 * a - 2 * b + 5, 3)])
    spec = {"inputs": [{"column": "x1", "min": 0, "max": 10}, {"column": "x2", "min": 0, "max": 10}],
            "outputs": [{"column": "y"}], "threshold": {"max": 0.5}}
    resp = client.post("/api/models/registry/import", headers=auth(owner),
                       data={"name": "Импорт", "spec": json.dumps(spec)},
                       files={"onnx": ("m.onnx", blob, "application/octet-stream"),
                              "holdout": ("h.csv", buf.getvalue().encode(), "text/csv")})
    assert resp.status_code == 200, resp.text
    mid = resp.json()["model"]["id"]
    pred = client.post(f"/api/models/registry/{mid}/predict", headers=auth(owner),
                       json={"inputs": {"x1": 1, "x2": 2}}).json()
    assert abs(pred["items"][0]["outputs"]["y1"]["value"] - 4.0) < 0.01


# ---------------------------------------------------------------------------
# Инструменты чата


@needs_sandbox
def test_chat_tools_train_run_optimize():
    from api.runtime.tools import build_default_registry

    client = _client()
    _, user_id = _register(client, "chat@example.ru", "Чатов Чат Чатович")
    reg = build_default_registry(get_settings().runtime_dir / "tools")
    for name in ("get_models", "run_model", "optimize_model", "train_model", "save_model_fact"):
        assert reg.has(name), name
    ctx = {"session": {"user_id": user_id}}
    rows = list(csv.reader(io.StringIO(_pla_csv().decode("utf-8")), delimiter=";"))
    data = {"columns": rows[0], "rows": [[v.replace(",", ".") if i < 3 or i == 4 else v for i, v in enumerate(r)]
                                         for r in rows[1:]]}
    trained = asyncio.run(reg.call("train_model", {
        "kind": "table", "name": "Диаметр из чата", "data": data,
        "inputs": PLA_SPEC["inputs"], "outputs": PLA_SPEC["outputs"], "threshold": {"max": 25}}, ctx))
    assert trained["ok"], trained
    mid = trained["model_id"]
    assert trained["card"]["component"] == "model_result"
    listed = asyncio.run(reg.call("get_models", {"query": "диаметр"}, ctx))
    assert [m["id"] for m in listed["модели"]] == [mid]
    run = asyncio.run(reg.call("run_model", {"model_id": mid, "inputs": {"Концентрация ПМК": 8,
                                                                          "Соотношение фаз": 6,
                                                                          "Скорость введения": 2,
                                                                          "Стабилизатор": "Tween"}}, ctx))
    assert run["ok"] and run["прогноз"][0]["область"] == "inside"
    opt = asyncio.run(reg.call("optimize_model", {"model_id": mid, "targets": {"Диаметр": {"value": 160}},
                                                  "fixed": {"Стабилизатор": "ПВС"}}, ctx))
    assert opt["ok"] and opt["варианты"]
    fact = asyncio.run(reg.call("save_model_fact", {"model_id": mid, "inputs": {"x1": 8, "x2": 6, "x3": 2,
                                                                                "x4": "Tween"},
                                                    "measured": {"Диаметр": 170}}, ctx))
    assert fact["ok"]
    anon = asyncio.run(reg.call("run_model", {"model_id": mid, "inputs": {"x1": 8}}, {"session": {}}))
    assert anon["ok"] is False  # черновик не виден без входа


@needs_sandbox
def test_agent_bound_model_goes_to_prompt():
    from api.runtime import skill_context, skill_drafts
    from api.runtime.tools import build_default_registry

    client = _client()
    # модель создаёт администратор: раздел «Модели» закрыт ролью на роутере
    token = _admin(client)
    user_id = UserStore(get_settings().runtime_dir / "users").get_by_login(ADMIN_LOGIN)["id"]
    import json

    resp = client.post("/api/models/registry/create", headers=auth(token),
                       data={"kind": "table", "name": "Диаметр для агента", "spec": json.dumps(PLA_SPEC)},
                       files={"file": ("d.csv", _pla_csv(), "text/csv")})
    mid = resp.json()["model"]["id"]
    reg = build_default_registry(get_settings().runtime_dir / "tools")
    ctx = {"session": {"user_id": user_id}}
    skill = {
        "id": "pla-size-by-model", "name": "Режим по модели", "domain": "Полимерные наночастицы",
        "description": "Подбирает режим нанопреципитации под целевой диаметр по модели коллектива.",
        "triggers": ["qq-тест-режим-1", "qq-тест-режим-2", "qq-тест-режим-3"],  # уникальные: не пересекаются с реальными агентами каталога
        "role": "подбирает режимы получения частиц ПМК по модели коллектива",
        "task": "по целевому диаметру выдаёт 2–3 режима с прогнозом и погрешностью",
        "steps": [{"title": "Цель", "text": "Спросите целевой диаметр и допуск."},
                  {"title": "Подбор", "text": "Подберите режимы по модели и покажите таблицу."}],
        "tools": [{"name": "optimize_model", "when": "подбор режима под целевой диаметр"},
                  {"name": "run_model", "when": "прогноз диаметра для режима пользователя"}],
        "final": "Человек получает 2–3 режима с прогнозом диаметра и погрешностью.",
        "model_ids": [mid],
    }
    saved = asyncio.run(reg.call("save_skill_draft", {"skill": skill}, ctx))
    assert saved["ok"], saved
    item = skill_drafts.get_store().get(user_id, "pla-size-by-model")
    assert f"- {mid}" in item["text"] or mid in item["text"]
    assert skill_drafts.bound_model_ids(["draft:pla-size-by-model"], user_id) == [mid]
    block = skill_context.connected_models_block({"model_ids": [mid]})
    assert mid in block and "run_model" in block and "Концентрация ПМК" in block
    # чужая неопубликованная модель агенту не привязывается
    bad = dict(skill, id="pla-size-other", model_ids=["mdl-0000000000"])
    assert asyncio.run(reg.call("save_skill_draft", {"skill": bad}, ctx))["ok"] is False
