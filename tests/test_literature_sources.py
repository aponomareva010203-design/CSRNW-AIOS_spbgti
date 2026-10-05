"""Внешние базы literature_search: OpenAlex, arXiv, PubMed (Europe PMC),
Crossref, Semantic Scholar.

Сеть не дёргается — парсеры проверяются на консервированных ответах API
(снятых с живых сервисов), диспетчер — на подставных функциях поиска.
Живая проверка всех пяти источников описана в инструкции по доработке и
выполняется на сервере.
"""

import asyncio
import json

import pytest

from api.runtime.config import get_settings
from api.runtime.tools import build_default_registry
from api.runtime import engineering_tools as et


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIOS_RUNTIME_DIR", str(tmp_path / "rt"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Спека инструмента


def test_literature_search_spec_lists_sources_and_sorts():
    """Спека уходит провайдеру как JSON: enum источника и сортировки должны
    существовать, иначе провайдер вернёт 400 на первый же вызов."""
    registry = build_default_registry(get_settings().runtime_dir / "tools")
    spec = next(s for s in registry.specs()
                if s["function"]["name"] == "literature_search")
    props = spec["function"]["parameters"]["properties"]
    assert props["source"]["enum"] == ["openalex", "arxiv", "pubmed", "crossref",
                                       "semanticscholar", "zenodo", "doaj",
                                       "datacite", "all"]
    assert props["sort"]["enum"] == ["relevance", "citations", "newest"]
    json.dumps(spec)


# ---------------------------------------------------------------------------
# Парсер OpenAlex (регрессия: было в старом literature_search)


def test_openalex_rebuilds_abstract_from_inverted_index():
    data = {
        "meta": {"count": 1},
        "results": [{
            "display_name": "Basalt fiber composites",
            "publication_year": 2024,
            "authorships": [{"author": {"display_name": "A. Ivanov"}},
                            {"author": {"display_name": "B. Petrov"}}],
            "primary_location": {"source": {"display_name": "Polymers"},
                                 "pdf_url": "https://x/y.pdf"},
            "cited_by_count": 7,
            "doi": "https://doi.org/10.1000/x",
            "open_access": {"is_oa": True},
            "abstract_inverted_index": {"Basalt": [0], "fiber": [1], "rules": [2]},
        }],
    }
    works = et._openalex_works(data)
    assert works[0]["аннотация"] == "Basalt fiber rules"
    assert works[0]["журнал"] == "Polymers"
    assert works[0]["doi"] == "10.1000/x"


# ---------------------------------------------------------------------------
# Парсер arXiv (Atom)

ARXIV_XML = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
      xmlns:arxiv="http://arxiv.org/schemas/atom"
      xmlns="http://www.w3.org/2005/Atom">
  <opensearch:totalResults>17</opensearch:totalResults>
  <entry>
    <id>https://arxiv.org/abs/2401.00001v1</id>
    <title>Epoxy/Graphene
      Nanocomposites</title>
    <published>2024-01-05T10:00:00Z</published>
    <summary>  Thermal conductivity
        improves. </summary>
    <author><name>A. Ivanov</name></author>
    <author><name>B. Petrov</name></author>
    <link href="https://arxiv.org/abs/2401.00001v1" rel="alternate" type="text/html"/>
    <link href="https://arxiv.org/pdf/2401.00001v1" rel="related" type="application/pdf"/>
    <arxiv:journal_ref>Composites Part B</arxiv:journal_ref>
    <arxiv:doi>10.1016/j.compositesb.2024.1</arxiv:doi>
  </entry>
  <entry>
    <id>https://arxiv.org/abs/2402.00002v1</id>
    <title>Second entry</title>
    <published>2023-02-01T10:00:00Z</published>
    <summary>Short.</summary>
    <author><name>C. Sidorov</name></author>
    <link href="https://arxiv.org/abs/2402.00002v1" rel="alternate" type="text/html"/>
  </entry>
</feed>
"""


def test_arxiv_parser_reads_atom_feed():
    works, total = et._arxiv_works(ARXIV_XML)
    assert total == 17
    first = works[0]
    assert first["название"] == "Epoxy/Graphene Nanocomposites"  # переносы убраны
    assert first["год"] == 2024
    assert first["авторы"] == ["A. Ivanov", "B. Petrov"]
    assert first["журнал"] == "Composites Part B"
    assert first["doi"] == "10.1016/j.compositesb.2024.1"
    assert first["pdf"] == "https://arxiv.org/pdf/2401.00001v1"
    assert first["открытый_доступ"] is True
    assert first["ссылка"] == "https://arxiv.org/abs/2401.00001v1"
    assert works[1]["журнал"] == "arXiv (препринт)"  # без journal_ref
    assert works[1]["pdf"] == ""


def test_arxiv_query_terms_are_anded():
    """Фразы в кавычках и склейки без поля на живом arXiv дают ноль —
    рабочий синтаксис: каждое слово со своим all: через AND."""
    assert et._arxiv_query("basalt fiber epoxy") == "all:basalt AND all:fiber AND all:epoxy"


def test_arxiv_search_filters_by_year():
    """year_from у arXiv применяется к выдаче: старые записи отбрасываются."""

    class Resp:
        status_code = 200
        text = ARXIV_XML

    async def fake_get(self, url, **kwargs):
        return Resp()

    orig_get = et.httpx.AsyncClient.get
    et.httpx.AsyncClient.get = fake_get
    try:
        r = asyncio.run(et._search_arxiv({"year_from": 2024, "limit": 10},
                                         "epoxy graphene", 10))
    finally:
        et.httpx.AsyncClient.get = orig_get
    assert r["ok"] and r["найдено_всего"] == 17
    assert [w["название"] for w in r["работы"]] == ["Epoxy/Graphene Nanocomposites"]


# ---------------------------------------------------------------------------
# Парсер PubMed (Europe PMC, JSON)

EPMC_JSON = {
    "hitCount": 939,
    "resultList": {"result": [
        {"id": "38543424", "source": "MED", "pmid": "38543424",
         "doi": "10.3390/polym16060819",
         "title": "Hygrothermal Aging of Basalt Fiber Composites",
         "authorString": "Li C, Zhang L, Wang H, Song Y, Wang J.",
         "pubYear": "2024", "citedByCount": 3,
         "isOpenAccess": "Y",
         "journalInfo": {"journal": {"title": "Polymers"}},
         "abstractText": "Water absorption of BF/ERCs treated with CeCl<sub>3</sub>…",
         "fullTextUrlList": {"fullTextUrl": [
             {"availability": "Subscription required", "availabilityCode": "Subscription",
              "documentStyle": "pdf", "url": "https://paywalled/pdf"},
             {"availability": "Open access", "availabilityCode": "OA",
              "documentStyle": "pdf", "url": "https://open/pdf"},
         ]}},
        {"id": "PPR1", "source": "PPR", "title": "Preprint &quot;quoted&quot;",
         "abstractText": None, "firstPublicationDate": "2025-06-01"},
    ]},
}


def test_europepmc_parser_strips_tags_and_prefers_oa_pdf():
    works = et._europepmc_works(EPMC_JSON)
    first = works[0]
    assert first["аннотация"].startswith("Water absorption of BF/ERCs treated with CeCl3")  # <sub> снят
    assert first["pdf"] == "https://open/pdf"  # OA-предпочтение, не первый попавшийся
    assert first["авторы"] == ["Li C", "Zhang L", "Wang H", "Song Y", "Wang J."]
    assert first["журнал"] == "Polymers"
    assert first["открытый_доступ"] is True
    assert first["ссылка"] == "https://doi.org/10.3390/polym16060819"
    preprint = works[1]
    assert preprint["название"] == 'Preprint "quoted"'  # сущности развёрнуты
    assert preprint["год"] == 2025  # год из firstPublicationDate
    assert preprint["ссылка"] == "https://europepmc.org/article/PPR/PPR1"


def test_europepmc_open_access_filter_extends_query():
    """open_access_only превращается в часть запроса, а не отбрасывается."""
    seen = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"hitCount": 0, "resultList": {"result": []}}

    async def fake_get(self, url, params=None, **kwargs):
        seen.update(params or {})
        return Resp()

    orig_get = et.httpx.AsyncClient.get
    et.httpx.AsyncClient.get = fake_get
    try:
        r = asyncio.run(et._search_pubmed({"open_access_only": True, "sort": "citations"},
                                          "basalt fiber", 10))
    finally:
        et.httpx.AsyncClient.get = orig_get
    assert r["ok"]
    assert seen["query"] == "basalt fiber AND OPEN_ACCESS:y"
    assert seen["sort"] == "CITED desc"


# ---------------------------------------------------------------------------
# Парсер Crossref (JSON)

CROSSREF_JSON = {
    "message": {
        "total-results": 1009405,
        "items": [
            {"title": ["Epoxy <jats:italic>composites</jats:italic> review"],
             "author": [{"given": "A.", "family": "Ivanov"},
                        {"given": "B.", "family": "Petrov"}],
             "container-title": ["Composites Part B"],
             "published": {"date-parts": [[2024, 5, 1]]},
             "is-referenced-by-count": 42,
             "DOI": "10.1016/j.compositesb.2024.1",
             "abstract": "<jats:p>Graphene <jats:italic>improves</jats:italic> properties.</jats:p>",
             "link": [{"content-type": "application/pdf",
                       "URL": "https://api.elsevier.com/content/article/PII?pdf"}],
             "license": [{"URL": "https://creativecommons.org/licenses/by/4.0/"}],
             "URL": "https://doi.org/10.1016/j.compositesb.2024.1"},
            {"title": ["Old one"], "issued": {"date-parts": [[1991]]},
             "is-referenced-by-count": 0, "DOI": "10.1/old"},
        ],
    },
}


def test_crossref_parser_reads_items():
    works = et._crossref_works(CROSSREF_JSON)
    first = works[0]
    assert first["название"] == "Epoxy composites review"  # jats-теги сняты
    assert first["аннотация"] == "Graphene improves properties."
    assert first["авторы"] == ["A. Ivanov", "B. Petrov"]
    assert first["журнал"] == "Composites Part B"
    assert first["год"] == 2024
    assert first["цитирований"] == 42
    assert first["открытый_доступ"] is True  # creativecommons в license
    assert first["pdf"].endswith("?pdf")
    assert first["ссылка"] == "https://doi.org/10.1016/j.compositesb.2024.1"
    assert works[1]["год"] == 1991  # год из issued, когда нет published
    assert works[1]["открытый_доступ"] is False


# ---------------------------------------------------------------------------
# Парсер Semantic Scholar (JSON)

S2_JSON = {
    "total": 1234,
    "offset": 0,
    "data": [
        {"paperId": "abc123", "title": "Basalt fiber composites: a review",
         "abstract": None, "tldr": {"model": "tldr", "text": "Short summary."},
         "year": 2024, "venue": "Composites Part B", "citationCount": 42,
         "isOpenAccess": True,
         "openAccessPdf": {"url": "https://open/pdf", "status": "GREEN"},
         "externalIds": {"DOI": "10.1000/x", "ArXiv": "2401.00001"},
         "authors": [{"authorId": "1", "name": "A. Ivanov"},
                     {"authorId": "2", "name": "B. Petrov"}],
         "url": "https://www.semanticscholar.org/paper/abc123"},
        {"paperId": "def456", "title": "  Old <i>one</i> ",
         "abstract": "A <b>bold</b> claim.", "year": None,
         "venue": "", "citationCount": 0, "isOpenAccess": False,
         "externalIds": {}, "authors": [], "url": "https://s2/paper/def456"},
    ],
}


def test_semanticscholar_parser_reads_papers():
    works = et._semanticscholar_works(S2_JSON)
    first = works[0]
    assert first["название"] == "Basalt fiber composites: a review"
    assert first["год"] == 2024
    assert first["авторы"] == ["A. Ivanov", "B. Petrov"]
    assert first["журнал"] == "Composites Part B"
    assert first["цитирований"] == 42
    assert first["doi"] == "10.1000/x"
    assert first["открытый_доступ"] is True
    assert first["pdf"] == "https://open/pdf"
    assert first["ссылка"] == "https://doi.org/10.1000/x"
    assert first["аннотация"] == "Short summary."  # TLDR вместо null-аннотации
    second = works[1]
    assert second["название"] == "Old one"  # разметка и пробелы сняты
    assert second["год"] is None
    assert second["журнал"] == ""
    assert second["открытый_доступ"] is False
    assert second["ссылка"] == "https://s2/paper/def456"  # нет DOI — страница S2
    assert second["аннотация"] == "A bold claim."


def test_semanticscholar_params_go_server_side():
    """year_from и open_access_only превращаются в фильтры запроса, сортировки
    — в значения sort Semantic Scholar."""
    seen = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"total": 0, "data": []}

    async def fake_get(self, url, params=None, **kwargs):
        seen.update(params or {})
        return Resp()

    orig_get = et.httpx.AsyncClient.get
    et.httpx.AsyncClient.get = fake_get
    try:
        r = asyncio.run(et._search_semanticscholar(
            {"year_from": 2024, "open_access_only": True, "sort": "citations"},
            "basalt fiber", 10))
        assert r["ok"]
        assert seen["year"] == "2024-"
        assert seen["openAccessPdf"] == "any"
        assert seen["sort"] == "citationCount:desc"
        seen.clear()
        asyncio.run(et._search_semanticscholar({"sort": "newest"},
                                               "basalt fiber", 10))
        assert seen["sort"] == "publicationDate:desc"
    finally:
        et.httpx.AsyncClient.get = orig_get


def test_semanticscholar_429_explains_rate_limit():
    """Без ключа лимит частоты жёсткий: 429 должен объяснять это, а не быть
    безымянной ошибкой."""

    class Resp:
        status_code = 429

        def json(self):
            return {}

    async def fake_get(self, url, params=None, **kwargs):
        return Resp()

    orig_get = et.httpx.AsyncClient.get
    et.httpx.AsyncClient.get = fake_get
    try:
        r = asyncio.run(et._search_semanticscholar({}, "basalt fiber", 10))
    finally:
        et.httpx.AsyncClient.get = orig_get
    assert not r["ok"] and "429" in r["error"] and "лимит" in r["error"]


def test_semanticscholar_403_explains_key(monkeypatch):
    """403 у S2 значит «сервис не пускает сеть/требует ключ» — ошибка должна
    называть и ключ, и переменную, где его задать."""

    class Resp:
        status_code = 403

        def json(self):
            return {}

    async def fake_get(self, url, params=None, **kwargs):
        return Resp()

    orig_get = et.httpx.AsyncClient.get
    et.httpx.AsyncClient.get = fake_get
    try:
        r = asyncio.run(et._search_semanticscholar({}, "basalt fiber", 10))
    finally:
        et.httpx.AsyncClient.get = orig_get
    assert not r["ok"] and "403" in r["error"] and "API_KEY" in r["error"]


def test_semanticscholar_sends_api_key(monkeypatch):
    """Заданный ключ уходит заголовком x-api-key (в конструктор клиента,
    как и User-Agent остальных источников)."""
    seen = {}
    monkeypatch.setenv("AIOS_SEMANTICSCHOLAR_API_KEY", "test-key-123")

    class Resp:
        status_code = 200

        def json(self):
            return {"total": 0, "data": []}

    async def fake_get(self, url, params=None, **kwargs):
        return Resp()

    orig_get = et.httpx.AsyncClient.get
    orig_init = et.httpx.AsyncClient.__init__

    def fake_init(self, *args, **kwargs):
        seen.update(kwargs.get("headers") or {})
        orig_init(self, *args, **kwargs)

    et.httpx.AsyncClient.__init__ = fake_init
    et.httpx.AsyncClient.get = fake_get
    try:
        r = asyncio.run(et._search_semanticscholar({}, "basalt fiber", 10))
    finally:
        et.httpx.AsyncClient.get = orig_get
        et.httpx.AsyncClient.__init__ = orig_init
    assert r["ok"]
    assert seen.get("x-api-key") == "test-key-123"


# ---------------------------------------------------------------------------
# Диспетчер


def test_dispatcher_rejects_empty_and_unknown():
    assert asyncio.run(et.literature_search({"query": "  "}, None))["ok"] is False
    r = asyncio.run(et.literature_search({"query": "x", "source": "scopus"}, None))
    assert r["ok"] is False
    assert "scopus" in r["error"] and "all" in r["доступные"]


def test_dispatcher_clamps_limit():
    """Мусор в limit не роняет вызов: пределы 1..25, иначе — умолчание."""
    seen = {}

    async def fake(args, query, limit):
        seen["limit"] = limit
        return {"ok": True, "запрос": query, "найдено_всего": 0, "работы": [],
                "источник": "OpenAlex"}

    orig = dict(et._LITERATURE_SOURCES)
    et._LITERATURE_SOURCES = {"openalex": fake}
    try:
        for raw, expect in ((0, 10), (999, 25), ("много", 10), (5, 5)):
            asyncio.run(et.literature_search({"query": "x", "limit": raw}, None))
            assert seen["limit"] == expect, raw
    finally:
        et._LITERATURE_SOURCES = orig


def test_all_mode_merges_sources_and_survives_failures(monkeypatch):
    """Один источник упал, второй вернул ошибку API — сводка всё равно
    собирается из остальных, происхождение размечено в каждой работе."""

    async def ok_a(args, query, limit):
        return {"ok": True, "запрос": query, "найдено_всего": 100,
                "работы": [{"название": f"A{i}"} for i in range(limit)],
                "источник": "OpenAlex"}

    async def ok_b(args, query, limit):
        return {"ok": True, "запрос": query, "найдено_всего": 5,
                "работы": [{"название": f"B{i}"} for i in range(limit)],
                "источник": "arXiv"}

    async def api_error(args, query, limit):
        return {"ok": False, "error": "arXiv ответил 503"}

    async def boom(args, query, limit):
        raise RuntimeError("сеть недоступна")

    monkeypatch.setattr(et, "_LITERATURE_SOURCES",
                        {"openalex": ok_a, "arxiv": ok_b,
                         "pubmed": api_error, "crossref": boom})
    r = asyncio.run(et.literature_search(
        {"query": "epoxy", "limit": 2, "source": "all"}, None))
    assert r["ok"], r
    titles = [w["название"] for w in r["работы"]]
    assert titles[:2] == ["A0", "B0"], "базы чередуются, а не идут блоками"
    assert r["работы"][0]["источник"] == "OpenAlex"
    assert r["работы"][1]["источник"] == "arXiv"
    assert r["найдено_всего"] == 105
    assert r["по_источникам"]["PubMed (Europe PMC)"] == {"ошибка": "arXiv ответил 503"}
    assert "сеть недоступна" in r["по_источникам"]["Crossref"]["ошибка"]


def test_all_mode_reports_when_everything_failed(monkeypatch):
    async def down(args, query, limit):
        return {"ok": False, "error": "таймаут"}

    monkeypatch.setattr(et, "_LITERATURE_SOURCES",
                        {"openalex": down, "arxiv": down,
                         "pubmed": down, "crossref": down})
    r = asyncio.run(et.literature_search({"query": "x", "source": "all"}, None))
    assert r["ok"] is False
    assert "не ответил" in r["error"]
    assert len(r["по_источникам"]) == 4
