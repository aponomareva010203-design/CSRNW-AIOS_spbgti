"""Индивидуальная SVG-иконка карточки проекта (api/runtime/project_icons.py).

Генерируется один раз языковой моделью по названию/описанию/цели проекта
(в стиле абстрактных узловых значков онтологий/RAG-баз — не буквальная
иллюстрация, а декоративный акцент), сохраняется в карточке проекта
(поле icon_svg) и переиспользуется всеми, кто видит список «Проекты» —
генерация не привязана к владельцу: иконка декоративна и общая.

Ответ модели проходит строгий allowlist по тегам/атрибутам (sanitize_svg)
перед сохранением и встраиванием в страницу как inline-SVG — модель здесь
не доверенный источник HTML.
"""

import re
import xml.etree.ElementTree as ET

from .config import get_settings
from .llm_settings import effective_config_for
from .providers import build_provider

ALLOWED_TAGS = {
    "svg", "g", "path", "circle", "rect", "line", "polyline", "polygon",
    "ellipse", "defs", "lineargradient", "radialgradient", "stop",
}

# canonical (правильный регистр) для атрибутов, где регистр значим в SVG;
# остальные допустимые атрибуты — как есть в нижнем регистре (fill, stroke…)
_ATTR_CANONICAL = {
    "viewbox": "viewBox",
    "gradientunits": "gradientUnits",
    "gradienttransform": "gradientTransform",
    "stopcolor": "stop-color",
}

ALLOWED_ATTRS = {
    "viewbox", "xmlns", "width", "height", "fill", "stroke", "stroke-width",
    "stroke-linecap", "stroke-linejoin", "opacity", "fill-opacity",
    "stroke-opacity", "cx", "cy", "r", "rx", "ry", "x", "y", "x1", "y1",
    "x2", "y2", "points", "d", "transform", "offset", "stop-color",
    "stop-opacity", "id", "gradientunits", "gradienttransform",
}

_SVG_RE = re.compile(r"<svg\b.*?</svg>", re.IGNORECASE | re.DOTALL)
MAX_SVG_LEN = 20_000


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def sanitize_svg(raw: str) -> str | None:
    """Строгий allowlist по тегам/атрибутам; None — модель отдала не то,
    что просили (мусор, разметку с script/style/image/on*-обработчиками и
    т.п.), и иконку просто не сохраняем — карточка остаётся с плейсхолдером.
    """
    if not raw:
        return None
    match = _SVG_RE.search(raw)
    if not match:
        return None
    snippet = match.group(0)
    if len(snippet) > MAX_SVG_LEN:
        return None
    try:
        root = ET.fromstring(snippet)
    except ET.ParseError:
        return None
    if _local(root.tag) != "svg":
        return None

    def clean(el: ET.Element) -> bool:
        tag = _local(el.tag)
        if tag not in ALLOWED_TAGS:
            return False
        el.tag = tag  # снять namespace — сериализуем как обычные теги
        for attr in list(el.attrib):
            key = _local(attr)
            value = el.attrib.pop(attr)
            if key not in ALLOWED_ATTRS or key.startswith("on"):
                continue
            if "javascript:" in value.lower() or "data:" in value.lower():
                continue
            el.set(_ATTR_CANONICAL.get(key, key), value)
        for child in list(el):
            if not clean(child):
                el.remove(child)
        return True

    if not clean(root):
        return None
    root.set("xmlns", "http://www.w3.org/2000/svg")
    if not any(_local(k) == "viewbox" for k in root.attrib):
        root.set("viewBox", "0 0 64 64")
    return ET.tostring(root, encoding="unicode")


SYSTEM_PROMPT = (
    "Ты рисуешь минималистичные плоские SVG-иконки для карточек проектов "
    "научно-технологической платформы — в стиле лаконичных линейных "
    "пиктограмм (как в icon-наборах Feather/Heroicons): НЕ фотореалистично, "
    "без текста, без сложных сцен и мелких деталей — один узнаваемый "
    "ассоциативный символ.\n"
    "Перед тем как рисовать, определи по названию/описанию/цели проекта "
    "ОДИН главный конкретный образ, ассоциирующийся с темой — материал, "
    "объект, процесс или явление, которое лучше всего передаёт суть "
    "проекта. Примеры уровня абстракции (не готовый список, придумывай "
    "свой образ под конкретную тему): волокно/композитный материал → "
    "несколько изогнутых параллельных линий, сплетённых в пучок или "
    "прядь; молекула/химический синтез → несколько кружков-атомов, "
    "соединённых линиями, как структурная формула; биология/клетка → "
    "овал с 1-2 внутренними элементами; производство/оборудование → "
    "простой силуэт детали, шестерни или сосуда; поток/процесс/энергия → "
    "path со стрелкой или спиралью.\n"
    "Рисуй образ простыми геометрическими примитивами (path, circle, "
    "rect, line, polyline, polygon, ellipse), 1-3 формы, без штриховки и "
    "мелких деталей, различимо в размере ~48px.\n"
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


def _render_task(project: dict) -> str:
    lines = [
        f"Название проекта: {project.get('title') or '—'}",
        f"Описание: {project.get('description') or '—'}",
        f"Цель: {project.get('goal') or '—'}",
        "Определи один главный ассоциативный образ по смыслу проекта "
        "(материал/процесс/явление) и нарисуй его в описанном "
        "минималистичном стиле, по правилам геометрии и палитры выше.",
    ]
    return "\n".join(lines)


def _current_provider(user_id: str | None = None):
    """Провайдер из действующих настроек LLM; None — LLM не настроена.

    У пользователя с личными настройками внешнего API — его endpoint и ключ.
    """
    settings = get_settings()
    return build_provider(
        effective_config_for(user_id), read_timeout=settings.request_timeout
    )


async def generate_icon(project: dict, provider=None, user_id: str | None = None) -> str | None:
    """Один вызов LLM → sanitize_svg; None при сбое или мусорном ответе.

    Расход токенов не учитывается персонально — это декоративная генерация,
    не связанная с диалогом пользователя (но по его личному ключу, если
    такие настройки заданы).
    """
    provider = provider if provider is not None else _current_provider(user_id)
    if provider is None:
        return None
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _render_task(project)},
    ]
    chunks: list[str] = []
    async for kind, payload in provider.stream_chat(messages, tools=None, max_tokens=900):
        if kind == "text":
            chunks.append(payload)
    return sanitize_svg("".join(chunks))
