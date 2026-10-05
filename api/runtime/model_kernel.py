"""Ядро моделей коллективов — исполняется ТОЛЬКО в расчётной песочнице.

Платформа не импортирует этот модуль в процесс API: numpy, scipy,
scikit-learn, sympy, pandas, rdkit и biopython установлены только в venv
песочницы. Файл читается как текст (api/runtime/model_engine.py) и кладётся
в одноразовый рабочий каталог песочницы рядом с job.json и данными;
сгенерированный main.py загружает его и вызывает run(job).

Весь код обучения и расчёта — здесь, фиксированный и проверяемый тестами.
LLM его не пишет: один и тот же вход всегда даёт одно и то же число, а
точность модели считает платформа на опытах, не участвовавших в обучении.

Виды моделей (spec["kind"]):
  table    — по таблице опытов: линейная, квадратичная, гауссов процесс,
             градиентный бустинг; выбор по ошибке на скользящем контроле;
  formula  — по формуле с коэффициентами, подобранными по данным;
  kinetics — по дифференциальному уравнению скорости (кинетика отверждения,
             деструкции): прогноз по температурной программе;
  onnx     — импортированная модель, обученная вне платформы.

Операции (job["op"]): train, predict, profile, optimize.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import warnings

import numpy as np

warnings.filterwarnings("ignore")

MIN_ROWS = 8            # меньше — модель не строим, нужен план эксперимента
LOO_MAX = 30            # до стольких точек проверка «исключи по одной»
MAX_ROWS = 5000
MAX_COLUMNS = 80
MAX_LEVELS = 40         # уровней у категориального входа
PREDICT_MAX = 5000      # точек в одном вызове прогноза
SIMPLER_TOLERANCE = 0.03  # более сложный метод берём, только если он лучше на 3 %
METHOD_ORDER = ("linear", "quadratic", "gp", "gbm")
METHOD_TITLES = {
    "linear": "линейная регрессия",
    "quadratic": "квадратичная регрессия",
    "gp": "гауссов процесс",
    "gbm": "градиентный бустинг",
}

# «E» сознательно не константа Эйлера: в кинетике это энергия активации,
# и такое имя должно становиться коэффициентом (e — это exp(1)).
FUNCTIONS = {
    "exp", "log", "ln", "log10", "sqrt", "sin", "cos", "tan", "tanh", "sinh",
    "cosh", "abs", "Abs", "pi", "Min", "Max",
}
# Типовые начальные значения коэффициентов кинетики по их именам: без них
# предэкспонента, стартующая с единицы, не подбирается.
KINETIC_DEFAULTS = {
    "A": {"init": 1e8, "log": True, "min": 1e-10},
    "A1": {"init": 1e8, "log": True, "min": 1e-10},
    "A2": {"init": 1e8, "log": True, "min": 1e-10},
    "k0": {"init": 1e8, "log": True, "min": 1e-10},
    "Z": {"init": 1e8, "log": True, "min": 1e-10},
    "E": {"init": 60000.0, "min": 0.0, "log": False},
    "Ea": {"init": 60000.0, "min": 0.0, "log": False},
    "E1": {"init": 60000.0, "min": 0.0, "log": False},
    "E2": {"init": 60000.0, "min": 0.0, "log": False},
    "n": {"init": 1.0, "min": 0.0, "max": 6.0, "log": False},
    "m": {"init": 0.5, "min": 0.0, "max": 6.0, "log": False},
}
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
EXPR_CHARS_RE = re.compile(r"^[A-Za-z0-9_+\-*/^().,\s]+$")


class ModelError(Exception):
    """Понятная человеку причина, по которой операция не выполнена."""


# ---------------------------------------------------------------------------
# Вход


def run(job: dict) -> dict:
    op = job.get("op")
    try:
        if op == "train":
            return train(job)
        if op == "predict":
            return predict(job)
        if op == "profile":
            return profile(job)
        if op == "optimize":
            return optimize(job)
        raise ModelError(f"Неизвестная операция: {op}")
    except ModelError as exc:
        return {"ok": False, "error": str(exc)}


def _env() -> dict:
    env = {}
    for mod in ("numpy", "scipy", "sklearn", "sympy", "pandas"):
        try:
            env[mod] = __import__(mod).__version__
        except Exception:  # noqa: BLE001
            pass
    return env


def _r(value, digits: int = 6):
    """Число для JSON: без NaN/inf, с разумной точностью."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    if v == 0:
        return 0.0
    return float(f"{v:.{digits}g}")


# ---------------------------------------------------------------------------
# Чтение таблиц участника


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _sniff_sep(text: str) -> str:
    """Разделитель CSV: считаем «;», табуляцию и «,» вне кавычек в первых
    строках. «Температура, °C;Время, мин» — точка с запятой, а не запятая."""
    lines = [ln for ln in text.splitlines()[:6] if ln.strip()]
    if not lines:
        return ","
    counts = {";": 0, "\t": 0, ",": 0}
    for ln in lines:
        outside = re.sub(r'"[^"]*"', "", ln)
        for sep in counts:
            counts[sep] += outside.count(sep)
    if counts[";"] >= len(lines):
        return ";"
    if counts["\t"] >= len(lines):
        return "\t"
    return ","


def _read_one(path: str):
    import pandas as pd

    low = path.lower()
    if low.endswith((".xlsx", ".xlsm")):
        df = pd.read_excel(path, sheet_name=0)
    else:
        text = _decode(open(path, "rb").read())
        sep = "," if os.path.basename(low) == "train.csv" else _sniff_sep(text)
        df = pd.read_csv(io.StringIO(text), sep=sep, dtype=str, keep_default_na=False)
    df.columns = [str(c).strip() for c in df.columns]
    df = df.loc[:, [c for c in df.columns if c and not c.lower().startswith("unnamed")]]
    df = df.replace({"": np.nan})
    df = df.dropna(how="all")
    if df.shape[1] > MAX_COLUMNS:
        raise ModelError(f"В таблице больше {MAX_COLUMNS} столбцов — оставьте нужные")
    return df


def to_number(value):
    """Число из ячейки: «12,5», «1 200», «3.4e-2» → float; иначе nan."""
    if value is None:
        return np.nan
    if isinstance(value, (int, float, np.integer, np.floating)):
        v = float(value)
        return v if math.isfinite(v) else np.nan
    s = str(value).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    s = s.replace("−", "-").replace("–", "-")
    if not s:
        return np.nan
    try:
        return float(s)
    except ValueError:
        return np.nan


def read_tables(paths: list):
    """Одна таблица из одного или нескольких файлов (дозагрузка опытов):
    столбцы сопоставляются по названию."""
    import pandas as pd

    frames = []
    for p in paths:
        frames.append(_read_one(p))
    if not frames:
        raise ModelError("Нет данных для обучения")
    df = pd.concat(frames, ignore_index=True, sort=False)
    if len(df) > MAX_ROWS:
        raise ModelError(f"В таблице больше {MAX_ROWS} строк")
    return df.reset_index(drop=True)


def table_payload(df) -> dict:
    """Нормализованная таблица для хранения в версии модели."""
    columns = [str(c) for c in df.columns]
    numeric = {}
    for c in columns:
        vals = [to_number(v) for v in df[c].tolist()]
        filled = [v for v, raw in zip(vals, df[c].tolist()) if not _is_missing(raw)]
        numeric[c] = bool(filled) and all(math.isfinite(v) for v in filled)
    rows = []
    for _, rec in df.iterrows():
        row = []
        for c in columns:
            raw = rec[c]
            if _is_missing(raw):
                row.append(None)
            elif numeric[c]:
                row.append(_r(to_number(raw), 10))
            else:
                row.append(str(raw).strip())
        rows.append(row)
    return {"columns": columns, "rows": rows, "numeric": [numeric[c] for c in columns]}


def _is_missing(raw) -> bool:
    if raw is None:
        return True
    if isinstance(raw, float) and math.isnan(raw):
        return True
    return str(raw).strip() in ("", "nan", "NaN", "None")


def _column(df, name: str):
    if name in df.columns:
        return df[name]
    low = {str(c).strip().lower(): c for c in df.columns}
    key = str(name).strip().lower()
    if key in low:
        return df[low[key]]
    raise ModelError(f"В таблице нет столбца «{name}». Есть: " + ", ".join(map(str, df.columns)))


# ---------------------------------------------------------------------------
# Признаки: числа, категории, молекулы (SMILES), белки (последовательность)

SMILES_DESCRIPTORS = (
    ("MolWt", "молекулярная масса"), ("MolLogP", "logP"), ("TPSA", "TPSA"),
    ("NumHDonors", "доноры H"), ("NumHAcceptors", "акцепторы H"),
    ("NumRotatableBonds", "вращаемые связи"), ("RingCount", "циклы"),
    ("NumAromaticRings", "ароматические циклы"), ("FractionCSP3", "доля sp3"),
    ("HeavyAtomCount", "тяжёлые атомы"),
)
AMINO = "ACDEFGHIKLMNPQRSTVWY"
ESM_VOCAB = ["<cls>", "<pad>", "<eos>", "<unk>", "L", "A", "G", "V", "S", "E", "R", "T",
             "I", "D", "P", "K", "Q", "N", "F", "Y", "M", "H", "W", "C", "X", "B", "U",
             "Z", "O", ".", "-", "<null_1>", "<mask>"]


def _mol(smiles: str):
    from rdkit import Chem, RDLogger

    RDLogger.DisableLog("rdApp.*")
    mol = Chem.MolFromSmiles(str(smiles).strip()) if smiles else None
    return mol


def smiles_features(smiles: str):
    from rdkit.Chem import Descriptors

    mol = _mol(smiles)
    if mol is None:
        return None
    return [float(getattr(Descriptors, name)(mol)) for name, _ in SMILES_DESCRIPTORS]


def _fingerprint(smiles: str):
    from rdkit.Chem import rdFingerprintGenerator

    mol = _mol(smiles)
    if mol is None:
        return None
    gen = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=1024)
    return gen.GetFingerprint(mol)


def sequence_features(seq: str, esm_path: str | None = None):
    from Bio.SeqUtils.ProtParam import ProteinAnalysis

    s = re.sub(r"[^A-Za-z]", "", str(seq or "")).upper()
    clean = "".join(ch for ch in s if ch in AMINO)
    if len(clean) < 5:
        return None
    pa = ProteinAnalysis(clean)
    comp = pa.amino_acids_percent if hasattr(pa, "amino_acids_percent") else pa.get_amino_acids_percent()
    feats = [float(len(clean)), float(pa.molecular_weight()), float(pa.isoelectric_point()),
             float(pa.aromaticity()), float(pa.instability_index()), float(pa.gravy())]
    feats += [float(comp.get(a, 0.0)) for a in AMINO]
    if esm_path:
        feats += list(_esm_embedding(clean, esm_path))
    return feats


_ESM_SESSION = {}


def _esm_embedding(seq: str, path: str):
    import onnxruntime as ort

    sess = _ESM_SESSION.get(path)
    if sess is None:
        sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
        _ESM_SESSION[path] = sess
    index = {tok: i for i, tok in enumerate(ESM_VOCAB)}
    ids = [0] + [index.get(ch, 3) for ch in seq[:1022]] + [2]
    arr = np.array([ids], dtype=np.int64)
    feeds = {}
    for inp in sess.get_inputs():
        if "mask" in inp.name:
            feeds[inp.name] = np.ones_like(arr)
        else:
            feeds[inp.name] = arr
    hidden = sess.run(None, feeds)[0][0]          # (L+2, d)
    return hidden[1:-1].mean(axis=0).astype(float)


class Featurizer:
    """Превращает входы модели в числовую матрицу. Уровни категорий и
    описание признаков строятся по обучающим данным — при прогнозе
    модель пересобирается из тех же данных, поэтому всё детерминировано."""

    def __init__(self, inputs: list, records: list, esm_path: str | None = None):
        self.inputs = inputs
        self.esm_path = esm_path
        self.levels = {}
        self.names = []
        for inp in inputs:
            t = inp.get("type", "number")
            key = inp["name"]
            if t == "category":
                levels = sorted({str(r.get(key)).strip() for r in records if not _is_missing(r.get(key))})
                if len(levels) > MAX_LEVELS:
                    raise ModelError(f"У входа «{inp.get('title') or key}» больше {MAX_LEVELS} разных значений")
                self.levels[key] = levels
                self.names += [f"{key}={lv}" for lv in levels]
            elif t == "smiles":
                self.names += [f"{key}:{title}" for _, title in SMILES_DESCRIPTORS]
            elif t == "sequence":
                base = ["длина", "масса", "pI", "ароматичность", "нестабильность", "GRAVY"] + list(AMINO)
                self.names += [f"{key}:{b}" for b in base]
                if esm_path:
                    probe = sequence_features("ACDEFGHIKL", esm_path)
                    self.names += [f"{key}:esm{i}" for i in range(len(probe) - len(base))]
            else:
                self.names.append(key)

    def row(self, rec: dict):
        out = []
        for inp in self.inputs:
            t = inp.get("type", "number")
            key = inp["name"]
            val = rec.get(key)
            if t == "category":
                s = None if _is_missing(val) else str(val).strip()
                if s is None:
                    return None
                out += [1.0 if s == lv else 0.0 for lv in self.levels[key]]
            elif t == "smiles":
                f = smiles_features(val) if not _is_missing(val) else None
                if f is None:
                    return None
                out += f
            elif t == "sequence":
                f = sequence_features(val, self.esm_path) if not _is_missing(val) else None
                if f is None:
                    return None
                out += f
            else:
                v = to_number(val)
                if not math.isfinite(v):
                    return None
                out.append(v)
        return out

    def matrix(self, records: list):
        rows, keep = [], []
        for i, rec in enumerate(records):
            r = self.row(rec)
            if r is not None:
                rows.append(r)
                keep.append(i)
        X = np.array(rows, dtype=float) if rows else np.zeros((0, len(self.names)))
        return X, keep


def records_from_df(df, inputs: list, outputs: list) -> list:
    """Строки таблицы как словари {имя входа/выхода: значение}."""
    cols = {}
    for item in list(inputs) + list(outputs):
        cols[item["name"]] = _column(df, item.get("column") or item["name"])
    recs = []
    for i in range(len(df)):
        recs.append({name: col.iloc[i] for name, col in cols.items()})
    return recs


# ---------------------------------------------------------------------------
# Методы по таблице


def make_regressor(method: str, n_features: int, restarts: int = 2, theta=None):
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import PolynomialFeatures, StandardScaler

    if method == "linear":
        return make_pipeline(StandardScaler(), Ridge(alpha=1e-3))
    if method == "quadratic":
        return make_pipeline(StandardScaler(), PolynomialFeatures(2, include_bias=False), Ridge(alpha=1e-2))
    if method == "gp":
        from sklearn.gaussian_process import GaussianProcessRegressor
        from sklearn.gaussian_process.kernels import ConstantKernel, Matern, WhiteKernel

        kernel = (ConstantKernel(1.0, (1e-3, 1e3))
                  * Matern(length_scale=np.ones(max(n_features, 1)), length_scale_bounds=(1e-2, 1e3), nu=2.5)
                  + WhiteKernel(1e-2, (1e-6, 1e1)))
        if theta is not None:
            kernel = kernel.clone_with_theta(np.asarray(theta, dtype=float))
            gpr = GaussianProcessRegressor(kernel=kernel, normalize_y=True, optimizer=None)
        else:
            gpr = GaussianProcessRegressor(kernel=kernel, normalize_y=True,
                                           n_restarts_optimizer=restarts, random_state=0)
        return make_pipeline(StandardScaler(), gpr)
    if method == "gbm":
        return GradientBoostingRegressor(n_estimators=300, max_depth=3, learning_rate=0.05,
                                         subsample=0.8, random_state=0)
    raise ModelError(f"Неизвестный метод: {method}")


def _cv_splits(n: int):
    from sklearn.model_selection import KFold, LeaveOneOut

    if n <= LOO_MAX:
        return list(LeaveOneOut().split(np.arange(n))), "loo"
    return list(KFold(n_splits=5, shuffle=True, random_state=0).split(np.arange(n))), "kfold5"


def _cv_predict(fit_predict, X, y, splits):
    pred = np.full(len(y), np.nan)
    for tr, te in splits:
        pred[te] = fit_predict(X[tr], y[tr], X[te])
    return pred


def _metrics(y, pred, base_pred) -> dict:
    err = pred - y
    mae = float(np.mean(np.abs(err)))
    rmse = float(np.sqrt(np.mean(err ** 2)))
    base_mae = float(np.mean(np.abs(base_pred - y)))
    sst = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1.0 - float(np.sum(err ** 2)) / sst if sst > 0 else 0.0
    skill = 1.0 - mae / base_mae if base_mae > 0 else 0.0
    return {"mae": _r(mae), "rmse": _r(rmse), "r2": _r(r2, 4), "baseline_mae": _r(base_mae),
            "skill": _r(max(-1.0, min(1.0, skill)), 4)}


def _baseline_cv(y, splits):
    pred = np.full(len(y), np.nan)
    for tr, te in splits:
        pred[te] = np.mean(y[tr])
    return pred


def _table_fit_predict(method, n_features, restarts=0):
    def fp(Xtr, ytr, Xte):
        model = make_regressor(method, n_features, restarts=restarts)
        model.fit(Xtr, ytr)
        return model.predict(Xte)
    return fp


def _candidates(n: int, p: int, forced: str | None) -> list:
    if forced and forced != "auto":
        if forced not in METHOD_ORDER:
            raise ModelError(f"Метод «{forced}» не поддерживается: " + ", ".join(METHOD_ORDER))
        return [forced]
    cands = ["linear"]
    if n >= 15 and p <= 6:
        cands.append("quadratic")
    if n >= 10:
        cands.append("gp")
    if n >= 40:
        cands.append("gbm")
    return cands


def _learning_curve(fit_predict, X, y, full_mae: float) -> list:
    n = len(y)
    sizes = sorted({int(math.ceil(f * n)) for f in (0.5, 0.65, 0.8)})
    sizes = [m for m in sizes if 6 <= m <= n - 2]
    rng = np.random.RandomState(0)
    curve = []
    for m in sizes:
        maes = []
        for _ in range(8):
            idx = rng.permutation(n)
            tr, te = idx[:m], idx[m:]
            try:
                pred = fit_predict(X[tr], y[tr], X[te])
            except Exception:  # noqa: BLE001 — неудачная подвыборка не роняет обучение
                continue
            maes.append(float(np.mean(np.abs(pred - y[te]))))
        if maes:
            curve.append({"n": int(m), "mae": _r(np.mean(maes))})
    curve.append({"n": int(n), "mae": _r(full_mae)})
    return curve


def _curve_verdict(curve: list) -> str:
    if len(curve) < 3:
        return "мало точек, чтобы судить, помогут ли новые опыты"
    first, last = curve[0]["mae"] or 0, curve[-1]["mae"] or 0
    prev = curve[-2]["mae"] or 0
    if first <= 0:
        return ""
    total_drop = (first - last) / first
    last_drop = (prev - last) / prev if prev > 0 else 0
    if last_drop > 0.05 and total_drop > 0.1:
        return "ошибка ещё падает с каждым опытом — новые опыты сделают модель точнее"
    if total_drop > 0.1:
        return "ошибка почти вышла на плато — больше пользы даст не число опытов, а новые факторы или точнее измерения"
    return "ошибка слабо зависит от числа опытов — проверьте, те ли факторы варьировались"


def _effects(predict_fn, inputs: list, records: list) -> list:
    """Как меняется прогноз, если сдвинуть один вход с 25-го на 75-й
    процентиль при остальных на медиане (категории — по уровням)."""
    base = {}
    for inp in inputs:
        key = inp["name"]
        vals = [r.get(key) for r in records if not _is_missing(r.get(key))]
        if inp.get("type") in ("category", "smiles", "sequence"):
            if vals:
                counts = {}
                for v in vals:
                    counts[str(v).strip()] = counts.get(str(v).strip(), 0) + 1
                base[key] = max(counts, key=counts.get)
        else:
            nums = np.array([to_number(v) for v in vals], dtype=float)
            nums = nums[np.isfinite(nums)]
            if len(nums):
                base[key] = float(np.median(nums))
    effects = []
    for inp in inputs:
        key = inp["name"]
        t = inp.get("type", "number")
        if t in ("smiles", "sequence"):
            continue
        if t == "category":
            levels = sorted({str(r.get(key)).strip() for r in records if not _is_missing(r.get(key))})
            if len(levels) < 2:
                continue
            recs = [dict(base, **{key: lv}) for lv in levels]
            preds = predict_fn(recs)
            if preds is None:
                continue
            hi, lo = int(np.argmax(preds)), int(np.argmin(preds))
            effects.append({"input": key, "kind": "category", "low": levels[lo], "high": levels[hi],
                            "delta": _r(preds[hi] - preds[lo])})
        else:
            nums = np.array([to_number(r.get(key)) for r in records], dtype=float)
            nums = nums[np.isfinite(nums)]
            if len(nums) < 2:
                continue
            lo, hi = float(np.percentile(nums, 25)), float(np.percentile(nums, 75))
            if hi == lo:
                lo, hi = float(nums.min()), float(nums.max())
            if hi == lo:
                continue
            preds = predict_fn([dict(base, **{key: lo}), dict(base, **{key: hi})])
            if preds is None:
                continue
            effects.append({"input": key, "kind": "number", "low": _r(lo), "high": _r(hi),
                            "delta": _r(preds[1] - preds[0])})
    total = sum(abs(e["delta"] or 0) for e in effects) or 1.0
    for e in effects:
        e["share"] = _r(abs(e["delta"] or 0) / total, 3)
    effects.sort(key=lambda e: -abs(e["delta"] or 0))
    return effects


def _domain(inputs: list, records: list) -> dict:
    dom = {}
    for inp in inputs:
        key = inp["name"]
        t = inp.get("type", "number")
        vals = [r.get(key) for r in records if not _is_missing(r.get(key))]
        if t == "category":
            dom[key] = {"type": "category", "levels": sorted({str(v).strip() for v in vals})}
        elif t in ("smiles", "sequence"):
            dom[key] = {"type": t, "count": len(vals)}
        else:
            nums = np.array([to_number(v) for v in vals], dtype=float)
            nums = nums[np.isfinite(nums)]
            if len(nums):
                dom[key] = {"type": "number", "min": _r(nums.min()), "max": _r(nums.max()),
                            "median": _r(np.median(nums))}
    return dom


def _nn_threshold(X):
    """95-й процентиль расстояния до ближайшего опыта в стандартизованных
    признаках — «насколько далеко от опытов» ещё нормально."""
    if len(X) < 3:
        return None, None, None
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd[sd == 0] = 1.0
    Z = (X - mu) / sd
    d = np.sqrt(((Z[:, None, :] - Z[None, :, :]) ** 2).sum(axis=2))
    np.fill_diagonal(d, np.inf)
    nn = d.min(axis=1)
    return float(np.percentile(nn, 95)), mu, sd


def check_domain(inputs: list, domain: dict, rec: dict, *, nn=None, train_fps=None) -> dict:
    """Статус прогноза: inside / edge / outside и пояснения."""
    status, notes = "inside", []

    def worse(new):
        nonlocal status
        order = {"inside": 0, "edge": 1, "outside": 2}
        if order[new] > order[status]:
            status = new

    for inp in inputs:
        key = inp["name"]
        d = domain.get(key) or {}
        val = rec.get(key)
        title = inp.get("title") or key
        if d.get("type") == "category":
            s = None if _is_missing(val) else str(val).strip()
            if s not in (d.get("levels") or []):
                worse("outside")
                notes.append(f"«{title}» = {s}: такого значения не было в опытах")
        elif d.get("type") == "number":
            v = to_number(val)
            lo, hi = d.get("min"), d.get("max")
            if not math.isfinite(v) or lo is None or hi is None:
                continue
            span = (hi - lo) or abs(hi) or 1.0
            if v < lo or v > hi:
                gap = (lo - v) if v < lo else (v - hi)
                worse("edge" if gap <= 0.1 * span else "outside")
                notes.append(f"«{title}» = {_r(v)} вне диапазона опытов {lo}–{hi}")
        elif d.get("type") == "smiles" and train_fps:
            fp = _fingerprint(val) if not _is_missing(val) else None
            if fp is None:
                worse("outside")
                notes.append(f"«{title}»: структура не распознана")
            else:
                from rdkit import DataStructs

                sim = max(DataStructs.BulkTanimotoSimilarity(fp, train_fps))
                if sim < 0.3:
                    worse("outside")
                    notes.append(f"«{title}»: молекула не похожа на обучающие (сходство {sim:.2f})")
                elif sim < 0.5:
                    worse("edge")
                    notes.append(f"«{title}»: молекула умеренно похожа на обучающие (сходство {sim:.2f})")
    if nn is not None and status == "inside":
        thr, x, mu, sd, Z = nn
        if thr and x is not None:
            z = (x - mu) / sd
            dist = float(np.sqrt(((Z - z) ** 2).sum(axis=1)).min())
            if dist > 2 * thr:
                worse("edge")
                notes.append("сочетание условий далеко от поставленных опытов")
    return {"status": status, "notes": notes}


# ---------------------------------------------------------------------------
# Формулы


def _safe_expr(expr: str, known: set) -> tuple:
    """Проверка формулы до sympy: только разрешённые символы и имена."""
    text = str(expr or "").strip()
    if not text:
        raise ModelError("Формула пустая")
    if "=" in text:
        text = text.split("=", 1)[1].strip()
    if len(text) > 400:
        raise ModelError("Формула длиннее 400 символов")
    if not EXPR_CHARS_RE.match(text) or "__" in text:
        raise ModelError("В формуле допустимы буквы, цифры, + - * / ^ ( ) и точка")
    names = set(IDENT_RE.findall(text))
    params = sorted(n for n in names if n not in known and n not in FUNCTIONS)
    for name in params:
        if len(name) > 24:
            raise ModelError(f"Слишком длинное имя «{name}»")
    return text.replace("^", "**"), params


def compile_expr(expr: str, variables: list, constants: dict):
    """(функция f(theta_dict, var_arrays) , список параметров, sympy-выражение)."""
    import sympy as sp

    known = set(variables) | set(constants)
    text, params = _safe_expr(expr, known)
    local = {name: sp.Symbol(name) for name in list(variables) + params + list(constants)}
    local.update({
        "exp": sp.exp, "log": sp.log, "ln": sp.log, "log10": lambda x: sp.log(x, 10),
        "sqrt": sp.sqrt, "sin": sp.sin, "cos": sp.cos, "tan": sp.tan, "tanh": sp.tanh,
        "sinh": sp.sinh, "cosh": sp.cosh, "abs": sp.Abs, "Abs": sp.Abs, "pi": sp.pi,
        "Min": sp.Min, "Max": sp.Max,
    })
    try:
        parsed = sp.sympify(text, locals=local)
    except Exception as exc:  # noqa: BLE001
        raise ModelError(f"Формула не разбирается: {exc}")
    parsed = parsed.subs({sp.Symbol(k): float(v) for k, v in constants.items()})
    order = list(params) + list(variables)
    fn = sp.lambdify([sp.Symbol(n) for n in order], parsed, modules="numpy")

    def f(theta, cols):
        args = [theta[p] for p in params] + [cols[v] for v in variables]
        with np.errstate(all="ignore"):
            out = fn(*args)
        return np.broadcast_to(np.asarray(out, dtype=float), np.shape(cols[variables[0]]) if variables else ()).astype(float)

    return f, params, parsed


def _fit_params(residual_fn, theta0: dict, bounds: dict, restarts: int = 4, max_nfev: int = 4000):
    from scipy.optimize import least_squares

    names = list(theta0)
    lo = np.array([bounds.get(n, (-np.inf, np.inf))[0] for n in names], dtype=float)
    hi = np.array([bounds.get(n, (-np.inf, np.inf))[1] for n in names], dtype=float)
    x0 = np.array([theta0[n] for n in names], dtype=float)

    def inside(x):
        # least_squares требует строго внутренней стартовой точки
        span = np.where(np.isfinite(hi - lo), (hi - lo) * 1e-6, 1e-9)
        return np.clip(x, np.where(np.isfinite(lo), lo + span, x), np.where(np.isfinite(hi), hi - span, x))

    def fun(x):
        r = residual_fn(dict(zip(names, x)))
        r = np.where(np.isfinite(r), r, 1e6)
        return r

    best = None
    rng = np.random.RandomState(0)
    step = np.maximum(0.3 * np.abs(x0), 0.3)
    starts = [x0] + [x0 + rng.normal(0, 1, size=len(x0)) * step for _ in range(restarts)]
    for s in starts:
        try:
            res = least_squares(fun, inside(s), bounds=(lo, hi), x_scale="jac", max_nfev=max_nfev)
        except Exception:  # noqa: BLE001
            continue
        if best is None or res.cost < best.cost:
            best = res
    if best is None:
        raise ModelError("Коэффициенты не подбираются: проверьте формулу и начальные значения")
    return dict(zip(names, best.x)), best


def _param_cov(res, n: int):
    p = len(res.x)
    dof = max(n - p, 1)
    s2 = 2 * res.cost / dof
    J = res.jac
    try:
        cov = np.linalg.pinv(J.T @ J) * s2
    except np.linalg.LinAlgError:
        cov = np.full((p, p), np.nan)
    return cov, s2, dof


def _t975(dof: int) -> float:
    try:
        from scipy.stats import t

        return float(t.ppf(0.975, dof))
    except Exception:  # noqa: BLE001
        return 1.96


class FormulaModel:
    """y = f(входы; коэффициенты), коэффициенты — по методу наименьших квадратов."""

    def __init__(self, spec: dict):
        fm = spec.get("formula") or {}
        self.inputs = [i for i in spec["inputs"]]
        self.symbols = [i.get("symbol") or i["name"] for i in self.inputs]
        self.constants = {str(k): float(v) for k, v in (fm.get("constants") or {}).items()}
        self.f, self.params, self.expr = compile_expr(fm.get("expression", ""), self.symbols, self.constants)
        if not self.params:
            raise ModelError("В формуле нет коэффициентов для подбора: объявите хотя бы один")
        self.log_scale = fm.get("fit_scale") == "log"
        pinfo = fm.get("params") or {}
        self.theta0 = {p: float((pinfo.get(p) or {}).get("init", 1.0)) for p in self.params}
        self.bounds = {}
        for p in self.params:
            info = pinfo.get(p) or {}
            lo = info.get("min")
            hi = info.get("max")
            self.bounds[p] = (-np.inf if lo is None else float(lo), np.inf if hi is None else float(hi))
        self.theta = None
        self.cov = None
        self.s2 = None

    def cols(self, X):
        return {s: X[:, i] for i, s in enumerate(self.symbols)}

    def _res(self, X, y):
        cols = self.cols(X)
        if self.log_scale:
            ly = np.log(y)
            return lambda th: np.log(np.maximum(self.f(th, cols), 1e-300)) - ly
        return lambda th: self.f(th, cols) - y

    def fit(self, X, y, theta0=None):
        if self.log_scale and np.any(y <= 0):
            raise ModelError("Подгонка в логарифмах требует положительных значений отклика")
        th, res = _fit_params(self._res(X, y), theta0 or self.theta0, self.bounds,
                              restarts=4 if theta0 is None else 1)
        self.theta = th
        self.cov, self.s2, self.dof = _param_cov(res, len(y))
        return self

    def predict(self, X, theta=None):
        return self.f(theta or self.theta, self.cols(X))

    def predict_std(self, X):
        """Погрешность прогноза: неопределённость коэффициентов (дельта-метод)
        плюс разброс точек вокруг кривой."""
        base = self.predict(X)
        names = list(self.theta)
        grads = []
        for p in names:
            h = 1e-6 * max(abs(self.theta[p]), 1e-3)
            th = dict(self.theta)
            th[p] += h
            val = self.predict(X, th)
            if self.log_scale:
                grads.append((np.log(np.maximum(val, 1e-300)) - np.log(np.maximum(base, 1e-300))) / h)
            else:
                grads.append((val - base) / h)
        G = np.array(grads).T
        var = np.einsum("ij,jk,ik->i", G, np.nan_to_num(self.cov), G) + self.s2
        sd = np.sqrt(np.maximum(var, 0))
        if self.log_scale:
            sd = np.abs(base) * sd
        return base, sd

    def equation(self) -> str:
        import sympy as sp

        subs = {sp.Symbol(k): sp.Float(float(f"{v:.4g}")) for k, v in self.theta.items()}
        return sp.sstr(self.expr.subs(subs).evalf(4)).replace("**", "^")


# ---------------------------------------------------------------------------
# Кинетика: dy/dt = rate(y, T; коэффициенты)


class KineticsModel:
    def __init__(self, spec: dict):
        kn = spec.get("kinetics") or {}
        self.state = str(kn.get("state") or "a")
        self.temp_symbol = "T"
        self.celsius = str(kn.get("temperature_unit") or "°C").strip().upper() not in ("K", "К")
        self.bounded = bool(kn.get("bounded", True))
        self.constants = {"R": 8.314}
        self.constants.update({str(k): float(v) for k, v in (kn.get("constants") or {}).items()})
        variables = [self.state, self.temp_symbol]
        self.f, self.params, self.expr = compile_expr(kn.get("rate", ""), variables, self.constants)
        if not self.params:
            raise ModelError("В уравнении скорости нет коэффициентов для подбора")
        pinfo = kn.get("params") or {}
        self.theta0, self.bounds, self.logp = {}, {}, set()
        for p in self.params:
            info = dict(KINETIC_DEFAULTS.get(p) or {})
            info.update({k: v for k, v in (pinfo.get(p) or {}).items() if v is not None})
            init = float(info.get("init", 1.0))
            is_log = bool(info.get("log", abs(init) >= 1e3))
            lo, hi = info.get("min"), info.get("max")
            if is_log:
                self.logp.add(p)
                self.theta0[p] = math.log10(max(init, 1e-300))
                self.bounds[p] = (-np.inf if lo in (None, 0) else math.log10(float(lo)),
                                  np.inf if hi is None else math.log10(float(hi)))
            else:
                self.theta0[p] = init
                self.bounds[p] = (-np.inf if lo is None else float(lo), np.inf if hi is None else float(hi))
        self.theta = None
        self.cov = None

    def real(self, th: dict) -> dict:
        return {p: (10 ** v if p in self.logp else v) for p, v in th.items()}

    def rate(self, th_real: dict, y, T):
        yv = np.clip(y, 0.0, 1.0) if self.bounded else y
        Tk = T + 273.15 if self.celsius else T
        return self.f(th_real, {self.state: np.atleast_1d(yv), self.temp_symbol: np.atleast_1d(Tk)})

    def simulate(self, th: dict, t, T, y0: float, t_eval=None):
        from scipy.integrate import solve_ivp

        th_real = self.real(th)
        t = np.asarray(t, dtype=float)
        T = np.asarray(T, dtype=float)
        t_eval = t if t_eval is None else np.asarray(t_eval, dtype=float)

        def rhs(tt, yy):
            Tt = np.interp(tt, t, T)
            v = self.rate(th_real, yy[0], Tt)
            v = float(np.nan_to_num(v[0] if np.ndim(v) else v, nan=0.0, posinf=1e6, neginf=-1e6))
            return [v]

        try:
            sol = solve_ivp(rhs, (t[0], t[-1]), [y0], t_eval=t_eval, method="LSODA",
                            rtol=1e-5, atol=1e-8, max_step=max((t[-1] - t[0]) / 80.0, 1e-9))
        except Exception:  # noqa: BLE001 — неудачные коэффициенты дают «плохую» точку, а не падение
            return np.full(len(t_eval), np.nan)
        if not sol.success or sol.y.shape[1] != len(t_eval):
            return np.full(len(t_eval), np.nan)
        y = sol.y[0]
        return np.clip(y, 0.0, 1.0) if self.bounded else y

    def residuals(self, runs):
        def res(th):
            parts = []
            for r in runs:
                sim = self.simulate(th, r["t"], r["T"], r["y0"])
                parts.append(np.where(np.isfinite(sim), sim - r["y"], 1.0))
            return np.concatenate(parts)
        return res

    def _rate_points(self, runs):
        """Скорости dy/dt по сглаженным кривым: точки для быстрой подгонки
        по скоростям (без интегрирования)."""
        ys, Ts, rs, ws = [], [], [], []
        for r in runs:
            t, y, T = r["t"], r["y"], r["T"]
            k = max(3, min(9, len(y) // 15) | 1)
            pad = k // 2
            ys_ = np.convolve(np.pad(y, pad, mode="edge"), np.ones(k) / k, mode="valid")
            rate = np.gradient(ys_, t)
            mask = (ys_ > 0.03) & (ys_ < 0.97) & (rate > 0) if self.bounded else np.isfinite(rate)
            if mask.sum() < 3:
                continue
            scale = float(np.max(rate[mask])) or 1.0
            ys.append(ys_[mask])
            Ts.append(T[mask])
            rs.append(rate[mask])
            ws.append(np.full(mask.sum(), 1.0 / scale))
        if not ys:
            return None
        return np.concatenate(ys), np.concatenate(Ts), np.concatenate(rs), np.concatenate(ws)

    def _arrhenius_guess(self, pts) -> dict:
        """Для типовой записи A·exp(-E/(R·T))·(1-a)^n — начальное приближение
        линейной регрессией ln r = ln A − E/(R·T) + n·ln(1−a)."""
        if not {"A", "E"} <= set(self.params) or pts is None:
            return {}
        y, T, r, _ = pts
        Tk = T + 273.15 if self.celsius else T
        R = self.constants.get("R", 8.314)
        cols = [np.ones_like(r), -1.0 / (R * Tk)]
        if "n" in self.params:
            cols.append(np.log(np.clip(1 - y, 1e-6, 1)))
        M = np.vstack(cols).T
        try:
            coef, *_ = np.linalg.lstsq(M, np.log(r), rcond=None)
        except np.linalg.LinAlgError:
            return {}
        guess = {"A": coef[0] / math.log(10) if "A" in self.logp else math.exp(coef[0]), "E": coef[1]}
        if "n" in self.params:
            guess["n"] = coef[2]
        out = {}
        for k, v in guess.items():
            if not math.isfinite(v):
                continue
            lo, hi = self.bounds.get(k, (-np.inf, np.inf))
            out[k] = float(min(max(v, lo if math.isfinite(lo) else v), hi if math.isfinite(hi) else v))
        return out

    def _diff_fit(self, runs) -> dict:
        pts = self._rate_points(runs)
        start = dict(self.theta0)
        start.update(self._arrhenius_guess(pts))
        if pts is None:
            return start
        y, T, r, w = pts

        def res(th):
            model = self.rate(self.real(th), y, T)
            return (model - r) * w

        try:
            th, _ = _fit_params(res, start, self.bounds, restarts=6, max_nfev=3000)
        except ModelError:
            return start
        return th

    def fit(self, runs, theta0=None, integral_nfev: int = 25):
        start = theta0 or self._diff_fit(runs)
        th, res = _fit_params(self.residuals(runs), start, self.bounds, restarts=0, max_nfev=integral_nfev)
        self.theta = th
        n = sum(len(r["y"]) for r in runs)
        self.cov, self.s2, self.dof = _param_cov(res, n)
        return self


def _kinetics_runs(df, spec: dict) -> list:
    kn = spec.get("kinetics") or {}
    cols = kn.get("columns") or {}
    run_c = _column(df, cols.get("run") or "run")
    t_c = _column(df, cols.get("t") or "t")
    T_c = _column(df, cols.get("T") or "T")
    y_c = _column(df, cols.get("y") or "y")
    groups = {}
    for i in range(len(df)):
        key = str(run_c.iloc[i]).strip()
        t, T, y = to_number(t_c.iloc[i]), to_number(T_c.iloc[i]), to_number(y_c.iloc[i])
        if key in ("", "nan") or not all(math.isfinite(v) for v in (t, T, y)):
            continue
        groups.setdefault(key, []).append((t, T, y))
    runs = []
    for key, pts in groups.items():
        pts.sort()
        if len(pts) < 5:
            continue
        arr = np.array(pts, dtype=float)
        # повторяющиеся моменты времени решатель не примет
        keep = np.concatenate([[True], np.diff(arr[:, 0]) > 0])
        arr = arr[keep]
        runs.append({"run": key, "t": arr[:, 0], "T": arr[:, 1], "y": arr[:, 2], "y0": float(arr[0, 2])})
    if not runs:
        raise ModelError("Нет ни одного опыта с 5 и более точками (время, температура, степень превращения)")
    return runs


def program_profile(program: list, T0: float, points: int = 300):
    """Температурная программа → (t, T). Сегменты: {"kind": "ramp", "to": 180,
    "rate": 5} — нагрев со скоростью в градусах на единицу времени;
    {"kind": "hold", "time": 60} — выдержка."""
    t_nodes, T_nodes = [0.0], [float(T0)]
    for seg in program or []:
        kind = str(seg.get("kind") or "hold")
        if kind == "ramp":
            to = float(seg.get("to"))
            rate = abs(float(seg.get("rate") or 0))
            if rate <= 0:
                raise ModelError("У нагрева нужна скорость больше нуля")
            dt = abs(to - T_nodes[-1]) / rate
            t_nodes.append(t_nodes[-1] + dt)
            T_nodes.append(to)
        else:
            dt = float(seg.get("time") or 0)
            if dt <= 0:
                raise ModelError("У выдержки нужно время больше нуля")
            t_nodes.append(t_nodes[-1] + dt)
            T_nodes.append(T_nodes[-1])
    if len(t_nodes) < 2:
        raise ModelError("Температурная программа пустая: добавьте нагрев или выдержку")
    t = np.linspace(0.0, t_nodes[-1], points)
    return t, np.interp(t, t_nodes, T_nodes), np.array(t_nodes), np.array(T_nodes)


# ---------------------------------------------------------------------------
# Обучение


def _outputs_values(records, out_name):
    return np.array([to_number(r.get(out_name)) for r in records], dtype=float)


def _controls(spec: dict, predict_fn) -> list:
    out = []
    for c in (spec.get("controls") or [])[:20]:
        inputs = c.get("inputs") or {}
        expected = c.get("expected") or {}
        tol = c.get("tolerance") or {}
        preds = predict_fn([inputs])
        if preds is None:
            continue
        for name, value in expected.items():
            if name not in preds:
                continue
            got = preds[name][0]
            e = to_number(value)
            t = to_number(tol.get(name)) if tol.get(name) is not None else None
            ok = bool(t is not None and math.isfinite(t) and abs(got - e) <= t) if math.isfinite(e) else False
            out.append({"inputs": inputs, "output": name, "expected": _r(e), "got": _r(got),
                        "tolerance": _r(t) if t is not None else None, "passed": ok})
    return out


def train(job: dict) -> dict:
    spec = job["spec"]
    kind = spec.get("kind") or "table"
    files = job.get("data_files") or []
    if kind == "onnx" and not files:
        body = _train_onnx(None, spec, job)
        body.update({"ok": True, "table": {"columns": [], "rows": [], "numeric": []}, "env": _env(),
                     "excluded_rows": []})
        return body
    df = read_tables(files)
    excluded = {int(e.get("row")) for e in (spec.get("excluded") or []) if str(e.get("row", "")).lstrip("-").isdigit()}
    table = table_payload(df)
    if excluded:
        df = df.drop(index=[i for i in excluded if 0 <= i < len(df)]).reset_index(drop=True)
    if kind == "table":
        body = _train_table(df, spec, job)
    elif kind == "formula":
        body = _train_formula(df, spec)
    elif kind == "kinetics":
        body = _train_kinetics(df, spec)
    elif kind == "onnx":
        body = _train_onnx(df, spec, job)
    else:
        raise ModelError(f"Неизвестный вид модели: {kind}")
    body.update({"ok": True, "table": table, "env": _env(), "excluded_rows": sorted(excluded)})
    return body


def _threshold_check(spec: dict, per_output: dict):
    th = spec.get("threshold") or {}
    name = th.get("output")
    limit = to_number(th.get("max"))
    metric = th.get("metric") or "mae"
    if not name or not math.isfinite(limit) or name not in per_output:
        return None
    value = per_output[name].get(metric)
    if value is None:
        return None
    return {"output": name, "metric": metric, "max": _r(limit), "value": value,
            "passed": bool(value <= limit)}


def _train_table(df, spec: dict, job: dict) -> dict:
    inputs, outputs = spec["inputs"], spec["outputs"]
    if not inputs or not outputs:
        raise ModelError("Нужен хотя бы один вход и один выход")
    esm = job.get("esm_path")
    records = records_from_df(df, inputs, outputs)
    fz = Featurizer(inputs, records, esm_path=esm)
    X_all, keep = fz.matrix(records)
    per_output, fits, parity, curves, warnings_ = {}, {}, {}, {}, []
    dropped_inputs = len(records) - len(keep)
    if dropped_inputs:
        warnings_.append(f"{dropped_inputs} строк без полного набора входов не участвовали в обучении")
    candidates_log = {}
    effects = {}
    finals = {}
    for out in outputs:
        y_all = _outputs_values([records[i] for i in keep], out["name"])
        mask = np.isfinite(y_all)
        X, y = X_all[mask], y_all[mask]
        n, p = X.shape
        if n < MIN_ROWS:
            raise ModelError(
                f"Для модели «{out.get('title') or out['name']}» нужно минимум {MIN_ROWS} опытов с "
                f"заполненными входами и выходом, сейчас {n}. Начните с плана эксперимента.")
        if np.ptp(y) == 0:
            raise ModelError(f"Выход «{out.get('title') or out['name']}» одинаков во всех опытах — предсказывать нечего")
        splits, scheme = _cv_splits(n)
        base_pred = _baseline_cv(y, splits)
        results = []
        for method in _candidates(n, p, spec.get("method")):
            try:
                pred = _cv_predict(_table_fit_predict(method, p), X, y, splits)
            except Exception as exc:  # noqa: BLE001
                results.append({"method": method, "error": str(exc)[:200]})
                continue
            m = _metrics(y, pred, base_pred)
            results.append({"method": method, "mae": m["mae"], "skill": m["skill"], "_pred": pred, "_m": m})
        ok = [r for r in results if "_m" in r]
        if not ok:
            raise ModelError("Ни один метод не обучился на этих данных")
        ok.sort(key=lambda r: METHOD_ORDER.index(r["method"]))
        best = min(ok, key=lambda r: r["mae"])
        for r in ok:  # более простой метод, если он почти так же хорош
            if r["mae"] <= best["mae"] * (1 + SIMPLER_TOLERANCE):
                best = r
                break
        method = best["method"]
        final = make_regressor(method, p, restarts=2)
        final.fit(X, y)
        finals[out["name"]] = final
        fit = {"method": method, "n": int(n), "features": fz.names}
        if method == "gp":
            gpr = final.steps[-1][1]
            fit["theta"] = [float(v) for v in gpr.kernel_.theta]
        sd_const = float(best["_m"]["rmse"])
        fit["sigma"] = _r(sd_const)
        fits[out["name"]] = fit
        per_output[out["name"]] = dict(best["_m"], method=method, n=int(n), scheme=scheme)
        parity[out["name"]] = [[_r(a), _r(b)] for a, b in zip(y, best["_pred"])]
        curves[out["name"]] = _learning_curve(_table_fit_predict(method, p), X, y, best["mae"])
        per_output[out["name"]]["curve_verdict"] = _curve_verdict(curves[out["name"]])
        candidates_log[out["name"]] = [{k: v for k, v in r.items() if not k.startswith("_")} for r in results]

        def pf(recs, _final=final):
            Xn, kk = fz.matrix(recs)
            if len(kk) != len(recs):
                return None
            return _final.predict(Xn)

        effects[out["name"]] = _effects(pf, inputs, [records[i] for i in keep])

    def predict_fn(recs):
        Xn, kk = fz.matrix(recs)
        if len(kk) != len(recs):
            return None
        return {name: model.predict(Xn) for name, model in finals.items()}

    return {
        "fit": {"outputs": fits, "levels": fz.levels},
        "validation": {"per_output": per_output, "parity": parity, "learning_curve": curves,
                       "candidates": candidates_log, "effects": effects},
        "domain": _domain(inputs, [records[i] for i in keep]),
        "threshold_check": _threshold_check(spec, per_output),
        "controls": _controls(spec, predict_fn),
        "warnings": warnings_,
        "n_rows": len(keep),
    }


def _formula_xy(df, spec):
    inputs = spec["inputs"]
    out = spec["outputs"][0]
    records = records_from_df(df, inputs, [out])
    X, y, keep = [], [], []
    for i, r in enumerate(records):
        xs = [to_number(r.get(inp["name"])) for inp in inputs]
        yv = to_number(r.get(out["name"]))
        if all(math.isfinite(v) for v in xs) and math.isfinite(yv):
            X.append(xs)
            y.append(yv)
            keep.append(i)
    return np.array(X, dtype=float), np.array(y, dtype=float), [records[i] for i in keep]


def _train_formula(df, spec: dict) -> dict:
    if len(spec.get("outputs") or []) != 1:
        raise ModelError("У модели по формуле ровно один выход — левая часть формулы")
    for inp in spec["inputs"]:
        if inp.get("type", "number") != "number":
            raise ModelError("В модели по формуле все входы — числа")
    X, y, recs = _formula_xy(df, spec)
    n = len(y)
    probe = FormulaModel(spec)
    p = len(probe.params)
    need = max(MIN_ROWS, p + 3)
    if n < need:
        raise ModelError(f"Для формулы с {p} коэффициентами нужно минимум {need} точек, сейчас {n}")
    model = FormulaModel(spec).fit(X, y)
    splits, scheme = _cv_splits(n)

    def fp(Xtr, ytr, Xte):
        m = FormulaModel(spec)
        m.fit(Xtr, ytr, theta0=dict(model.theta))
        return m.predict(Xte)

    pred = _cv_predict(fp, X, y, splits)
    base = _baseline_cv(y, splits)
    m = _metrics(y, pred, base)
    out = spec["outputs"][0]["name"]
    t = _t975(model.dof)
    params = []
    for i, name in enumerate(model.params):
        sd = math.sqrt(max(model.cov[i, i], 0)) if np.isfinite(model.cov[i, i]) else None
        params.append({"name": name, "value": _r(model.theta[name]),
                       "ci95": _r(t * sd) if sd is not None else None})
    curve = _learning_curve(fp, X, y, m["mae"])
    m.update(method="formula", n=int(n), scheme=scheme, curve_verdict=_curve_verdict(curve))

    def pfn(recs_):
        Xn = np.array([[to_number(r.get(inp["name"])) for inp in spec["inputs"]] for r in recs_], dtype=float)
        if not np.all(np.isfinite(Xn)):
            return None
        return model.predict(Xn)

    def predict_fn(recs_):
        v = pfn(recs_)
        return None if v is None else {out: v}

    return {
        "fit": {"outputs": {out: {"method": "formula", "n": int(n), "theta": {k: float(v) for k, v in model.theta.items()},
                                  "cov": np.nan_to_num(model.cov).tolist(), "s2": float(model.s2),
                                  "dof": int(model.dof), "equation": model.equation(), "params": params,
                                  "sigma": _r(m["rmse"])}}},
        "validation": {"per_output": {out: m}, "parity": {out: [[_r(a), _r(b)] for a, b in zip(y, pred)]},
                       "learning_curve": {out: curve}, "candidates": {},
                       "effects": {out: _effects(pfn, spec["inputs"], recs)}},
        "domain": _domain(spec["inputs"], recs),
        "threshold_check": _threshold_check(spec, {out: m}),
        "controls": _controls(spec, predict_fn),
        "warnings": [],
        "n_rows": int(n),
    }


def _train_kinetics(df, spec: dict) -> dict:
    runs = _kinetics_runs(df, spec)
    model = KineticsModel(spec).fit(runs)
    out = (spec.get("outputs") or [{"name": "y"}])[0]["name"]
    per_run, pairs = [], []
    all_err, base_err = [], []
    if len(runs) >= 2:
        for i, r in enumerate(runs):
            rest = [q for j, q in enumerate(runs) if j != i]
            m = KineticsModel(spec).fit(rest, theta0=dict(model.theta), integral_nfev=8)
            sim = m.simulate(m.theta, r["t"], r["T"], r["y0"])
            err = np.abs(np.where(np.isfinite(sim), sim, 1.0) - r["y"])
            mean_rest = float(np.mean(np.concatenate([q["y"] for q in rest])))
            per_run.append({"run": r["run"], "mae": _r(np.mean(err)), "points": int(len(r["y"]))})
            all_err.append(err)
            base_err.append(np.abs(mean_rest - r["y"]))
            step = max(1, len(r["y"]) // 40)
            pairs += [[_r(a), _r(b)] for a, b in zip(r["y"][::step], np.nan_to_num(sim)[::step])]
        scheme = "loro"
    else:
        sim = model.simulate(model.theta, runs[0]["t"], runs[0]["T"], runs[0]["y0"])
        all_err.append(np.abs(np.nan_to_num(sim) - runs[0]["y"]))
        base_err.append(np.abs(np.mean(runs[0]["y"]) - runs[0]["y"]))
        per_run.append({"run": runs[0]["run"], "mae": _r(np.mean(all_err[0])), "points": int(len(runs[0]["y"])),
                        "in_sample": True})
        scheme = "in_sample"
    err = np.concatenate(all_err)
    berr = np.concatenate(base_err)
    mae, bmae = float(np.mean(err)), float(np.mean(berr))
    metrics = {"mae": _r(mae), "rmse": _r(np.sqrt(np.mean(err ** 2))), "baseline_mae": _r(bmae),
               "skill": _r(max(-1.0, min(1.0, 1 - mae / bmae)) if bmae > 0 else 0.0, 4),
               "method": "kinetics", "n": int(sum(len(r["y"]) for r in runs)), "runs": len(runs),
               "scheme": scheme, "per_run": per_run}
    if scheme == "in_sample":
        metrics["curve_verdict"] = "один опыт: честной проверки нет — добавьте кривые при других скоростях нагрева"
    else:
        metrics["curve_verdict"] = "каждая кривая предсказана моделью, подобранной без неё"
    t = _t975(model.dof)
    params = []
    for i, name in enumerate(model.params):
        sd = math.sqrt(max(model.cov[i, i], 0)) if np.isfinite(model.cov[i, i]) else None
        val = model.theta[name]
        if name in model.logp:
            params.append({"name": name, "value": _r(10 ** val), "log10": _r(val),
                           "ci95_log10": _r(t * sd) if sd is not None else None})
        else:
            params.append({"name": name, "value": _r(val), "ci95": _r(t * sd) if sd is not None else None})
    temps = np.concatenate([r["T"] for r in runs])
    times = np.concatenate([r["t"] for r in runs])
    return {
        "fit": {"outputs": {out: {"method": "kinetics", "theta": {k: float(v) for k, v in model.theta.items()},
                                  "log_params": sorted(model.logp), "cov": np.nan_to_num(model.cov).tolist(),
                                  "s2": float(model.s2), "dof": int(model.dof), "params": params,
                                  "sigma": _r(metrics["rmse"])}}},
        "validation": {"per_output": {out: metrics}, "parity": {out: pairs},
                       "learning_curve": {}, "candidates": {}, "effects": {}},
        "domain": {"T": {"type": "number", "min": _r(temps.min()), "max": _r(temps.max())},
                   "t": {"type": "number", "min": _r(times.min()), "max": _r(times.max())},
                   "runs": [r["run"] for r in runs]},
        "threshold_check": _threshold_check(spec, {out: metrics}),
        "controls": [],
        "warnings": [],
        "n_rows": int(sum(len(r["y"]) for r in runs)),
        "curves": [{"run": r["run"], "t": [_r(v) for v in r["t"][:: max(1, len(r["t"]) // 60)]],
                    "y": [_r(v) for v in r["y"][:: max(1, len(r["y"]) // 60)]],
                    "T": [_r(v) for v in r["T"][:: max(1, len(r["T"]) // 60)]]} for r in runs],
    }


def _onnx_session(path: str):
    try:
        import onnxruntime as ort
    except ImportError:
        raise ModelError("На сервере не установлен onnxruntime — импортированные модели пока "
                         "не исполняются. Администратору: установить onnxruntime в окружение песочницы.")
    try:
        return ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    except Exception as exc:  # noqa: BLE001
        raise ModelError(f"Файл модели ONNX не открывается: {str(exc)[:300]}")


def _onnx_predict(sess, X):
    inp = sess.get_inputs()[0]
    out = sess.run(None, {inp.name: X.astype(np.float32)})[0]
    out = np.asarray(out, dtype=float)
    if out.ndim == 1:
        out = out[:, None]
    return out


def _train_onnx(df, spec: dict, job: dict) -> dict:
    sess = _onnx_session(job.get("onnx_file") or "model.onnx")
    inputs, outputs = spec["inputs"], spec["outputs"]
    declared = {i["name"]: i for i in inputs}
    domain = {}
    for inp in inputs:
        lo, hi = to_number(inp.get("min")), to_number(inp.get("max"))
        if math.isfinite(lo) and math.isfinite(hi):
            domain[inp["name"]] = {"type": "number", "min": _r(lo), "max": _r(hi)}
    per_output, parity = {}, {}
    n_rows = 0
    if df is not None and len(df):
        records = records_from_df(df, inputs, outputs)
        X, keep = [], []
        for i, r in enumerate(records):
            xs = [to_number(r.get(k)) for k in declared]
            if all(math.isfinite(v) for v in xs):
                X.append(xs)
                keep.append(i)
        X = np.array(X, dtype=float)
        if len(X) == 0:
            raise ModelError("В проверочной таблице нет строк с полным набором входов")
        preds = _onnx_predict(sess, X)
        n_rows = len(X)
        for j, out in enumerate(outputs):
            if j >= preds.shape[1]:
                raise ModelError(f"Модель выдаёт {preds.shape[1]} выходов, в паспорте объявлено {len(outputs)}")
            y = np.array([to_number(records[i].get(out["name"])) for i in keep], dtype=float)
            mask = np.isfinite(y)
            if mask.sum() < 3:
                continue
            m = _metrics(y[mask], preds[mask, j], np.full(mask.sum(), np.mean(y[mask])))
            m.update(method="onnx", n=int(mask.sum()), scheme="holdout")
            per_output[out["name"]] = m
            parity[out["name"]] = [[_r(a), _r(b)] for a, b in zip(y[mask], preds[mask, j])]
        for inp in inputs:
            if inp["name"] not in domain:
                vals = X[:, list(declared).index(inp["name"])]
                domain[inp["name"]] = {"type": "number", "min": _r(vals.min()), "max": _r(vals.max())}
    else:
        probe = np.array([[domain.get(i["name"], {}).get("min") or 0.0 for i in inputs]], dtype=float)
        preds = _onnx_predict(sess, probe)
        if preds.shape[1] < len(outputs):
            raise ModelError(f"Модель выдаёт {preds.shape[1]} выходов, в паспорте объявлено {len(outputs)}")
    fits = {o["name"]: {"method": "onnx", "index": j,
                        "sigma": (per_output.get(o["name"]) or {}).get("rmse")} for j, o in enumerate(outputs)}
    return {
        "fit": {"outputs": fits},
        "validation": {"per_output": per_output, "parity": parity, "learning_curve": {},
                       "candidates": {}, "effects": {}},
        "domain": domain,
        "threshold_check": _threshold_check(spec, per_output),
        "controls": [],
        "warnings": [] if per_output else ["без проверочной таблицы точность модели не измерена"],
        "n_rows": n_rows,
    }


# ---------------------------------------------------------------------------
# Прогноз


class Loaded:
    """Модель, пересобранная из версии: те же данные, те же гиперпараметры."""

    def __init__(self, job: dict):
        self.spec = job["spec"]
        self.kind = self.spec.get("kind") or "table"
        self.fit_info = job["fit"]
        self.domain = job.get("domain") or {}
        self.inputs = self.spec.get("inputs") or []
        self.outputs = self.spec.get("outputs") or []
        if self.kind == "table":
            self._load_table(job)
        elif self.kind == "formula":
            self._load_formula()
        elif self.kind == "kinetics":
            self._load_kinetics()
        elif self.kind == "onnx":
            self.sess = _onnx_session(job.get("onnx_file") or "model.onnx")

    def _load_table(self, job):
        df = read_tables([job.get("train_file") or "train.csv"])
        excluded = set(job.get("excluded_rows") or [])
        if excluded:
            df = df.drop(index=[i for i in excluded if 0 <= i < len(df)]).reset_index(drop=True)
        recs = records_from_df(df, self.inputs, self.outputs)
        self.fz = Featurizer(self.inputs, recs, esm_path=job.get("esm_path"))
        X, keep = self.fz.matrix(recs)
        self.train_records = [recs[i] for i in keep]
        self.models, self.sigmas = {}, {}
        for out in self.outputs:
            info = self.fit_info["outputs"][out["name"]]
            y = _outputs_values(self.train_records, out["name"])
            mask = np.isfinite(y)
            model = make_regressor(info["method"], X.shape[1], theta=info.get("theta"))
            model.fit(X[mask], y[mask])
            self.models[out["name"]] = (info["method"], model)
            self.sigmas[out["name"]] = float(info.get("sigma") or 0.0)
        num_idx = [i for i, name in enumerate(self.fz.names)
                   if any(name == inp["name"] for inp in self.inputs if inp.get("type", "number") == "number")]
        self.nn = None
        if len(num_idx) >= 2 and len(X) >= 3:
            Xn = X[:, num_idx]
            thr, mu, sd = _nn_threshold(Xn)
            if thr:
                self.nn = (thr, num_idx, mu, sd, (Xn - mu) / sd)
        self.train_fps = {}
        for inp in self.inputs:
            if inp.get("type") == "smiles":
                fps = [_fingerprint(r.get(inp["name"])) for r in self.train_records]
                self.train_fps[inp["name"]] = [f for f in fps if f is not None]

    def _load_formula(self):
        out = self.outputs[0]["name"]
        info = self.fit_info["outputs"][out]
        self.formula = FormulaModel(self.spec)
        self.formula.theta = {k: float(v) for k, v in info["theta"].items()}
        self.formula.cov = np.array(info.get("cov") or [[0.0]], dtype=float)
        self.formula.s2 = float(info.get("s2") or 0.0)

    def _load_kinetics(self):
        out = (self.outputs or [{"name": "y"}])[0]["name"]
        info = self.fit_info["outputs"][out]
        self.kin = KineticsModel(self.spec)
        self.kin.theta = {k: float(v) for k, v in info["theta"].items()}
        self.kin.cov = np.array(info.get("cov") or [[0.0]], dtype=float)

    # ---- расчёт для табличных, формульных и импортированных моделей

    def predict_records(self, recs: list) -> dict:
        n = len(recs)
        result = {}
        if self.kind == "table":
            X, keep = self.fz.matrix(recs)
            for out in self.outputs:
                method, model = self.models[out["name"]]
                vals = np.full(n, np.nan)
                sds = np.full(n, np.nan)
                if len(keep):
                    if method == "gp":
                        mean, sd = model.predict(X, return_std=True)
                    else:
                        mean = model.predict(X)
                        sd = np.full(len(mean), self.sigmas[out["name"]])
                    vals[keep] = mean
                    sds[keep] = sd
                result[out["name"]] = (vals, sds)
        elif self.kind == "formula":
            X = np.array([[to_number(r.get(inp["name"])) for inp in self.inputs] for r in recs], dtype=float)
            ok = np.all(np.isfinite(X), axis=1)
            vals, sds = np.full(n, np.nan), np.full(n, np.nan)
            if ok.any():
                mean, sd = self.formula.predict_std(X[ok])
                vals[ok], sds[ok] = mean, sd
            result[self.outputs[0]["name"]] = (vals, sds)
        elif self.kind == "onnx":
            X = np.array([[to_number(r.get(inp["name"])) for inp in self.inputs] for r in recs], dtype=float)
            ok = np.all(np.isfinite(X), axis=1)
            preds = _onnx_predict(self.sess, np.nan_to_num(X)) if n else np.zeros((0, len(self.outputs)))
            for j, out in enumerate(self.outputs):
                info = self.fit_info["outputs"][out["name"]]
                vals = np.where(ok, preds[:, j], np.nan)
                sig = info.get("sigma")
                sds = np.full(n, float(sig) if sig is not None else np.nan)
                result[out["name"]] = (vals, sds)
        return result

    def domain_of(self, rec: dict) -> dict:
        nn = None
        if self.kind == "table" and self.nn is not None:
            thr, idx, mu, sd, Z = self.nn
            row = self.fz.row(rec)
            if row is not None:
                x = np.array(row, dtype=float)[idx]
                nn = (thr, x, mu, sd, Z)
        fps = None
        if self.kind == "table":
            fps = next(iter(self.train_fps.values()), None) if self.train_fps else None
        return check_domain(self.inputs, self.domain, rec, nn=nn, train_fps=fps)


def _clean_inputs(inputs, loaded: Loaded) -> list:
    if isinstance(inputs, dict):
        inputs = [inputs]
    if not isinstance(inputs, list) or not inputs:
        raise ModelError("Передайте значения входов модели")
    if len(inputs) > PREDICT_MAX:
        raise ModelError(f"Не больше {PREDICT_MAX} точек за один вызов")
    return [dict(r) if isinstance(r, dict) else {} for r in inputs]


def predict(job: dict) -> dict:
    loaded = Loaded(job)
    if loaded.kind == "kinetics":
        return _predict_kinetics(loaded, job)
    recs = _clean_inputs(job.get("inputs"), loaded)
    preds = loaded.predict_records(recs)
    items = []
    for i, rec in enumerate(recs):
        outs = {}
        for out in loaded.outputs:
            vals, sds = preds[out["name"]]
            outs[out["name"]] = {"value": _r(vals[i]), "error": _r(sds[i], 3)}
        dom = loaded.domain_of(rec) if i < 200 else {"status": "inside", "notes": []}
        missing = [inp.get("title") or inp["name"] for inp in loaded.inputs if _is_missing(rec.get(inp["name"]))]
        if missing:
            dom = {"status": "outside", "notes": ["не заданы входы: " + ", ".join(missing)]}
        items.append({"inputs": rec, "outputs": outs, "domain": dom})
    return {"ok": True, "items": items}


def profile(job: dict) -> dict:
    """Кривая «выход от одного входа» при остальных фиксированных."""
    loaded = Loaded(job)
    if loaded.kind == "kinetics":
        raise ModelError("Для кинетической модели используйте температурную программу")
    base = dict(job.get("base") or {})
    vary = job.get("vary")
    inp = next((i for i in loaded.inputs if i["name"] == vary), None)
    if inp is None or inp.get("type", "number") != "number":
        raise ModelError("Выберите числовой вход для графика")
    dom = loaded.domain.get(vary) or {}
    lo, hi = dom.get("min"), dom.get("max")
    if lo is None or hi is None or lo == hi:
        v = to_number(base.get(vary))
        lo, hi = (v * 0.5, v * 1.5) if math.isfinite(v) and v else (0.0, 1.0)
    span = hi - lo
    xs = np.linspace(lo - 0.1 * span, hi + 0.1 * span, int(job.get("points") or 41))
    recs = [dict(base, **{vary: float(x)}) for x in xs]
    preds = loaded.predict_records(recs)
    series = {}
    for out in loaded.outputs:
        vals, sds = preds[out["name"]]
        series[out["name"]] = {"y": [_r(v) for v in vals], "err": [_r(s, 3) for s in sds]}
    return {"ok": True, "x": [_r(x) for x in xs], "series": series, "range": [lo, hi], "vary": vary}


def optimize(job: dict) -> dict:
    loaded = Loaded(job)
    if loaded.kind == "kinetics":
        return _optimize_kinetics(loaded, job)
    targets = job.get("targets") or {}
    if not targets:
        raise ModelError("Задайте цель: значение выхода с допуском, минимум или максимум")
    fixed = dict(job.get("fixed") or {})
    bounds = dict(job.get("bounds") or {})
    count = max(1, min(int(job.get("count") or 3), 8))
    rng = np.random.RandomState(0)
    free = [i for i in loaded.inputs if i["name"] not in fixed]
    if not free:
        raise ModelError("Все входы зафиксированы — подбирать нечего")
    box = {}
    for inp in free:
        key = inp["name"]
        d = loaded.domain.get(key) or {}
        if inp.get("type", "number") == "number":
            lo, hi = bounds.get(key, [d.get("min"), d.get("max")])
            lo, hi = to_number(lo), to_number(hi)
            if not (math.isfinite(lo) and math.isfinite(hi)):
                raise ModelError(f"Не знаю диапазона для «{inp.get('title') or key}»: задайте границы")
            box[key] = ("number", min(lo, hi), max(lo, hi))
        elif inp.get("type") == "category":
            levels = d.get("levels") or []
            if not levels:
                raise ModelError(f"Нет значений для «{inp.get('title') or key}»")
            box[key] = ("category", levels)
        else:
            raise ModelError(f"Вход «{inp.get('title') or key}» (молекула или последовательность) зафиксируйте")

    def sample(m):
        recs = []
        for _ in range(m):
            r = dict(fixed)
            for key, b in box.items():
                r[key] = float(rng.uniform(b[1], b[2])) if b[0] == "number" else b[1][rng.randint(len(b[1]))]
            recs.append(r)
        return recs

    scales = {}
    for name, tg in targets.items():
        info = loaded.fit_info["outputs"].get(name) or {}
        scales[name] = max(float(info.get("sigma") or 0.0), 1e-9)

    def score(recs):
        preds = loaded.predict_records(recs)
        total = np.zeros(len(recs))
        for name, tg in targets.items():
            if name not in preds:
                raise ModelError(f"У модели нет выхода «{name}»")
            vals, sds = preds[name]
            if tg.get("goal") in ("min", "max"):
                sign = 1.0 if tg["goal"] == "min" else -1.0
                total += sign * vals / scales[name]
            else:
                target = to_number(tg.get("value"))
                tol = to_number(tg.get("tolerance"))
                width = max(tol if math.isfinite(tol) and tol > 0 else scales[name], 1e-9)
                total += np.abs(vals - target) / width
        return np.nan_to_num(total, nan=1e9), preds

    recs = sample(3000)
    sc, _ = score(recs)
    order = np.argsort(sc)
    num_keys = [k for k, b in box.items() if b[0] == "number"]
    refined = []
    from scipy.optimize import minimize

    for idx in order[:12]:
        start = recs[idx]
        if not num_keys:
            refined.append(start)
            continue
        x0 = np.array([start[k] for k in num_keys], dtype=float)
        lows = np.array([box[k][1] for k in num_keys])
        highs = np.array([box[k][2] for k in num_keys])

        def obj(x, _start=start):
            r = dict(_start, **{k: float(v) for k, v in zip(num_keys, np.clip(x, lows, highs))})
            return float(score([r])[0][0])

        try:
            res = minimize(obj, x0, method="Nelder-Mead", options={"maxiter": 200, "xatol": 1e-4, "fatol": 1e-6})
            x = np.clip(res.x, lows, highs)
        except Exception:  # noqa: BLE001
            x = x0
        refined.append(dict(start, **{k: float(v) for k, v in zip(num_keys, x)}))
    sc2, preds2 = score(refined)
    chosen, chosen_idx = [], []
    spans = {k: (box[k][2] - box[k][1]) or 1.0 for k in num_keys}
    for idx in np.argsort(sc2):
        cand = refined[idx]
        diverse = True
        for other in chosen:
            same_cat = all(cand.get(k) == other.get(k) for k, b in box.items() if b[0] == "category")
            dist = math.sqrt(sum(((cand[k] - other[k]) / spans[k]) ** 2 for k in num_keys)) if num_keys else 0.0
            if same_cat and dist < 0.12:
                diverse = False
                break
        if diverse:
            chosen.append(cand)
            chosen_idx.append(idx)
        if len(chosen) >= count:
            break
    items = []
    for c, idx in zip(chosen, chosen_idx):
        outs = {}
        for name in loaded.fit_info["outputs"]:
            vals, sds = preds2[name]
            outs[name] = {"value": _r(vals[idx]), "error": _r(sds[idx], 3)}
        dev = {}
        for name, tg in targets.items():
            if tg.get("goal") not in ("min", "max"):
                dev[name] = _r(outs[name]["value"] - to_number(tg.get("value")))
        rounded = {k: (_r(v, 4) if isinstance(v, float) else v) for k, v in c.items()}
        items.append({"inputs": rounded, "outputs": outs, "deviation": dev, "domain": loaded.domain_of(c)})
    return {"ok": True, "items": items}


# ---- кинетика


def _predict_kinetics(loaded: Loaded, job: dict) -> dict:
    kin = loaded.kin
    prog = job.get("program") or []
    T0 = to_number(job.get("T0"))
    if not math.isfinite(T0):
        T0 = (loaded.domain.get("T") or {}).get("min") or 25.0
    y0 = to_number(job.get("y0"))
    y0 = 0.0 if not math.isfinite(y0) else y0
    t, T, t_nodes, T_nodes = program_profile(prog, T0, points=int(job.get("points") or 240))
    y = kin.simulate(kin.theta, t_nodes if len(t_nodes) > 1 else t, T_nodes, y0, t_eval=t)
    band_lo, band_hi = None, None
    cov = np.nan_to_num(kin.cov)
    if cov.shape[0] == len(kin.params) and np.any(cov):
        rng = np.random.RandomState(0)
        names = list(kin.theta)
        mean = np.array([kin.theta[n] for n in names])
        sims = []
        for _ in range(40):
            try:
                draw = rng.multivariate_normal(mean, cov)
            except Exception:  # noqa: BLE001
                break
            s = kin.simulate(dict(zip(names, draw)), t_nodes, T_nodes, y0, t_eval=t)
            if np.all(np.isfinite(s)):
                sims.append(s)
        if len(sims) >= 10:
            arr = np.array(sims)
            band_lo, band_hi = np.percentile(arr, 16, axis=0), np.percentile(arr, 84, axis=0)
    targets = [to_number(v) for v in (job.get("targets") or [0.5, 0.9, 0.95])]
    reach = []
    for target in targets:
        if not math.isfinite(target):
            continue
        hit = np.where(y >= target)[0]
        reach.append({"target": _r(target), "time": _r(t[hit[0]]) if len(hit) else None})
    dom = loaded.domain.get("T") or {}
    notes = []
    lo, hi = dom.get("min"), dom.get("max")
    if lo is not None and hi is not None:
        if T.min() < lo - 0.1 * (hi - lo) or T.max() > hi + 0.1 * (hi - lo):
            notes.append(f"температура программы выходит за диапазон опытов {lo}–{hi}")
    status = "edge" if notes else "inside"
    return {"ok": True, "t": [_r(v) for v in t], "T": [_r(v) for v in T], "y": [_r(v) for v in y],
            "y_lo": [_r(v) for v in band_lo] if band_lo is not None else None,
            "y_hi": [_r(v) for v in band_hi] if band_hi is not None else None,
            "final": _r(y[-1]), "reach": reach, "domain": {"status": status, "notes": notes}}


def _optimize_kinetics(loaded: Loaded, job: dict) -> dict:
    """Время до заданной степени превращения при выдержке на разных
    температурах (после нагрева с заданной скоростью от T0)."""
    kin = loaded.kin
    target = to_number(job.get("target"))
    if not math.isfinite(target):
        target = 0.9
    temps = [to_number(v) for v in (job.get("temperatures") or [])]
    if not temps:
        dom = loaded.domain.get("T") or {}
        lo, hi = dom.get("min") or 80.0, dom.get("max") or 180.0
        temps = list(np.linspace(lo, hi, 6))
    T0 = to_number(job.get("T0"))
    rate = to_number(job.get("rate"))
    horizon = to_number(job.get("max_time"))
    if not (math.isfinite(horizon) and horizon > 0):
        tmax = (loaded.domain.get("t") or {}).get("max")
        horizon = float(tmax) * 5 if tmax else 1000.0
    items = []
    for Th in temps:
        if not math.isfinite(Th):
            continue
        prog = []
        start = Th
        if math.isfinite(T0) and math.isfinite(rate) and rate > 0 and T0 != Th:
            prog.append({"kind": "ramp", "to": Th, "rate": rate})
            start = T0
        prog.append({"kind": "hold", "time": horizon})
        t, T, tn, Tn = program_profile(prog, start, points=600)
        y = kin.simulate(kin.theta, tn, Tn, 0.0, t_eval=t)
        hit = np.where(y >= target)[0]
        items.append({"T": _r(Th, 5), "time": _r(t[hit[0]]) if len(hit) else None,
                      "final": _r(y[-1])})
    return {"ok": True, "target": _r(target), "items": items}


if __name__ == "__main__":  # pragma: no cover — ручной запуск: python model_kernel.py job.json
    import sys

    print(json.dumps(run(json.load(open(sys.argv[1], encoding="utf-8"))), ensure_ascii=False))
