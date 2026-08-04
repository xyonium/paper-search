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
