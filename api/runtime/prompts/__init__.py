"""Сборка системного промпта: базовая роль + выбранные навыки-агенты.

Тексты промптов вынесены в отдельные файлы рядом с пакетом:
    base.md   — базовый системный промпт ассистента «Рабочего пространства»;
    skill.md  — шаблон блока навыка-агента ({name}, {id}, {body}).

Шаблоны используют подстановки str.format и читаются один раз за процесс
(lru_cache) — изменения файлов применяются после перезапуска сервиса.
Каталог промптов лежит внутри api/runtime/ и не раздаётся публично
(в отличие от data/). Навыки-агенты берутся из data/skills.json — те же
карточки, что видны на странице «Агенты». Текст навыка подгружается из
markdown-файла (поле file_url карточки).
"""

from functools import lru_cache
from pathlib import Path

from .. import datarepo, skill_drafts

# Самый большой скилл («Формулировка гипотез», ~53 000 символов) должен
# попадать в промпт целиком — усечение посреди диалоговых шагов ломает навык.
SKILL_BODY_LIMIT = 55_000

_PROMPTS_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def _load_template(name: str) -> str:
    """Читает файл промпта из каталога пакета (один раз за процесс)."""
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


def approval_rules_block() -> str:
    """Блок правил подтверждений действий (approval.md).

    Подмешивается в системный промпт ТОЛЬКО когда gate включён
    (AIOS_APPROVAL_TOOLS непуст): выключенный gate не должен упоминаться
    в промпте — иначе модель ждёт карточек, которые никогда не появятся.
    """
    return _load_template("approval.md").strip()


def build_system_prompt(skill_ids: list | None = None, user_id: str | None = None) -> str:
    """Собирает системный промпт сессии.

    Неизвестные id навыков молча пропускаются — валидация запроса
    выполняется на роутере.
    """
    parts = [_load_template("base.md").strip()]

    for skill_id in skill_ids or []:
        if str(skill_id).startswith(skill_drafts.DRAFT_PREFIX):
            # личный черновик навыка (проверка из кабинета): виден только владельцу
            draft = skill_drafts.get_store().load_for_prompt(user_id, str(skill_id))
            if draft:
                draft_name, draft_body = draft
                parts.append(
                    _load_template("skill.md").format(
                        name=f"{draft_name} (черновик)", id=skill_id, body=draft_body
                    ).strip()
                )
            continue
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
        # Справочники навыка (references в frontmatter) не вшиваем в промпт:
        # отдаём моделью список путей — читает инструментом read_file по мере
        # необходимости. Держит промпт компактным, а bulky-материалы (шаблоны,
        # таблицы, контракты) — доступными.
        references = datarepo.skill_meta(skill_id).get("references")
        if isinstance(references, list) and references:
            lines = "\n".join(
                f"- {ref}" for ref in references if isinstance(ref, str) and ref.strip()
            )
            if lines:
                parts.append(
                    "Справочные материалы навыка «{name}»: читай инструментом "
                    "read_file, когда понадобятся детали (не загружай всё заранее):\n"
                    f"{lines}".format(name=skill.get("name", skill_id))
                )

    return "\n\n".join(parts)
