"""Химические возможности агентов (api/runtime/chemistry_tools.py).

Порт инструментария ChemCrow (ur-whitelab/chemcrow-public, MIT) на рантайм
платформы — без зависимости от пакета: расчётная песочница платформы уже
умеет rdkit, а сетевые запросы идут httpx-ом с таймаутами и вежливым
User-Agent (как у compound_lookup/literature_search). Подключение — как у
engineering_tools, одна строка в build_default_registry:

    from .chemistry_tools import chemistry_tools
    for tool in chemistry_tools():
        registry.register(tool)

Состав и происхождение из ChemCrow:

    chem_mol_convert         представления молекулы: SMILES/InChI/InChIKey/формула
    chem_descriptors         дескрипторы и правило Липински (SMILES2Weight)
    chem_functional_groups   функциональные группы по SMARTS-словарю (FuncGroups)
    chem_similarity          сходство Танимото двух структур (MolSimilarity)
    chem_substructure        поиск подструктуры по SMARTS
    chem_reaction_check      валидация reaction SMILES и атомный баланс
    chem_depict              2D-рисунок структуры/реакции — PNG-вложение чата
    chem_control_chem_screen скрининг по списку контролируемых веществ
                             (SimilarityToControlChem, порог Танимото 0.35)
    chem_safety_lookup       GHS-безопасность из PubChem (ExplosiveCheck и
                             данные SafetySummary; суммаризирует сам агент-цикл)
    rxn_retrosynthesis       ретросинтез IBM RXN (RXNRetrosynthesis)
    rxn_forward              предсказание продукта реакции (RXNPredict)

Разделение сред: RDKit-вычисления исполняются в расчётной песочнице
(rdkit установлен в /opt/aios-sandbox/venv, сеть из неё закрыта), внешние
сервисы (PubChem, IBM RXN) вызываются отсюда через httpx. RXN-инструменты
регистрируются только при заданном AIOS_RXN_API_KEY, RDKit-инструменты —
только при установленной песочнице: инструмент, который всегда отвечает
«не настроено», хуже отсутствующего.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path

import httpx

from . import compute_sandbox, uploads
from .config import get_settings
from .tools import Tool

HTTP_TIMEOUT = 20.0
CONTACT = "aios@technolog.edu.ru"  # как в engineering_tools: вежливый пул PubChem
USER_AGENT = f"AIOS-TechnologicalInstitute/1.0 (mailto:{CONTACT})"

# IBM RXN for Chemistry: ключ и (опционально) свой инстанс через .env.
RXN_DEFAULT_BASE = "https://rxn.app.accelerate.science/rxn/api/api/v1"
RXN_PROJECT_NAME = "AIOS Chemistry Tools"
RXN_TIMEOUT = 30.0
RXN_FORWARD_MODEL = "2020-08-10"
RXN_RETRO_MODEL = "2020-07-01"
RXN_POLL_INTERVAL = 5.0
RXN_FORWARD_BUDGET = 90.0
RXN_RETRO_BUDGET = 150.0
RXN_MAX_ROUTES = 5
RXN_MAX_STEPS = 5

CONTROL_CHEM_THRESHOLD = 0.35
CONTROL_CHEM_CSV = Path(__file__).resolve().parent / "chemistry_data" / "control_chem.csv"


# ---------------------------------------------------------------------------
# Мостик в песочницу: фиксированные сниппеты + аргументы JSON-литералом.
# Соглашение песочницы: сниппет выставляет переменную result (dict), ошибки
# внутри сниппета возвращаются как {"ok": False, "error": ...}, а не исключением.


async def _run_sandbox(snippet: str, args: dict, files: dict[str, bytes] | None = None) -> dict:
    prefix = "import json\nARGS = json.loads(%r)\n" % (json.dumps(args, ensure_ascii=False),)
    payload = await compute_sandbox.run_code(prefix + snippet, files=files)
    if not payload.get("ok"):
        return {
            "ok": False,
            "error": "Расчётная песочница не выполнила вычисление: "
                     + str(payload.get("error") or "")[-600:],
        }
    result = payload.get("result")
    if not isinstance(result, dict):
        return {"ok": False, "error": "Вычисление не вернуло результат"}
    return result


def _require_smiles(args: dict, key: str = "smiles") -> str | None:
    smiles = str(args.get(key) or "").strip()
    return smiles or None


# ---------------------------------------------------------------------------
# Сниппеты песочницы (rdkit там уже установлен)


_MOL_CONVERT = '''from rdkit import Chem
from rdkit.Chem import rdMolDescriptors

mol = Chem.MolFromSmiles(ARGS["smiles"])
if mol is None:
    result = {"ok": False, "error": "Некорректный SMILES: " + ARGS["smiles"]}
else:
    out = {
        "входной_smiles": ARGS["smiles"],
        "канонический_smiles": Chem.MolToSmiles(mol),
        "формула": rdMolDescriptors.CalcMolFormula(mol),
        "InChI": Chem.MolToInchi(mol),
        "InChIKey": Chem.MolToInchiKey(mol),
        "тяжёлых_атомов": mol.GetNumHeavyAtoms(),
        "формальный_заряд": Chem.GetFormalCharge(mol),
    }
    if ARGS.get("include_molblock"):
        out["molblock"] = Chem.MolToMolBlock(mol)
    result = {"ok": True, **out}
'''


_DESCRIPTORS = '''from rdkit import Chem
from rdkit.Chem import Crippen, Descriptors, rdMolDescriptors

mol = Chem.MolFromSmiles(ARGS["smiles"])
if mol is None:
    result = {"ok": False, "error": "Некорректный SMILES: " + ARGS["smiles"]}
else:
    hbd = rdMolDescriptors.CalcNumHBD(mol)
    hba = rdMolDescriptors.CalcNumHBA(mol)
    rot = rdMolDescriptors.CalcNumRotatableBonds(mol)
    mw = Descriptors.MolWt(mol)
    violations = sum([mw > 500, hbd > 5, hba > 10, rot > 10])
    result = {
        "ok": True,
        "входной_smiles": ARGS["smiles"],
        "канонический_smiles": Chem.MolToSmiles(mol),
        "формула": rdMolDescriptors.CalcMolFormula(mol),
        "масса_точная": round(rdMolDescriptors.CalcExactMolWt(mol), 3),
        "масса_средняя": round(mw, 2),
        "logp": round(Crippen.MolLogP(mol), 2),
        "tpsa_Å2": round(rdMolDescriptors.CalcTPSA(mol), 2),
        "доноры_водородной_связи": hbd,
        "акцепторы_водородной_связи": hba,
        "вращаемые_связи": rot,
        "колец": rdMolDescriptors.CalcNumRings(mol),
        "ароматических_колец": rdMolDescriptors.CalcNumAromaticRings(mol),
        "тяжёлых_атомов": mol.GetNumHeavyAtoms(),
        "формальный_заряд": Chem.GetFormalCharge(mol),
        "нарушений_липински": violations,
        "правило_липински_5": "соблюдается" if violations == 0 else "нарушений: " + str(violations),
    }
'''


# Словарь в духе ChemCrow FuncGroups (источник паттернов — RDKit
# FunctionalGroups / Daylight SMARTS); имена русские — как поля ответов.
_FUNCTIONAL_GROUPS = '''from rdkit import Chem

FG = {
    "гидроксил (спирт)": "[#6][OX2H]",
    "фенольный гидроксил": "[c][OX2H]",
    "первичный амин": "[NX3;H2;!$(N[C,S]=O)][#6]",
    "вторичный амин": "[NX3;H1;!$(N[C,S]=O)]([#6])[#6]",
    "третичный амин": "[NX3;H0;!$(N[C,S]=O)]([#6])([#6])[#6]",
    "амид": "[NX3][CX3]=[OX1]",
    "альдегид": "[CX3H1](=O)[#6]",
    "кетон": "[#6][CX3](=O)[#6]",
    "карбоновая кислота": "[CX3](=O)[OX2H1]",
    "сложный эфир": "[CX3](=O)[OX2H0][#6]",
    "простой эфир": "[OD2]([#6])[#6]",
    "нитрогруппа": "[$([NX3](=O)=O),$([NX3+](=O)[O-])]",
    "нитрильная группа": "[CX2]#N",
    "изоцианат": "[NX2]=[CX2]=[OX1]",
    "азидогруппа": "[$([NX1]=[NX2+]=[NX1-]),$([N-]=[N+]=N)]",
    "фтор": "[F]",
    "хлор": "[Cl]",
    "бром": "[Br]",
    "иод": "[I]",
    "тиол (меркаптан)": "[SX2H]",
    "сульфид (тиоэфир)": "[#6][SX2][#6]",
    "дисульфид": "[SX2][SX2]",
    "сульфоксид": "[SX3](=O)[#6]",
    "сульфон": "[SX4](=O)(=O)[#6]",
    "сульфонамид": "[SX4](=O)(=O)[NX3]",
    "мочевина": "[NX3][CX3](=[OX1])[NX3]",
    "карбамат (уретан)": "[OX2][CX3](=[OX1])[NX3]",
    "имин (основание Шиффа)": "[#6][CX3]=[NX2]",
    "эпоксид (оксиран)": "[CX4]1[OX2][CX4]1",
    "ацеталь": "[CX4]([OX2][#6])[OX2][#6]",
    "метокси-группа": "[#6][OX2]C",
    "ароматический цикл": "[a]",
}

mol = Chem.MolFromSmiles(ARGS["smiles"])
if mol is None:
    result = {"ok": False, "error": "Некорректный SMILES: " + ARGS["smiles"]}
else:
    found = []
    for name, smarts in FG.items():
        patt = Chem.MolFromSmarts(smarts)
        if patt is None:
            continue
        count = len(mol.GetSubstructMatches(patt))
        if count:
            found.append({"группа": name, "вхождений": count})
    result = {
        "ok": True,
        "входной_smiles": ARGS["smiles"],
        "канонический_smiles": Chem.MolToSmiles(mol),
        "группы": found,
        "всего_типов": len(found),
    }
'''


_SIMILARITY = '''from rdkit import Chem
from rdkit.Chem import DataStructs, rdFingerprintGenerator

m1 = Chem.MolFromSmiles(ARGS["smiles1"])
m2 = Chem.MolFromSmiles(ARGS["smiles2"])
if m1 is None:
    result = {"ok": False, "error": "Некорректный первый SMILES: " + ARGS["smiles1"]}
elif m2 is None:
    result = {"ok": False, "error": "Некорректный второй SMILES: " + ARGS["smiles2"]}
else:
    gen = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
    sim = DataStructs.TanimotoSimilarity(gen.GetFingerprint(m1), gen.GetFingerprint(m2))
    if sim >= 0.9:
        verdict = "структуры почти идентичны"
    elif sim >= 0.75:
        verdict = "очень похожие структуры"
    elif sim >= 0.5:
        verdict = "умеренное сходство"
    elif sim >= 0.35:
        verdict = "слабое сходство"
    else:
        verdict = "структуры не похожи"
    result = {
        "ok": True,
        "структура_1": Chem.MolToSmiles(m1),
        "структура_2": Chem.MolToSmiles(m2),
        "сходство_танимото": round(sim, 3),
        "интерпретация": verdict,
    }
'''


_SUBSTRUCTURE = '''from rdkit import Chem

mol = Chem.MolFromSmiles(ARGS["smiles"])
patt = Chem.MolFromSmarts(ARGS["smarts"])
if mol is None:
    result = {"ok": False, "error": "Некорректный SMILES: " + ARGS["smiles"]}
elif patt is None:
    result = {"ok": False, "error": "Некорректный SMARTS: " + ARGS["smarts"]}
else:
    matches = mol.GetSubstructMatches(patt)
    result = {
        "ok": True,
        "канонический_smiles": Chem.MolToSmiles(mol),
        "найдено": bool(matches),
        "вхождений": len(matches),
        "атомы_первого_вхождения": sorted(matches[0]) if matches else [],
    }
'''


_REACTION_CHECK = '''from collections import Counter

from rdkit import Chem
from rdkit.Chem import rdChemReactions

rxn_smiles = ARGS["reaction_smiles"]
parts = rxn_smiles.split(">")
if len(parts) != 3:
    result = {"ok": False,
              "error": "Reaction SMILES должен иметь вид реагенты>агенты>продукты"}
else:
    reactants_s, agents_s, products_s = parts

    def parse(fragment):
        mols = []
        for smi in [s for s in fragment.split(".") if s]:
            mols.append((smi, Chem.MolFromSmiles(smi)))
        return mols

    parsed = {side: parse(frag)
              for side, frag in (("реагенты", reactants_s),
                                 ("агенты", agents_s),
                                 ("продукты", products_s))}
    bad = [smi for mols in parsed.values() for smi, m in mols if m is None]
    if bad:
        result = {"ok": False, "error": "Некорректные фрагменты: " + ", ".join(bad)}
    else:
        def elements(fragment):
            counter = Counter()
            for _, m in parsed[fragment]:
                for atom in Chem.AddHs(m).GetAtoms():
                    counter[atom.GetSymbol()] += 1
            return counter

        left, right = elements("реагенты"), elements("продукты")
        diff = {el: [left.get(el, 0), right.get(el, 0)]
                for el in set(left) | set(right)
                if left.get(el, 0) != right.get(el, 0)}
        rxn = rdChemReactions.ReactionFromSmiles(rxn_smiles)
        result = {
            "ok": True,
            "reaction_smiles": rxn_smiles,
            "реакция_корректна": rxn is not None,
            "реагентов": len(parsed["реагенты"]),
            "агентов": len(parsed["агенты"]),
            "продуктов": len(parsed["продукты"]),
            "атомный_баланс": "соблюдается" if not diff else "нарушен",
            "расхождения_по_элементам": diff,
        }
'''


_DEPICT = '''from rdkit import Chem
from rdkit.Chem import rdChemReactions, rdDepictor
from rdkit.Chem.Draw import rdMolDraw2D

reaction = ARGS.get("reaction_smiles") or ""
smiles = ARGS.get("smiles") or ""
legend = ARGS.get("legend") or ""

if reaction:
    rxn = rdChemReactions.ReactionFromSmiles(reaction)
    if rxn is None:
        result = {"ok": False, "error": "Некорректный reaction SMILES: " + reaction}
    else:
        drawer = rdMolDraw2D.MolDraw2DCairo(900, 400)
        drawer.DrawReaction(rxn)
        drawer.FinishDrawing()
        with open("structure.png", "wb") as fh:
            fh.write(drawer.GetDrawingText())
        result = {"ok": True, "тип": "реакция", "reaction_smiles": reaction}
elif smiles:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        result = {"ok": False, "error": "Некорректный SMILES: " + smiles}
    else:
        rdDepictor.Compute2DCoords(mol)
        drawer = rdMolDraw2D.MolDraw2DCairo(600, 500)
        rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol, legend=legend)
        drawer.FinishDrawing()
        with open("structure.png", "wb") as fh:
            fh.write(drawer.GetDrawingText())
        result = {"ok": True, "тип": "молекула",
                  "канонический_smiles": Chem.MolToSmiles(mol)}
else:
    result = {"ok": False, "error": "Укажите smiles или reaction_smiles"}
'''


_CONTROL_SCREEN = '''import csv

from rdkit import Chem
from rdkit.Chem import DataStructs, rdFingerprintGenerator

query_smiles = ARGS.get("smiles") or ""
query_cas = ARGS.get("cas") or ""
threshold = ARGS.get("threshold", 0.35)

rows = []
with open("control_chem.csv", newline="", encoding="utf-8") as fh:
    for row in csv.DictReader(fh):
        if row.get("smiles") or row.get("cas"):
            rows.append(row)

if not query_smiles and not query_cas:
    result = {"ok": False, "error": "Укажите smiles или cas"}
else:
    qmol = Chem.MolFromSmiles(query_smiles) if query_smiles else None
    if query_smiles and qmol is None:
        result = {"ok": False, "error": "Некорректный SMILES: " + query_smiles}
    else:
        def clean(row):
            return {"вещество": row.get("name"), "cas": row.get("cas"),
                    "список": row.get("schedule")}

        exact = []
        qcanon = Chem.MolToSmiles(qmol) if qmol is not None else None
        for row in rows:
            hit = False
            if query_cas and (row.get("cas") or "").strip() == query_cas.strip():
                hit = True
            if qcanon and row.get("smiles"):
                m = Chem.MolFromSmiles(row["smiles"])
                if m is not None and Chem.MolToSmiles(m) == qcanon:
                    hit = True
            if hit:
                exact.append(clean(row))

        similar = []
        if qmol is not None and not exact:
            gen = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
            qfp = gen.GetFingerprint(qmol)
            for row in rows:
                if not row.get("smiles"):
                    continue
                m = Chem.MolFromSmiles(row["smiles"])
                if m is None:
                    continue
                sim = DataStructs.TanimotoSimilarity(qfp, gen.GetFingerprint(m))
                if sim >= threshold:
                    entry = clean(row)
                    entry["сходство_танимото"] = round(sim, 3)
                    similar.append(entry)
            similar.sort(key=lambda item: item["сходство_танимото"], reverse=True)

        if exact:
            verdict = ("ОБНАРУЖЕНО точное совпадение со списком контролируемых "
                       "веществ. Требуется официальная проверка уполномоченным "
                       "органом и консультация ответственного по безопасности.")
        elif similar:
            verdict = ("Структурно похоже на вещества из списка контролируемых "
                       "(сходство Танимото >= порога). Это не совпадение и не "
                       "основание для выводов — проверьте вещество по CAS.")
        else:
            verdict = ("Точных совпадений и выраженного структурного сходства со "
                       "списком контролируемых веществ не найдено. Скрининг "
                       "информационный и не заменяет официальную проверку.")
        result = {
            "ok": True,
            "точные_совпадения": exact,
            "похожие": similar[:10],
            "порог_сходства": threshold,
            "вывод": verdict,
            "дисклеймер": "Информационный скрининг по открытому списку ОЗХМ; "
                          "не является официальной экспертизой.",
        }
'''


# ---------------------------------------------------------------------------
# Обработчики дорожки A: RDKit через песочницу


async def chem_mol_convert(args: dict, ctx: dict | None) -> dict:
    smiles = _require_smiles(args)
    if not smiles:
        return {"ok": False, "error": "Не указан SMILES"}
    return await _run_sandbox(_MOL_CONVERT, {
        "smiles": smiles,
        "include_molblock": bool(args.get("include_molblock", False)),
    })


async def chem_descriptors(args: dict, ctx: dict | None) -> dict:
    smiles = _require_smiles(args)
    if not smiles:
        return {"ok": False, "error": "Не указан SMILES"}
    return await _run_sandbox(_DESCRIPTORS, {"smiles": smiles})


async def chem_functional_groups(args: dict, ctx: dict | None) -> dict:
    smiles = _require_smiles(args)
    if not smiles:
        return {"ok": False, "error": "Не указан SMILES"}
    return await _run_sandbox(_FUNCTIONAL_GROUPS, {"smiles": smiles})


async def chem_similarity(args: dict, ctx: dict | None) -> dict:
    smiles1 = _require_smiles(args, "smiles1")
    smiles2 = _require_smiles(args, "smiles2")
    if not smiles1 or not smiles2:
        return {"ok": False, "error": "Нужны оба SMILES (smiles1 и smiles2)"}
    return await _run_sandbox(_SIMILARITY, {"smiles1": smiles1, "smiles2": smiles2})


async def chem_substructure(args: dict, ctx: dict | None) -> dict:
    smiles = _require_smiles(args)
    smarts = str(args.get("smarts") or "").strip()
    if not smiles:
        return {"ok": False, "error": "Не указан SMILES молекулы"}
    if not smarts:
        return {"ok": False, "error": "Не указан SMARTS-запрос"}
    return await _run_sandbox(_SUBSTRUCTURE, {"smiles": smiles, "smarts": smarts})


async def chem_reaction_check(args: dict, ctx: dict | None) -> dict:
    reaction = str(args.get("reaction_smiles") or "").strip()
    if not reaction:
        return {"ok": False, "error": "Не указан reaction SMILES"}
    return await _run_sandbox(_REACTION_CHECK, {"reaction_smiles": reaction})


def _save_artifacts(payload: dict, ctx: dict | None) -> dict:
    """PNG-артефакты песочницы → вложения владельца (паттерн run_calc)."""
    user_id = ((ctx or {}).get("session") or {}).get("user_id") or ""
    saved = []
    for item in payload.pop("artifacts", []) or []:
        name = str(item.get("name") or "структура.png")
        if not user_id:
            saved.append({"файл": name, "сохранён": False,
                          "причина": "расчёт без авторизации — файл не сохранён"})
            continue
        try:
            blob = base64.b64decode(item.get("base64") or "")
            meta = uploads.get_store().save(user_id, name, blob)
        except Exception as exc:  # noqa: BLE001 — неудача с файлом не роняет инструмент
            saved.append({"файл": name, "сохранён": False, "причина": str(exc)[:200]})
            continue
        saved.append({
            "файл": name,
            "сохранён": True,
            "размер_кб": round(len(blob) / 1024, 1),
            "ссылка": f"/api/chat/attachments/{meta['id']}",
        })
    if saved:
        payload["созданные_файлы"] = saved
        payload["подсказка_по_файлам"] = (
            "Покажите каждый файл человеку компонентом file_card — готовый "
            "блок, подставьте поля из созданные_файлы:\n"
            '```aios-view\n{"component": "file_card", "filename": "<файл>", '
            '"url": "<ссылка>", "size": "<размер_кб> КБ"}\n```\n'
            "Не вставляйте содержимое файла в ответ и не оставляйте ссылку "
            "голым текстом или в код-блоке: без file_card участник не увидит "
            "картинку в чате."
        )
    return payload


async def chem_depict(args: dict, ctx: dict | None) -> dict:
    smiles = _require_smiles(args)
    reaction = str(args.get("reaction_smiles") or "").strip()
    legend = str(args.get("legend") or "").strip()[:120]
    if not smiles and not reaction:
        return {"ok": False, "error": "Укажите smiles (молекула) или reaction_smiles (реакция)"}
    sandbox_args = {"smiles": smiles, "reaction_smiles": reaction, "legend": legend}
    prefix = "import json\nARGS = json.loads(%r)\n" % (json.dumps(sandbox_args, ensure_ascii=False),)
    payload = await compute_sandbox.run_code(prefix + _DEPICT)
    if not payload.get("ok"):
        return {
            "ok": False,
            "error": "Расчётная песочница не выполнила вычисление: "
                     + str(payload.get("error") or "")[-600:],
        }
    result = payload.get("result")
    if not isinstance(result, dict) or not result.get("ok"):
        return result if isinstance(result, dict) else {"ok": False, "error": "Рисунок не построен"}
    return _save_artifacts({**result, "artifacts": payload.get("artifacts")}, ctx)


async def chem_control_chem_screen(args: dict, ctx: dict | None) -> dict:
    smiles = _require_smiles(args)
    cas = str(args.get("cas") or "").strip()
    if not smiles and not cas:
        return {"ok": False, "error": "Укажите smiles или cas вещества"}
    if not CONTROL_CHEM_CSV.is_file():
        return {"ok": False, "error": "Список контролируемых веществ не найден на сервере"}
    return await _run_sandbox(
        _CONTROL_SCREEN,
        {"smiles": smiles, "cas": cas, "threshold": CONTROL_CHEM_THRESHOLD},
        files={"control_chem.csv": CONTROL_CHEM_CSV.read_bytes()},
    )


# ---------------------------------------------------------------------------
# Дорожка B: GHS-безопасность из PubChem (httpx, без ключа)

_GHS_PICTOGRAMS = (
    "Exploding Bomb", "Flame Over Circle", "Flame", "Gas Cylinder",
    "Corrosion", "Skull and Crossbones", "Health Hazard", "Exclamation Mark",
    "Environment",
)


def _collect_strings(node, out: list) -> None:
    """Рекурсивно собирает строки PUG View (StringWithMarkup/строки)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "StringWithMarkup" and isinstance(value, list):
                for item in value:
                    text = item.get("String") if isinstance(item, dict) else None
                    if text:
                        out.append(str(text))
            elif key == "String" and isinstance(value, str):
                out.append(value)
            else:
                _collect_strings(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_strings(item, out)


async def _resolve_cid(client: httpx.AsyncClient, query: str) -> int | None:
    """Название/SMILES/CID → PubChem CID; перебираем пространство имён."""
    namespaces = []
    if query.isdigit():
        namespaces.append(f"cid/{query}")
    else:
        namespaces.append(f"name/{query}")
        if not re.search(r"\s", query):
            namespaces.append(f"smiles/{query}")
    for ns in namespaces:
        url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/{ns}/cids/JSON"
        try:
            resp = await client.get(url)
        except httpx.HTTPError:
            continue
        if resp.status_code != 200:
            continue
        cids = ((resp.json() or {}).get("IdentifierList") or {}).get("CID") or []
        if cids:
            return int(cids[0])
    return None


async def _ghs_text(client: httpx.AsyncClient, cid: int) -> str:
    url = (f"https://pubchem.ncbi.nlm.nih.gov/rest/pug_view/data/compound/{cid}/JSON"
           "?heading=GHS%20Classification")
    try:
        resp = await client.get(url)
    except httpx.HTTPError:
        return ""
    if resp.status_code != 200:
        return ""
    strings: list[str] = []
    _collect_strings(resp.json(), strings)
    return "\n".join(strings)


async def chem_safety_lookup(args: dict, ctx: dict | None) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "Не указано вещество (название, SMILES или CID)"}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT,
                                 headers={"User-Agent": USER_AGENT}) as client:
        cid = await _resolve_cid(client, query)
        if cid is None:
            return {"ok": False, "error": f"В PubChem не найдено: {query}"}
        text = await _ghs_text(client, cid)
    if not text.strip():
        return {
            "ok": True,
            "cid": cid,
            "запрос": query,
            "ghs": None,
            "источник": "PubChem",
            "note": "GHS-классификация для этого вещества в PubChem отсутствует "
                    "(например, вещество не классифицировано).",
        }

    h_codes = sorted(set(re.findall(r"\bH\d{3}\b", text)))
    p_codes = sorted(set(re.findall(r"\bP\d{3}\b", text)))[:30]
    signal = "Danger" if re.search(r"\bDanger\b", text) else \
             ("Warning" if re.search(r"\bWarning\b", text) else None)
    lower = text.lower()
    pictograms = [p for p in _GHS_PICTOGRAMS if p.lower() in lower]
    pictograms += sorted(set(re.findall(r"\bGHS0\d\b", text)))
    explosive = bool([h for h in h_codes if "200" <= h[1:] <= "299"]) or "explos" in lower
    warnings = [line[:200] for line in text.splitlines() if line.strip()][:12]
    return {
        "ok": True,
        "cid": cid,
        "запрос": query,
        "сигнальное_слово": signal,
        "пиктограммы": pictograms,
        "коды_опасности_H": h_codes[:40],
        "коды_мер_предосторожности_P": p_codes,
        "взрывоопасность": explosive,
        "предупреждения": warnings,
        "источник": "PubChem PUG View (GHS Classification)",
        "note": "Данные справочные. Перечислите пользователю классы опасности и "
                "пиктограммы; это не замена паспорту безопасности (SDS).",
    }


# ---------------------------------------------------------------------------
# Дорожка C: IBM RXN for Chemistry (ключ AIOS_RXN_API_KEY в .env)

_rxn_project: dict = {"id": None}


def _rxn_base() -> str:
    return (get_settings().rxn_base_url or RXN_DEFAULT_BASE).rstrip("/")


def _rxn_headers() -> dict:
    # Схема аутентификации IBM RXN: ключ целиком в Authorization (без Bearer).
    return {"Content-Type": "application/json",
            "Authorization": get_settings().rxn_api_key}


async def _rxn_project_id(client: httpx.AsyncClient) -> str | None:
    """Проект для предсказаний: находим свой, иначе создаём (и кэшируем)."""
    if _rxn_project["id"]:
        return _rxn_project["id"]
    base = _rxn_base()
    resp = await client.get(f"{base}/projects", params={"size": 100})
    if resp.status_code == 200:
        payload = (resp.json() or {}).get("payload") or {}
        items = payload.get("content") if isinstance(payload, dict) else payload
        for item in items or []:
            if item.get("name") == RXN_PROJECT_NAME and item.get("id") is not None:
                _rxn_project["id"] = str(item["id"])
                return _rxn_project["id"]
    resp = await client.post(
        f"{base}/projects", json={"name": RXN_PROJECT_NAME, "invitations": []})
    if resp.status_code in (200, 201):
        pid = ((resp.json() or {}).get("payload") or {}).get("id")
        if pid is not None:
            _rxn_project["id"] = str(pid)
            return _rxn_project["id"]
    return None


async def _rxn_poll(client: httpx.AsyncClient, path: str, budget: float,
                    params: dict) -> dict | None:
    """Поллинг асинхронной задачи RXN до SUCCESS в рамках бюджета секунд."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while True:
        await asyncio.sleep(RXN_POLL_INTERVAL)
        resp = await client.get(path, params=params)
        if resp.status_code != 200:
            return None
        payload = (resp.json() or {}).get("payload") or {}
        status = str(payload.get("status") or "").upper()
        if status in ("SUCCESS", "DONE"):
            return payload
        if status in ("ERROR", "FAILED", "CANCELLED"):
            return payload
        if loop.time() >= deadline:
            return payload


def _rxn_error(payload: dict | None, what: str) -> dict:
    status = str((payload or {}).get("status") or "нет ответа")
    return {
        "ok": False,
        "error": f"IBM RXN: {what} (статус: {status}). Попросите пользователя "
                 "повторить запрос позже или упростить структуру.",
    }


async def rxn_forward(args: dict, ctx: dict | None) -> dict:
    reactants = str(args.get("reactants_smiles") or "").strip()
    if not reactants:
        return {"ok": False, "error": "Не указаны реагенты (SMILES, при нескольких — через точку)"}
    try:
        top_n = max(1, min(int(args.get("top_n", 3)), 5))
    except (TypeError, ValueError):
        top_n = 3

    async with httpx.AsyncClient(timeout=RXN_TIMEOUT, headers=_rxn_headers() | {"User-Agent": USER_AGENT}) as client:
        project_id = await _rxn_project_id(client)
        if project_id is None:
            return {"ok": False,
                    "error": "IBM RXN: не удалось получить проект для предсказаний. "
                             "Проверьте AIOS_RXN_API_KEY и доступность сервиса."}
        resp = await client.post(
            f"{_rxn_base()}/predictions/pr",
            params={"projectId": project_id, "aiModel": RXN_FORWARD_MODEL},
            json={"aiModel": RXN_FORWARD_MODEL, "reactants": reactants},
        )
        if resp.status_code not in (200, 201):
            return {"ok": False,
                    "error": f"IBM RXN отклонил запрос ({resp.status_code}): "
                             f"{resp.text[:300]}"}
        prediction_id = ((resp.json() or {}).get("payload") or {}).get("id")
        if prediction_id is None:
            return {"ok": False, "error": "IBM RXN не вернул id предсказания"}
        payload = await _rxn_poll(
            client, f"{_rxn_base()}/predictions/{prediction_id}",
            RXN_FORWARD_BUDGET, {"projectId": project_id})
    if payload is None or str(payload.get("status") or "").upper() not in ("SUCCESS", "DONE"):
        return _rxn_error(payload, "предсказание продукта не завершилось")

    results = []
    for attempt in (payload.get("attempts") or [])[:top_n]:
        product = ((attempt.get("productMolecule") or {}).get("smiles")
                   or attempt.get("smiles"))
        entry = {"продукт": product}
        try:
            entry["уверенность"] = round(float(attempt.get("confidence") or 0), 3)
        except (TypeError, ValueError):
            entry["уверенность"] = None
        results.append(entry)
    if not results:
        return {"ok": False, "error": "IBM RXN не вернул вариантов продукта"}
    return {
        "ok": True,
        "реагенты": reactants,
        "результаты": results,
        "лучший_продукт": results[0],
        "источник": "IBM RXN for Chemistry",
    }


def _route_from_sequence(sequence: dict, number: int) -> dict:
    """Линейризует дерево ретросинтеза: каждый узел с детьми — разрыв связи."""
    tree = sequence.get("tree") or {}
    meta = tree.get("metaData") or tree.get("metadata") or {}
    steps: list[dict] = []

    def walk(node: dict, depth: int) -> None:
        if depth > RXN_MAX_STEPS + 2 or len(steps) > 24:
            return
        children = node.get("children") or []
        node_meta = node.get("metaData") or node.get("metadata") or {}
        commercial = bool(node_meta.get("isCommercial")) or \
            str(node_meta.get("borderColor") or "").upper() == "GREEN"
        if children:
            precursors = ".".join(str(child.get("smiles") or "") for child in children)
            steps.append({
                "шаг": len(steps) + 1,
                "синтез": f"{node.get('smiles')} >> {precursors}",
                "продукт_шага": node.get("smiles"),
                "исходники_шага": precursors,
            })
        elif commercial:
            steps.append({
                "шаг": len(steps) + 1,
                "коммерчески_доступный_реагент": node.get("smiles"),
            })
        for child in children:
            walk(child, depth + 1)

    walk(tree, 0)
    confidence = sequence.get("confidence")
    if confidence is None:
        confidence = meta.get("confidence")
    try:
        confidence = round(float(confidence), 3) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    return {
        "номер": number,
        "уверенность": confidence,
        "число_шагов": len([s for s in steps if "синтез" in s]),
        "шаги": steps,
    }


async def rxn_retrosynthesis(args: dict, ctx: dict | None) -> dict:
    product = _require_smiles(args)
    if not product:
        return {"ok": False, "error": "Не указан SMILES целевого продукта"}
    try:
        max_routes = max(1, min(int(args.get("max_routes", 3)), RXN_MAX_ROUTES))
        max_steps = max(1, min(int(args.get("max_steps", 3)), RXN_MAX_STEPS))
    except (TypeError, ValueError):
        max_routes, max_steps = 3, 3

    body = {
        "aiModel": RXN_RETRO_MODEL,
        "isinteractive": False,
        "parameters": {
            "fap": 0.6, "max_steps": max_steps, "nbeams": 10,
            "pruning_steps": 2, "search_strategy": "hyper",
        },
        "product": product,
    }
    async with httpx.AsyncClient(timeout=RXN_TIMEOUT, headers=_rxn_headers() | {"User-Agent": USER_AGENT}) as client:
        project_id = await _rxn_project_id(client)
        if project_id is None:
            return {"ok": False,
                    "error": "IBM RXN: не удалось получить проект для предсказаний. "
                             "Проверьте AIOS_RXN_API_KEY и доступность сервиса."}
        resp = await client.post(
            f"{_rxn_base()}/retrosynthesis/rs",
            params={"projectId": project_id, "aiModel": RXN_RETRO_MODEL},
            json=body,
        )
        if resp.status_code not in (200, 201):
            return {"ok": False,
                    "error": f"IBM RXN отклонил запрос ({resp.status_code}): "
                             f"{resp.text[:300]}"}
        prediction_id = ((resp.json() or {}).get("payload") or {}).get("id")
        if prediction_id is None:
            return {"ok": False, "error": "IBM RXN не вернул id задачи ретросинтеза"}
        payload = await _rxn_poll(
            client, f"{_rxn_base()}/retrosynthesis/{prediction_id}",
            RXN_RETRO_BUDGET, {"projectId": project_id})
    if payload is None or str(payload.get("status") or "").upper() not in ("SUCCESS", "DONE"):
        return _rxn_error(payload, "ретросинтез не завершился")

    routes = []
    for sequence in (payload.get("sequences") or [])[:max_routes]:
        routes.append(_route_from_sequence(sequence, len(routes) + 1))
    if not routes and payload.get("retrosynthetic_paths"):
        for path in payload["retrosynthetic_paths"][:max_routes]:
            routes.append({
                "номер": len(routes) + 1,
                "ретро_реакция": path.get("smiles"),
                "sequence_id": path.get("sequenceId"),
            })
    if not routes:
        return {"ok": False,
                "error": "IBM RXN не нашёл ретросинтетических маршрутов "
                         "(возможно, структура слишком сложная или экзотическая)"}
    return {
        "ok": True,
        "продукт": product,
        "маршруты": routes,
        "источник": "IBM RXN for Chemistry",
        "note": "Каждый шаг читается как «продукт >> исходники». Маршруты "
                "ранжированы по уверенности; проверяйте шаги chem_reaction_check, "
                "а доступность реагентов — compound_lookup/chem_safety_lookup.",
    }


# ---------------------------------------------------------------------------
# Регистрация


def chemistry_tools() -> list:
    tools: list[Tool] = []

    if compute_sandbox.sandbox_available():
        tools.extend([
            Tool(
                name="chem_mol_convert",
                description="Представления молекулы по SMILES: канонический SMILES, "
                            "брутто-формула, InChI, InChIKey, заряд. include_molblock=true "
                            "добавит MolBlock (для импорта в редакторы).",
                parameters={
                    "type": "object",
                    "properties": {
                        "smiles": {"type": "string", "description": "SMILES молекулы"},
                        "include_molblock": {"type": "boolean", "default": False,
                                             "description": "Добавить MolBlock"},
                    },
                    "required": ["smiles"],
                },
                handler=chem_mol_convert,
            ),
            Tool(
                name="chem_descriptors",
                description="Дескрипторы молекулы по SMILES: точная и средняя масса, "
                            "logP, TPSA, доноры/акцепторы водородной связи, вращаемые "
                            "связи, кольца, заряд и проверка правила Липински-5.",
                parameters={
                    "type": "object",
                    "properties": {
                        "smiles": {"type": "string", "description": "SMILES молекулы"},
                    },
                    "required": ["smiles"],
                },
                handler=chem_descriptors,
            ),
            Tool(
                name="chem_functional_groups",
                description="Функциональные группы молекулы по SMILES (спирты, амины, "
                            "амиды, карбонильные, нитро, галогены, сульфоны и др.) — "
                            "SMARTS-словарь с числом вхождений каждой группы.",
                parameters={
                    "type": "object",
                    "properties": {
                        "smiles": {"type": "string", "description": "SMILES молекулы"},
                    },
                    "required": ["smiles"],
                },
                handler=chem_functional_groups,
            ),
            Tool(
                name="chem_similarity",
                description="Структурное сходство двух молекул по Танимото "
                            "(Morgan-фингерпринты): 0 — не похожи, 1 — идентичны.",
                parameters={
                    "type": "object",
                    "properties": {
                        "smiles1": {"type": "string", "description": "SMILES первой структуры"},
                        "smiles2": {"type": "string", "description": "SMILES второй структуры"},
                    },
                    "required": ["smiles1", "smiles2"],
                },
                handler=chem_similarity,
            ),
            Tool(
                name="chem_substructure",
                description="Поиск подструктуры: содержит ли молекула (SMILES) фрагмент "
                            "по SMARTS-запросу; возвращает число вхождений и атомы первого.",
                parameters={
                    "type": "object",
                    "properties": {
                        "smiles": {"type": "string", "description": "SMILES молекулы"},
                        "smarts": {"type": "string", "description": "SMARTS-запрос фрагмента"},
                    },
                    "required": ["smiles", "smarts"],
                },
                handler=chem_substructure,
            ),
            Tool(
                name="chem_reaction_check",
                description="Проверка реакции в формате reaction SMILES "
                            "(реагенты>агенты>продукты): корректность, число компонентов "
                            "и атомный баланс по элементам.",
                parameters={
                    "type": "object",
                    "properties": {
                        "reaction_smiles": {"type": "string",
                                            "description": "Реакция, например CCO.O=C=O>>CCOC(=O)O"},
                    },
                    "required": ["reaction_smiles"],
                },
                handler=chem_reaction_check,
            ),
            Tool(
                name="chem_depict",
                description="Рисует 2D-структуру молекулы (smiles) или схемы реакции "
                            "(reaction_smiles) и возвращает PNG-вложение чата. Укажите "
                            "ровно один из двух параметров.",
                parameters={
                    "type": "object",
                    "properties": {
                        "smiles": {"type": "string", "description": "SMILES молекулы"},
                        "reaction_smiles": {"type": "string", "description": "Reaction SMILES"},
                        "legend": {"type": "string", "description": "Подпись под рисунком"},
                    },
                },
                handler=chem_depict,
            ),
            Tool(
                name="chem_control_chem_screen",
                description="Информационный скрининг вещества (SMILES или CAS) по открытому "
                            "списку контролируемых веществ (Конвенция ОЗХМ): точные совпадения "
                            "и структурно похожие (Танимото ≥ 0.35). Не является официальной "
                            "экспертизой.",
                parameters={
                    "type": "object",
                    "properties": {
                        "smiles": {"type": "string", "description": "SMILES вещества"},
                        "cas": {"type": "string", "description": "CAS-номер, например 505-60-2"},
                    },
                },
                handler=chem_control_chem_screen,
            ),
        ])

    # GHS-безопасность — только httpx, песочница не нужна
    tools.append(
        Tool(
            name="chem_safety_lookup",
            description="Классификация опасности вещества по GHS из PubChem: сигнальное "
                        "слово, пиктограммы, H-/P-коды, признак взрывоопасности. Принимает "
                        "название, SMILES или CID.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "Название вещества, SMILES или CID PubChem"},
                },
                "required": ["query"],
            },
            handler=chem_safety_lookup,
        )
    )

    # IBM RXN — только при настроенном ключе
    if get_settings().rxn_api_key:
        tools.extend([
            Tool(
                name="rxn_retrosynthesis",
                description="Планирует синтез целевого продукта (SMILES) моделью IBM RXN: "
                            "несколько альтернативных маршрутов с шагами «продукт >> исходники» "
                            "и уверенностью. Занимает до 2-3 минут. Не отправляйте повторно тот "
                            "же продукт — у сервиса лимиты; сначала проверьте полученные маршруты.",
                parameters={
                    "type": "object",
                    "properties": {
                        "smiles": {"type": "string", "description": "SMILES целевого продукта"},
                        "max_routes": {"type": "integer", "minimum": 1, "maximum": 5,
                                       "default": 3, "description": "Сколько маршрутов вернуть"},
                        "max_steps": {"type": "integer", "minimum": 1, "maximum": 5,
                                      "default": 3, "description": "Максимум шагов в маршруте"},
                    },
                    "required": ["smiles"],
                },
                handler=rxn_retrosynthesis,
            ),
            Tool(
                name="rxn_forward",
                description="Предсказывает продукт реакции по SMILES реагентов (через точку) "
                            "моделью IBM RXN: топ вариантов с уверенностью. Не отправляйте "
                            "повторно тот же набор реагентов — у сервиса лимиты.",
                parameters={
                    "type": "object",
                    "properties": {
                        "reactants_smiles": {"type": "string",
                                             "description": "Реагенты SMILES через точку, например CCO.OC(=O)C"},
                        "top_n": {"type": "integer", "minimum": 1, "maximum": 5,
                                  "default": 3, "description": "Сколько вариантов вернуть"},
                    },
                    "required": ["reactants_smiles"],
                },
                handler=rxn_forward,
            ),
        ])

    return tools
