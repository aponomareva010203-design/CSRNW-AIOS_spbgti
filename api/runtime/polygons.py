"""Полигоны — симуляционная среда для проверки гипотез офиса проректора (api/runtime/polygons.py).

Полигон — сохранённая модель инновационного портфеля вуза и набор сценариев,
на которых проверяется гипотеза («стартап-студия даст ≥ 8 стартапов за 5 лет»,
«удвоение внутренних грантов вытащит проекты из долины смерти УГТ 3–5»).
Два режима:

  1. Сценарная модель (simulate): воронка проектов по УГТ 1–9. Каждый год
     проект с вероятностью p_up переходит на следующий уровень, с p_drop
     закрывается, иначе остаётся (и «застаивается»). Дойдя до exit_level,
     проект выходит из портфеля — лицензией (доход каждый год) или стартапом
     (привлечённые инвестиции). Бюджет ограничивает прогресс: если затраты
     портфеля выше бюджета, шансы перехода пропорционально снижаются.
     Монте-Карло (runs прогонов) даёт среднее и коридор p10–p90, а гипотеза
     проверяется как доля прогонов, в которых метрика достигла цели.
     Считается без LLM, детерминированно при заданном seed.
  2. ИИ-фокус-группа (panel_prompts / parse_opinion): виртуальные участники
     экосистемы (инвестор, индустриальный партнёр, эксперт фонда, ЦТТ,
     студент-основатель, финансовый директор) оценивают идею — вызов LLM
     делает роутер api/routers/polygons.py.

Хранение — как у остальных артефактов: примеры платформы в data/polygons/*.json
(в git, только чтение), свои полигоны — AIOS_RUNTIME_DIR/polygons/{user_id}/{pg-…}.json.
"""

from __future__ import annotations

import json
import math
import random
import re
import secrets
import time
from functools import lru_cache
from pathlib import Path

from .config import get_settings

USER_ID_RE = re.compile(r"^[a-f0-9]{12}$")
POLYGON_ID_RE = re.compile(r"^pg-[a-f0-9]{10}$")

MAX_YEARS = 10
MAX_RUNS = 1000
MAX_SCENARIOS = 6
MAX_PROJECTS = 400  # предохранитель: размер портфеля в одном прогоне
LEVELS = range(1, 10)

METRICS = {
    "startups": "Стартапы (выход через компанию)",
    "licenses": "Лицензионные сделки",
    "exits": "Все выходы на рынок",
    "income": "Доход от лицензий, тыс. ₽",
    "investments": "Привлечённые инвестиции, тыс. ₽",
    "cost": "Затраты на портфель, тыс. ₽",
    "roi": "Отдача на 1 ₽ затрат",
    "stale": "Проекты-кандидаты на закрытие (застой)",
    "active": "Активные проекты в портфеле",
}
OPS = (">=", "<=")


class PolygonError(ValueError):
    """Некорректные параметры полигона."""


def _num(value, default, lo, hi, integer=False):
    try:
        x = float(value)
    except (TypeError, ValueError):
        x = float(default)
    if math.isnan(x) or math.isinf(x):
        x = float(default)
    x = max(lo, min(hi, x))
    return int(round(x)) if integer else x


def _text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


# ---------------------------------------------------------------------------
# Параметры
# ---------------------------------------------------------------------------

DEFAULT_LEVELS = {
    # p_up — шанс перейти на следующий УГТ за год, p_drop — шанс закрытия,
    # cost — затраты на один проект в год, тыс. ₽ (ориентиры для демо)
    1: {"p_up": 0.35, "p_drop": 0.10, "cost": 300},
    2: {"p_up": 0.35, "p_drop": 0.12, "cost": 500},
    3: {"p_up": 0.25, "p_drop": 0.15, "cost": 1200},
    4: {"p_up": 0.20, "p_drop": 0.15, "cost": 2500},
    5: {"p_up": 0.20, "p_drop": 0.12, "cost": 4000},
    6: {"p_up": 0.25, "p_drop": 0.10, "cost": 6000},
    7: {"p_up": 0.30, "p_drop": 0.08, "cost": 8000},
    8: {"p_up": 0.35, "p_drop": 0.05, "cost": 9000},
}


def normalize_params(raw: dict | None) -> dict:
    """Параметры модели с ограничениями; недостающее — значения по умолчанию."""
    raw = raw if isinstance(raw, dict) else {}
    levels_raw = raw.get("levels") if isinstance(raw.get("levels"), dict) else {}
    levels = {}
    for lvl in range(1, 9):
        src = levels_raw.get(str(lvl)) or levels_raw.get(lvl) or {}
        d = DEFAULT_LEVELS[lvl]
        levels[str(lvl)] = {
            "p_up": _num(src.get("p_up"), d["p_up"], 0, 1),
            "p_drop": _num(src.get("p_drop"), d["p_drop"], 0, 1),
            "cost": _num(src.get("cost"), d["cost"], 0, 1_000_000),
        }
        total = levels[str(lvl)]["p_up"] + levels[str(lvl)]["p_drop"]
        if total > 1:  # шансы перехода и закрытия делят один год
            levels[str(lvl)]["p_up"] /= total
            levels[str(lvl)]["p_drop"] /= total
    initial_raw = raw.get("initial") if isinstance(raw.get("initial"), dict) else {}
    initial = {str(l): _num(initial_raw.get(str(l)), 0, 0, MAX_PROJECTS, True) for l in range(1, 9)}
    if sum(initial.values()) > MAX_PROJECTS:
        raise PolygonError(f"В стартовом портфеле больше {MAX_PROJECTS} проектов")
    exit_level = _num(raw.get("exit_level"), 7, 3, 9, True)
    return {
        "horizon_years": _num(raw.get("horizon_years"), 5, 1, MAX_YEARS, True),
        "runs": _num(raw.get("runs"), 400, 20, MAX_RUNS, True),
        "seed": _num(raw.get("seed"), 2030, 0, 10**9, True),
        "initial": initial,
        "intake_per_year": _num(raw.get("intake_per_year"), 10, 0, 200, True),
        "entry_level": _num(raw.get("entry_level"), 2, 1, 8, True),
        "exit_level": exit_level,
        "levels": levels,
        "p_license": _num(raw.get("p_license"), 0.5, 0, 1),
        "license_income": _num(raw.get("license_income"), 1500, 0, 1_000_000),
        "investment_per_startup": _num(raw.get("investment_per_startup"), 15000, 0, 10_000_000),
        "annual_budget": _num(raw.get("annual_budget"), 0, 0, 100_000_000),
        "fixed_cost": _num(raw.get("fixed_cost"), 0, 0, 10_000_000),
        "stale_years": _num(raw.get("stale_years"), 2, 1, 10, True),
    }


def normalize_scenario(raw: dict | None, i: int) -> dict:
    """Сценарий — правки базовых параметров: приток, бюджет, усиление «долины смерти»."""
    raw = raw if isinstance(raw, dict) else {}
    sc = {
        "id": _text(raw.get("id"), 40) or f"s{i + 1}",
        "name": _text(raw.get("name"), 120) or f"Сценарий {i + 1}",
        "description": _text(raw.get("description"), 600),
        # относительные изменения, %: +30 — шанс перехода на УГТ 3–5 выше на 30 %
        "valley_boost": _num(raw.get("valley_boost"), 0, -90, 300),
        "up_boost": _num(raw.get("up_boost"), 0, -90, 300),
        "drop_change": _num(raw.get("drop_change"), 0, -90, 300),
        "p_license_change": _num(raw.get("p_license_change"), 0, -100, 100),
    }
    for key, hi in (("intake_per_year", 200), ("annual_budget", 100_000_000), ("fixed_cost", 10_000_000)):
        if raw.get(key) not in (None, ""):
            sc[key] = _num(raw.get(key), 0, 0, hi, key == "intake_per_year")
    return sc


def apply_scenario(params: dict, sc: dict) -> dict:
    p = json.loads(json.dumps(params))
    for key in ("intake_per_year", "annual_budget", "fixed_cost"):
        if key in sc:
            p[key] = sc[key]
    for lvl, lv in p["levels"].items():
        boost = sc.get("up_boost", 0) + (sc.get("valley_boost", 0) if 3 <= int(lvl) <= 5 else 0)
        lv["p_up"] = min(1.0, lv["p_up"] * (1 + boost / 100))
        lv["p_drop"] = min(1.0, lv["p_drop"] * (1 + sc.get("drop_change", 0) / 100))
        total = lv["p_up"] + lv["p_drop"]
        if total > 1:
            lv["p_up"] /= total
            lv["p_drop"] /= total
    p["p_license"] = max(0.0, min(1.0, p["p_license"] + sc.get("p_license_change", 0) / 100))
    return p


def normalize_hypothesis(raw: dict | None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    metric = raw.get("metric") if raw.get("metric") in METRICS else "startups"
    op = raw.get("op") if raw.get("op") in OPS else ">="
    return {
        "statement": _text(raw.get("statement"), 600),
        "metric": metric,
        "op": op,
        "value": _num(raw.get("value"), 0, -1e12, 1e12),
        "scenario_id": _text(raw.get("scenario_id"), 40),
    }


# ---------------------------------------------------------------------------
# Симуляция
# ---------------------------------------------------------------------------


def _one_run(p: dict, rng: random.Random) -> dict:
    """Один прогон: помесячная точность не нужна — шаг год."""
    projects = []  # [уровень, лет на уровне]
    for lvl, count in p["initial"].items():
        projects.extend([int(lvl), 0] for _ in range(count))
    exit_level = p["exit_level"]
    licenses = startups = 0
    cost = income = investments = 0.0
    yearly = []
    for _year in range(p["horizon_years"]):
        projects.extend([p["entry_level"], 0] for _ in range(p["intake_per_year"]))
        if len(projects) > MAX_PROJECTS:
            projects = projects[:MAX_PROJECTS]
        demand = sum(p["levels"][str(lv)]["cost"] for lv, _ in projects)
        budget = p["annual_budget"]
        scale = 1.0 if not budget or demand <= budget else budget / demand
        cost += min(demand, budget) if budget else demand
        cost += p["fixed_cost"]
        income += licenses * p["license_income"]  # доход от лицензий прошлых лет
        survivors = []
        for proj in projects:
            lv = p["levels"][str(proj[0])]
            r = rng.random()
            if r < lv["p_up"] * scale:
                proj[0] += 1
                proj[1] = 0
                if proj[0] >= exit_level:
                    if rng.random() < p["p_license"]:
                        licenses += 1
                    else:
                        startups += 1
                        investments += p["investment_per_startup"]
                    continue
            elif r < lv["p_up"] * scale + lv["p_drop"]:
                continue
            else:
                proj[1] += 1
            survivors.append(proj)
        projects = survivors
        stale = sum(1 for _, yrs in projects if yrs >= p["stale_years"])
        yearly.append({
            "active": len(projects), "licenses": licenses, "startups": startups,
            "exits": licenses + startups, "cost": cost, "income": income,
            "investments": investments, "stale": stale,
        })
    levels = [0] * 9
    for lv, _ in projects:
        levels[lv - 1] += 1
    final = dict(yearly[-1]) if yearly else {}
    final["roi"] = (final.get("income", 0) + final.get("investments", 0)) / final["cost"] if final.get("cost") else 0.0
    return {"yearly": yearly, "final": final, "levels": levels}


def _quantile(sorted_vals: list, q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(idx), math.ceil(idx)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)


def _stats(values: list) -> dict:
    s = sorted(values)
    mean = sum(s) / len(s) if s else 0.0
    return {"mean": round(mean, 2), "p10": round(_quantile(s, 0.1), 2), "p90": round(_quantile(s, 0.9), 2)}


def _check(value: float, hyp: dict) -> bool:
    return value >= hyp["value"] if hyp["op"] == ">=" else value <= hyp["value"]


def simulate(params_raw: dict, scenarios_raw: list | None = None, hypothesis_raw: dict | None = None) -> dict:
    """Монте-Карло по базовому сценарию и каждому из сценариев полигона."""
    params = normalize_params(params_raw)
    scenarios = [normalize_scenario({"id": "base", "name": "Базовый сценарий"}, 0)]
    for i, sc in enumerate((scenarios_raw or [])[:MAX_SCENARIOS]):
        sc = normalize_scenario(sc, i + 1)
        if sc["id"] == "base":
            sc["id"] = f"s{i + 1}"
        scenarios.append(sc)
    hyp = normalize_hypothesis(hypothesis_raw)
    started = time.perf_counter()
    results = []
    for sc in scenarios:
        p = apply_scenario(params, sc)
        rng = random.Random(params["seed"])  # одинаковые случайные числа — честное сравнение
        runs = [_one_run(p, rng) for _ in range(params["runs"])]
        years = params["horizon_years"]
        series = {
            m: [_stats([r["yearly"][y][m] for r in runs]) for y in range(years)]
            for m in ("active", "licenses", "startups", "exits", "cost", "income", "investments", "stale")
        }
        final = {m: _stats([r["final"].get(m, 0) for r in runs]) for m in METRICS}
        level_mean = [round(sum(r["levels"][i] for r in runs) / len(runs), 2) for i in range(9)]
        hit = None
        if hyp["statement"] or hyp["value"]:
            hit = round(sum(1 for r in runs if _check(r["final"].get(hyp["metric"], 0), hyp)) / len(runs), 3)
        results.append({
            "id": sc["id"], "name": sc["name"], "description": sc["description"],
            "series": series, "final": final, "levels": level_mean, "hypothesis_probability": hit,
        })
    return {
        "params": params,
        "scenarios": scenarios,
        "hypothesis": hyp,
        "metrics": METRICS,
        "results": results,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


# ---------------------------------------------------------------------------
# ИИ-фокус-группа
# ---------------------------------------------------------------------------

DEFAULT_PANEL = [
    {"id": "investor", "name": "Венчурный инвестор",
     "persona": "партнёр посевного фонда; смотрит на команду, размер рынка, тракшн, права на технологию и долю вуза"},
    {"id": "industry", "name": "Индустриальный партнёр",
     "persona": "технический директор промышленной компании; важны применимость, сроки пилота, цена внедрения, риски для производства"},
    {"id": "fund", "name": "Эксперт Фонда содействия инновациям",
     "persona": "эксперт грантовых конкурсов; оценивает научно-техническую новизну, реалистичность плана, соответствие УГТ и требованиям программ"},
    {"id": "tto", "name": "Руководитель ЦТТ",
     "persona": "отвечает за РИД вуза; смотрит на охраноспособность, патентную чистоту, способ коммерциализации, нагрузку на команду ЦТТ"},
    {"id": "student", "name": "Студент-основатель",
     "persona": "студент старших курсов в акселераторе; важны понятность, время, деньги на старт, поддержка трекера, «Стартап как диплом»"},
    {"id": "cfo", "name": "Финансовый директор вуза",
     "persona": "проректор по экономике; важны затраты, окупаемость, риски для бюджета и отчётность по программе развития"},
]
MAX_PANEL = 8
VERDICTS = ("поддержать", "доработать", "отклонить")


def normalize_panel(raw) -> list:
    if not isinstance(raw, list) or not raw:
        return [dict(r) for r in DEFAULT_PANEL]
    roles = []
    for i, r in enumerate(raw[:MAX_PANEL]):
        if not isinstance(r, dict):
            continue
        name = _text(r.get("name"), 80)
        if not name:
            continue
        roles.append({"id": _text(r.get("id"), 40) or f"r{i + 1}", "name": name,
                      "persona": _text(r.get("persona"), 400)})
    return roles or [dict(r) for r in DEFAULT_PANEL]


def panel_messages(role: dict, idea: str, context: str = "") -> list:
    system = (
        f"Ты — {role['name']} ({role['persona']}). Ты участвуешь в фокус-группе на платформе "
        "управления инновациями университета и оцениваешь идею, гипотезу или проект офиса "
        "проректора по инновациям. Отвечай строго от лица своей роли, по-русски, конкретно и "
        "критично, без выдуманных фактов о вузе. Верни ТОЛЬКО JSON без пояснений вида "
        '{"score": <целое 1-10>, "verdict": "поддержать|доработать|отклонить", '
        '"pros": ["..."], "cons": ["..."], "condition": "при каком условии поддержишь", '
        '"question": "главный вопрос к авторам"}. В pros и cons — по 2–3 коротких пункта.'
    )
    user = f"Идея / гипотеза для оценки:\n{idea[:4000]}"
    if context:
        user += f"\n\nРезультаты сценарной модели полигона:\n{context[:3000]}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_opinion(text: str, role: dict) -> dict:
    """Мнение роли из ответа модели; без валидного JSON — текст как есть."""
    raw = (text or "").strip()
    m = re.search(r"\{.*\}", raw, re.S)
    data = None
    if m:
        try:
            data = json.loads(m.group(0))
        except ValueError:
            data = None
    if not isinstance(data, dict):
        return {"role": role["name"], "role_id": role["id"], "score": None, "verdict": "",
                "pros": [], "cons": [], "condition": "", "question": "", "text": raw[:2000]}
    verdict = str(data.get("verdict") or "").strip().lower()
    score = data.get("score")
    try:
        score = max(1, min(10, int(round(float(score)))))
    except (TypeError, ValueError):
        score = None

    def lst(v):
        return [_text(x, 300) for x in (v if isinstance(v, list) else [v]) if _text(x, 300)][:4]

    return {
        "role": role["name"], "role_id": role["id"], "score": score,
        "verdict": verdict if verdict in VERDICTS else "",
        "pros": lst(data.get("pros")), "cons": lst(data.get("cons")),
        "condition": _text(data.get("condition"), 500), "question": _text(data.get("question"), 500),
    }


def summarize_panel(opinions: list) -> dict:
    scores = [o["score"] for o in opinions if isinstance(o.get("score"), int)]
    votes = {v: sum(1 for o in opinions if o.get("verdict") == v) for v in VERDICTS}
    avg = round(sum(scores) / len(scores), 1) if scores else None
    if avg is None:
        decision = "нет оценок"
    elif votes["отклонить"] * 2 >= len(opinions) or avg < 4.5:
        decision = "идея не прошла фокус-группу"
    elif votes["поддержать"] * 2 > len(opinions) and avg >= 7:
        decision = "идею поддерживают"
    else:
        decision = "идею стоит доработать"
    return {"average_score": avg, "votes": votes, "decision": decision}


def results_context(sim: dict) -> str:
    """Короткая текстовая сводка результатов симуляции — контекст для фокус-группы."""
    lines = []
    for r in sim.get("results", []):
        f = r["final"]
        lines.append(
            f"{r['name']}: стартапов {f['startups']['mean']:.1f}, лицензий {f['licenses']['mean']:.1f}, "
            f"затраты {f['cost']['mean']:.0f} тыс. ₽, отдача {f['roi']['mean']:.2f} ₽ на 1 ₽"
            + (f", гипотеза подтверждается в {r['hypothesis_probability'] * 100:.0f} % прогонов"
               if r.get("hypothesis_probability") is not None else "")
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Хранилище
# ---------------------------------------------------------------------------


def clean_polygon(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise PolygonError("Ожидается объект полигона")
    name = _text(raw.get("name"), 200)
    if not name:
        raise PolygonError("Нужно название полигона")
    scenarios = [normalize_scenario(s, i) for i, s in enumerate((raw.get("scenarios") or [])[:MAX_SCENARIOS])]
    return {
        "name": name,
        "description": _text(raw.get("description"), 2000),
        "hypothesis": normalize_hypothesis(raw.get("hypothesis")),
        "params": normalize_params(raw.get("params")),
        "scenarios": scenarios,
        "panel": normalize_panel(raw.get("panel")),
        "idea": _text(raw.get("idea"), 4000),
        "tags": [_text(t, 40) for t in (raw.get("tags") or [])[:8] if _text(t, 40)],
        "project_id": _text(raw.get("project_id"), 20),
    }


class PolygonStore:
    def __init__(self, root: Path, examples_dir: Path) -> None:
        self.root = Path(root)
        self.examples_dir = Path(examples_dir)

    def _dir(self, user_id: str) -> Path:
        if not USER_ID_RE.match(user_id or ""):
            raise PolygonError("Некорректный пользователь")
        return self.root / user_id

    def examples(self) -> list:
        items = []
        if self.examples_dir.is_dir():
            for path in sorted(self.examples_dir.glob("pg-*.json")):
                try:
                    card = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                card["example"] = True
                items.append(card)
        return items

    def list(self, user_id: str) -> list:
        folder = self._dir(user_id)
        items = []
        if folder.is_dir():
            for path in folder.glob("pg-*.json"):
                try:
                    items.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
        items.sort(key=lambda c: c.get("updated_at", 0), reverse=True)
        return items

    def get(self, polygon_id: str, user_id: str | None = None) -> dict | None:
        if not POLYGON_ID_RE.match(polygon_id or ""):
            return None
        for card in self.examples():
            if card.get("id") == polygon_id:
                return card
        if user_id:
            path = self._dir(user_id) / f"{polygon_id}.json"
            if path.is_file():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    return None
        return None

    def save(self, user_id: str, raw: dict, author: str = "") -> dict:
        data = clean_polygon(raw)
        folder = self._dir(user_id)
        pid = str(raw.get("id") or "")
        existing = None
        if POLYGON_ID_RE.match(pid):
            path = folder / f"{pid}.json"
            if path.is_file():
                existing = json.loads(path.read_text(encoding="utf-8"))
        if existing is None:
            pid = "pg-" + secrets.token_hex(5)
        now = int(time.time())
        card = {
            "id": pid,
            "owner_id": user_id,
            "author": (existing or {}).get("author") or author,
            "created_at": (existing or {}).get("created_at") or now,
            "updated_at": now,
            **data,
        }
        for key in ("last_run", "last_panel"):
            if key in raw and isinstance(raw[key], dict):
                card[key] = raw[key]
            elif existing and key in existing:
                card[key] = existing[key]
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f".{pid}.tmp"
        tmp.write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(folder / f"{pid}.json")
        return card

    def delete(self, user_id: str, polygon_id: str) -> bool:
        if not POLYGON_ID_RE.match(polygon_id or ""):
            return False
        path = self._dir(user_id) / f"{polygon_id}.json"
        if path.is_file():
            path.unlink()
            return True
        return False


EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "data" / "polygons"


@lru_cache(maxsize=1)
def get_store() -> PolygonStore:
    return PolygonStore(get_settings().runtime_dir / "polygons", EXAMPLES_DIR)
