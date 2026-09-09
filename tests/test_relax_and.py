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


# ---------- 反爬 → firecrawl 浏览器兜底（v2.9.3） ----------

def _antibot_page():
    r = MagicMock()
    r.status_code = 200
    r.headers = {"content-type": "text/html; charset=utf-8"}
    r.text = "<html>Making sure you're not a bot!</html>"
    return r


_DBLP_JSON = {"result": {"hits": {"hit": [{
    "info": {"title": "Via Firecrawl", "year": "2024",
             "authors": {"author": [{"text": "Bob"}]},
             "doi": "10.1/fc", "url": "https://dblp/y"},
}]}}}


def _wrap_pre(payload: dict) -> str:
    import json as _json
    return f'<html><body><pre>{_json.dumps(payload)}</pre></body></html>'


@pytest.mark.asyncio
async def test_dblp_antibot_falls_back_to_firecrawl(monkeypatch):
    """直连被 Anubis 拦 + 配了 firecrawl_base_url → scrape 的 <pre> JSON 被解析返回。"""
    t = make_tool()
    t.valves.firecrawl_base_url = "http://mcpo:8000/firecrawl"
    calls = []

    monkeypatch.setattr(tool_mod.requests, "get", lambda url, **kw: _antibot_page())

    def fake_mcp(base, tool, args, timeout):
        calls.append({"tool": tool, "args": args})
        assert tool == "firecrawl_scrape"
        assert "dblp.org/search/publ/api" in args["url"]
        assert args["formats"] == ["rawHtml"]
        return {"rawHtml": _wrap_pre(_DBLP_JSON)}

    monkeypatch.setattr(t, "_mcp_call_service_url", fake_mcp)
    papers = await t._dblp_search("zero knowledge proof", 5)
    assert len(papers) == 1
    assert papers[0]["title"] == "Via Firecrawl"
    assert papers[0]["authors"] == "Bob"
    assert len(calls) == 1  # 兜底只调一次


@pytest.mark.asyncio
async def test_dblp_antibot_no_firecrawl_suggests_valve(monkeypatch):
    """未配 firecrawl_base_url 时错误信息应引导配置。"""
    t = make_tool()
    t.valves.firecrawl_base_url = ""
    monkeypatch.setattr(tool_mod.requests, "get", lambda url, **kw: _antibot_page())
    with pytest.raises(RuntimeError) as ei:
        await t._dblp_search("zero knowledge proof", 5)
    assert "firecrawl_base_url" in str(ei.value)
    assert "反爬" in str(ei.value)


@pytest.mark.asyncio
async def test_dblp_antibot_firecrawl_also_fails(monkeypatch):
    """firecrawl 兜底也拿不到 JSON（质询未解）→ 明确报错。"""
    t = make_tool()
    t.valves.firecrawl_base_url = "http://mcpo:8000/firecrawl"
    monkeypatch.setattr(tool_mod.requests, "get", lambda url, **kw: _antibot_page())
    monkeypatch.setattr(t, "_mcp_call_service_url",
                        lambda base, tool, args, timeout: {"rawHtml": "<html>challenge</html>"})
    with pytest.raises(RuntimeError) as ei:
        await t._dblp_search("zero knowledge proof", 5)
    assert "兜底未取到 JSON" in str(ei.value)


@pytest.mark.asyncio
async def test_dblp_antibot_firecrawl_raw_json_passthrough(monkeypatch):
    """Content-Type=application/json 时 firecrawl 原样透传 body（mcpo 侧实测形态），
    无 <pre> 包装也要能解析。"""
    import json as _json
    t = make_tool()
    t.valves.firecrawl_base_url = "http://mcpo:8000/firecrawl"
    monkeypatch.setattr(tool_mod.requests, "get", lambda url, **kw: _antibot_page())
    monkeypatch.setattr(
        t, "_mcp_call_service_url",
        lambda base, tool, args, timeout: {"rawHtml": _json.dumps(_DBLP_JSON)})
    papers = await t._dblp_search("zero knowledge proof", 5)
    assert len(papers) == 1 and papers[0]["title"] == "Via Firecrawl"


# ---------- Google Scholar Apify actor 源（v2.9.3） ----------

_SCHOLAR_ITEMS = [{
    "paper_title": "Graph neural networks",
    "link": "https://www.nature.com/articles/s43586-024-00294-7",
    "snippet": "Graphs are flexible mathematical objects ...",
    "result_id": "bfJWK1lrry4J",
    "publication_info": {
        "summary": "G Corso, H Stark - Nature Reviews, 2024 - nature.com",
        "authors": [{"name": "G Corso"}, {"name": "H Stark"}],
    },
    "inline_links": {"cited_by_total": 691},
}, {
    "error": True, "error_message": "some mode error",  # 错误项应跳过
}]


@pytest.mark.asyncio
async def test_scholar_actor_parses_items(monkeypatch):
    t = make_tool()
    t.valves.apify_rotator_base_url = "http://api-key-rotator:8788"
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen["url"] = url
        seen["body"] = json
        r = MagicMock()
        r.status_code = 201
        r.json.return_value = _SCHOLAR_ITEMS
        return r

    monkeypatch.setattr(tool_mod.requests, "post", fake_post)
    papers = await t._google_scholar_actor_search("graph neural network", 5)
    assert seen["url"].endswith("/v2/acts/johnvc~google-scholar-api/run-sync-get-dataset-items")
    assert seen["body"]["q"] == "graph neural network"
    assert len(papers) == 1  # error 项被跳过
    p = papers[0]
    assert p["title"] == "Graph neural networks"
    assert p["authors"] == "G Corso; H Stark"
    assert p["published_date"] == "2024"  # 从 summary 提取年份
    assert p["citations"] == 691
    assert p["source"] == "google_scholar"
    assert p["paper_id"] == "scholar:bfJWK1lrry4J"
    assert p["url"].startswith("https://www.nature.com")


@pytest.mark.asyncio
async def test_scholar_actor_requires_base_url():
    t = make_tool()
    t.valves.apify_rotator_base_url = ""
    with pytest.raises(RuntimeError) as ei:
        await t._google_scholar_actor_search("q", 3)
    assert "apify_rotator_base_url" in str(ei.value)


@pytest.mark.asyncio
async def test_scholar_actor_replaces_backend_in_dispatch(monkeypatch):
    """配了 rotator base 后：sources=google_scholar 走 actor 直连，后端完全不调。"""
    import json as _json
    t = make_tool()
    t.valves.apify_rotator_base_url = "http://rotator:8788"
    backend_calls = []
    monkeypatch.setattr(t, "_mcp_call",
                        lambda tool, args, timeout=180: backend_calls.append(args)
                        or {"papers": [], "source_results": {}, "errors": {}})

    def fake_post(url, json=None, **kw):
        r = MagicMock()
        r.status_code = 201
        r.json.return_value = _SCHOLAR_ITEMS
        return r

    monkeypatch.setattr(tool_mod.requests, "post", fake_post)
    out = _json.loads(await t.search_papers("graph neural network",
                                            sources="google_scholar"))
    assert out["source_results"]["google_scholar"] == 1
    assert backend_calls == []  # 唯一请求的源走 actor，后端零调用


@pytest.mark.asyncio
async def test_scholar_stays_backend_without_rotator(monkeypatch):
    """未配 rotator base：google_scholar 保持后端路径（回归保护）。"""
    import json as _json
    t = make_tool()
    backend_calls = []

    def fake_mcp(tool, args, timeout=180):
        backend_calls.append(dict(args))
        return {"papers": [{"title": "S", "authors": "", "published_date": "",
                            "abstract": "", "paper_id": "gs_1", "doi": "",
                            "source": "google_scholar", "pdf_url": "",
                            "citations": 0, "url": ""}],
                "source_results": {"google_scholar": 1}, "errors": {}}

    monkeypatch.setattr(t, "_mcp_call", fake_mcp)
    out = _json.loads(await t.search_papers("graph neural network",
                                            sources="google_scholar"))
    assert out["source_results"]["google_scholar"] == 1
    assert backend_calls and "google_scholar" in backend_calls[0]["sources"]


# ---------- Google Scholar firecrawl 首选链（v2.9.4） ----------

# 2026-09-09 实测 firecrawl（mcpo→rotator→官方云）抓回的 scholar 搜索页结构
_SCHOLAR_MD = r"""
Page chrome header text

### [**Graph neural networks**](https://www.nature.com/articles/s43586-024-00294-7)

[G Corso](https://scholar.google.com/citations?user=abc), [H Stark](https://scholar.google.com/citations?user=def)… - Nature Reviews …, 2024 - nature.com

**Graphs** are flexible mathematical objects with a rich history ...

[Save](javascript:void(0)) [Cite](javascript:void(0)) [Cited by 691](https://scholar.google.com/scholar?cites=3364025477392429677&hl=en) [All 3 versions](https://scholar.google.com/scholar?cluster=3364025477392429677&hl=en)

[\[PDF\] arxiv.org](https://arxiv.org/pdf/1901.00596)

### \[HTML\]\[HTML\] [**Second paper title**](https://www.sciencedirect.com/science/article/pii/xxx)

[J Doe](https://scholar.google.com/citations?user=ghi) - Science, 2023 - sciencedirect.com

Another snippet line here.

[Save](javascript:void(0)) [Cited by 12](https://scholar.google.com/scholar?cites=111&hl=en) [All 2 versions](https://scholar.google.com/scholar?cluster=111&hl=en)

### [One **protein** is all you need](https://proceedings.iclr.cc/paper_files/paper/2026/hash/6198-Abstract-Conference.html)

A Bushuiev, R Bushuiev, O Pimenova… - International …, 2026 - proceedings.iclr.cc

… of **protein** **language** **models** to one target **protein** at a time, on …
with well-established **models**, … in **protein** **fitness** **prediction** …

SaveCite [Cited by 6](https://scholar.google.com/scholar?cites=9619944207348205917&hl=en) [All 4 versions](https://scholar.google.com/scholar?cluster=9619944207348205917&hl=en)
"""


def test_parse_scholar_markdown_extracts_entries():
    papers = tool_mod.Tools._parse_scholar_markdown(_SCHOLAR_MD, 10)
    assert len(papers) == 3
    p = papers[0]
    assert p["title"] == "Graph neural networks"
    assert p["authors"] == "G Corso; H Stark"
    assert p["published_date"] == "2024"
    assert p["citations"] == 691
    assert p["pdf_url"] == "https://arxiv.org/pdf/1901.00596"
    assert p["paper_id"] == "scholar:3364025477392429677"
    assert p["source"] == "google_scholar"
    p2 = papers[1]  # 带 \[HTML\] 字面前缀的块也要能解析
    assert p2["title"] == "Second paper title"
    assert p2["published_date"] == "2023"
    assert p2["pdf_url"] == ""
    p3 = papers[2]  # 纯文本作者 + SaveCite 行动线 + 多行 snippet（另一种实测形态）
    assert p3["title"] == "One protein is all you need"
    assert p3["authors"] == "A Bushuiev; R Bushuiev; O Pimenova"
    assert p3["published_date"] == "2026"
    assert p3["citations"] == 6
    assert "protein language models" in p3["abstract"]
    assert "fitness prediction" in p3["abstract"]


def test_parse_scholar_markdown_captcha_raises():
    with pytest.raises(tool_mod._AntiBotBlocked):
        tool_mod.Tools._parse_scholar_markdown(
            "Our systems have detected unusual traffic from your computer network", 5)


def test_parse_scholar_markdown_limit_respected():
    papers = tool_mod.Tools._parse_scholar_markdown(_SCHOLAR_MD, 1)
    assert len(papers) == 1


@pytest.mark.asyncio
async def test_scholar_dispatch_firecrawl_first_no_actor_call(monkeypatch):
    """两个通路都配了：firecrawl 出结果 → actor（requests.post）完全不调。"""
    import json as _json
    t = make_tool()
    t.valves.firecrawl_base_url = "http://mcpo:8000/firecrawl"
    t.valves.apify_rotator_base_url = "http://rotator:8788"
    monkeypatch.setattr(t, "_mcp_call",
                        lambda tool, args, timeout=180:
                        {"papers": [], "source_results": {}, "errors": {}})
    monkeypatch.setattr(t, "_mcp_call_service_url",
                        lambda base, tool, args, timeout: {"markdown": _SCHOLAR_MD})
    post_calls = []
    monkeypatch.setattr(tool_mod.requests, "post",
                        lambda url, **kw: post_calls.append(url))
    out = _json.loads(await t.search_papers("graph neural network",
                                            sources="google_scholar"))
    assert out["source_results"]["google_scholar"] == 3
    assert post_calls == []


@pytest.mark.asyncio
async def test_scholar_dispatch_falls_back_to_actor_on_captcha(monkeypatch):
    """firecrawl 抓到 CAPTCHA 页 → 自动落 Apify actor。"""
    import json as _json
    t = make_tool()
    t.valves.firecrawl_base_url = "http://mcpo:8000/firecrawl"
    t.valves.apify_rotator_base_url = "http://rotator:8788"
    monkeypatch.setattr(t, "_mcp_call",
                        lambda tool, args, timeout=180:
                        {"papers": [], "source_results": {}, "errors": {}})
    monkeypatch.setattr(
        t, "_mcp_call_service_url",
        lambda base, tool, args, timeout:
        {"markdown": "Our systems have detected unusual traffic from your computer network"})

    def fake_post(url, json=None, **kw):
        r = MagicMock()
        r.status_code = 201
        r.json.return_value = _SCHOLAR_ITEMS
        return r

    monkeypatch.setattr(tool_mod.requests, "post", fake_post)
    out = _json.loads(await t.search_papers("graph neural network",
                                            sources="google_scholar"))
    assert out["source_results"]["google_scholar"] == 1
    assert out["papers"][0]["paper_id"] == "scholar:bfJWK1lrry4J"


@pytest.mark.asyncio
async def test_scholar_firecrawl_only_failure_records_error(monkeypatch):
    """只配 firecrawl 且它失败（无 actor 兜底）→ errors 里有明确报错。"""
    import json as _json
    t = make_tool()
    t.valves.firecrawl_base_url = "http://mcpo:8000/firecrawl"
    monkeypatch.setattr(t, "_mcp_call",
                        lambda tool, args, timeout=180:
                        {"papers": [], "source_results": {}, "errors": {}})

    def boom(base, tool, args, timeout):
        raise RuntimeError("firecrawl down")

    monkeypatch.setattr(t, "_mcp_call_service_url", boom)
    out = _json.loads(await t.search_papers("graph neural network",
                                            sources="google_scholar"))
    assert out["source_results"]["google_scholar"] == 0
    assert "firecrawl down" in out["errors"]["google_scholar"]


@pytest.mark.asyncio
async def test_scholar_stays_backend_without_either_valve(monkeypatch):
    """两个 valve 都没配：google_scholar 保持后端路径（v2.9.4 回归保护）。"""
    import json as _json
    t = make_tool()
    t.valves.firecrawl_base_url = ""
    t.valves.apify_rotator_base_url = ""
    backend_calls = []

    def fake_mcp(tool, args, timeout=180):
        backend_calls.append(dict(args))
        return {"papers": [{"title": "S", "authors": "", "published_date": "",
                            "abstract": "", "paper_id": "gs_1", "doi": "",
                            "source": "google_scholar", "pdf_url": "",
                            "citations": 0, "url": ""}],
                "source_results": {"google_scholar": 1}, "errors": {}}

    monkeypatch.setattr(t, "_mcp_call", fake_mcp)
    out = _json.loads(await t.search_papers("graph neural network",
                                            sources="google_scholar"))
    assert out["source_results"]["google_scholar"] == 1
    assert backend_calls and "google_scholar" in backend_calls[0]["sources"]
