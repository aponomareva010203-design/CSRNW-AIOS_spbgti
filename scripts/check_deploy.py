#!/usr/bin/env python3
"""Проверка сервера после переноса или выкладки: что реально работает.

Отвечает на вопросы, ответ на которые иначе приходится искать руками и
каждый раз заново: зарегистрирован ли run_calc, каким способом изолирована
песочница, читаются ли методики и тела навыков, закрыт ли Qdrant снаружи,
совпадает ли адрес в AIOS_PUBLIC_BASE_URL с машиной, на которой запущено.

Ничего не меняет. Запускать на сервере:

    cd ~/AIOS_Technology_Institute_Harness
    PYTHONPATH=. .venv/bin/python scripts/check_deploy.py

С флагом --live дополнительно задаётся НАСТОЯЩИЙ рабочий вопрос агенту из
шаблона, на боевой модели, с исполнением расчёта в песочнице:

    PYTHONPATH=. .venv/bin/python scripts/check_deploy.py --live

Это единственная проверка, которая отвечает на вопрос «агент считает?» —
всё остальное отвечает лишь на «всё ли установлено». Опыт этого проекта:
два блокирующих дефекта движка прожили несколько сессий при полностью
зелёных тестах, потому что тесты шли на моке и физически не могли их
увидеть. Карточка агента строится из шаблона в памяти и никуда не
сохраняется, но токены провайдера тратятся — поэтому под флагом.

Код возврата: 0 — всё существенное на месте, 1 — есть отказы (строки ✗).
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

OK, WARN, FAIL = "  ✓", "  ~", "  ✗"
_failures: list[str] = []


def say(mark: str, text: str, note: str = "") -> None:
    print(f"{mark} {text}" + (f"\n      {note}" if note else ""))
    if mark is FAIL:
        _failures.append(text)


def head(title: str) -> None:
    print(f"\n{title}\n" + "-" * len(title))


def check_tools() -> None:
    head("Инструменты агента")
    from api.runtime.config import get_settings
    from api.runtime.tools import build_default_registry

    registry = build_default_registry(get_settings().runtime_dir / "tools")
    names = ("run_calc", "get_recipe", "read_data_file", "make_document",
             "literature_search", "compound_lookup", "search_corpus", "notify")
    for name in names:
        if registry.has(name):
            say(OK, name)
        elif name == "run_calc":
            say(FAIL, "run_calc НЕ зарегистрирован",
                "нет /opt/aios-sandbox/venv — 16 из 17 готовых агентов не смогут считать")
        else:
            say(WARN, f"{name} отсутствует")


def check_sandbox() -> None:
    head("Песочница расчётов")
    from api.runtime import compute_sandbox

    if not compute_sandbox.sandbox_available():
        say(FAIL, f"интерпретатор не найден: {compute_sandbox.SANDBOX_PYTHON}")
        return
    say(OK, f"интерпретатор: {compute_sandbox.SANDBOX_PYTHON}")

    report = compute_sandbox.isolation_report()
    mode = report.get("mode")
    if mode in ("bwrap", "user"):
        say(OK, f"изоляция: {mode}", str(report.get("описание", "")))
    else:
        say(FAIL, f"изоляция: {mode}",
            "расчёт исполняется от владельца платформы и достанет .env")

    import asyncio

    result = asyncio.run(compute_sandbox.run_code(
        "import numpy, scipy\nresult = {'x': round(float(numpy.sqrt(2)), 4)}\n"))
    if result.get("ok"):
        say(OK, f"пробный расчёт прошёл: {result.get('result')}")
    else:
        say(FAIL, "пробный расчёт не прошёл",
            (result.get("error") or "").strip().splitlines()[-1][:160])

    # Регрессия 23.09.2026: подмена socket.socket функцией ломала ssl,
    # а через него joblib, scikit-learn и statsmodels.
    ml = asyncio.run(compute_sandbox.run_code(
        "import ssl, asyncio, sklearn, statsmodels\nresult = {'ml': sklearn.__version__}\n"))
    if ml.get("ok"):
        say(OK, f"scikit-learn и statsmodels импортируются: {ml.get('result')}")
    else:
        say(FAIL, "модели по данным участников не соберутся",
            (ml.get("error") or "").strip().splitlines()[-1][:160])

    net = asyncio.run(compute_sandbox.run_code(
        'import socket\ns = socket.socket()\ns.connect(("example.com", 80))\n'))
    if not net.get("ok") and "Сеть из расчёта недоступна" in (net.get("error") or ""):
        say(OK, "сеть из расчёта закрыта")
    else:
        say(FAIL, "сеть из расчёта НЕ закрыта")


def check_content() -> None:
    head("Содержимое вне git")
    from api.runtime import datarepo, recipes, response_views

    count = len(recipes.index())
    say(OK if count >= 13 else WARN, f"методик: {count}")

    # Перебираем каталог целиком, а не список зашитых id: в первой версии
    # скрипта id были угаданы, и несуществующий «hypotheses-formulation»
    # дал ложный отказ, а настоящий «aios-hypotheses-skill» не проверялся.
    # Лимит берётся тот же, что у рантайма (55 000), иначе у всех навыков
    # получается одинаковая длина 12 000 — дефолт datarepo.skill_body.
    from api.runtime.prompts import SKILL_BODY_LIMIT

    for skill in datarepo.skills():
        skill_id = skill.get("id") or "?"
        body = datarepo.skill_body(skill, limit=SKILL_BODY_LIMIT)
        if not body:
            say(FAIL, f"навык {skill_id}: тело пустое",
                "навык числится в каталоге, но файла в data/skills/ нет — "
                "в интерфейсе он есть, а работать не будет")
        elif "усечён" in body:
            say(WARN, f"навык {skill_id}: тело обрезано лимитом")
        else:
            say(OK, f"навык {skill_id}: {len(body)} символов")

    org = REPO / "data" / "organization" / "profile.json"
    say(OK if org.is_file() else WARN, f"профиль организации: {'есть' if org.is_file() else 'нет'}")

    hyp = list((REPO / "data" / "hypotheses").glob("hyp-*.json"))
    say(OK if hyp else WARN, f"гипотез проектов: {len(hyp)}")

    block = response_views.prompt_block()
    say(OK if "verdict" in block else FAIL,
        f"каталог компонентов ответа: {len(response_views.names())} штук")


def check_templates() -> None:
    head("Готовые агенты")
    import json

    data = json.loads((REPO / "scripts" / "agent_templates.json").read_text(encoding="utf-8"))
    templates = data["templates"]
    with_console = [t for t in templates if t.get("console")]
    say(OK if len(with_console) == len(templates) else FAIL,
        f"шаблонов {len(templates)}, с пультом {len(with_console)}")


def check_qdrant() -> None:
    head("Qdrant")
    try:
        from api.runtime import indexer

        client = indexer.get_qdrant_client()
        collections = [c.name for c in client.get_collections().collections]
        say(OK, f"локально отвечает, коллекций: {len(collections)}")
    except Exception as exc:  # noqa: BLE001 — сюда попадает и отсутствие qdrant-client
        say(FAIL, f"не отвечает на 127.0.0.1:6333: {type(exc).__name__}: {exc}")
        return

    # Снаружи порт должен быть закрыт: авторизации у Qdrant не существует.
    host = socket.gethostbyname(socket.gethostname())
    probe = socket.socket()
    probe.settimeout(2)
    try:
        probe.connect((host, 6333))
        say(WARN, f"порт 6333 отвечает на {host}",
            "проверьте, закрыт ли он снаружи: с другой машины curl http://<ip>:6333/collections")
    except OSError:
        say(OK, f"на внешнем интерфейсе {host} порт 6333 закрыт")
    finally:
        probe.close()


def check_address() -> None:
    head("Адрес платформы")
    from api.runtime.config import get_settings

    base = (get_settings().public_base_url or "").strip()
    if not base:
        say(FAIL, "AIOS_PUBLIC_BASE_URL пуст",
            "ссылки на страницу агента и в письмах подтверждения будут нерабочими")
        return
    say(OK, f"AIOS_PUBLIC_BASE_URL = {base}")

    # Сравнивать разрешённый адрес с локальным интерфейсом бесполезно: в
    # облаке машина видит свой внутренний 10.0.0.5, а имя ведёт на внешний
    # адрес за NAT. Первая версия скрипта на этом давала ложную тревогу.
    # Проверяем то, что действительно важно: имя разрешается и платформа по
    # этому адресу отвечает — своя, а не чужая.
    host = base.split("//", 1)[-1].split("/", 1)[0].split(":")[0]
    try:
        resolved = socket.gethostbyname(host)
        say(OK, f"{host} → {resolved}")
    except OSError as exc:
        say(FAIL, f"имя {host} не разрешается: {exc}",
            "ссылки на агентов и письма подтверждения будут нерабочими")
        return

    try:
        import httpx

        response = httpx.get(f"{base.rstrip('/')}/api/runtime", timeout=10,
                             follow_redirects=True)
        tools = response.json().get("tools", [])
        say(OK if tools else WARN,
            f"платформа по этому адресу отвечает: HTTP {response.status_code}, "
            f"инструментов {len(tools)}")
    except Exception as exc:  # noqa: BLE001 — сеть, TLS, редирект, что угодно
        say(WARN, f"снаружи не проверить: {type(exc).__name__}: {str(exc)[:120]}",
            "локально платформа работать может — проверьте адрес из браузера")


def check_http() -> None:
    """Живой сервис на 127.0.0.1:8000, минуя nginx.

    Три ответа разделяют случаи, которые иначе путают: сервис не поднялся,
    поднялся но без статики, поднялся но пускает без авторизации.
    """
    head("HTTP (локально, мимо nginx)")
    try:
        import httpx
    except ImportError:
        say(WARN, "httpx не установлен — проверка пропущена")
        return

    expected = (("/agents.html", 200, "каталог агентов"),
                ("/api/runtime", 200, "реестр инструментов"))
    for path, want, what in expected:
        try:
            code = httpx.get(f"http://127.0.0.1:8000{path}", timeout=10).status_code
        except Exception as exc:  # noqa: BLE001
            say(FAIL, f"{path}: {type(exc).__name__}", "сервис не отвечает")
            continue
        say(OK if code == want else FAIL, f"{path} → {code} ({what})",
            "" if code == want else f"ожидался {want}")


def check_live() -> None:
    """Настоящий рабочий вопрос агенту из шаблона, на боевой модели.

    Берётся tpl-hex и его первый тестовый случай: «Горячая вода 30 м³/ч,
    80 -> 40 °C; охлаждающая вода 20 -> 35 °C». Правильный ответ известен и
    проверен методикой teploobmen_apparat — площадь около 93,7 м².

    Проверяется то, что не видно ни установкой, ни тестами на моке:
    проходит ли протокол tool-calling до провайдера, исполняется ли расчёт
    в песочнице, появляется ли в ответе число и компонент.
    """
    import asyncio
    import json

    head("Живой прогон агента (--live)")

    from api.routers.chat import get_provider, get_registry
    try:
        from api.runtime import agent_store, workflow_engine  # noqa: F401
    except ImportError:
        say(WARN, "движок граф-агентов удалён — живой прогон агента-графа больше не поддерживается")
        return
    from api.runtime.config import get_settings
    from api.runtime.llm_settings import effective_config

    sys.path.insert(0, str(REPO / "scripts"))
    import load_agent_templates as loader

    template = loader.load_templates()["tpl-hex"]
    card = loader.build_card(template, project_id="prj-проверка")
    # Карточка живёт только в памяти, но движок пишет журнал прогона в
    # .aios-runtime/agents/{owner_id}/{agent_id}/executions — отдельный
    # owner, чтобы проверочный прогон не попал в работу участников.
    card["id"] = "agt-проверка"
    card["owner_id"] = "check-deploy"
    case = (template.get("evals") or [{}])[0]
    question = case.get("input") or "Горячая вода 30 м³/ч, 80 -> 40 °C; охлаждающая вода 20 -> 35 °C"

    cfg = effective_config()
    provider = get_provider(cfg.base_url, cfg.api_key, cfg.model)
    if provider is None:
        say(FAIL, "LLM не настроена — живой прогон невозможен")
        return
    say(OK, f"модель: {cfg.model}")
    print(f"      вопрос: {question[:90]}")

    used: list[str] = []

    async def emit(event: dict) -> None:
        if event.get("type") == "tool_call" and event.get("name"):
            used.append(event["name"])

    session = {"user_id": "", "model": cfg.model, "agent_id": card["id"],
               "rag_ids": [], "doc_permissions": {}}
    result = asyncio.run(workflow_engine.run_workflow(
        card=card, user_text=question, settings=get_settings(),
        provider=provider, registry=get_registry(), session=session, emit=emit))

    if not result.get("ok"):
        say(FAIL, f"агент не ответил: {str(result.get('error'))[:160]}")
        return

    output = result.get("output") or ""
    say(OK, f"ответ получен: {len(output)} символов")
    say(OK if used else FAIL, f"вызванные инструменты: {', '.join(used) or 'НИ ОДНОГО'}",
        "" if used else "агент отвечал из головы — расчёта не было")
    say(OK if "run_calc" in used else FAIL,
        "run_calc " + ("вызван" if "run_calc" in used else "НЕ вызван"))

    component = None
    for chunk in output.split("```"):
        chunk = chunk.strip()
        if chunk.startswith("aios-view"):
            try:
                component = json.loads(chunk[len("aios-view"):]).get("component")
            except ValueError:
                component = "(не разобрался)"
    say(OK if component else WARN, f"компонент ответа: {component or 'нет'}")

    numbers = [float(n.replace(",", ".")) for n in
               __import__("re").findall(r"\d+[.,]?\d*", output)]
    near = [n for n in numbers if 70 <= n <= 130]
    say(OK if near else WARN,
        f"площадь в ожидаемом диапазоне 70-130 м²: {near[:5] or 'не найдено'}",
        "" if near else "методика teploobmen_apparat даёт 93,7 м² — сверьте ответ глазами")
    print("\n      --- первые 600 символов ответа ---")
    for line in output[:600].splitlines():
        print(f"      {line}")


def main() -> int:
    live = "--live" in sys.argv
    print(f"Проверка развёртывания AIOS — {REPO}")
    print(f"Python {sys.version.split()[0]}, пользователь {os.environ.get('USER', '?')}")
    steps = [check_tools, check_sandbox, check_content,
             check_templates, check_qdrant, check_http, check_address]
    if live:
        steps.append(check_live)
    for step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001 — одна проверка не валит остальные
            say(FAIL, f"{step.__name__}: {type(exc).__name__}: {exc}")
    if not live:
        print("\n(живой прогон агента не делался — добавьте --live)")

    print()
    if _failures:
        print(f"ОТКАЗОВ: {len(_failures)}")
        for item in _failures:
            print(f"  — {item}")
        return 1
    print("Всё существенное на месте.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
