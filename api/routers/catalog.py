"""HTTP-слой обратной связи каталога агентов (страница «Агенты»).

GET    /api/catalog/agents/overview              — счётчики (звёзды, комментарии,
                                                    проекты) всех агентов каталога;
GET    /api/catalog/agents/{id}/feedback         — звёзды, комментарии и проекты
                                                    одного агента (+ «моя звезда»
                                                    при валидном токене);
POST   /api/catalog/agents/{id}/star             — поставить/снять звезду (вход);
POST   /api/catalog/agents/{id}/comments         — добавить комментарий (вход);
DELETE /api/catalog/agents/{id}/comments/{cid}   — удалить комментарий (автор
                                                    или роль «Администратор»);
DELETE /api/catalog/agents/{id}                  — полное удаление агента каталога
                                                    (карточка skills.json + .md +
                                                    runtime-экземпляр agt-{id} с
                                                    данными) — только роль
                                                    «Администратор».

Чтение публичное (как таблица пилота): звёзды и комментарии видны всем.
В блоке проектов наружу отдаются только названия карточек — персональные
поля (бюджет, участники, задел) не раскрываются; удалённые и тестовые
(admin_only, для не-администраторов) проекты в списке не показываются.
Логины (e-mail) авторов комментариев не отдаются: имя автора собирается
из анкеты «Кабинета» или учётной записи, без него — «Пользователь».
"""

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..runtime import agent_feedback, cabinet, datarepo, projects, skill_drafts
from ..runtime.users import ROLE_ADMIN
from .auth import get_current_user, get_optional_user, get_user_store

router = APIRouter(prefix="/api/catalog")

# метки применения агента в проекте (usage.artifacts) для интерфейса
ARTIFACT_LABELS = {
    "project": "карточка проекта",
    "ontology": "онтология",
    "rag": "RAG-база",
    "agent": "агент проекта",
}


class CommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=agent_feedback.COMMENT_MAX_CHARS)


def _require_agent(agent_id: str) -> dict:
    """Карточка агента каталога; 404 для посторонних id."""
    skill = datarepo.get_skill(agent_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Агент не найден")
    return skill


def _author_name(user_id: str | None) -> str:
    """Отображаемое имя автора: анкета «Кабинета» → имя учётной записи →
    «Пользователь». Логин (e-mail) не показывается."""
    if not user_id:
        return "Пользователь"
    profile = cabinet.get_store().get_profile(user_id)
    name = cabinet.full_name(profile)
    if name:
        return name
    user = get_user_store().get(user_id)
    if user and user.get("name"):
        return str(user["name"])
    return "Пользователь"


def _project_view(entry: dict, is_admin: bool) -> dict | None:
    """Проект из записи usage для ответа API; None — не показывать
    (проект удалён или тестовый admin_only без роли администратора)."""
    card = projects.get_store().find(entry.get("project_id") or "")
    if card is None:
        return None
    if projects.is_admin_only(card) and not is_admin:
        return None
    artifacts = [
        ARTIFACT_LABELS.get(a, a)
        for a in (entry.get("artifacts") or [])
        if isinstance(a, str) and a
    ]
    return {
        "project_id": card.get("id"),
        "title": card.get("title") or "Без названия",
        "status": card.get("status"),
        "runs": int(entry.get("runs") or 0),
        "last_at": entry.get("last_at"),
        "artifacts": artifacts,
    }


def _feedback_view(agent_id: str, user: dict | None) -> dict:
    store = agent_feedback.get_store()
    data = store.feedback(agent_id)
    is_admin = bool(user) and user.get("role") == ROLE_ADMIN
    project_views = [
        view
        for view in (
            _project_view(e, is_admin)
            for e in (data.get("usage") or [])
            if isinstance(e, dict)
        )
        if view is not None
    ]
    comments = [
        {
            "id": c.get("id"),
            "author": _author_name(c.get("user_id")),
            "mine": bool(user) and c.get("user_id") == user.get("id"),
            "text": str(c.get("text") or ""),
            "created_at": c.get("created_at"),
        }
        for c in (data.get("comments") or [])
        if isinstance(c, dict)
    ]
    stars = data.get("stars") or {}
    return {
        "agent_id": agent_id,
        "stars": len(stars),
        "starred_by_me": bool(user) and user.get("id") in stars,
        "can_delete_any": is_admin,
        "comments": comments,
        "projects": project_views,
    }


@router.get("/agents/overview")
def agents_overview() -> dict:
    """Счётчики всех агентов каталога — для карточек страницы «Агенты»."""
    store = agent_feedback.get_store()
    return {
        "items": {
            skill.get("id"): store.counts(skill.get("id") or "")
            for skill in datarepo.skills()
            if skill.get("id")
        }
    }


@router.get("/agents/{agent_id}/feedback")
def agent_feedback_view(agent_id: str, user: dict | None = Depends(get_optional_user)) -> dict:
    _require_agent(agent_id)
    return _feedback_view(agent_id, user)


@router.post("/agents/{agent_id}/star")
def toggle_star(agent_id: str, user: dict = Depends(get_current_user)) -> dict:
    _require_agent(agent_id)
    result = agent_feedback.get_store().toggle_star(agent_id, user["id"])
    result["stars_by_me"] = result.pop("starred")
    return result


@router.post("/agents/{agent_id}/comments")
def add_comment(agent_id: str, payload: CommentIn, user: dict = Depends(get_current_user)) -> dict:
    _require_agent(agent_id)
    try:
        comment = agent_feedback.get_store().add_comment(agent_id, user["id"], payload.text)
    except agent_feedback.AgentFeedbackError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    comment.pop("user_id", None)  # наружу — только отображаемое имя
    comment["author"] = _author_name(user["id"])
    comment["mine"] = True
    return comment


@router.delete("/agents/{agent_id}/comments/{comment_id}")
def delete_comment(
    agent_id: str, comment_id: str, user: dict = Depends(get_current_user)
) -> dict:
    _require_agent(agent_id)
    is_admin = user.get("role") == ROLE_ADMIN
    deleted = agent_feedback.get_store().delete_comment(
        agent_id, comment_id, user["id"], is_admin
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Комментарий не найден")
    return {"ok": True}


def _archive_public_skill(skill: dict, deleted_by: str) -> bool:
    """Копия публичных файлов удаляемого агента каталога в закрытый архив
    .aios-runtime/skills-removed/_catalog/{id}.json (карточка + полный текст
    .md с пометкой deleted_at/deleted_by). Для агентов из черновиков текст
    и так остаётся в архиве у автора, но для платформенных навыков публичный
    .md — единственная копия. Возвращает True, если записано."""
    import time

    from ..runtime.config import get_settings

    path = datarepo.skill_path(skill)
    text = ""
    if path is not None and path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
    record = {
        "id": skill.get("id"),
        "card": skill,
        "text": text,
        "deleted_at": time.time(),
        "deleted_by": str(deleted_by)[:80],
    }
    trash_dir = get_settings().runtime_dir / "skills-removed" / "_catalog"
    trash_dir.mkdir(parents=True, exist_ok=True)
    dest = trash_dir / f"{skill.get('id')}.json"
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(dest)
    return True


@router.delete("/agents/{agent_id}")
def delete_catalog_agent(agent_id: str, user: dict = Depends(get_current_user)) -> dict:
    """Удаление агента каталога — только роль «Администратор».

    Карточка каталога — это и есть агент (единая модель страницы «Агенты»):
    удаление убирает карточку из data/skills.json и .md-файл с публичного
    доступа, но ничего не стирает физически: публичные файлы архивируются
    в .aios-runtime/skills-removed/_catalog/, runtime-экземпляр agt-{id}
    уезжает в agents-removed (AgentStore.delete). API отвечает 404, вернуть
    можно scripts/restore_agent.py. Черновик автора удаление не трогает
    (см. unpublish: навык, опубликованный из личного черновика, — только
    текст).
    """
    if user.get("role") != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Удалять агентов может только администратор")
    skill = datarepo.get_skill(agent_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Агент не найден")

    archived = _archive_public_skill(skill, deleted_by=user["id"])
    removed_file = datarepo.remove_skill_file(skill)
    removed_card = datarepo.remove_skill(agent_id)

    from ..runtime import agent_store

    runtime_removed = False
    store = agent_store.get_store()
    # навык, опубликованный из личного черновика, — только текст: агент автора остаётся
    runtime_card = None if skill.get("source") == "draft" else store.find_any_owner(f"agt-{agent_id}")
    if runtime_card is not None:
        runtime_removed = store.delete(
            runtime_card["owner_id"], runtime_card["id"], deleted_by=user["id"]
        )

    return {
        "ok": True, "card": removed_card, "file": removed_file,
        "runtime": runtime_removed, "archived": archived,
    }


# ---------------------------------------------------------------------------
# Личные навыки (кабинет → «Навыки»): черновик → тест в чате → публикация.
# Агент живёт у автора в кабинете, а публикуется только навык по нему; снятие
# навыка с публикации агента не трогает (см. delete_catalog_agent).


class SkillTextIn(BaseModel):
    text: str = Field(min_length=20, max_length=40_000)


class SkillPublishIn(BaseModel):
    category: str = Field(default="special", max_length=20)


def _available_tools() -> set:
    from .chat import get_registry  # лениво: chat собирает реестр при первом обращении

    return set(get_registry().names())


def _draft_or_404(user: dict, skill_id: str) -> dict:
    item = skill_drafts.get_store().get(user["id"], skill_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Черновик не найден")
    return item


@router.get("/skills/drafts")
def list_skill_drafts(user: dict = Depends(get_current_user)) -> dict:
    """Личные агенты пользователя (черновики и опубликованные) — раздел «Агенты» кабинета."""
    items = skill_drafts.get_store().list(user["id"])
    return {"items": items, "count": len(items)}


@router.get("/skills/drafts/{skill_id}")
def get_skill_draft(skill_id: str, user: dict = Depends(get_current_user)) -> dict:
    item = _draft_or_404(user, skill_id)
    return {"meta": item["meta"], "text": item["text"], "launch_id": f"{skill_drafts.DRAFT_PREFIX}{skill_id}"}


@router.put("/skills/drafts/{skill_id}")
def update_skill_draft(skill_id: str, payload: SkillTextIn, user: dict = Depends(get_current_user)) -> dict:
    """Правка текста черновика человеком; та же проверка, что и при сборке."""
    _draft_or_404(user, skill_id)
    errors = skill_drafts.validate_md(payload.text, _available_tools(), own_id=skill_id)
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))
    front, _body, _problem = skill_drafts.split_md(payload.text)
    saved = skill_drafts.get_store().save(
        user["id"], skill_id, payload.text, name=front["name"], description=front["description"]
    )
    return {"meta": saved}


@router.delete("/skills/drafts/{skill_id}")
def delete_skill_draft(skill_id: str, user: dict = Depends(get_current_user)) -> dict:
    item = _draft_or_404(user, skill_id)
    if item["meta"].get("status") == "published":
        raise HTTPException(status_code=409, detail="Агент опубликован: сначала снимите его с публикации")
    skill_drafts.get_store().delete(user["id"], skill_id, deleted_by=user["id"])
    return {"ok": True}


@router.post("/skills/drafts/{skill_id}/publish")
def publish_skill_draft(skill_id: str, payload: SkillPublishIn, user: dict = Depends(get_current_user)) -> dict:
    """Публикация агента в общий каталог «Агенты» — действие человека, не модели."""
    _draft_or_404(user, skill_id)
    try:
        card = skill_drafts.get_store().publish(
            user["id"], skill_id, author=_author_name(user["id"]),
            category=payload.category, available_tools=_available_tools(),
        )
    except skill_drafts.SkillError as exc:
        raise HTTPException(status_code=422, detail="; ".join(exc.errors))
    return {"ok": True, "card": card}


@router.post("/skills/drafts/{skill_id}/unpublish")
def unpublish_skill_draft(skill_id: str, user: dict = Depends(get_current_user)) -> dict:
    """Снять агента с публикации: публичные файлы убираются, черновик остаётся."""
    _draft_or_404(user, skill_id)
    try:
        skill_drafts.get_store().unpublish(user["id"], skill_id)
    except skill_drafts.SkillError as exc:
        raise HTTPException(status_code=409, detail="; ".join(exc.errors))
    return {"ok": True}
