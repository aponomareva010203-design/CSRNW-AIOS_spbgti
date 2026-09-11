"""Обратная связь от пользователей: предложения по улучшению платформы.

POST /api/feedback/suggestions — сохранить предложение пользователя.

Предложения дописываются в JSON-файл feedback/proposals.json в корне
проекта (каждый элемент — {id, created_at, login, message}); файл
создаётся при первом предложении. Авторизация опциональна: если
передан валидный Bearer-токен, вместе с текстом сохраняется логин
из сессии (а не из тела запроса).
"""

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from .auth import get_token_store, get_user_store

router = APIRouter(prefix="/api/feedback")

_FEEDBACK_DIR = Path(__file__).resolve().parent.parent.parent / "feedback"
_FEEDBACK_LOCK = threading.Lock()


class SuggestionIn(BaseModel):
    message: str = Field(min_length=1, max_length=5000)
    login: str | None = None


class SuggestionOut(BaseModel):
    ok: bool


def _append_suggestion(record: dict) -> None:
    path = _FEEDBACK_DIR / "proposals.json"
    with _FEEDBACK_LOCK:
        _FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                data = []
        except (FileNotFoundError, json.JSONDecodeError):
            data = []
        data.append(record)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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
        }
    )
    return SuggestionOut(ok=True)
