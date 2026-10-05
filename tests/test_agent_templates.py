"""Проверка готовых шаблонов агентов (scripts/agent_templates.json).

Шаблоны — основа формата семинара: участник не собирает агента с нуля
(15-20 минут при идеальном ходе на полтора часа и 17 человек), а берёт
готового под свой проект и дорабатывает словами. Граф в шаблоне написан
один раз вручную, а не порождается моделью для каждого участника — это
снимает главный источник отказов конструктора.

Поэтому шаблоны должны быть проверены как код: битый шаблон означает
участника без работающего агента в четверг.
"""

import json
from pathlib import Path

import pytest

TEMPLATES_PATH = Path(__file__).resolve().parent.parent / "scripts" / "agent_templates.json"


@pytest.fixture(scope="module")
def templates():
    data = json.loads(TEMPLATES_PATH.read_text(encoding="utf-8"))
    return data["templates"]


def test_every_project_has_a_template(templates):
    assert len(templates) == 17, "по одному агенту на проект Техноложки"
    ids = [t["template_id"] for t in templates]
    assert len(set(ids)) == len(ids), "id шаблонов должны быть уникальны"


def test_graph_is_consistent(templates):
    """Рёбра и available_tools ссылаются на существующие узлы.

    Рассинхронизация даёт агента без инструментов, который молча отвечает
    из головы — то есть худший из возможных отказов: незаметный.
    """
    for t in templates:
        ids = {n["id"] for n in t["nodes"]}
        reasoning = [n for n in t["nodes"] if n["type"] == "reasoning"]
        assert len(reasoning) == 1, t["template_id"]
        available = set(reasoning[0]["config"]["available_tools"])
        assert available <= ids, f"{t['template_id']}: available_tools в никуда"
        assert all(n["id"] in available for n in t["nodes"]
                   if n["type"].startswith("tool.")), \
            f"{t['template_id']}: узел инструмента не подключён к reasoning"
        for edge in t["edges"]:
            assert edge["from"] in ids and edge["to"] in ids, t["template_id"]
        assert any(n["type"] == "trigger" for n in t["nodes"]), t["template_id"]
        assert any(n["type"] == "output" for n in t["nodes"]), t["template_id"]


def test_fits_storage_limits(templates):
    """Лимиты agent_store: карточка сверх них будет молча обрезана."""
    from api.runtime.agent_store import LIST_FIELD_LIMITS

    for t in templates:
        assert len(t["nodes"]) <= LIST_FIELD_LIMITS["nodes"], t["template_id"]
        assert len(t["edges"]) <= LIST_FIELD_LIMITS["edges"], t["template_id"]
        assert len(t["evals"]) <= LIST_FIELD_LIMITS["evals"], t["template_id"]
        assert len(t["title"]) <= 200, t["template_id"]
        assert len(t["role_description"]) <= 500, t["template_id"]


def test_no_chatbot_over_own_documents(templates):
    """Ни один шаблон не сводится к «чат-бот по своей же базе знаний».

    Критерий полезности: ответ содержит число, таблицу, файл или ссылку,
    которых у участника не было до вопроса. Агент, умеющий только искать
    по документам, которые участник сам и написал, этому не отвечает.
    """
    external = {"run_calc", "literature_search", "compound_lookup",
                "read_data_file", "make_document"}
    for t in templates:
        assert external & set(t["capabilities"]), \
            f"{t['template_id']}: только внутренние возможности — замысел слабый"


def test_calc_agents_forbid_inventing_numbers(templates):
    """Расчётные агенты обязаны запрещать выдумывание чисел при сбое.

    Живой прогон 22.09 показал, зачем: когда run_calc упал, модель написала
    «ΔT = 25/0,8109 = 60,0 К» (верно 30,8) и выдала площадь 48 м² вместо 95 —
    с формулами, таблицей и примечанием про запас на загрязнение. Красиво
    оформленное неверное число опаснее отказа: по нему закажут аппарат.
    """
    for t in templates:
        if "run_calc" not in t["capabilities"]:
            continue
        for text in (t["system_prompt"],
                     *(n["config"]["system_prompt"] for n in t["nodes"]
                       if n["type"] == "reasoning")):
            assert "НЕ НАЗЫВАЙ НИКАКИХ ЧИСЕЛ" in text, t["template_id"]
            assert "run_calc" in text, t["template_id"]


def test_evals_are_checkable(templates):
    """У каждого шаблона есть тест, который нельзя пройти, не сделав работу."""
    strong = {"expect_number", "expect_tool", "expect_component"}
    for t in templates:
        assert t["evals"], f"{t['template_id']}: нет ни одного теста"
        assert any(strong & set(case) for case in t["evals"]), \
            f"{t['template_id']}: только подстроки — такой тест проходится без работы"


def test_templates_load_into_store(tmp_path, monkeypatch):
    """Шаблон разворачивается в карточку, повторная загрузка не плодит дубли."""
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("AIOS_AGENT_DATA_DIR", str(tmp_path / "data"))

    from api.runtime import agent_store
    from api.runtime.config import get_settings

    get_settings.cache_clear()
    agent_store._STORE = None
    try:
        import sys
        sys.path.insert(0, str(TEMPLATES_PATH.parent))
        import load_agent_templates as loader

        store = agent_store.get_store()
        template = loader.load_templates()["tpl-hex"]
        owner, project = "a1b2c3d4e5f6", "prj-test123"

        loader.assign(store, template, owner, project, rag_id="rag-test01")
        loader.assign(store, template, owner, project, rag_id="rag-test01")

        cards = store.list(owner, project)
        assert len(cards) == 1, "повторная загрузка не должна создавать дубль"
        card = cards[0]
        assert card["template_id"] == "tpl-hex"
        assert card["status"] == "draft", "публикует владелец, а не скрипт"
        rag_nodes = [n for n in card["nodes"] if n["type"] == "tool.rag_search"]
        assert rag_nodes and rag_nodes[0]["config"]["rag_id"] == "rag-test01"
    finally:
        agent_store._STORE = None
        get_settings.cache_clear()


def test_template_without_rag_drops_the_node(tmp_path, monkeypatch):
    """У проекта нет базы знаний — узел поиска не остаётся висеть пустым."""
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path))
    from api.runtime.config import get_settings

    get_settings.cache_clear()
    try:
        import sys
        sys.path.insert(0, str(TEMPLATES_PATH.parent))
        import load_agent_templates as loader

        card = loader.build_card(loader.load_templates()["tpl-hex"], "prj-x", rag_id="")
        assert not [n for n in card["nodes"] if n["type"] == "tool.rag_search"]
        reasoning = [n for n in card["nodes"] if n["type"] == "reasoning"][0]
        ids = {n["id"] for n in card["nodes"]}
        assert set(reasoning["config"]["available_tools"]) <= ids
    finally:
        get_settings.cache_clear()


def test_every_template_has_a_working_console(templates):
    """У каждого шаблона есть пульт, и он переживает нормализацию хранилища.

    Зачем это проверять. Пульт — единственное, что отличает рабочую страницу
    агента от окна чата: инженер приходит с ПАРАМЕТРАМИ (расход, температура,
    доля волокна), а не с формулировкой. Механизм в платформе был (console в
    карточке, compute_console, отрисовка в agent_page.js), но ни один из
    семнадцати шаблонов пульта не объявлял — и участник открывал страницу с
    пустым полем ввода, то есть ровно тот интерфейс, от которого уходили.

    Проверяется не наличие ключа, а результат: agent_store._normalize_console
    молча выбрасывает поля без имени, select без вариантов и кнопки без
    шаблона вопроса, поэтому «пульт есть» и «пульт доедет до страницы» —
    разные утверждения.
    """
    import re

    from api.runtime import agent_store

    for t in templates:
        tid = t["template_id"]
        raw = t.get("console")
        assert raw, f"{tid}: нет пульта — страница агента будет пустым чатом"

        norm = agent_store._normalize_console(raw)
        assert len(norm["inputs"]) == len(raw["inputs"]), f"{tid}: поле пульта отброшено"
        assert len(norm["actions"]) == len(raw["actions"]), f"{tid}: кнопка пульта отброшена"
        assert sum(1 for a in norm["actions"] if a["primary"]) == 1, \
            f"{tid}: главное действие должно быть ровно одно"

        blocks = agent_store.compute_console(dict(t, console=norm))
        assert "params" in blocks, f"{tid}: платформа не покажет пульт"

        # плейсшолдер без поля подставится как «(не задано)» — вопрос уедет
        # агенту с дырой, и участник этого не увидит
        names = {i["name"] for i in norm["inputs"]}
        for action in norm["actions"]:
            for placeholder in re.findall(r"\{([^}]+)\}", action["prompt"]):
                assert placeholder.strip() in names, \
                    f"{tid}: кнопка «{action['label']}» ссылается на поле {placeholder}"


def test_components_are_not_copied_into_prompts(templates):
    """Каталог компонентов ответа подмешивает платформа, а не шаблон.

    Список был вписан в промпт каждого шаблона по два раза (карточка и узел
    рассуждения) — тридцать четыре копии одного текста. Новый компонент не
    доходил до уже созданных агентов, а участник, правящий инструкцию своего
    агента словами, сносил блок целиком, и агент переставал показывать
    расчёты. Теперь блок добавляется в момент исполнения (workflow_engine).
    """
    from api.runtime import response_views

    for t in templates:
        texts = [t["system_prompt"]] + [
            n["config"]["system_prompt"] for n in t["nodes"] if n["type"] == "reasoning"
        ]
        for text in texts:
            assert "## Как показывать результат" not in text, \
                f"{t['template_id']}: каталог компонентов снова скопирован в промпт"

    block = response_views.prompt_block()
    assert "aios-view" in block and "calc_trace" in block and "verdict" in block


def test_backend_and_frontend_component_catalogs_match():
    """Имя компонента, которого нет во фронтенде, отрисуется блоком кода.

    Каталог живёт в двух местах по необходимости: платформа объясняет модели,
    что есть (api/runtime/response_views.py), а рисует фронтенд
    (frontend/js/agent_views.js). Расхождение молчаливое — агент послушно
    вернёт компонент, а участник увидит JSON, — поэтому сверяется тестом.
    """
    import re

    from api.runtime import response_views

    js = (Path(__file__).resolve().parent.parent
          / "frontend" / "js" / "agent_views.js").read_text(encoding="utf-8")
    body = js[js.index("var COMPONENTS = {"):]
    body = body[: body.index("};")]
    rendered = set(re.findall(r"^\s*(\w+)\s*:", body, re.M))

    described = set(response_views.names())
    assert described <= rendered, \
        f"платформа обещает модели то, что не рисуется: {sorted(described - rendered)}"
    assert rendered <= described, \
        f"компонент есть, но модель о нём не узнает: {sorted(rendered - described)}"


def test_reload_does_not_silently_erase_owner_edits(tmp_path, monkeypatch):
    """Повторная загрузка шаблонов не должна молча стирать работу участника.

    Смысл семинара — «возьми готового агента и доведи словами». Доводка
    меняет ровно те поля, которые повторный запуск скрипта перезаписывает
    шаблонными: инструкцию, приветствие, тестовые случаи. Раньше это
    происходило без единого слова в выводе — участник возвращался к агенту
    и не находил своих правок, а причину было не восстановить.

    Теперь: --only-new не трогает существующих вовсе, а без флага скрипт
    вслух говорит, чьи правки заменил.
    """
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("AIOS_AGENT_DATA_DIR", str(tmp_path / "data"))

    from api.runtime import agent_store
    from api.runtime.config import get_settings

    get_settings.cache_clear()
    agent_store._STORE = None
    try:
        import sys

        sys.path.insert(0, str(TEMPLATES_PATH.parent))
        import load_agent_templates as loader

        store = agent_store.get_store()
        template = loader.load_templates()["tpl-hex"]
        owner, project = "a1b2c3d4e5f6", "prj-seminar"

        loader.assign(store, template, owner, project)
        card = store.list(owner, project)[0]
        assert not loader.was_edited(card, template), "сразу после загрузки правок нет"

        card["agent_id"] = card["id"]
        card["system_prompt"] += "\n\nУ нас на кафедре k берут не выше 900."
        card["welcome_message"] = "Назовите режим."
        store.save(owner, card)
        edited = store.list(owner, project)[0]
        assert loader.was_edited(edited, template), "доработка должна распознаваться"

        skipped = loader.assign(store, template, owner, project, only_new=True)
        assert "пропущен" in skipped and "доработан владельцем" in skipped
        kept = store.list(owner, project)[0]
        assert "не выше 900" in kept["system_prompt"]
        assert kept["welcome_message"] == "Назовите режим."

        warned = loader.assign(store, template, owner, project, dry_run=True)
        assert "ВНИМАНИЕ" in warned, "пробный прогон обязан предупредить"

        overwritten = loader.assign(store, template, owner, project)
        assert "правки владельца заменены" in overwritten
        assert "не выше 900" not in store.list(owner, project)[0]["system_prompt"]
    finally:
        agent_store._STORE = None
        get_settings.cache_clear()
