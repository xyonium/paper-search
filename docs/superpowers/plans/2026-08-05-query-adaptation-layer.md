# 查询词适配层 + hal 直连 + biorxiv 学科参数 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 tool.py 的 search_papers 增加确定性查询词适配层（按源分发 original/精简 core 变体）、hal 直连（绕后端 isoformat bug）、biorxiv/medrxiv 学科参数，提升多源命中率且不损语义。

**Architecture:** 全部在 tool.py 编排层，不改第三方后端。`_make_query_variants` 生成 original/core 两个变体；`search_papers` 把 sources 按语义组/字面组拆分，core≠original 时并发调两次后端再合并；zhihuiya（已有）与新增 hal 直连用 core；biorxiv/medrxiv 学科参数透传后端。

**Tech Stack:** Python 3, OpenWebUI Tool, `anyio`, `requests`, `mcp` SDK, `pytest`

**Spec:** `docs/superpowers/specs/2026-08-05-query-adaptation-layer-design.md`

## Global Constraints

- 仅修改 `tool.py`；测试追加到 `tests/test_zhihuiya.py`（沿用其 importlib loader）。**不改** mcpo config.json、docker-compose、第三方后端 paper-search-mcp。
- **字面组（用 core 变体）严格为**：`zhihuiya, doaj, iacr`（仅此三源实测长自然语言会 0、精简恢复）。其余全部归语义组用 original。
- **语义组（用 original）**：openalex, semantic, crossref, pmc, europepmc, pubmed, arxiv, biorxiv, medrxiv, openaire, core, dblp, hal, patsnap。
- core 变体：去引号字符、去裸露布尔运算符（独立成词的 OR/AND/NOT）、去中英噪声词、CJK 感知，**不截断词数**；core 为空回退 original。
- `core == original` 时只调一次后端（零开销）；不等时拆两次（语义组 original、字面组 core）并发合并。
- hal 直连用 `anyio.to_thread.run_sync` 包同步 requests（线程池，**绝不阻塞事件循环**），加超时，错误进 `errors['hal']`。
- 所有 zhihuiya/patsnap 错误消息经 `_redact_zhihuiya_key` 脱敏；hal 无 key 无需脱敏。

## 文件结构

- Modify: `tool.py`
  - 模块级常量/函数：`_QUERY_NOISE_EN`、`_QUERY_NOISE_CN`、`_make_query_variants()`、`_LITERAL_SOURCES`、`_DIRECT_SOURCES`（zhihuiya/hal/patsnap，不进后端）
  - 新增 `Tools._hal_search()`（直连，实例方法，经 `anyio.to_thread.run_sync`）
  - 改造 `Tools.search_papers()`（分组分发 + hal/zhihuiya 直连分支 + biorxiv/medrxiv 学科参数）
  - 更新 docstring、版本号 2.4.0→2.5.0
- Modify: `tests/test_zhihuiya.py`（追加查询适配与 hal 测试）

现有可复用接口（已实现，勿改行为）：
- `Tools._mcp_call(tool, args, timeout=180)`（同步）
- `Tools._trim_paper(p, max_abstract=600) -> dict`
- `Tools._zhihuiya_search(query, limit, key) -> list`（async）
- `Tools._zhihuiya_enabled_key(__user__=None) -> tuple`

---

### Task 1: 查询变体生成器 `_make_query_variants` + 源分组常量

**Files:**
- Modify: `tool.py`（模块级，import 区之后、`class Tools` 之前）
- Test: `tests/test_zhihuiya.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `_make_query_variants(query: str) -> dict` → `{"original": str, "core": str}`
  - `LITERAL_SOURCES = frozenset({"zhihuiya", "doaj", "iacr"})`
  - `DIRECT_SOURCES = frozenset({"zhihuiya", "hal", "patsnap"})`（直连，不进后端 sources）

- [ ] **Step 1: 写失败测试**

`tests/test_zhihuiya.py` 追加（`tool_mod` 已可用）：

```python
def test_variants_strip_quotes_and_boolean():
    v = tool_mod._make_query_variants('"early signal drop" glucose sensor OR biosensor')
    assert v["original"] == '"early signal drop" glucose sensor OR biosensor'
    assert '"' not in v["core"] and " OR " not in v["core"]
    assert "early signal drop" in v["core"] and "glucose" in v["core"]


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
    assert tool_mod.LITERAL_SOURCES == frozenset({"zhihuiya", "doaj", "iacr"})
    assert "hal" in tool_mod.DIRECT_SOURCES and "zhihuiya" in tool_mod.DIRECT_SOURCES
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: FAIL，`AttributeError: module 'tool' has no attribute '_make_query_variants'`

- [ ] **Step 3: 实现**

`tool.py` 在 `import` 区之后、`class Tools` 之前插入：

```python
# ---------- 查询词适配（参考 reach-mcp query_core，确定性，不截断词数） ----------
_QUERY_NOISE_EN = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "of", "in", "on", "for",
    "with", "about", "to", "how", "what", "which", "who", "why", "when",
    "where", "does", "should", "could", "would",
    "best", "top", "latest", "new", "news", "recent", "advances", "advance",
    "review", "reviews", "overview", "progress", "developments", "trends",
    "using", "based", "via", "their", "its", "his", "her", "we", "you",
    "study", "studies", "research", "analysis", "investigation",
})
_QUERY_NOISE_CN = frozenset({
    "最新", "研究进展", "进展", "综述", "怎么样", "如何", "什么", "哪些",
    "哪个", "推荐", "对比", "比较", "最近", "近期", "现状", "应用", "方法",
})
_QUERY_NOISE_CN_SORTED = sorted(_QUERY_NOISE_CN, key=len, reverse=True)
_QUERY_BOOL_RE = re.compile(r"\b(?:OR|AND|NOT)\b")
_QUERY_CJK_RE = re.compile(r"[一-鿿㐀-䶿]")
_QUERY_PREFIXES = (
    "what are the latest", "what are the", "what is the", "what are", "what is",
    "recent advances in", "latest advances in", "advances in", "progress in",
    "review of", "research on", "studies on",
)

LITERAL_SOURCES = frozenset({"zhihuiya", "doaj", "iacr"})
DIRECT_SOURCES = frozenset({"zhihuiya", "hal", "patsnap"})


def _make_query_variants(query: str) -> dict:
    """生成 original/core 两个查询变体。core 去引号/裸露布尔/中英噪声词，
    CJK 感知，不截断词数（保语义）；全噪声时回退 original。"""
    original = (query or "").strip()
    text = original.lower().rstrip("?!.")
    if not text:
        return {"original": original, "core": original}
    for p in _QUERY_PREFIXES:
        if text.startswith(p + " "):
            text = text[len(p):].strip()
            break
    text = text.replace('"', " ").replace("'", " ")
    text = _QUERY_BOOL_RE.sub(" ", text)
    for phrase in _QUERY_NOISE_CN_SORTED:
        text = text.replace(phrase, " ")
    kept = [w for w in text.split() if w and w not in _QUERY_NOISE_EN]
    core = " ".join(kept).strip()
    core = re.sub(r"\s+", " ", core)
    if not core:
        core = original
    return {"original": original, "core": core}
```

`tool.py` import 区确保有 `import re`（若无则加）。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: 全部 PASS（27 旧 + 6 新 = 33）

- [ ] **Step 5: 提交**

```bash
git add tool.py tests/test_zhihuiya.py
git commit -m "feat(query): add _make_query_variants and source-group constants"
```

---

### Task 2: hal 直连 `_hal_search`

**Files:**
- Modify: `tool.py`（`_zhihuiya_search` 之后）
- Test: `tests/test_zhihuiya.py`

**Interfaces:**
- Consumes: 无（用 requests + anyio，模块已 import）
- Produces: `async Tools._hal_search(query: str, limit: int) -> list` — 返回 `_trim_paper` 兼容 dict 列表；失败抛 RuntimeError

- [ ] **Step 1: 写失败测试（mock requests）**

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: FAIL，`AttributeError: 'Tools' object has no attribute '_hal_search'`

- [ ] **Step 3: 实现**

`tool.py` 在 `_zhihuiya_search` 之后加：

```python
    _HAL_SEARCH_URL = "https://api.archives-ouvertes.fr/search/"
    _HAL_FIELDS = ("halId_s,title_s,authFullName_s,abstract_s,doiId_s,"
                   "publicationDateY_i,producedDateY_i,submittedDate_s,"
                   "fileMain_s,uri_s,docType_s")

    async def _hal_search(self, query: str, limit: int) -> list:
        """直连 HAL API（Solr JSON，无需 key）检索，返回 _trim_paper 兼容 dict 列表。
        绕过第三方后端 hal.py 的 isoformat bug。anyio 线程池包装，不阻塞事件循环。"""
        def _fetch():
            r = requests.get(
                self._HAL_SEARCH_URL,
                params={
                    "q": query,
                    "fl": self._HAL_FIELDS,
                    "rows": max(1, min(int(limit), 100)),
                    "wt": "json",
                    "sort": "score desc",
                },
                headers={"User-Agent": "paper-search-mcp/1.0", "Accept": "application/json"},
                timeout=20,
            )
            r.raise_for_status()
            return r.json()

        try:
            data = await anyio.to_thread.run_sync(_fetch)
        except Exception as e:
            raise RuntimeError(f"HAL 检索失败: {e}")

        docs = ((data or {}).get("response") or {}).get("docs") or []
        papers = []
        for d in docs:
            if not isinstance(d, dict):
                continue
            year = d.get("publicationDateY_i") or d.get("producedDateY_i") or ""
            pub = str(year) if year else (str(d.get("submittedDate_s", "") or "")[:10])
            title = d.get("title_s") or [""]
            authors = d.get("authFullName_s") or []
            abstract = d.get("abstract_s") or [""]
            papers.append({
                "title": title[0] if isinstance(title, list) else str(title),
                "authors": "; ".join(a for a in authors if a),
                "published_date": pub,
                "abstract": (abstract[0] if isinstance(abstract, list) else str(abstract)),
                "paper_id": f"hal:{d.get('halId_s', '')}",
                "doi": d.get("doiId_s") or "",
                "source": "hal",
                "pdf_url": d.get("fileMain_s") or "",
                "citations": 0,
                "url": d.get("uri_s") or "",
            })
        return papers
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: 35 PASS

- [ ] **Step 5: 提交**

```bash
git add tool.py tests/test_zhihuiya.py
git commit -m "feat(hal): add direct HAL search bypassing backend isoformat bug"
```

---

### Task 3: search_papers 分组分发 + hal/zhihuiya 直连 + 学科参数

**Files:**
- Modify: `tool.py`（`search_papers` 方法，约 line 430-520）
- Test: `tests/test_zhihuiya.py`

**Interfaces:**
- Consumes: `_make_query_variants`(T1)、`LITERAL_SOURCES`/`DIRECT_SOURCES`(T1)、`_hal_search`(T2)、`_zhihuiya_search`(已有)、`_zhihuiya_enabled_key`(已有)、`_mcp_call`(已有)、`_trim_paper`(已有)
- Produces: `search_papers(query, max_results_per_source, sources, biorxiv_category="", medrxiv_category="", __user__)` 返回合并 JSON

- [ ] **Step 1: 写失败测试**

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: FAIL（当前 search_papers 不支持分组/学科参数/hal 直连）

- [ ] **Step 3: 改造 `search_papers`**

签名加两个参数；方法体替换 src 计算之后的逻辑（保留 `_trim_paper`/合并/`_zhihuiya_enabled_key`）。新逻辑：

```python
        uv = __user__.get("valves") if __user__ else None
        src = (
            sources
            or (uv.default_sources if uv else None)
            or "arxiv,semantic,openalex,pubmed,pmc,core,europepmc"
        )
        src_set = {s.strip().lower() for s in src.split(",") if s.strip()}
        all_mode = src.strip().lower() == "all"

        variants = self._make_query_variants(query)
        original, core = variants["original"], variants["core"]

        zh_enabled, zh_key = self._zhihuiya_enabled_key(__user__)
        want_zh = zh_enabled and ("zhihuiya" in src_set or all_mode)
        want_hal = "hal" in src_set or all_mode

        # 直连源不进后端 sources
        backend_set = src_set - self.DIRECT_SOURCES
        if all_mode:
            backend_set = None  # None 表示传 "all" 给后端

        # 后端按变体分组：字面组用 core，语义组用 original
        backend_literal = (src_set & self.LITERAL_SOURCES) - self.DIRECT_SOURCES
        literal_query = core if core != original else original

        async def _backend_all():
            # core==original 或无需拆分时，一次调用（含全部后端源）
            args = {
                "query": original,
                "max_results_per_source": max_results_per_source,
                "sources": ("all" if all_mode else ",".join(sorted(backend_set))),
            }
            if biorxiv_category:
                args["biorxiv_category"] = biorxiv_category
            if medrxiv_category:
                args["medrxiv_category"] = medrxiv_category
            return await anyio.to_thread.run_sync(self._mcp_call, "search_papers", args)

        async def _backend_split():
            # core!=original：语义组 original + 字面组 core 两次并发
            sem_set = (backend_set - self.LITERAL_SOURCES) if backend_set is not None else None
            tasks = []
            labels = []
            if sem_set is None or sem_set:
                async def _sem():
                    args = {"query": original,
                            "max_results_per_source": max_results_per_source,
                            "sources": ("all" if all_mode else ",".join(sorted(sem_set)))}
                    if biorxiv_category: args["biorxiv_category"] = biorxiv_category
                    if medrxiv_category: args["medrxiv_category"] = medrxiv_category
                    return await anyio.to_thread.run_sync(self._mcp_call, "search_papers", args)
                tasks.append(_sem()); labels.append("sem")
            if backend_literal:
                async def _lit():
                    return await anyio.to_thread.run_sync(
                        self._mcp_call, "search_papers",
                        {"query": literal_query,
                         "max_results_per_source": max_results_per_source,
                         "sources": ",".join(sorted(backend_literal))})
                tasks.append(_lit()); labels.append("lit")
            results = await asyncio.gather(*tasks, return_exceptions=True)
            merged = {"papers": [], "source_results": {}, "errors": {}}
            for lbl, r in zip(labels, results):
                if isinstance(r, Exception):
                    merged["errors"][lbl] = str(r)
                    continue
                if isinstance(r, dict):
                    merged["papers"].extend(r.get("papers", []))
                    merged["source_results"].update(r.get("source_results", {}))
                    merged["errors"].update(r.get("errors", {}))
            return merged

        async def _zh():
            return await self._zhihuiya_search(literal_query, max_results_per_source, zh_key)

        async def _hal():
            return await self._hal_search(literal_query, max_results_per_source)

        # 组装并发分支
        branches = {}
        branches["backend"] = _backend_split() if core != original else _backend_all()
        if want_zh:
            branches["zhihuiya"] = _zh()
        if want_hal:
            branches["hal"] = _hal()

        keys = list(branches)
        results = await asyncio.gather(*branches.values(), return_exceptions=True)
        outcome = dict(zip(keys, results))

        backend_result = outcome.get("backend")
        zh_result = outcome.get("zhihuiya")
        hal_result = outcome.get("hal")

        # 后端失败处理：若任一直连源有结果则保留，否则报错
        direct_ok = [r for r in (zh_result, hal_result) if isinstance(r, list) and r]
        if isinstance(backend_result, Exception):
            if direct_ok:
                result = {"papers": [], "source_results": {},
                          "errors": {"backend": str(backend_result)}}
            else:
                return json.dumps(
                    {"error": f"后端 search_papers 调用失败: {backend_result}"},
                    ensure_ascii=False)
        else:
            result = backend_result

        if not isinstance(result, dict):
            return json.dumps({"error": "backend 返回异常", "raw": str(result)[:500]}, ensure_ascii=False)

        papers = [self._trim_paper(p) for p in result.get("papers", [])]
        source_results = dict(result.get("source_results") or {})
        errors = dict(result.get("errors") or {})

        if want_zh:
            if isinstance(zh_result, Exception):
                source_results["zhihuiya"] = 0
                errors["zhihuiya"] = str(zh_result)
            elif zh_result is not None:
                zp = [self._trim_paper(p) for p in zh_result]
                papers.extend(zp)
                source_results["zhihuiya"] = len(zp)
        if want_hal:
            if isinstance(hal_result, Exception):
                source_results["hal"] = 0
                errors["hal"] = str(hal_result)
            elif hal_result is not None:
                hp = [self._trim_paper(p) for p in hal_result]
                papers.extend(hp)
                source_results["hal"] = len(hp)

        return json.dumps(
            {"query": query, "total": len(papers),
             "source_results": source_results, "errors": errors, "papers": papers},
            ensure_ascii=False, indent=2)
```

注意：删除原方法中旧的 `_backend`/`_zh`/`backend_result`/`zh_result` 重复逻辑与旧 return，确保只有一份；`biorxiv_category`/`medrxiv_category` 加入函数签名（默认 `""`）。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_zhihuiya.py -v`
Expected: 38 PASS（含原有 search_papers/zhihuiya 测试仍通过）

- [ ] **Step 5: 提交**

```bash
git add tool.py tests/test_zhihuiya.py
git commit -m "feat(query): split sources by variant, direct hal, biorxiv/medrxiv category params"
```

---

### Task 4: 真实端点验证 + docstring/版本号

**Files:**
- Modify: `tool.py`（docstring、version）

- [ ] **Step 1: 真实验证字面源精简恢复**

```bash
docker exec open-webui python3 -c "
import json,urllib.request,time
def call(a,t=60):
    req=urllib.request.Request('http://mcp:8000/papers/search_papers',data=json.dumps(a).encode(),headers={'Content-Type':'application/json'})
    import time;s=time.time()
    with urllib.request.urlopen(req,timeout=t) as r: return time.time()-s,json.loads(r.read())
# 长自然语言，doaj+iacr 应经 core 变体恢复
dt,res=call({'query':'what are recent advances in zero knowledge proof systems for blockchain','max_results_per_source':3,'sources':'iacr,doaj,openalex'})
print(dt, res.get('source_results'), list(res.get('errors',{})))
"
```
Expected: iacr/doaj 从 0 → 有结果（注：此验证在 tool.py 部署到 open-webui 后才有分组逻辑；本地先验证 _make_query_variants 对后端的效果见 Step 3）

- [ ] **Step 2: 真实验证 hal 直连**

```bash
python3 -c "
import asyncio, importlib.util
spec = importlib.util.spec_from_file_location('tool','tool.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
t = m.Tools()
papers = asyncio.run(t._hal_search('glucose biosensor', 3))
print('hal n=', len(papers))
for p in papers[:2]: print(p['title'][:50], '|', p['published_date'], '|', p['source'], '|', p['doi'])
"
```
Expected: hal n>0，published_date 为年份字符串，无 isoformat 报错

- [ ] **Step 3: 更新 docstring + 版本号**
模块 docstring【工具用法】补查询适配说明；biorxiv/medrxiv 源说明加学科参数与"近30天新论文"标注；版本号 2.4.0→2.5.0。

- [ ] **Step 4: 全量回归**

Run: `pytest tests/ -q`
Expected: 38 PASS

- [ ] **Step 5: 提交**

```bash
git add tool.py
git commit -m "feat(query): update docstring and bump version to 2.5.0"
```

---

### Task 5: 文档 README/CLAUDE.md + 推送

**Files:**
- Modify: `README.md`、`CLAUDE.md`

- [ ] **Step 1: README** — 特性区加"智能查询适配"；源表标注各源查询特性（语义/字面/直连/学科过滤）。
- [ ] **Step 2: CLAUDE.md** — 源矩阵加"查询特性"列（语义原样/字面精简/直连/学科近30天）。
- [ ] **Step 3: 提交并推送**

```bash
git add README.md CLAUDE.md
git commit -m "docs: query adaptation layer, hal direct, biorxiv category"
git checkout main && git merge --no-ff feat/query-adaptation && git push origin main
```

---

## Self-Review 记录

- **Spec 覆盖**：Global Constraints + T1(变体/分组常量) + T2(hal直连) + T3(search_papers 分组+直连+学科参数) + T4(真实验证+docstring+版本) + T5(文档+推送) — 覆盖 spec 全部 11 节。
- **占位符**：无 TBD/TODO；每步含完整代码与命令。
- **类型一致性**：`_make_query_variants(query)->{"original","core"}`、`LITERAL_SOURCES`/`DIRECT_SOURCES`、`_hal_search(query,limit)->list`、`search_papers(...,biorxiv_category,medrxiv_category,...)` 在 T1-T5 间一致；复用接口签名与现有实现一致。
