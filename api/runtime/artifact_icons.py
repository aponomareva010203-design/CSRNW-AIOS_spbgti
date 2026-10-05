"""Индивидуальные SVG-логотипы артефактов (api/runtime/artifact_icons.py).

Онтологии, RAG-базы и агенты получают уникальный логотип в момент создания:
языковая модель рисует минималистичную пиктограмму по содержанию карточки
(название, описание, домен, теги), файл сохраняется в
data/icons/{artifact_id}.svg (публично отдаётся статикой как
/data/icons/{artifact_id}.svg) и подставляется:

  - в карточки каталогов data/ontologies.json, data/rag_bases.json,
    data/user_agents.json — поле image_url в catalog_entry каждого
    хранилища (файл есть → его URL, нет → прежний плейсхолдер);
  - в приватную карточку агента — поле icon (inline-SVG на /agent.html,
    AgentStore.set_icon без bump версии/history).

Генерация фоновая (schedule_generation из инструментов-конструкторов):
вызов LLM не задерживает ответ пользователю. Ответ модели проходит строгий
allowlist по тегам/атрибутам (sanitize_svg из project_icons) — модель здесь
не доверенный источник разметки. Логотип генерируется один раз и не
перегенерируется при правках карточки: это обложка артефакта, а не снимок
его текущего состояния.
"""

import asyncio
import json
import re
from pathlib import Path

from .config import get_settings
from .project_icons import _current_provider, sanitize_svg

# виды артефактов; kind задает акцент промпта — при одинаковой стилистике
# композиция должна читаться как «онтология», «база знаний», «агент»
_KIND_BRIEFS = {
    "ontology": (
        "Это логотип ОНТОЛОГИИ — графа знаний предметной области. Образ "
        "должен отражать конкретную область онтологии (её объекты и "
        "процессы), а композиция — сетевую природу: несколько "
        "узлов-кружков, соединённых линиями, либо ячеистая структура, "
        "внутри или рядом с образом области."
    ),
    "rag": (
        "Это логотип RAG-базы знаний — корпуса документов с поиском по "
        "смыслу. Образ должен отражать тематику корпуса; композиция может "
        "использовать мотив документов/слоёв/строк текста, лупы или "
        "точек-фрагментов, соединённых с образом темы."
    ),
    "agent": (
        "Это логотип ИИ-АГЕНТА — цифрового помощника с конкретной ролью "
        "(подбор рецептур, обзор литературы, координация работ и т.п.). "
        "Образ должен отражать роль агента; композиция может включать "
        "мотив искры/импульса/узла, реагирующего на запрос."
    ),
    "skill": (
        "Это логотип платформенного ИИ-АГЕНТА (навыка AIOS): конструкторы "
        "онтологий и RAG-баз, поиск источников, обратная связь. Образ "
        "должен отражать назначение навыка; композиция может включать "
        "мотив искры/импульса или узла, реагирующего на запрос."
    ),
}

_KIND_TITLES = {
    "ontology": "Онтология",
    "rag": "RAG-база знаний",
    "agent": "ИИ-агент",
    "skill": "Платформенный ИИ-агент (навык)",
}

_KIND_CATALOGS = {
    "ontology": "ontologies",
    "rag": "rag",
    "agent": "agent_store",
}

ARTIFACT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")

STYLE_PROMPT = (
    "Ты рисуешь минималистичные плоские SVG-логотипы для карточек "
    "артефактов научно-технологической платформы — в стиле лаконичных "
    "линейных пиктограмм (как в icon-наборах Feather/Heroicons): НЕ "
    "фотореалистично, без текста и букв, без сложных сцен и мелких "
    "деталей — один узнаваемый ассоциативный символ, различимый "
    "в размере ~48px.\n"
    "Рисуй образ простыми геометрическими примитивами (path, circle, "
    "rect, line, polyline, polygon, ellipse), 1-3 формы, без штриховки "
    "и мелких деталей.\n"
    "Геометрия — вся фигура должна быть центрирована и умещаться в "
    "прямоугольник x: 8-56, y: 8-56 внутри viewBox 0 0 64 64, с отступом "
    "от края со всех сторон, не смещена к какому-либо краю или углу.\n"
    "Ответ — ТОЛЬКО валидный SVG-код одним фрагментом <svg ...>...</svg>, "
    "без markdown-обрамления, комментариев и пояснений. viewBox=\"0 0 64 64\". "
    "Разрешены только теги svg, g, path, circle, rect, line, polyline, "
    "polygon, ellipse, defs, linearGradient, radialGradient, stop — никаких "
    "script, style, image, foreignObject, a, use, text и обработчиков on*. "
    "Палитра — только эти цвета (подбирай 2-4 по смыслу темы: "
    "природные/сырьевые темы — больше зелёных и золотых тонов; "
    "технологические/точные — больше тёмного и светлого фона с акцентным "
    "золотым): #7C8B6C (пыльный зелёный), #5D6C50 (глубокий зелёный), "
    "#C2A054 (золотой), #E9DCBB (светлый золотой), #2C3128 (тёмный), "
    "#F4F6EF (светлый фон). Толщина линий/обводок — 1.5-3px."
)


# ----- файлы логотипов -----

def _icons_dir() -> Path:
    return get_settings().icon_data_dir / "icons"


def logo_path(artifact_id: str) -> Path | None:
    """Путь к файлу логотипа артефакта; None при некорректном id
    (защита от ../ в идентификаторе)."""
    artifact_id = str(artifact_id or "").strip()
    if not ARTIFACT_ID_RE.match(artifact_id):
        return None
    return _icons_dir() / f"{artifact_id}.svg"


def logo_url(artifact_id: str) -> str | None:
    """Публичный URL существующего логотипа; None — логотипа ещё нет."""
    path = logo_path(artifact_id)
    if path is None or not path.is_file():
        return None
    return f"/data/icons/{artifact_id}.svg"


def store_logo(artifact_id: str, svg: str) -> str:
    """Атомарно сохраняет SVG и возвращает его публичный URL."""
    path = logo_path(artifact_id)
    if path is None:
        raise ValueError(f"Некорректный идентификатор артефакта: {artifact_id}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(svg, encoding="utf-8")
    tmp.replace(path)
    return f"/data/icons/{artifact_id}.svg"


# ----- генерация -----

def _render_task(kind: str, card: dict) -> str:
    tags = card.get("tags")
    if isinstance(tags, (list, tuple)):
        tags = ", ".join(str(t) for t in tags if t)
    lines = [
        f"Тип артефакта: {_KIND_TITLES.get(kind, kind)}",
        f"Название: {card.get('name') or card.get('title') or '—'}",
        f"Домен/область: {card.get('domain') or card.get('project') or '—'}",
        f"Описание: {card.get('description') or card.get('description_ru') or card.get('role_description') or '—'}",
    ]
    if card.get("classes_count"):
        lines.append(f"Классов в графе: {card['classes_count']}")
    if card.get("documents_count"):
        lines.append(f"Документов в корпусе: {card['documents_count']}")
    if tags:
        lines.append(f"Теги: {tags}")
    lines.append(
        "Определи один главный ассоциативный образ по смыслу артефакта "
        "и нарисуй его в описанном минималистичном стиле, по правилам "
        "геометрии и палитры выше."
    )
    return "\n".join(lines)


async def generate_logo(
    kind: str, card: dict, provider=None, user_id: str | None = None
) -> str | None:
    """Один вызов LLM → sanitize_svg; None при сбое или мусорном ответе.

    Как и у иконок проектов, расход токенов не учитывается персонально —
    это декоративная генерация (но по личному ключу пользователя, если
    такие настройки заданы).
    """
    if kind not in _KIND_BRIEFS:
        return None
    provider = provider if provider is not None else _current_provider(user_id)
    if provider is None:
        return None
    messages = [
        {
            "role": "system",
            "content": _KIND_BRIEFS[kind] + "\n" + STYLE_PROMPT,
        },
        {"role": "user", "content": _render_task(kind, card)},
    ]
    chunks: list[str] = []
    try:
        # max_tokens не ограничиваем: у reasoning-моделей лимит съедается
        # размышлениями и ответ (сам SVG) не успевает родиться
        async for kind_evt, payload in provider.stream_chat(messages, tools=None):
            if kind_evt == "text":
                chunks.append(payload)
    except Exception:  # noqa: BLE001 — фоновая декорация не должна ронять цикл
        return None
    return sanitize_svg("".join(chunks))


# ----- применение к каталогам и карточкам -----

def _catalog_path(kind: str) -> Path | None:
    """Путь к JSON-каталогу опубликованных артефактов этого вида.

    Хранилища импортируются лениво — сам artifact_icons подключается
    модулями хранилищ на верхнем уровне, прямой импорт зациклил бы их.
    """
    module_name = _KIND_CATALOGS.get(kind)
    if module_name is None:
        return None
    import importlib

    module = importlib.import_module(f".{module_name}", package=__package__)
    store = getattr(module, "get_store")()
    return getattr(store, "catalog_path", None)


def patch_catalog(kind: str, artifact_id: str, url: str) -> bool:
    """Прописывает image_url в уже опубликованную карточку каталога.

    Логотип генерируется фоном уже после того, как publish() записал
    карточку с плейсхолдером; здесь мы точечно заменяем image_url, не
    перепублизовывая граф (карточка могла успеть измениться). True —
    карточка найдена и обновлена.
    """
    catalog_path = _catalog_path(kind)
    if catalog_path is None:
        return False
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    items = catalog.get("items") if isinstance(catalog, dict) else None
    if not isinstance(items, list):
        return False
    found = False
    for item in items:
        if isinstance(item, dict) and item.get("id") == artifact_id:
            item["image_url"] = url
            found = True
    if not found:
        return False
    tmp = catalog_path.with_suffix(catalog_path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    tmp.replace(catalog_path)
    return True


async def generate_and_apply(
    kind: str, card: dict, user_id: str | None = None, provider=None
) -> str | None:
    """Идемпотентная генерация «до конца»: файл + каталог + карточка агента.

    Возвращает публичный URL логотипа либо None (нет LLM, сбой вызова,
    мусорный ответ). Все ошибки проглатываются — это фоновая декорация:
    артефакт остается с плейсхолдером, а не падает операция создания.
    """
    artifact_id = str(card.get("id") or "").strip()
    if not artifact_id:
        return None
    existing = logo_url(artifact_id)
    if existing:
        # логотип уже есть (повторное сохранение черновика) — только
        # гарантируем, что каталог ссылается на него
        try:
            patch_catalog(kind, artifact_id, existing)
        except Exception:  # noqa: BLE001
            pass
        return existing
    svg = await generate_logo(kind, card, provider=provider, user_id=user_id)
    if svg is None:
        return None
    try:
        url = store_logo(artifact_id, svg)
    except ValueError:
        return None
    if kind == "agent" and user_id:
        try:
            from .agent_store import get_store as get_agent_store

            get_agent_store().set_icon(user_id, artifact_id, svg)
        except Exception:  # noqa: BLE001
            pass
    try:
        patch_catalog(kind, artifact_id, url)
    except Exception:  # noqa: BLE001
        pass
    return url


# ----- фоновый запуск -----

_background_tasks: set[asyncio.Task] = set()


def schedule_generation(kind: str, card: dict, user_id: str | None = None) -> None:
    """Планирует фоновую генерацию логотипа; вне event-loop — no-op.

    Вызывается инструментами save_ontology/save_rag сразу
    после сохранения: ответ пользователю не ждёт LLM-рисование, логотип
    появляется в каталоге через несколько секунд.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _run() -> None:
        try:
            await generate_and_apply(kind, card, user_id=user_id)
        except Exception:  # noqa: BLE001 — см. generate_and_apply
            pass

    task = loop.create_task(_run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# ----- бэкфилл существующих артефактов (scripts/generate_artifact_icons.py) -----

def iter_existing(kind: str) -> list[dict]:
    """Все карточки артефактов вида kind: опубликованные каталоги + черновики.

    Для опубликованных берём карточку каталога (в ней описание/теги уже
    собраны), для черновиков — приватную карточку владельца; дубликаты по
    id исключаются (каталог приоритетнее — он то, что видят пользователи).
    """
    cards: dict[str, dict] = {}
    catalog_path = _catalog_path(kind)
    if catalog_path is not None:
        try:
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            items = catalog.get("items") if isinstance(catalog, dict) else None
            for item in items or []:
                if isinstance(item, dict) and item.get("id"):
                    cards[str(item["id"])] = dict(item)
        except (OSError, ValueError):
            pass
    if kind in ("ontology", "rag"):
        import importlib

        module = importlib.import_module(
            f".{_KIND_CATALOGS[kind]}", package=__package__
        )
        for card in getattr(module, "get_store")().list_all():
            if card.get("id") and str(card["id"]) not in cards:
                cards[str(card["id"])] = card
    elif kind == "agent":
        from .agent_store import get_store as get_agent_store

        own_cards = {
            str(c["id"]): c
            for c in get_agent_store().list_all()
            if c.get("id")
        }
        for cid, own in own_cards.items():
            if cid in cards:
                # каталожная запись приоритетнее по контенту, но владелец
                # нужен, чтобы set_icon дописал логотип в приватную карточку
                cards[cid]["owner_id"] = own.get("owner_id")
            else:
                cards[cid] = own
    return list(cards.values())


def apply_agent_icon(user_id: str, agent_id: str, svg: str) -> bool:
    """Обёртка для бэкфилла: пишет icon в приватную карточку агента."""
    from .agent_store import get_store as get_agent_store

    return get_agent_store().set_icon(user_id, agent_id, svg)
