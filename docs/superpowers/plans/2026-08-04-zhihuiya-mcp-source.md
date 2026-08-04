# 智慧芽 (zhihuiya) 源接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 tool.py 新增第 17 个文献源智慧芽（zhihuiya），通过 apikey 启用，集成进 search_papers 聚合与 read_paper 元数据级 read。

**Architecture:** 不改 mcpo config。tool.py 作为编排层，用 open-webui 容器自带的 `mcp` SDK（1.27.2）的 `streamablehttp_client` 直连智慧芽 streamable-http MCP 端点，每次调用注入 apikey。search 走 `search_literature` + `literature_bibliography` 两步补 abstract；read 走 `literature_bibliography` 元数据级 read；全文靠 doi 走现有 OA fallback 链。

**Tech Stack:** Python 3, OpenWebUI Tool (pydantic Valves/UserValves), `mcp` SDK (streamablehttp_client), `anyio`, `requests`, `pytest`

**Spec:** `docs/superpowers/specs/2026-08-04-zhihuiya-mcp-source-design.md`

## Global Constraints

- 仅修改 `tool.py`；新增 `tests/test_zhihuiya.py`。**不改** mcpo `config.json`、docker-compose、后端 `paper-search-mcp`。
- 启用判定：`(user_valves.zhihuiya_apikey or valves.zhihuiya_apikey).strip()` 非空 且 `user_valves.zhihuiya_enabled` 为 True，才发请求。
- 智慧芽分支必须全程 async（用 mcp SDK 的 async client），禁止在 async 函数里裸跑同步阻塞调用。
- 智慧芽分支独立 `asyncio.timeout(30)` 兜底；任何异常写入 `errors["zhihuiya"]`，`source_results["zhihuiya"]=0`，不影响其它 16 源。
- `search_literature` 必须带合法 `type`（`title/abstract/author/publication/title/abstract/all`），默认用 `all`。
- `literature_bibliography` 的 `paper_id` 支持批量逗号分隔（≤100），search 里 N 篇只调 1 次。
- 字段映射到 `_trim_paper` 期望键：`title/authors/published_date/abstract/paper_id/doi/source/pdf_url/citations/url`。

## 文件结构

- Modify: `tool.py`
  - `Tools.Valves` 增加 `zhihuiya_apikey`
  - `Tools.UserValves` 增加 `zhihuiya_apikey`、`zhihuiya_enabled`
  - 新增内部方法：`_zhihuiya_enabled_key()`、`_zhihuiya_call()`、`_zhihuiya_search()`、`_zhihuiya_bibliography()`、`_zhihuiya_map_paper()`
  - `_READ_TOOLS` 增加 `"zhihuiya": "zhihuiya_bibliography"`（哨兵值，非后端工具）
  - `search_papers` 增加 zhihuiya 并发分支与合并
  - `read_paper` 增加 zhihuiya 元数据级 read 分支
- Create: `tests/test_zhihuiya.py` — 纯函数与阀门逻辑的 mock 单元测试

---

### Task 1: 配置阀门（Valves / UserValves）与启用判定

**Files:**
- Modify: `tool.py`（`Tools.Valves` 约 line 33-48；`Tools.UserValves` 约 line 50-65）
- Test: `tests/test_zhihuiya.py`

**Interfaces:**
- Consumes: 无（首个任务）
- Produces:
  - `Tools.Valves.zhihuiya_apikey: str`
  - `Tools.UserValves.zhihuiya_apikey: str`
  - `Tools.UserValves.zhihuiya_enabled: bool`
  - `Tools._zhihuiya_enabled_key(__user__: dict) -> tuple[bool, str]`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_zhihuiya.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: FAIL，`AttributeError: 'Tools' object has no attribute '_zhihuiya_enabled_key'`

- [ ] **Step 3: 实现阀门与启用判定**

`tool.py` `Tools.Valves` 内（`shared_download_dir` 字段之后）追加：

```python
        zhihuiya_apikey: str = Field(
            default="",
            description="智慧芽(zhihuiya)科学文献 API key（管理员/公司级，留空则不启用该源）",
        )
```

`tool.py` `Tools.UserValves` 内（`scihub_url` 字段之后）追加：

```python
        zhihuiya_apikey: str = Field(
            default="",
            description="智慧芽个人 API key（非空时覆盖管理员 key）",
            json_schema_extra={"input": {"type": "password"}},
        )
        zhihuiya_enabled: bool = Field(
            default=True,
            description="是否启用智慧芽文献源（需 admin 或个人已配 key）",
        )
```

`tool.py` 内部方法区（`_mcp_call` 之前）新增：

```python
    # ---------- 智慧芽 zhihuiya ----------
    _ZHIHUIYA_MCP_URL = "https://connect.zhihuiya.com/eba075/mcp?apikey={key}"

    def _zhihuiya_enabled_key(self, __user__=None) -> tuple:
        uv = __user__.get("valves") if __user__ else None
        user_key = (getattr(uv, "zhihuiya_apikey", "") or "").strip()
        admin_key = (getattr(self.valves, "zhihuiya_apikey", "") or "").strip()
        key = user_key or admin_key
        enabled = bool(getattr(uv, "zhihuiya_enabled", True)) and bool(key)
        return enabled, key
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: 4 PASS

- [ ] **Step 5: 提交**

```bash
git add tool.py tests/test_zhihuiya.py
git commit -m "feat(zhihuiya): add Valves/UserValves and enablement check"
```

---

### Task 2: 直连调用封装 `_zhihuiya_call`

**Files:**
- Modify: `tool.py`（Task 1 的 `_zhihuiya_enabled_key` 之后）
- Test: `tests/test_zhihuiya.py`

**Interfaces:**
- Consumes: `_zhihuiya_enabled_key()`（Task 1）
- Produces: `async Tools._zhihuiya_call(tool_name: str, args: dict, key: str, timeout: int = 30) -> dict`
  — 返回解析后的 dict；失败抛 `RuntimeError`

- [ ] **Step 1: 写失败测试（mock SDK）**

`tests/test_zhihuiya.py` 追加：

```python
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
```

测试文件头部需加（若尚未配置 asyncio 模式）：

```python
pytestmark = pytest.mark.asyncio
```

并在 repo 根新建 `pytest.ini`：

```ini
[pytest]
asyncio_mode = auto
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: FAIL，`AttributeError: 'Tools' object has no attribute '_zhihuiya_call'`

- [ ] **Step 3: 实现 `_zhihuiya_call`**

`tool.py` 顶部 import 区追加（容器已装 mcp 1.27.2，本地 1.28.1 亦可）：

```python
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
```

`tool.py` `_zhihuiya_enabled_key` 之后新增：

```python
    async def _zhihuiya_call(self, tool_name: str, args: dict, key: str, timeout: int = 30) -> dict:
        """直连智慧芽 MCP 调用单个工具，返回解析后的 dict。失败抛 RuntimeError。"""
        url = self._ZHIHUIYA_MCP_URL.format(key=key)

        async def _run():
            async with streamablehttp_client(url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await session.call_tool(tool_name, args)

        try:
            result = await asyncio.wait_for(_run(), timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(f"智慧芽 {tool_name} 调用超时 ({timeout}s)")
        except Exception as e:
            raise RuntimeError(f"智慧芽 {tool_name} 连接失败: {e}")

        if getattr(result, "isError", False):
            msg = ""
            for c in getattr(result, "content", []) or []:
                msg = getattr(c, "text", "") or msg
            raise RuntimeError(f"智慧芽 {tool_name} 返回错误: {msg[:300]}")

        for c in getattr(result, "content", []) or []:
            text = getattr(c, "text", None)
            if not text:
                continue
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return {"raw": text}
        return {}
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: 6 PASS

- [ ] **Step 5: 提交**

```bash
git add tool.py tests/test_zhihuiya.py pytest.ini
git commit -m "feat(zhihuiya): add async direct MCP call wrapper"
```

---

### Task 3: 字段映射 `_zhihuiya_map_paper`

**Files:**
- Modify: `tool.py`
- Test: `tests/test_zhihuiya.py`

**Interfaces:**
- Consumes: 无
- Produces: `Tools._zhihuiya_map_paper(search_item: dict, bib: dict) -> dict`
  — 输出键与 `_trim_paper` 期望一致：`title/authors/published_date/abstract/paper_id/doi/source/pdf_url/citations/url`

- [ ] **Step 1: 写失败测试**

`tests/test_zhihuiya.py` 追加：

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: FAIL，`AttributeError: type object 'Tools' has no attribute '_zhihuiya_map_paper'`

- [ ] **Step 3: 实现 `_zhihuiya_map_paper`**

`tool.py` `_zhihuiya_call` 之后新增（静态方法）：

```python
    @staticmethod
    def _zhihuiya_text_list(field) -> str:
        """智慧芽多语言字段 [{lang,text}] 或纯 list -> 拼接字符串。"""
        if not field:
            return ""
        if isinstance(field, str):
            return field.strip()
        parts = []
        for item in field:
            if isinstance(item, dict):
                parts.append((item.get("text") or "").strip())
            else:
                parts.append(str(item).strip())
        return "; ".join(p for p in parts if p)

    @staticmethod
    def _zhihuiya_map_paper(search_item: dict, bib: dict = None) -> dict:
        bib = bib or {}
        title = Tools._zhihuiya_text_list(bib.get("title")) or Tools._zhihuiya_text_list(
            search_item.get("title")
        )
        authors = search_item.get("author") or bib.get("author") or []
        if isinstance(authors, str):
            authors = [authors]
        abstract = Tools._zhihuiya_text_list(bib.get("abstract"))
        published = (
            str(bib.get("publication_year") or bib.get("publication_date") or "")[:4]
        )
        return {
            "title": title,
            "authors": "; ".join(a for a in authors if a),
            "published_date": published,
            "abstract": abstract,
            "paper_id": search_item.get("paper_id") or "",
            "doi": search_item.get("doi") or bib.get("doi") or "",
            "source": "zhihuiya",
            "pdf_url": "",
            "citations": 0,
            "url": bib.get("website") or "",
        }
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: 8 PASS

- [ ] **Step 5: 提交**

```bash
git add tool.py tests/test_zhihuiya.py
git commit -m "feat(zhihuiya): add search+bibliography field mapper"
```

---

### Task 4: 搜索编排 `_zhihuiya_search`（两步）

**Files:**
- Modify: `tool.py`
- Test: `tests/test_zhihuiya.py`

**Interfaces:**
- Consumes: `_zhihuiya_call()`（Task 2）、`_zhihuiya_map_paper()`（Task 3）
- Produces: `async Tools._zhihuiya_search(query: str, limit: int, key: str) -> list[dict]`
  — 返回 map 后的 paper dict 列表；失败抛 RuntimeError

- [ ] **Step 1: 写失败测试（mock `_zhihuiya_call`）**

`tests/test_zhihuiya.py` 追加：

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: FAIL，`AttributeError: 'Tools' object has no attribute '_zhihuiya_search'`

- [ ] **Step 3: 实现 `_zhihuiya_search`**

`tool.py` `_zhihuiya_map_paper` 之后新增：

```python
    async def _zhihuiya_search(self, query: str, limit: int, key: str) -> list:
        """search_literature + literature_bibliography 两步，返回 map 后的 paper 列表。"""
        search_resp = await self._zhihuiya_call(
            "search_literature",
            {"text": query, "type": "all", "limit": max(1, min(int(limit), 100))},
            key,
        )
        results = ((search_resp or {}).get("data") or {}).get("results") or []
        if not results:
            return []

        ids = [r.get("paper_id") for r in results if r.get("paper_id")]
        bib_by_id = {}
        if ids:
            bib_resp = await self._zhihuiya_call(
                "literature_bibliography", {"paper_id": ",".join(ids[:100])}, key
            )
            for b in (bib_resp or {}).get("data") or []:
                if isinstance(b, dict) and b.get("paper_id"):
                    bib_by_id[b["paper_id"]] = b

        return [
            self._zhihuiya_map_paper(r, bib_by_id.get(r.get("paper_id")))
            for r in results
        ]
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: 10 PASS

- [ ] **Step 5: 提交**

```bash
git add tool.py tests/test_zhihuiya.py
git commit -m "feat(zhihuiya): add two-step search orchestration"
```

---

### Task 5: search_papers 集成 zhihuiya 并发分支

**Files:**
- Modify: `tool.py`（`search_papers` 方法，约 line 258-308）
- Test: `tests/test_zhihuiya.py`

**Interfaces:**
- Consumes: `_zhihuiya_enabled_key()`（T1）、`_zhihuiya_search()`（T4）
- Produces: `search_papers` 返回 JSON 的 `papers` 含 zhihuiya 条目、`source_results`/`errors` 含 zhihuiya 键

- [ ] **Step 1: 写失败测试**

`tests/test_zhihuiya.py` 追加：

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: 后 3 个 FAIL（zhihuiya 分支未接入；当前 search_papers 只走 `_mcp_call`）

- [ ] **Step 3: 改造 `search_papers`**

`tool.py` `search_papers` 方法体，将 try/except 调用 `_mcp_call` 之后到 return 之前的逻辑替换为并发编排。保留既有 `src` 计算，新增 zhihuiya 分支：

```python
        uv = __user__.get("valves") if __user__ else None
        src = (
            sources
            or (uv.default_sources if uv else None)
            or "arxiv,semantic,openalex,pubmed,pmc,core,europepmc"
        )

        zh_enabled, zh_key = self._zhihuiya_enabled_key(__user__)
        want_zh = zh_enabled and (
            "zhihuiya" in {s.strip().lower() for s in src.split(",")} or src.strip().lower() == "all"
        )

        async def _backend():
            return await anyio.to_thread.run_sync(
                self._mcp_call,
                "search_papers",
                {
                    "query": query,
                    "max_results_per_source": max_results_per_source,
                    "sources": src,
                },
            )

        async def _zh():
            return await self._zhihuiya_search(query, max_results_per_source, zh_key)

        backend_result, zh_result = None, None
        zh_error = None
        if want_zh:
            results = await asyncio.gather(_backend(), _zh(), return_exceptions=True)
            backend_result, zh_result = results[0], results[1]
            if isinstance(zh_result, Exception):
                zh_error, zh_result = zh_result, None
        else:
            backend_result = await _backend()

        if isinstance(backend_result, Exception):
            return json.dumps({"error": f"后端 search_papers 调用失败: {backend_result}"}, ensure_ascii=False)

        result = backend_result
        if not isinstance(result, dict):
            return json.dumps({"error": "backend 返回异常", "raw": str(result)[:500]}, ensure_ascii=False)

        papers = [self._trim_paper(p) for p in result.get("papers", [])]
        source_results = dict(result.get("source_results", {}))
        errors = dict(result.get("errors", {}))

        if want_zh:
            if zh_error is not None:
                source_results["zhihuiya"] = 0
                errors["zhihuiya"] = str(zh_error)
            else:
                zh_papers = [self._trim_paper(p) for p in (zh_result or [])]
                papers.extend(zh_papers)
                source_results["zhihuiya"] = len(zh_papers)

        return json.dumps(
            {
                "query": query,
                "total": len(papers),
                "source_results": source_results,
                "errors": errors,
                "papers": papers,
            },
            ensure_ascii=False,
            indent=2,
        )
```

注意：删除原方法中旧的 `uv/src` 重复定义与旧 try/except 块，确保只有一份。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: 13 PASS

- [ ] **Step 5: 提交**

```bash
git add tool.py tests/test_zhihuiya.py
git commit -m "feat(zhihuiya): integrate zhihuiya branch into search_papers"
```

---

### Task 6: read_paper 元数据级 read

**Files:**
- Modify: `tool.py`（`_READ_TOOLS` 约 line 69-96；`read_paper` 约 line 310-391）
- Test: `tests/test_zhihuiya.py`

**Interfaces:**
- Consumes: `_zhihuiya_enabled_key()`（T1）、`_zhihuiya_call()`（T2）
- Produces: `read_paper(source="zhihuiya", paper_id=...)` 返回 abstract 文本或降级 JSON

- [ ] **Step 1: 写失败测试**

`tests/test_zhihuiya.py` 追加：

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: FAIL（`_READ_TOOLS` 无 zhihuiya，走"未知源"分支）

- [ ] **Step 3: 接入 `_READ_TOOLS` 与 `read_paper`**

`tool.py` `_READ_TOOLS` 字典中（`"acm"` 之后）追加：

```python
        # ↓ 智慧芽：直连元数据级 read（literature_bibliography），非后端工具
        "zhihuiya": "zhihuiya_bibliography",
```

`tool.py` `read_paper` 方法，在 `backend_tool = self._READ_TOOLS.get(src)` 之后、`if backend_tool and paper_id:` 之前，插入 zhihuiya 专属分支：

```python
        if src == "zhihuiya":
            zh_enabled, zh_key = self._zhihuiya_enabled_key(__user__)
            if not zh_enabled:
                return json.dumps(
                    {"error": "智慧芽源未启用（未配 apikey 或已关闭）"},
                    ensure_ascii=False,
                )
            if paper_id:
                try:
                    bib = await self._zhihuiya_call(
                        "literature_bibliography", {"paper_id": paper_id}, zh_key
                    )
                    data = (bib or {}).get("data") or []
                    entry = data[0] if data else {}
                    abstract = self._zhihuiya_text_list(entry.get("abstract"))
                    if abstract:
                        header = self._zhihuiya_text_list(entry.get("title"))
                        pub = entry.get("publication") or ""
                        year = str(entry.get("publication_year") or "")
                        meta = " | ".join(x for x in [pub, year] if x)
                        return (
                            f"{header}\n{meta}\n\n{abstract}"
                            "\n\n[智慧芽元数据级 read；全文请用 doi 走 download_paper_to_knowledge 的 OA fallback 链]"
                        )[:max_chars]
                    backend_err = "智慧芽无可用 abstract"
                except Exception as e:
                    backend_err = f"智慧芽读取失败: {e}"
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: 15 PASS

- [ ] **Step 5: 提交**

```bash
git add tool.py tests/test_zhihuiya.py
git commit -m "feat(zhihuiya): add metadata-level read via literature_bibliography"
```

---

### Task 7: 真实端点集成验证（手动，非 pytest）

**Files:**
- 无新增文件（用 `.env` 的 `ZHIHUIYA_API_KEY`）

**Interfaces:**
- Consumes: Task 1-6 全部

- [ ] **Step 1: 验证 SDK 真实调用**

```bash
python3 -c "
import asyncio, os
from dotenv import load_dotenv
load_dotenv('.env')
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
async def m():
    url = f'https://connect.zhihuiya.com/eba075/mcp?apikey={os.getenv(\"ZHIHUIYA_API_KEY\")}'
    async with streamablehttp_client(url) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool('search_literature', {'text': 'CRISPR', 'type': 'all', 'limit': 2})
            print([c.text[:200] for c in res.content])
asyncio.run(m())
"
```
Expected: 打印含 `"success": true` 与 `paper_id`

- [ ] **Step 2: 验证 tool.py search 两步**

```bash
python3 -c "
import asyncio, importlib.util, os, json
from dotenv import load_dotenv
load_dotenv('.env')
spec = importlib.util.spec_from_file_location('tool', 'tool.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
t = m.Tools(); t.valves = m.Tools.Valves(zhihuiya_apikey=os.getenv('ZHIHUIYA_API_KEY'))
papers = asyncio.run(t._zhihuiya_search('CRISPR base editing', 3, os.getenv('ZHIHUIYA_API_KEY')))
print(json.dumps(papers[:2], ensure_ascii=False, indent=2)[:1500])
"
```
Expected: 输出含非空 `title`/`authors`/`abstract`/`doi`/`source=='zhihuiya'`

- [ ] **Step 3: 验证 read_paper 元数据级 read**

```bash
python3 -c "
import asyncio, importlib.util, os
from dotenv import load_dotenv
load_dotenv('.env')
spec = importlib.util.spec_from_file_location('tool', 'tool.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
t = m.Tools(); t.valves = m.Tools.Valves(zhihuiya_apikey=os.getenv('ZHIHUIYA_API_KEY'))
pid = asyncio.run(t._zhihuiya_search('CRISPR', 1, os.getenv('ZHIHUIYA_API_KEY')))[0]['paper_id']
out = asyncio.run(t.read_paper(source='zhihuiya', paper_id=pid))
print(out[:800])
"
```
Expected: 输出含 abstract 与"元数据级 read"提示

- [ ] **Step 4: 全量单测回归**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: 15 PASS

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "test(zhihuiya): verify against live endpoint"
```

---

### Task 8: 文档 — README / GitHub About / License / 推送

**Files:**
- Modify: `README.md`、`CLAUDE.md`（源清单）

- [ ] **Step 1: 更新 README.md**
加入吸引人的项目简介、架构图、16+1 源清单（含 zhihuiya 及其启用方式）、配置示例、License 徽章。

- [ ] **Step 2: 更新 CLAUDE.md 源清单**
`default_sources` 说明处补充 `zhihuiya`（标注：需配 apikey，直连不经 mcpo）。

- [ ] **Step 3: 确认 LICENSE 为 MIT**（已存在，核对内容无误）。

- [ ] **Step 4: 设置 GitHub About**

```bash
gh repo edit --description "Multi-source academic paper search/read/download for OpenWebUI — 16+ open sources via mcpo + zhihuiya scientific literature, with OA fallback chain and Knowledge RAG ingestion" \
  --add-topic openwebui --add-topic academic-search --add-topic mcp --add-topic rag --add-topic literature-search
```
（若未登录 gh 或无远程，则提示用户在 GitHub 网页手动填写。）

- [ ] **Step 5: 推送 GitHub**

```bash
git push origin main
```

- [ ] **Step 6: 提交文档变更**

```bash
git add README.md CLAUDE.md
git commit -m "docs: refresh README and source list for zhihuiya"
git push origin main
```

---

## Self-Review 记录

- **Spec 覆盖**：Global Constraints + T1(阀门/启用) + T2(直连) + T3(映射) + T4(两步搜索) + T5(search集成/错误隔离) + T6(read) + T7(真实验证) + T8(文档/About/License/推送) — 覆盖 spec 全部 11 节。
- **占位符**：无 TBD/TODO；每步含完整代码与命令。
- **类型一致性**：`_zhihuiya_enabled_key()->tuple[bool,str]`、`_zhihuiya_call(...)->dict`、`_zhihuiya_search(...)->list[dict]`、`_zhihuiya_map_paper(...)->dict`、`_zhihuiya_text_list(...)->str` 在 T1-T6 间签名一致；`_READ_TOOLS["zhihuiya"]="zhihuiya_bibliography"` 哨兵值与 read_paper 分支一致。
