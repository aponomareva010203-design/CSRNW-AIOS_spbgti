"""HTTP-слой полигонов (api/routers/polygons.py).

GET    /api/polygons              — примеры платформы (всем) + свои полигоны (вошедшим);
GET    /api/polygons/{id}         — один полигон (пример или свой);
POST   /api/polygons              — создать / обновить свой полигон (upsert по id);
DELETE /api/polygons/{id}         — удалить свой полигон;
POST   /api/polygons/simulate     — прогон сценарной модели (без входа, без LLM);
POST   /api/polygons/panel        — ИИ-фокус-группа по идее (вход + настроенный LLM).

Модель и хранилище — api/runtime/polygons.py.
"""

import asyncio

from fastapi import APIRouter, Body, Depends, HTTPException

from ..runtime import polygons
from ..runtime.llm_settings import effective_config_for
from ..runtime.model_engine import author_name
from ..runtime.polygons import PolygonError
from .auth import get_current_user, get_optional_user

router = APIRouter(prefix="/api")

PANEL_TIMEOUT = 90.0


@router.get("/polygons")
def list_polygons(user: dict | None = Depends(get_optional_user)):
    store = polygons.get_store()
    mine = store.list(user["id"]) if user else []
    return {"examples": store.examples(), "items": mine, "metrics": polygons.METRICS}


@router.post("/polygons/simulate")
def simulate(payload: dict = Body(...)):
    try:
        return polygons.simulate(
            payload.get("params") or {},
            payload.get("scenarios") or [],
            payload.get("hypothesis") or {},
        )
    except PolygonError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/polygons/{polygon_id}")
def get_polygon(polygon_id: str, user: dict | None = Depends(get_optional_user)):
    card = polygons.get_store().get(polygon_id, user["id"] if user else None)
    if card is None:
        raise HTTPException(status_code=404, detail="Полигон не найден")
    return card


@router.post("/polygons")
def save_polygon(payload: dict = Body(...), user: dict = Depends(get_current_user)):
    store = polygons.get_store()
    if payload.get("id") and store.get(payload["id"]) and store.get(payload["id"]).get("example"):
        payload = {**payload, "id": None}  # правка примера сохраняется как свой полигон
    try:
        return store.save(user["id"], payload, author=author_name(user["id"]))
    except PolygonError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.delete("/polygons/{polygon_id}")
def delete_polygon(polygon_id: str, user: dict = Depends(get_current_user)):
    if not polygons.get_store().delete(user["id"], polygon_id):
        raise HTTPException(status_code=404, detail="Полигон не найден")
    return {"ok": True}


async def _ask(provider, model, role: dict, idea: str, context: str) -> dict:
    parts: list = []
    try:
        async for kind, chunk in provider.stream_chat(polygons.panel_messages(role, idea, context), [], model):
            if kind == "text":
                parts.append(chunk)
    except Exception as exc:  # noqa: BLE001 — одна роль не должна ронять всю группу
        return {"role": role["name"], "role_id": role["id"], "score": None, "verdict": "",
                "pros": [], "cons": [], "condition": "", "question": "",
                "error": f"Роль не ответила: {type(exc).__name__}"}
    return polygons.parse_opinion("".join(parts), role)


@router.post("/polygons/panel")
async def run_panel(payload: dict = Body(...), user: dict = Depends(get_current_user)):
    from .chat import get_provider  # лениво: chat тянет рантайм целиком

    idea = str(payload.get("idea") or "").strip()
    if len(idea) < 10:
        raise HTTPException(status_code=422, detail="Опишите идею или гипотезу хотя бы одной фразой")
    cfg = effective_config_for(user["id"])
    provider = get_provider(cfg.base_url, cfg.api_key, cfg.model)
    if provider is None:
        raise HTTPException(
            status_code=503,
            detail="ИИ-фокус-группе нужен подключённый LLM: «Админ-панель → Настройки внешних API» "
                   "или «Настройки → Внешний API». Сценарная модель работает без него.",
        )
    roles = polygons.normalize_panel(payload.get("panel"))
    context = ""
    if isinstance(payload.get("simulation"), dict):
        context = polygons.results_context(payload["simulation"])
    try:
        opinions = await asyncio.wait_for(
            asyncio.gather(*[_ask(provider, cfg.model, r, idea, context) for r in roles]),
            timeout=PANEL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Фокус-группа не уложилась во время — попробуйте ещё раз")
    return {"idea": idea, "opinions": list(opinions), "summary": polygons.summarize_panel(list(opinions))}
