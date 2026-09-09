"""v2.9.1：AND 语义源逐级放宽（_relax_and）+ dblp 反爬拦截识别。

背景（用户实测 2026-09）：hal/europepmc/pubmed/pmc/openaire/ieee 把空格分隔的
词当 AND，11 词自然语言查询全 0 命中；dblp 上 Anubis 反爬质询页返回 200+HTML，
导致 "Expecting value" 误导性报错。
"""
from unittest.mock import MagicMock

import importlib.util
import os

import pytest

# tool.py 含连字符路径无法直接包名导入，用 importlib 按路径加载
SPEC = importlib.util.spec_from_file_location(
    "tool", os.path.join(os.path.dirname(__file__), "..", "tool.py")
)
tool_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool_mod)


def make_tool():
    return tool_mod.Tools()


# ---------- _relax_and 单元行为 ----------

def test_relax_and_short_query_single_attempt():
    calls = []

    def try_fn(q):
        calls.append(q)
        return [{"title": "x"}]

    out = tool_mod._relax_and("glucose biosensor", try_fn)
    assert out == [{"title": "x"}]
    assert calls == ["glucose biosensor"]  # 2 词 ≤5，一次命中不重试


def test_relax_and_drops_tail_terms_until_hit():
    calls = []

    def try_fn(q):
        calls.append(q)
        # 只有砍到 2 词才有结果
        return [{"title": "x"}] if len(q.split()) <= 2 else []

    out = tool_mod._relax_and("a b c d e", try_fn)
    assert out == [{"title": "x"}]
    assert calls == ["a b c d e", "a b c d", "a b c", "a b"]


def test_relax_and_all_empty_returns_empty():
    calls = []

    def try_fn(q):
        calls.append(q)
        return []

    out = tool_mod._relax_and("a b c", try_fn)
    assert out == []
    assert calls == ["a b c", "a b", "a"]  # 一直砍到 1 词


def test_relax_and_distills_long_query_first(monkeypatch):
    """超过 max_terms 的查询先蒸馏再逐级放宽。"""
    seen = {}

    def fake_distill(q, max_terms=6):
        seen["distilled"] = True
        return "core terms here"

    monkeypatch.setattr(tool_mod, "_distill_core_terms", fake_distill)
    calls = []

    def try_fn(q):
        calls.append(q)
        return [{"title": "x"}]

    out = tool_mod._relax_and("one two three four five six seven eight", try_fn)
    assert seen.get("distilled") is True
    assert out == [{"title": "x"}]
    assert calls == ["core terms here"]


def test_relax_and_empty_query_returns_empty():
    assert tool_mod._relax_and("", lambda q: [{"title": "x"}]) == []
    assert tool_mod._relax_and("   ", lambda q: [{"title": "x"}]) == []


# ---------- hal 走放宽路径（集成：stub requests.get） ----------

def _hal_resp(docs):
    r = MagicMock()
    r.json.return_value = {"response": {"docs": docs}}
    r.raise_for_status = MagicMock()
    return r


@pytest.mark.asyncio
async def test_hal_relaxes_and_query_when_zero(monkeypatch):
    """5 词 AND 0 命中时，砍尾词重试直到有结果；验证实际发出的 query 逐级变短。"""
    t = make_tool()
    sent_queries = []

    def fake_get(url, params=None, **kw):
        sent_queries.append(params.get("q", ""))
        n = len(params.get("q", "").split())
        if n >= 3:
            return _hal_resp([])  # 3+ 词 AND 无结果
        return _hal_resp([{
            "halId_s": "hal-9", "title_s": ["Hydrogel"],
            "authFullName_s": ["A"], "publicationDateY_i": 2022,
        }])

    monkeypatch.setattr(tool_mod.requests, "get", fake_get)
    papers = await t._hal_search("alpha beta gamma delta epsilon", 5)
    assert len(papers) == 1
    assert papers[0]["paper_id"] == "hal:hal-9"
    # 逐级放宽：5 → 4 → 3 → 2（2 词时命中）
    assert sent_queries == [
        "alpha beta gamma delta epsilon",
        "alpha beta gamma delta",
        "alpha beta gamma",
        "alpha beta",
    ]


@pytest.mark.asyncio
async def test_hal_relax_all_empty_returns_empty(monkeypatch):
    t = make_tool()
    monkeypatch.setattr(tool_mod.requests, "get",
                        lambda url, params=None, **kw: _hal_resp([]))
    papers = await t._hal_search("zzz yyy xxx", 5)
    assert papers == []


# ---------- dblp 反爬拦截识别 ----------

@pytest.mark.asyncio
async def test_dblp_antibot_page_raises_clear_error(monkeypatch):
    """200 + text/html（Anubis 质询页）→ 明确的反爬报错，而不是 JSON 解析错误，
    且不做无谓重试（只请求一次）。"""
    t = make_tool()
    calls = []

    def fake_get(url, params=None, headers=None, **kw):
        calls.append(url)
        r = MagicMock()
        r.status_code = 200
        r.headers = {"content-type": "text/html; charset=utf-8"}
        r.text = "<html>Making sure you're not a bot!</html>"
        return r

    monkeypatch.setattr(tool_mod.requests, "get", fake_get)
    with pytest.raises(RuntimeError) as ei:
        await t._dblp_search("zero knowledge proof", 5)
    assert "反爬" in str(ei.value) or "非 JSON" in str(ei.value)
    assert len(calls) == 1  # 持续性拦截不重试


@pytest.mark.asyncio
async def test_dblp_json_200_still_works(monkeypatch):
    """正常 JSON 响应不受反爬检测影响。"""
    t = make_tool()

    def fake_get(url, params=None, headers=None, **kw):
        r = MagicMock()
        r.status_code = 200
        r.headers = {"content-type": "application/json"}
        r.json.return_value = {"result": {"hits": {"hit": [{
            "info": {"title": "ZK Survey", "year": "2023",
                     "authors": {"author": [{"text": "Alice"}]},
                     "doi": "10.1/zk", "url": "https://dblp/x"},
        }]}}}
        return r

    monkeypatch.setattr(tool_mod.requests, "get", fake_get)
    papers = await t._dblp_search("zero knowledge", 5)
    assert len(papers) == 1
    assert papers[0]["title"] == "ZK Survey"
    assert papers[0]["source"] == "dblp"


# ---------- 反爬 → 住宅代理一次性重试（v2.9.2） ----------

def _antibot_page():
    r = MagicMock()
    r.status_code = 200
    r.headers = {"content-type": "text/html; charset=utf-8"}
    r.text = "<html>Making sure you're not a bot!</html>"
    return r


def test_antibot_proxies_helper():
    t = make_tool()
    assert t._antibot_proxies() == {}  # 未配 → 空
    t.valves.antibot_proxy_url = "http://groups-RESIDENTIAL:pw@proxy.apify.com:8000"
    px = t._antibot_proxies()
    assert px["http"].startswith("http://groups-RESIDENTIAL")
    assert px["https"] == px["http"]


@pytest.mark.asyncio
async def test_dblp_antibot_retries_via_proxy_when_configured(monkeypatch):
    """直连被拦 + 配了代理 → 换代理（带浏览器 UA）重试一次并成功。"""
    t = make_tool()
    t.valves.antibot_proxy_url = "http://u:p@proxy.example:8000"
    seen = []

    def fake_get(url, params=None, headers=None, proxies=None, **kw):
        seen.append({"proxies": proxies, "ua": (headers or {}).get("User-Agent", "")})
        if not proxies:
            return _antibot_page()  # 直连被拦
        r = MagicMock()  # 代理出口干净 → 正常 JSON
        r.status_code = 200
        r.headers = {"content-type": "application/json"}
        r.json.return_value = {"result": {"hits": {"hit": [{
            "info": {"title": "Via Proxy", "year": "2024"},
        }]}}}
        return r

    monkeypatch.setattr(tool_mod.requests, "get", fake_get)
    papers = await t._dblp_search("zero knowledge proof", 5)
    assert len(papers) == 1 and papers[0]["title"] == "Via Proxy"
    assert seen[0]["proxies"] is None            # 第一次直连
    assert seen[1]["proxies"]["https"].startswith("http://u:p@")  # 第二次走代理
    assert "Mozilla" in seen[1]["ua"]            # 代理重试换浏览器 UA
    assert len(seen) == 2                        # 代理只重试一轮


@pytest.mark.asyncio
async def test_dblp_antibot_proxy_still_blocked(monkeypatch):
    """代理重试仍被拦 → 明确报错，不无限重试。"""
    t = make_tool()
    t.valves.antibot_proxy_url = "http://u:p@proxy.example:8000"
    calls = []

    def fake_get(url, **kw):
        calls.append(kw.get("proxies"))
        return _antibot_page()

    monkeypatch.setattr(tool_mod.requests, "get", fake_get)
    with pytest.raises(RuntimeError) as ei:
        await t._dblp_search("zero knowledge proof", 5)
    assert "代理重试仍被反爬拦截" in str(ei.value)
    assert len(calls) == 2  # 直连 1 + 代理 1，仅此而已


@pytest.mark.asyncio
async def test_dblp_antibot_no_proxy_suggests_valve(monkeypatch):
    """未配代理时错误信息应引导配置 antibot_proxy_url。"""
    t = make_tool()
    monkeypatch.setattr(tool_mod.requests, "get", lambda url, **kw: _antibot_page())
    with pytest.raises(RuntimeError) as ei:
        await t._dblp_search("zero knowledge proof", 5)
    assert "antibot_proxy_url" in str(ei.value)
