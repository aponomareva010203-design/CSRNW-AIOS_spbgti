"""Тесты инженерных возможностей агентов (engineering_tools.py, compute_sandbox.py).

Проверяется то, что можно проверить без сети и без боевой LLM: регистрация в
реестре, разбор таблиц участника, сборка документов, поведение песочницы
(расчёт, ошибка, отказ в сети, таймаут, входные файлы и артефакты).

Внешние API (OpenAlex, PubChem) здесь не дёргаются — сетевые тесты делают
набор хрупким; их живая проверка описана в инструкции по доработке и
выполняется на сервере одной командой.
"""

import asyncio
import csv
import json
import io
import os
import sys

import pytest

from api.runtime import compute_sandbox
from api.runtime.config import get_settings
from api.runtime.tools import build_default_registry


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def sandbox(monkeypatch):
    """Песочница, указывающая на текущий интерпретатор.

    В тестовой среде отдельного venv /opt/aios-sandbox нет, а проверить
    поведение песочницы надо: подменяем интерпретатор на тот, которым
    запущены тесты. Расчётных библиотек в нём может не быть — поэтому
    примеры ниже считают на чистом Python.
    """
    monkeypatch.setattr(compute_sandbox, "SANDBOX_PYTHON", sys.executable)
    monkeypatch.setattr(compute_sandbox, "SANDBOX_ROOT",
                        os.path.dirname(os.path.dirname(sys.executable)))
    return compute_sandbox


# ---------------------------------------------------------------------------
# Реестр


def test_engineering_tools_registered():
    registry = build_default_registry(get_settings().runtime_dir / "tools")
    for name in ("read_data_file", "literature_search", "compound_lookup",
                 "make_document", "notify"):
        assert registry.has(name), name


def test_run_calc_registered_only_with_sandbox(monkeypatch):
    """Инструмент, который всегда отвечает «не установлено», хуже
    отсутствующего: модель будет строить на нём планы. Поэтому run_calc
    появляется в реестре только вместе с песочницей."""
    monkeypatch.setattr(compute_sandbox, "SANDBOX_PYTHON", "/nonexistent/python")
    assert not build_default_registry(get_settings().runtime_dir / "tools").has("run_calc")

    monkeypatch.setattr(compute_sandbox, "SANDBOX_PYTHON", sys.executable)
    assert build_default_registry(get_settings().runtime_dir / "tools").has("run_calc")


def test_tool_specs_are_serializable():
    """Спецификации уходят провайдеру как JSON: обязательные параметры
    должны существовать в properties, иначе провайдер вернёт 400."""
    import json

    registry = build_default_registry(get_settings().runtime_dir / "tools")
    for spec in registry.specs():
        json.dumps(spec)
        params = spec["function"]["parameters"]
        for required in params.get("required", []):
            assert required in params.get("properties", {}), spec["function"]["name"]


# ---------------------------------------------------------------------------
# Песочница


def test_sandbox_computes_and_returns_result(sandbox):
    result = asyncio.run(sandbox.run_code(
        'A = 420000 / (540 * 31.4)\nprint(f"{A:.1f}")\nresult = {"A": round(A, 1)}'))
    assert result["ok"], result
    assert result["stdout"].strip() == "24.8"
    assert result["result"] == {"A": 24.8}
    assert "420000" in result["code"], "код расчёта возвращается для показа человеку"


def test_sandbox_reports_error_without_crashing(sandbox):
    result = asyncio.run(sandbox.run_code("1 / 0"))
    assert result["ok"] is False
    assert "ZeroDivisionError" in result["error"]


def test_sandbox_blocks_network(sandbox):
    """За внешними данными ходят инструменты платформы, у которых есть
    лимиты и логирование, а не произвольный код расчёта.

    Закрыто именно соединение: сокет создаётся (иначе ломается наследование
    в стандартной библиотеке, см. следующий тест), но никуда не идёт."""
    result = asyncio.run(sandbox.run_code(
        'import socket\n'
        's = socket.socket()\n'
        's.connect(("example.com", 80))\n'
    ))
    assert result["ok"] is False
    assert "Сеть из расчёта недоступна" in result["error"]

    through_urllib = asyncio.run(sandbox.run_code(
        'import urllib.request\n'
        'urllib.request.urlopen("http://example.com", timeout=3)\n'
    ))
    assert through_urllib["ok"] is False


def test_sandbox_keeps_socket_subclassable(sandbox):
    """Блокировка сети не должна ломать саму стандартную библиотеку.

    Пока socket.socket подменялся функцией, ssl.py падал на объявлении
    «class SSLSocket(socket)», а через ssl импортируется asyncio, через
    него joblib и дальше scikit-learn со statsmodels: ни одна модель по
    данным участника в песочнице не собиралась, и методики podgonka_modeli
    и plan_eksperimenta падали на первой же строке. Регрессия дорогая и
    незаметная, а расчётных библиотек в тестовом venv может не быть —
    поэтому проверяется сам механизм наследования, а не sklearn."""
    result = asyncio.run(sandbox.run_code(
        'import socket, ssl, asyncio\n'
        'class MySocket(socket.socket):\n'
        '    pass\n'
        'print(issubclass(ssl.SSLSocket, socket.socket), MySocket.__name__)\n'
        'result = {"ssl": True}\n'
    ))
    assert result["ok"] is True, result["error"]
    assert "True MySocket" in result["stdout"]


def test_sandbox_kills_runaway_code(sandbox, monkeypatch):
    """Бесконечный цикл останавливается по таймауту: на сервере нет swap,
    и неограниченный расчёт означал бы OOM-killer, а не замедление."""
    monkeypatch.setattr(compute_sandbox, "WALL_TIMEOUT", 3)
    result = asyncio.run(sandbox.run_code("while True: pass"))
    assert result["ok"] is False
    assert "не уложился" in result["error"]


def test_sandbox_input_files_and_artifacts(sandbox):
    """Данные участника доступны расчёту; обратно возвращается только то,
    что расчёт создал сам (входной файл артефактом не считается)."""
    code = (
        'rows = [l.split(",") for l in open("input.csv").read().strip().splitlines()[1:]]\n'
        'best = max(rows, key=lambda r: float(r[1]))\n'
        'open("out.csv", "w").write("best\\n" + best[0])\n'
        'result = {"best_T": float(best[0])}\n'
    )
    result = asyncio.run(sandbox.run_code(
        code, files={"input.csv": b"T,yield\n220,0.78\n240,0.91\n260,0.83\n"}))
    assert result["ok"], result
    assert result["result"] == {"best_T": 240.0}
    assert [a["name"] for a in result["artifacts"]] == ["out.csv"]


def test_sandbox_absent_is_reported_not_crashed(monkeypatch):
    monkeypatch.setattr(compute_sandbox, "SANDBOX_PYTHON", "/nonexistent/python")
    result = asyncio.run(compute_sandbox.run_code("result = 1"))
    assert result["ok"] is False
    assert "не установлена" in result["error"]


# ---------------------------------------------------------------------------
# Чтение таблиц и сборка документов


USER_ID = "a1b2c3d4e5f6"  # формат платформы: 12 hex (projects.USER_ID_RE)


def _upload(tmp_path, monkeypatch, filename: str, content: bytes) -> str:
    from api.runtime import uploads

    uploads.get_store.cache_clear()
    store = uploads.get_store()
    return store.save(USER_ID, filename, content)["id"]


def test_read_data_file_describes_csv(tmp_path, monkeypatch):
    """Модель получает паспорт таблицы (колонки, типы, диапазоны, образец),
    а не сам файл: таблица на 5000 строк не должна занимать весь контекст."""
    from api.runtime.engineering_tools import read_data_file

    att = _upload(tmp_path, monkeypatch, "опыты.csv",
                  "T,выход,катализатор\n180,0.62,NaOH\n190,0.78,NaOH\n200,0.71,KOH\n"
                  .encode("utf-8"))
    result = asyncio.run(read_data_file({"attachment_id": att}, {"session": {"user_id": USER_ID}}))

    assert result["ok"], result
    sheet = result["листы"][0]
    assert sheet["строк"] == 3
    columns = {c["название"]: c for c in sheet["колонки"]}
    assert columns["T"]["тип"] == "число"
    assert columns["T"]["мин"] == 180 and columns["T"]["макс"] == 200
    assert columns["катализатор"]["тип"] == "текст"
    assert "NaOH" in columns["катализатор"]["примеры"]


def test_read_data_file_rejects_unknown_format(tmp_path, monkeypatch):
    from api.runtime.engineering_tools import read_data_file

    att = _upload(tmp_path, monkeypatch, "статья.pdf", b"%PDF-1.4 fake")
    result = asyncio.run(read_data_file({"attachment_id": att}, {"session": {"user_id": USER_ID}}))
    assert result["ok"] is False
    assert "Формат не поддерживается" in result["error"]


def test_make_document_produces_downloadable_file(tmp_path, monkeypatch):
    """Участник уносит файл, а не ссылку на переписку."""
    pytest.importorskip("docx")
    from api.runtime.engineering_tools import make_document

    result = asyncio.run(make_document({
        "format": "docx",
        "title": "Карта партии",
        "blocks": [
            {"type": "heading", "text": "Состав"},
            {"type": "table", "columns": ["Компонент", "Масса, г"],
             "rows": [{"Компонент": "ЭД-20", "Масса, г": "8640"}]},
            {"type": "list", "items": ["вакуумирование 15 мин"]},
        ],
    }, {"session": {"user_id": USER_ID}}))

    assert result["ok"], result
    assert result["файл"].endswith(".docx")
    assert result["скачать"].startswith("/api/chat/attachments/")


def test_make_document_requires_login():
    from api.runtime.engineering_tools import make_document

    result = asyncio.run(make_document({"title": "x", "blocks": []}, {"session": {}}))
    assert result["ok"] is False
    assert "вход" in result["error"].lower()


def test_tabular_attachment_tells_model_how_to_use_it():
    """Таблицу нельзя подмешать в контекст текстом, но и объявлять её
    «только хранение и скачивание» нельзя — модель решит, что файл
    бесполезен, и сценарий «загрузите свой Excel» молча сломается."""
    from api.runtime.uploads import attachment_context_block

    block = attachment_context_block(
        [{"filename": "опыты.xlsx", "id": "att-123"}, {"filename": "схема.docx", "id": "att-456"}],
        ["", ""],
    )
    assert "read_data_file" in block and "run_calc" in block
    assert "att-123" in block
    assert "только хранение и скачивание" in block, "для не-табличных подпись прежняя"


def test_sandbox_frees_slot_after_timeout_and_error(sandbox, monkeypatch):
    """Слот в очереди освобождается при любом исходе.

    Без finally один зависший или упавший расчёт навсегда съедал бы место:
    при MAX_PARALLEL=2 двух таких хватило бы, чтобы расчёты перестали
    работать у всех до перезапуска сервиса.
    """
    monkeypatch.setattr(compute_sandbox, "WALL_TIMEOUT", 2)
    asyncio.run(sandbox.run_code("while True: pass"))   # таймаут
    asyncio.run(sandbox.run_code("1 / 0"))              # ошибка
    result = asyncio.run(sandbox.run_code("result = 1"))
    assert result["ok"], "после таймаута и ошибки расчёты должны работать"


def test_sandbox_refuses_instead_of_waiting_forever(sandbox, monkeypatch):
    """Переполненная очередь отвечает отказом, а не молчаливым ожиданием:
    участник должен видеть причину, а не пустой экран до обрыва соединения."""
    monkeypatch.setattr(compute_sandbox, "WALL_TIMEOUT", 10)
    monkeypatch.setattr(compute_sandbox, "QUEUE_TIMEOUT", 1)

    async def scenario():
        busy = [asyncio.create_task(sandbox.run_code("while True: pass"))
                for _ in range(compute_sandbox.MAX_PARALLEL)]
        await asyncio.sleep(0.5)
        result = await sandbox.run_code("result = 1")
        for task in busy:
            task.cancel()
        return result

    result = asyncio.run(scenario())
    assert result["ok"] is False
    assert "слот не освободился" in result["error"]


def test_isolation_mode_is_probed_not_assumed(monkeypatch):
    """Режим изоляции определяется пробным запуском, а не наличием бинарника.

    На Ubuntu 24.04 bubblewrap установлен и запускается, но создать user
    namespace не может (kernel.apparmor_restrict_unprivileged_userns=1) и
    падает с «setting up uid map: Permission denied». Проверка по
    `which bwrap` уверенно сообщала бы о работающей изоляции там, где её нет.
    """
    compute_sandbox._isolation_mode.cache_clear()
    monkeypatch.setattr(compute_sandbox.shutil, "which", lambda name: "/usr/bin/bwrap")
    monkeypatch.setattr(compute_sandbox, "SANDBOX_USER", "")

    calls: list[list[str]] = []

    class Failed:
        returncode = 1
        stdout = b""
        stderr = b"bwrap: setting up uid map: Permission denied"

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return Failed()

    monkeypatch.setattr(compute_sandbox.subprocess, "run", fake_run)
    try:
        assert compute_sandbox._isolation_mode() == "none"
        assert calls and calls[0][0] == "bwrap", "проба bwrap должна была выполниться"
    finally:
        compute_sandbox._isolation_mode.cache_clear()


def test_isolation_report_names_the_actual_mode(sandbox):
    """Режим должен быть виден снаружи: он идёт в эксплуатационную
    документацию и на страницу администратора, а не подразумевается."""
    compute_sandbox._isolation_mode.cache_clear()
    try:
        report = compute_sandbox.isolation_report()
        assert report["mode"] in ("bwrap", "user", "none")
        assert report["описание"]
        assert report["лимиты"]["одновременно"] == compute_sandbox.MAX_PARALLEL
    finally:
        compute_sandbox._isolation_mode.cache_clear()


def test_calc_artifacts_come_back_as_links_not_base64(sandbox, tmp_path, monkeypatch):
    """Файл, созданный расчётом (график, выгрузка), сохраняется во вложения
    и отдаётся модели ССЫЛКОЙ.

    Раньше артефакты собирались и выбрасывались: агент рисовал график, и он
    никуда не попадал. Base64 картинки в ответе модели бесполезен и дорог —
    он занял бы тысячи токенов контекста без всякой пользы.
    """
    from api.runtime import uploads
    from api.runtime.engineering_tools import run_calc

    uploads.get_store.cache_clear()
    code = (
        'open("отчёт.csv", "w", encoding="utf-8").write("T,yield\\n190,78\\n")\n'
        'result = {"строк": 1}\n'
    )
    result = asyncio.run(run_calc({"code": code}, {"session": {"user_id": USER_ID}}))

    assert result["ok"], result
    files = result["созданные_файлы"]
    assert len(files) == 1 and files[0]["сохранён"] is True
    assert files[0]["ссылка"].startswith("/api/chat/attachments/att-")
    assert "base64" not in json.dumps(result, ensure_ascii=False), \
        "содержимое файла не должно попадать в контекст модели"


def test_calc_without_login_reports_unsaved_file(sandbox):
    """Расчёт без владельца не падает, но честно сообщает, что файл не сохранён."""
    from api.runtime.engineering_tools import run_calc

    result = asyncio.run(run_calc(
        {"code": 'open("x.csv","w").write("a\\n")\nresult=1'}, {"session": {}}))
    assert result["ok"]
    assert result["созданные_файлы"][0]["сохранён"] is False
