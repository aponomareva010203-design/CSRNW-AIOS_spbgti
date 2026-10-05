"""HTTP-слой раздела «Проекты» (api/routers/projects.py).

GET    /api/projects          — проекты всех команд (активные карточки
                                всех владельцев + свои черновики + чужие
                                черновики, где пользователь участник);
POST   /api/projects          — пустая карточка-черновик владельца
                                (пустая карточка на «Проектах»);
GET    /api/projects/{id}     — одна карточка (руководитель, участник
                                с любым уровнем доступа, администратор);
GET    /api/projects/{id}/members    — участники и их уровни доступа;
PUT    /api/projects/{id}/members/{user_id}   — выдать/сменить уровень
                                участнику (руководитель, участник с
                                «Полным доступом», администратор роли);
DELETE /api/projects/{id}/members/{user_id}   — снять явный уровень;
PATCH  /api/projects/{id}     — правка карточки (руководитель, участник
                                с правом «Редактирование» или правом
                                «проекты» из «Моей команды»);
DELETE /api/projects/{id}     — удаление карточки, только роль
                                «Администратор» (любой карточки по id);
GET  /api/projects/{id}/files/{name} — скачивание файла карточки
                                (вложение чата или загрузка из формы);
POST /api/projects/{id}/files   — прикрепить файл к карточке
                                (задел/презентация, multipart;
                                руководитель или участник с правкой);
POST /api/projects/{id}/icon    — сгенерировать (или отдать уже
                                сохранённую) SVG-иконку карточки.

Уровни доступа участников — как на GitHub (runtime/projects.LEVELS):
Просмотр (read), Редактирование (write), Полный доступ (admin). Уровень
хранится в блоке acl карточки; добавленный в состав команды (поле
participants) зарегистрированный пользователь автоматически получает
Просмотр артефактов и черновиков проекта. Свои карточки адресуются по
идентификатору владельца из Bearer-токена (get_current_user) — из запроса
он не принимается. Карточки создаются навыком «Онбординг проекта» через
инструмент save_project_card в рабочем чате.
"""

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ..runtime import cabinet, hypotheses, project_icons, projects, team
from ..runtime.projects import ProjectError
from ..runtime.users import ROLE_ADMIN
from .auth import get_current_user, get_user_store, require_admin

router = APIRouter(prefix="/api")


def _visible_card(store, project_id: str, user: dict) -> dict | None:
    """Карточка проекта, если пользователю она видна по общим правилам
    раздела «Проекты» (своя, найденная по id, тестовая — только админам)."""
    card = store.get(user["id"], project_id)
    if card is not None:
        return card
    card = store.find(project_id)
    if card is None:
        return None
    if projects.is_admin_only(card) and user.get("role") != ROLE_ADMIN:
        return None
    return card


@router.get("/projects")
def list_projects(user: dict = Depends(get_current_user)):
    """Проекты всех команд — активные карточки всех владельцев.

    Раздел «Проекты» общий: любой вошедший пользователь видит активные
    карточки всех владельцев (руководитель, состав команды, гипотезы —
    всё в public_card). Свои черновики онбординга видны только владельцу;
    черновики чужих проектов видны участникам с доступом (read+ по
    access_level) — добавление в состав команды открывает и черновики.
    Тестовые карточки (admin_only) видны только администраторам. Каждая
    карточка дополняется именем руководителя (owner), флагом is_own и
    уровнем доступа пользователя my_level (owner/read/write/admin; None —
    только общий просмотр активной карточки).
    """
    store = projects.get_store()
    is_admin = user.get("role") == ROLE_ADMIN
    profile = projects.user_profile(user["id"])  # одна анкета на весь список
    by_id: dict = {}
    for card in store.list_active():
        by_id[card["id"]] = card
    for card in store.list(user["id"]):
        if card.get("status") != projects.STATUS_ACTIVE:
            by_id[card["id"]] = card
    for card in store.list_all():
        if card["id"] in by_id or card.get("status") == projects.STATUS_ACTIVE:
            continue
        if projects.is_admin_only(card) and not is_admin:
            continue
        if projects.access_level(card, user, profile):
            by_id[card["id"]] = card
    users = get_user_store()
    items = []
    for card in by_id.values():
        if projects.is_admin_only(card) and not is_admin:
            continue
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
        public["my_level"] = projects.access_level(card, user, profile)
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
    создаёт карточку без полей. Блок acl при создании игнорируется —
    права участников выдаются после сохранения через .../members.
    """
    patch = dict(fields or {})
    patch.pop("acl", None)
    try:
        saved = projects.get_store().save(user["id"], patch)
    except ProjectError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return projects.get_store().public_card(saved)


@router.get("/projects/{project_id}")
def get_project(project_id: str, user: dict = Depends(get_current_user)):
    """Одна карточка проекта: руководителю, участнику с уровнем доступа,
    администратору роли; остальным — 404 (как и для несуществующей)."""
    store = projects.get_store()
    card = store.get(user["id"], project_id)
    if card is None:
        card = _visible_card(store, project_id, user)
        if card is None or not (
            projects.access_level(card, user) or user.get("role") == ROLE_ADMIN
        ):
            raise HTTPException(status_code=404, detail="Проект не найден")
    public = store.public_card(card)
    public["my_level"] = projects.access_level(card, user)
    return public


@router.get("/projects/{project_id}/members")
def list_project_members(project_id: str, user: dict = Depends(get_current_user)):
    """Участники проекта и их уровни доступа (видны команде и админам).

    items — зарегистрированные пользователи: руководитель (level: owner),
    участники с явным уровнем из acl (source: acl) и совпавшие по ФИО
    с полем participants — автоматический Просмотр (source: matched).
    unregistered — записи состава команды без совпавшей учётной записи
    (доступ откроется после регистрации/совпадения ФИО).
    """
    store = projects.get_store()
    card = _visible_card(store, project_id, user)
    if card is None or not (
        projects.access_level(card, user) or user.get("role") == ROLE_ADMIN
    ):
        raise HTTPException(status_code=404, detail="Проект не найден")

    users_store = get_user_store()
    cabinet_store = cabinet.get_store()
    user_needles: list = []
    for u in users_store.list_all():
        needles = projects.participant_needles(u, cabinet_store.get_profile(u["id"]))
        if needles:
            user_needles.append((u["id"], needles))

    def row_for(member_id: str) -> dict:
        u = users_store.get(member_id) or {}
        profile = cabinet_store.get_profile(member_id)
        return {
            "user_id": member_id,
            "full_name": cabinet.full_name(profile) or (u.get("name") or ""),
            "position": (profile.get("positions") or [""])[0],
        }

    owner_id = card.get("user_id") or ""
    acl = card.get("acl") if isinstance(card.get("acl"), dict) else {}
    items = []
    if owner_id:
        row = row_for(owner_id)
        row["level"] = projects.LEVEL_OWNER
        row["source"] = "owner"
        items.append(row)
    matched = projects.matched_participant_ids(card, user_needles)
    for member_id in sorted(a for a in acl if isinstance(acl.get(a), str)):
        level = acl[member_id]
        if member_id == owner_id or level not in projects.LEVELS:
            continue
        row = row_for(member_id)
        row["level"] = level
        row["source"] = "acl"
        items.append(row)
    for member_id in matched:
        if member_id == owner_id or member_id in acl:
            continue
        row = row_for(member_id)
        row["level"] = projects.LEVEL_READ
        row["source"] = "matched"
        items.append(row)
    return {
        "items": items,
        "unregistered": projects.unmatched_participant_names(card, user_needles),
    }


def _manager_access(card: dict, user: dict) -> bool:
    """Может ли пользователь управлять участниками и правами проекта:
    руководитель, участник с «Полным доступом» или роль «Администратор»."""
    if user.get("role") == ROLE_ADMIN:
        return True
    level = projects.access_level(card, user)
    return level in (projects.LEVEL_OWNER, projects.LEVEL_ADMIN)


@router.put("/projects/{project_id}/members/{member_id}")
def set_project_member(
    project_id: str,
    member_id: str,
    body: dict = Body(default={}),
    user: dict = Depends(get_current_user),
):
    """Выдача/смена уровня доступа участника проекта.

    Тело: {"level": "read"|"write"|"admin"} (Просмотр, Редактирование,
    Полный доступ — названия как на GitHub). Менять уровни может
    руководитель проекта, участник с «Полным доступом» и администратор
    роли; выдавать — только пользователям из состава команды (ФИО совпало
    с полем participants либо уже есть явный уровень). Себе уровень
    сменить нельзя. Явный уровень перекрывает автопросмотр по ФИО;
    удаление артефактов он не даёт (только роль «Администратор»).
    """
    store = projects.get_store()
    card = _visible_card(store, project_id, user)
    if card is None or not _manager_access(card, user):
        raise HTTPException(status_code=404, detail="Проект не найден")
    level = str((body or {}).get("level") or "")
    if level not in projects.LEVELS:
        raise HTTPException(
            status_code=422,
            detail="Уровень доступа должен быть одним из: " + ", ".join(projects.LEVELS),
        )
    if member_id == user["id"]:
        raise HTTPException(status_code=400, detail="Себе уровень выдать нельзя — у руководителя все права есть")
    users_store = get_user_store()
    member = users_store.get(member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    needles = projects.participant_needles(
        member, cabinet.get_store().get_profile(member_id)
    )
    acl = card.get("acl") if isinstance(card.get("acl"), dict) else {}
    if member_id not in acl and member_id not in projects.matched_participant_ids(
        card, [(member_id, needles)]
    ):
        raise HTTPException(
            status_code=404, detail="Участник не найден в составе команды проекта"
        )
    try:
        projects.set_member_level(project_id, member_id, level)
    except ProjectError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"ok": True, "user_id": member_id, "level": level}


@router.delete("/projects/{project_id}/members/{member_id}")
def remove_project_member(
    project_id: str,
    member_id: str,
    user: dict = Depends(get_current_user),
):
    """Снятие явного уровня доступа участника.

    Автопросмотр по ФИО остаётся, пока человек указан в поле participants
    карточки: чтобы закрыть доступ полностью, руководитель убирает его из
    состава команды при правке карточки. Возвращает оставшийся уровень
    (level: null — доступа больше нет).
    """
    store = projects.get_store()
    card = _visible_card(store, project_id, user)
    if card is None or not _manager_access(card, user):
        raise HTTPException(status_code=404, detail="Проект не найден")
    users_store = get_user_store()
    member = users_store.get(member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    acl = card.get("acl") if isinstance(card.get("acl"), dict) else {}
    needles = projects.participant_needles(
        member, cabinet.get_store().get_profile(member_id)
    )
    if member_id not in acl and member_id not in projects.matched_participant_ids(
        card, [(member_id, needles)]
    ):
        raise HTTPException(
            status_code=404, detail="Участник не найден в составе команды проекта"
        )
    try:
        saved = projects.remove_member_level(project_id, member_id)
    except ProjectError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "ok": True,
        "user_id": member_id,
        # уровень после снятия: автопросмотр по ФИО, пока человек в составе команды
        "level": projects.access_level(saved, member),
    }


@router.get("/projects/{project_id}/hypotheses")
def get_project_hypotheses(
    project_id: str, user: dict = Depends(get_current_user)
):
    """JSON-карточка гипотез проекта (data/hypotheses/hyp-{id}.json).

    Раздел «Проекты» общий, поэтому карточка ищется у всех владельцев
    (find), как в list_projects. Гипотезы, как и карточки, создаются
    навыком в рабочем чате; если карточки гипотез ещё нет — 404, клиент
    показывает только краткие гипотезы из поля artifacts. Тестовый проект
    (admin_only) отдаётся только администраторам.
    """
    card = projects.get_store().find(project_id)
    if card is None or (
        projects.is_admin_only(card) and user.get("role") != ROLE_ADMIN
    ):
        raise HTTPException(status_code=404, detail="Проект не найден")
    data = hypotheses.load_hypotheses(project_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Гипотезы не найдены")
    return data


@router.patch("/projects/{project_id}/hypotheses")
def update_project_hypothesis(
    project_id: str,
    patch: dict = Body(default={}),
    user: dict = Depends(get_current_user),
):
    """Правка текста одной гипотезы (title/statement) в кабинете.

    Тело: {"section": "ontology"|"rag"|"models"|"agents"|"polygons"|
    "dashboards", "index": int, "title"?: str, "statement"?: str}.
    Права — как у PATCH /api/projects/{id}: владелец карточки, участник
    с правом «Редактирование» на проект, участник команды с правом правки
    проектов («Моя команда»). Структуру карточки (число гипотез, skipped
    и т.п.) эндпоинт не меняет — только текст указанной записи.
    """
    store = projects.get_store()
    card = store.get(user["id"], project_id)
    if card is None:
        card = store.find(project_id)
        if (
            card is None
            or projects.is_admin_only(card) and user.get("role") != ROLE_ADMIN
            or not (
                team.get_store().can_edit(card.get("user_id") or "", user["id"], "projects")
                or projects.level_allows(projects.access_level(card, user), projects.LEVEL_WRITE)
            )
        ):
            raise HTTPException(status_code=404, detail="Проект не найден")
    section = str((patch or {}).get("section") or "")
    try:
        index = int((patch or {}).get("index"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="index должен быть целым числом")
    try:
        data = hypotheses.save_hypothesis_item(
            project_id,
            section,
            index,
            title=(patch or {}).get("title"),
            statement=(patch or {}).get("statement"),
        )
    except hypotheses.HypothesesError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return data


@router.post("/projects/{project_id}/icon")
async def generate_project_icon(
    project_id: str,
    user: dict = Depends(get_current_user),
):
    """Индивидуальная SVG-иконка карточки (генерация ИИ по описанию проекта).

    Доступна любому, кто видит карточку (см. list_projects): иконка
    декоративна и общая, не привязана к владельцу. Если иконка уже
    сгенерирована — отдаём сохранённую, повторной генерации не делаем
    (idempotent, экономия токенов). Пустой результат (ошибка генерации,
    мусорный ответ модели или LLM не настроена) не сохраняется — карточка
    остаётся с фронтовым плейсхолдером, следующий вызов попробует снова.
    """
    store = projects.get_store()
    card = _visible_card(store, project_id, user)
    if card is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    if card.get("icon_svg"):
        return {"icon_svg": card["icon_svg"]}
    svg = await project_icons.generate_icon(card, user_id=user["id"])
    if not svg:
        return {"icon_svg": None}
    saved = store.set_icon(project_id, svg)
    return {"icon_svg": (saved or {}).get("icon_svg")}


@router.patch("/projects/{project_id}")
def update_project(
    project_id: str,
    patch: dict = Body(default={}),
    user: dict = Depends(get_current_user),
):
    """Правка карточки руководителем проекта или участником с правкой.

    Тело — частичный словарь полей карточки (title, description, goal,
    team_size, participants, departments, budget, groundwork,
    groundwork_links/files, presentation_files, presentation, status):
    переданные поля перезаписываются, остальные сохраняются.

    Редактировать может руководитель, участник с уровнем «Редактирование»/
    «Полный доступ» на проект (см. access_level) либо участник команды
    с правом правки проектов («Моя команда»). Участнику недоступны поля
    acl (права выдаёт .../members) и статусные переключения status /
    admin_only (черновик подтверждает руководитель); удаление остаётся
    у роли «Администратор».
    """
    store = projects.get_store()
    is_admin = user.get("role") == ROLE_ADMIN
    card = store.get(user["id"], project_id)
    own = card is not None
    if card is None:
        # чужая карточка: доступна участнику с правом редактирования
        card = store.find(project_id)
        level = projects.access_level(card, user) if card else None
        if (
            card is None
            or projects.is_admin_only(card) and not is_admin
            or not (
                is_admin
                or projects.level_allows(level, projects.LEVEL_WRITE)
                or team.get_store().can_edit(card.get("user_id") or "", user["id"], "projects")
            )
        ):
            raise HTTPException(status_code=404, detail="Проект не найден")
        owner_id = card["user_id"]
    else:
        owner_id = user["id"]
    patch = dict(patch or {})
    patch.pop("acl", None)  # права участников меняет только эндпоинт .../members
    if not own and not is_admin:
        # статус черновика и флаг тестовой карточки переключает руководитель
        patch.pop("status", None)
        patch.pop("admin_only", None)
    try:
        saved = store.save(owner_id, {**patch, "project_id": project_id})
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
    """Прикрепить файл к карточке (multipart).

    kind — groundwork_files (файлы задела) или presentation_files
    (файлы презентации). Файл попадает в files/ карточки, его имя
    дописывается в соответствующий списковый параметр карточки —
    дальше он доступен для скачивания (GET /api/projects/{id}/files/{name}).
    Загружают руководитель и участники с правом редактирования проекта.
    """
    if kind not in ("groundwork_files", "presentation_files"):
        raise HTTPException(
            status_code=422,
            detail="kind должен быть groundwork_files или presentation_files",
        )
    store = projects.get_store()
    is_admin = user.get("role") == ROLE_ADMIN
    card = store.get(user["id"], project_id)
    own = card is not None
    if card is None:
        card = store.find(project_id)
        level = projects.access_level(card, user) if card else None
        if (
            card is None
            or projects.is_admin_only(card) and not is_admin
            or not (
                is_admin
                or projects.level_allows(level, projects.LEVEL_WRITE)
                or team.get_store().can_edit(card.get("user_id") or "", user["id"], "projects")
            )
        ):
            raise HTTPException(status_code=404, detail="Проект не найден")
    owner_id = card["user_id"]
    content = file.file.read()
    if not content:
        raise HTTPException(status_code=422, detail="Пустой файл")
    try:
        stored_name = store.save_file(owner_id, project_id, file.filename or "file", content)
        current = list(card.get(kind) or [])
        if stored_name not in current:
            current.append(stored_name)
        saved = store.save(owner_id, {"project_id": project_id, kind: current})
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

    Владельцу — всегда; участникам с доступом (read+) — и файлы черновика;
    остальным — только файлы активных карточек: раздел «Проекты» общий,
    активная карточка видна всем вошедшим (см. list_projects). Файлы
    черновика онбординга без доступа к проекту недоступны.
    """
    store = projects.get_store()
    is_admin = user.get("role") == ROLE_ADMIN
    card = store.get(user["id"], project_id)
    if card is None:
        card = store.find(project_id)
        if card is None or (projects.is_admin_only(card) and not is_admin):
            raise HTTPException(status_code=404, detail="Проект не найден")
        if card.get("status") != projects.STATUS_ACTIVE and not (
            is_admin or projects.access_level(card, user)
        ):
            raise HTTPException(status_code=404, detail="Проект не найден")
    path = store.file_path(card.get("user_id") or user["id"], project_id, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(path, filename=filename)
