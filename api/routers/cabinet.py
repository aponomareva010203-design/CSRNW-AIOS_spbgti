"""HTTP-слой страницы «Кабинет» (api/routers/cabinet.py).

GET    /api/cabinet/profile         — блок «Данные пользователя» (ФИО, должности,
                                      учёная степень, научные интересы);
PUT    /api/cabinet/profile         — сохранить блок;
GET    /api/cabinet/sessions        — история сессий владельца токена (сводки);
DELETE /api/cabinet/sessions/{id}   — удалить свою сессию из истории;
GET    /api/cabinet/projects        — раздел «Результаты работы»: проекты,
                                      созданные пользователем (role: owner)
                                      и где он указан среди участников
                                      (role: participant, поиск по ФИО);
GET    /api/cabinet/team            — раздел «Моя команда»: зарегистрированные
                                      пользователи из карточек, где владелец
                                      кабинета руководит или участвует
                                      (руководитель/участник проекта);
PUT    /api/cabinet/team/{id}/permissions — выдать/снять права редактирования
                                      (без удаления) проектов, онтологий и
                                      RAG-систем участнику своей команды.

Доступны пользователю только собственные данные: идентификатор берётся из
Bearer-токена (get_current_user) и из запроса не принимается — чужие блоки
недостижимы. Скрытый блок «Контекст о пользователе» (user/{id}-context.json)
через API не отдаётся вообще: он формируется планировщиком на сервере
(runtime/user_context.py) и используется только в системном промпте.
"""

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..runtime import cabinet
from ..runtime import hypotheses as hypotheses_runtime
from ..runtime import projects as projects_runtime
from ..runtime import team as team_runtime
from ..runtime.cabinet import CabinetError, POSITION_MAX_CHARS, POSITIONS_MAX_ITEMS
from ..runtime.config import get_settings
from ..runtime.projects import _name_matches, _name_tokens, participant_needles
from ..runtime.sessions import SessionStore
from ..runtime.team import PERM_KINDS
from .auth import get_current_user, get_user_store

router = APIRouter(prefix="/api")


class CabinetProfileRequest(BaseModel):
    """Тело PUT /api/cabinet/profile — блок «Данные пользователя»."""

    last_name: str = Field(default="", max_length=cabinet.NAME_MAX_CHARS)
    first_name: str = Field(default="", max_length=cabinet.NAME_MAX_CHARS)
    patronymic: str = Field(default="", max_length=cabinet.NAME_MAX_CHARS)
    positions: list[str] = Field(
        default_factory=list,
        max_length=POSITIONS_MAX_ITEMS,  # не более 10 должностей
    )
    degree: str = Field(default="", max_length=cabinet.DEGREE_MAX_CHARS)
    interests: str = Field(default="", max_length=cabinet.INTERESTS_MAX_CHARS)


@router.get("/cabinet/profile")
def get_profile(user: dict = Depends(get_current_user)):
    """Блок «Данные пользователя» — только собственный, по токену."""
    return cabinet.get_store().get_profile(user["id"])


@router.put("/cabinet/profile")
def save_profile(req: CabinetProfileRequest, user: dict = Depends(get_current_user)):
    """Сохраняет блок «Данные пользователя» в user/{id}-profile.json."""
    for position in req.positions:
        if len(position.strip()) > POSITION_MAX_CHARS:
            raise HTTPException(
                status_code=422, detail=f"Каждая должность — до {POSITION_MAX_CHARS} символов"
            )
    try:
        return cabinet.get_store().save_profile(
            user["id"],
            last_name=req.last_name,
            first_name=req.first_name,
            patronymic=req.patronymic,
            positions=req.positions,
            degree=req.degree,
            interests=req.interests,
        )
    except CabinetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


# ----- история сессий -----


def _session_store() -> SessionStore:
    """То же хранилище, что у рабочего чата (api/routers/chat.py)."""
    return SessionStore(get_settings().runtime_dir / "sessions")


@router.get("/cabinet/sessions")
def list_sessions(user: dict = Depends(get_current_user)):
    """История сессий владельца токена: сводки, свежие сверху."""
    return {"items": _session_store().summaries(user_id=user["id"])}


@router.delete("/cabinet/sessions/{session_id}")
def delete_session(session_id: str, user: dict = Depends(get_current_user)):
    """Удаляет свою сессию из истории; чужая сессия неотличима от несуществующей."""
    store = _session_store()
    session = store.get(session_id)
    if session is None or session.get("user_id") != user["id"]:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    store.delete(session_id)
    return {"ok": True, "deleted": session_id}


# ----- результаты работы (проекты) -----


def _with_hypotheses(item: dict) -> dict:
    """Дополняет карточку проекта признаком сформулированных гипотез
    (hypotheses_done — по JSON-карточке hyp-{id}.json, а не по полю
    artifacts, которое навык «Гипотезы проекта» не заполняет)."""
    item["hypotheses_done"] = hypotheses_runtime.hypotheses_done(item.get("id") or "")
    return item


@router.get("/cabinet/projects")
def list_result_projects(user: dict = Depends(get_current_user)):
    """Проекты для раздела «Результаты работы».

    Входят карточки, созданные пользователем (role: "owner"), и активные
    карточки других владельцев, где он указан среди участников (role:
    "participant" — совпадение по вариантам ФИО из анкеты «Кабинета»).
    """
    store = projects_runtime.get_store()
    profile = cabinet.get_store().get_profile(user["id"])

    items = []
    seen = set()
    for card in store.list(user["id"]):
        seen.add(card["id"])
        items.append(_with_hypotheses(dict(store.public_card(card), role="owner")))
    for card in store.find_participant_cards(participant_needles(user, profile)):
        if card["id"] in seen:
            continue
        items.append(_with_hypotheses(dict(store.public_card(card), role="participant")))
    items.sort(key=lambda c: c.get("updated_at") or 0, reverse=True)
    return {"items": items}


# ----- моя команда -----

STATUS_OWNER = "руководитель проекта"
STATUS_PARTICIPANT = "участник проекта"


def _my_cards(user: dict, profile: dict) -> list:
    """Карточки, в которых пользователь состоит: свои (руководит) и с участием."""
    store = projects_runtime.get_store()
    cards: list = []
    seen: set = set()
    for card in store.list(user["id"]):
        seen.add(card["id"])
        cards.append(card)
    for card in store.find_participant_cards(participant_needles(user, profile)):
        if card["id"] not in seen:
            cards.append(card)
    return cards


def _registered_participants(entries: list[str], user_needles: list[tuple[str, list[str]]]) -> list[str]:
    """Идентификаторы зарегистрированных пользователей, чьи ФИО-варианты
    совпали с одной из записей участников карточки (entries)."""
    matched: list[str] = []
    for entry in entries:
        entry_tokens = _name_tokens(entry)
        if len(entry_tokens) < 2 or len(entry_tokens[0]) < 4:
            continue
        for user_id, needles in user_needles:
            if any(_name_matches(entry_tokens, _name_tokens(n)) for n in needles):
                if user_id not in matched:
                    matched.append(user_id)
    return matched


def _team_rows(user: dict) -> list[dict]:
    """Строки таблицы «Моя команда» по карточкам, где пользователь состоит."""
    users_store = get_user_store()
    cabinet_store = cabinet.get_store()
    profile = cabinet_store.get_profile(user["id"])
    cards = _my_cards(user, profile)

    # варианты ФИО каждого зарегистрированного пользователя — один раз
    user_needles: list[tuple[str, list[str]]] = []
    for u in users_store.list_all():
        u_profile = cabinet_store.get_profile(u["id"])
        needles = participant_needles(u, u_profile)
        if needles:
            user_needles.append((u["id"], needles))

    rows: dict[str, dict] = {}

    def row_for(user_id: str) -> dict:
        if user_id not in rows:
            u = users_store.get(user_id) or {}
            u_profile = cabinet_store.get_profile(user_id)
            rows[user_id] = {
                "user_id": user_id,
                "full_name": cabinet.full_name(u_profile) or (u.get("name") or ""),
                "position": (u_profile.get("positions") or [""])[0],
                "statuses": [],
                # названия проектов по статусу — для подсказки при наведении на чип
                "projects": {},
                "permissions": team_runtime.get_store().member_permissions(user["id"], user_id),
            }
        return rows[user_id]

    for card in cards:
        title = str(card.get("title") or "Без названия")
        owner_id = card.get("user_id") or ""
        if owner_id and users_store.get(owner_id) is not None:
            row = row_for(owner_id)
            if STATUS_OWNER not in row["statuses"]:
                row["statuses"].append(STATUS_OWNER)
            projects = row["projects"].setdefault(STATUS_OWNER, [])
            if title not in projects:
                projects.append(title)
        # участники разделяются запятыми, точками с запятой и переносами строк
        entries = [e for e in re.split(r"[;,\n\r]+", str(card.get("participants") or "")) if e.strip()]
        for entry in entries:
            for user_id in _registered_participants([entry], user_needles):
                row = row_for(user_id)
                if STATUS_PARTICIPANT not in row["statuses"]:
                    row["statuses"].append(STATUS_PARTICIPANT)
                projects = row["projects"].setdefault(STATUS_PARTICIPANT, [])
                if title not in projects:
                    projects.append(title)

    ordered = list(rows.values())
    # руководитель кабинета — первым, дальше по ФИО
    ordered.sort(key=lambda r: (r["user_id"] != user["id"], r["full_name"].lower()))
    for row in ordered:
        if row["user_id"] == user["id"]:
            row["permissions"] = None  # у руководителя все права, выдача не нужна
    return ordered


class TeamPermissionsRequest(BaseModel):
    """Тело PUT /api/cabinet/team/{id}/permissions."""

    permissions: dict[str, bool] = Field(default_factory=dict)


@router.get("/cabinet/team")
def list_team(user: dict = Depends(get_current_user)):
    """Таблица «Моя команда» владельца токена.

    Строки — зарегистрированные пользователи из карточек проектов, где
    владелец кабинета руководит или указан среди участников: владелец
    карточки — «руководитель проекта», совпавшие по ФИО пользователи —
    «участник проекта». Права отдаёт хранилище runtime/team.py.
    """
    return {"items": _team_rows(user)}


@router.put("/cabinet/team/{user_id}/permissions")
def set_team_permissions(
    user_id: str,
    req: TeamPermissionsRequest,
    user: dict = Depends(get_current_user),
):
    """Выдача прав редактирования участнику своей команды.

    Менять права может только руководитель (владелец токена), только для
    участника своей команды и только по известным видам артефактов; себе —
    нельзя. Право даёт редактирование (PATCH) карточек, удаление остаётся
    у роли «Администратор».
    """
    unknown = [k for k in req.permissions if k not in PERM_KINDS]
    if unknown:
        raise HTTPException(
            status_code=422, detail="Неизвестные виды артефактов: " + ", ".join(unknown)
        )
    if user_id == user["id"]:
        raise HTTPException(status_code=400, detail="Себе права выдать нельзя — они уже есть")
    member_ids = {row["user_id"] for row in _team_rows(user)}
    if user_id not in member_ids:
        raise HTTPException(status_code=404, detail="Участник не найден в вашей команде")
    try:
        saved = team_runtime.get_store().set_member_permissions(
            user["id"], user_id, req.permissions
        )
    except team_runtime.TeamError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"ok": True, "user_id": user_id, "permissions": saved}
