"""v2.9.11：sources 里「请求了但不会执行」的源必须回报，不能静默 0 结果。

覆盖：
- B1 patsnap（专利源）误传 → ignored_sources + 提示改用 search_patents
- B2 未知/拼错源 → 回报且不发给后端
- B3 key 门控源（zhihuiya/ieee/firecrawl）未配置：显式请求逐条回报，默认路径只汇总
- B4 跨源去重：只按 DOI 精确匹配（不做标题模糊合并）+ duplicates_removed
- B5 download 端点跟随 papers_service_url
- 新增：非连接/限流类检索错误 → admin_alert（疑似上游接口漂移，转告管理员）
"""
import importlib.util
import json
import os
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "tool", os.path.join(os.path.dirname(__file__), "..", "tool.py"))
tool_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool_mod)
Tools = tool_mod.Tools


def _user(zh_key="", zh_enabled=True, ieee="", fc="", default_sources=None):
    v = SimpleNamespace(zhihuiya_apikey=zh_key, zhihuiya_enabled=zh_enabled,
                        ieee_apikey=ieee, firecrawl_base_url=fc)
    if default_sources is not None:
        v.default_sources = default_sources
    return {"valves": v}


def _no_backend(t):
    calls = []

    def fake(tool, args, timeout=180):
        calls.append(dict(args))
        return {"papers": [], "source_results": {}, "errors": {}}
    t._papers_call = fake
    return calls


def _paper(**kw):
    """构造源原始条目（search_papers 的 _trim_paper 只认 published_date，
    "year" 是它的输出字段，这里做一次翻译，避免测试写成实际不存在的形状）。"""
    p = {"title": "t", "authors": "", "published_date": "2024-01-01",
         "source": "arxiv", "paper_id": "arxiv:1", "doi": "", "citations": 0,
         "pdf_url": "", "url": "", "abstract": ""}
    year = kw.pop("year", None)
    if year is not None:
        p["published_date"] = f"{year}-01-01"
    p.update(kw)
    return p


def _fake_arxiv(t, papers):
    async def fake(q, limit):
        return [dict(p) for p in papers]
    t._arxiv_search = fake


async def _run(t, sources, user=None, query="CRISPR off-target"):
    return json.loads(await t.search_papers(query, 5, sources, "", "", user or {}))


# ---------- B1 patsnap ----------

def test_patsnap_is_patent_only_not_a_paper_source():
    assert "patsnap" in tool_mod.PATENT_ONLY_SOURCES
    assert "patsnap" not in tool_mod.DIRECT_SOURCES
    # 后端不认识 patsnap：既不在直连源，也不在后端源清单里
    assert "patsnap" not in tool_mod._BACKEND_ALL_SOURCES.split(",")


@pytest.mark.asyncio
async def test_sources_patsnap_reports_ignored_instead_of_silent_zero():
    t = Tools(); t.valves = Tools.Valves()
    calls = _no_backend(t)
    out = await _run(t, "patsnap")
    assert out["total"] == 0 and not calls
    hint = out["ignored_sources"]["patsnap"]
    assert "search_patents" in hint and "专利" in hint


@pytest.mark.asyncio
async def test_mixed_request_reports_patsnap_and_keeps_others():
    t = Tools(); t.valves = Tools.Valves()
    calls = _no_backend(t)
    _fake_arxiv(t, [_paper(title="A")])
    out = await _run(t, "patsnap,arxiv")
    assert out["total"] == 1
    assert out["source_results"] == {"arxiv": 1}
    assert "patsnap" in out["ignored_sources"]
    assert not calls


@pytest.mark.asyncio
async def test_patsnap_with_backend_source_sends_only_that_source():
    t = Tools(); t.valves = Tools.Valves()
    calls = _no_backend(t)
    out = await _run(t, "patsnap,doaj")
    assert calls and calls[0]["sources"] == "doaj"
    assert "patsnap" in out["ignored_sources"]


@pytest.mark.asyncio
async def test_patsnap_case_and_spaces():
    t = Tools(); t.valves = Tools.Valves()
    calls = _no_backend(t)
    out = await _run(t, " Patsnap ")
    assert "patsnap" in out["ignored_sources"] and not calls


# ---------- B2 未知源 ----------

@pytest.mark.asyncio
async def test_unknown_source_reported_and_not_sent_to_backend():
    t = Tools(); t.valves = Tools.Valves()
    _no_backend(t)
    _fake_arxiv(t, [_paper(title="A")])
    out = await _run(t, "foo,arxiv")
    assert "未知源" in out["ignored_sources"]["foo"]

    t2 = Tools(); t2.valves = Tools.Valves()
    calls2 = _no_backend(t2)
    await _run(t2, "foo,doaj")
    assert calls2 and calls2[0]["sources"] == "doaj"


@pytest.mark.asyncio
async def test_all_token_in_list_still_means_all_sources():
    """历史行为：'all,arxiv' 里后端会收到 'all'（=全源）。修正后仍按全源执行，
    只在 notes 里说明，绝不降级成"只搜 arxiv"。"""
    t = Tools(); t.valves = Tools.Valves()
    calls = _no_backend(t)
    for name in ("_hal_search", "_dblp_search", "_zenodo_search", "_openaire_search",
                 "_pubmed_search", "_pmc_search", "_arxiv_search", "_semantic_search",
                 "_openalex_search", "_crossref_search", "_europepmc_search",
                 "_core_search", "_iacr_search"):
        async def _empty(*a, **k):
            return []
        setattr(t, name, _empty)

    def boom(*a, **k):
        raise ConnectionError("offline")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(tool_mod.requests, "get", boom)
    try:
        out = await _run(t, "all,arxiv")
    finally:
        monkey.undo()
    assert "ignored_sources" not in out
    assert any("全源模式" in n for n in out.get("notes", []))
    # 后端确实被调用，且收到的是合法的后端源清单（不是裸 "all"）
    assert calls and calls[0]["sources"] != "all"


# ---------- B3 key 门控源 ----------

@pytest.mark.asyncio
async def test_explicit_disabled_sources_are_reported():
    t = Tools(); t.valves = Tools.Valves()
    _no_backend(t)
    out = await _run(t, "zhihuiya,ieee,firecrawl", user=_user())
    ign = out["ignored_sources"]
    assert set(ign) == {"zhihuiya", "ieee", "firecrawl"}
    assert "zhihuiya_apikey" in ign["zhihuiya"]
    assert "ieee_apikey" in ign["ieee"]
    assert "firecrawl_base_url" in ign["firecrawl"]


@pytest.mark.asyncio
async def test_default_path_reports_disabled_as_single_note():
    """落回 valve 默认值时不逐条刷 ignored_sources，只在 notes 汇总。"""
    t = Tools(); t.valves = Tools.Valves()
    _no_backend(t)
    _fake_arxiv(t, [_paper(title="A")])
    out = await _run(t, "", user=_user(default_sources="arxiv,zhihuiya,ieee"))
    assert "ignored_sources" not in out
    assert any("未配置 key/url" in n for n in out.get("notes", []))


@pytest.mark.asyncio
async def test_configured_source_is_not_reported_as_ignored():
    t = Tools(); t.valves = Tools.Valves()

    async def fake_zh(q, limit, key):
        return [_paper(title="Z", source="zhihuiya", paper_id="zh:1")]
    t._zhihuiya_search = fake_zh
    _no_backend(t)
    out = await _run(t, "zhihuiya", user=_user(zh_key="K"))
    assert out["source_results"] == {"zhihuiya": 1}
    assert "ignored_sources" not in out


# ---------- B4 去重（只按 DOI） ----------

@pytest.mark.asyncio
async def test_dedupe_by_doi_across_sources():
    t = Tools(); t.valves = Tools.Valves()
    # 直连源按固定顺序回填（zhihuiya 先于 arxiv），先出现的那条保留归属
    _fake_arxiv(t, [_paper(title="Same", doi="10.1/SAME", paper_id="arxiv:1",
                           pdf_url="http://arxiv/pdf")])

    async def fake_zh(q, limit, key):
        return [_paper(title="Same", doi="10.1/same", source="zhihuiya",
                       paper_id="zh:1")]
    t._zhihuiya_search = fake_zh
    _no_backend(t)
    out = await _run(t, "arxiv,zhihuiya", user=_user(zh_key="K"))
    assert out["total"] == 1
    assert out["duplicates_removed"] == 1
    assert out["papers"][0]["source"] == "zhihuiya"
    assert out["papers"][0]["pdf_url"] == "http://arxiv/pdf"


@pytest.mark.asyncio
async def test_same_title_without_doi_is_not_merged():
    """刻意不做标题模糊合并：预印本/正式版、同名综述不能被吞掉。"""
    t = Tools(); t.valves = Tools.Valves()
    _no_backend(t)
    _fake_arxiv(t, [_paper(title="Deep Learning for Protein Folding",
                           paper_id="arxiv:1")])

    async def fake_zh(q, limit, key):
        return [_paper(title="Deep Learning for Protein Folding", year=2024,
                       source="zhihuiya", paper_id="zh:1")]
    t._zhihuiya_search = fake_zh
    out = await _run(t, "arxiv,zhihuiya", user=_user(zh_key="K"))
    assert out["total"] == 2 and "duplicates_removed" not in out


@pytest.mark.asyncio
async def test_distinct_dois_are_kept():
    t = Tools(); t.valves = Tools.Valves()
    _no_backend(t)
    _fake_arxiv(t, [_paper(title="A", doi="10.1/a")])

    async def fake_zh(q, limit, key):
        return [_paper(title="B", doi="10.1/b", source="zhihuiya", paper_id="zh:1")]
    t._zhihuiya_search = fake_zh
    out = await _run(t, "arxiv,zhihuiya", user=_user(zh_key="K"))
    assert out["total"] == 2


# ---------- B5 download 端点跟随 valve ----------

@pytest.mark.asyncio
async def test_download_uses_papers_service_url_valve():
    t = Tools(); t.valves = Tools.Valves()
    t.valves.papers_service_url = "http://custom-papers:9000/papers"
    seen = {}

    async def fake_endpoint(endpoint, *a, **k):
        seen["endpoint"] = endpoint
        return None, "stub"
    t._download_via_endpoint = fake_endpoint
    out = json.loads(await t.download_paper_to_knowledge(
        title="X", doi="10.1/x", __user__={}))
    assert seen["endpoint"] == "http://custom-papers:9000/papers/download_with_fallback"
    assert "error" in out


# ---------- 字段隔离 / all 模式 / 兜底去重 ----------

@pytest.mark.asyncio
async def test_all_mode_reports_no_ignored_sources():
    """all 模式不该报未知源、不该有 ignored_sources（否则每次全源搜索都刷噪声）。"""
    t = Tools(); t.valves = Tools.Valves()
    _no_backend(t)
    for name in ("_hal_search", "_dblp_search", "_zenodo_search", "_openaire_search",
                 "_pubmed_search", "_pmc_search", "_arxiv_search", "_semantic_search",
                 "_openalex_search", "_crossref_search", "_europepmc_search",
                 "_core_search", "_iacr_search"):
        async def _empty(*a, **k):
            return []
        setattr(t, name, _empty)

    def boom(*a, **k):
        raise ConnectionError("offline")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(tool_mod.requests, "get", boom)
    try:
        out = await _run(t, "all")
    finally:
        monkey.undo()
    assert "ignored_sources" not in out


@pytest.mark.asyncio
async def test_ignored_sources_is_isolated_from_errors():
    """ignored_sources 不能进 errors：errors 会触发 web 兜底，也会被 LLM 当失败。"""
    t = Tools(); t.valves = Tools.Valves()
    _no_backend(t)
    out = await _run(t, "patsnap")
    assert out["ignored_sources"] and out["errors"] == {}
    assert "web_fallback" not in out["errors"]


@pytest.mark.asyncio
async def test_backend_failure_early_return_keeps_ignored_sources():
    t = Tools(); t.valves = Tools.Valves()

    def boom(*a, **k):
        raise RuntimeError("backend down")
    t._papers_call = boom
    # query 用两词小写，确保 core == original 走 _backend_all（_backend_split 会把异常
    # 收进 errors["lit"] 而不是上抛）
    out = await _run(t, "patsnap,doaj", query="glucose biosensor")
    assert "error" in out
    assert "patsnap" in out["ignored_sources"]


@pytest.mark.asyncio
async def test_web_fallback_results_are_deduped_too():
    t = Tools(); t.valves = Tools.Valves()
    t.valves.tavily_base_url = "http://tavily.local"
    _no_backend(t)
    _fake_arxiv(t, [_paper(title="Same Paper Title For Dedupe", doi="10.1/s",
                           paper_id="arxiv:1")])

    t._net_failed_sources = lambda errors: ["arxiv"]

    async def fake_fallback(query, limit, user=None, domains=None):
        return "tavily", [_paper(title="Same Paper Title For Dedupe", doi="10.1/s",
                                 source="tavily", paper_id="t:1")]
    t._web_search_fallback = fake_fallback
    out = await _run(t, "arxiv")
    assert out["total"] == 1, "兜底补回的同一篇不应重复计入"
    assert out["duplicates_removed"] == 1
    assert out["source_results"]["tavily"] == 1


# ---------- 接口漂移告警 ----------

@pytest.mark.asyncio
async def test_non_network_error_raises_admin_alert():
    """400/字段缺失/解析失败这类错误说明上游可能改版，必须冒泡给使用者。"""
    t = Tools(); t.valves = Tools.Valves()
    _no_backend(t)

    async def boom(q, limit):
        raise RuntimeError("openalex 返回缺少 results 字段（HTTP 200，body 结构变了）")
    t._openalex_search = boom
    out = await _run(t, "openalex")
    alert = out["admin_alert"]
    assert "openalex" in alert["sources"]
    assert "漂移" in alert["message"] and "管理员" in alert["message"]
    assert out["errors"]["openalex"].startswith("openalex 返回缺少")


@pytest.mark.asyncio
async def test_network_error_does_not_raise_admin_alert():
    """连接/限流类错误已由 web 兜底链负责，不该惊动管理员。"""
    t = Tools(); t.valves = Tools.Valves()
    _no_backend(t)

    async def boom(q, limit):
        raise RuntimeError("openalex 调用超时 (30s)")
    t._openalex_search = boom
    out = await _run(t, "openalex")
    assert "admin_alert" not in out
    assert "openalex" in out["errors"]


@pytest.mark.asyncio
async def test_antibot_error_does_not_raise_admin_alert():
    """反爬拦截是站点行为（已有 firecrawl 兜底），不算接口漂移。"""
    t = Tools(); t.valves = Tools.Valves()
    _no_backend(t)

    async def boom(q, limit, user=None):
        raise RuntimeError("dblp 返回非 JSON（text/html），疑似反爬拦截页")
    t._dblp_search = boom
    out = await _run(t, "dblp")
    assert "admin_alert" not in out

def test_web_fallback_failure_is_not_reported_as_drift():
    """兜底服务自身的失败不是学术源漂移，别让管理员去查错东西。"""
    t = Tools()
    assert t._admin_drift_errors({"web_fallback": "web 兜底失败: 500 internal"}) == {}
    assert t._admin_drift_errors({"crossref": "message.items 缺失"}) == {
        "crossref": "message.items 缺失"}

def test_split_branch_labels_are_normalized_in_admin_alert():
    """sem/lit 是内部子分支标签，告警里要归并成 backend，人才看得懂。"""
    t = Tools()
    out = t._admin_drift_errors({"lit": "返回结构变了", "sem": "字段缺失", "crossref": "404"})
    assert set(out) == {"backend", "crossref"}

@pytest.mark.asyncio
async def test_read_failure_explains_junk_rejection():
    """三个抓取渠道都拿不到可信内容时，错误里要说清"为什么没内容"（过短/挑战页）。"""
    t = Tools(); t.valves = Tools.Valves()

    async def junk(*a, **k):
        return "", ""
    t._web_read_fallback = junk
    out = json.loads(await t.read_paper("openalex", "W123", url="http://example.org/x"))
    assert "过短" in out["error"] and "挑战页" in out["error"]
