"""Обратная связь от пользователей: предложения по улучшению платформы.

POST   /api/feedback/suggestions — сохранить предложение пользователя.
GET    /api/feedback/suggestions — все предложения (только администратор).
GET    /api/feedback/suggestions/mine — предложения текущего аккаунта со
       статусами (модалка «Предложить изменения» → «Статусы предложений»):
       невыполненные показываются как «в работе», отмеченные администратором
       в «Предложениях пользователей» — как «выполнено».
PATCH  /api/feedback/suggestions/{id} — отметка «выполнено» (только
       администратор; выполненные предложения не удаляются — остаются в
       списке с флагом done, фронтенд показывает их бледными внизу).

Предложения дописываются в JSON-файл feedback/proposals.json в корне
проекта (каждый элемент — {id, created_at, login, message, done,
done_at}); файл создаётся при первом предложении. Авторизация опциональна:
если передан валидный Bearer-токен, вместе с текстом сохраняется логин
из сессии (а не из тела запроса). Записи без полей done/done_at (старый
формат) читаются как невыполненные.
"""

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from .auth import get_current_user, get_token_store, get_user_store, require_admin

router = APIRouter(prefix="/api/feedback")

_FEEDBACK_DIR = Path(__file__).resolve().parent.parent.parent / "feedback"
_FEEDBACK_LOCK = threading.Lock()


class SuggestionIn(BaseModel):
    message: str = Field(min_length=1, max_length=5000)
    login: str | None = None


class SuggestionPatch(BaseModel):
    """Тело PATCH /api/feedback/suggestions/{id} — отметка выполнения."""

    done: bool


class SuggestionOut(BaseModel):
    ok: bool


def _path() -> Path:
    return _FEEDBACK_DIR / "proposals.json"


def _load() -> list[dict]:
    """Читает список предложений (без захвата лока — под локом вызывающего)."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(data: list[dict]) -> None:
    """Атомарно записывает список предложений (под локом вызывающего)."""
    _FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_path())


def _append_suggestion(record: dict) -> None:
    with _FEEDBACK_LOCK:
        data = _load()
        data.append(record)
        _save(data)


@router.post("/suggestions", response_model=SuggestionOut)
def submit_suggestion(
    payload: SuggestionIn,
    authorization: str | None = Header(default=None),
) -> SuggestionOut:
    login = payload.login
    if authorization:
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        user_id = get_token_store().resolve(token) if token else None
        if user_id:
            user = get_user_store().get(user_id)
            if user:
                login = user.get("login", login)
    if not (payload.message or "").strip():
        raise HTTPException(status_code=422, detail="Пустое предложение")

    _append_suggestion(
        {
            "id": uuid.uuid4().hex,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "login": login,
            "message": payload.message.strip(),
            "done": False,
            "done_at": None,
        }
    )
    return SuggestionOut(ok=True)


@router.get("/suggestions")
def list_suggestions(user: dict = Depends(require_admin)) -> dict:
    """Все предложения (включая выполненные) — для админ-панели."""
    with _FEEDBACK_LOCK:
        data = _load()
    return {"suggestions": data}


@router.get("/suggestions/mine")
def my_suggestions(user: dict = Depends(get_current_user)) -> dict:
    """Предложения текущего аккаунта, свежие сверху, со статусами.

    Привязка — по логину из сессии (тот же принцип, что при отправке):
    анонимные предложения (без учётной записи) через этот эндпоинт не
    видны. done=True — администратор отметил «выполнено» в разделе
    «Предложения пользователей»; иначе статус «в работе».
    """
    login = user.get("login")
    with _FEEDBACK_LOCK:
        data = _load()
    items = [
        {
            "id": record.get("id"),
            "message": str(record.get("message") or ""),
            "created_at": record.get("created_at"),
            "done": bool(record.get("done")),
            "done_at": record.get("done_at"),
        }
        for record in data
        if record.get("login") == login
    ]
    items.sort(key=lambda record: record.get("created_at") or "", reverse=True)
    return {"items": items}


@router.patch("/suggestions/{suggestion_id}")
def set_suggestion_done(
    suggestion_id: str,
    patch: SuggestionPatch,
    user: dict = Depends(require_admin),
) -> dict:
    """Отметка «выполнено»/«вернуть в работу». Предложения не удаляются."""
    with _FEEDBACK_LOCK:
        data = _load()
        for record in data:
            if record.get("id") == suggestion_id:
                record["done"] = bool(patch.done)
                record["done_at"] = (
                    datetime.now(timezone.utc).isoformat() if patch.done else None
                )
                _save(data)
                return record
    raise HTTPException(status_code=404, detail="Предложение не найдено")
