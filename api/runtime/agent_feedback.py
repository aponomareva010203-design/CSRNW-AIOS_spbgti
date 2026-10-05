"""Обратная связь по агентам платформенного каталога (страница «Агенты»).

Три сущности в одном файле на агента — AIOS_RUNTIME_DIR/agent_feedback/
{agent_id}.json (запись атомарная: временный файл + replace):

    stars    — звёзды: {user_id: ts} (счётчик = количество ключей);
    comments — публичные комментарии: {id, user_id, text, created_at};
    usage    — автоматическая статистика применения агента в проектах:
               {project_id, first_at, last_at, runs, artifacts}.
               Пишется инструментами навыков (save_project_card /
                             save_ontology / save_rag) из сессии
                             с активными агентами (session.skill_ids);
    runs дедуплицируются по проекту: повторные применения наращивают счётчик
    и обновляют last_at, а не плодят записи.

Идентификатор агента валидируется по каталогу datarepo.get_skill (тот же
data/skills.json, что питает страницу «Агенты»), файлы с посторонними
именами не читаются и не пишутся. Названия проектов и имена авторов
комментариев в хранилище не дублируются — разрешаются в момент чтения
(роутером), чтобы не рассинхронизироваться при правке карточек и анкет.
"""

import json
import re
import time
from functools import lru_cache
from pathlib import Path

from .config import get_settings

AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")

COMMENT_MAX_CHARS = 2_000
MAX_COMMENTS = 500  # верхняя граница списка на агента
MAX_USAGE_PROJECTS = 500


def _valid_agent_id(agent_id: str) -> bool:
    """Агент есть в каталоге data/skills.json (страница «Агенты»)."""
    from . import datarepo  # локальный импорт: datarepo тянет yaml

    if not AGENT_ID_RE.match(agent_id or ""):
        return False
    return datarepo.get_skill(agent_id) is not None


class AgentFeedbackError(ValueError):
    """Некорректные данные обратной связи."""


class AgentFeedbackStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ----- низкоуровневые операции -----

    def _path(self, agent_id: str) -> Path:
        if not AGENT_ID_RE.match(agent_id or ""):
            raise AgentFeedbackError("Некорректный идентификатор агента")
        return self.root / f"{agent_id}.json"

    def _load(self, agent_id: str) -> dict:
        try:
            data = json.loads(self._path(agent_id).read_text(encoding="utf-8"))
        except (OSError, ValueError, AgentFeedbackError):
            return self._empty()
        return data if isinstance(data, dict) else self._empty()

    @staticmethod
    def _empty() -> dict:
        return {"stars": {}, "comments": [], "usage": []}

    def _save(self, agent_id: str, data: dict) -> None:
        path = self._path(agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    # ----- звёзды -----

    def toggle_star(self, agent_id: str, user_id: str) -> dict:
        """Переключает звезду пользователя; возвращает состояние после."""
        data = self._load(agent_id)
        stars = data["stars"] if isinstance(data.get("stars"), dict) else {}
        if user_id in stars:
            stars.pop(user_id, None)
            starred = False
        else:
            stars[user_id] = time.time()
            starred = True
        data["stars"] = stars
        self._save(agent_id, data)
        return {"starred": starred, "stars": len(stars)}

    # ----- комментарии -----

    def add_comment(self, agent_id: str, user_id: str, text: str) -> dict:
        """Дописывает комментарий автора; свежие сверху списка."""
        cleaned = str(text or "").strip()
        if not cleaned:
            raise AgentFeedbackError("Пустой комментарий")
        if len(cleaned) > COMMENT_MAX_CHARS:
            cleaned = cleaned[:COMMENT_MAX_CHARS]
        data = self._load(agent_id)
        comments = data["comments"] if isinstance(data.get("comments"), list) else []
        comment = {
            "id": f"cmt-{int(time.time() * 1000):x}-{len(comments):x}",
            "user_id": user_id,
            "text": cleaned,
            "created_at": time.time(),
        }
        comments.insert(0, comment)
        data["comments"] = comments[:MAX_COMMENTS]
        self._save(agent_id, data)
        return comment

    def delete_comment(self, agent_id: str, comment_id: str, user_id: str, is_admin: bool) -> bool:
        """Удаляет комментарий (автор или администратор); False — не найден
        или нет прав: чужой комментарий неотличим от несуществующего."""
        data = self._load(agent_id)
        comments = data.get("comments") or []
        kept = [
            c for c in comments
            if not (
                isinstance(c, dict)
                and c.get("id") == comment_id
                and (is_admin or c.get("user_id") == user_id)
            )
        ]
        if len(kept) == len(comments):
            return False
        data["comments"] = kept
        self._save(agent_id, data)
        return True

    # ----- применение в проектах (пишется автоматически) -----

    def record_usage(self, agent_id: str, project_id: str, artifact: str = "", user_id: str = "") -> bool:
        """Отмечает применение агента в проекте (upsert по project_id)."""
        if not project_id:
            return False
        data = self._load(agent_id)
        usage = data["usage"] if isinstance(data.get("usage"), list) else []
        now = time.time()
        entry = next(
            (u for u in usage if isinstance(u, dict) and u.get("project_id") == project_id),
            None,
        )
        if entry is None:
            entry = {
                "project_id": project_id,
                "first_at": now,
                "runs": 0,
                "artifacts": [],
            }
            usage.append(entry)
        entry["last_at"] = now
        entry["runs"] = int(entry.get("runs") or 0) + 1
        if user_id:
            entry["last_user_id"] = user_id
        artifacts = entry.get("artifacts") if isinstance(entry.get("artifacts"), list) else []
        if artifact and artifact not in artifacts:
            artifacts.append(artifact)
            entry["artifacts"] = artifacts
        # свежие сверху, общий размер ограничен
        usage.sort(key=lambda u: u.get("last_at") or 0, reverse=True)
        data["usage"] = usage[:MAX_USAGE_PROJECTS]
        self._save(agent_id, data)
        return True

    # ----- чтение -----

    def feedback(self, agent_id: str) -> dict:
        """Полная запись агента (нормализованная)."""
        return self._load(agent_id)

    def counts(self, agent_id: str) -> dict:
        """Счётчики для карточек каталога: звёзды, комментарии, проекты."""
        data = self._load(agent_id)
        return {
            "stars": len(data.get("stars") or {}),
            "comments": len(data.get("comments") or []),
            "projects": len(data.get("usage") or []),
        }


def record_usage_from_session(session, project_id: str, artifact: str = "") -> None:
    """Помечает применение всех активных агентов сессии в проекте.

    Вызывается инструментами сохранения артефактов (tools.py): сессия чата
    знает подключённых агентов (skill_ids), сохраняемый артефакт — проект
    (project_id). Словарь обратной связи — надстройка над основной работой
    навыка, поэтому любые сбои глотаются: поломка учёта не должна ронять
    сохранение онтологии или карточки проекта.
    """
    try:
        if not isinstance(session, dict) or not project_id:
            return
        skill_ids = session.get("skill_ids")
        if not isinstance(skill_ids, list):
            return
        user_id = str(session.get("user_id") or "")
        store = get_store()
        for skill_id in skill_ids:
            if not isinstance(skill_id, str) or not _valid_agent_id(skill_id):
                continue
            store.record_usage(skill_id, project_id, artifact, user_id)
    except Exception:  # noqa: BLE001 — см. докстринг
        pass


@lru_cache(maxsize=1)
def get_store() -> AgentFeedbackStore:
    """Синглтон; в тестах сбрасывается cache_clear()."""
    return AgentFeedbackStore(get_settings().runtime_dir / "agent_feedback")
