"""szlib（深圳图书馆/读秀）接入：REST 契约、登录态、显式点名语义。

契约来源：szlib-mcp/docs/paper-search-integration.md + szlib-mcp/src/szlib_mcp/server.py
（200 ok/items；202 三种 code；400/500 error）。
"""
import importlib.util
import json
import os

import pytest

SPEC = importlib.util.spec_from_file_location(
    "tool", os.path.join(os.path.dirname(__file__), "..", "tool.py"))
tool_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool_mod)
Tools = tool_mod.Tools


class FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _item(**kw):
    it = {"type": "期刊", "title": "中文文献标题", "context": "某学报, 2023, (4)", "year": "2023"}
    it.update(kw)
    return it


def _patch_post(monkeypatch, resp_or_fn, record=None):
    def fake(url, json=None, timeout=None, **kw):
        if record is not None:
            record.append({"url": url, "json": json, "timeout": timeout})
        return resp_or_fn(url, json, timeout) if callable(resp_or_fn) else resp_or_fn
    monkeypatch.setattr(tool_mod.requests, "post", fake)


def _no_backend(t):
    t._papers_call = lambda *a, **k: {"papers": [], "source_results": {}, "errors": {}}


@pytest.mark.asyncio
async def test_szlib_200_maps_items_to_papers(monkeypatch):
    t = Tools(); t.valves = Tools.Valves()
    t.valves.szlib_url = "http://szlib.test:8765"
    _no_backend(t)
    rec = []
    _patch_post(monkeypatch, FakeResp(200, {
        "ok": True, "source": "duxiu", "total": 100,
        "by_type": {"期刊": 90}, "items": [_item()],
    }), rec)

    out = json.loads(await t.search_papers("深圳 城市更新", 3, "szlib", "", "", {}))
    p = out["papers"][0]
    assert out["total"] == 1 and out["source_results"] == {"szlib": 1}
    assert p["title"] == "中文文献标题"
    assert p["source"] == "szlib"
    assert p["year"] == "2023"
    assert p["paper_id"].startswith("szlib:")
    assert "[期刊]" in p["abstract"] and "某学报" in p["abstract"]
    assert out["errors"] == {}
    # 请求形状
    assert rec[0]["url"] == "http://szlib.test:8765/api/search"
    assert rec[0]["json"]["query"] == "深圳 城市更新"
    assert rec[0]["json"]["session"] == "default"
    assert rec[0]["timeout"] == 180


@pytest.mark.asyncio
async def test_szlib_paper_id_is_stable(monkeypatch):
    t = Tools(); t.valves = Tools.Valves()
    t.valves.szlib_url = "http://szlib.test:8765"
    _no_backend(t)
    _patch_post(monkeypatch, FakeResp(200, {"ok": True, "items": [_item()]}))
    a = json.loads(await t.search_papers("q", 3, "szlib", "", "", {}))
    b = json.loads(await t.search_papers("q2", 3, "szlib", "", "", {}))
    assert a["papers"][0]["paper_id"] == b["papers"][0]["paper_id"]


@pytest.mark.asyncio
async def test_szlib_202_awaiting_login_returns_login_url(monkeypatch):
    t = Tools(); t.valves = Tools.Valves()
    t.valves.szlib_url = "http://szlib.test:8765"
    _no_backend(t)
    _patch_post(monkeypatch, FakeResp(202, {
        "status": "AWAITING_LOGIN", "url": "https://mcp.example.com/szlib-verify/abc",
        "login_token": "tok123", "expires_in": 300,
    }))
    out = json.loads(await t.search_papers("q", 3, "szlib", "", "", {}))
    req = out["login_required"]
    assert out["total"] == 0 and out["errors"] == {}
    assert req["login_url"] == "https://mcp.example.com/szlib-verify/abc"
    assert req["login_token"] == "tok123"
    assert req["expires_in"] == 300
    assert "5 分钟" in req["notice"] or "300" in req["notice"] or "分钟" in req["notice"]
    assert any("login_url" in n for n in out.get("notes", []))


@pytest.mark.asyncio
async def test_szlib_202_relative_url_is_prefixed(monkeypatch):
    """未配 SZLIB_PUBLIC_BASE_URL 时服务端回相对路径 /verify/xxx。"""
    t = Tools(); t.valves = Tools.Valves()
    t.valves.szlib_url = "http://szlib.test:8765"
    _no_backend(t)
    _patch_post(monkeypatch, FakeResp(202, {"status": "AWAITING_LOGIN",
                                            "url": "/verify/xyz", "login_token": "t"}))
    out = json.loads(await t.search_papers("q", 3, "szlib", "", "", {}))
    assert out["login_required"]["login_url"] == "http://szlib.test:8765/verify/xyz"


@pytest.mark.asyncio
async def test_szlib_202_login_timeout_has_no_url(monkeypatch):
    """LOGIN_TIMEOUT/LOGIN_FAILED 的 202 不带 url，提示要重新发起。"""
    t = Tools(); t.valves = Tools.Valves()
    t.valves.szlib_url = "http://szlib.test:8765"
    _no_backend(t)
    _patch_post(monkeypatch, FakeResp(202, {"code": "LOGIN_TIMEOUT",
                                            "message": "验证超时"}))
    out = json.loads(await t.search_papers("q", 3, "szlib", "", "", {}))
    req = out["login_required"]
    assert "login_url" not in req and req["code"] == "LOGIN_TIMEOUT"
    assert "重新发起" in req["notice"]


@pytest.mark.asyncio
async def test_szlib_login_token_is_forwarded_with_long_timeout(monkeypatch):
    t = Tools(); t.valves = Tools.Valves()
    t.valves.szlib_url = "http://szlib.test:8765"
    _no_backend(t)
    rec = []
    _patch_post(monkeypatch, FakeResp(200, {"ok": True, "items": [_item()]}), rec)
    out = json.loads(await t.search_papers(
        "q", 3, "szlib", "", "", {}, szlib_session="user-42", szlib_login_token="tok-9"))
    assert out["total"] == 1
    assert rec[0]["json"]["login_token"] == "tok-9"
    assert rec[0]["json"]["session"] == "user-42"
    assert rec[0]["timeout"] == 330, "带 token 时服务端会阻塞等验证，超时必须放大"


@pytest.mark.asyncio
async def test_szlib_200_ok_false_is_surfaced_not_swallowed(monkeypatch):
    """200 + error（如 SEARCH_BOX_NOT_FOUND）= 上游页面漂移，不能当 0 命中。"""
    t = Tools(); t.valves = Tools.Valves()
    t.valves.szlib_url = "http://szlib.test:8765"
    _no_backend(t)
    _patch_post(monkeypatch, FakeResp(200, {"ok": False, "error": "SEARCH_BOX_NOT_FOUND"}))
    out = json.loads(await t.search_papers("q", 3, "szlib", "", "", {}))
    assert "SEARCH_BOX_NOT_FOUND" in out["errors"]["szlib"]
    assert "szlib" in out["admin_alert"]["sources"]


@pytest.mark.asyncio
async def test_szlib_500_goes_to_errors(monkeypatch):
    t = Tools(); t.valves = Tools.Valves()
    t.valves.szlib_url = "http://szlib.test:8765"
    _no_backend(t)
    _patch_post(monkeypatch, FakeResp(500, {"error": "RuntimeError: boom"}))
    out = json.loads(await t.search_papers("q", 3, "szlib", "", "", {}))
    assert out["source_results"]["szlib"] == 0
    assert "500" in out["errors"]["szlib"]


@pytest.mark.asyncio
async def test_szlib_is_never_run_in_all_mode(monkeypatch):
    """szlib 要浏览器+登录，禁止被 all 模式带进去。"""
    t = Tools(); t.valves = Tools.Valves()
    called = []

    async def boom(*a, **k):
        called.append(1)
        raise AssertionError("all 模式不应调用 szlib")
    t._szlib_search = boom
    _no_backend(t)
    for name in ("_hal_search", "_dblp_search", "_zenodo_search", "_openaire_search",
                 "_pubmed_search", "_pmc_search", "_arxiv_search", "_semantic_search",
                 "_openalex_search", "_crossref_search", "_europepmc_search",
                 "_core_search", "_iacr_search"):
        async def _empty(*a, **k):
            return []
        setattr(t, name, _empty)

    def boom_get(*a, **k):
        raise ConnectionError("offline")
    monkeypatch.setattr(tool_mod.requests, "get", boom_get)
    out = json.loads(await t.search_papers("q", 3, "all", "", "", {}))
    assert not called
    assert "szlib" not in out["source_results"]


def test_szlib_not_in_default_sources():
    v = Tools.UserValves()
    assert "szlib" not in (v.default_sources or "").split(",")
    assert "szlib" in tool_mod.DIRECT_SOURCES
    assert "szlib" in tool_mod.KNOWN_SOURCES
    assert tool_mod.Tools._READ_TOOLS.get("szlib", "missing") is None

# ---------- 评审补测（A1 + 边界） ----------

@pytest.mark.asyncio
async def test_login_required_survives_backend_failure(monkeypatch):
    """sources="szlib,doaj" 且后端炸了：login_url 不能被 early-return 吞掉。"""
    t = Tools(); t.valves = Tools.Valves()
    t.valves.szlib_url = "http://szlib.test:8765"
    _patch_post(monkeypatch, FakeResp(202, {
        "status": "AWAITING_LOGIN", "url": "https://pub.example/szlib/verify/x",
        "login_token": "tok", "expires_in": 300}))

    def boom(*a, **k):
        raise RuntimeError("backend down")
    t._papers_call = boom

    out = json.loads(await t.search_papers(
        "glucose biosensor", 3, "szlib,doaj", "", "", {}))
    assert "error" in out, "后端失败仍应报错"
    assert out["login_required"]["login_url"] == "https://pub.example/szlib/verify/x"
    assert out["login_required"]["login_token"] == "tok"


@pytest.mark.asyncio
async def test_szlib_token_timeout_says_token_invalid_and_skips_web_fallback(monkeypatch):
    """带 token 的请求超时：token 已作废要明说；且 szlib 不该触发 web 兜底。"""
    t = Tools(); t.valves = Tools.Valves()
    t.valves.szlib_url = "http://szlib.test:8765"
    t.valves.tavily_base_url = "http://tavily.local"
    _no_backend(t)
    fallback_calls = []

    async def fake_fallback(*a, **k):
        fallback_calls.append(1)
        return "tavily", []
    t._web_search_fallback = fake_fallback

    def timeout_post(*a, **k):
        raise tool_mod.requests.exceptions.ReadTimeout("read timed out")
    monkeypatch.setattr(tool_mod.requests, "post", timeout_post)

    out = json.loads(await t.search_papers(
        "城市更新", 3, "szlib", "", "", {}, szlib_login_token="tok-x"))
    assert "作废" in out["errors"]["szlib"] and "重新发起" in out["errors"]["szlib"]
    assert not fallback_calls, "szlib 需要浏览器登录，不应触发 tavily/firecrawl 兜底"
    assert "fallback_domains" not in out


@pytest.mark.asyncio
async def test_szlib_non_json_200_is_error(monkeypatch):
    t = Tools(); t.valves = Tools.Valves()
    t.valves.szlib_url = "http://szlib.test:8765"
    _no_backend(t)

    class BadResp:
        status_code = 200
        text = "<html>not json</html>"

        def json(self):
            raise ValueError("no json")
    monkeypatch.setattr(tool_mod.requests, "post", lambda *a, **k: BadResp())
    out = json.loads(await t.search_papers("q", 3, "szlib", "", "", {}))
    assert "非 JSON" in out["errors"]["szlib"]


@pytest.mark.asyncio
async def test_szlib_filters_bad_items(monkeypatch):
    t = Tools(); t.valves = Tools.Valves()
    t.valves.szlib_url = "http://szlib.test:8765"
    _no_backend(t)
    _patch_post(monkeypatch, FakeResp(200, {"ok": True, "items": [
        "not-a-dict", {"title": ""}, {"title": "有效标题", "year": "2024", "context": "刊"}]}))
    out = json.loads(await t.search_papers("q", 3, "szlib", "", "", {}))
    assert out["total"] == 1
    assert out["papers"][0]["title"] == "有效标题"


@pytest.mark.asyncio
async def test_szlib_limit_is_clamped_to_50(monkeypatch):
    t = Tools(); t.valves = Tools.Valves()
    t.valves.szlib_url = "http://szlib.test:8765"
    _no_backend(t)
    rec = []
    _patch_post(monkeypatch, FakeResp(200, {"ok": True, "items": []}), rec)
    await t.search_papers("q", 100, "szlib", "", "", {})
    assert rec[0]["json"]["limit"] == 50


@pytest.mark.asyncio
async def test_szlib_user_valve_url_wins(monkeypatch):
    from types import SimpleNamespace
    t = Tools(); t.valves = Tools.Valves()
    t.valves.szlib_url = "http://admin-szlib:8765"
    _no_backend(t)
    rec = []
    _patch_post(monkeypatch, FakeResp(200, {"ok": True, "items": [_item()]}), rec)
    await t.search_papers("q", 3, "szlib", "", "", {"valves": SimpleNamespace(
        szlib_url="http://user-szlib:9999")})
    assert rec[0]["url"].startswith("http://user-szlib:9999")


@pytest.mark.asyncio
async def test_szlib_never_sent_to_backend(monkeypatch):
    t = Tools(); t.valves = Tools.Valves()
    t.valves.szlib_url = "http://szlib.test:8765"
    calls = []

    def fake_backend(tool, args, timeout=180):
        calls.append(dict(args))
        return {"papers": [], "source_results": {}, "errors": {}}
    t._papers_call = fake_backend
    _patch_post(monkeypatch, FakeResp(200, {"ok": True, "items": [_item()]}))
    await t.search_papers("glucose biosensor", 3, "szlib,doaj", "", "", {})
    assert calls and calls[0]["sources"] == "doaj", "szlib 不能出现在后端 sources 里"
