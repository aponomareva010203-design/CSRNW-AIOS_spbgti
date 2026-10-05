"""Библиотека инженерных методик института (api/runtime/recipes.py).

Программа ПИШ «Химический инжиниринг» ставит задачу «создать библиотеку
проверенных моделей вещества и процесса». Это она и есть, в той части,
которая доступна агенту прямо сейчас: не обученные модели, а проверенные
ПОРЯДКИ РАСЧЁТА — что считать, в какой последовательности, какими
функциями, где типовые ошибки и какие пороги означают «так делать нельзя».

Зачем отдельный механизм, а не текст в системном промпте агента:

  • промпт один на агента и уходит в модель при КАЖДОМ запросе; двенадцать
    методик в нём — это лишние тысячи токенов на каждый вопрос, включая
    вопросы, где расчёт вообще не нужен;
  • методик со временем станут десятки (по пяти кластерам ПИШ), и в промпт
    они не поместятся физически;
  • методику правит технолог, а не программист: это markdown-файл в
    data/recipes/, правка не требует ни деплоя, ни перезапуска.

Поэтому — прогрессивное раскрытие: модель всегда видит короткий список
(имя + одна строка, когда применять), а полный текст запрашивает по
надобности. Тот же приём, что в открытом стандарте Agent Skills.

Живой прогон 22 сентября показал, зачем это нужно практически: модель
потратила два обращения из четырёх на угадывание сигнатур ht, а третье —
на inspect.signature, и человек ждал лишние сорок секунд. Методика даёт
правильный вызов сразу.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from .config import get_settings

NAME_RE = re.compile(r"^[a-z0-9_\-]{2,60}$")
MAX_BODY_CHARS = 12_000


def recipes_dir() -> Path:
    return get_settings().tools_root / "recipes"


def _parse(path: Path) -> dict | None:
    """Методика = markdown с двумя первыми строками-заголовками.

    # Название методики
    > когда применять — одна строка, попадает в список

    Дальше — свободный текст: порядок расчёта, код, типовые ошибки.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    title = ""
    when = ""
    for line in lines[:6]:
        stripped = line.strip()
        if not title and stripped.startswith("# "):
            title = stripped[2:].strip()
        elif not when and stripped.startswith("> "):
            when = stripped[2:].strip()
    if not title:
        title = path.stem
    return {
        "name": path.stem,
        "title": title,
        "when": when,
        "body": text[:MAX_BODY_CHARS],
    }


@lru_cache(maxsize=1)
def _load() -> dict[str, dict]:
    directory = recipes_dir()
    if not directory.is_dir():
        return {}
    found: dict[str, dict] = {}
    for path in sorted(directory.glob("*.md")):
        if not NAME_RE.match(path.stem):
            continue
        parsed = _parse(path)
        if parsed:
            found[parsed["name"]] = parsed
    return found


def reload_recipes() -> int:
    """Перечитать каталог: методику правит технолог, перезапуск не нужен."""
    _load.cache_clear()
    return len(_load())


def index() -> list[dict]:
    """Короткий список для промпта агента: имя + когда применять."""
    return [{"name": r["name"], "title": r["title"], "when": r["when"]}
            for r in _load().values()]


def get(name: str) -> dict | None:
    return _load().get(str(name or "").strip())


def prompt_block(limit: int = 40) -> str:
    """Блок для системного промпта: что вообще есть в библиотеке.

    Намеренно короткий — полный текст модель запросит инструментом.
    """
    items = index()[:limit]
    if not items:
        return ""
    lines = ["## Методики института", "",
             "Проверенные порядки расчёта. Прежде чем считать что-то из этого "
             "списка, прочитайте методику инструментом get_recipe — там метод, "
             "рабочий код и типовые ошибки:", ""]
    for item in items:
        lines.append(f"  {item['name']} — {item['when'] or item['title']}")
    return "\n".join(lines)
