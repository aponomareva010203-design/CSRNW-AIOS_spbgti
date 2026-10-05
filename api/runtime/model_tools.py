"""Инструменты чата для моделей коллективов (api/runtime/model_tools.py).

Агент вызывает модель как прибор: get_models — найти, run_model — прогноз
с погрешностью и отметкой области применимости, optimize_model — подобрать
режим под цель, train_model — обучить новую модель или новую версию по
таблице опытов (обучает и проверяет платформа, не LLM), save_model_fact —
внести замер после опыта («прогноз → факт»).

Каждый ответ помечен номером версии модели. Карточку результата в чате
рисует интерфейс (frontend/js/workspace.js) прямо из ответа инструмента —
агенту не нужно её переписывать.
"""

from __future__ import annotations

from . import model_engine as engine
from . import model_registry as registry
from .model_engine import ModelEngineError
from .model_registry import ModelRegistryError
from .tools import Tool


def _user(ctx: dict | None) -> str:
    return (((ctx or {}).get("session") or {}).get("user_id")) or ""


def _fmt(value, digits: int = 4):
    if value is None:
        return None
    try:
        return float(f"{float(value):.{digits}g}")
    except (TypeError, ValueError):
        return value


def _compact(card: dict) -> dict:
    payload = registry.get_store().current(card) or {}
    spec = payload.get("spec") or {}
    domain = payload.get("domain") or {}
    inputs = []
    for inp in spec.get("inputs") or []:
        item = {"имя": inp["name"], "название": inp.get("title"), "единицы": inp.get("unit") or "",
                "тип": inp.get("type", "number")}
        d = domain.get(inp["name"]) or {}
        if d.get("type") == "number":
            item["диапазон_опытов"] = [d.get("min"), d.get("max")]
        elif d.get("type") == "category":
            item["значения"] = d.get("levels")
        inputs.append(item)
    per_output = (payload.get("validation") or {}).get("per_output") or {}
    outputs = []
    for out in spec.get("outputs") or []:
        m = per_output.get(out["name"]) or {}
        outputs.append({"имя": out["name"], "название": out.get("title"), "единицы": out.get("unit") or "",
                        "типичная_ошибка": _fmt(m.get("mae")), "лучше_среднего_на_процентов":
                        round(m["skill"] * 100) if isinstance(m.get("skill"), (int, float)) else None})
    view = {"id": card["id"], "название": card.get("name"), "вид": registry.KIND_TITLES.get(card.get("kind")),
            "статус": registry.STATUS_TITLES.get(card.get("status")), "версия": card.get("current_version"),
            "опытов": payload.get("n_rows"), "входы": inputs, "выходы": outputs,
            "страница": f"model.html?id={card['id']}"}
    if card.get("kind") == "kinetics":
        view["как_вызывать"] = ("run_model с program — температурная программа: сегменты "
                                "{kind: ramp, to, rate} и {kind: hold, time}, T0 — начальная температура")
    return view


def _ui_card(result: dict, mode: str) -> dict:
    """Данные для карточки модели в чате (рисует интерфейс)."""
    return {"component": "model_result", "mode": mode, "model": result.get("model"),
            "outputs_meta": result.get("outputs_meta"), "inputs_meta": result.get("inputs_meta"),
            "items": (result.get("items") or [])[:12], "kinetics": {k: result.get(k) for k in ("final", "reach", "domain")}
            if result.get("t") is not None else None, "target": result.get("target")}


def model_tools() -> list[Tool]:
    async def get_models(args: dict, ctx: dict | None) -> dict:
        user_id = _user(ctx)
        query = str((args or {}).get("query") or "").strip().lower()
        cards = engine.visible_cards(user_id or None)
        if query:
            words = [w for w in query.split() if len(w) >= 3]

            def match(card):
                payload = registry.get_store().current(card) or {}
                blob = " ".join([card.get("name") or "", card.get("description") or "", card.get("domain") or ""]
                                + [o.get("title") or "" for o in (payload.get("spec") or {}).get("outputs") or []]
                                + [i.get("title") or "" for i in (payload.get("spec") or {}).get("inputs") or []]).lower()
                return all(w in blob for w in words) if words else query in blob

            cards = [c for c in cards if match(c)]
        cards = [c for c in cards if c.get("current_version")][:20]
        if not cards:
            return {"ok": True, "модели": [], "подсказка": "Подходящих обученных моделей нет. Модель можно "
                    "обучить по таблице опытов — инструмент train_model (агент «Конструктор моделей»)."}
        return {"ok": True, "модели": [_compact(c) for c in cards]}

    async def run_model(args: dict, ctx: dict | None) -> dict:
        user_id = _user(ctx) or None
        args = args or {}
        try:
            card = engine.resolve(str(args.get("model_id") or ""), user_id, engine.LEVEL_USE)
            if card.get("kind") == "kinetics":
                res = await engine.kinetics_run(card, program=args.get("program") or [], T0=args.get("T0"),
                                                targets=args.get("targets"), version=args.get("version"),
                                                source="chat")
                step = max(1, len(res.get("t") or []) // 12)
                return {"ok": True, "модель": res["model"],
                        "итог": {"степень_превращения_в_конце": res.get("final"),
                                 "время_достижения": res.get("reach"), "область": res.get("domain")},
                        "траектория_кратко": [{"t": t, "T": T, "y": y} for t, T, y in
                                              list(zip(res["t"], res["T"], res["y"]))[::step]],
                        "card": _ui_card(res, "kinetics"),
                        "подсказка": "Назовите версию модели и итог. Если область не inside — скажите, что "
                                     "режим выходит за температуры опытов и прогноз недостоверен."}
            res = await engine.predict(card, args.get("inputs") or {}, version=args.get("version"), source="chat")
        except ModelEngineError as exc:
            return {"ok": False, "error": str(exc)}
        items = []
        for it in res.get("items") or []:
            items.append({"входы": it.get("inputs"), "выходы": it.get("outputs"),
                          "область": it.get("domain", {}).get("status"),
                          "пояснения": it.get("domain", {}).get("notes")})
        return {"ok": True, "модель": res["model"], "прогноз": items[:50], "card": _ui_card(res, "predict"),
                "подсказка": "Карточку с числами интерфейс покажет сам. В тексте назовите значение ± ошибку "
                             "с единицами и версию модели. Если область outside — прямо скажите, что прогноз "
                             "недостоверен и нужен опыт; edge — что прогноз на границе опытов."}

    async def optimize_model(args: dict, ctx: dict | None) -> dict:
        user_id = _user(ctx) or None
        args = args or {}
        try:
            card = engine.resolve(str(args.get("model_id") or ""), user_id, engine.LEVEL_USE)
            res = await engine.optimize(card, targets=args.get("targets"), fixed=args.get("fixed"),
                                        bounds=args.get("bounds"), count=args.get("count") or 3,
                                        version=args.get("version"),
                                        kinetics={k: args.get(k) for k in ("target", "temperatures", "T0",
                                                                           "rate", "max_time")},
                                        source="chat")
        except ModelEngineError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "модель": res["model"], "варианты": res.get("items"), "цель": res.get("target"),
                "card": _ui_card(res, "optimize"),
                "подсказка": "Покажите 2–3 режима с прогнозом ± ошибка и отклонением от цели; отметьте, "
                             "какие вне области опытов. Предложите поставить опыт и внести результат "
                             "(save_model_fact)."}

    async def train_model(args: dict, ctx: dict | None) -> dict:
        user_id = _user(ctx)
        if not user_id:
            return {"ok": False, "error": "Нужен вход в AIOS: модели сохраняются в вашем кабинете."}
        args = args or {}
        blobs = []
        try:
            att = str(args.get("data_file") or "").strip()
            if att:
                from . import uploads

                store = uploads.get_store()
                meta = store.get(user_id, att)
                if meta is None:
                    return {"ok": False, "error": f"Файл данных не найден: {att}"}
                path = store.content_path(user_id, meta)
                blobs.append((meta.get("filename") or "data.xlsx", path.read_bytes()))
            elif isinstance(args.get("data"), dict):
                blobs.append(("data.csv", engine.inline_data(args["data"])))
            spec_raw = {k: args.get(k) for k in ("inputs", "outputs", "method", "formula", "kinetics",
                                                 "threshold", "controls", "excluded", "protein_embedding")
                        if args.get(k) is not None}
            model_id = str(args.get("model_id") or "").strip()
            if model_id:
                card = engine.resolve(model_id, user_id, engine.LEVEL_WRITE)
                current = registry.get_store().current(card)
                if current is None:
                    return {"ok": False, "error": "У модели нет текущей версии"}
                merged = dict(current["spec"])
                merged.update(spec_raw)
                prev = registry.get_store().version_file(card["user_id"], card["id"], current["version"], "train.csv")
                mode = str(args.get("mode") or ("append" if blobs else "same"))
                if mode in ("append", "same") and prev.is_file():
                    blobs.insert(0, ("train.csv", prev.read_bytes()))
                result = await engine.train_version(user_id, card=card, fields={}, spec_raw=merged, blobs=blobs,
                                                    note=str(args.get("note") or ""))
            else:
                result = await engine.train_version(
                    user_id, card=None,
                    fields={"kind": args.get("kind") or "table", "name": args.get("name"),
                            "description": args.get("description"), "domain": args.get("domain"),
                            "project_id": args.get("project_id")},
                    spec_raw=spec_raw, blobs=blobs, note=str(args.get("note") or "первая версия"))
        except (ModelEngineError, ModelRegistryError) as exc:
            return {"ok": False, "error": str(exc)}
        card, version = result["card"], result["version"]
        per_output = (version.get("validation") or {}).get("per_output") or {}
        effects = (version.get("validation") or {}).get("effects") or {}
        report = {}
        for out in version["spec"].get("outputs") or []:
            m = per_output.get(out["name"]) or {}
            report[out.get("title") or out["name"]] = {
                "метод": m.get("method"), "опытов": m.get("n"), "проверка": m.get("scheme"),
                "типичная_ошибка": _fmt(m.get("mae")), "единицы": out.get("unit") or "",
                "ошибка_угадывания_средним": _fmt(m.get("baseline_mae")),
                "лучше_среднего_на_процентов": round(m["skill"] * 100) if isinstance(m.get("skill"), (int, float)) else None,
                "новые_опыты": m.get("curve_verdict"),
                "главные_факторы": (effects.get(out["name"]) or [])[:3],
            }
        fit_info = ((version.get("fit") or {}).get("outputs") or {})
        formulas = {k: v.get("equation") for k, v in fit_info.items() if v.get("equation")}
        params = {k: v.get("params") for k, v in fit_info.items() if v.get("params")}
        return {
            "ok": True, "model_id": card["id"], "версия": version["version"],
            "стала_текущей": result["made_current"], "сообщение": result["message"] or None,
            "статус": registry.STATUS_TITLES.get(card.get("status")),
            "качество": report, "формула": formulas or None, "коэффициенты": params or None,
            "порог_приёмки": version.get("threshold_check"), "контрольные_значения": version.get("controls") or None,
            "предупреждения": version.get("warnings") or None,
            "страница": f"model.html?id={card['id']}",
            "card": {"component": "model_result", "mode": "train",
                     "model": {"id": card["id"], "name": card.get("name"), "version": version["version"],
                               "status": card.get("status"), "kind": card.get("kind"),
                               "url": f"model.html?id={card['id']}"},
                     "summary": registry.version_summary(version),
                     "outputs_meta": [{"name": o["name"], "title": o.get("title"), "unit": o.get("unit")}
                                      for o in version["spec"].get("outputs") or []]},
            "подсказка": "Расскажите человеку простыми словами: насколько модель лучше угадывания средним, "
                         "типичная ошибка с единицами, главные факторы и помогут ли новые опыты. Дайте "
                         "ссылку на страницу модели Markdown-ссылкой. Публикует модель человек кнопкой на странице.",
        }

    async def save_model_fact(args: dict, ctx: dict | None) -> dict:
        user_id = _user(ctx)
        if not user_id:
            return {"ok": False, "error": "Нужен вход в AIOS"}
        args = args or {}
        try:
            card = engine.resolve(str(args.get("model_id") or ""), user_id, engine.LEVEL_WRITE)
            obs = await engine.add_fact(card, user_id, args.get("inputs") or {}, args.get("measured") or {},
                                        str(args.get("note") or ""))
        except (ModelEngineError, ModelRegistryError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "замер": obs, "статистика": engine.facts_stats(card),
                "подсказка": "Скажите, насколько прогноз разошёлся с опытом и попал ли в ±2 ошибки. Если "
                             "расхождения систематические — предложите переобучить модель с новыми опытами."}

    return [
        Tool(
            name="get_models",
            description="Модели коллективов на платформе: что предсказывают, входы с диапазонами опытов, "
            "типичная ошибка, статус и версия. Вызывайте, когда вопрос требует числа, которое может посчитать "
            "модель коллектива (режим, свойство, выход), — до того как считать самим.",
            parameters={"type": "object", "properties": {
                "query": {"type": "string", "description": "слова из задачи: что предсказать (необязательно)"}}},
            handler=get_models,
        ),
        Tool(
            name="run_model",
            description="Прогноз моделью коллектива: значение ± типичная ошибка, отметка области применимости "
            "(inside — внутри опытов, edge — на границе, outside — прогноз недостоверен) и номер версии. "
            "inputs — объект {вход: значение} или список таких объектов (таблица вариантов). Для кинетических "
            "моделей вместо inputs — program (температурная программа) и T0.",
            parameters={"type": "object", "properties": {
                "model_id": {"type": "string", "description": "id модели (mdl-…) из get_models"},
                "inputs": {"description": "объект {имя или название входа: значение} или список объектов"},
                "program": {"type": "array", "items": {"type": "object"},
                            "description": "кинетика: сегменты {kind: ramp, to, rate} и {kind: hold, time}"},
                "T0": {"type": "number", "description": "кинетика: начальная температура"},
                "targets": {"type": "array", "items": {"type": "number"},
                            "description": "кинетика: степени превращения, для которых нужно время достижения"},
                "version": {"type": "integer", "description": "номер версии (по умолчанию текущая)"}},
                "required": ["model_id"]},
            handler=run_model,
        ),
        Tool(
            name="optimize_model",
            description="Обратная задача: подобрать режим под цель по модели коллектива. targets — "
            "{выход: {value, tolerance}} или {выход: {goal: min|max}}; fixed — входы, которые нельзя менять; "
            "bounds — {вход: [мин, макс]}. Для кинетики: target (степень превращения) и temperatures — "
            "время выдержки до цели при каждой температуре.",
            parameters={"type": "object", "properties": {
                "model_id": {"type": "string"},
                "targets": {"type": "object"},
                "fixed": {"type": "object"},
                "bounds": {"type": "object"},
                "count": {"type": "integer", "minimum": 1, "maximum": 8},
                "target": {"type": "number", "description": "кинетика: целевая степень превращения"},
                "temperatures": {"type": "array", "items": {"type": "number"}},
                "T0": {"type": "number"}, "rate": {"type": "number"},
                "version": {"type": "integer"}},
                "required": ["model_id"]},
            handler=optimize_model,
        ),
        Tool(
            name="train_model",
            description="Обучает модель коллектива по таблице опытов и проверяет её силами платформы "
            "(ошибка на опытах, не участвовавших в обучении, сравнение с угадыванием средним, кривая "
            "обучения, влияние факторов). Без model_id — новая модель (черновик владельца); с model_id — "
            "новая версия (mode: append — добавить опыты из файла, replace — заменить данные, same — те же "
            "данные с другими настройками). kind: table — по таблице; formula — по формуле (formula.expression, "
            "входы с symbol); kinetics — по уравнению скорости (kinetics.rate с a и T, kinetics.columns: run, t, "
            "T, y). threshold — порог приёмки {output, max}: без него модель нельзя опубликовать.",
            parameters={"type": "object", "properties": {
                "model_id": {"type": "string", "description": "для новой версии существующей модели"},
                "mode": {"type": "string", "enum": ["append", "replace", "same"]},
                "kind": {"type": "string", "enum": ["table", "formula", "kinetics"]},
                "name": {"type": "string"}, "description": {"type": "string"}, "domain": {"type": "string"},
                "data_file": {"type": "string", "description": "id вложения чата (att-…) с таблицей Excel/CSV"},
                "data": {"type": "object", "description": "таблица прямо в запросе: {columns: [...], rows: [[...]]}"},
                "inputs": {"type": "array", "items": {"type": "object"},
                           "description": "входы: {column (заголовок столбца), title, unit, type: number|category|smiles|sequence, symbol (для формул)}"},
                "outputs": {"type": "array", "items": {"type": "object"},
                            "description": "выходы: {column, title, unit}"},
                "method": {"type": "string", "enum": ["auto", "linear", "quadratic", "gp", "gbm"]},
                "protein_embedding": {"type": "string", "enum": ["esm2"],
                                      "description": "для входов-последовательностей белка: добавить признаки открытой модели ESM-2 (если установлена на сервере)"},
                "formula": {"type": "object", "description": "{expression, fit_scale: linear|log, params: {имя: {init, min, max}}, constants}"},
                "kinetics": {"type": "object", "description": "{rate, state, temperature_unit, time_unit, params, columns: {run, t, T, y}}"},
                "threshold": {"type": "object", "description": "{output, max} — какую ошибку коллектив готов принять"},
                "controls": {"type": "array", "items": {"type": "object"},
                             "description": "контрольные значения: {inputs, expected, tolerance}"},
                "excluded": {"type": "array", "items": {"type": "object"},
                             "description": "исключённые строки: {row, reason}"},
                "note": {"type": "string", "description": "что изменилось в этой версии"}}},
            handler=train_model,
        ),
        Tool(
            name="save_model_fact",
            description="Замер после опыта: условия и измеренное значение. Платформа сохраняет его рядом с "
            "прогнозом текущей версии — так видна реальная ошибка модели на новых опытах.",
            parameters={"type": "object", "properties": {
                "model_id": {"type": "string"},
                "inputs": {"type": "object", "description": "условия опыта"},
                "measured": {"type": "object", "description": "{выход: измеренное значение}"},
                "note": {"type": "string"}},
                "required": ["model_id", "inputs", "measured"]},
            handler=save_model_fact,
        ),
    ]
