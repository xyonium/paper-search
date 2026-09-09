# ---------------- arXiv 直连（v2.8）----------------
# 模式照 tests/test_zhihuiya.py 的 NCBI 直连段：monkeypatch tool_mod.requests.get。

import importlib.util
import os
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "tool", os.path.join(os.path.dirname(__file__), "..", "tool.py")
)
tool_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool_mod)
Tools = tool_mod.Tools


class _FakeResp:
    def __init__(self, content, status=200):
        self.content = content.encode()
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests as _rq
            raise _rq.exceptions.HTTPError(f"HTTP {self.status_code}")


_ATOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <title>Attention Is All
You Need</title>
    <published>2017-06-12T17:57:34Z</published>
    <summary>  The dominant sequence transduction models are based on
complex recurrent or convolutional neural networks.  </summary>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Noam Shazeer</name></author>
    <arxiv:doi>10.48550/arXiv.1706.03762</arxiv:doi>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2010.11929v2</id>
    <title>An Image is Worth 16x16 Words: Transformers for Image Recognition at Scale</title>
    <published>2020-10-22T17:58:08Z</published>
    <summary>While the Transformer architecture has become the de-facto standard.</summary>
    <author><name>Alexey Dosovitskiy</name></author>
  </entry>
</feed>"""


def _fake_arxiv_get(url, params=None, headers=None, timeout=None):
    assert url == "https://export.arxiv.org/api/query"  # 必须 https（http 被 301）
    assert timeout == 20
    # 查询必须是 all: 字段 AND 组合，不是自然语言整句
    assert params["search_query"].startswith("all:")
    assert " " not in params["search_query"].replace(" AND ", "")
    return _FakeResp(_ATOM_XML)


async def test_arxiv_search_direct_parses_results():
    t = Tools(); t.valves = Tools.Valves()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(tool_mod.requests, "get", _fake_arxiv_get)
    papers = await t._arxiv_search("attention transformer", 5)
    monkey.undo()
    assert len(papers) == 2
    p = papers[0]
    assert p["source"] == "arxiv"
    assert p["paper_id"] == "arxiv:1706.03762"  # v7 版本后缀已剥离
    assert p["doi"] == "10.48550/arXiv.1706.03762"
    assert p["title"] == "Attention Is All You Need"  # 换行已折叠
    assert p["authors"] == "Ashish Vaswani; Noam Shazeer"
    assert p["published_date"] == "2017-06-12"
    assert p["pdf_url"] == "https://arxiv.org/pdf/1706.03762"
    assert p["url"] == "https://arxiv.org/abs/1706.03762"
    assert "convolutional" in p["abstract"]
    # 第二篇无 doi → 空串
    assert papers[1]["paper_id"] == "arxiv:2010.11929"
    assert papers[1]["doi"] == ""


async def test_arxiv_long_query_distilled_to_six_terms():
    t = Tools(); t.valves = Tools.Valves()
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen["q"] = params["search_query"]
        return _FakeResp(_ATOM_XML)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(tool_mod.requests, "get", fake_get)
    await t._arxiv_search(
        "iCVD conformal polymer film coating sensor biomedical implant device flexible substrate",
        5)
    monkey.undo()
    n_terms = len(seen["q"].split(" AND "))
    assert n_terms <= 6, f"长查询应蒸馏到 ≤6 词, 实际 {n_terms}: {seen['q']}"


async def test_arxiv_not_sent_to_backend():
    """arxiv 是直连源：不进后端 sources，后端批次不含它。"""
    t = Tools(); t.valves = Tools.Valves()
    calls = []
    t._papers_call = lambda tool, args, timeout=180: (
        calls.append(dict(args)),
        {"papers": [], "source_results": {}, "errors": {}},
    )[1]
    monkey = pytest.MonkeyPatch()
    monkey.setattr(tool_mod.requests, "get", _fake_arxiv_get)
    out = tool_mod.json.loads(await t.search_papers("attention is all you need", sources="arxiv"))
    monkey.undo()
    for c in calls:
        assert "arxiv" not in (c.get("sources") or "")
    assert out["source_results"].get("arxiv") == 2
    assert any(p["paper_id"] == "arxiv:1706.03762" for p in out["papers"])


async def test_arxiv_error_surfaces_in_errors_not_crash():
    """直连失败进 errors 字段，source_results 记 0，不影响其他源。"""
    t = Tools(); t.valves = Tools.Valves()
    t._papers_call = lambda tool, args, timeout=180: {"papers": [], "source_results": {}, "errors": {}}

    def boom(url, params=None, headers=None, timeout=None, **_):
        raise ConnectionError("connection reset")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(tool_mod.requests, "get", boom)
    monkey.setattr(tool_mod.time, "sleep", lambda s: None) if hasattr(tool_mod, "time") else None
    import time as _time_mod
    monkey.setattr(_time_mod, "sleep", lambda s: None)  # 跳过退避等待
    out = tool_mod.json.loads(await t.search_papers("attention", sources="arxiv"))
    monkey.undo()
    assert out["source_results"]["arxiv"] == 0
    assert "arxiv" in out["errors"]
    assert "arXiv 检索失败" in out["errors"]["arxiv"]


async def test_arxiv_retries_on_429():
    t = Tools(); t.valves = Tools.Valves()
    calls = {"n": 0}

    def flaky(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResp("", status=429)
        return _FakeResp(_ATOM_XML)

    import time as _time_mod
    monkey = pytest.MonkeyPatch()
    monkey.setattr(tool_mod.requests, "get", flaky)
    monkey.setattr(_time_mod, "sleep", lambda s: None)
    papers = await t._arxiv_search("attention", 5)
    monkey.undo()
    assert calls["n"] == 2  # 429 后重试成功
    assert len(papers) == 2


_EMPTY_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
</feed>"""


async def test_arxiv_progressive_relaxation():
    """全词 AND 0 命中时逐级砍尾词放宽，直到有结果（专精查询落在稀有词上）。"""
    t = Tools(); t.valves = Tools.Valves()
    seen = []

    def sparse(url, params=None, headers=None, timeout=None):
        q = params["search_query"]
        seen.append(q)
        # 只有 ≤2 词时才给结果
        if q.count(" AND ") <= 1:
            return _FakeResp(_ATOM_XML)
        return _FakeResp(_EMPTY_FEED)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(tool_mod.requests, "get", sparse)
    papers = await t._arxiv_search("icvd conformal polymer coating", 5)
    monkey.undo()
    assert len(seen) == 3  # 4词 → 3词 → 2词命中
    assert seen[0] == "all:icvd AND all:conformal AND all:polymer AND all:coating"
    assert seen[-1] == "all:icvd AND all:conformal"
    assert len(papers) == 2


async def test_arxiv_all_levels_empty_returns_empty():
    t = Tools(); t.valves = Tools.Valves()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(tool_mod.requests, "get",
                   lambda url, params=None, headers=None, timeout=None, **_:  _FakeResp(_EMPTY_FEED))
    papers = await t._arxiv_search("zzzqQQ nonexistentterm", 5)
    monkey.undo()
    assert papers == []
