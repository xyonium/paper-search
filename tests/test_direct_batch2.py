# ---------------- v2.9 直连批次二（semantic/openalex/crossref/europepmc/core/rxiv/iacr）----------------
# 模式照 tests/test_arxiv.py：monkeypatch tool_mod.requests.get。

import importlib.util
import os
import time as _time_mod

import pytest

SPEC = importlib.util.spec_from_file_location(
    "tool", os.path.join(os.path.dirname(__file__), "..", "tool.py")
)
tool_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool_mod)
Tools = tool_mod.Tools


class _FakeResp:
    def __init__(self, payload=None, status=200, text="", headers=None):
        self._payload = payload
        self.status_code = status
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests as _rq
            raise _rq.exceptions.HTTPError(f"HTTP {self.status_code}")


def _mk():
    t = Tools()
    t.valves = Tools.Valves()
    return t


# ---------------- Semantic Scholar ----------------

_S2_JSON = {
    "data": [
        {
            "paperId": "abc123hash",
            "title": "Conformal coatings for biosensors",
            "abstract": "We study iCVD films.",
            "year": 2023,
            "citationCount": 42,
            "authors": [{"name": "Alice A"}, {"name": "Bob B"}],
            "url": "https://www.semanticscholar.org/paper/abc123hash",
            "publicationDate": "2023-05-01",
            "externalIds": {"DOI": "10.1000/xyz", "ArXiv": "2301.00001"},
            "openAccessPdf": {"url": "https://example.org/x.pdf"},
        },
        {
            "paperId": "def456hash",
            "title": "No pdf, disclaimer only",
            "abstract": None,
            "year": 2022,
            "citationCount": 0,
            "authors": [],
            "url": "",
            "publicationDate": None,
            "externalIds": {},
            "openAccessPdf": {"url": None,
                              "disclaimer": "See https://arxiv.org/pdf/2201.00002 for OA pdf"},
        },
    ]
}


async def test_semantic_parses_results():
    t = _mk()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(tool_mod.requests, "get",
                   lambda url, params=None, headers=None, timeout=None: _FakeResp(_S2_JSON))
    papers = await t._semantic_search("conformal coating biosensor", 5)
    monkey.undo()
    assert len(papers) == 2
    p = papers[0]
    assert p["source"] == "semantic"
    assert p["paper_id"] == "semantic:abc123hash"
    assert p["doi"] == "10.1000/xyz"
    assert p["authors"] == "Alice A; Bob B"
    assert p["citations"] == 42
    assert p["published_date"] == "2023-05-01"
    assert p["pdf_url"] == "https://example.org/x.pdf"
    # 第二篇：disclaimer 提取 pdf，publicationDate 缺失回退 year
    assert papers[1]["pdf_url"] == "https://arxiv.org/pdf/2201.00002"
    assert papers[1]["published_date"] == "2022"
    assert papers[1]["url"] == "https://www.semanticscholar.org/paper/def456hash"


async def test_semantic_403_drops_key_and_retries_anonymous():
    t = _mk()
    t.valves.semantic_api_key = "bad-key"
    seen_headers = []

    def fake_get(url, params=None, headers=None, timeout=None):
        seen_headers.append(dict(headers or {}))
        if headers and headers.get("x-api-key"):
            return _FakeResp(status=403)
        return _FakeResp(_S2_JSON)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(tool_mod.requests, "get", fake_get)
    papers = await t._semantic_search("q", 5)
    monkey.undo()
    assert len(seen_headers) == 2
    assert seen_headers[0].get("x-api-key") == "bad-key"
    assert "x-api-key" not in seen_headers[1]  # key 被拒后匿名重试
    assert len(papers) == 2


async def test_semantic_429_honors_retry_after():
    t = _mk()
    calls = {"n": 0}
    sleeps = []

    def flaky(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResp(status=429, headers={"Retry-After": "3"})
        return _FakeResp(_S2_JSON)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(tool_mod.requests, "get", flaky)
    monkey.setattr(_time_mod, "sleep", lambda s: sleeps.append(s))
    papers = await t._semantic_search("q", 5)
    monkey.undo()
    assert calls["n"] == 2
    assert sleeps == [3]  # 用 Retry-After 而非默认退避
    assert len(papers) == 2


# ---------------- OpenAlex ----------------

_OPENALEX_JSON = {
    "results": [
        {
            "id": "https://openalex.org/W1234567",
            "title": "OpenAlex Paper",
            "publication_date": "2021-03-04",
            "cited_by_count": 7,
            "doi": "https://doi.org/10.1234/oa",
            "abstract_inverted_index": {"Hello": [0], "world": [1], "foo": [3], "bar": [2]},
            "authorships": [{"author": {"display_name": "Carol C"}},
                            {"author": {"display_name": "Dan D"}}],
            "primary_location": {"landing_page_url": "https://journal.example/x",
                                 "pdf_url": "https://journal.example/x.pdf"},
            "open_access": {"is_oa": True, "oa_url": "https://oa.example/x.pdf"},
        },
        {
            "id": "https://openalex.org/W999",
            "title": "No primary pdf",
            "publication_date": "2020-01-01",
            "cited_by_count": 0,
            "doi": None,
            "abstract_inverted_index": None,
            "authorships": [],
            "primary_location": None,
            "open_access": {"is_oa": True, "oa_url": "https://oa.example/y"},
        },
    ]
}


async def test_openalex_parses_and_reconstructs_abstract():
    t = _mk()
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(params)
        return _FakeResp(_OPENALEX_JSON)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(tool_mod.requests, "get", fake_get)
    papers = await t._openalex_search("q", 5)
    monkey.undo()
    assert seen["per-page"] == 5
    p = papers[0]
    assert p["paper_id"] == "openalex:W1234567"  # https://openalex.org/ 前缀已剥
    assert p["doi"] == "10.1234/oa"  # https://doi.org/ 前缀已剥
    assert p["abstract"] == "Hello world bar foo"  # 倒排索引按位置重建
    assert p["authors"] == "Carol C; Dan D"
    assert p["citations"] == 7
    assert p["pdf_url"] == "https://journal.example/x.pdf"  # primary_location 优先
    assert p["url"] == "https://journal.example/x"
    # 第二篇：无 primary_location → oa_url 兜底
    assert papers[1]["pdf_url"] == "https://oa.example/y"
    assert papers[1]["abstract"] == ""


# ---------------- Crossref ----------------

_CROSSREF_JSON = {
    "message": {
        "items": [
            {
                "DOI": "10.5555/cr1",
                "title": ["Crossref Title One"],
                "author": [{"given": "Eve", "family": "Eason"},
                           {"family": "FamilyOnly"}],
                "abstract": "<jats:p>Abstract with <jats:italic>tags</jats:italic>.</jats:p>",
                "published": {"date-parts": [[2019, 11, 2]]},
                "is-referenced-by-count": 15,
                "URL": "https://doi.org/10.5555/cr1",
                "link": [{"content-type": "application/pdf",
                          "URL": "https://publisher.example/cr1.pdf"}],
            },
            {
                "DOI": "10.5555/cr2",
                "title": ["Second"],
                "issued": {"date-parts": [[2018]]},
                "is-referenced-by-count": "not-an-int",
            },
        ]
    }
}


async def test_crossref_parses_list_title_jats_dateparts():
    t = _mk()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(tool_mod.requests, "get",
                   lambda url, params=None, headers=None, timeout=None: _FakeResp(_CROSSREF_JSON))
    papers = await t._crossref_search("q", 5)
    monkey.undo()
    assert len(papers) == 2
    p = papers[0]
    assert p["title"] == "Crossref Title One"  # title 是 list，取 [0]
    assert p["paper_id"] == "crossref:10.5555/cr1"
    assert p["authors"] == "Eve Eason; FamilyOnly"
    assert p["abstract"] == "Abstract with tags ."  # JATS 标签已剥离
    assert p["published_date"] == "2019-11-02"
    assert p["citations"] == 15
    assert p["pdf_url"] == "https://publisher.example/cr1.pdf"
    # 第二篇：只有年；citations 非 int → 0
    assert papers[1]["published_date"] == "2018"
    assert papers[1]["citations"] == 0


# ---------------- Europe PMC ----------------

_EUPMC_JSON = {
    "resultList": {
        "result": [
            {
                "id": "33123456", "source": "MED",
                "title": "Europe PMC med paper",
                "authorList": {"author": [{"fullName": "Fred F"}, "Plain String"]},
                "abstractText": "Epi abstract.",
                "doi": "10.1001/eupmc1",
                "pubYear": "2024", "pubMonth": "6", "pubDay": "15",
                "fullTextUrlList": {"fullTextUrl": [
                    {"documentStyle": "html", "url": "https://europepmc.org/articles/x"},
                    {"documentStyle": "pdf", "url": "https://europepmc.org/articles/x.pdf"},
                ]},
            },
            {
                "id": "7654321", "source": "PMC",
                "title": "PMC item",
                "pubYear": "2023",
                "fullTextUrlList": {"fullTextUrl": []},
            },
        ]
    }
}


async def test_europepmc_id_mapping_and_urls():
    t = _mk()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(tool_mod.requests, "get",
                   lambda url, params=None, headers=None, timeout=None: _FakeResp(_EUPMC_JSON))
    papers = await t._europepmc_search("q", 5)
    monkey.undo()
    assert len(papers) == 2
    p = papers[0]
    assert p["paper_id"] == "pmid:33123456"  # MED → pmid:
    assert p["authors"] == "Fred F; Plain String"
    assert p["published_date"] == "2024-06-15"
    assert p["pdf_url"] == "https://europepmc.org/articles/x.pdf"
    assert p["url"] == "https://europepmc.org/articles/x"
    # 第二篇：PMC 源补前缀；无 fullTextUrl → 推导 PMC 落地页
    assert papers[1]["paper_id"] == "pmc:PMC7654321"
    assert papers[1]["url"] == "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7654321/"
    assert papers[1]["published_date"] == "2023"


# ---------------- CORE ----------------

_CORE_JSON = {
    "totalHits": 2, "limit": 5, "offset": 0, "searchId": "x",
    "results": [
        {
            "id": 12345, "title": "CORE paper",
            "authors": [{"name": "Gina G"}, "Plain Author"],
            "abstract": "Core abstract.",
            "doi": "10.6666/core1",
            "publishedDate": "2022-08-09T00:00:00",
            "downloadUrl": "https://core.ac.uk/download/12345.pdf",
            "url": "https://core.ac.uk/works/12345",
        },
        {
            "id": 67890, "title": "CORE no doi no pdf",
            "authors": [], "abstract": "", "doi": None,
            "publishedDate": "2021",
            "downloadUrl": "https://core.ac.uk/download/67890",  # 非 .pdf 结尾
            "fullTextUrls": ["https://repo.example/67890.pdf"],
        },
    ],
}


async def test_core_parses_results():
    t = _mk()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(tool_mod.requests, "get",
                   lambda url, params=None, headers=None, timeout=None: _FakeResp(_CORE_JSON))
    papers = await t._core_search("q", 5)
    monkey.undo()
    assert len(papers) == 2
    p = papers[0]
    assert p["paper_id"] == "core:12345"
    assert p["authors"] == "Gina G; Plain Author"
    assert p["doi"] == "10.6666/core1"
    assert p["published_date"] == "2022-08-09"  # ISO 截断到日
    assert p["pdf_url"] == "https://core.ac.uk/download/12345.pdf"
    # 第二篇：doi None → ""；downloadUrl 非 pdf → fullTextUrls 兜底
    assert papers[1]["doi"] == ""
    assert papers[1]["pdf_url"] == "https://repo.example/67890.pdf"
    assert papers[1]["published_date"] == "2021"


async def test_core_sends_bearer_and_drops_on_401():
    t = _mk()
    t.valves.core_api_key = "core-key"
    seen = []

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.append(dict(headers or {}))
        if headers and headers.get("Authorization"):
            return _FakeResp(status=401)
        return _FakeResp(_CORE_JSON)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(tool_mod.requests, "get", fake_get)
    papers = await t._core_search("q", 5)
    monkey.undo()
    assert seen[0].get("Authorization") == "Bearer core-key"
    assert "Authorization" not in seen[1]  # 401 后匿名重试
    assert len(papers) == 2


# ---------------- bioRxiv / medRxiv ----------------

_RXIV_JSON = {
    "collection": [
        {
            "doi": "10.1101/2026.09.01.123456",
            "title": "Bio  paper\twith whitespace",
            "authors": "Hank H; Ivy I",
            "date": "2026-09-01",
            "category": "bioengineering",
            "abstract": "Rxiv abstract.",
            "version": "2",
        },
        {
            "doi": "10.1101/2026.09.02.999999",
            "title": "No version field",
            "authors": "Solo S",
            "date": "2026-09-02",
            "category": "bioengineering",
            "abstract": "",
        },
    ]
}


async def test_rxiv_browse_parses_and_formats_category():
    t = _mk()
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen["url"] = url
        seen["params"] = params or {}
        return _FakeResp(_RXIV_JSON)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(tool_mod.requests, "get", fake_get)
    papers = await t._rxiv_search("biorxiv", "Cell Biology", 5)
    monkey.undo()
    assert "/details/biorxiv/" in seen["url"]
    assert seen["params"]["category"] == "cell_biology"  # 空格转下划线
    p = papers[0]
    assert p["paper_id"] == "biorxiv:10.1101/2026.09.01.123456"
    assert p["title"] == "Bio paper with whitespace"
    assert p["authors"] == "Hank H; Ivy I"
    assert p["url"] == "https://www.biorxiv.org/content/10.1101/2026.09.01.123456v2"
    assert p["pdf_url"].endswith("v2.full.pdf")
    # 第二篇：version 缺失默认 v1
    assert papers[1]["url"].endswith("v1")


# ---------------- IACR ----------------

_IACR_HTML = """
<html><body>
<div class="ms-lg-4 mt-3 results">
  <div class="mb-4">
    <div class="d-flex"><a title="2026/1892" class="paperlink" href="/2026/1892">2026/1892</a>
      <span class="ms-2"><a href="/2026/1892.pdf">(PDF)</a></span>
      <small class="ms-auto">Last updated: 2026-09-04</small>
    </div>
    <div class="ms-md-4">
      <strong>Dynasaurs: Efficient <mark>zkSNARKs</mark></strong>
      <div class="mt-1"><span class="fst-italic">Mart&iacute; Batista, Carla R&agrave;fols</span></div>
      <p class="mb-0 mt-1 search-abstract">Dynamic zkSNARKs were recently introduced.
Multi-line abstract body.</p>
    </div>
  </div>
  <div class="mb-4">
    <div class="d-flex"><a title="2026/1888" class="paperlink" href="/2026/1888">2026/1888</a></div>
    <div class="ms-md-4"><strong>Second Title</strong></div>
  </div>
</div>
</body></html>
"""


async def test_iacr_regex_parses_html():
    t = _mk()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(tool_mod.requests, "get",
                   lambda url, params=None, headers=None, timeout=None:
                   _FakeResp(text=_IACR_HTML))
    papers = await t._iacr_search("zero knowledge", 5)
    monkey.undo()
    assert len(papers) == 2
    p = papers[0]
    assert p["paper_id"] == "iacr:2026/1892"
    assert p["title"] == "Dynasaurs: Efficient zkSNARKs"  # <mark> 标签已剥
    assert p["authors"] == "Martí Batista; Carla Ràfols"  # HTML 实体已反转义，逗号→分号
    assert p["published_date"] == "2026-09-04"
    assert "Multi-line abstract" in p["abstract"]
    assert p["pdf_url"] == "https://eprint.iacr.org/2026/1892.pdf"
    # 第二篇：缺 authors/abstract/date 不崩
    assert papers[1]["paper_id"] == "iacr:2026/1888"
    assert papers[1]["authors"] == ""


# ---------------- 调度与 read 联动 ----------------

async def test_new_direct_sources_not_sent_to_backend():
    """8 个新直连源全部不进后端 sources；纯直连请求时后端不被调用。"""
    t = _mk()
    calls = []
    t._mcp_call = lambda tool, args, timeout=180: (
        calls.append(dict(args)),
        {"papers": [], "source_results": {}, "errors": {}},
    )[1]

    canned = {
        "api.semanticscholar.org": _FakeResp(_S2_JSON),
        "api.openalex.org": _FakeResp(_OPENALEX_JSON),
        "api.crossref.org": _FakeResp(_CROSSREF_JSON),
        "www.ebi.ac.uk": _FakeResp(_EUPMC_JSON),
        "api.core.ac.uk": _FakeResp(_CORE_JSON),
        "api.biorxiv.org": _FakeResp(_RXIV_JSON),
        "eprint.iacr.org": _FakeResp(text=_IACR_HTML),
    }

    def fake_get(url, params=None, headers=None, timeout=None):
        for host, resp in canned.items():
            if host in url:
                return resp
        raise AssertionError(f"unexpected url {url}")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(tool_mod.requests, "get", fake_get)
    out = tool_mod.json.loads(await t.search_papers(
        "q",
        sources="semantic,openalex,crossref,europepmc,core,biorxiv,iacr",
        biorxiv_category="bioengineering"))
    monkey.undo()
    for c in calls:
        for s in ("semantic", "openalex", "crossref", "europepmc", "core",
                  "biorxiv", "medrxiv", "iacr"):
            assert s not in (c.get("sources") or "")
    for s in ("semantic", "openalex", "crossref", "europepmc", "core",
              "biorxiv", "iacr"):
        assert out["source_results"].get(s) == 2, s


async def test_direct_error_isolated_in_errors():
    """单一直连源失败进 errors，不影响其他源。"""
    t = _mk()
    t._mcp_call = lambda tool, args, timeout=180: {"papers": [], "source_results": {}, "errors": {}}

    def fake_get(url, params=None, headers=None, timeout=None):
        if "api.crossref.org" in url:
            raise ConnectionError("connection reset")
        if "api.openalex.org" in url:
            return _FakeResp(_OPENALEX_JSON)
        raise AssertionError(f"unexpected url {url}")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(tool_mod.requests, "get", fake_get)
    monkey.setattr(_time_mod, "sleep", lambda s: None)
    out = tool_mod.json.loads(await t.search_papers("q", sources="openalex,crossref"))
    monkey.undo()
    assert out["source_results"]["openalex"] == 2
    assert out["source_results"]["crossref"] == 0
    assert "Crossref 检索失败" in out["errors"]["crossref"]


async def test_read_paper_strips_own_source_prefix_for_backend():
    """直连源 paper_id 带自家前缀（semantic:HASH），调后端 read 工具时剥掉。"""
    t = _mk()
    seen = {}

    def fake_mcp(tool, args, timeout=180, _retried=False):
        seen["tool"] = tool
        seen["paper_id"] = args.get("paper_id")
        return "x" * 5000  # 超过 _is_unsupported_msg 的 3000 阈值，视为真实全文

    t._mcp_call = fake_mcp
    out = await t.read_paper(source="semantic", paper_id="semantic:abc123hash")
    assert seen["tool"] == "read_semantic_paper"
    assert seen["paper_id"] == "abc123hash"  # 前缀已剥
    assert out.startswith("x" * 100)
