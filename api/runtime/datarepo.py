"""Чтение JSON-активов data/: навыки-агенты, RAG-базы, онтологии.

Каталог считается «живыми» данными: файлы могут редактироваться (в том числе
инструментами агента), поэтому читаем с диска на каждый вызов — без кэша.

Рантайм-метаданные навыка (фразы-триггеры, этап конвейера, ссылки на
справочники) живут в YAML-frontmatter его .md-файла; карта этапов конвейера —
в stage-map.json. Оба файла тоже живые: кэш сбрасывается по mtime.
"""

import json
import re
from pathlib import Path

import yaml

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

# Порог длины запроса для подсказок навыков: короче — слишком шумно
SUGGEST_MIN_QUERY = 8

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)

# кэши метаданных по mtime файла: правка .md/.json подхватывается без рестарта
_frontmatter_cache: dict[str, tuple[float, dict]] = {}
_stage_map_cache: tuple[float, dict] | None = None


def load_items(filename: str) -> list:
    """Читает data/<filename> и возвращает items; любая ошибка → пустой список."""
    path = DATA_DIR / filename
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def skills() -> list:
    return load_items("skills.json")


def is_admin_only(skill: dict) -> bool:
    """Агент сопровождает закрытый админ-раздел (сейчас — «Конструктор
    моделей» и раздел «Модели»): обычным пользователям не показывается
    и не запускается; для админа — как остальные."""
    return bool(skill.get("admin_only"))


def rag_bases() -> list:
    return load_items("rag_bases.json")


def ontologies() -> list:
    return load_items("ontologies.json")


def get_skill(skill_id) -> dict | None:
    if not skill_id:
        return None
    return next((s for s in skills() if s.get("id") == skill_id), None)


def skill_path(skill: dict) -> Path | None:
    """Путь к .md-файлу навыка внутри data/ (file_url вида /data/… или
    стандартное место data/skills/{id}.md); None — файл не найден."""
    file_url = str(skill.get("file_url") or "").strip()
    data_root = DATA_DIR.resolve()
    if file_url:
        candidate = (DATA_DIR.parent / file_url.lstrip("/")).resolve()
        if candidate.is_file() and candidate.is_relative_to(data_root):
            return candidate
    candidate = (DATA_DIR / "skills" / f"{skill.get('id')}.md").resolve()
    if candidate.is_file() and candidate.is_relative_to(data_root):
        return candidate
    return None


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Разделяет YAML-frontmatter и тело markdown-файла навыка.

    Возвращает (meta, body); без блока «--- … ---» в начале файла —
    ({}, text). Битовый YAML не роняет вызов: метаданные просто пусты.
    """
    match = _FRONTMATTER_RE.match(text or "")
    if not match:
        return {}, text or ""
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        meta = None
    if not isinstance(meta, dict):
        meta = {}
    return meta, text[match.end():]


def skill_meta(skill_id: str) -> dict:
    """Рантайм-метаданные навыка из frontmatter его .md (кэш по mtime):
    triggers (фразы для подсказок), stage (этап конвейера), references
    (справочники для отложенной подгрузки), version, status."""
    skill = get_skill(skill_id)
    if skill is None:
        return {}
    path = skill_path(skill)
    if path is None:
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _frontmatter_cache.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    except OSError:
        meta = {}
    _frontmatter_cache[str(path)] = (mtime, meta)
    return meta


def suggest_skills(
    text: str, exclude: list | None = None, limit: int = 3, is_admin: bool = False
) -> list:
    """Навыки, чьи фразы-триггеры встречаются в тексте сообщения.

    Матчинг подстрокой без учёта регистра (для русского хватает основы слова:
    «онтолог» ловит «онтологии»). Активные навыки (exclude) не предлагаем —
    переход к новому навыку всё равно означает новый чат. Admin-only навыки
    предлагаются только роли «Администратор».
    """
    query = (text or "").strip().lower()
    if len(query) < SUGGEST_MIN_QUERY:
        return []
    excluded = {str(x) for x in (exclude or [])}
    found = []
    for skill in skills():
        if skill.get("id") in excluded:
            continue
        if is_admin_only(skill) and not is_admin:
            continue
        meta = skill_meta(skill.get("id") or "")
        triggers = meta.get("triggers")
        if not isinstance(triggers, list):
            continue
        if any(isinstance(t, str) and t.strip().lower() in query for t in triggers):
            found.append(
                {
                    "id": skill.get("id"),
                    "name": skill.get("name"),
                    "description_ru": skill.get("description_ru", ""),
                }
            )
        if len(found) >= limit:
            break
    return found


def stage_map() -> dict:
    """Карта этапов конвейера из data/stage-map.json (кэш по mtime).

    Формат: {"stages": [{"key", "name_ru", "order", "skills": [...],
    "next": "<key|None>"}]}. Правка файла подхватывается без рестарта —
    перенастройка конвейера не требует кода.
    """
    global _stage_map_cache
    path = DATA_DIR / "stage-map.json"
    try:
        mtime = path.stat().st_mtime
        if _stage_map_cache and _stage_map_cache[0] == mtime:
            return _stage_map_cache[1]
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"stages": []}
    stages = data.get("stages") if isinstance(data, dict) else None
    if not isinstance(stages, list):
        stages = []
    result = {"stages": stages}
    _stage_map_cache = (mtime, result)
    return result


def stage_entry(stage_key: str | None) -> dict | None:
    """Описание этапа конвейера по ключу."""
    if not stage_key:
        return None
    return next(
        (s for s in stage_map()["stages"] if s.get("key") == stage_key), None
    )


def stage_of_skill(skill_id: str | None) -> dict | None:
    """Этап конвейера, к которому относится навык (поле stage в frontmatter;
    whitelist — только ключи из stage-map.json)."""
    if not skill_id:
        return None
    meta = skill_meta(skill_id)
    return stage_entry(meta.get("stage"))


def stage_next(stage_key: str | None) -> dict | None:
    """Следующий этап конвейера (поле next текущего)."""
    entry = stage_entry(stage_key)
    if not entry or not entry.get("next"):
        return None
    return stage_entry(entry.get("next"))


def remove_skill(skill_id: str) -> bool:
    """Удаляет карточку агента из data/skills.json (атомарная запись).

    Модерация каталога «Агенты»: право проверяет роутер, здесь только
    механика. False — карточки с таким id не было.
    """
    path = DATA_DIR / "skills.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return False
    kept = [s for s in items if not (isinstance(s, dict) and s.get("id") == skill_id)]
    if len(kept) == len(items):
        return False
    data["items"] = kept
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return True


def remove_skill_file(skill: dict) -> bool:
    """Удаляет .md-файл агента (best-effort, только внутри data/)."""
    path = skill_path(skill)
    if path is None:
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def skill_body(skill: dict, limit: int = 55_000) -> str:
    """Текст навыка из markdown-файла без frontmatter (он — метаданные
    для подсказок и конвейера, а не часть исполняемого протокола).

    Лимит по умолчанию рассчитан на самый большой навык целиком (~53 000
    символов, «Формулировка гипотез»): усечение посреди диалоговых шагов
    ломает навык, поэтому меньше — только по явной просьбе вызывающего."""
    path = skill_path(skill)
    if path is None:
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    _, text = parse_frontmatter(text)
    text = text.lstrip("\r\n")
    if len(text) > limit:
        text = text[:limit] + "\n\n[…текст навыка усечён]"
    return text
