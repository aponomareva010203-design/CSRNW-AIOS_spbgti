"""HTTP-слой онтологий (api/routers/ontologies.py).

GET    /api/ontologies               — онтологии текущего пользователя (полные графы);
GET    /api/ontologies/{id}          — одна онтология владельца токена;
PATCH  /api/ontologies/{id}          — правка онтологии (частичный словарь полей);
POST   /api/ontologies/{id}/publish  — публикация в каталог «Данные и знания»
                                        (владелец): карточка в data/ontologies.json,
                                        граф в data/ontologies/objects/{id}.json;
DELETE /api/ontologies/{id}          — удаление, только роль «Администратор»
                                        (любой онтологии по id, вместе с публикацией).

Онтологии конструируются навыком «Конструктор онтологий» через инструменты
get_ontologies / save_ontology в рабочем чате; опубликованный граф берёт
3D-визуализатор (ontology.html). Собственные онтологии адресуются по
идентификатору владельца из Bearer-токена (get_current_user) — из запроса
он не принимается, чужие онтологии владельцу недостижимы.
"""

from fastapi import APIRouter, Body, Depends, HTTPException

from ..runtime import ontologies, team
from ..runtime.ontologies import OntologyError
from .auth import get_current_user, require_admin
from ..runtime.users import ROLE_ADMIN

router = APIRouter(prefix="/api")


@router.get("/ontologies")
def list_ontologies(user: dict = Depends(get_current_user)):
    """Онтологии владельца токена, свежие сверху."""
    store = ontologies.get_store()
    cards = store.list(user["id"])
    return {"items": [store.public_card(c) for c in cards]}


@router.get("/ontologies/{ontology_id}")
def get_ontology(ontology_id: str, user: dict = Depends(get_current_user)):
    """Одна онтология владельца токена (полный граф)."""
    card = ontologies.get_store().get(user["id"], ontology_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Онтология не найдена")
    return ontologies.get_store().public_card(card)


@router.patch("/ontologies/{ontology_id}")
def update_ontology(
    ontology_id: str,
    patch: dict = Body(default={}),
    user: dict = Depends(get_current_user),
):
    """Правка онтологии владельцем токена или администратором.

    Тело — частичный словарь полей (name, domain, description, author, tags,
    project_id, competency_questions, classes, relations, rag_hints, status):
    переданные поля перезаписываются, остальные сохраняются.

    Чужая онтология доступна также участнику команды владельца с правом
    правки онтологий (удалять её может только администратор).
    """
    store = ontologies.get_store()
    owner_id = user["id"]
    if store.get(owner_id, ontology_id) is None:
        found = store.find(ontology_id)
        allowed = found is not None and (
            user.get("role") == ROLE_ADMIN
            or team.get_store().can_edit(found.get("user_id") or "", user["id"], "ontologies")
        )
        if found is None or not allowed:
            raise HTTPException(status_code=404, detail="Онтология не найдена")
        owner_id = found["user_id"]
    try:
        saved = store.save(owner_id, dict(patch or {}, ontology_id=ontology_id))
    except OntologyError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return store.public_card(saved)


@router.post("/ontologies/{ontology_id}/publish")
def publish_ontology(ontology_id: str, user: dict = Depends(get_current_user)):
    """Публикация онтологии в каталог «Данные и знания» (владелец токена).

    Проверяет полноту графа (валидация require_complete), переводит в active
    и выкладывает карточку + файл графа; идемпотентно.
    """
    store = ontologies.get_store()
    if store.get(user["id"], ontology_id) is None:
        raise HTTPException(status_code=404, detail="Онтология не найдена")
    try:
        saved = store.save(user["id"], {"ontology_id": ontology_id, "status": "active"})
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