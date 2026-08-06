# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview & Architecture

This repository integrates multi-source academic paper searching, reading, and downloading into **OpenWebUI** using **mcpo** (MCP-to-OpenAPI bridge) and a custom OpenWebUI Python Tool.

```
[User / OpenWebUI UI]
        │
        ├── Native Remote MCP (Streamable HTTP): Consensus MCP / Exa MCP
        │
        └── OpenWebUI Python Tool (Bridge & Interceptor Layer)
                 │
                 ├── search_papers()  ──┬─► POST http://mcp:8000/papers/search_papers
                 │                      │    (Aggregates 16 active academic platforms)
                 │                      └─► zhihuiya MCP 直连 (streamable-http, apikey 启用)
                 │                           search_literature + literature_bibliography
                 │
                 ├── read_paper()     ──► Backend Tool (_READ_TOOLS) / zhihuiya bibliography
                 │                        ──► PDF Direct Fallback
                 │
                 ├── search_patents() ──► patsnap MCP 直连 (streamable-http, 同 zhihuiya key)
                 │                         patsnap_search (source=patent, 语义检索)
                 ├── read_patent()    ──► patsnap_fetch → 权利要求+说明书+法律状态 (Markdown)
                 │
                 └── download_paper_to_knowledge()
                          │
                          ├─► [Path 1] Direct pdf_url download (fastest)
                          │
                          └─► [Path 2] mcpo POST /papers/download_with_fallback
                                   │  (Saves PDF to shared Docker volume)
                                   ▼
                             Read from `/downloads/` (shared volume)
                                   │
                             Push to OpenWebUI `/api/v1/files/`
                                   │
                             Add to Knowledge Collection (`/api/v1/knowledge/{id}/file/add`)
```

---

## Environment & Service Setup

### Docker Compose
`mcp` (`mcpo` wrapping `paper-search-mcp`) and `open-webui` containers share the `paper-downloads` volume at `/downloads`:

```yaml
services:
  mcp:
    image: ghcr.io/open-webui/mcpo:main
    command: --port 8000 --api-key "YOUR_MCPO_API_KEY" --config /config/config.json --hot-reload
    volumes:
      - ./config.json:/config/config.json
      - paper-downloads:/downloads

  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    environment:
      - OPENWEBUI_URL=http://open-webui:8080
    volumes:
      - open-webui-data:/app/backend/data
      - paper-downloads:/downloads

volumes:
  paper-downloads:
  open-webui-data:
```

### mcpo Configuration (`config.json`)
Maps server name `"papers"` to `http://mcp:8000/papers`:

```json
{
  "mcpServers": {
    "papers": {
      "command": "uvx",
      "args": ["paper-search-mcp"],
      "env": {
        "PAPER_SEARCH_MCP_UNPAYWALL_EMAIL": "your_email@example.com",
        "PAPER_SEARCH_MCP_SEMANTIC_SCHOLAR_API_KEY": "s2k-xxx",
        "PAPER_SEARCH_MCP_DOAJ_API_KEY": "xxx",
        "PAPER_SEARCH_MCP_ZENODO_ACCESS_TOKEN": "xxx",
        "NCBI_API_KEY": "xxx",
        "PAPER_SEARCH_MCP_GOOGLE_SCHOLAR_PROXY_URL": "http://your_proxy:port"
      }
    }
  }
}
```

---

## Data Sources Matrix

### Verified Active Sources
Default selection in UserValves:
`default_sources = "arxiv,pubmed,iacr,semantic,crossref,openalex,pmc,core,europepmc,dblp,openaire,doaj,hal"`

### Query Adaptation（查询特性，v2.5.0+）

`search_papers` 按源自动分发查询变体（`_make_query_variants`，确定性；v2.5.2 起字面源再经 `_distill_core_terms` 截断）：

| 源类 | 源 | 发送的查询 |
|---|---|---|
| **语义/分词** | openalex, semantic, crossref, pmc, europepmc, pubmed, arxiv, openaire, core, patsnap | `original`（原始完整查询，保语义） |
| **字面关键词** | zhihuiya, doaj, iacr | `core`（去引号/裸露 OR/AND/NOT/中英噪声词）再 `_distill_core_terms` 截断到 ≤5 个高区分度术语 |
| **直连**（绕后端） | hal, zhihuiya, patsnap, dblp, zenodo | hal 走 `_hal_search`（绕后端 hal.py 的 isoformat bug）；hal 用 core，zhihuiya 用 distilled；dblp 走 `_dblp_search`（v2.5.3+，绕后端 dblp.py 的并发 ConnectionError + 无退避重试 bug）用 original（CS 书目，非 CS 查询 0 结果属正常）；zenodo 走 `_zenodo_search`（v2.5.3+，绕后端 zenodo.py 的 published_date str→isoformat crash bug）用 original |

**字面源截断临界点实测**（真实环境，决定 max_terms=5 的依据）：

| 源 | 11 词 | 6 词 | 5 词 | 3 词 | 临界 |
|---|---|---|---|---|---|
| zhihuiya | 0 | 恢复 | ✅ | ✅ | ≤6 词 |
| doaj | 0 | **0** | ✅ | ✅ | **必须 ≤5 词** |
| iacr | 0 | 恢复 | ✅ | ✅ | ≤6 词 |

→ 三者统一截到 **5 词**（doaj 是硬需求，其余更保守不亏）。截断按术语区分度：保留专业/罕见词
（含连字符/数字/括号、全大写缩写、长词），砍泛化词（sensor/coating/film/room temperature 等稀释相关性）。

- `biorxiv/medrxiv` 非关键词检索，返回"该学科近30天新论文"；可用 `biorxiv_category`/`medrxiv_category` 传学科。
- `dblp` 后端 dblp.py 有 bug（v2.5.3 起改直连 `_dblp_search` 绕过）：并发/快速请求时 dblp 服务器直接断开连接（ConnectionError），后端无重试退避；且后端 dblp.py 无 rate-limit 感知，生产环境 500/ConnectionError 频繁。直连版加 3 次指数退避重试。注意 dblp 是 CS 书目库，仅收录计算机科学文献，非 CS 查询（如生物医学、材料科学）返回 0 属正常，非 bug。
- `zenodo` 后端 zenodo.py 有 bug（v2.5.3 起改直连 `_zenodo_search` 绕过）：published_date 传 str 给 Paper 对象，Paper.to_dict() 调 `.isoformat()` 崩溃。直连版正确解析日期字符串。Zenodo 是 OA 仓储，多数记录有 PDF。可选配 `zenodo_access_token`（Valves/UserValves）提额/访问受限记录，不配走公共 API（免费但限频）。直连 timeout=60s（公共 API 较慢）。
- `ieee` 后端 ieee.py 是骨架（v2.5.3 起改直连 `_ieee_search` 绕过）：`raise NotImplementedError` 占位，但 IEEE Xplore 有公开 REST API（`ieeexploreapi.ieee.org`），需配 `ieee_apikey`（Valves 管理员级或 UserValves 个人级）。返回 metadata 级（abstract+著录），OA 论文有 pdf_url 可直接下载，LOCKED 论文需机构访问。**间歇性挂起实测**（2026-08，host/容器均复现）：含常见词的较长 querytext 偶发挂起（30s read timeout 或 ~80s SSL EOF），同查询重试即 1.5s 恢复，非容器网络问题 → `_ieee_search` 与 dblp 同模式加 3 次退避重试（2s/4s），最坏延迟 ~68s，失败信息标注"重试3次"；4xx 不重试。
- `openaire` 后端 openaire.py 双 bug（v2.5.4 起改直连 `_openaire_search` 绕过，2026-08 实测 100% 必现，非慢/非网络）：路径1 `search/researchProducts` 端点已废弃 404（Tomcat 报错）；路径2 legacy fallback 用 `query=` 参数，OpenAIRE API 只认 `keywords=` → 400 Bad Request。直连版用 `search/publications?keywords=`，返回结构 `response.results.result[].metadata.oaf:entity.oaf:result`（title/creator/pid 为 dict 或 dict 列表，文本在 `$` 键，doi 取 `pid[@classid=doi]`）。OpenAIRE 是欧洲仓储聚合，OA 记录有 pdf_url。无 abstract 字段（API 不返回）。
- **firecrawl 兜底**（v2.5.4 新增，Valves/UserValves `firecrawl_fallback` 开关，默认开）：任一**请求的源**出现连接/超时类错误（匹配 超时/timeout/ssl/eof/connection/502/503/504/429 等；0 命中或 400 参数错不触发）时，经 mcpo `firecrawl_research_search_papers` 用 **original** 查询兜底（firecrawl 内部有查询处理，无需截断）。结果标注 `source="firecrawl"`，`paper_id` 保留原始 id（如 `arxiv:xxx`）。定位：只在主链出现网络类失败时补位，不替代结构化 API 源。
- **境外学术 API 间歇性不稳定**（2026-08 实测）：出口链路对突发并发 TLS 流不稳（疑似中间设备 RST/限速），后端 mcpo 一次 fan-out 11 源时随机个别 host（eutils/openaire/ieee 轮流）SSL EOF 或握手超时，同请求重试即恢复 → `_mcp_call` 超时后自动重试 1 次（+3s），直连源均带 3 次退避。
- `all` 模式下后端源用 `_BACKEND_ALL_SOURCES`（排除直连源 hal/zhihuiya/patsnap/dblp/zenodo/ieee/openaire）；拆分时语义组用 `_SEMANTIC_ALL_SOURCES`（再排除字面组 doaj/iacr）。
- 截断发生时返回 `query_adapted` 字段，列出各字面源实际用的精简查询。

| Platform | Search | Read Tool | Native Download | Notes |
|---|---|---|---|---|
| **arXiv** | ✅ | `read_arxiv_paper` | ✅ | Open PDF, fast & reliable |
| **PubMed** | ✅ | ⚠️ metadata only | ❌ | Requires `NCBI_API_KEY` for rate limits |
| **bioRxiv / medRxiv** | ✅ | `read_biorxiv/medrxiv_paper` | ✅ | Open preprints (DOI based) |
| **Semantic Scholar** | ✅ | `read_semantic_paper` | ✅ (OA) | Requires `SEMANTIC_SCHOLAR_API_KEY` |
| **Crossref** | ✅ | ⚠️ metadata only | ❌ | Citation & DOI backbone |
| **OpenAlex** | ✅ | ⚠️ metadata only | ❌ | Open metadata backbone |
| **PMC / Europe PMC** | ✅ | ⚠️ Fallback to PDF | ✅ (OA) | High quality biomedical full-text |
| **CORE** | ✅ | ⚠️ Fallback to PDF | ✅ (OA) | Global repository aggregator |
| **Google Scholar** | ✅ | ❌ | ❌ | May return 403 without proxy |
| **IACR** | ✅ | `read_iacr_paper` | ✅ | Cryptography ePrints |
| **OpenAIRE / DOAJ / HAL** | ✅ | Varies / Fallback | Record-dependent | Domain repositories |
| **dblp** | ✅ (v2.5.3+ 直连) | ⚠️ ee/DOI → OA fallback | Record-dependent | CS 书目库，无全文；read_paper 自动查 ee 链接 → arXiv PDF / Unpaywall OA；付费墙返回错误提示走 download |
| **Zenodo** | ✅ (v2.5.3+ 直连) | ⚠️ pdf_url fallback | ✅ (多数 OA) | OA 仓储，多数记录有 PDF；read 走 pdf_url 直接提取；可选 `zenodo_access_token`（Valves/UserValves）提额/访问受限记录 |
| **IEEE** | ✅ (v2.5.3+ 直连，需 key) | ⚠️ pdf_url fallback (OA) | ⚠️ (OA only) | IEEE Xplore REST API，metadata 级（abstract+著录）；OA 论文有 pdf_url，LOCKED 需机构访问 |

### Optional Premium Source (apikey-gated, **direct** — not via mcpo)

| Platform | Search | Read Tool | Native Download | Notes |
|---|---|---|---|---|
| **zhihuiya (智慧芽)** | ✅ `search_literature` | ⚠️ `literature_bibliography` (abstract+著录) | ❌ | 科学文献 MCP，tool.py 经 `streamablehttp_client` 直连。需 `Valves/UserValves.zhihuiya_apikey` 非空且 `zhihuiya_enabled` 才启用；search=search_literature+批量 literature_bibliography(补 abstract)；全文靠 doi 走 OA fallback 链 |
| **patsnap (智慧芽专利)** | ✅ `patsnap_search` | ✅ `patsnap_fetch` (全文 Markdown) | ❌ | 专利 MCP（同公司同 key）。独立工具 `search_patents`/`read_patent`（不并入 search_papers）。`read_patent` 返回**权利要求+说明书+法律状态**（默认 `module=['basic','legal']`），截断到 max_chars |

### 非默认源（按"是否支持关键词搜索"区分）

| 源 | 关键词搜索 | 状态 | 说明 |
|---|---|---|---|
| **bioRxiv / medRxiv** | ❌（学科分类过滤） | 移出默认 | **学科近30天新论文浏览**，非关键词检索；显式 `sources="biorxiv"` + `biorxiv_category` 使用。默认启用会返回无关结果误导 |
| **Google Scholar** | ✅ | 反爬 | 支持搜索，但无 proxy 易 403 |
| **SSRN** | ✅ | 反爬 | 支持搜索，Cloudflare 403 |
| **BASE** | ✅ | 反爬 | 支持搜索，但 IP 被封（403 Access denied）；OAI-PMH 端点不稳 |
| **CiteSeerX** | ✅（代码有） | 端点已死 | 支持搜索，但 API 重定向 archive.org 404，无法修复 |
| **Zenodo** | ✅（Elasticsearch） | ✅ 已直连修复 | 后端 `zenodo.py` isoformat bug（published_date str 传给 Paper）→ v2.5.3 起改直连 `_zenodo_search`，多数记录有 OA PDF |
| **IEEE** | ✅（REST API） | ✅ 已直连修复 | 后端 `ieee.py` 是骨架（`raise NotImplementedError`），但 IEEE Xplore 有公开 REST API → v2.5.3 起改直连 `_ieee_search`，需配 `ieee_apikey`（Valves/UserValves），metadata 级（abstract+著录），OA 论文有 pdf_url |
| **ACM** | ⚠️ 骨架 | 未实现 | `search is not yet implemented`，无公开 REST API，需 ACM 会员，tool.py 层无法绕过 |
| **Unpaywall** | ❌ | 仅 DOI 查询 | **不支持关键词搜索**；用于 download fallback 链按 DOI 查 OA PDF |

---

## Fallback & Download Mechanics

### 1. `download_with_fallback` Pipeline
```
[Request: source, paper_id, doi, title]
   │
   ▼
1. Source-native Download
   (arXiv, bioRxiv, medrxiv, iacr, semantic, pmc, core, europepmc, etc.)
   │ Failed?
   ▼
2. Open Access Repository Search
   Query OpenAIRE / CORE / Europe PMC / PMC using DOI or Title
   │ Failed?
   ▼
3. Unpaywall Resolution
   Query Unpaywall API using DOI -> Resolve direct open access PDF URL
   │ Failed?
   ▼
4. Sci-Hub Fallback (If use_scihub=True)
   Attempt download via Sci-Hub mirror (default: https://sci-hub.se)
```

### 2. File Ingestion into OpenWebUI Knowledge Base
1. **Fetch**: Direct Stream via `pdf_url` OR backend `download_with_fallback` reading from `/downloads/{filename}.pdf`.
2. **Upload**: `POST {OPENWEBUI_URL}/api/v1/files/` with multipart form-data.
3. **Associate**: `POST {OPENWEBUI_URL}/api/v1/knowledge/{knowledge_id}/file/add` with `{"file_id": "file_id_xxx"}`.
4. **Cleanup**: Remove local file from `/downloads/` after upload.

---

## Code Reference: OpenWebUI Tool Script
in:./tool.py
