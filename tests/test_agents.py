"""Смоук-тесты агентного модуля: манифест агента, workflow_engine, API.

Манифест (agent_store.py): нормализация autonomy/evals/views, вычисление
представлений compute_views (платформа решает, какие шаблоны показывать),
служебная отметка фонового запуска mark_schedule_run без bump версии,
статусы записей (очередь утверждений), AgentEvalStore.

Движок (workflow_engine.py, скриптованный провайдер вместо LLM):
граница проекта в tool.agent_call (делегирование только внутри своего
проекта — find_any_owner ищет по всем пользователям), requires_approval
(действие выполняется только после утверждения человеком, однократно),
стриминг delta наружу, инструменты-оформители format_table/chart_generate
доступны без узлов. Планировщик: due_agents/mark_schedule_run.

API (routers/agents.py): 401 без токена; карточка + views; PATCH статуса
публикации; вердикты по записям; прогон evals с MockProvider.
"""

import asyncio
import json

import pytest

from api.runtime import agent_store, projects
from api.runtime.config import get_settings
from api.runtime.providers import ToolCall
from api.runtime.tools import build_default_registry

ADMIN_LOGIN = "a.ponomareva@csr-nw.ru"
ADMIN_PASSWORD = "1234"


class ScriptedProvider:
    """Детерминированный провайдер для движка: сценарий ответов по порядку.

    Каждый элемент — ("text", "…") или ("tool_calls", [{"name", "arguments"}]).
    Запоминает обращения, чтобы тесты видели, сколько раз модель вызывалась
    и что она получила в результатах инструментов.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def stream_chat(self, messages, tools=None, model=None, max_tokens=None):
        self.calls.append(list(messages))
        if self.script:
            kind, payload = self.script.pop(0)
            if kind == "tool_calls":
                calls = [
                    ToolCall(id=f"call_{i}", name=c["name"], arguments=c.get("arguments") or {})
                    for i, c in enumerate(payload)
                ]
                yield ("tool_calls", calls)
                return
            yield (kind, payload)
            return
        yield ("text", "")


def _graph(available_tools=("n3",), extra_nodes=()):
    nodes = [
        {"id": "n1", "type": "trigger", "config": {}},
        {"id": "n2", "type": "reasoning", "config": {"system_prompt": "тест", "available_tools": list(available_tools)}},
        {"id": "n3", "type": "tool.agent_call", "config": {"agent_id": "agt-target"}},
        {"id": "n4", "type": "output", "config": {}},
    ]
    nodes.extend(extra_nodes)
    return nodes, [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n4"}]


def _card(**overrides):
    nodes, edges = _graph()
    card = {
        "id": "agt-caller",
        "owner_id": "u1",
        "project_id": "prj-1",
        "title": "Оркестратор",
        "nodes": nodes,
        "edges": edges,
        "trigger": {"type": "chat"},
    }
    card.update(overrides)
    return card


def _run(card, provider):
    settings = get_settings()
    registry = build_default_registry(settings.runtime_dir / "tools")
    return asyncio.run(
        workflow_engine.run_workflow(
            card=card, user_text="задача", settings=settings,
            provider=provider, registry=registry, session={},
        )
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    from api.routers import auth as auth_router

    monkeypatch.setenv("AIOS_ADMIN_LOGIN", ADMIN_LOGIN)
    monkeypatch.setenv("AIOS_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    # без SMTP регистрация идёт в dev-режиме: без MX-проверки домена
    monkeypatch.setenv("AIOS_SMTP_HOST", "")
    monkeypatch.setenv("AIOS_AGENT_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    agent_store._STORE = None
    agent_store._RECORD_STORE = None
    agent_store._EVAL_STORE = None
    auth_router.get_user_store.cache_clear()
    auth_router.get_token_store.cache_clear()
    auth_router.get_confirmation_store.cache_clear()
    yield
    agent_store._STORE = None
    agent_store._RECORD_STORE = None
    agent_store._EVAL_STORE = None
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# compute_views: платформа сама подбирает представления


def test_compute_views_chat_by_default():
    assert agent_store.compute_views({"interface_type": "chat"}) == ["chat"]


def test_compute_views_interface_and_records():
    card = {"interface_type": "dashboard", "nodes": [{"type": "tool.save_record"}]}
    assert agent_store.compute_views(card) == ["chat", "catalog", "dashboard"]


def test_compute_views_records_count_and_schedule_and_approval():
    card = {
        "interface_type": "chat",
        "autonomy": {"requires_approval": ["call_agent"]},
        "trigger": {"type": "schedule"},
    }
    views = agent_store.compute_views(card, records_count=3)
    assert views == ["chat", "catalog", "approval_queue", "background_monitor"]


def test_compute_views_explicit_views_added():
    card = {"interface_type": "chat", "views": ["benchmark", "чепуха"]}
    assert agent_store.compute_views(card) == ["chat", "benchmark"]


# ---------------------------------------------------------------------------
# save(): нормализация манифеста


def test_save_normalizes_manifest():
    store = agent_store.get_store()
    saved = store.save(
        "u1",
        {
            "project_id": "prj-1",
            "title": "Агент",
            "autonomy": {"level": "что-то", "can": {"call_agents": False}, "requires_approval": ["call_agent", ""]},
            "views": ["catalog", "нет-такого"],
            "evals": [{"input": "вопрос", "expect_contains": ["ответ"], "note": ""}, {"нет_input": True}],
            "welcome_message": "Здравствуйте",
        },
    )
    assert saved["autonomy"]["level"] == "assistive"  # неизвестный уровень → assistive
    assert saved["autonomy"]["can"] == {"call_agents": False}
    assert saved["autonomy"]["requires_approval"] == ["call_agent"]
    assert saved["views"] == ["catalog"]
    assert saved["evals"] == [{"input": "вопрос", "expect_contains": ["ответ"], "note": ""}]
    assert saved["welcome_message"] == "Здравствуйте"


def test_mark_schedule_run_no_version_bump():
    store = agent_store.get_store()
    saved = store.save("u1", {"project_id": "prj-1", "title": "А"})
    version = saved["version"]
    store.mark_schedule_run("u1", saved["id"], 123.0, "ok")
    again = store.get("u1", saved["id"])
    assert again["last_run_at"] == 123.0
    assert again["last_run_status"] == "ok"
    assert again["version"] == version
    assert again["history"] == saved["history"]


# ---------------------------------------------------------------------------
# записи: очередь утверждений; eval-хранилище


def test_record_status_roundtrip():
    records = agent_store.get_record_store()
    rec = records.add("u1", "agt-1", {"тип": "запрос на утверждение", "signature": "abc"}, "autonomy")
    assert rec["status"] == ""
    records.set_status("u1", "agt-1", rec["id"], "approved")
    assert records.get("u1", "agt-1", rec["id"])["status"] == "approved"
    assert records.list("u1", "agt-1", status="pending") == []
    assert records.get("u1", "agt-1", "rec-нет") is None


def test_eval_store_roundtrip():
    store = agent_store.get_eval_store()
    run = store.add_run("u1", "agt-1", {"summary": {"total": 1, "passed": 1}, "results": []})
    assert store.list("u1", "agt-1")[0]["id"] == run["id"]


# ---------------------------------------------------------------------------
# workflow_engine: граница проекта при делегировании


# ---------------------------------------------------------------------------
# workflow_engine: requires_approval — действие после утверждения, однократно


# ---------------------------------------------------------------------------
# workflow_engine: стриминг и инструменты-оформители


# ---------------------------------------------------------------------------
# планировщик расписаний


# ---------------------------------------------------------------------------
# API


def _login_admin(client):
    resp = client.post("/api/auth/login", json={"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


# ---------------------------------------------------------------------------
# Протокол tool-calling и память диалога
#
# Эти проверки закрывают класс дефектов, который MockProvider/ScriptedProvider
# обнаружить не могут в принципе: они смотрят только на наличие роли "tool" и
# не валидируют структуру запроса. Из-за этого сломанный протокол жил в
# движке при полностью зелёных тестах, а боевой провайдер отвечал на него
# 400 «tool messages must include a non-empty string tool_call_id».
# Поэтому валидируем ЗАПРОС, а не только ответ.


def _protocol_errors(messages: list) -> list[str]:
    """Нарушения протокола OpenAI tool-calling в массиве сообщений.

    Те же правила, которые проверяет провайдер: каждое tool-сообщение имеет
    непустой tool_call_id и отвечает на вызов из предшествующего
    assistant-сообщения; на каждый объявленный вызов есть ответ.
    """
    errors: list[str] = []
    awaiting: set[str] = set()
    for i, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                if not call.get("id"):
                    errors.append(f"[{i}] tool_call без id")
                awaiting.add(call.get("id"))
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if not call_id:
                errors.append(f"[{i}] role=tool без tool_call_id")
            elif call_id not in awaiting:
                errors.append(f"[{i}] tool_call_id={call_id} не отвечает ни на один вызов выше")
            else:
                awaiting.discard(call_id)
    if awaiting:
        errors.append(f"вызовы без ответа: {sorted(awaiting)}")
    return errors


# ---------------------------------------------------------------------------
# Критерии тестовых случаев (evals)
#
# Живой прогон на боевой модели показал, чего стоят подстроки: когда расчёт
# упал, модель написала «ΔT = 25/0,8109 = 60,0 К» (верно 30,8) и выдала
# A = 48 м² вместо 95 — с формулами, таблицей и примечанием про запас.
# Подстроки такой ответ пропустили бы. Число с допуском и факт вызова
# инструмента — нет.


def test_new_eval_fields_survive_normalization():
    """Поля expect_number/expect_tool/expect_component не должны отбрасываться
    при сохранении карточки — иначе конструктор их пишет, а прогон не видит."""
    normalized = agent_store._normalize_evals([{
        "input": "Вода 30 м3/ч, 80→40 C",
        "expect_number": {"value": 95.2, "tolerance": 0.15, "unit": "м²"},
        "expect_tool": "run_calc",
        "expect_component": "calc_trace",
    }])
    case = normalized[0]
    assert case["expect_number"] == {"value": 95.2, "tolerance": 0.15, "unit": "м²"}
    assert case["expect_tool"] == "run_calc"
    assert case["expect_component"] == "calc_trace"


def test_eval_number_tolerance_is_bounded():
    """Мусорный допуск не должен превращать проверку в «подойдёт любое число»."""
    case = agent_store._normalize_evals([
        {"input": "q", "expect_number": {"value": 10, "tolerance": 99}},
    ])[0]
    assert case["expect_number"]["tolerance"] == 1.0


# ---------------------------------------------------------------------------
# Пульт агента и сборка страницы из блоков


def test_console_normalization_drops_broken_fields():
    """Пульт собирает модель, поэтому мусор в нём — вопрос времени.
    Поле без имени, select без вариантов и действие без шаблона не должны
    попадать в карточку: на странице они дали бы пустой контрол или кнопку,
    которая ничего не отправляет."""
    console = agent_store._normalize_console({
        "title": "Параметры",
        "inputs": [
            {"name": "Vf", "label": "Доля волокна", "type": "number",
             "min": 0.1, "max": 0.7, "default": "0.55"},
            {"name": "", "label": "без имени", "type": "text"},
            {"name": "empty", "label": "Пустой выбор", "type": "select", "options": []},
            {"name": "kind", "label": "Волокно", "type": "select",
             "options": ["базальт", "стекло"]},
        ],
        "actions": [
            {"label": "Сравнить", "prompt": "Vf={Vf}, волокно {kind}"},
            {"label": "Без шаблона"},
        ],
    })
    assert [i["name"] for i in console["inputs"]] == ["Vf", "kind"]
    assert len(console["actions"]) == 1
    assert console["actions"][0]["primary"] is True, "единственное действие — главное"


def test_console_absent_means_pure_chat():
    assert agent_store._normalize_console({}) == {}
    assert agent_store._normalize_console("мусор") == {}


def test_page_blocks_follow_what_agent_can_do():
    """Набор блоков страницы вычисляется по составу агента, а не по типу,
    выбранному при сборке: тип устаревает на первой доработке, состав — нет."""
    calc = {"console": {"inputs": [{"name": "x"}]}, "nodes": [{"type": "tool.run_calc"}]}
    assert agent_store.compute_console(calc) == ["params", "results", "talk"]

    experimenter = {"console": {"inputs": [{"name": "x"}]},
                    "nodes": [{"type": "tool.read_data_file"}, {"type": "tool.run_calc"}]}
    assert agent_store.compute_console(experimenter) == ["params", "dataset", "results", "talk"]

    chatbot = {"nodes": [{"type": "tool.rag_search"}]}
    assert agent_store.compute_console(chatbot) == ["results", "talk"], \
        "агенту со свободным входом пульт не нужен — это правильный исход"

    background = {"nodes": [], "trigger": {"type": "schedule"},
                  "autonomy": {"requires_approval": ["notify"]}}
    assert agent_store.compute_console(background) == ["results", "talk", "queue", "monitor"]


def test_console_survives_save():
    """Пульт должен доживать до карточки: конструктор его пишет, страница читает."""
    store = agent_store.get_store()
    saved = store.save("a1b2c3d4e5f6", {
        "project_id": "prj-console", "title": "С пультом",
        "nodes": [{"id": "n1", "type": "trigger", "config": {}}], "edges": [],
        "console": {"title": "Параметры потоков",
                    "inputs": [{"name": "G", "label": "Расход", "type": "number", "unit": "м³/ч"}],
                    "actions": [{"label": "Посчитать", "prompt": "Расход {G}"}]},
    })
    assert saved["console"]["inputs"][0]["unit"] == "м³/ч"
    assert saved["console"]["actions"][0]["primary"] is True


# ---------------------------------------------------------------------------
# DELETE /api/agents/{id}: удаление агента администратором


def _agent_card_min(project_id="prj-del"):
    return {
        "project_id": project_id,
        "title": "Агент на удаление",
        "nodes": [
            {"id": "n1", "type": "trigger", "config": {}},
            {"id": "n2", "type": "reasoning", "config": {"system_prompt": "т", "available_tools": []}},
            {"id": "n3", "type": "output", "config": {}},
        ],
        "edges": [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n3"}],
    }


# ---------------------------------------------------------------------------
# DELETE /api/catalog/agents/{id}: удаление агента из каталога (карточка = агент)


def test_catalog_agent_delete_admin_full(tmp_path, monkeypatch):
    """Администратор удаляет агента каталога: карточка skills.json и .md-файл
    уходят с публичного доступа, но архивируются в skills-removed/_catalog/,
    runtime-экземпляр agt-{id} с данными — в agents-removed. Не-админ — 403,
    несуществующий — 404."""
    import json as _json
    from pathlib import Path

    from fastapi.testclient import TestClient

    from api.main import app
    from api.runtime import datarepo
    from api.runtime.config import get_settings

    # изолированный каталог data/ для теста (не трогаем репозиторный)
    data_dir = tmp_path / "data"
    (data_dir / "skills").mkdir(parents=True)
    (data_dir / "skills" / "del-me.md").write_text(
        "---\nname: На удаление\nversion: 1\ntriggers:\n  - удалить меня\n---\n\n# Текст агента\n",
        encoding="utf-8",
    )
    (data_dir / "skills.json").write_text(
        _json.dumps({"categories": [], "items": [{
            "id": "del-me", "category": "general", "name": "На удаление",
            "domain": "Тест", "description_ru": "тест",
            "author": "тест", "recommended_model": "—",
            "file_url": "/data/skills/del-me.md",
        }]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    monkeypatch.setattr(datarepo, "DATA_DIR", data_dir)

    client = TestClient(app)
    admin_login = client.post("/api/auth/login", json={"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD})
    admin_id = admin_login.json()["user"]["id"]
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['token']}"}

    # runtime-экземпляр по конвенции agt-{id} + запись в копилке
    store = agent_store.get_store()
    saved = store.save(admin_id, dict(_agent_card_min(project_id="prj-catalog"), agent_id="agt-del-me"))
    assert saved["id"] == "agt-del-me"
    agent_store.get_record_store().add(admin_id, "agt-del-me", {"найдено": "x"}, "работа")

    # не-админ: регистрируем обычного пользователя
    reg = client.post("/api/auth/register", json={
        "login": "cat-del@ex.com", "password": "pass1234", "name": "Зритель",
        "consent_pdn": True, "consent_policy": True,
    })
    assert reg.status_code == 201
    from api.routers import auth as auth_router
    from api.runtime.users import UserStore

    viewer_id = UserStore(get_settings().runtime_dir / "users").get_by_login("cat-del@ex.com")["id"]
    confirm = auth_router.get_confirmation_store().issue(viewer_id)
    client.get(f"/api/auth/confirm?token={confirm}", follow_redirects=True)
    viewer_login = client.post("/api/auth/login", json={"login": "cat-del@ex.com", "password": "pass1234"})
    viewer_headers = {"Authorization": f"Bearer {viewer_login.json()['token']}"}

    forbidden = client.delete("/api/catalog/agents/del-me", headers=viewer_headers)
    assert forbidden.status_code == 403
    assert datarepo.get_skill("del-me") is not None  # ничего не тронуто

    ok = client.delete("/api/catalog/agents/del-me", headers=admin_headers)
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["card"] is True and body["file"] is True and body["runtime"] is True
    assert body["archived"] is True

    # с публичного доступа ушло, но в закрытом архиве всё сохранилось
    assert datarepo.get_skill("del-me") is None
    assert not (data_dir / "skills" / "del-me.md").exists()
    assert store.find_any_owner("agt-del-me") is None
    assert not (get_settings().runtime_dir / "agents" / admin_id / "agt-del-me").exists()
    manifest = get_settings().runtime_dir / "skills-removed" / "_catalog" / "del-me.json"
    assert manifest.is_file()
    archived = _json.loads(manifest.read_text(encoding="utf-8"))
    assert archived["card"]["id"] == "del-me" and "# Текст агента" in archived["text"]
    assert (get_settings().runtime_dir / "agents-removed" / admin_id / "agt-del-me.json").is_file()

    # повторно и несуществующее — 404; feedback тоже уходит в 404
    assert client.delete("/api/catalog/agents/del-me", headers=admin_headers).status_code == 404
    assert client.get("/api/catalog/agents/del-me/feedback").status_code == 404