"""Тесты save_record / get_records в обычном чате: записи хранятся по
(пользователь сессии, первый навык сессии) в отдельном корне chat_records."""

import asyncio

import pytest

from api.runtime.config import get_settings
from api.runtime.tools import build_default_registry


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _call(tmp_path, name, args, user="u1", skills=("epoxy-recipe-selector",)):
    registry = build_default_registry(tmp_path / "tools")
    ctx = {"session": {"user_id": user, "skill_ids": list(skills)}}
    return asyncio.run(registry.call(name, args, ctx))


def test_save_and_get_roundtrip(tmp_path):
    saved = _call(tmp_path, "save_record", {"fields": {"рецептура": "R1", "модуль": "3.1"}, "source": "doi:10/x"})
    assert saved["ok"], saved
    got = _call(tmp_path, "get_records", {})
    assert got["ok"] and got["всего"] == 1
    rec = got["записи"][0]
    assert rec["поля"] == {"рецептура": "R1", "модуль": "3.1"}
    assert rec["источник"] == "doi:10/x"


def test_newest_first(tmp_path):
    for name in ("первая", "вторая", "третья"):
        assert _call(tmp_path, "save_record", {"fields": {"имя": name}})["ok"]
    got = _call(tmp_path, "get_records", {"limit": 2})
    assert [r["поля"]["имя"] for r in got["записи"]] == ["третья", "вторая"]
    assert got["всего"] == 3


def test_isolated_between_users_and_skills(tmp_path):
    _call(tmp_path, "save_record", {"fields": {"a": "1"}}, user="u1")
    assert _call(tmp_path, "get_records", {}, user="u2")["всего"] == 0
    assert _call(tmp_path, "get_records", {}, user="u1", skills=("other-skill",))["всего"] == 0
    assert _call(tmp_path, "get_records", {}, user="u1")["всего"] == 1


def test_no_skill_uses_chat_bucket(tmp_path):
    _call(tmp_path, "save_record", {"fields": {"a": "1"}}, skills=())
    assert _call(tmp_path, "get_records", {}, skills=())["всего"] == 1


def test_requires_user(tmp_path):
    res = _call(tmp_path, "save_record", {"fields": {"a": "1"}}, user="")
    assert res["ok"] is False
    assert _call(tmp_path, "get_records", {}, user="")["ok"] is False


def test_empty_fields_rejected(tmp_path):
    assert _call(tmp_path, "save_record", {"fields": {}})["ok"] is False
    assert _call(tmp_path, "save_record", {})["ok"] is False


def test_path_traversal_ids_do_not_escape(tmp_path):
    # небезопасный user_id отклоняется, небезопасный skill_id уходит в корзину chat
    assert _call(tmp_path, "save_record", {"fields": {"a": "1"}}, user="../evil")["ok"] is False
    assert _call(tmp_path, "save_record", {"fields": {"a": "1"}}, skills=("../x",))["ok"]
    assert _call(tmp_path, "get_records", {}, skills=())["всего"] == 1
