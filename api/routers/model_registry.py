"""HTTP-слой моделей коллективов (api/routers/model_registry.py).

Раздел «Модели» закрыт ролью «Администратор»: зависимость require_admin
стоит на уровне роутера, поэтому все эндпоинты (включая чтение каталога
и паспорта) отвечают 401 без входа и 403 не-администратору.

GET    /api/models/registry                    — каталог: опубликованные модели
                                                 всех + свои и проектные черновики;
POST   /api/models/registry/create             — новая модель по таблице (multipart:
                                                 file, kind, name, spec JSON…);
POST   /api/models/registry/import             — импорт модели ONNX (multipart:
                                                 onnx, holdout, spec JSON…);
GET    /api/models/registry/{id}               — паспорт выбранной версии (?version=);
PATCH  /api/models/registry/{id}               — название, описание, проект;
DELETE /api/models/registry/{id}               — удаление в корзину, только администратор;
POST   /api/models/registry/{id}/predict       — прогноз (кинетика: температурная программа);
POST   /api/models/registry/{id}/profile       — график «выход от одного входа»;
POST   /api/models/registry/{id}/optimize      — подбор режима под цель;
GET    /api/models/registry/{id}/data          — обучающая таблица версии (участникам);
POST   /api/models/registry/{id}/retrain       — новая версия: дозагрузка опытов,
                                                 исключение строк, смена метода/порога;
POST   /api/models/registry/{id}/current       — сделать версию текущей (откат);
POST   /api/models/registry/{id}/publish|unpublish|verify|archive — статусы;
GET    /api/models/registry/{id}/facts         — «прогноз → факт»;
POST   /api/models/registry/{id}/facts         — внести замер после опыта;
POST   /api/models/registry/{id}/fork          — копия модели себе.

Старый GET /api/models (витрина открытых моделей Hugging Face) не меняется.
Внутри раздела права — api/runtime/model_engine.level_for: владелец и
администратор — всё; участники проекта — по уровню доступа к проекту;
опубликованной моделью может пользоваться любой вошедший. Чужая
неопубликованная модель неотличима от несуществующей (404).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile

from ..runtime import model_engine as engine
from ..runtime import model_registry as registry
from ..runtime.model_engine import ModelEngineError
from ..runtime.model_registry import ModelRegistryError
from ..runtime.users import ROLE_ADMIN
from .auth import get_current_user, get_optional_user, require_admin

# require_admin на уровне роутера: раздел «Модели» — только администраторы
# (см. docstring). Точка — один замок на все эндпоинты сразу.
router = APIRouter(prefix="/api/models/registry", dependencies=[Depends(require_admin)])


def _card(model_id: str, user, need: str) -> dict:
    try:
        return engine.resolve(model_id, user, need)
    except ModelEngineError:
        raise HTTPException(status_code=404, detail="Модель не найдена")


def _fail(exc: Exception):
    raise HTTPException(status_code=422, detail=str(exc))


def _json_field(raw: str | None, label: str) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{label}: ожидается JSON")
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail=f"{label}: ожидается объект")
    return data


async def _read_upload(upload: UploadFile | None, limit: int) -> tuple[str, bytes] | None:
    if upload is None or not upload.filename:
        return None
    blob = await upload.read(limit + 1)
    if len(blob) > limit:
        raise HTTPException(status_code=413, detail="Файл слишком большой")
    return upload.filename, blob


def _train_response(result: dict, user) -> dict:
    card = result["card"]
    version = result["version"]
    return {
        "ok": True,
        "model": engine.summary(card, user),
        "version": registry.version_summary(version),
        "made_current": result["made_current"],
        "message": result["message"],
        "warnings": version.get("warnings") or [],
    }


# ---------------------------------------------------------------------------
# Каталог и создание


@router.get("")
def list_models(user: dict | None = Depends(get_optional_user)):
    items = [engine.summary(card, user) for card in engine.visible_cards(user)]
    return {"items": items}


@router.post("/create")
async def create_model(
    kind: str = Form(...),
    name: str = Form(...),
    spec: str = Form(...),
    description: str = Form(""),
    domain: str = Form(""),
    project_id: str = Form(""),
    note: str = Form(""),
    file: UploadFile | None = File(default=None),
    user: dict = Depends(get_current_user),
):
    upload = await _read_upload(file, engine.MAX_UPLOAD)
    if upload is None:
        raise HTTPException(status_code=422, detail="Приложите таблицу опытов (Excel или CSV)")
    try:
        result = await engine.train_version(
            user, card=None,
            fields={"kind": kind, "name": name, "description": description, "domain": domain,
                    "project_id": project_id},
            spec_raw=_json_field(spec, "spec"), blobs=[upload], note=note or "первая версия")
    except (ModelEngineError, ModelRegistryError) as exc:
        _fail(exc)
    return _train_response(result, user)


@router.post("/import")
async def import_model(
    name: str = Form(...),
    spec: str = Form(...),
    description: str = Form(""),
    domain: str = Form(""),
    project_id: str = Form(""),
    note: str = Form(""),
    onnx: UploadFile = File(...),
    holdout: UploadFile | None = File(default=None),
    user: dict = Depends(get_current_user),
):
    model_file = await _read_upload(onnx, engine.MAX_ONNX)
    if model_file is None or not model_file[0].lower().endswith(".onnx"):
        raise HTTPException(status_code=422, detail="Нужен файл модели в формате .onnx")
    check = await _read_upload(holdout, engine.MAX_UPLOAD)
    try:
        result = await engine.train_version(
            user, card=None,
            fields={"kind": "onnx", "name": name, "description": description, "domain": domain,
                    "project_id": project_id},
            spec_raw=_json_field(spec, "spec"), blobs=[check] if check else [],
            onnx=model_file[1], note=note or "импорт модели")
    except (ModelEngineError, ModelRegistryError) as exc:
        _fail(exc)
    return _train_response(result, user)


# ---------------------------------------------------------------------------
# Одна модель


@router.get("/{model_id}")
def get_model(model_id: str, version: int | None = None, user: dict | None = Depends(get_optional_user)):
    card = _card(model_id, user, engine.LEVEL_USE)
    return engine.full_view(card, user, version)


@router.patch("/{model_id}")
def update_model(model_id: str, patch: dict = Body(...), user: dict = Depends(get_current_user)):
    card = _card(model_id, user, engine.LEVEL_WRITE)
    allowed_keys = {k: patch[k] for k in ("name", "description", "domain", "project_id") if k in patch}
    try:
        card = registry.get_store().update(card["user_id"], card["id"], allowed_keys)
    except ModelRegistryError as exc:
        _fail(exc)
    return engine.summary(card, user)


@router.delete("/{model_id}")
def delete_model(model_id: str, _admin: dict = Depends(require_admin)):
    card = registry.get_store().find(model_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Модель не найдена")
    registry.get_store().delete(card["user_id"], model_id)
    return {"ok": True}


@router.post("/{model_id}/predict")
async def predict(model_id: str, body: dict = Body(...), user: dict = Depends(get_current_user)):
    card = _card(model_id, user, engine.LEVEL_USE)
    try:
        if card.get("kind") == "kinetics":
            return await engine.kinetics_run(card, program=body.get("program") or [], T0=body.get("T0"),
                                             y0=body.get("y0"), targets=body.get("targets"),
                                             version=body.get("version"), source="page")
        return await engine.predict(card, body.get("inputs") or {}, version=body.get("version"), source="page")
    except ModelEngineError as exc:
        _fail(exc)


@router.post("/{model_id}/profile")
async def profile(model_id: str, body: dict = Body(...), user: dict = Depends(get_current_user)):
    card = _card(model_id, user, engine.LEVEL_USE)
    try:
        return await engine.profile(card, body.get("base") or {}, str(body.get("vary") or ""),
                                    points=body.get("points") or 41, version=body.get("version"))
    except ModelEngineError as exc:
        _fail(exc)


@router.post("/{model_id}/optimize")
async def optimize(model_id: str, body: dict = Body(...), user: dict = Depends(get_current_user)):
    card = _card(model_id, user, engine.LEVEL_USE)
    try:
        return await engine.optimize(card, targets=body.get("targets"), fixed=body.get("fixed"),
                                     bounds=body.get("bounds"), count=body.get("count") or 3,
                                     version=body.get("version"), kinetics=body.get("kinetics"),
                                     source="page")
    except ModelEngineError as exc:
        _fail(exc)


@router.get("/{model_id}/data")
def data(model_id: str, version: int | None = None, user: dict = Depends(get_current_user)):
    card = _card(model_id, user, engine.LEVEL_READ)
    try:
        return engine.data_view(card, version)
    except ModelEngineError as exc:
        _fail(exc)


@router.post("/{model_id}/retrain")
async def retrain(
    model_id: str,
    mode: str = Form("append"),
    spec_patch: str = Form(""),
    note: str = Form(""),
    file: UploadFile | None = File(default=None),
    user: dict = Depends(get_current_user),
):
    """Новая версия модели. mode: append — опыты из файла добавляются к данным
    текущей версии; replace — файл заменяет данные; same — те же данные
    (например, исключена строка, сменён метод или порог)."""
    card = _card(model_id, user, engine.LEVEL_WRITE)
    store = registry.get_store()
    current = store.current(card)
    if current is None:
        raise HTTPException(status_code=422, detail="У модели нет текущей версии")
    upload = await _read_upload(file, engine.MAX_UPLOAD)
    blobs = []
    prev = store.version_file(card["user_id"], card["id"], current["version"], "train.csv")
    if mode in ("append", "same") and prev.is_file():
        blobs.append(("train.csv", prev.read_bytes()))
    if mode in ("append", "replace"):
        if upload is None:
            raise HTTPException(status_code=422, detail="Приложите файл с опытами")
        blobs.append(upload)
    spec = dict(current["spec"])
    patch = _json_field(spec_patch, "spec_patch")
    for key in ("method", "threshold", "excluded", "controls", "formula", "kinetics", "inputs", "outputs",
                "protein_embedding"):
        if key in patch:
            spec[key] = patch[key]
    if mode == "replace":
        spec.pop("excluded", None)
    onnx = None
    if card.get("kind") == "onnx":
        onnx_path = store.version_file(card["user_id"], card["id"], current["version"], "model.onnx")
        onnx = onnx_path.read_bytes() if onnx_path.is_file() else None
    try:
        result = await engine.train_version(user, card=card, fields={}, spec_raw=spec, blobs=blobs,
                                            onnx=onnx, note=note)
    except (ModelEngineError, ModelRegistryError) as exc:
        _fail(exc)
    return _train_response(result, user)


@router.post("/{model_id}/current")
def set_current(model_id: str, body: dict = Body(...), user: dict = Depends(get_current_user)):
    card = _card(model_id, user, engine.LEVEL_WRITE)
    try:
        card = registry.get_store().set_current(card["user_id"], card["id"], int(body.get("version") or 0))
    except (ModelRegistryError, ValueError) as exc:
        _fail(exc)
    return engine.summary(card, user)


def _status(model_id: str, user: dict, status: str, evidence: str = "") -> dict:
    card = _card(model_id, user, engine.LEVEL_ADMIN)
    try:
        card = engine.set_status(card, user, status, evidence)
    except ModelRegistryError as exc:
        _fail(exc)
    return engine.summary(card, user)


@router.post("/{model_id}/publish")
def publish(model_id: str, user: dict = Depends(get_current_user)):
    return _status(model_id, user, registry.STATUS_PUBLISHED)


@router.post("/{model_id}/unpublish")
def unpublish(model_id: str, user: dict = Depends(get_current_user)):
    return _status(model_id, user, registry.STATUS_DRAFT)


@router.post("/{model_id}/archive")
def archive(model_id: str, user: dict = Depends(get_current_user)):
    return _status(model_id, user, registry.STATUS_ARCHIVED)


@router.post("/{model_id}/verify")
def verify(model_id: str, body: dict = Body(...), user: dict = Depends(get_current_user)):
    return _status(model_id, user, registry.STATUS_VERIFIED, str(body.get("evidence") or ""))


@router.get("/{model_id}/facts")
def facts(model_id: str, user: dict | None = Depends(get_optional_user)):
    card = _card(model_id, user, engine.LEVEL_USE)
    items = engine.observations_view(card) if engine.allowed(card, user, engine.LEVEL_READ) else []
    return {"items": items, "stats": engine.facts_stats(card)}


@router.post("/{model_id}/facts")
async def add_fact(model_id: str, body: dict = Body(...), user: dict = Depends(get_current_user)):
    card = _card(model_id, user, engine.LEVEL_WRITE)
    try:
        obs = await engine.add_fact(card, user, body.get("inputs") or {}, body.get("measured") or {},
                                    str(body.get("note") or ""))
    except (ModelEngineError, ModelRegistryError) as exc:
        _fail(exc)
    return {"ok": True, "fact": obs, "stats": engine.facts_stats(card)}


@router.post("/{model_id}/fork")
def fork(model_id: str, user: dict = Depends(get_current_user)):
    card = _card(model_id, user, engine.LEVEL_USE)
    if card.get("user_id") == user["id"]:
        raise HTTPException(status_code=422, detail="Это ваша модель — копия не нужна")
    try:
        new = registry.get_store().fork(card, user["id"], engine.author_name(user["id"]))
    except ModelRegistryError as exc:
        _fail(exc)
    return engine.summary(new, user)


def is_admin(user: dict | None) -> bool:
    return bool(user) and user.get("role") == ROLE_ADMIN
