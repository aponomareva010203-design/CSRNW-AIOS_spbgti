"""Оркестрация моделей коллективов (api/runtime/model_engine.py).

Связывает реестр (model_registry), ядро (model_kernel — исполняется в
песочнице) и потребителей: HTTP-роутер страницы модели и инструменты чата.

Здесь:
  - проверка спецификации модели до запуска песочницы (быстрые понятные ошибки);
  - запуск ядра в песочнице: model_kernel.py и job.json кладутся в рабочий
    каталог, сгенерированный main.py вызывает run(job);
  - обучение новой версии: паспорт, снимок данных, решение о текущей версии,
    синхронизация статуса «Модели» в таблице пилота;
  - прогноз, график по одному входу, подбор режима — всегда по конкретной
    версии; ответ помечен номером версии;
  - права: владелец, участники проекта по уровню, администратор; пользоваться
    опубликованной моделью может любой.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import time
from pathlib import Path

from . import compute_sandbox
from . import model_registry as registry
from .config import get_settings

KERNEL_PATH = Path(__file__).with_name("model_kernel.py")

# main.py в песочнице: интерпретатор запускается с -I (без каталога скрипта
# в sys.path), поэтому ядро загружается по пути, а не import-ом.
MAIN_CODE = """import importlib.util, json, os
_spec = importlib.util.spec_from_file_location("model_kernel", os.path.join(os.getcwd(), "model_kernel.py"))
_mk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mk)
with open("job.json", encoding="utf-8") as _fh:
    result = _mk.run(json.load(_fh))
"""

INPUT_TYPES = ("number", "category", "smiles", "sequence")
METHODS = ("auto", "linear", "quadratic", "gp", "gbm")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,23}$")
EXPR_RE = re.compile(r"^[A-Za-z0-9_+\-*/^().,=\s]+$")
MAX_INPUTS = 30
MAX_OUTPUTS = 5
MAX_UPLOAD = 20 * 1024 * 1024
MAX_ONNX = 60 * 1024 * 1024
NAME_LIMIT = 60

LEVEL_VIEW, LEVEL_USE, LEVEL_READ, LEVEL_WRITE, LEVEL_ADMIN = "view", "use", "read", "write", "admin"


class ModelEngineError(ValueError):
    """Понятная человеку причина, почему операция с моделью не выполнена."""


# ---------------------------------------------------------------------------
# Спецификация


def _s(value, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()[:limit]


def _num(value):
    if value is None or value == "":
        return None
    try:
        v = float(str(value).replace(",", ".").replace(" ", ""))
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _field(raw: dict, i: int, prefix: str, kind: str, allowed_types: tuple) -> dict:
    if not isinstance(raw, dict):
        raw = {"column": str(raw)}
    title = _s(raw.get("title") or raw.get("column") or raw.get("name"), 120)
    column = _s(raw.get("column") or raw.get("title") or raw.get("name"), 200)
    symbol = _s(raw.get("symbol"), 24)
    if symbol and not IDENT_RE.match(symbol):
        raise ModelEngineError(f"Обозначение «{symbol}» — латиница и цифры, с буквы (например T, Re, c_pla)")
    name = symbol or _s(raw.get("name"), NAME_LIMIT)
    if not name or not re.match(r"^[\w.-]{1,60}$", name):
        name = f"{prefix}{i + 1}"
    ftype = _s(raw.get("type"), 20) or "number"
    if ftype not in allowed_types:
        raise ModelEngineError(f"Тип «{ftype}» для «{title or name}» не подходит: " + ", ".join(allowed_types))
    if kind in ("formula", "kinetics") and prefix == "x" and not symbol:
        raise ModelEngineError(f"У входа «{title or name}» нужно обозначение для формулы (symbol), например T")
    field = {"name": name, "column": column or name, "title": title or column or name,
             "unit": _s(raw.get("unit"), 40), "type": ftype}
    if symbol:
        field["symbol"] = symbol
    lo, hi = _num(raw.get("min")), _num(raw.get("max"))
    if lo is not None and hi is not None:
        field["min"], field["max"] = min(lo, hi), max(lo, hi)
    return field


def _params_block(raw) -> dict:
    out = {}
    if not isinstance(raw, dict):
        return out
    for name, info in list(raw.items())[:20]:
        if not IDENT_RE.match(str(name)):
            raise ModelEngineError(f"Коэффициент «{name}» — латиница и цифры, с буквы")
        info = info if isinstance(info, dict) else {"init": info}
        item = {}
        for key in ("init", "min", "max"):
            v = _num(info.get(key))
            if v is not None:
                item[key] = v
        if "log" in info:
            item["log"] = bool(info.get("log"))
        out[str(name)] = item
    return out


def _constants_block(raw) -> dict:
    out = {}
    if isinstance(raw, dict):
        for name, value in list(raw.items())[:20]:
            v = _num(value)
            if not IDENT_RE.match(str(name)) or v is None:
                raise ModelEngineError(f"Константа «{name}» задана неверно")
            out[str(name)] = v
    return out


def _expression(raw, label: str) -> str:
    text = _s(raw, 400)
    if not text:
        raise ModelEngineError(f"Нужна {label}")
    if not EXPR_RE.match(text) or "__" in text:
        raise ModelEngineError(f"В {label} допустимы буквы, цифры, + - * / ^ ( ) = и точка")
    return text


def normalize_spec(raw: dict, kind: str) -> dict:
    """Проверенная спецификация модели. Всё, что уйдёт в песочницу, — здесь."""
    raw = raw if isinstance(raw, dict) else {}
    if kind not in registry.KINDS:
        raise ModelEngineError("Вид модели: " + ", ".join(registry.KINDS))
    spec: dict = {"kind": kind}
    in_types = INPUT_TYPES if kind == "table" else ("number",)
    raw_inputs = raw.get("inputs") or []
    raw_outputs = raw.get("outputs") or []
    if not isinstance(raw_inputs, list) or not isinstance(raw_outputs, list):
        raise ModelEngineError("inputs и outputs — списки")
    if kind != "kinetics":
        if not 1 <= len(raw_inputs) <= MAX_INPUTS:
            raise ModelEngineError(f"Входов модели — от 1 до {MAX_INPUTS}")
        spec["inputs"] = [_field(r, i, "x", kind, in_types) for i, r in enumerate(raw_inputs)]
    else:
        spec["inputs"] = []
    max_out = 1 if kind in ("formula", "kinetics") else (10 if kind == "onnx" else MAX_OUTPUTS)
    if not 1 <= len(raw_outputs) <= max_out:
        raise ModelEngineError(f"Выходов у этой модели — от 1 до {max_out}")
    spec["outputs"] = [_field(r, i, "y", "table", ("number",)) for i, r in enumerate(raw_outputs)]
    names = [f["name"] for f in spec["inputs"] + spec["outputs"]]
    if len(set(names)) != len(names):
        raise ModelEngineError("Имена входов и выходов должны различаться")

    if kind == "table":
        method = _s(raw.get("method"), 20) or "auto"
        if method not in METHODS:
            raise ModelEngineError("Метод: " + ", ".join(METHODS))
        spec["method"] = method
        if raw.get("protein_embedding") in ("esm2", True):
            spec["protein_embedding"] = "esm2"
    elif kind == "formula":
        fm = raw.get("formula") if isinstance(raw.get("formula"), dict) else {}
        spec["formula"] = {
            "expression": _expression(fm.get("expression"), "формула"),
            "fit_scale": "log" if fm.get("fit_scale") == "log" else "linear",
            "params": _params_block(fm.get("params")),
            "constants": _constants_block(fm.get("constants")),
        }
    elif kind == "kinetics":
        kn = raw.get("kinetics") if isinstance(raw.get("kinetics"), dict) else {}
        state = _s(kn.get("state"), 24) or "a"
        if not IDENT_RE.match(state) or state == "T":
            raise ModelEngineError("Обозначение степени превращения — латиница, не T (например a)")
        cols = kn.get("columns") if isinstance(kn.get("columns"), dict) else {}
        spec["kinetics"] = {
            "rate": _expression(kn.get("rate"), "формула скорости"),
            "state": state,
            "temperature_unit": "K" if str(kn.get("temperature_unit") or "").strip().upper() in ("K", "К") else "°C",
            "time_unit": _s(kn.get("time_unit"), 20) or "мин",
            "bounded": kn.get("bounded", True) is not False,
            "params": _params_block(kn.get("params")),
            "constants": _constants_block(kn.get("constants")),
            "columns": {k: _s(cols.get(k), 200) or k for k in ("run", "t", "T", "y")},
        }
    th = raw.get("threshold")
    if isinstance(th, dict) and _num(th.get("max")) is not None:
        out_name = _s(th.get("output"), NAME_LIMIT) or spec["outputs"][0]["name"]
        out_name = _resolve_name(spec["outputs"], out_name) or spec["outputs"][0]["name"]
        spec["threshold"] = {"output": out_name, "metric": "mae", "max": abs(_num(th.get("max")))}
    controls = []
    for c in (raw.get("controls") or [])[:20]:
        if isinstance(c, dict) and isinstance(c.get("inputs"), dict) and isinstance(c.get("expected"), dict):
            controls.append({
                "inputs": map_inputs(spec, c["inputs"]),
                "expected": {(_resolve_name(spec["outputs"], k) or k): _num(v) for k, v in c["expected"].items()},
                "tolerance": {(_resolve_name(spec["outputs"], k) or k): _num(v)
                              for k, v in (c.get("tolerance") or {}).items()},
            })
    if controls:
        spec["controls"] = controls
    excluded = []
    for e in (raw.get("excluded") or [])[:500]:
        if isinstance(e, dict) and str(e.get("row", "")).isdigit():
            excluded.append({"row": int(e["row"]), "reason": _s(e.get("reason"), 200) or "исключено автором"})
    if excluded:
        spec["excluded"] = excluded
    return spec


def _resolve_name(fields: list, key: str) -> str | None:
    """Имя поля по имени, обозначению, названию столбца или подписи."""
    k = str(key or "").strip().lower()
    if not k:
        return None
    for f in fields:
        if k in {str(f.get(a) or "").strip().lower() for a in ("name", "symbol", "column", "title")}:
            return f["name"]
    return None


def map_inputs(spec: dict, raw: dict) -> dict:
    """Входы от человека или агента → ключи модели. Неизвестные ключи
    отбрасываются, ключи можно давать названием столбца или подписью."""
    out = {}
    for key, value in (raw or {}).items():
        name = _resolve_name(spec.get("inputs") or [], key)
        if name:
            out[name] = value
    return out


# ---------------------------------------------------------------------------
# Песочница


def esm_path() -> str | None:
    base = os.environ.get("AIOS_EMBEDDERS_DIR", "/opt/aios-sandbox/embedders")
    path = Path(base) / "esm2_t6_8M.onnx"
    return str(path) if path.is_file() else None


def _friendly(error: str | None) -> str:
    text = str(error or "").strip()
    if not text:
        return "Расчёт модели не выполнен"
    if "Traceback" in text:
        last = [ln for ln in text.splitlines() if ln.strip()][-1]
        return "Ошибка расчёта модели: " + last[-400:]
    return text[-600:]


async def run_job(job: dict, files: dict, profile: str = "model") -> dict:
    if not compute_sandbox.sandbox_available():
        raise ModelEngineError("Расчётная песочница не установлена на этом сервере — модели не считаются. "
                               "Сообщите администратору платформы.")
    payload = {"model_kernel.py": KERNEL_PATH.read_bytes(),
               "job.json": json.dumps(job, ensure_ascii=False).encode("utf-8")}
    payload.update(files or {})
    res = await compute_sandbox.run_code(MAIN_CODE, files=payload, profile=profile)
    if not res.get("ok"):
        raise ModelEngineError(_friendly(res.get("error")))
    out = res.get("result")
    if not isinstance(out, dict):
        raise ModelEngineError("Модель вернула неожиданный ответ")
    if not out.get("ok"):
        raise ModelEngineError(out.get("error") or "Операция модели не выполнена")
    return out


def rows_to_csv(columns: list, rows: list) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([str(c) for c in columns])
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
    return buf.getvalue().encode("utf-8")


def inline_data(data: dict) -> bytes:
    """Таблица, переданная прямо в запросе: {columns: [...], rows: [[...]]}."""
    if not isinstance(data, dict):
        raise ModelEngineError("data — объект {columns, rows}")
    columns = data.get("columns")
    rows = data.get("rows")
    if not isinstance(columns, list) or not columns or not isinstance(rows, list) or not rows:
        raise ModelEngineError("В data нужны непустые columns и rows")
    if len(rows) > 5000 or len(columns) > 80:
        raise ModelEngineError("Слишком большая таблица: до 5000 строк и 80 столбцов")
    clean = [[v if isinstance(v, (int, float)) or v is None else str(v)[:200] for v in (r if isinstance(r, list) else [])]
             for r in rows]
    return rows_to_csv([str(c)[:200] for c in columns], clean)


# ---------------------------------------------------------------------------
# Права


def _user_dict(user_or_id) -> dict | None:
    if isinstance(user_or_id, dict):
        return user_or_id if user_or_id.get("id") else None
    if not user_or_id:
        return None
    from .users import UserStore

    user = UserStore(get_settings().runtime_dir / "users").get(str(user_or_id))
    return dict(user, id=str(user_or_id)) if user else None


def level_for(card: dict, user) -> str | None:
    """Наибольший уровень пользователя: admin / write / read / use / view / None."""
    from . import projects
    from .users import ROLE_ADMIN

    user = _user_dict(user)
    public = card.get("status") in registry.PUBLIC_STATUSES
    if user:
        if user.get("role") == ROLE_ADMIN or user["id"] == card.get("user_id"):
            return LEVEL_ADMIN
        level = projects.artifact_level(card.get("project_id") or "", user)
        if projects.level_allows(level, projects.LEVEL_ADMIN):
            return LEVEL_ADMIN
        if projects.level_allows(level, projects.LEVEL_WRITE):
            return LEVEL_WRITE
        if projects.level_allows(level, projects.LEVEL_READ):
            return LEVEL_READ
    if public:
        return LEVEL_USE
    return None


_RANK = {LEVEL_VIEW: 0, LEVEL_USE: 1, LEVEL_READ: 2, LEVEL_WRITE: 3, LEVEL_ADMIN: 4}


def allowed(card: dict, user, need: str) -> bool:
    level = level_for(card, user)
    return level is not None and _RANK[level] >= _RANK[need]


def resolve(model_id: str, user, need: str) -> dict:
    card = registry.get_store().find(model_id)
    if card is None or not allowed(card, user, need):
        raise ModelEngineError("Модель не найдена")
    return card


def author_name(user_id: str) -> str:
    try:
        from . import cabinet

        name = cabinet.full_name(cabinet.get_store().get_profile(user_id))
        if name:
            return name
    except Exception:  # noqa: BLE001 — подпись автора не должна ронять обучение
        pass
    user = _user_dict(user_id)
    return (user or {}).get("name") or "Пользователь"


def visible_cards(user) -> list:
    """Модели, которые пользователь видит в каталоге: опубликованные всех,
    свои и проектные черновики."""
    user = _user_dict(user)
    out = []
    for card in registry.get_store().list_all():
        if card.get("status") == registry.STATUS_ARCHIVED and not (user and allowed(card, user, LEVEL_READ)):
            continue
        if allowed(card, user, LEVEL_USE):
            out.append(card)
    return out


# ---------------------------------------------------------------------------
# Обучение


def _choose_current(card: dict, new_summary: dict) -> tuple[bool, str]:
    versions = card.get("versions") or []
    current = next((v for v in versions if v.get("version") == card.get("current_version")), None)
    if current is None or card.get("status") == registry.STATUS_DRAFT:
        return True, ""
    if card.get("status") == registry.STATUS_VERIFIED:
        return False, ("Модель подтверждена руководителем, поэтому новая версия сохранена кандидатом. "
                       "Сделайте её текущей вручную — подтверждение потребуется заново.")
    if new_summary.get("passed") is False:
        return False, "Новая версия не прошла порог приёмки и сохранена кандидатом."
    old, new = current.get("skill"), new_summary.get("skill")
    if old is not None and new is not None and new < old - 0.02:
        return False, (f"Новая версия точнее угадывания средним на {round(new * 100)} %, текущая — "
                       f"на {round(old * 100)} %. Новая сохранена кандидатом.")
    return True, ""


async def train_version(user, *, card: dict | None, fields: dict, spec_raw: dict,
                        blobs: list, onnx: bytes | None = None, note: str = "") -> dict:
    """Обучает новую версию (и создаёт модель, если card=None).

    blobs — [(имя файла, содержимое)] обучающих таблиц по порядку (снимок
    прежней версии + дозагрузка — одна таблица по названиям столбцов).
    """
    user = _user_dict(user)
    if not user:
        raise ModelEngineError("Нужен вход в AIOS: модели сохраняются в вашем кабинете")
    kind = card["kind"] if card else fields.get("kind")
    spec = normalize_spec(spec_raw, kind)
    if kind == "table" and spec.get("protein_embedding") == "esm2" and not esm_path():
        raise ModelEngineError("На сервере нет модели ESM-2 для последовательностей: уберите protein_embedding")
    files, names = {}, []
    for i, (name, blob) in enumerate(blobs or []):
        if len(blob) > MAX_UPLOAD:
            raise ModelEngineError("Файл данных больше 20 МБ")
        low = str(name).lower()
        ext = next((e for e in (".xlsx", ".xlsm", ".csv", ".tsv", ".txt") if low.endswith(e)), None)
        if ext is None:
            raise ModelEngineError("Данные принимаются в Excel (.xlsx) или CSV")
        safe = "train.csv" if low == "train.csv" else f"data{i}{ext}"
        files[safe] = blob
        names.append(safe)
    if kind != "onnx" and not names:
        raise ModelEngineError("Нужна таблица опытов: приложите Excel или CSV")
    job = {"op": "train", "spec": spec, "data_files": names}
    if kind == "onnx":
        if not onnx:
            raise ModelEngineError("Приложите файл модели .onnx")
        if len(onnx) > MAX_ONNX:
            raise ModelEngineError("Файл модели больше 60 МБ")
        files["model.onnx"] = onnx
        job["onnx_file"] = "model.onnx"
    if spec.get("protein_embedding") == "esm2":
        job["esm_path"] = esm_path()

    out = await run_job(job, files, profile="train")

    table = out.get("table") or {}
    stored = {}
    if table.get("columns"):
        stored["train.csv"] = rows_to_csv(table["columns"], table.get("rows") or [])
    if kind == "onnx":
        stored["model.onnx"] = onnx
    sha = hashlib.sha256(stored.get("train.csv", b"")).hexdigest() if "train.csv" in stored else None
    payload = {
        "created_at": time.time(),
        "created_by": user["id"],
        "created_by_name": author_name(user["id"]),
        "note": _s(note, 300),
        "kind": kind,
        "spec": spec,
        "fit": out.get("fit") or {},
        "validation": out.get("validation") or {},
        "domain": out.get("domain") or {},
        "threshold_check": out.get("threshold_check"),
        "controls": out.get("controls") or [],
        "warnings": out.get("warnings") or [],
        "env": out.get("env") or {},
        "n_rows": out.get("n_rows"),
        "excluded_rows": out.get("excluded_rows") or [],
        "data": {"file": "train.csv" if sha else None, "sha256": sha,
                 "rows": len(table.get("rows") or []), "columns": table.get("columns") or []},
    }
    if out.get("curves"):
        payload["curves"] = out["curves"]

    store = registry.get_store()
    if card is None:
        card = store.create(user["id"], {
            "kind": kind, "name": fields.get("name"), "description": fields.get("description"),
            "domain": fields.get("domain"), "project_id": fields.get("project_id"),
            "author": author_name(user["id"]),
        })
    make_current, message = _choose_current(card, registry.version_summary(payload))
    card, payload = store.add_version(card["user_id"], card["id"], payload, stored, make_current=make_current)
    _sync_pilot(card)
    return {"card": card, "version": payload, "made_current": make_current, "message": message}


def _sync_pilot(card: dict) -> None:
    if not card.get("project_id"):
        return
    try:
        from .projects import bump_artifact_status

        status = 3 if card.get("status") in registry.PUBLIC_STATUSES else 2
        bump_artifact_status(card["project_id"], "models", status)
    except Exception:  # noqa: BLE001 — таблица пилота не должна ронять работу с моделью
        pass


def set_status(card: dict, user, status: str, evidence: str = "") -> dict:
    user = _user_dict(user)
    card = registry.get_store().set_status(card["user_id"], card["id"], status,
                                           by=(user or {}).get("id", ""),
                                           by_name=author_name(user["id"]) if user else "",
                                           evidence=evidence)
    _sync_pilot(card)
    return card


# ---------------------------------------------------------------------------
# Расчёт по версии


def _version_or_error(card: dict, version: int | None) -> dict:
    store = registry.get_store()
    number = int(version or card.get("current_version") or 0)
    payload = store.version(card["user_id"], card["id"], number) if number else None
    if payload is None:
        raise ModelEngineError("У модели нет такой версии" if version else "Модель ещё не обучена")
    return payload


def _version_files(card: dict, payload: dict) -> dict:
    store = registry.get_store()
    files = {}
    for name in ("train.csv", "model.onnx"):
        path = store.version_file(card["user_id"], card["id"], payload["version"], name)
        if path.is_file():
            files[name] = path.read_bytes()
    return files


def _base_job(payload: dict) -> dict:
    job = {"spec": payload["spec"], "fit": payload.get("fit") or {}, "domain": payload.get("domain") or {},
           "train_file": "train.csv", "excluded_rows": payload.get("excluded_rows") or [],
           "onnx_file": "model.onnx"}
    if payload["spec"].get("protein_embedding") == "esm2":
        job["esm_path"] = esm_path()
    return job


def _stamp(card: dict, payload: dict, body: dict) -> dict:
    body["model"] = {"id": card["id"], "name": card.get("name"), "version": payload["version"],
                     "status": card.get("status"), "kind": card.get("kind"),
                     "url": f"model.html?id={card['id']}"}
    body["outputs_meta"] = [{"name": o["name"], "title": o.get("title"), "unit": o.get("unit")}
                            for o in payload["spec"].get("outputs") or []]
    body["inputs_meta"] = [{"name": i["name"], "title": i.get("title"), "unit": i.get("unit"),
                            "type": i.get("type")} for i in payload["spec"].get("inputs") or []]
    return body


async def predict(card: dict, inputs, *, version: int | None = None, source: str = "page") -> dict:
    payload = _version_or_error(card, version)
    items = inputs if isinstance(inputs, list) else [inputs]
    mapped = [map_inputs(payload["spec"], i if isinstance(i, dict) else {}) for i in items][:5000]
    job = dict(_base_job(payload), op="predict", inputs=mapped)
    out = await run_job(job, _version_files(card, payload))
    registry.get_store().bump_usage(card["user_id"], card["id"], source)
    return _stamp(card, payload, out)


async def kinetics_run(card: dict, *, program: list, T0=None, y0=None, targets=None,
                       version: int | None = None, source: str = "page") -> dict:
    payload = _version_or_error(card, version)
    if card.get("kind") != "kinetics":
        raise ModelEngineError("Температурная программа — только для кинетических моделей")
    job = dict(_base_job(payload), op="predict", program=program or [], T0=T0, y0=y0,
               targets=targets or [0.5, 0.9, 0.95])
    out = await run_job(job, _version_files(card, payload))
    registry.get_store().bump_usage(card["user_id"], card["id"], source)
    return _stamp(card, payload, out)


async def profile(card: dict, base: dict, vary: str, *, points: int = 41, version: int | None = None) -> dict:
    payload = _version_or_error(card, version)
    name = _resolve_name(payload["spec"].get("inputs") or [], vary)
    if not name:
        raise ModelEngineError("Нет такого входа")
    job = dict(_base_job(payload), op="profile", base=map_inputs(payload["spec"], base or {}),
               vary=name, points=max(5, min(int(points or 41), 121)))
    out = await run_job(job, _version_files(card, payload))
    return _stamp(card, payload, out)


async def optimize(card: dict, *, targets: dict | None = None, fixed: dict | None = None,
                   bounds: dict | None = None, count: int = 3, version: int | None = None,
                   kinetics: dict | None = None, source: str = "page") -> dict:
    payload = _version_or_error(card, version)
    spec = payload["spec"]
    if card.get("kind") == "kinetics":
        kn = kinetics or {}
        job = dict(_base_job(payload), op="optimize", target=kn.get("target"),
                   temperatures=kn.get("temperatures") or [], T0=kn.get("T0"), rate=kn.get("rate"),
                   max_time=kn.get("max_time"))
    else:
        clean_targets = {}
        for key, tg in (targets or {}).items():
            name = _resolve_name(spec.get("outputs") or [], key)
            if not name or not isinstance(tg, dict):
                continue
            goal = tg.get("goal")
            if goal in ("min", "max"):
                clean_targets[name] = {"goal": goal}
            elif _num(tg.get("value")) is not None:
                clean_targets[name] = {"value": _num(tg.get("value")), "tolerance": _num(tg.get("tolerance"))}
        if not clean_targets:
            raise ModelEngineError("Задайте цель: значение выхода (с допуском), минимум или максимум")
        clean_bounds = {}
        for key, pair in (bounds or {}).items():
            name = _resolve_name(spec.get("inputs") or [], key)
            if name and isinstance(pair, (list, tuple)) and len(pair) == 2:
                lo, hi = _num(pair[0]), _num(pair[1])
                if lo is not None and hi is not None:
                    clean_bounds[name] = [lo, hi]
        job = dict(_base_job(payload), op="optimize", targets=clean_targets,
                   fixed=map_inputs(spec, fixed or {}), bounds=clean_bounds,
                   count=max(1, min(int(count or 3), 8)))
    out = await run_job(job, _version_files(card, payload))
    registry.get_store().bump_usage(card["user_id"], card["id"], source)
    return _stamp(card, payload, out)


# ---------------------------------------------------------------------------
# «Прогноз → факт»


async def add_fact(card: dict, user, inputs: dict, measured: dict, note: str = "") -> dict:
    """Замер после опыта: сохраняется вместе с прогнозом текущей версии —
    так видна реальная ошибка модели на новых данных."""
    payload = _version_or_error(card, None)
    spec = payload["spec"]
    if card.get("kind") == "kinetics":
        raise ModelEngineError("Для кинетики новые кривые добавляются в данные модели и переобучением")
    mapped = map_inputs(spec, inputs or {})
    meas = {}
    for key, value in (measured or {}).items():
        name = _resolve_name(spec.get("outputs") or [], key)
        if name and _num(value) is not None:
            meas[name] = _num(value)
    if not meas:
        raise ModelEngineError("Укажите измеренное значение хотя бы одного выхода")
    pred = await predict(card, mapped, source="fact")
    item = pred["items"][0]
    user = _user_dict(user)
    obs = {"version": payload["version"], "inputs": mapped, "measured": meas,
           "predicted": {k: (item["outputs"].get(k) or {}).get("value") for k in meas},
           "error": {k: (item["outputs"].get(k) or {}).get("error") for k in meas},
           "domain": item.get("domain", {}).get("status"),
           "user_id": (user or {}).get("id"), "note": _s(note, 300)}
    return registry.get_store().add_observation(card["user_id"], card["id"], obs)


def facts_stats(card: dict) -> dict:
    """Реальная ошибка модели на опытах после обучения — по выходам."""
    obs = registry.get_store().observations(card["user_id"], card["id"])
    stats = {}
    for o in obs:
        for name, m in (o.get("measured") or {}).items():
            p = (o.get("predicted") or {}).get(name)
            if isinstance(p, (int, float)) and isinstance(m, (int, float)):
                s = stats.setdefault(name, {"n": 0, "abs": 0.0, "inside": 0})
                s["n"] += 1
                s["abs"] += abs(p - m)
                err = (o.get("error") or {}).get(name)
                if isinstance(err, (int, float)) and abs(p - m) <= 2 * err:
                    s["inside"] += 1
    return {name: {"n": s["n"], "mae": round(s["abs"] / s["n"], 6),
                   "within_2sigma": round(s["inside"] / s["n"], 3)} for name, s in stats.items() if s["n"]}


# ---------------------------------------------------------------------------
# Представления


def _headline(card: dict) -> dict | None:
    versions = card.get("versions") or []
    current = next((v for v in versions if v.get("version") == card.get("current_version")), None)
    if not current:
        return None
    outputs = current.get("outputs") or {}
    if not outputs:
        return {"skill": current.get("skill"), "n": current.get("n")}
    name = next(iter(outputs))
    m = outputs[name]
    return {"output": name, "mae": m.get("mae"), "baseline_mae": m.get("baseline_mae"),
            "skill": m.get("skill"), "n": current.get("n"), "passed": current.get("passed")}


def _project_title(project_id: str) -> str:
    if not project_id:
        return ""
    try:
        from .projects import get_store as project_store

        return (project_store().find(project_id) or {}).get("title") or ""
    except Exception:  # noqa: BLE001
        return ""


def summary(card: dict, user=None) -> dict:
    """Карточка для каталога и списков."""
    payload = registry.get_store().current(card)
    outputs = [{"name": o["name"], "title": o.get("title"), "unit": o.get("unit")}
               for o in ((payload or {}).get("spec") or {}).get("outputs") or []]
    inputs = [{"name": i["name"], "title": i.get("title"), "unit": i.get("unit"), "type": i.get("type")}
              for i in ((payload or {}).get("spec") or {}).get("inputs") or []]
    head = _headline(card)
    if head and head.get("output"):
        unit = next((o.get("unit") for o in outputs if o["name"] == head["output"]), "")
        head["unit"] = unit or ""
        head["title"] = next((o.get("title") for o in outputs if o["name"] == head["output"]), head["output"])
    level = level_for(card, user) if user is not None else None
    return {
        "id": card["id"], "name": card.get("name"), "description": card.get("description"),
        "domain": card.get("domain"), "kind": card.get("kind"),
        "kind_title": registry.KIND_TITLES.get(card.get("kind"), card.get("kind")),
        "status": card.get("status"), "status_title": registry.STATUS_TITLES.get(card.get("status")),
        "version": card.get("current_version"), "versions": len(card.get("versions") or []),
        "headline": head, "inputs": inputs, "outputs": outputs,
        "project_id": card.get("project_id") or "", "project_title": _project_title(card.get("project_id") or ""),
        "author": card.get("author") or "", "updated_at": card.get("updated_at"),
        "url": f"model.html?id={card['id']}", "level": level,
    }


def full_view(card: dict, user, version: int | None = None) -> dict:
    """Карточка и паспорт выбранной версии для страницы модели."""
    store = registry.get_store()
    level = level_for(card, user)
    view = summary(card, user)
    number = int(version or card.get("current_version") or 0)
    payload = store.version(card["user_id"], card["id"], number) if number else None
    view.update({
        "created_at": card.get("created_at"), "published_at": card.get("published_at"),
        "verified": card.get("verified"), "forked_from": card.get("forked_from"),
        "history": card.get("versions") or [], "current_version": card.get("current_version"),
        "shown_version": number or None, "level": level,
        "facts": facts_stats(card), "usage": store.usage(card["user_id"], card["id"]),
        "can": {"use": allowed(card, user, LEVEL_USE), "read": allowed(card, user, LEVEL_READ),
                "write": allowed(card, user, LEVEL_WRITE), "admin": allowed(card, user, LEVEL_ADMIN)},
    })
    if payload:
        passport = {k: payload.get(k) for k in (
            "version", "created_at", "created_by_name", "note", "kind", "spec", "validation", "domain",
            "threshold_check", "controls", "warnings", "env", "n_rows", "excluded_rows", "data", "curves")}
        fit = payload.get("fit") or {}
        passport["fit"] = {name: {k: v for k, v in (info or {}).items() if k not in ("cov",)}
                           for name, info in (fit.get("outputs") or {}).items()}
        if not view["can"]["read"]:
            # данные коллектива остаются у коллектива: наружу — паспорт и метрики
            passport["excluded_rows"] = []
            passport["data"] = {"rows": (payload.get("data") or {}).get("rows")}
            passport.pop("curves", None)
            passport["spec"] = {k: v for k, v in payload["spec"].items() if k not in ("excluded", "controls")}
        view["passport"] = passport
    return view


def observations_view(card: dict) -> list:
    items = registry.get_store().observations(card["user_id"], card["id"])
    return sorted(items, key=lambda o: o.get("at") or 0, reverse=True)[:200]


def data_view(card: dict, version: int | None = None) -> dict:
    payload = _version_or_error(card, version)
    path = registry.get_store().version_file(card["user_id"], card["id"], payload["version"], "train.csv")
    if not path.is_file():
        return {"columns": [], "rows": [], "excluded": []}
    reader = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))
    columns, rows = (reader[0], reader[1:]) if reader else ([], [])
    excluded = {int(e["row"]): e.get("reason") for e in payload["spec"].get("excluded") or []}
    return {"columns": columns, "rows": rows[:5000], "excluded": [{"row": k, "reason": v} for k, v in excluded.items()],
            "version": payload["version"]}
