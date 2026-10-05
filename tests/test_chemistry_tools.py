"""Тесты химических инструментов (chemistry_tools.py).

Проверяется то, что можно проверить без сети: регистрация (вместе с
gating-ом — песочница/ключ RXN), валидация аргументов до похода в песочницу,
скрытие RXN-инструментов без ключа. RDKit-вычисления гоняются только если
доступен интерпретатор с rdkit (боевая песочница /opt/aios-sandbox или
AIOS_SANDBOX_PYTHON): сниппеты считают реальные структуры.

Внешние API (PubChem, IBM RXN) живьём не дёргаются — как и в тестах
engineering_tools, сетевые проверки выполняются на сервере отдельно.
"""

import asyncio
import os
import subprocess
import sys

import pytest

from api.runtime import chemistry_tools as ct
from api.runtime import compute_sandbox
from api.runtime.config import get_settings
from api.runtime.tools import build_default_registry

CHEM_SANDBOX_NAMES = (
    "chem_mol_convert", "chem_descriptors", "chem_functional_groups",
    "chem_similarity", "chem_substructure", "chem_reaction_check",
    "chem_depict", "chem_control_chem_screen",
)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.delenv("AIOS_RXN_API_KEY", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def sandbox(monkeypatch):
    """Песочница, указывающая на текущий интерпретатор (как в engineering).

    Режим изоляции кэшируется в compute_sandbox._isolation_mode и зависит от
    того, какой интерпретатор пробовался, — сбрасываем кэш на входе и выходе:
    иначе следующие тесты запустят свой интерпретатор в чужом режиме (venv-
    питон недостижим внутри bwrap, смонтировавшего только /usr и корень
    боевой песочницы).
    """
    monkeypatch.setattr(compute_sandbox, "SANDBOX_PYTHON", sys.executable)
    monkeypatch.setattr(compute_sandbox, "SANDBOX_ROOT",
                        os.path.dirname(os.path.dirname(sys.executable)))
    compute_sandbox._isolation_mode.cache_clear()
    yield compute_sandbox
    compute_sandbox._isolation_mode.cache_clear()


_RDKIT_CACHE: dict = {}


def _python_has_rdkit(python: str) -> bool:
    if python not in _RDKIT_CACHE:
        try:
            done = subprocess.run(
                [python, "-I", "-c", "import rdkit"],
                capture_output=True, timeout=60, check=False,
            )
            _RDKIT_CACHE[python] = done.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _RDKIT_CACHE[python] = False
    return _RDKIT_CACHE[python]


@pytest.fixture
def rdkit_sandbox(monkeypatch):
    """Песочница с rdkit: боевая /opt/aios-sandbox или AIOS_SANDBOX_PYTHON.

    На машинах без неё RDKit-тесты пропускаются: в тестовом интерпретаторе
    платформы rdkit не установлен (и не должен — философия compute_sandbox).
    """
    candidates = [os.environ.get("AIOS_SANDBOX_PYTHON") or "",
                  "/opt/aios-sandbox/venv/bin/python"]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and _python_has_rdkit(candidate):
            monkeypatch.setattr(compute_sandbox, "SANDBOX_PYTHON", candidate)
            monkeypatch.setattr(compute_sandbox, "SANDBOX_ROOT",
                                os.path.dirname(os.path.dirname(candidate)))
            # как в фикстуре sandbox: кэш режима изоляции не должен протекать
            # в тесты с другим интерпретатором
            compute_sandbox._isolation_mode.cache_clear()
            yield compute_sandbox
            compute_sandbox._isolation_mode.cache_clear()
            return
    pytest.skip("нет интерпретатора песочницы с rdkit")


def _tools_by_name():
    return {tool.name: tool for tool in ct.chemistry_tools()}


# ---------------------------------------------------------------------------
# Реестр и gating


def test_chemistry_tools_registered(sandbox):
    registry = build_default_registry(get_settings().runtime_dir / "tools")
    for name in CHEM_SANDBOX_NAMES:
        assert registry.has(name), name
    assert registry.has("chem_safety_lookup")


def test_chem_tools_absent_without_sandbox(monkeypatch):
    monkeypatch.setattr(compute_sandbox, "SANDBOX_PYTHON", "/nonexistent/python")
    tools = _tools_by_name()
    for name in CHEM_SANDBOX_NAMES:
        assert name not in tools, name
    # GHS-безопасность — чистый httpx, песочница не нужна
    assert "chem_safety_lookup" in tools


def test_rxn_tools_hidden_without_key(sandbox):
    tools = _tools_by_name()
    assert "rxn_retrosynthesis" not in tools
    assert "rxn_forward" not in tools


def test_rxn_tools_registered_with_key(sandbox, monkeypatch):
    monkeypatch.setenv("AIOS_RXN_API_KEY", "test-key")
    get_settings.cache_clear()
    tools = _tools_by_name()
    assert "rxn_retrosynthesis" in tools
    assert "rxn_forward" in tools


def test_tool_specs_are_serializable(sandbox):
    import json

    registry = build_default_registry(get_settings().runtime_dir / "tools")
    for spec in registry.specs():
        json.dumps(spec)
        params = spec["function"]["parameters"]
        for required in params.get("required", []):
            assert required in params.get("properties", {}), spec["function"]["name"]


# ---------------------------------------------------------------------------
# Валидация аргументов (до песочницы, rdkit не нужен)


def test_validation_errors_before_sandbox(sandbox):
    tools = _tools_by_name()
    cases = [
        ("chem_mol_convert", {}),
        ("chem_descriptors", {"smiles": "   "}),
        ("chem_functional_groups", {}),
        ("chem_similarity", {"smiles1": "CCO"}),
        ("chem_substructure", {"smiles": "CCO"}),
        ("chem_reaction_check", {}),
        ("chem_depict", {}),
        ("chem_control_chem_screen", {}),
    ]
    for name, args in cases:
        result = asyncio.run(tools[name].handler(args, None))
        assert result["ok"] is False, (name, result)
        assert result["error"], name


# ---------------------------------------------------------------------------
# RDKit-вычисления (только при песочнице с rdkit)


def test_mol_convert_ethanol(rdkit_sandbox):
    result = asyncio.run(_tools_by_name()["chem_mol_convert"].handler({"smiles": "OCC"}, None))
    assert result["ok"], result
    assert result["канонический_smiles"] == "CCO"
    assert result["формула"] == "C2H6O"
    assert result["InChIKey"] == "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"


def test_descriptors_aspirin(rdkit_sandbox):
    result = asyncio.run(_tools_by_name()["chem_descriptors"].handler(
        {"smiles": "CC(=O)Oc1ccccc1C(=O)O"}, None))
    assert result["ok"], result
    assert abs(result["масса_точная"] - 180.042) < 0.01
    assert result["доноры_водородной_связи"] == 1
    assert result["нарушений_липински"] == 0


def test_functional_groups_aspirin(rdkit_sandbox):
    result = asyncio.run(_tools_by_name()["chem_functional_groups"].handler(
        {"smiles": "CC(=O)Oc1ccccc1C(=O)O"}, None))
    assert result["ok"], result
    groups = {entry["группа"] for entry in result["группы"]}
    assert "сложный эфир" in groups
    assert "карбоновая кислота" in groups
    assert "ароматический цикл" in groups


def test_similarity_pair(rdkit_sandbox):
    tools = _tools_by_name()
    identical = asyncio.run(tools["chem_similarity"].handler(
        {"smiles1": "CCO", "smiles2": "OCC"}, None))
    assert identical["ok"], identical
    assert identical["сходство_танимото"] == 1.0

    different = asyncio.run(tools["chem_similarity"].handler(
        {"smiles1": "CCO", "smiles2": "CC(=O)O"}, None))
    assert different["ok"], different
    assert 0.1 < different["сходство_танимото"] < 1.0


def test_substructure_search(rdkit_sandbox):
    result = asyncio.run(_tools_by_name()["chem_substructure"].handler(
        {"smiles": " OCCO ", "smarts": "[OX2H]"}, None))
    assert result["ok"], result
    assert result["найдено"] is True
    assert result["вхождений"] == 2


def test_reaction_check_balance(rdkit_sandbox):
    tools = _tools_by_name()
    balanced = asyncio.run(tools["chem_reaction_check"].handler(
        {"reaction_smiles": "CCO.O=C=O>>CCOC(=O)O"}, None))
    assert balanced["ok"], balanced
    assert balanced["реакция_корректна"] is True
    assert balanced["атомный_баланс"] == "соблюдается"

    unbalanced = asyncio.run(tools["chem_reaction_check"].handler(
        {"reaction_smiles": "CC>>CCO"}, None))
    assert unbalanced["ok"], unbalanced
    assert unbalanced["атомный_баланс"] == "нарушен"
    assert "O" in unbalanced["расхождения_по_элементам"]


def test_depict_returns_attachment(rdkit_sandbox, monkeypatch, tmp_path):
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt2"))
    get_settings.cache_clear()
    result = asyncio.run(_tools_by_name()["chem_depict"].handler(
        {"smiles": "c1ccccc1C(=O)O", "legend": "бензойная кислота"},
        {"session": {"user_id": "0123456789ab"}}))
    assert result["ok"], result
    assert result["созданные_файлы"][0]["сохранён"] is True
    assert result["созданные_файлы"][0]["ссылка"].startswith("/api/chat/attachments/")


def test_control_chem_screen_exact_and_clean(rdkit_sandbox):
    tools = _tools_by_name()
    sarin = asyncio.run(tools["chem_control_chem_screen"].handler(
        {"smiles": "CC(C)OP(=O)(C)F"}, None))
    assert sarin["ok"], sarin
    assert sarin["точные_совпадения"], sarin
    assert sarin["точные_совпадения"][0]["cas"] == "107-44-8"

    by_cas = asyncio.run(tools["chem_control_chem_screen"].handler(
        {"cas": "75-44-5"}, None))
    assert by_cas["ok"], by_cas
    assert any(row["cas"] == "75-44-5" for row in by_cas["точные_совпадения"])

    ethanol = asyncio.run(tools["chem_control_chem_screen"].handler(
        {"smiles": "CCO"}, None))
    assert ethanol["ok"], ethanol
    assert ethanol["точные_совпадения"] == []


def test_control_chem_csv_is_readable():
    """Список поставляется вместе с кодом (не в перезаписываемой data/)."""
    assert ct.CONTROL_CHEM_CSV.is_file()
    content = ct.CONTROL_CHEM_CSV.read_text(encoding="utf-8")
    assert content.count("\n") >= 30
    assert "cas,name,smiles,schedule" in content
