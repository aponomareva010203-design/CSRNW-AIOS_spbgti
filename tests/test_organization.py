"""Раздел «Об организации»: хранилище JSON, админ-эндпоинты, инструменты агента."""

import io
import json
import re

import pytest

from api.runtime.config import get_settings
from api.runtime.loop import grant_pending_permissions
from api.runtime.organization import OrganizationStore

ADMIN_LOGIN = "a.ponomareva@csr-nw.ru"
ADMIN_PASSWORD = "1234"

DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    """Чистое окружение: без LLM из .env, отдельные каталоги рантайма и организации."""
    monkeypatch.delenv("AIOS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("AIOS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("AIOS_LLM_MODEL", raising=False)
    monkeypatch.setenv("AIOS_ADMIN_LOGIN", ADMIN_LOGIN)
    monkeypatch.setenv("AIOS_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    # без SMTP регистрация идёт в dev-режиме: без MX-проверки домена
    monkeypatch.setenv("AIOS_SMTP_HOST", "")
    monkeypatch.setenv("AIOS_ORGANIZATION_DIR", str(tmp_path / "org"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def minimal_pdf(text: str = "Hello AIOS organization") -> bytes:
    """Минимальный корректный PDF с одной страницей и текстом (для pypdf)."""
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref_pos).encode() + b"\n%%EOF\n"
    return out


# ----- хранилище -----


def test_store_creates_templates_and_saves_to_source_json(tmp_path):
    store = OrganizationStore(tmp_path / "org")
    assert not (tmp_path / "org" / "profile.json").exists()

    profile = store.load_profile()
    assert profile["type"] == "university"
    assert [f["id"] for f in profile["fields"]] == ["name", "aliases", "description", "activity_areas"]
    # отсутствующий файл создаётся из шаблона на диске
    assert (tmp_path / "org" / "profile.json").is_file()

    metrics = store.load_metrics()
    assert [m["id"] for m in metrics["items"]] == [
        "rd_income", "faculty_count", "researchers_count", "students_count", "ege_avg",
    ]

    saved = store.save_profile(
        {"name": "Университет", "aliases": ["Унив", "", "  "], "description": "Описание"},
        source="Мониторинг 2025",
    )
    assert saved["fields"][1]["value"] == ["Унив"]  # пустые строки отбрасываются
    assert DATE_RE.match(saved["updated_at"]), saved["updated_at"]
    # изменения попадают в исходный json-файл
    on_disk = json.loads((tmp_path / "org" / "profile.json").read_text(encoding="utf-8"))
    assert on_disk["fields"][0]["value"] == "Университет"
    assert on_disk["source"] == "Мониторинг 2025"
    assert DATE_RE.match(on_disk["updated_at"])

    saved_m = store.save_metrics({"rd_income": "1 250 000,5", "ege_avg": "82,4", "faculty_count": 420})
    values = {m["id"]: m["value"] for m in saved_m["items"]}
    assert values["rd_income"] == 1250000.5
    assert values["ege_avg"] == 82.4
    assert values["faculty_count"] == 420  # integer-показатель округляется до целого

    with pytest.raises(Exception):
        store.save_metrics({"ege_avg": "не число"})


def test_store_documents_roundtrip(tmp_path):
    store = OrganizationStore(tmp_path / "org")
    doc = store.add_document(
        filename="Стратегия.pdf",
        category="strategy",
        title="Стратегия 2030",
        summary="## Осн. содержание",
        data=b"%PDF-1.4 ..." + b"x" * 50,
    )
    assert doc["id"].startswith("doc-")
    stored = store.document_path(doc)
    assert stored.is_file() and stored.parent.name == "objects"

    with pytest.raises(Exception):
        store.add_document(filename="virus.exe", category="other", data=b"MZ")
    with pytest.raises(Exception):
        store.add_document(filename="ok.txt", category="unknown", data=b"x")

    updated = store.update_document(doc["id"], summary="новое описание", category="development")
    assert updated["summary"] == "новое описание" and updated["category"] == "development"
    assert store.update_document("doc-00000000", summary="x") is None

    assert store.delete_document(doc["id"]) is True
    assert not stored.exists()
    assert store.get_document(doc["id"]) is None


# ----- админ-эндпоинты -----


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _reset_clients():
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import auth as auth_router

    for fn in (
        auth_router.get_user_store,
        auth_router.get_token_store,
        auth_router.get_llm_store,
    ):
        fn.cache_clear()
    return TestClient(app), auth_router


def _admin_token(client):
    return client.post(
        "/api/auth/login", json={"login": ADMIN_LOGIN, "password": ADMIN_PASSWORD}
    ).json()["token"]


def test_admin_organization_endpoints(tmp_path):
    client, auth_router = _reset_clients()

    # обычному пользователю раздел закрыт
    client.post("/api/auth/register", json={"login": "u@ex.com", "password": "pass1234", "name": "Юзер Ю.", "consent_pdn": True, "consent_policy": True})
    auth_router.get_user_store().confirm(auth_router.get_user_store().get_by_login("u@ex.com"))
    utoken = client.post(
        "/api/auth/login", json={"login": "u@ex.com", "password": "pass1234"}
    ).json()["token"]
    for method, path in (
        ("get", "/api/admin/organization"),
        ("put", "/api/admin/organization/profile"),
        ("put", "/api/admin/organization/metrics"),
        ("post", "/api/admin/organization/documents"),
        ("put", "/api/admin/organization/documents/doc-00000000"),
        ("delete", "/api/admin/organization/documents/doc-00000000"),
    ):
        r = getattr(client, method)(path, headers=_auth(utoken))
        assert r.status_code == 403, (method, path)

    atoken = _admin_token(client)

    # исходное состояние: шаблон университета
    data = client.get("/api/admin/organization", headers=_auth(atoken)).json()
    assert data["profile"]["type"] == "university"
    assert data["categories"]["strategy"] == "Стратегия"

    # сохранение профиля и показателей пишется в исходные json
    r = client.put(
        "/api/admin/organization/profile",
        json={"values": {"name": "Тестовый университет", "aliases": ["ТУ"], "description": "d"},
              "source": "источник"},
        headers=_auth(atoken),
    )
    assert r.status_code == 200
    assert DATE_RE.match(r.json()["updated_at"])
    r = client.put(
        "/api/admin/organization/metrics",
        json={"values": {"rd_income": 1000, "ege_avg": "80,5"}, "source": "ВПО-1"},
        headers=_auth(atoken),
    )
    assert r.status_code == 200
    disk_p = json.loads((tmp_path / "org" / "profile.json").read_text(encoding="utf-8"))
    disk_m = json.loads((tmp_path / "org" / "metrics.json").read_text(encoding="utf-8"))
    assert disk_p["fields"][0]["value"] == "Тестовый университет"
    assert {m["id"]: m["value"] for m in disk_m["items"]}["ege_avg"] == 80.5

    # нечисловой показатель → 422
    assert client.put(
        "/api/admin/organization/metrics",
        json={"values": {"ege_avg": "abc"}},
        headers=_auth(atoken),
    ).status_code == 422

    # загрузка документа с .md-описанием
    r = client.post(
        "/api/admin/organization/documents",
        data={"category": "strategy", "title": "Стратегия 2030", "summary": "## Содержание"},
        files={"file": ("Стратегия.pdf", io.BytesIO(minimal_pdf()), "application/pdf")},
        headers=_auth(atoken),
    )
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["summary"] == "## Содержание"
    assert doc["file_url"].startswith("/data/organization/objects/")
    assert "stored_name" not in doc
    assert (tmp_path / "org" / "objects").joinpath(doc["file_url"].rsplit("/", 1)[1]).is_file()
    # file_url ссылается на публичную статику /data — в бою это data/organization
    # (в тесте каталог организации перенаправлён во tmp_path; отдачу реального
    # data/organization проверяет test_data_organization_served_statically)

    # неподдерживаемое расширение → 422
    assert client.post(
        "/api/admin/organization/documents",
        data={"category": "other"},
        files={"file": ("bad.exe", io.BytesIO(b"MZ"), "application/octet-stream")},
        headers=_auth(atoken),
    ).status_code == 422

    # правка .md-описания
    r = client.put(
        f"/api/admin/organization/documents/{doc['id']}",
        json={"title": "Стратегия 2035", "category": "development", "summary": "обновлено"},
        headers=_auth(atoken),
    )
    assert r.status_code == 200 and r.json()["title"] == "Стратегия 2035"

    # удаление убирает и запись, и файл
    assert client.delete(
        f"/api/admin/organization/documents/{doc['id']}", headers=_auth(atoken)
    ).status_code == 200
    assert client.get("/api/admin/organization", headers=_auth(atoken)).json()["documents"] == []
    assert client.delete(
        f"/api/admin/organization/documents/{doc['id']}", headers=_auth(atoken)
    ).status_code == 404


def test_data_organization_served_statically():
    """Статика /data раздаёт data/organization — JSON читаются (в т.ч. агентом) без разрешений."""
    client, _ = _reset_clients()
    profile = client.get("/data/organization/profile.json")
    assert profile.status_code == 200
    assert profile.json()["type"] == "university"
    metrics = client.get("/data/organization/metrics.json")
    assert metrics.status_code == 200
    assert len(metrics.json()["items"]) == 5


# ----- инструменты агента -----


def test_agent_tools_organization(tmp_path):
    from api.runtime import organization, tools

    registry = tools.build_default_registry(tmp_path / "sandbox")
    assert "get_organization" in registry.names() and "read_document" in registry.names()

    store = organization.get_store()
    store.save_profile({"name": "Тестовый университет"}, source="тест")
    store.save_metrics({"students_count": 5400}, source="ВПО-1")

    small = store.add_document(
        filename="small.pdf", category="strategy", title="Малый PDF",
        summary="Короткое .md-описание", data=minimal_pdf("Small doc text"),
    )
    # большой PDF (> порога 100 КБ): дописываем хвост после %%EOF
    big_pdf = minimal_pdf("Big doc text") + b"\n%" + b"0" * 150_000 + b"\n"
    big = store.add_document(
        filename="big.pdf", category="development", title="Большой PDF",
        summary="Описание большого документа", data=big_pdf,
    )
    txt = store.add_document(
        filename="notes.txt", category="other", title="Заметки",
        summary="", data="текст документа".encode("utf-8"),
    )
    session = {"id": "s1", "messages": []}

    async def scenario():
        # get_organization: данные читаются без каких-либо разрешений
        res = await registry.call("get_organization", {"section": "all"})
        assert res["ok"] and res["profile"]["fields"][0]["value"] == "Тестовый университет"
        assert {m["id"]: m["value"] for m in res["metrics"]["items"]}["students_count"] == 5400
        assert all("stored_name" not in d for d in res["documents"])

        # 1) read_document без full → .md-описание, разрешение не нужно
        res = await registry.call("read_document", {"document_id": big["id"]})
        assert res["ok"] and res["summary"] == "Описание большого документа"

        # 2) полный большой PDF без разрешения → needs_permission + pending в сессии
        res = await registry.call(
            "read_document", {"document_id": big["id"], "full": True}, ctx={"session": session}
        )
        assert res["ok"] is False and res["needs_permission"] is True
        assert session["doc_permissions"][big["id"]] == "pending"

        # 3) ответ пользователя → pending становится granted → чтение проходит
        assert grant_pending_permissions(session) is True
        assert session["doc_permissions"][big["id"]] == "granted"
        assert grant_pending_permissions(session) is False  # повторно ничего не меняется
        res = await registry.call(
            "read_document",
            {"document_id": big["id"], "full": True, "confirmed": True},
            ctx={"session": session},
        )
        assert res["ok"] and res["pages_total"] == 1 and "Big doc text" in res["content"]

        # 4) маленький PDF читается сразу, без разрешения
        res = await registry.call(
            "read_document", {"document_id": small["id"], "full": True}, ctx={"session": {}}
        )
        assert res["ok"] and "Small doc text" in res["content"]

        # 5) текстовый документ читается полностью
        res = await registry.call("read_document", {"document_id": txt["id"], "full": True})
        assert res["ok"] and res["content"] == "текст документа"

        # 6) несуществующий документ и неизвестная секция
        assert (await registry.call("read_document", {"document_id": "doc-00000000"}))["ok"] is False
        assert (await registry.call("get_organization", {"section": "nope"}))["ok"] is False

    import asyncio

    asyncio.run(scenario())



