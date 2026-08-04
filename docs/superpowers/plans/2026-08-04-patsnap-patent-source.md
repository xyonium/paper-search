# patsnap（智慧芽）专利源接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 tool.py 新增 patsnap 专利检索/阅读源（第 18 个源，首个专利源），通过共用的智慧芽 apikey 启用，提供 `search_patents` / `read_patent` 两个独立工具。

**Architecture:** 不改 mcpo config。复用现有 zhihuiya 直连机制：`_zhihuiya_call` 增加可选 `url` 参数，新增 `_PATSNAP_MCP_URL` 常量指向 patsnap 端点（`2b0355/logic-mcp`）。`search_patents` 调 `patsnap_search`(source='patent')，`read_patent` 调 `patsnap_fetch`(module=['basic','legal']) 取权利要求+说明书 markdown。与已接入的 zhihuiya 文献源共用同一把 key 与同一个启用开关。

**Tech Stack:** Python 3, OpenWebUI Tool (pydantic Valves/UserValves), `mcp` SDK (streamablehttp_client), `anyio`, `pytest`

**Spec:** `docs/superpowers/specs/2026-08-04-patsnap-patent-source-design.md`

## Global Constraints

- 仅修改 `tool.py`；向 `tests/test_zhihuiya.py` 追加测试。**不改** mcpo config.json、docker-compose、后端 paper-search-mcp、现有 17 源逻辑。
- **只接专利**：`patsnap_search` 必须 `source='patent'`；不接 patsnap 论文。
- **独立工具**：`search_patents`/`read_patent` 是新的 LLM 工具方法，与 `search_papers`/`read_paper`/`download_paper_to_knowledge` 并列，**不并入** search_papers 聚合。
- **共用 key + 共用开关**：复用 `_zhihuiya_enabled_key(__user__)`（任一 key 非空且 `zhihuiya_enabled` 为 True 才启用，否则返回错误 JSON 不发请求）。
- 全程 async（mcp SDK async client），禁止同步阻塞；`_zhihuiya_call` 用 `asyncio.wait_for` 兜底。
- 所有 zhihuiya/patsnap 错误消息必须经 `_redact_zhihuiya_key` 脱敏（key 可能泄在 URL/异常文本）。
- `patsnap_search` 用 semantic_query 时必须显式传 `search_strategy=['semantic']`（否则端点返回 hints 警告）。
- `read_patent` 默认 `module=['basic','legal']`，markdown 大（~91KB），必须截断到 max_chars。

## 文件结构

- Modify: `tool.py`
  - 新增类常量 `_PATSNAP_MCP_URL`
  - `_zhihuiya_call` 签名加 `url: str = None`
  - 新增内部映射 `_patsnap_map_patent()`
  - 新增 LLM 工具 `search_patents()`、`read_patent()`
  - 更新模块 docstring 工具用法、版本号 2.3.0→2.4.0
- Modify: `tests/test_zhihuiya.py`（追加 patsnap 测试）
- Modify: `README.md`、`CLAUDE.md`（文档任务）

现有可复用接口（已实现，勿改动其行为）：
- `Tools._zhihuiya_enabled_key(__user__=None) -> tuple` → `(enabled: bool, key: str)`
- `Tools._redact_zhihuiya_key(text: str) -> str`
- `Tools._zhihuiya_call(tool_name, args, key, timeout=30) -> dict`（本计划给它加 `url` 参数）

---

### Task 1: `_PATSNAP_MCP_URL` 常量 + `_zhihuiya_call` 支持自定义 url

**Files:**
- Modify: `tool.py`（`_ZHIHUIYA_MCP_URL` 约 line 141；`_zhihuiya_call` 约 line 151-186）
- Test: `tests/test_zhihuiya.py`

**Interfaces:**
- Consumes: 现有 `_zhihuiya_call`、`_ZHIHUIYA_MCP_URL`
- Produces:
  - `Tools._PATSNAP_MCP_URL = "https://connect.zhihuiya.com/2b0355/logic-mcp?apikey={key}"`
  - `Tools._zhihuiya_call(tool_name, args, key, timeout=30, url=None)` — `url=None` 时用 `_ZHIHUIYA_MCP_URL`，否则用传入 url

- [ ] **Step 1: 写失败测试**

`tests/test_zhihuiya.py` 追加（沿用文件已有的 importlib loader `tool_mod`/`Tools` 与 mock helpers `_fake_session`/`_text_content`）：

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: FAIL，`AttributeError: type object 'Tools' has no attribute '_PATSNAP_MCP_URL'`（及 `_zhihuiya_call` 不接受 `url` kwarg）

- [ ] **Step 3: 实现**

`tool.py` 在 `_ZHIHUIYA_MCP_URL` 之后加常量：

```python
    _ZHIHUIYA_MCP_URL = "https://connect.zhihuiya.com/eba075/mcp?apikey={key}"
    _PATSNAP_MCP_URL = "https://connect.zhihuiya.com/2b0355/logic-mcp?apikey={key}"
```

修改 `_zhihuiya_call` 签名与首行（其余不变）：

```python
    async def _zhihuiya_call(self, tool_name: str, args: dict, key: str,
                             timeout: int = 30, url: str = None) -> dict:
        """直连智慧芽 MCP 调用单个工具，返回解析后的 dict。失败抛 RuntimeError。
        url 为 None 时用文献源 _ZHIHUIYA_MCP_URL，否则用传入的端点（如 patsnap）。"""
        url = (url or self._ZHIHUIYA_MCP_URL).format(key=key)
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: 全部 PASS（21 个：19 旧 + 2 新）

- [ ] **Step 5: 提交**

```bash
git add tool.py tests/test_zhihuiya.py
git commit -m "feat(patsnap): add _PATSNAP_MCP_URL and custom-url support in _zhihuiya_call"
```

---

### Task 2: 专利字段映射 `_patsnap_map_patent`

**Files:**
- Modify: `tool.py`（`_zhihuiya_map_paper` 之后）
- Test: `tests/test_zhihuiya.py`

**Interfaces:**
- Consumes: 无（纯静态函数）
- Produces: `Tools._patsnap_map_patent(doc: dict) -> dict`
  — 输出键：`patent_number, title, ipc, legal_status, application_date, publication_date, cited_count, assignees, inventors, jurisdiction, url, view_url`

- [ ] **Step 1: 写失败测试**

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: FAIL，`AttributeError: type object 'Tools' has no attribute '_patsnap_map_patent'`

- [ ] **Step 3: 实现**

`tool.py` 在 `_zhihuiya_map_paper` 之后加静态方法：

```python
    @staticmethod
    def _patsnap_map_patent(doc: dict) -> dict:
        """patsnap_search 的 data.docs[] 项 -> 统一专利条目。"""
        def _join(v):
            if isinstance(v, list):
                return "; ".join(str(x) for x in v if x)
            return str(v) if v else ""
        return {
            "patent_number": doc.get("patent_number") or "",
            "title": (doc.get("title") or "").strip(),
            "ipc": doc.get("ipc") or "",
            "legal_status": doc.get("legal_status") or "",
            "application_date": str(doc.get("application_date") or ""),
            "publication_date": str(doc.get("publication_date") or ""),
            "cited_count": doc.get("cited_count", 0) or 0,
            "assignees": _join(doc.get("assignees")),
            "inventors": _join(doc.get("inventors")),
            "jurisdiction": doc.get("jurisdiction") or "",
            "url": doc.get("url") or "",
            "view_url": doc.get("view_url") or "",
        }
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: 23 PASS

- [ ] **Step 5: 提交**

```bash
git add tool.py tests/test_zhihuiya.py
git commit -m "feat(patsnap): add patent field mapper"
```

---

### Task 3: `search_patents` 工具

**Files:**
- Modify: `tool.py`（在 `search_papers` 之后、暴露给 LLM 的方法区）
- Test: `tests/test_zhihuiya.py`

**Interfaces:**
- Consumes: `_zhihuiya_enabled_key`(T-已存在)、`_zhihuiya_call`(Task1)、`_patsnap_map_patent`(Task2)、`_PATSNAP_MCP_URL`(Task1)
- Produces: `async Tools.search_patents(query, limit=10, sort="relevance", filters=None, __user__={}) -> str`（JSON）

- [ ] **Step 1: 写失败测试**

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: FAIL，`AttributeError: 'Tools' object has no attribute 'search_patents'`

- [ ] **Step 3: 实现**

`tool.py` 在 `search_papers` 方法之后加：

```python
    async def search_patents(
        self,
        query: str,
        limit: int = 10,
        sort: str = "relevance",
        filters: dict = None,
        __user__={},
    ) -> str:
        """
        检索专利（智慧芽 patsnap，语义检索）。返回 patent_number/title/ipc/legal_status/
        application_date/publication_date/cited_count/assignees/inventors/jurisdiction/url。
        - 需在 Valves 配 zhihuiya_apikey（管理员或个人）才启用，否则返回错误 JSON
        - 读专利全文用 read_patent(patent_number)
        :param query: 自然语言技术问题/概念（如 'CRISPR gene editing'）
        :param limit: 返回数量（1-100，默认10）
        :param sort: 排序，默认 relevance；专利可选 publication/application/granted/
            expired/priority/cited_count，前缀 '-' 降序（如 '-publication' 最新优先）
        :param filters: 结构化筛选（申请人/IPC/日期/受理局等，可选）
        """
        zh_enabled, zh_key = self._zhihuiya_enabled_key(__user__)
        if not zh_enabled:
            return json.dumps(
                {"error": "智慧芽源未启用（未配 apikey 或已关闭）"}, ensure_ascii=False
            )
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            limit = 10
        args = {
            "semantic_query": query,
            "search_strategy": ["semantic"],
            "source": "patent",
            "limit": limit,
            "sort": sort or "relevance",
        }
        if filters:
            args["filters"] = filters
        try:
            resp = await self._zhihuiya_call(
                "patsnap_search", args, zh_key, url=self._PATSNAP_MCP_URL
            )
        except Exception as e:
            return json.dumps({"error": f"专利检索失败: {e}"}, ensure_ascii=False)
        data = (resp or {}).get("data") or {}
        docs = data.get("docs") or []
        patents = [self._patsnap_map_patent(d) for d in docs]
        return json.dumps(
            {
                "query": query,
                "total_hits": data.get("total_hits", len(patents)),
                "returned_count": data.get("returned_count", len(patents)),
                "patents": patents,
            },
            ensure_ascii=False,
            indent=2,
        )
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: 25 PASS

- [ ] **Step 5: 提交**

```bash
git add tool.py tests/test_zhihuiya.py
git commit -m "feat(patsnap): add search_patents tool"
```

---

### Task 4: `read_patent` 工具

**Files:**
- Modify: `tool.py`（`search_patents` 之后）
- Test: `tests/test_zhihuiya.py`

**Interfaces:**
- Consumes: `_zhihuiya_enabled_key`、`_zhihuiya_call`(Task1)、`_PATSNAP_MCP_URL`(Task1)
- Produces: `async Tools.read_patent(patent_number, max_chars=25000, __user__={}) -> str`

- [ ] **Step 1: 写失败测试**

```python
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
    t.valves = Tools.Valves()
    out = json.loads(await t.read_patent("US1", __user__=_user()))
    assert "error" in out
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: FAIL，`AttributeError: 'Tools' object has no attribute 'read_patent'`

- [ ] **Step 3: 实现**

`tool.py` 在 `search_patents` 之后加：

```python
    async def read_patent(
        self,
        patent_number: str,
        max_chars: int = 25000,
        __user__={},
    ) -> str:
        """
        阅读专利全文（智慧芽 patsnap_fetch，markdown：著录项+权利要求+说明书+法律状态）。
        - 需在 Valves 配 zhihuiya_apikey 才启用
        - 用 search_patents 先拿到 patent_number（公开号，如 US11530424B1）
        :param patent_number: 专利公开号（pn）
        :param max_chars: 最大返回字符数（默认25000，专利文档很大会截断）
        """
        zh_enabled, zh_key = self._zhihuiya_enabled_key(__user__)
        if not zh_enabled:
            return json.dumps(
                {"error": "智慧芽源未启用（未配 apikey 或已关闭）"}, ensure_ascii=False
            )
        if not (patent_number or "").strip():
            return json.dumps(
                {"error": "需提供 patent_number（专利公开号）"}, ensure_ascii=False
            )
        try:
            max_chars = int(max_chars)
        except (TypeError, ValueError):
            max_chars = 25000
        try:
            resp = await self._zhihuiya_call(
                "patsnap_fetch",
                {
                    "keys": [patent_number.strip()],
                    "key_type": "pn",
                    "module": ["basic", "legal"],
                },
                zh_key,
                timeout=60,
                url=self._PATSNAP_MCP_URL,
            )
        except Exception as e:
            return json.dumps({"error": f"专利获取失败: {e}"}, ensure_ascii=False)
        results = (resp or {}).get("results") or []
        md = results[0].get("markdown", "") if results else ""
        if not md:
            return json.dumps(
                {"error": f"未获取到专利 {patent_number} 的内容"}, ensure_ascii=False
            )
        return md[:max_chars] + ("\n\n[…专利文档截断…]" if len(md) > max_chars else "")
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: 27 PASS

- [ ] **Step 5: 提交**

```bash
git add tool.py tests/test_zhihuiya.py
git commit -m "feat(patsnap): add read_patent tool"
```

---

### Task 5: 真实端点验证 + docstring/版本号

**Files:**
- Modify: `tool.py`（模块 docstring、version）
- 用 `.env` 的 `ZHIHUIYA_API_KEY` 实测

**Interfaces:**
- Consumes: Task 1-4 全部

- [ ] **Step 1: 真实验证 search_patents**

```bash
python3 -c "
import asyncio, importlib.util, os, json
from dotenv import load_dotenv
load_dotenv('.env')
spec = importlib.util.spec_from_file_location('tool', 'tool.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
t = m.Tools(); t.valves = m.Tools.Valves(zhihuiya_apikey=os.getenv('ZHIHUIYA_API_KEY'))
out = json.loads(asyncio.run(t.search_patents('CRISPR gene editing', limit=2)))
print('total_hits:', out['total_hits'], 'returned:', out['returned_count'])
for p in out['patents'][:2]:
    print(p['patent_number'], '|', p['title'][:50], '|', p['legal_status'], '|', p['assignees'][:40])
"
```
Expected: total_hits>0，≥1 条含 patent_number/title/legal_status

- [ ] **Step 2: 真实验证 read_patent**

```bash
python3 -c "
import asyncio, importlib.util, os
from dotenv import load_dotenv
load_dotenv('.env')
spec = importlib.util.spec_from_file_location('tool', 'tool.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
t = m.Tools(); t.valves = m.Tools.Valves(zhihuiya_apikey=os.getenv('ZHIHUIYA_API_KEY'))
out = asyncio.run(t.read_patent('US11530424B1', max_chars=800))
print(out[:800])
"
```
Expected: 含 "Patent Details"/权利要求/说明书 的 markdown

- [ ] **Step 3: 更新 docstring + 版本号**

`tool.py` 模块 docstring 的【工具用法】改为：

```
  【工具用法】
  1. search_papers(query)      → 多源并发搜索+去重，返回标题/作者/摘要/引用数/pdf_url
  2. read_paper(source, paper_id, pdf_url) → 读全文（后端工具 + pdf_url 自动 fallback）
  3. download_paper_to_knowledge(...)      → PDF 下载并加入 Knowledge 知识库
  4. search_patents(query)     → 智慧芽专利语义检索（需配 zhihuiya_apikey）
  5. read_patent(patent_number) → 读专利全文 markdown（权利要求+说明书+法律状态）
```

源清单区（【搜索源清单】末尾 zhihuiya 行后）加一行：

```
  · 智慧芽专利（独立工具 search_patents/read_patent，同 key 启用）: patsnap
```

版本号 `version: 2.3.0` → `version: 2.4.0`。

- [ ] **Step 4: 全量回归**

Run: `pytest tests/ -q`
Expected: 27 PASS

- [ ] **Step 5: 提交**

```bash
git add tool.py
git commit -m "feat(patsnap): update docstring and bump version to 2.4.0"
```

---

### Task 6: 文档 README/CLAUDE.md + 推送

**Files:**
- Modify: `README.md`、`CLAUDE.md`

- [ ] **Step 1: README**
特性区加一条专利能力；架构图加 patsnap 直连分支；源表加 patsnap 专利行（含 search_patents/read_patent）；配置区说明同一把 zhihuiya_apikey 即启用专利工具。

- [ ] **Step 2: CLAUDE.md**
源矩阵加 patsnap 专利源行（标注：独立工具、同 key、直连不经 mcpo、fetch 含权利要求/说明书全文）。

- [ ] **Step 3: 提交并推送**

```bash
git add README.md CLAUDE.md
git commit -m "docs: add patsnap patent source to README and CLAUDE.md"
git checkout main && git merge --no-ff feat/patsnap-patent
git push origin main
```

---

## Self-Review 记录

- **Spec 覆盖**：Global Constraints + T1(URL/常量) + T2(映射) + T3(search_patents) + T4(read_patent) + T5(真实验证+docstring+版本) + T6(文档+推送) — 覆盖 spec 全部 10 节。
- **占位符**：无 TBD/TODO；每步含完整代码与命令。
- **类型一致性**：`_PATSNAP_MCP_URL`(常量)、`_zhihuiya_call(..., url=None)`、`_patsnap_map_patent(doc)->dict`、`search_patents(...)->str`、`read_patent(...)->str` 在 T1-T6 间签名一致；复用的 `_zhihuiya_enabled_key`/`_redact_zhihuiya_key` 签名与现有实现一致。
