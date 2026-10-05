"""Личные черновики агентов (api/runtime/skill_drafts.py).

Агент платформы — обычный .md-сценарий, как остальные агенты каталога: любой
сотрудник запускает его в чате, без расписания, почты, копилки и личных
данных автора. Здесь — путь агента от черновика до публикации.

  черновик (приватно)  .aios-runtime/skills/{user_id}/{id}.md + {id}.json
        │  тест: чат принимает skill_ids=["draft:{id}"] только от владельца
        ▼
  публикация (кнопка в кабинете)  data/skills/{id}.md + карточка в data/skills.json
        │  снятие с публикации убирает публичные файлы, черновик остаётся
        ▼
  черновик

Почему модель не пишет файл целиком. Раньше конструктор собирал .md одним
вызовом write_file прямо в публичный data/skills/, и слабая модель на длинной
генерации разваливалась (в каталоге остался файл с «5 5 5 5» и «Token Limit»).
Теперь модель передаёт короткие поля (название, триггеры, шаги…), а файл
собирает render_md() по шаблону платформы: YAML сериализуется библиотекой,
инструменты сверяются с реестром, личное вычищается проверкой. Публикует
человек, модель в публичный data/ писать не может.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from threading import Lock

import yaml

from . import datarepo

DRAFT_PREFIX = "draft:"
MAX_MD_CHARS = 30_000                     # лимит конструктора; сборщик промпта берёт до 55 000
ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
ID_MIN, ID_MAX = 3, 48
CATEGORIES = ("special", "general")       # service — только платформенные навыки
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)

# Инструменты, которые в общем чате не работают: call_agent отвечает ошибкой
# «только внутри выполнения агента». notify, save_record и get_records в чате
# работают: письмо уходит пользователю, записи хранятся по (пользователь, навык).
AGENT_ONLY_TOOLS = ("call_agent",)

# Все имена инструментов, когда-либо бывшие в реестре: упоминание того, чего на
# этом сервере нет (например rxn_* без AIOS_RXN_API_KEY), — ошибка навыка.
KNOWN_TOOL_NAMES = frozenset("""
run_calc read_data_file literature_search compound_lookup make_document notify get_recipe
search_corpus search_knowledge get_catalog read_document read_file list_files write_file
attach_project_file get_projects save_project_card get_ontologies save_ontology get_rag
save_rag get_agents save_agent call_agent save_record get_records format_table chart_generate
get_organization chem_mol_convert chem_descriptors chem_functional_groups chem_similarity
chem_substructure chem_reaction_check chem_depict chem_control_chem_screen chem_safety_lookup
rxn_retrosynthesis rxn_forward save_skill_draft get_skill_drafts
get_models run_model optimize_model train_model save_model_fact
""".split())

PRIVATE_PATTERNS = (
    (re.compile(r"\bprj-[0-9a-f]{6,}\b"), "идентификатор проекта"),
    (re.compile(r"\bagt-[a-z0-9-]{4,}\b"), "идентификатор личного агента"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "адрес электронной почты"),
    (re.compile(r"\bAIOS_[A-Z_]{3,}\b"), "имя настройки платформы"),
)

CALC_RULES = """## Правила расчёта

Любое число получай через run_calc — не считай в уме и не бери из памяти.
Проставляй единицы измерения везде. Не хватает исходных данных — спроси
недостающее ОДНИМ вопросом, не подставляй умолчания молча. Сделал
допущение — назови его явно.

Если run_calc вернул ok:false — НЕ НАЗЫВАЙ НИКАКИХ ЧИСЕЛ. Прочитай error,
исправь код, вызови снова. Не вышло за две-три попытки — прямо скажи, что
расчёт не выполнен и что именно не получилось. Подставлять правдоподобные
значения вместо неудавшегося расчёта запрещено: уверенно поданное неверное
число хуже отсутствия ответа."""

STYLE = """- на Вы, деловой но лёгкий; по имени и отчеству, если известно.
- один вопрос – одно сообщение; вопросы с конечным набором вариантов – через aios-choices.
- не выдумывай данные: чего не знаешь – спроси; «не знаю» – пропускаем.
- отвечай на русском."""


class SkillError(Exception):
    """Ошибка проверки или публикации; errors — список причин для человека и модели."""

    def __init__(self, errors):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("; ".join(self.errors))


# ---------------------------------------------------------------- поля навыка

def _s(value, limit: int) -> str:
    return re.sub(r"[ \t]+", " ", str(value if value is not None else "").strip())[:limit]


def normalize_spec(raw: dict) -> dict:
    """Приводит поля от модели к ожидаемым типам и обрезает лишнее."""
    raw = raw if isinstance(raw, dict) else {}
    steps = []
    for item in raw.get("steps") if isinstance(raw.get("steps"), list) else []:
        if isinstance(item, dict):
            choices = item.get("choices") if isinstance(item.get("choices"), list) else []
            steps.append({"title": _s(item.get("title"), 90), "text": str(item.get("text") or "").strip()[:1600],
                          "choices": [_s(c, 90) for c in choices if _s(c, 90)][:8]})
    tools = []
    for item in raw.get("tools") if isinstance(raw.get("tools"), list) else []:
        if isinstance(item, dict):
            tools.append({"name": _s(item.get("name"), 60), "when": _s(item.get("when"), 300)})
    rules = raw.get("rules") if isinstance(raw.get("rules"), list) else []
    triggers = raw.get("triggers") if isinstance(raw.get("triggers"), list) else []
    return {
        "id": _s(raw.get("id"), 80).lower(), "name": _s(raw.get("name"), 100),
        "description": _s(raw.get("description"), 320), "domain": _s(raw.get("domain"), 100),
        "triggers": [_s(t, 50).lower() for t in triggers if _s(t, 50)][:10],
        "role": _s(raw.get("role"), 320), "task": _s(raw.get("task"), 520),
        "steps": steps[:14], "tools": tools[:20],
        "rules": [_s(r, 320) for r in rules if _s(r, 320)][:12],
        "final": str(raw.get("final") or "").strip()[:900],
        "agent_id": _s(raw.get("agent_id"), 64),
        "rag_id": _s(raw.get("rag_id"), 80),
        "model_ids": [_s(m, 20) for m in (raw.get("model_ids") if isinstance(raw.get("model_ids"), list) else [])
                      if _s(m, 20)][:3],
    }


def _private_hits(text: str) -> list[str]:
    return [label for pattern, label in PRIVATE_PATTERNS if pattern.search(text or "")]


RAG_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,79}$")


def rag_check(rag_id: str, user_id: str | None) -> str:
    """'' — базу можно привязать к агенту; иначе причина отказа (для модели и человека)."""
    from . import rag as rag_module

    if not RAG_ID_RE.match(rag_id or ""):
        return "rag_id: некорректный идентификатор RAG-базы"
    store = rag_module.get_store()
    own = bool(user_id) and store.get(user_id, rag_id) is not None
    if store.find(rag_id) is None and not own:
        return f"RAG-база {rag_id} не найдена"
    return ""


MODEL_ID_RE = re.compile(r"^mdl-[a-f0-9]{10}$")


def model_check(model_id: str, user_id: str | None) -> str:
    """'' — модель можно привязать к агенту; иначе причина отказа."""
    from . import model_engine

    if not MODEL_ID_RE.match(model_id or ""):
        return f"model_ids: некорректный идентификатор модели «{model_id}»"
    try:
        model_engine.resolve(model_id, user_id, model_engine.LEVEL_USE)
    except model_engine.ModelEngineError:
        return f"Модель {model_id} не найдена"
    return ""


def bound_model_ids(skill_ids, user_id: str | None) -> list[str]:
    """Модели, привязанные к первому агенту сессии, доступные пользователю."""
    from . import model_engine

    for sid in skill_ids or []:
        sid = str(sid)
        if sid.startswith(DRAFT_PREFIX):
            item = get_store().get(user_id, sid[len(DRAFT_PREFIX):]) if user_id else None
            meta = (split_md(item["text"])[0] if item else None) or {}
        else:
            meta = datarepo.skill_meta(sid) or {}
        ids = meta.get("model_ids")
        if not isinstance(ids, list):
            continue
        out = []
        for mid in ids[:3]:
            mid = str(mid).strip()
            if not MODEL_ID_RE.match(mid):
                continue
            try:
                model_engine.resolve(mid, user_id, model_engine.LEVEL_USE)
            except model_engine.ModelEngineError:
                continue
            out.append(mid)
        return out
    return []


def bound_rag_id(skill_ids, user_id: str | None) -> str:
    """id RAG-базы, привязанной к первому агенту сессии. '' — привязки нет либо база не
    опубликована (в чате подключаются только активные базы)."""
    from . import rag as rag_module

    store = rag_module.get_store()
    for sid in skill_ids or []:
        sid = str(sid)
        if sid.startswith(DRAFT_PREFIX):
            item = get_store().get(user_id, sid[len(DRAFT_PREFIX):]) if user_id else None
            meta = (split_md(item["text"])[0] if item else None) or {}
        else:
            meta = datarepo.skill_meta(sid) or {}
        rid = str(meta.get("rag_id") or "").strip()
        if not rid or not RAG_ID_RE.match(rid):
            continue
        card = store.find(rid)
        return rid if card and card.get("status") == rag_module.STATUS_ACTIVE else ""
    return ""


def _mentioned_tools(text: str) -> set[str]:
    return {name for name in KNOWN_TOOL_NAMES if re.search(rf"(?<![A-Za-z0-9_]){name}(?![A-Za-z0-9_])", text or "")}


def trigger_conflicts(triggers: list[str], own_id: str | None = None) -> list[str]:
    """Пересечения с триггерами опубликованных агентов: подсказки в чате начнут
    путаться (матчинг подстрокой, поэтому пересечение — это вложенность)."""
    out = []
    for skill in datarepo.skills():
        other_id = skill.get("id")
        if not other_id or other_id == own_id:
            continue
        others = datarepo.skill_meta(other_id).get("triggers")
        for other in others if isinstance(others, list) else []:
            for mine in triggers:
                o = str(other).strip().lower()
                if o and (mine == o or mine in o or o in mine):
                    out.append(f"фраза «{mine}» пересекается с агентом «{skill.get('name', other_id)}» («{o}»)")
    return out


def check_id(skill_id: str, own_published: bool = False) -> list[str]:
    if not (ID_MIN <= len(skill_id) <= ID_MAX and ID_RE.match(skill_id)):
        return [f"id «{skill_id}» не подходит: латиница в нижнем регистре и дефисы, от {ID_MIN} до {ID_MAX} символов"]
    if not own_published and datarepo.get_skill(skill_id) is not None:
        return [f"id «{skill_id}» уже занят в каталоге — предложите другое название или id"]
    return []


def validate_spec(spec: dict, available_tools: set[str], own_published: bool = False) -> list[str]:
    errs: list[str] = []
    errs += check_id(spec["id"], own_published)
    if not 3 <= len(spec["name"]) <= 100:
        errs.append("name — русское название агента, от 3 до 100 символов")
    if not 10 <= len(spec["description"]) <= 320 or "\n" in spec["description"]:
        errs.append("description — одно предложение, от 10 до 320 символов, без переносов строк")
    if not 3 <= len(spec["domain"]) <= 100:
        errs.append("domain — предметная область одной фразой, от 3 до 100 символов")
    trig = spec["triggers"]
    if not 3 <= len(trig) <= 6 or any(len(t) < 4 for t in trig) or len(set(trig)) != len(trig):
        errs.append("triggers — от 3 до 6 разных основ слов, каждая от 4 символов (например «литобзор», «свежие статьи»)")
    else:
        errs += trigger_conflicts(trig, spec["id"])
    if not 10 <= len(spec["role"]) <= 320:
        errs.append("role — кто ты и для кого работаешь, продолжение фразы «ты – АГЕНТ, который …», от 10 до 320 символов")
    if not 10 <= len(spec["task"]) <= 520:
        errs.append("task — что пользователь получает на выходе, продолжение фразы «твоя задача – …», от 10 до 520 символов")
    if not 2 <= len(spec["steps"]) <= 14:
        errs.append("steps — от 2 до 14 шагов")
    for i, step in enumerate(spec["steps"], 1):
        if not 3 <= len(step["title"]) <= 90 or not 10 <= len(step["text"]) <= 1600:
            errs.append(f"шаг {i}: title от 3 до 90 символов, text от 10 до 1600")
        if len(step["choices"]) == 1:
            errs.append(f"шаг {i}: choices — либо не менее двух вариантов, либо пусто")
    if not 1 <= len(spec["tools"]) <= 20:
        errs.append("tools — от 1 до 20 инструментов с правилом «когда использовать»")
    for tool in spec["tools"]:
        if tool["name"] in AGENT_ONLY_TOOLS:
            errs.append(f"инструмент {tool['name']} в чате не работает или пишет не тому, убери его из агента")
        elif tool["name"] not in available_tools:
            errs.append(f"инструмента {tool['name']} нет на этом сервере")
        if not 5 <= len(tool["when"]) <= 300:
            errs.append(f"инструмент {tool['name']}: when — когда вызывать, от 5 до 300 символов")
    if not 10 <= len(spec["final"]) <= 900:
        errs.append("final — что пользователь получил и что предложить дальше, от 10 до 900 символов")
    # agent_id — служебная ссылка на исходного агента прежних версий конструктора,
    # в текст агента не попадает
    blob = json.dumps({k: v for k, v in spec.items() if k != "agent_id"}, ensure_ascii=False)
    if "```" in blob:
        errs.append("в полях не должно быть блоков кода (```): варианты быстрых ответов передавайте в choices")
    errs += [f"в тексте есть {label} — личное в агента не переносится" for label in _private_hits(blob)]
    return errs


# ---------------------------------------------------------------- сборка .md

def _cell(text: str) -> str:
    return str(text).replace("|", "/").replace("\n", " ").strip()


def render_md(spec: dict) -> str:
    """Собирает .md по шаблону платформы (см. agent-builder.md, «Скелет агента»)."""
    front = {"name": spec["name"], "description": spec["description"], "version": int(spec.get("version") or 1),
             "status": "draft", "triggers": list(spec["triggers"])}
    if spec.get("rag_id"):
        front["rag_id"] = spec["rag_id"]
    if spec.get("model_ids"):
        front["model_ids"] = list(spec["model_ids"])
    head = yaml.safe_dump(front, allow_unicode=True, sort_keys=False, default_flow_style=False, width=10_000).strip()
    lines = [
        "---", head, "---", "",
        f"# Агент «{spec['name']}»", "",
        f"ты – АГЕНТ, который {spec['role'].rstrip('.')}. С тобой взаимодействует ПОЛЬЗОВАТЕЛЬ – сотрудник ОРГАНИЗАЦИИ.", "",
        f"твоя задача – {spec['task'].rstrip('.')}.", "",
        "## Стиль", "", STYLE, "",
        "## Подгрузка контекста", "",
        "1. **Пользователь** — имя и отчество из блока «Сведения о пользователе».", "",
        "## Возможности", "", "| Возможность | Когда использовать |", "|---|---|",
    ]
    for tool in spec["tools"]:
        lines.append(f"| `{tool['name']}` | {_cell(tool['when'])} |")
    lines.append("")
    if spec["rules"]:
        lines += ["Правила работы:", ""] + [f"- {_cell(r)}" for r in spec["rules"]] + [""]
    if any(t["name"] == "run_calc" for t in spec["tools"]):
        lines += [CALC_RULES, ""]
    lines += ["## Шаги", ""]
    for i, step in enumerate(spec["steps"], 1):
        lines.append(f"**Шаг {i}. {step['title'].rstrip('.')}.** {step['text']}")
        if step["choices"]:
            lines += ["", "```aios-choices"] + step["choices"] + ["```"]
        lines.append("")
    lines += ["## Финал", "", spec["final"], ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- проверка .md

def split_md(text: str):
    """(meta, body, ошибка): в отличие от рантайма битый YAML не молчит."""
    match = _FRONTMATTER_RE.match(text or "")
    if not match:
        return None, text or "", "файл должен начинаться с блока метаданных «--- … ---»"
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return None, text[match.end():], f"метаданные не разбираются как YAML ({str(exc).splitlines()[0]}): в значениях нельзя «двоеточие+пробел»"
    if not isinstance(meta, dict):
        return None, text[match.end():], "метаданные пусты"
    return meta, text[match.end():], None


def validate_md(text: str, available_tools: set[str], own_id: str | None = None) -> list[str]:
    errs: list[str] = []
    if len(text or "") > MAX_MD_CHARS:
        errs.append(f"файл длиннее {MAX_MD_CHARS} символов")
    meta, body, problem = split_md(text)
    if problem:
        return errs + [problem]
    if not (isinstance(meta.get("name"), str) and 3 <= len(meta["name"]) <= 100):
        errs.append("в метаданных нужен name — русское название, от 3 до 100 символов")
    if not (isinstance(meta.get("description"), str) and 10 <= len(meta["description"]) <= 320):
        errs.append("в метаданных нужен description — одно предложение, от 10 до 320 символов")
    trig = meta.get("triggers")
    if not (isinstance(trig, list) and 3 <= len(trig) <= 6 and all(isinstance(t, str) and len(t.strip()) >= 4 for t in trig)):
        errs.append("в метаданных нужен triggers — от 3 до 6 основ слов, каждая от 4 символов")
    else:
        errs += trigger_conflicts([t.strip().lower() for t in trig], own_id)
    if "## Шаги" not in body:
        errs.append("нет раздела «## Шаги»")
    mentioned = _mentioned_tools(text)
    for name in sorted(mentioned & set(AGENT_ONLY_TOOLS)):
        errs.append(f"упомянут {name}: в чате он не работает или пишет не тому")
    for name in sorted(mentioned - set(AGENT_ONLY_TOOLS) - set(available_tools)):
        errs.append(f"упомянут инструмент {name}, которого нет на этом сервере")
    errs += [f"в тексте есть {label} — личное в агента не переносится" for label in _private_hits(text)]
    return errs


# ---------------------------------------------------------------- хранилище

_CATALOG_LOCK = Lock()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class DraftStore:
    def __init__(self, root: Path, trash_root: Path | None = None) -> None:
        self._root = Path(root)
        # Мягкое удаление: удалённые черновики переезжают в закрытый архив
        # .aios-runtime/skills-removed/{user}/ — API их не отдаёт, вернуть
        # может администратор через scripts/restore_agent.py.
        self.trash_root = Path(trash_root) if trash_root else self._root.parent / "skills-removed"
        self._lock = Lock()

    def _paths(self, user_id: str, skill_id: str):
        for key in (user_id, skill_id):
            if not re.match(r"^[A-Za-z0-9_-]{1,80}$", str(key or "")):
                raise SkillError(f"недопустимый идентификатор: {key!r}")
        base = self._root / user_id
        return base / f"{skill_id}.md", base / f"{skill_id}.json"

    def _read_meta(self, meta_path: Path) -> dict | None:
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _reconcile(self, meta: dict, meta_path: Path) -> dict:
        """Публичную карточку мог снять администратор: черновик возвращается в статус «черновик»."""
        if meta.get("status") == "published" and datarepo.get_skill(meta.get("id")) is None:
            meta["status"] = "draft"
            meta["published_at"] = None
            _atomic_write(meta_path, json.dumps(meta, ensure_ascii=False, indent=2))
        meta["dirty"] = meta.get("status") == "published" and meta.get("version") != meta.get("published_version")
        return meta

    def exists(self, user_id: str, skill_id: str) -> bool:
        try:
            md, meta = self._paths(user_id, skill_id)
        except SkillError:
            return False
        return md.is_file() and meta.is_file()

    def get(self, user_id: str, skill_id: str) -> dict | None:
        try:
            md_path, meta_path = self._paths(user_id, skill_id)
            text = md_path.read_text(encoding="utf-8")
        except (OSError, SkillError):
            return None
        meta = self._read_meta(meta_path)
        return {"meta": self._reconcile(meta, meta_path), "text": text} if meta else None

    def list(self, user_id: str) -> list[dict]:
        base = self._root / user_id
        out = []
        if re.match(r"^[A-Za-z0-9_-]{1,80}$", str(user_id or "")) and base.is_dir():
            for meta_path in base.glob("*.json"):
                meta = self._read_meta(meta_path)
                if meta and meta.get("id"):
                    out.append(self._reconcile(meta, meta_path))
        return sorted(out, key=lambda m: m.get("updated_at") or 0, reverse=True)

    def save(self, user_id: str, skill_id: str, text: str, *, name: str, description: str,
             domain: str = "", source_agent_id: str = "") -> dict:
        md_path, meta_path = self._paths(user_id, skill_id)
        with self._lock:
            old = self._read_meta(meta_path) or {}
            now = time.time()
            meta = {
                "id": skill_id, "name": name, "description": description,
                "domain": domain or old.get("domain") or "",
                "source_agent_id": source_agent_id or old.get("source_agent_id") or "",
                "status": old.get("status") or "draft", "version": int(old.get("version") or 0) + 1,
                "published_version": old.get("published_version"), "published_at": old.get("published_at"),
                "created_at": old.get("created_at") or now, "updated_at": now,
            }
            _atomic_write(md_path, text)
            _atomic_write(meta_path, json.dumps(meta, ensure_ascii=False, indent=2))
        meta["dirty"] = meta["status"] == "published" and meta["version"] != meta["published_version"]
        return meta

    def delete(self, user_id: str, skill_id: str, *, deleted_by: str | None = None) -> bool:
        """Удаление черновика — мягкое: файлы переезжают в закрытый архив
        .aios-runtime/skills-removed/{user}/ (вне _root — get/list их не
        читают), метка deleted_at/deleted_by пишется в метаданные архива.
        Публикацию удаление не трогает — как и раньше. Вернуть черновик
        может администратор через scripts/restore_agent.py."""
        import shutil

        md_path, meta_path = self._paths(user_id, skill_id)
        with self._lock:
            found = md_path.is_file() or meta_path.is_file()
            if not found:
                return False
            trash_dir = self.trash_root / user_id
            trash_dir.mkdir(parents=True, exist_ok=True)
            if meta_path.is_file():
                meta = self._read_meta(meta_path) or {}
                meta["deleted_at"] = time.time()
                meta["deleted_by"] = str(deleted_by or user_id)[:80]
                _atomic_write(trash_dir / f"{skill_id}.json", json.dumps(meta, ensure_ascii=False, indent=2))
            for src, name in ((md_path, f"{skill_id}.md"), (meta_path, f"{skill_id}.json")):
                if src.is_file():
                    dest = trash_dir / name
                    if dest.exists():
                        dest = dest.with_name(f"{dest.stem}-{int(time.time())}{dest.suffix}")
                    shutil.move(str(src), str(dest))
        return found

    def load_for_prompt(self, user_id: str, draft_id: str):
        """(название, текст без метаданных) для сборки промпта; None — нет такого черновика."""
        skill_id = draft_id[len(DRAFT_PREFIX):] if draft_id.startswith(DRAFT_PREFIX) else draft_id
        item = self.get(user_id, skill_id) if user_id else None
        if not item:
            return None
        _meta, body, _ = split_md(item["text"])
        return item["meta"].get("name") or skill_id, (body or item["text"]).lstrip("\r\n")

    # ------------------------------------------------------------ публикация

    def publish(self, user_id: str, skill_id: str, *, author: str, category: str, available_tools: set[str]) -> dict:
        item = self.get(user_id, skill_id)
        if not item:
            raise SkillError("Черновик не найден")
        meta = item["meta"]
        if category not in CATEGORIES:
            raise SkillError(f"category — одно из: {', '.join(CATEGORIES)}")
        own = meta.get("status") == "published"
        problems = validate_md(item["text"], available_tools, own_id=skill_id) + check_id(skill_id, own_published=own)
        if problems:
            raise SkillError(problems)
        front, body, _ = split_md(item["text"])
        rid = str((front or {}).get("rag_id") or "").strip()
        if rid:
            from . import rag as rag_module
            base = rag_module.get_store().find(rid)
            if not base or base.get("status") != rag_module.STATUS_ACTIVE:
                raise SkillError("База знаний агента не опубликована: опубликуйте RAG-базу в разделе «Данные и знания» "
                                 "или уберите привязку, иначе другие пользователи не смогут ею пользоваться")
        mids = (front or {}).get("model_ids") if isinstance((front or {}).get("model_ids"), list) else []
        if mids:
            from . import model_registry
            for mid in mids:
                model = model_registry.get_store().find(str(mid))
                if not model or model.get("status") not in model_registry.PUBLIC_STATUSES:
                    raise SkillError("Модель агента не опубликована: опубликуйте её на странице модели "
                                     "или уберите привязку, иначе другие пользователи не смогут ею пользоваться")
        front["status"] = "active"
        head = yaml.safe_dump(front, allow_unicode=True, sort_keys=False, default_flow_style=False, width=10_000).strip()
        public_text = f"---\n{head}\n---\n{body.lstrip(chr(10))}"
        card = {
            "id": skill_id, "category": category, "name": meta["name"], "domain": meta.get("domain") or "—",
            "description_ru": meta["description"], "author": author or "Пользователь",
            "recommended_model": "—", "file_url": f"/data/skills/{skill_id}.md",
            "image_url": "/assets/icons/agent/default.svg", "source": "draft",
        }
        if rid:
            card["rag_id"] = rid
        if mids:
            card["model_ids"] = [str(m) for m in mids]
        with _CATALOG_LOCK:
            path = datarepo.DATA_DIR / "skills.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                items = data["items"]
            except (OSError, ValueError, KeyError, TypeError):
                raise SkillError("Каталог агентов недоступен")
            clash = next((c for c in items if isinstance(c, dict) and c.get("id") == skill_id), None)
            if clash is not None and not (own and clash.get("source") == "draft"):
                raise SkillError(f"id «{skill_id}» уже занят в каталоге")
            data["items"] = [c for c in items if not (isinstance(c, dict) and c.get("id") == skill_id)] + [card]
            _atomic_write(datarepo.DATA_DIR / "skills" / f"{skill_id}.md", public_text)
            _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
        with self._lock:
            _md, meta_path = self._paths(user_id, skill_id)
            meta.pop("dirty", None)
            meta.update(status="published", published_at=time.time(), published_version=meta["version"])
            _atomic_write(meta_path, json.dumps(meta, ensure_ascii=False, indent=2))
        meta["dirty"] = False
        return card

    def unpublish(self, user_id: str, skill_id: str) -> bool:
        item = self.get(user_id, skill_id)
        if not item or item["meta"].get("status") != "published":
            raise SkillError("Агент не опубликован")
        card = datarepo.get_skill(skill_id)
        if card is not None and card.get("source") == "draft":
            datarepo.remove_skill_file(card)
            datarepo.remove_skill(skill_id)
        _md, meta_path = self._paths(user_id, skill_id)
        meta = dict(item["meta"])
        meta.pop("dirty", None)
        meta.update(status="draft", published_at=None)
        with self._lock:
            _atomic_write(meta_path, json.dumps(meta, ensure_ascii=False, indent=2))
        return True


_STORE: DraftStore | None = None


def get_store() -> DraftStore:
    global _STORE
    if _STORE is None:
        from .config import get_settings

        _STORE = DraftStore(get_settings().runtime_dir / "skills")
    return _STORE


def skill_available(skill_id: str, user_id: str | None, is_admin: bool = False) -> bool:
    """Годится ли id для skill_ids чата: агент каталога либо личный черновик
    владельца. Admin-only агенты каталога (datarepo.is_admin_only) — только
    роли «Администратор»: без права агент неотличим от несуществующего."""
    if str(skill_id).startswith(DRAFT_PREFIX):
        return bool(user_id) and get_store().exists(user_id, str(skill_id)[len(DRAFT_PREFIX):])
    skill = datarepo.get_skill(skill_id)
    if skill is None:
        return False
    return not datarepo.is_admin_only(skill) or is_admin
