"""HTTP-слой онтологий (api/routers/ontologies.py).

GET    /api/ontologies               — онтологии текущего пользователя +
                                        онтологии, привязанные к проектам,
                                        где пользователь участник (Просмотр+);
GET    /api/ontologies/{id}          — одна онтология (своя, или привязанная
                                        к проекту с доступом);
PATCH  /api/ontologies/{id}          — правка онтологии (частичный словарь
                                        полей): владелец, администратор роли
                                        или участник проекта с «Редактированием»;
POST   /api/ontologies/{id}/publish  — публикация в каталог «Данные и знания»:
                                        владелец или участник проекта с
                                        «Полным доступом»;
DELETE /api/ontologies/{id}          — удаление, только роль «Администратор»
                                        (любой онтологии по id, вместе с
                                        публикацией).

Онтологии конструируются навыком «Конструктор онтологий» через инструменты
get_ontologies / save_ontology в рабочем чате; опубликованный граф берёт
3D-визуализатор (ontology.html). Собственные онтологии адресуются по
идентификатору владельца из Bearer-токена (get_current_user) — из запроса
он не принимается. Онтология, привязанная к проекту (project_id), открывается
участникам проекта с уровнем Просмотр и выше (см. runtime/projects.LEVELS —
названия уровней как на GitHub).
"""

from fastapi import APIRouter, Body, Depends, HTTPException

from ..runtime import ontologies, projects, team
from ..runtime.ontologies import OntologyError
from .auth import get_current_user, require_admin
from ..runtime.users import ROLE_ADMIN

router = APIRouter(prefix="/api")


def _shared_card(store, card: dict, user: dict) -> dict | None:
    """Публичная карточка чужой онтологии для участника проекта, к которому
    она привязана (Просмотр и выше); None — доступа нет. Дополняет карточку
    флагом shared и уровнем my_level (клиент прячет правку чужих карточек)."""
    if card.get("admin_only") and user.get("role") != ROLE_ADMIN:
        return None
    level = projects.artifact_level(card.get("project_id") or "", user)
    if not projects.level_allows(level, projects.LEVEL_READ):
        return None
    public = store.public_card(card)
    public["shared"] = True
    public["my_level"] = level
    return public


@router.get("/ontologies")
def list_ontologies(user: dict = Depends(get_current_user)):
    """Онтологии владельца токена + привязанные к его проектам, свежие сверху."""
    store = ontologies.get_store()
    items: list = []
    seen: set = set()
    for card in store.list(user["id"]):
        seen.add(card["id"])
        items.append(store.public_card(card))
    for card in store.list_all():
        if card["id"] in seen or not card.get("project_id"):
            continue
        shared = _shared_card(store, card, user)
        if shared is not None:
            items.append(shared)
    items.sort(key=lambda c: c.get("updated_at") or 0, reverse=True)
    return {"items": items}


@router.get("/ontologies/{ontology_id}")
def get_ontology(ontology_id: str, user: dict = Depends(get_current_user)):
    """Одна онтология: своя — полный граф; привязанная к проекту с доступом —
    тоже (флаг shared, уровень my_level)."""
    store = ontologies.get_store()
    card = store.get(user["id"], ontology_id)
    if card is not None:
        return store.public_card(card)
    card = store.find(ontology_id)
    shared = _shared_card(store, card, user) if card is not None else None
    if shared is None:
        raise HTTPException(status_code=404, detail="Онтология не найдена")
    return shared


@router.patch("/ontologies/{ontology_id}")
def update_ontology(
    ontology_id: str,
    patch: dict = Body(default={}),
    user: dict = Depends(get_current_user),
):
    """Правка онтологии владельцем токена, администратором роли или
    участником проекта с правом редактирования.

    Тело — частичный словарь полей (name, domain, description, author, tags,
    project_id, competency_questions, classes, relations, rag_hints, status):
    переданные поля перезаписываются, остальные сохраняются.

    Участник с правом «Редактирование» на проект, к которому привязана
    онтология, правит её без права удаления (удаление — только роль
    «Администратор»). Тестовая онтология (admin_only) правится только
    администраторами.
    """
    store = ontologies.get_store()
    owner_id = user["id"]
    if store.get(owner_id, ontology_id) is None:
        found = store.find(ontology_id)
        is_admin = user.get("role") == ROLE_ADMIN
        allowed = found is not None and (
            is_admin
            or (
                not found.get("admin_only")
                and (
                    team.get_store().can_edit(
                        found.get("user_id") or "", user["id"], "ontologies"
                    )
                    or projects.level_allows(
                        projects.artifact_level(found.get("project_id") or "", user),
                        projects.LEVEL_WRITE,
                    )
                )
            )
        )
        if not allowed:
            raise HTTPException(status_code=404, detail="Онтология не найдена")
        owner_id = found["user_id"]
    try:
        saved = store.save(owner_id, dict(patch or {}, ontology_id=ontology_id))
    except OntologyError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return store.public_card(saved)


@router.post("/ontologies/{ontology_id}/publish")
def publish_ontology(ontology_id: str, user: dict = Depends(get_current_user)):
    """Публикация онтологии в каталог «Данные и знания».

    Публикует владелец токена или участник проекта с «Полным доступом»
    (read/write публикацию не дают — она открывает граф всем посетителям
    сайта). Проверяет полноту графа (валидация require_complete), переводит
    в active и выкладывает карточку + файл графа; идемпотентно.
    """
    store = ontologies.get_store()
    owner_id = user["id"]
    if store.get(owner_id, ontology_id) is None:
        found = store.find(ontology_id)
        if found is None or found.get("admin_only") and user.get("role") != ROLE_ADMIN:
            raise HTTPException(status_code=404, detail="Онтология не найдена")
        level = projects.artifact_level(found.get("project_id") or "", user)
        if not (
            user.get("role") == ROLE_ADMIN
            or projects.level_allows(level, projects.LEVEL_ADMIN)
        ):
            raise HTTPException(status_code=404, detail="Онтология не найдена")
        owner_id = found["user_id"]
    try:
        saved = store.save(owner_id, {"ontology_id": ontology_id, "status": "active"})
    except OntologyError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "ok": True,
        "ontology": store.public_card(saved),
        "catalog_entry": store.catalog_entry(saved),
    }


@router.delete("/ontologies/{ontology_id}")
def delete_ontology(ontology_id: str, _: dict = Depends(require_admin)):
    """Удаление онтологии — только роль «Администратор», любой по id."""
    store = ontologies.get_store()
    card = store.find(ontology_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Онтология не найдена")
    store.delete(card["user_id"], ontology_id)
    return {"ok": True, "deleted": ontology_id}
