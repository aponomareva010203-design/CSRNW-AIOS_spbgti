"""HTTP-слой страницы «Кабинет» (api/routers/cabinet.py).

GET /api/cabinet/profile — блок «Данные пользователя» (ФИО, должности,
                            учёная степень, научные интересы);
PUT /api/cabinet/profile — сохранить блок.

Доступны пользователю только собственные данные: идентификатор берётся из
Bearer-токена (get_current_user) и из запроса не принимается — чужие блоки
недостижимы. Скрытый блок «Контекст о пользователе» (user/{id}-context.json)
через API не отдаётся вообще: он формируется планировщиком на сервере
(runtime/user_context.py) и используется только в системном промпте.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..runtime import cabinet
from ..runtime.cabinet import CabinetError, POSITION_MAX_CHARS, POSITIONS_MAX_ITEMS
from .auth import get_current_user

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
