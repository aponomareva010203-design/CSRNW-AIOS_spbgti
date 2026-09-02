"""Pydantic-схемы каталога моделей AIOS."""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

ModelSource = Literal["spbti", "huggingface"]

MAX_DESCRIPTION_LEN = 200


class ModelCard(BaseModel):
    """Карточка модели в каталоге."""

    id: str
    name: str
    source: ModelSource
    provider: str
    model_id: Optional[str] = None
    image_url: Optional[str] = None
    description_ru: str = Field(default="", max_length=MAX_DESCRIPTION_LEN)
    tags: List[str] = Field(default_factory=list)
    access_url: Optional[str] = None
    is_placeholder: bool = False

    @field_validator("description_ru")
    @classmethod
    def truncate_description(cls, value: str) -> str:
        """Жёсткое ограничение описания 200 символами на стороне сервера."""
        value = (value or "").strip()
        return value[:MAX_DESCRIPTION_LEN]


class ModelsResponse(BaseModel):
    """Ответ GET /api/models."""

    items: List[ModelCard]
