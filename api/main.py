"""FastAPI-приложение AIOS: API каталога моделей + раздача статики."""

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles

from .catalog import CATALOG
from .schemas import ModelsResponse, ModelSource

BASE_DIR = Path(__file__).resolve().parent.parent

app = FastAPI(title="AIOS API", version="0.1.0")


@app.get("/api/models", response_model=ModelsResponse)
def list_models(source: Optional[ModelSource] = Query(default=None)) -> ModelsResponse:
    """Каталог моделей.

    Query-параметры:
        source: фильтр по источнику — только «spbti» или «huggingface».
                Неверное значение отклоняется схемой (HTTP 422).

    Порядок элементов стабилен: соответствует порядку в каталоге.
    """
    items = [m for m in CATALOG if source is None or m.source == source]
    return ModelsResponse(items=items)


# JSON-данные (вне /api), затем статика сайта — порядок как у /api/*
app.mount("/data", StaticFiles(directory=BASE_DIR / "data"), name="data")
app.mount("/", StaticFiles(directory=BASE_DIR / "frontend", html=True), name="site")
