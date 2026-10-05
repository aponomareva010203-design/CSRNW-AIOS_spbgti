"""Привязка RAG-базы к агенту: поле rag_id в спецификации, в файле .md и проверки."""

from api.runtime import skill_drafts

SPEC = dict(
    id="x-agent", name="Агент", description="описание", domain="область",
    triggers=["теплообменн", "площадь", "перепад"], role="считает", task="выдаёт расчёт",
    steps=[{"title": "Шаг", "text": "Сделайте."}], tools=[{"name": "run_calc", "when": "все числа"}], final="Результат",
)


def test_normalize_keeps_rag_id():
    assert skill_drafts.normalize_spec(dict(SPEC, rag_id="rag-abc123"))["rag_id"] == "rag-abc123"
    assert skill_drafts.normalize_spec(SPEC)["rag_id"] == ""


def test_render_md_writes_rag_id_to_frontmatter():
    with_rag = skill_drafts.render_md(skill_drafts.normalize_spec(dict(SPEC, rag_id="rag-abc123")))
    meta, _body, err = skill_drafts.split_md(with_rag)
    assert err is None and meta["rag_id"] == "rag-abc123"
    without = skill_drafts.render_md(skill_drafts.normalize_spec(SPEC))
    meta2, _b, _e = skill_drafts.split_md(without)
    assert "rag_id" not in meta2


def test_rag_check_rejects_bad_and_unknown_ids():
    assert "некорректный" in skill_drafts.rag_check("!!", "u1")
    assert "не найдена" in skill_drafts.rag_check("rag-doesnotexist-000", "u1")


def test_bound_rag_id_empty_without_binding():
    assert skill_drafts.bound_rag_id([], "u1") == ""
    assert skill_drafts.bound_rag_id(["no-such-agent"], "u1") == ""
