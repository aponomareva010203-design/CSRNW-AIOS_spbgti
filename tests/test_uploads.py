"""Вложения рабочего чата: загрузка, контекст сообщения, привязка к карточке.

Хранилище (UploadStore): лимиты размера/типов, изоляция пользователей,
resolve_many (чужие/битые вложения — ошибка). API: POST /api/chat/attachments
— multipart по Bearer; GET .../{id} — скачивание владельцем; POST /api/chat
с attachment_ids подмешивает текст вложений в сообщение пользователя
(проверяется через перехват run_agent_loop). Инструмент attach_project_file:
файл перемещается в files/ карточки, имя дописывается в список, повторный
attach того же вложения невозможен (вложение удаляется). GET
/api/projects/{id}/files/{name}: владельцу — всегда, другим — только файлы
активных карточек.
"""

import asyncio
import io

import pytest

from api.runtime import projects, uploads
from api.runtime.config import get_settings
from api.runtime.tools import build_default_registry
from api.runtime.users import UserStore

ADMIN_LOGIN = "m.kharitonov@csr-nw.ru"
ADMIN_PASSWORD = "1234"

PDF_BYTES = b"%PDF-1.4 minimal"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    """Seed-админ, изолированный runtime-каталог, свежие кэши хранилищ."""
    from api.routers import auth as auth_router

    monkeypatch.setenv("AIOS_ADMIN_LOGIN", ADMIN_LOGIN)
    monkeypatch.setenv("AIOS_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    get_settings.cache_clear()
    projects.get_store.cache_clear()
    uploads.get_store.cache_clear()
    auth_router.get_user_store.cache_clear()
    auth_router.get_token_store.cache_clear()
    auth_router.get_confirmation_store.cache_clear()
    yield
    get_settings.cache_clear()
    projects.get_store.cache_clear()
    uploads.get_store.cache_clear()
    auth_router.get_user_store.cache_clear()
    auth_router.get_token_store.cache_clear()
    auth_router.get_confirmation_store.cache_clear()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def _client():
    from fastapi.testclient import TestClient

    from api.main import app

    return TestClient(app)


def _login(client):
    resp = client.post("/api/auth/login", json={"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _upload(client, token, name="report.pdf", content=PDF_BYTES):
    resp = client.post(
        "/api/chat/attachments",
        headers=auth(token),
        files={"file": (name, io.BytesIO(content), "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ----- хранилище -----

def test_store_limits_and_isolation(tmp_path):
    store = uploads.UploadStore(tmp_path / "uploads")
    meta = store.save("abc123def456", " отчёт.pdf ", PDF_BYTES)
    assert meta["filename"] == "отчёт.pdf"
    assert meta["id"].startswith("att-")
    assert store.get("abc123def456", meta["id"])["size"] == len(PDF_BYTES)

    # чужому пользователю вложение недостижимо
    assert store.get("fff000fff000", meta["id"]) is None

    # таблицы участника принимаются: их читает инструмент read_data_file,
    # через RAG таблица проходит только с потерей структуры
    assert store.save("abc123def456", "опыты.csv", b"T,yield\n180,0.62\n")["id"]
    assert store.save("abc123def456", "прогоны.xlsx", b"PK\x03\x04fake")["id"]
    with pytest.raises(uploads.UploadError):
        store.save("abc123def456", "script.exe", b"MZ")  # тип не поддержан
    with pytest.raises(uploads.UploadError):
        store.save("abc123def456", "big.pdf", b"x" * (uploads.MAX_UPLOAD_BYTES + 1))
    with pytest.raises(uploads.UploadError):
        store.save("abc123def456", "empty.pdf", b"")

    # дубль отфильтрован
    metas = store.resolve_many("abc123def456", [meta["id"], meta["id"]])
    assert len(metas) == 1
    # неизвестный id и превышение лимита — ошибки
    with pytest.raises(uploads.UploadError):
        store.resolve_many("abc123def456", ["att-deadbeefdead"])
    with pytest.raises(uploads.UploadError):
        store.resolve_many(
            "abc123def456", ["att-%010x" % i for i in range(uploads.MAX_ATTACHMENTS_PER_MESSAGE + 1)]
        )


def test_extract_text_pdf_and_txt():
    assert uploads.extract_text("note.txt", "привет".encode()) == "привет"
    assert uploads.extract_text("doc.docx", b"bin") == ""


def test_context_block_contains_ids():
    block = uploads.attachment_context_block(
        [{"id": "att-1234567890ab", "filename": "a.pdf"}],
        ["содержимое" * 10_000],
    )
    assert "att-1234567890ab" in block
    assert "a.pdf" in block
    assert "(текст файла обрезан)" in block
    assert len(block) < uploads.MAX_EXTRACTED_CHARS + 500


# ----- API вложений -----

def test_attachments_api_upload_and_download():
    client = _client()
    # без токена — 401 (вложения привязаны к учётной записи)
    resp = client.post(
        "/api/chat/attachments",
        files={"file": ("a.txt", io.BytesIO(b"hi"), "text/plain")},
    )
    assert resp.status_code == 401

    token = _login(client)
    meta = _upload(client, token, name="a.txt", content="привет".encode())
    assert meta["filename"] == "a.txt"

    # скачивание владельцем; чужой/анонимный — 401/404
    got = client.get(f"/api/chat/attachments/{meta['attachment_id']}", headers=auth(token))
    assert got.status_code == 200
    assert got.content == "привет".encode()
    assert client.get(f"/api/chat/attachments/{meta['attachment_id']}").status_code == 401

    # другой пользователь вложение не видит
    client2 = _client()
    token2 = _second_user_token("u2@csr-nw.ru")
    assert (
        client2.get(f"/api/chat/attachments/{meta['attachment_id']}", headers=auth(token2)).status_code
        == 404
    )


def test_chat_injects_attachment_text(monkeypatch):
    client = _client()
    token = _login(client)
    meta = _upload(client, token, name="note.txt", content="ключевые данные задела".encode())

    captured = {}

    async def fake_loop(**kwargs):
        captured["user_text"] = kwargs["user_text"]
        captured["attachments"] = kwargs.get("user_attachments")
        yield {"type": "done"}

    monkeypatch.setattr("api.routers.chat.run_agent_loop", fake_loop)
    # POST /api/chat без настроенной LLM отвечает 503 — подменяем провайдера
    monkeypatch.setattr("api.routers.chat.get_provider", lambda *a, **k: object())

    resp = client.post(
        "/api/chat",
        headers=auth(token),
        json={"message": "Смотри вложение", "attachment_ids": [meta["attachment_id"]]},
    )
    assert resp.status_code == 200
    # текст вложения подмешан в сообщение пользователя для модели
    assert "Смотри вложение" in captured["user_text"]
    assert "ключевые данные задела" in captured["user_text"]
    assert meta["attachment_id"] in captured["user_text"]
    assert captured["attachments"] == [
        {"attachment_id": meta["attachment_id"], "filename": "note.txt"}
    ]

    # вложение без входа отклоняется, неизвестный id — 422
    resp = client.post("/api/chat", json={"message": "x", "attachment_ids": [meta["attachment_id"]]})
    assert resp.status_code == 401
    resp = client.post(
        "/api/chat",
        headers=auth(token),
        json={"message": "x", "attachment_ids": ["att-deadbeefdead"]},
    )
    assert resp.status_code == 422


# ----- инструмент attach_project_file и файлы карточки -----

def _second_user_token(login):
    """Второй пользователь напрямую в хранилище (регистрация требует e-mail).

    Возвращает Bearer-токен для проверки изоляции: чужие вложения и файлы
    чужих черновиков должны быть недостижимы.
    """
    from api.routers.auth import get_token_store

    users = UserStore(get_settings().runtime_dir / "users")
    user = users.create(login=login, password="123456", name="Второй Тестовый", confirmed=True)
    return get_token_store().issue(user["id"])


def _registry_with_user(monkeypatch, user_id):
    registry = build_default_registry(get_settings().tools_root)

    async def call(name, args):
        return await registry.call(name, args, ctx={"session": {"user_id": user_id}})

    return call


def test_attach_project_file_flow(monkeypatch):
    client = _client()
    token = _login(client)
    users = UserStore(get_settings().runtime_dir / "users")
    user_id = users.get_by_login(ADMIN_LOGIN)["id"]

    meta = _upload(client, token, name="статья.pdf")
    store = projects.get_store()
    card = store.save(user_id, {"title": "Проект с файлами"})
    call = _registry_with_user(monkeypatch, user_id)

    result = asyncio.run(
        call(
            "attach_project_file",
            {"project_id": card["id"], "attachment_id": meta["attachment_id"], "field": "groundwork_files"},
        )
    )
    assert result["ok"] is True, result
    assert result["filename"] == "статья.pdf"

    saved = store.get(user_id, card["id"])
    assert saved["groundwork_files"] == ["статья.pdf"]
    # вложение потрачено: повторная привязка невозможна
    assert uploads.get_store().get(user_id, meta["attachment_id"]) is None

    # неверное поле / чужая карточка / неизвестное вложение
    bad = asyncio.run(call("attach_project_file", {"project_id": card["id"], "attachment_id": meta["attachment_id"], "field": "budget"}))
    assert bad["ok"] is False
    anon = asyncio.run(_registry_with_user(monkeypatch, None)("attach_project_file", {"project_id": card["id"], "attachment_id": "att-000000000000", "field": "groundwork_files"}))
    assert anon["ok"] is False

    # скачивание: владельцу — да; другому — только после active
    dl = client.get(f"/api/projects/{card['id']}/files/статья.pdf", headers=auth(token))
    assert dl.status_code == 200
    assert dl.content == PDF_BYTES

    store.save(user_id, {"project_id": card["id"], "status": "active"})
    # после active файл доступен другим вошедшим (раздел «Проекты» общий)
    client2 = _client()
    token3 = _second_user_token("u3@csr-nw.ru")
    assert client2.get(f"/api/projects/{card['id']}/files/статья.pdf", headers=auth(token3)).status_code == 200


def test_filename_collision_gets_suffix(tmp_path):
    store = projects.ProjectStore(tmp_path / "projects")
    name1 = store.save_file("abc123def456", "prj-12345678", "a.pdf", b"1")
    name2 = store.save_file("abc123def456", "prj-12345678", "a.pdf", b"2")
    assert name1 == "a.pdf"
    assert name2 == "a-2.pdf"
    path = store.file_path("abc123def456", "prj-12345678", "a-2.pdf")
    assert path is not None and path.read_bytes() == b"2"
    # обход пути отклоняется
    assert store.file_path("abc123def456", "prj-12345678", "../a.pdf") is None
