"""HTTP-слой раздела «Проекты» (api/routers/projects.py).

GET    /api/projects          — проекты всех команд (активные карточки
                                всех владельцев + свои черновики);
POST   /api/projects          — пустая карточка-черновик владельца
                                (пустая карточка на «Проектах»);
GET    /api/projects/{id}     — одна карточка;
PATCH  /api/projects/{id}     — правка карточки руководителем проекта
                                (владельцем карточки);
DELETE /api/projects/{id}     — удаление карточки, только роль
                                «Администратор» (любой карточки по id);
GET  /api/projects/{id}/files/{name} — скачивание файла карточки
                                (вложение чата или загрузка из формы);
POST /api/projects/{id}/files   — прикрепить файл к своей карточке
                                (задел/презентация, multipart).

Собственные карточки адресуются по идентификатору владельца из Bearer-токена
(get_current_user) — из запроса он не принимается, чужие карточки владельцу
недостижимы. Карточки создаются навыком «Онбординг проекта» через
инструмент save_project_card в рабочем чате; на «Моих проектах» показываются
лишь карточки со статусом active (черновики видны модели для продолжения
онбординга).
"""

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ..runtime import cabinet, hypotheses, projects, team
from ..runtime.projects import ProjectError
from .auth import get_current_user, get_user_store, require_admin

router = APIRouter(prefix="/api")


@router.get("/projects")
def list_projects(user: dict = Depends(get_current_user)):
    """Проекты всех команд — активные карточки всех владельцев.

    Раздел «Проекты» общий: любой вошедший пользователь видит активные
    карточки всех владельцев (руководителя, состав команды, гипотезы —
    всё в public_card). Свои черновики онбординга видны только владельцу.
    Каждая карточка дополняется именем руководителя (owner) и флагом
    is_own — клиенту это нужно, чтобы показывать правку только своей
    карточки (PATCH по-прежнему доступен лишь владельцу).
    """
    store = projects.get_store()
    by_id: dict = {}
    for card in store.list_active():
        by_id[card["id"]] = card
    for card in store.list(user["id"]):
        if card.get("status") != projects.STATUS_ACTIVE:
            by_id[card["id"]] = card
    users = get_user_store()
    items = []
    for card in by_id.values():
        public = store.public_card(card)
        owner = users.get(card.get("user_id") or "")
        name = (owner or {}).get("name") or ""
        if not name:
            # имя из регистрации может быть пустым — берём ФИО из анкеты кабинета
            try:
                name = cabinet.full_name(
                    cabinet.get_store().get_profile(card.get("user_id") or "")
                )
            except cabinet.CabinetError:
                name = ""
        public["owner"] = name
        public["is_own"] = card.get("user_id") == user["id"]
        items.append(public)
    items.sort(key=lambda c: c.get("updated_at") or 0, reverse=True)
    return {"items": items}


@router.post("/projects")
def create_project(
    fields: dict = Body(default={}),
    user: dict = Depends(get_current_user),
):
    """Пустая карточка-черновик владельца токена.

    Источник — пустая карточка на странице «Проекты», когда проектов ещё
    нет: пользователь заполняет поля в форме, карточка сохраняется как
    черновик (status=active по-прежнему ставится отдельным вызовом —
    см. ProjectStore.save). Тело — те же поля, что и у PATCH; пустое тело
    создаёт карточку без полей.
    """
    try:
        saved = projects.get_store().save(user["id"], dict(fields or {}))
    except ProjectError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return projects.get_store().public_card(saved)


@router.get("/projects/{project_id}")
def get_project(project_id: str, user: dict = Depends(get_current_user)):
    """Одна карточка проекта владельца токена."""
    card = projects.get_store().get(user["id"], project_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    return projects.get_store().public_card(card)


@router.get("/projects/{project_id}/hypotheses")
def get_project_hypotheses(
    project_id: str, user: dict = Depends(get_current_user)
):
    """JSON-карточка гипотез проекта (data/hypotheses/hyp-{id}.json).

    Раздел «Проекты» общий, поэтому карточка ищется у всех владельцев
    (find), как в list_projects. Гипотезы, как и карточки, создаются
    навыком в рабочем чате; если карточки гипотез ещё нет — 404, клиент
    показывает только краткие гипотезы из поля artifacts.
    """
    if projects.get_store().find(project_id) is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    data = hypotheses.load_hypotheses(project_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Гипотезы не найдены")
    return data


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

    Руководитель может выдать участнику своей команды право редактирования
    карточек (но не удаления): такой участник правит карточку по её id.
    """
    store = projects.get_store()
    card = store.get(user["id"], project_id)
    if card is None:
        # чужая карточка: доступна, если владельц выдал право правки проектов
        card = store.find(project_id)
        if (
            card is None
            or not team.get_store().can_edit(card.get("user_id") or "", user["id"], "projects")
        ):
            raise HTTPException(status_code=404, detail="Проект не найден")
        owner_id = card["user_id"]
    else:
        owner_id = user["id"]
    try:
        saved = store.save(owner_id, dict(patch or {}, project_id=project_id))
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


@router.post("/projects/{project_id}/files")
def upload_project_file(
    project_id: str,
    kind: str = Form(...),
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """Прикрепить файл к своей карточке (multipart, владелец токена).

    kind — groundwork_files (файлы задела) или presentation_files
    (файлы презентации). Файл попадает в files/ карточки, его имя
    дописывается в соответствующий списковый параметр карточки —
    дальше он доступен для скачивания (GET /api/projects/{id}/files/{name}).
    """
    if kind not in ("groundwork_files", "presentation_files"):
        raise HTTPException(
            status_code=422,
            detail="kind должен быть groundwork_files или presentation_files",
        )
    store = projects.get_store()
    card = store.get(user["id"], project_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    content = file.file.read()
    if not content:
        raise HTTPException(status_code=422, detail="Пустой файл")
    try:
        stored_name = store.save_file(user["id"], project_id, file.filename or "file", content)
        current = list(card.get(kind) or [])
        if stored_name not in current:
            current.append(stored_name)
        saved = store.save(user["id"], {"project_id": project_id, kind: current})
    except projects.ProjectError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "ok": True,
        "field": kind,
        "filename": stored_name,
        "files": saved.get(kind) or [],
        "card": store.public_card(saved),
    }


@router.get("/projects/{project_id}/files/{filename}")
def download_project_file(
    project_id: str, filename: str, user: dict = Depends(get_current_user)
):
    """Файл карточки проекта.

    Владельцу — всегда; остальным — только файлы активных карточек: раздел
    «Проекты» общий, активная карточка видна всем вошедшим (см. list_projects),
    и участники проекта должны забирать приложенные к ней файлы. Файлы
    черновика онбординга доступны только владельцу.
    """
    store = projects.get_store()
    card = store.get(user["id"], project_id)
    if card is None:
        # чужая карточка: файлы есть только у активных (см. list_projects)
        card = store.find(project_id)
        if card is None or card.get("status") != projects.STATUS_ACTIVE:
            raise HTTPException(status_code=404, detail="Проект не найден")
    path = store.file_path(card.get("user_id") or user["id"], project_id, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(path, filename=filename)
