"""Сборка системного промпта: базовая роль + выбранный агент + выбранные навыки.

Тексты промптов вынесены в отдельные файлы рядом с пакетом:
    base.md   — базовый системный промпт ассистента «Рабочего пространства»;
    agent.md  — шаблон блока роли агента ({name}, {provider}, {description},
                {tags}, {access_url});
    skill.md  — шаблон блока навыка ({name}, {id}, {body}).

Шаблоны используют подстановки str.format и читаются один раз за процесс
(lru_cache) — изменения файлов применяются после перезапуска сервиса.
Каталог промптов лежит внутри api/runtime/ и не раздаётся публично
(в отличие от data/). Агенты и навыки берутся из data/agents.json и
data/skills.json — те же карточки, что видны на страницах приложения.
Текст навыка подгружается из markdown-файла (поле file_url карточки).
"""

from functools import lru_cache
from pathlib import Path

from .. import datarepo

# Самый большой скилл («Формулировка гипотез», ~53 000 символов) должен
# попадать в промпт целиком — усечение посреди диалоговых шагов ломает навык.
SKILL_BODY_LIMIT = 55_000

_PROMPTS_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def _load_template(name: str) -> str:
    """Читает файл промпта из каталога пакета (один раз за процесс)."""
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


def build_system_prompt(agent_id: str | None = None, skill_ids: list | None = None) -> str:
    """Собирает системный промпт сессии.

    Неизвестные id агентов/навыков молча пропускаются — валидация запроса
    выполняется на роутере.
    """
    parts = [_load_template("base.md").strip()]

    agent = datarepo.get_agent(agent_id) if agent_id else None
    if agent:
        parts.append(
            _load_template("agent.md").format(
                name=agent.get("name", agent_id),
                provider=agent.get("provider", "—"),
                description=agent.get("description_ru", ""),
                tags=", ".join(agent.get("tags") or []) or "—",
                access_url=agent.get("access_url") or "—",
            ).strip()
        )

    for skill_id in skill_ids or []:
        skill = datarepo.get_skill(skill_id)
        if not skill:
            continue
        body = datarepo.skill_body(skill, limit=SKILL_BODY_LIMIT)
        parts.append(
            _load_template("skill.md").format(
                name=skill.get("name", skill_id),
                id=skill.get("id", skill_id),
                body=body or skill.get("description_ru", ""),
            ).strip()
        )

    return "\n\n".join(parts)
