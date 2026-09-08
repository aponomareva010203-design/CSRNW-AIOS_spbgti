"""HTTP-слой страницы «Мои проекты» (api/routers/projects.py).

GET    /api/projects          — список карточек текущего пользователя;
GET    /api/projects/{id}     — одна карточка;
PATCH  /api/projects/{id}     — правка карточки руководителем проекта
                                (владельцем карточки);
DELETE /api/projects/{id}     — удаление карточки, только роль
                                «Администратор» (любой карточки по id).

Собственные карточки адресуются по идентификатору владельца из Bearer-токена
(get_current_user) — из запроса он не принимается, чужие карточки владельцу
недостижимы. Карточки создаются навыком «Онбординг проекта» через
инструмент save_project_card в рабочем чате; на «Моих проектах» показываются
лишь карточки со статусом active (черновики видны модели для продолжения
онбординга).
"""

from fastapi import APIRouter, Body, Depends, HTTPException

from ..runtime import projects
from ..runtime.projects import ProjectError
from .auth import get_current_user, require_admin

router = APIRouter(prefix="/api")


@router.get("/projects")
def list_projects(user: dict = Depends(get_current_user)):
    """Карточки проектов владельца токена, свежие сверху."""
    cards = projects.get_store().list(user["id"])
    return {"items": [projects.get_store().public_card(c) for c in cards]}


@router.get("/projects/{project_id}")
def get_project(project_id: str, user: dict = Depends(get_current_user)):
    """Одна карточка проекта владельца токена."""
    card = projects.get_store().get(user["id"], project_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    return projects.get_store().public_card(card)


@router.patch("/projects/{project_id}")
def update_project(
    project_id: str,
    patch: dict = Body(default={}),
    user: dict = Depends(get_current_user),
):
    """Правка карточки руководителем проекта (владельцем токена).

    Тело — частичный словарь полей карточки (title, description, goal,
    team_size, participants, departments, budget, groundwork,
    groundwork_links/files, presentation_files, presentation, status):
    переданные поля перезаписываются, остальные сохраняются.
    """
    store = projects.get_store()
    if store.get(user["id"], project_id) is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    try:
        saved = store.save(user["id"], dict(patch or {}, project_id=project_id))
    except ProjectError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return store.public_card(saved)


@router.delete("/projects/{project_id}")
def delete_project(
    project_id: str, _: dict = Depends(require_admin)
):
    """Удаление карточки — только роль «Администратор», любой карточки по id."""
    store = projects.get_store()
    card = store.find(project_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    store.delete(card["user_id"], project_id)
    return {"ok": True, "deleted": project_id}
