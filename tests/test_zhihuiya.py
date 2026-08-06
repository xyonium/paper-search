import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

# tool.py 含连字符路径无法直接包名导入，用 importlib 按路径加载
SPEC = importlib.util.spec_from_file_location(
    "tool", os.path.join(os.path.dirname(__file__), "..", "tool.py")
)
tool_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool_mod)
Tools = tool_mod.Tools


def _user(apikey="", enabled=True):
    return {"valves": SimpleNamespace(zhihuiya_apikey=apikey, zhihuiya_enabled=enabled)}


def test_disabled_when_no_key():
    t = Tools()
    t.valves = Tools.Valves()  # zhihuiya_apikey 默认 ""
    enabled, key = t._zhihuiya_enabled_key(_user())
    assert enabled is False and key == ""


def test_enabled_with_admin_key():
    t = Tools()
    t.valves = Tools.Valves(zhihuiya_apikey="admin-key")
    enabled, key = t._zhihuiya_enabled_key(_user())
    assert enabled is True and key == "admin-key"


def test_user_key_overrides_admin():
    t = Tools()
    t.valves = Tools.Valves(zhihuiya_apikey="admin-key")
    enabled, key = t._zhihuiya_enabled_key(_user(apikey="user-key"))
    assert enabled is True and key == "user-key"


def test_user_switch_off_disables_even_with_key():
    t = Tools()
    t.valves = Tools.Valves(zhihuiya_apikey="admin-key")
    enabled, key = t._zhihuiya_enabled_key(_user(apikey="user-key", enabled=False))
    assert enabled is False


import json
from unittest.mock import AsyncMock, MagicMock, patch


def _fake_session(call_result):
    session = AsyncMock()
    session.initialize = AsyncMock()
    session.call_tool = AsyncMock(return_value=call_result)
    return session


def _text_content(payload):
    c = MagicMock()
    c.text = json.dumps(payload)
    return c


@pytest.mark.asyncio
async def test_zhihuiya_call_parses_json_text():
    t = Tools()
    payload = {"success": True, "data": {"results": [{"paper_id": "p1"}]},
               "error_code": 0, "error_msg": ""}
    result = MagicMock()
    result.isError = False
    result.content = [_text_content(payload)]
    session = _fake_session(result)

    with patch.object(tool_mod, "streamablehttp_client") as m_client, \
         patch.object(tool_mod, "ClientSession") as m_sess:
        m_client.return_value.__aenter__ = AsyncMock(return_value=("r", "w", None))
        m_client.return_value.__aexit__ = AsyncMock(return_value=False)
        m_sess.return_value.__aenter__ = AsyncMock(return_value=session)
        m_sess.return_value.__aexit__ = AsyncMock(return_value=False)

        out = await t._zhihuiya_call("search_literature", {"text": "x", "type": "all"}, "key123")

    assert out["success"] is True
    session.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_zhihuiya_call_raises_on_error_result():
    t = Tools()
    result = MagicMock()
    result.isError = True
    result.content = [_text_content({"error_msg": "apikey not Pass and call failed!"})]
    session = _fake_session(result)

    with patch.object(tool_mod, "streamablehttp_client") as m_client, \
         patch.object(tool_mod, "ClientSession") as m_sess:
        m_client.return_value.__aenter__ = AsyncMock(return_value=("r", "w", None))
        m_client.return_value.__aexit__ = AsyncMock(return_value=False)
        m_sess.return_value.__aenter__ = AsyncMock(return_value=session)
        m_sess.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(RuntimeError):
            await t._zhihuiya_call("search_literature", {"text": "x", "type": "all"}, "bad")


def test_map_paper_merges_search_and_bibliography():
    search_item = {
        "paper_id": "p1",
        "doi": "10.1/abc",
        "title": ["Some Title"],
        "author": ["Doe, John", "Roe, Jane"],
    }
    bib = {
        "publication_year": "2021",
        "publication": "Nature",
        "abstract": [{"lang": "EN", "text": "An abstract."}],
        "title": [{"lang": "EN", "text": "Some Title"}],
    }
    out = Tools._zhihuiya_map_paper(search_item, bib)
    assert out["title"] == "Some Title"
    assert out["authors"] == "Doe, John; Roe, Jane"
    assert out["published_date"] == "2021"
    assert out["abstract"] == "An abstract."
    assert out["paper_id"] == "p1"
    assert out["doi"] == "10.1/abc"
    assert out["source"] == "zhihuiya"
    assert out["pdf_url"] == ""
    assert out["citations"] == 0


def test_map_paper_handles_missing_bib_and_list_fields():
    search_item = {"paper_id": "p2", "title": ["T"], "author": []}
    out = Tools._zhihuiya_map_paper(search_item, None)
    assert out["title"] == "T"
    assert out["authors"] == ""
    assert out["abstract"] == ""
    assert out["published_date"] == ""


@pytest.mark.asyncio
async def test_zhihuiya_search_two_step_merges_abstract():
    t = Tools()
    search_resp = {
        "success": True,
        "data": {"results": [
            {"paper_id": "p1", "doi": "10.1/a", "title": ["T1"], "author": ["A"]},
            {"paper_id": "p2", "doi": "10.1/b", "title": ["T2"], "author": ["B"]},
        ]},
    }
    bib_resp = {
        "success": True,
        "data": [
            {"paper_id": "p1", "abstract": [{"lang": "EN", "text": "abs1"}],
             "publication_year": "2020"},
            {"paper_id": "p2", "abstract": [{"lang": "EN", "text": "abs2"}],
             "publication_year": "2021"},
        ],
    }
    calls = []

    async def fake_call(tool_name, args, key, timeout=30):
        calls.append((tool_name, args))
        return search_resp if tool_name == "search_literature" else bib_resp

    t._zhihuiya_call = fake_call
    papers = await t._zhihuiya_search("CRISPR", 2, "key")

    assert [c[0] for c in calls] == ["search_literature", "literature_bibliography"]
    # bibliography 批量一次调用，逗号分隔
    assert calls[1][1]["paper_id"] == "p1,p2"
    assert len(papers) == 2
    assert papers[0]["abstract"] == "abs1"
    assert papers[1]["abstract"] == "abs2"
    assert all(p["source"] == "zhihuiya" for p in papers)


@pytest.mark.asyncio
async def test_zhihuiya_search_skips_bibliography_when_no_ids():
    t = Tools()

    async def fake_call(tool_name, args, key, timeout=30):
        return {"success": True, "data": {"results": []}}

    t._zhihuiya_call = fake_call
    papers = await t._zhihuiya_search("nothing", 5, "key")
    assert papers == []


@pytest.mark.asyncio
async def test_search_papers_merges_zhihuiya_branch():
    t = Tools()
    t.valves = Tools.Valves(zhihuiya_apikey="k")

    backend = {"papers": [{"title": "X", "authors": "a", "published_date": "2020",
                            "source": "arxiv", "paper_id": "1", "doi": "",
                            "citations": 3, "pdf_url": "", "url": "", "abstract": "aa"}],
               "source_results": {"arxiv": 1}, "errors": {}}

    async def fake_zh_search(query, limit, key):
        return [{"title": "Z", "authors": "z", "published_date": "2021",
                 "abstract": "zz", "paper_id": "zp", "doi": "10.1/z",
                 "source": "zhihuiya", "pdf_url": "", "citations": 0, "url": ""}]

    t._mcp_call = lambda *a, **k: backend
    t._zhihuiya_search = fake_zh_search

    out = json.loads(await t.search_papers("q", sources="arxiv,zhihuiya",
                                           __user__=_user()))
    assert out["source_results"]["zhihuiya"] == 1
    sources = {p["source"] for p in out["papers"]}
    assert "zhihuiya" in sources and "arxiv" in sources


@pytest.mark.asyncio
async def test_search_papers_zhihuiya_failure_isolated():
    t = Tools()
    t.valves = Tools.Valves(zhihuiya_apikey="k")
    backend = {"papers": [], "source_results": {"arxiv": 0}, "errors": {}}

    async def boom(query, limit, key):
        raise RuntimeError("智慧芽连接失败: 401")

    t._mcp_call = lambda *a, **k: backend
    t._zhihuiya_search = boom

    out = json.loads(await t.search_papers("q", sources="arxiv,zhihuiya",
                                           __user__=_user()))
    assert "zhihuiya" in out["errors"]
    assert out["source_results"]["zhihuiya"] == 0


@pytest.mark.asyncio
async def test_search_papers_zhihuiya_not_called_when_disabled():
    t = Tools()
    t.valves = Tools.Valves()  # 无 key
    called = []

    async def fake_zh_search(query, limit, key):
        called.append(1)
        return []

    t._mcp_call = lambda *a, **k: {"papers": [], "source_results": {}, "errors": {}}
    t._zhihuiya_search = fake_zh_search

    out = json.loads(await t.search_papers("q", sources="zhihuiya", __user__=_user()))
    assert called == []
    assert "zhihuiya" not in out["source_results"]


@pytest.mark.asyncio
async def test_read_paper_zhihuiya_returns_abstract():
    t = Tools()
    t.valves = Tools.Valves(zhihuiya_apikey="k")
    bib = {"success": True, "data": [{
        "paper_id": "p1",
        "title": [{"lang": "EN", "text": "T"}],
        "abstract": [{"lang": "EN", "text": "Full abstract text."}],
        "publication": "Nature", "publication_year": "2021",
    }]}

    async def fake_call(tool_name, args, key, timeout=30):
        assert tool_name == "literature_bibliography"
        return bib

    t._zhihuiya_call = fake_call
    out = await t.read_paper(source="zhihuiya", paper_id="p1", __user__=_user())
    assert "Full abstract text." in out


@pytest.mark.asyncio
async def test_read_paper_zhihuiya_no_key_returns_hint():
    t = Tools()
    t.valves = Tools.Valves()  # 无 key
    out = json.loads(await t.read_paper(source="zhihuiya", paper_id="p1",
                                        __user__=_user()))
    assert "error" in out


@pytest.mark.asyncio
async def test_read_paper_zhihuiya_noabstract_keeps_specific_error():
    t = Tools()
    t.valves = Tools.Valves(zhihuiya_apikey="k")

    async def fake_call(tool_name, args, key, timeout=30):
        return {"success": True, "data": [{"paper_id": "p1", "abstract": []}]}

    def boom_mcp(*a, **k):
        raise AssertionError("mcpo must not be called for zhihuiya")

    t._zhihuiya_call = fake_call
    t._mcp_call = boom_mcp  # prove no wasted backend call
    out = json.loads(await t.read_paper(source="zhihuiya", paper_id="p1",
                                        __user__=_user()))
    assert "智慧芽无可用 abstract" in out.get("error", "")


@pytest.mark.asyncio
async def test_zhihuiya_call_redacts_apikey_in_error():
    t = Tools()
    with patch.object(tool_mod, "streamablehttp_client") as m_client:
        m_client.side_effect = RuntimeError(
            "httpx.ConnectError: https://connect.zhihuiya.com/eba075/mcp?apikey=secret123"
        )
        with pytest.raises(RuntimeError) as exc:
            await t._zhihuiya_call("search_literature", {"text": "x"}, "secret123")
    msg = str(exc.value)
    assert "apikey=***" in msg
    assert "secret123" not in msg


@pytest.mark.asyncio
async def test_search_papers_keeps_zhihuiya_when_backend_fails():
    t = Tools()
    t.valves = Tools.Valves(zhihuiya_apikey="k")

    def boom_mcp(*a, **k):
        raise RuntimeError("后端 search_papers 挂了")

    async def fake_zh_search(query, limit, key):
        return [{"title": "Z", "authors": "z", "published_date": "2021",
                 "abstract": "zz", "paper_id": "zp", "doi": "10.1/z",
                 "source": "zhihuiya", "pdf_url": "", "citations": 0, "url": ""}]

    t._mcp_call = boom_mcp
    t._zhihuiya_search = fake_zh_search

    out = json.loads(await t.search_papers("q", sources="arxiv,zhihuiya",
                                           __user__=_user()))
    assert "backend" in out["errors"]
    assert out["source_results"]["zhihuiya"] == 1
    assert {p["source"] for p in out["papers"]} == {"zhihuiya"}


@pytest.mark.asyncio
async def test_zhihuiya_search_degrades_when_bibliography_fails():
    t = Tools()
    search_resp = {
        "success": True,
        "data": {"results": [
            {"paper_id": "p1", "doi": "10.1/a", "title": ["T1"], "author": ["A"]},
        ]},
    }

    async def fake_call(tool_name, args, key, timeout=30):
        if tool_name == "search_literature":
            return search_resp
        raise RuntimeError("literature_bibliography 富化失败")

    t._zhihuiya_call = fake_call
    papers = await t._zhihuiya_search("CRISPR", 2, "key")

    assert len(papers) == 1
    assert papers[0]["paper_id"] == "p1"
    assert papers[0]["title"] == "T1"
    assert papers[0]["abstract"] == ""


@pytest.mark.asyncio
async def test_zhihuiya_call_uses_custom_url():
    t = Tools()
    payload = {"status": "success", "data": {"docs": []}}
    result = MagicMock()
    result.isError = False
    result.content = [_text_content(payload)]
    session = _fake_session(result)
    captured = {}

    def fake_client(url):
        captured["url"] = url
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=("r", "w", None))
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    with patch.object(tool_mod, "streamablehttp_client", side_effect=fake_client), \
         patch.object(tool_mod, "ClientSession") as m_sess:
        m_sess.return_value.__aenter__ = AsyncMock(return_value=session)
        m_sess.return_value.__aexit__ = AsyncMock(return_value=False)
        await t._zhihuiya_call("patsnap_search", {"source": "patent"}, "KEY9",
                               url=Tools._PATSNAP_MCP_URL)

    assert captured["url"] == "https://connect.zhihuiya.com/2b0355/logic-mcp?apikey=KEY9"


def test_patsnap_url_constant_distinct_from_zhihuiya():
    assert "2b0355/logic-mcp" in Tools._PATSNAP_MCP_URL
    assert "eba075" in Tools._ZHIHUIYA_MCP_URL


def test_patsnap_map_patent_full():
    doc = {
        "patent_number": "US11530424B1", "title": "CRISPR system",
        "ipc": "C12N15/90", "legal_status": "active",
        "application_date": 20190930, "publication_date": 20221220,
        "cited_count": 5, "jurisdiction": "US",
        "assignees": ["UNIV A", "UNIV B"], "inventors": ["DOE, J."],
        "url": "https://eureka...", "view_url": "https://analytics...",
    }
    out = Tools._patsnap_map_patent(doc)
    assert out["patent_number"] == "US11530424B1"
    assert out["title"] == "CRISPR system"
    assert out["legal_status"] == "active"
    assert out["application_date"] == "20190930"
    assert out["publication_date"] == "20221220"
    assert out["cited_count"] == 5
    assert out["assignees"] == "UNIV A; UNIV B"
    assert out["inventors"] == "DOE, J."
    assert out["jurisdiction"] == "US"
    assert out["url"] == "https://eureka..."


def test_patsnap_map_patent_missing_fields():
    out = Tools._patsnap_map_patent({"patent_number": "X1"})
    assert out["patent_number"] == "X1"
    assert out["title"] == "" and out["assignees"] == ""
    assert out["application_date"] == "" and out["cited_count"] == 0


@pytest.mark.asyncio
async def test_search_patents_returns_mapped():
    t = Tools()
    t.valves = Tools.Valves(zhihuiya_apikey="k")
    resp = {"status": "success",
            "data": {"total_hits": 100, "returned_count": 1, "docs": [
                {"patent_number": "US1", "title": "T", "legal_status": "active",
                 "application_date": 20200101, "assignees": ["A"], "cited_count": 2}]}}

    async def fake_call(tool_name, args, key, timeout=30, url=None):
        assert tool_name == "patsnap_search"
        assert args["source"] == "patent"
        assert args["search_strategy"] == ["semantic"]
        assert url == Tools._PATSNAP_MCP_URL
        return resp

    t._zhihuiya_call = fake_call
    out = json.loads(await t.search_patents("CRISPR", limit=5, __user__=_user()))
    assert out["total_hits"] == 100
    assert out["patents"][0]["patent_number"] == "US1"
    assert out["patents"][0]["assignees"] == "A"


@pytest.mark.asyncio
async def test_search_patents_disabled_no_key():
    t = Tools()
    t.valves = Tools.Valves()  # 无 key
    out = json.loads(await t.search_patents("x", __user__=_user()))
    assert "error" in out


@pytest.mark.asyncio
async def test_read_patent_returns_markdown_truncated():
    t = Tools()
    t.valves = Tools.Valves(zhihuiya_apikey="k")
    big_md = "# Patent Details\n" + ("x" * 30000)
    resp = {"total": 1, "success_count": 1,
            "results": [{"key": "US1", "markdown": big_md}]}
    seen = {}

    async def fake_call(tool_name, args, key, timeout=30, url=None):
        seen.update(args)
        assert tool_name == "patsnap_fetch"
        assert url == Tools._PATSNAP_MCP_URL
        return resp

    t._zhihuiya_call = fake_call
    out = await t.read_patent("US1", max_chars=1000, __user__=_user())
    assert seen["keys"] == ["US1"]
    assert seen["key_type"] == "pn"
    assert seen["module"] == ["basic", "legal"]
    assert out.startswith("# Patent Details")
    assert len(out) <= 1100 and "截断" in out


@pytest.mark.asyncio
async def test_read_patent_disabled_no_key():
    t = Tools()
    t.valves = Tools.Valves()  # 无 key
    out = json.loads(await t.read_patent("US1", __user__=_user()))
    assert "error" in out


def test_variants_strip_quotes_and_boolean():
    v = tool_mod._make_query_variants('"early signal drop" glucose sensor OR biosensor')
    assert v["original"] == '"early signal drop" glucose sensor OR biosensor'
    assert '"' not in v["core"]
    # 布尔运算符（任意大小写）都被剥离，且不误伤其它词
    tokens = v["core"].split()
    assert "or" not in tokens and "and" not in tokens and "not" not in tokens
    assert "early signal drop" in v["core"] and "glucose" in v["core"] and "biosensor" in v["core"]
    # 不误伤含 or/and 子串的词
    v2 = tool_mod._make_query_variants("sensor and standard")
    assert "sensor" in v2["core"].split() and "standard" in v2["core"].split()
    assert "and" not in v2["core"].split()


def test_variants_strip_en_noise_words():
    v = tool_mod._make_query_variants("what are the latest advances in glucose biosensor")
    core = v["core"].lower()
    for noise in ("what", "are", "the", "latest", "advances", "in"):
        assert f" {noise} " not in f" {core} "
    assert "glucose" in core and "biosensor" in core


def test_variants_chinese_noise_and_cjk():
    v = tool_mod._make_query_variants("最新的葡萄糖传感器怎么样")
    assert "最新" not in v["core"] and "怎么样" not in v["core"]
    assert "葡萄糖" in v["core"] and "传感器" in v["core"]


def test_variants_no_change_when_clean():
    v = tool_mod._make_query_variants("electropolymerization glucose sensor")
    assert v["core"] == "electropolymerization glucose sensor"
    assert v["original"] == v["core"]


def test_variants_all_noise_falls_back_to_original():
    v = tool_mod._make_query_variants("what are the")
    assert v["core"]  # 非空
    assert isinstance(v["core"], str)


def test_source_groups():
    assert tool_mod.LITERAL_SOURCES == frozenset({"zhihuiya", "doaj"})
    assert "hal" in tool_mod.DIRECT_SOURCES and "zhihuiya" in tool_mod.DIRECT_SOURCES
    assert "pubmed" in tool_mod.DIRECT_SOURCES and "pmc" in tool_mod.DIRECT_SOURCES


@pytest.mark.asyncio
async def test_hal_search_maps_fields():
    t = Tools()
    hal_resp = {"response": {"docs": [{
        "halId_s": "hal-001", "title_s": ["Glucose Biosensor"],
        "authFullName_s": ["Doe J.", "Roe K."], "abstract_s": ["An abstract."],
        "doiId_s": "10.1/hal", "publicationDateY_i": 2021,
        "fileMain_s": "https://hal/x.pdf", "uri_s": "https://hal/record",
    }]}}
    fake = MagicMock()
    fake.json.return_value = hal_resp
    fake.raise_for_status = MagicMock()

    with patch.object(tool_mod.requests, "get", return_value=fake) as mget:
        papers = await t._hal_search("glucose biosensor", 3)
    assert mget.called
    p = papers[0]
    assert p["paper_id"] == "hal:hal-001"
    assert p["title"] == "Glucose Biosensor"
    assert p["authors"] == "Doe J.; Roe K."
    assert p["published_date"] == "2021"
    assert p["doi"] == "10.1/hal"
    assert p["pdf_url"] == "https://hal/x.pdf"
    assert p["source"] == "hal"


@pytest.mark.asyncio
async def test_hal_search_error_raises():
    t = Tools()
    with patch.object(tool_mod.requests, "get", side_effect=Exception("conn fail")):
        with pytest.raises(RuntimeError):
            await t._hal_search("x", 3)


@pytest.mark.asyncio
async def test_search_papers_splits_literal_vs_semantic():
    t = Tools()
    t.valves = Tools.Valves()
    backend_calls = []

    def fake_mcp(tool, args, timeout=180):
        backend_calls.append(dict(args))
        return {"papers": [], "source_results": {}, "errors": {}}

    async def fake_hal(q, limit):
        return [{"title": "H", "authors": "", "published_date": "2020",
                 "abstract": "", "paper_id": "hal:1", "doi": "",
                 "source": "hal", "pdf_url": "", "citations": 0, "url": ""}]

    t._mcp_call = fake_mcp
    t._hal_search = fake_hal
    # doaj(字面) + openalex(语义) + hal(直连) + zhihuiya 未启用
    out = json.loads(await t.search_papers(
        '"early signal drop" glucose sensor OR biosensor',
        sources="doaj,openalex,hal", __user__=_user()))
    # core!=original → 后端应被调两次：一次 original(语义 openalex)，一次 core(字面 doaj)
    queries = sorted(c["query"] for c in backend_calls)
    srcs = sorted(c["sources"] for c in backend_calls)
    assert len(backend_calls) == 2
    assert any('"early signal drop"' in c["query"] for c in backend_calls)  # original
    assert any('OR' not in c["query"] and '"' not in c["query"] for c in backend_calls)  # core
    # hal 走直连，不进后端 sources
    assert all("hal" not in c["sources"] for c in backend_calls)
    assert out["source_results"]["hal"] == 1


@pytest.mark.asyncio
async def test_search_papers_single_call_when_core_equals_original():
    t = Tools()
    t.valves = Tools.Valves()
    calls = []
    t._mcp_call = lambda tool, args, timeout=180: (calls.append(dict(args)), {"papers": [], "source_results": {}, "errors": {}})[1]
    await t.search_papers("glucose biosensor", sources="openalex,doaj", __user__=_user())
    # 无引号/布尔/噪声 → core==original → 只调一次后端
    assert len(calls) == 1
    assert calls[0]["query"] == "glucose biosensor"


@pytest.mark.asyncio
async def test_search_papers_passes_biorxiv_category():
    t = Tools()
    t.valves = Tools.Valves()
    calls = []
    t._mcp_call = lambda tool, args, timeout=180: (calls.append(dict(args)), {"papers": [], "source_results": {}, "errors": {}})[1]
    await t.search_papers("glucose", sources="biorxiv",
                          biorxiv_category="biochemistry", __user__=_user())
    assert calls[0].get("biorxiv_category") == "biochemistry"


@pytest.mark.asyncio
async def test_hal_search_skips_empty_title_and_id():
    t = Tools()
    hal_resp = {"response": {"docs": [
        {"halId_s": "", "title_s": ["No Id"]},                       # 空 id → 跳过
        {"halId_s": "hal-2", "title_s": []},                          # 空 title → 跳过
        {"halId_s": "hal-3", "title_s": ["Good"], "authFullName_s": ["A"],
         "abstract_s": ["part1", "part2"], "doiId_s": ["10.1/x", "10.1/y"],
         "publicationDateY_i": 2022, "fileMain_s": "", "uri_s": ""},
    ]}}
    fake = MagicMock(); fake.json.return_value = hal_resp; fake.raise_for_status = MagicMock()
    with patch.object(tool_mod.requests, "get", return_value=fake):
        papers = await t._hal_search("q", 5)
    assert len(papers) == 1
    assert papers[0]["paper_id"] == "hal:hal-3"
    assert papers[0]["abstract"] == "part1 part2"   # 多段拼接
    assert papers[0]["doi"] == "10.1/x"             # list 取首


@pytest.mark.asyncio
async def test_search_papers_all_mode_excludes_direct_sources():
    t = Tools()
    t.valves = Tools.Valves()
    calls = []
    t._mcp_call = lambda tool, args, timeout=180: (calls.append(dict(args)), {"papers": [], "source_results": {}, "errors": {}})[1]
    async def fake_hal(q, limit):
        return []
    t._hal_search = fake_hal
    await t.search_papers("glucose biosensor", sources="all", __user__=_user())
    # 后端 sources 不得是裸 "all"（后端 "all" 隐含 hal 等直连源），也不得含直连源
    assert calls, "all_mode 应有后端调用"
    for c in calls:
        assert c["sources"] != "all", "后端不得收到裸 all"
        for direct in ("hal", "zhihuiya", "patsnap"):
            assert direct not in {s.strip() for s in c["sources"].split(",")}


@pytest.mark.asyncio
async def test_all_mode_split_gives_literal_sources_core():
    t = Tools(); t.valves = Tools.Valves()
    calls = []
    t._mcp_call = lambda tool, args, timeout=180: (calls.append(dict(args)), {"papers": [], "source_results": {}, "errors": {}})[1]
    async def fake_hal(q, limit): return []
    t._hal_search = fake_hal
    # 长自然语言 → core!=original → all_mode 拆分
    await t.search_papers("what are the latest advances in zero knowledge proof systems",
                          sources="all", __user__=_user())
    assert len(calls) == 2
    by_q = {c["query"]: c["sources"] for c in calls}
    core_q = [q for q in by_q if "latest" not in q and "what" not in q][0]
    orig_q = [q for q in by_q if q != core_q][0]
    # 字面组用 core，且只含 doaj（iacr 已移出字面组与默认源）
    lit_srcs = set(by_q[core_q].split(","))
    assert lit_srcs == {"doaj"}
    # 语义组用 original，且不含 doaj；iacr 归入语义组
    sem_srcs = set(by_q[orig_q].split(","))
    assert "doaj" not in sem_srcs and "iacr" in sem_srcs
    assert "hal" not in sem_srcs  # 直连源不进后端


@pytest.mark.asyncio
async def test_direct_only_sources_skip_backend():
    t = Tools(); t.valves = Tools.Valves()
    calls = []
    t._mcp_call = lambda tool, args, timeout=180: (calls.append(dict(args)), {"papers": [], "source_results": {}, "errors": {}})[1]
    async def fake_hal(q, limit): return []
    t._hal_search = fake_hal
    await t.search_papers("glucose biosensor", sources="hal", __user__=_user())
    assert calls == []  # 只请直连源 → 不调后端


def test_default_sources_exclude_biorxiv_medrxiv():
    """biorxiv/medrxiv 已移出默认源（学科近30天浏览，非关键词检索，避免误导）。"""
    ds = Tools.UserValves().default_sources
    tokens = {s.strip() for s in ds.split(",")}
    assert "biorxiv" not in tokens and "medrxiv" not in tokens
    # 主力检索源仍在
    for keep in ("arxiv", "pubmed", "semantic", "openalex", "hal"):
        assert keep in tokens


def test_default_sources_include_zenodo_not_ieee():
    """zenodo 已加入默认源（v2.5.3 直连修复）；ieee/firecrawl 在默认列表但配 key/url 才启用。"""
    ds = Tools.UserValves().default_sources
    tokens = {s.strip() for s in ds.split(",")}
    assert "zenodo" in tokens      # 直连修复后可用，加入默认
    assert "ieee" in tokens        # 默认列表成员；未配 key 时静默跳过（want_ieee 双条件）
    assert "firecrawl" in tokens   # 默认列表成员；未配 firecrawl_base_url 时静默跳过
    assert "dblp" in tokens        # 直连修复后仍在默认
    assert "pubmed" in tokens and "pmc" in tokens  # v2.6 直连（绕后端无 timeout 挂起 bug）


def test_distill_core_terms_truncates_long_query():
    q = "initiated chemical vapor deposition iCVD conformal polymer film room temperature biosensor coating"
    core = tool_mod._make_query_variants(q)["core"]
    d = tool_mod._distill_core_terms(core, max_terms=5)
    assert len(d.split()) <= 5
    # 专业词（长词/缩写）应保留
    assert "deposition" in d or "icvd" in d or "chemical" in d
    # 泛化词被砍
    for g in ("coating", "film", "sensor", "room", "temperature", "conformal"):
        assert g not in d.split()


def test_distill_core_terms_short_query_unchanged():
    core = "glucose biosensor"
    assert tool_mod._distill_core_terms(core, max_terms=5) == "glucose biosensor"


def test_distill_preserves_order_and_no_dup():
    core = "plasma polymerization room temperature conformal thin film biomedical coating"
    d = tool_mod._distill_core_terms(core, max_terms=5)
    toks = d.split()
    assert len(toks) == len(set(toks))  # 无重复
    core_toks = core.split()
    idx = [core_toks.index(t) for t in toks]
    assert idx == sorted(idx)  # 原顺序保持


@pytest.mark.asyncio
async def test_search_papers_adds_query_adapted_hint():
    t = Tools(); t.valves = Tools.Valves()
    t._mcp_call = lambda tool, args, timeout=180: {"papers": [], "source_results": {}, "errors": {}}
    async def fake_zh(q, limit, key):
        return []
    t._zhihuiya_search = fake_zh
    t2_valves = _user(apikey="k")
    t.valves = Tools.Valves(zhihuiya_apikey="k")
    long_q = "what are the latest advances in initiated chemical vapor deposition iCVD conformal polymer film coating sensor"
    out = json.loads(await t.search_papers(long_q, sources="zhihuiya,doaj,iacr", __user__=_user()))
    assert "query_adapted" in out
    # 字面源的查询应被截断到 ≤5 词
    for s, qq in out["query_adapted"].items():
        assert len(qq.split()) <= 5


# ---------------- NCBI 直连（pubmed/pmc，v2.6）----------------

_ESEARCH_XML = """<?xml version="1.0"?>
<eSearchResult><Count>2</Count><IdList><Id>111</Id><Id>222</Id></IdList></eSearchResult>"""

_EFETCH_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
<PubmedArticle>
  <MedlineCitation>
    <PMID>111</PMID>
    <Article>
      <ArticleTitle>Continuous <i>glucose</i> monitoring sensor biofouling</ArticleTitle>
      <AuthorList><Author><LastName>Smith</LastName><Initials>J</Initials></Author></AuthorList>
      <Abstract><AbstractText>In vivo biofouling <b>degrades</b> sensor signal.</AbstractText></Abstract>
      <Journal><JournalIssue><PubDate><Year>2021</Year></PubDate></JournalIssue></Journal>
    </Article>
  </MedlineCitation>
  <PubmedData><ArticleIdList>
    <ArticleId IdType="pubmed">111</ArticleId>
    <ArticleId IdType="doi">10.1000/xyz111</ArticleId>
    <ArticleId IdType="pmc">PMC999</ArticleId>
  </ArticleIdList></PubmedData>
</PubmedArticle>
<PubmedArticle>
  <MedlineCitation>
    <PMID>222</PMID>
    <Article>
      <ArticleTitle>Second paper</ArticleTitle>
      <Abstract><AbstractText>abstract two</AbstractText></Abstract>
    </Article>
  </MedlineCitation>
  <PubmedData><ArticleIdList><ArticleId IdType="pubmed">222</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle>
</PubmedArticleSet>"""


class _FakeResp:
    def __init__(self, content, status=200):
        self.content = content.encode()
        self.status_code = status
    def raise_for_status(self):
        if self.status_code >= 400:
            import requests as _rq
            raise _rq.exceptions.HTTPError(f"HTTP {self.status_code}")


def _fake_eutils_get(url, params=None, headers=None, timeout=None):
    """模拟 eutils：esearch 返回 2 个 id，efetch 返回 2 篇文章。"""
    assert url.startswith("http://eutils.ncbi.nlm.nih.gov/")  # 必须走 HTTP 而非 HTTPS
    assert timeout == 20
    if "esearch" in url:
        return _FakeResp(_ESEARCH_XML)
    return _FakeResp(_EFETCH_XML)


@pytest.mark.asyncio
async def test_pubmed_search_direct_parses_results():
    t = Tools(); t.valves = Tools.Valves()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(tool_mod.requests, "get", _fake_eutils_get)
    papers = await t._pubmed_search("glucose sensor", 5)
    monkey.undo()
    assert len(papers) == 2
    p = papers[0]
    assert p["source"] == "pubmed"
    assert p["paper_id"] == "pubmed:111"
    assert p["doi"] == "10.1000/xyz111"
    assert p["pdf_url"].endswith("PMC999/pdf/")
    assert "glucose" in p["title"] and "<i>" not in p["title"]  # 子标签文本已拼接
    assert p["authors"] == "Smith J"
    assert p["published_date"] == "2021"
    assert "biofouling" in p["abstract"] and "<b>" not in p["abstract"]
    # 第二篇无 PMCID → 无 pdf_url，但保留
    assert papers[1]["paper_id"] == "pubmed:222"
    assert papers[1]["pdf_url"] == ""


@pytest.mark.asyncio
async def test_pmc_search_direct_uses_pmcid():
    t = Tools(); t.valves = Tools.Valves()
    monkey = pytest.MonkeyPatch()

    jats = """<?xml version="1.0"?>
<pmc-articleset>
<article><front><article-meta>
  <article-id pub-id-type="pmcid">PMC13437042</article-id>
  <article-id pub-id-type="doi">10.1093/rb/rbae001</article-id>
  <title-group><article-title>Implantable <italic>glucose</italic> sensors: biofouling</article-title></title-group>
  <contrib-group><contrib contrib-type="author"><name><surname>Wang</surname><given-names>Li</given-names></name></contrib></contrib-group>
  <abstract><p>In vivo biofouling limits CGM lifetime.</p></abstract>
  <pub-date><year>2024</year></pub-date>
</article-meta></front></article>
<article><front><article-meta>
  <article-id pub-id-type="doi">10.x/no-pmc</article-id>
  <title-group><article-title>no pmcid should be skipped</article-title></title-group>
</article-meta></front></article>
</pmc-articleset>"""

    def fake_get(url, params=None, headers=None, timeout=None):
        if "esearch" in url:
            return _FakeResp(_ESEARCH_XML)
        return _FakeResp(jats)

    monkey.setattr(tool_mod.requests, "get", fake_get)
    papers = await t._pmc_search("glucose sensor", 5)
    monkey.undo()
    assert len(papers) == 1  # 无 PMCID 的第二篇被跳过
    p = papers[0]
    assert p["source"] == "pmc"
    assert p["paper_id"] == "pmc:PMC13437042"
    assert p["doi"] == "10.1093/rb/rbae001"
    assert "pmc/articles/PMC13437042" in p["url"]
    assert p["pdf_url"].endswith("PMC13437042/pdf/")
    assert "glucose" in p["title"] and "<italic>" not in p["title"]
    assert p["authors"] == "Wang Li"
    assert p["published_date"] == "2024"


@pytest.mark.asyncio
async def test_pubmed_not_sent_to_backend():
    """pubmed/pmc 是直连源：不进后端 sources，后端批次不含它们。"""
    t = Tools(); t.valves = Tools.Valves()
    calls = []
    t._mcp_call = lambda tool, args, timeout=180: (calls.append(dict(args)), {"papers": [], "source_results": {}, "errors": {}})[1]
    t._pubmed_search = lambda q, n, u=None: _async_ret([_paper_pubmed()])
    t._pmc_search = lambda q, n, u=None: _async_ret([])
    out = json.loads(await t.search_papers("glucose sensor biofouling", sources="arxiv,pubmed,pmc"))
    assert calls, "arxiv 应走后端"
    for c in calls:
        assert "pubmed" not in c["sources"] and "pmc" not in c["sources"]
    assert out["source_results"].get("pubmed") == 1
    assert any(p["paper_id"] == "pubmed:111" for p in out["papers"])


def _paper_pubmed():
    return {"title": "x", "authors": "", "published_date": "", "abstract": "",
            "paper_id": "pubmed:111", "doi": "", "source": "pubmed", "pdf_url": "",
            "citations": 0, "url": ""}


async def _async_ret(v):
    return v
