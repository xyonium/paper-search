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
`default_sources = "arxiv,pubmed,iacr,semantic,crossref,openalex,pmc,core,europepmc,dblp,openaire,doaj,hal,zenodo,google_scholar,zhihuiya,ieee"`

zhihuiya/ieee/firecrawl 在默认列表中，但**必须同时满足两个条件才启用**：(a) 源在 `default_sources`（或 `all` 模式、或调用时显式 `sources=`）；(b) 配了对应 key/url。即配 key/url 是必要非充分条件——未配时该源静默跳过（`want_zh`/`want_ieee`/`want_firecrawl` 为 False）。google_scholar 同批加回默认（无 proxy 可能 403，失败自动降级不影响整体）。

### Query Adaptation（查询特性，v2.5.0+）

`search_papers` 按源自动分发查询变体（`_make_query_variants`，确定性；v2.5.2 起字面源再经 `_distill_core_terms` 截断）：

| 源类 | 源 | 发送的查询 |
|---|---|---|
| **语义/分词** | openalex, semantic, crossref, europepmc, arxiv, core, patsnap | `original`（原始完整查询，保语义） |
| **字面关键词** | zhihuiya, doaj | `core`（去引号/裸露 OR/AND/NOT/中英噪声词）再 `_distill_core_terms` 截断到 ≤5 个高区分度术语 |
| **直连**（绕后端） | hal, zhihuiya, patsnap, dblp, zenodo, pubmed, pmc | hal 走 `_hal_search`（绕后端 hal.py 的 isoformat bug）；hal 用 core，zhihuiya 用 distilled；dblp 走 `_dblp_search`（v2.5.3+，绕后端 dblp.py 的并发 ConnectionError + 无退避重试 bug）用 original（CS 书目，非 CS 查询 0 结果属正常）；zenodo 走 `_zenodo_search`（v2.5.3+，绕后端 zenodo.py 的 published_date str→isoformat crash bug）用 original；pubmed/pmc 走 `_pubmed_search`/`_pmc_search`（v2.6+，绕后端 pubmed.py 无 timeout 无限挂起 bug）用 original |

**字面源截断临界点实测**（真实环境，决定 max_terms=5 的依据）：

| 源 | 11 词 | 6 词 | 5 词 | 3 词 | 临界 |
|---|---|---|---|---|---|
| zhihuiya | 0 | 恢复 | ✅ | ✅ | ≤6 词 |
| doaj | 0 | **0** | ✅ | ✅ | **必须 ≤5 词** |
| ~~iacr~~ | — | — | — | — | 已移出字面组（v2.6） |

→ 统一截到 **5 词**（doaj 是硬需求，其余更保守不亏）。截断按术语区分度：保留专业/罕见词
（含连字符/数字/括号、全大写缩写、长词），砍泛化词（sensor/coating/film/room temperature 等稀释相关性）。

**doaj 词数限制复核**（2026-08-06，同主题词 2→11 词逐个测 + DOAJ 官方 API 直打对照）：**不是词数硬限制，是查询组合太具体无匹配文献**。同主题词 3 词=11 命中、4 词=1、5 词=0，官方 API 直打结果一致；AND 连接 5 词同样 0 → DOAJ 把空格分词当 AND 处理（Lucene 默认），词越多要求同时命中的词越多、结果越窄直至 0。5 词截断保留是因为真实查询多为长尾描述性短语（biofouling/degradation/mechanism/vivo 堆叠必然 0 命中），截断后取高区分度词可恢复命中；并非"超过 N 词 API 报错/拒绝"。

**iacr 移出字面组与默认源**（v2.6）：iacr 无需精简——密码学主题词实测 2-8 词全部 ≥3 命中（ePrint 全文索引，相关性宽松）；之前 04:16 日志的 0 命中是"非密码学查询必然 0"（`continuous monitoring biofouling...`），不是词数问题。iacr 是密码学 ePrint **细分库**（非覆盖完整大学科），默认源应覆盖完整学科（计算机/生物/医学等），故移出默认；需要密码学文献时 `sources=iacr` 显式调用（仍走后端，用 original 查询）。

- `biorxiv/medrxiv` 非关键词检索，返回"该学科近30天新论文"；可用 `biorxiv_category`/`medrxiv_category` 传学科。
- `dblp` 后端 dblp.py 有 bug（v2.5.3 起改直连 `_dblp_search` 绕过）：并发/快速请求时 dblp 服务器直接断开连接（ConnectionError），后端无重试退避；且后端 dblp.py 无 rate-limit 感知，生产环境 500/ConnectionError 频繁。直连版加 3 次指数退避重试。注意 dblp 是 CS 书目库，仅收录计算机科学文献，非 CS 查询（如生物医学、材料科学）返回 0 属正常，非 bug。
- `zenodo` 后端 zenodo.py 有 bug（v2.5.3 起改直连 `_zenodo_search` 绕过）：published_date 传 str 给 Paper 对象，Paper.to_dict() 调 `.isoformat()` 崩溃。直连版正确解析日期字符串。Zenodo 是 OA 仓储，多数记录有 PDF。可选配 `zenodo_access_token`（Valves/UserValves）提额/访问受限记录，不配走公共 API（免费但限频）。直连 timeout=60s（公共 API 较慢）。
- `ieee` 后端 ieee.py 是骨架（v2.5.3 起改直连 `_ieee_search` 绕过）：`raise NotImplementedError` 占位，但 IEEE Xplore 有公开 REST API（`ieeexploreapi.ieee.org`），需配 `ieee_apikey`（Valves 管理员级或 UserValves 个人级）。返回 metadata 级（abstract+著录），OA 论文有 pdf_url 可直接下载，LOCKED 论文需机构访问。**间歇性挂起实测**（2026-08，host/容器均复现）：含常见词的较长 querytext 偶发挂起（30s read timeout 或 ~80s SSL EOF），同查询重试即 1.5s 恢复，非容器网络问题 → `_ieee_search` 与 dblp 同模式加 3 次退避重试（2s/4s），最坏延迟 ~68s，失败信息标注"重试3次"；4xx 不重试。
- `openaire` 后端 openaire.py 双 bug（v2.5.4 起改直连 `_openaire_search` 绕过，2026-08 实测 100% 必现，非慢/非网络）：路径1 `search/researchProducts` 端点已废弃 404（Tomcat 报错）；路径2 legacy fallback 用 `query=` 参数，OpenAIRE API 只认 `keywords=` → 400 Bad Request。直连版用 `search/publications?keywords=`，返回结构 `response.results.result[].metadata.oaf:entity.oaf:result`（title/creator/pid 为 dict 或 dict 列表，文本在 `$` 键，doi 取 `pid[@classid=doi]`）。OpenAIRE 是欧洲仓储聚合，OA 记录有 pdf_url。无 abstract 字段（API 不返回）。
- `pubmed`/`pmc` 后端 pubmed.py 致命 bug（v2.6 起改直连 `_pubmed_search`/`_pmc_search` 绕过，2026-08 实测**首次后端批量调用必现 180s 超时**的根因）：pubmed.py 走 **HTTPS** 且 `requests.get` **无 timeout**，境外出口对突发并发 TLS 不稳（SSL: UNEXPECTED_EOF_WHILE_READING，urllib3 对 SSL EOF 默认不重试）→ 偶发无限挂起，后端 `asyncio.gather` 等齐所有源 → 整批 180s 超时；连续两次超时的另一案例是 google_scholar 有界重试最坏 ~240s 叠加所致（偶发，网络条件决定）。直连版改走 **HTTP**（绕开境外 TLS 中间设备；NCBI 与 pmc/europepmc 等同 host，后端这些源实测零异常）+ `timeout=20` + 3 次退避（2s/4s），429/5xx/连接错误重试、4xx 不重试。pubmed efetch 返回 `PubmedArticleSet` XML；**pmc efetch 返回 JATS 全文 XML（pmc-articleset，非 PubmedArticleSet）**，`pub-id-type` 是 `pmcid`（值带 PMC 前缀）/`pmcaid`（纯数字），解析见 `_parse_pmc_article`。可选 `ncbi_api_key`（Valves/UserValves）：配了 10 req/s、否则 3 req/s；config.json 里 `NCBI_API_KEY` 仅对后端生效，直连走 tool.py 自己的阀值。pmc 记录恒有 PMCID（OA）→ `pdf_url` 恒有值；pubmed 仅当文章在 PMC 有存档时有 pdf_url。read_paper 对 pubmed 仍走后端 `read_pubmed_paper`（metadata 提示后自动降级 pdf_url）。
- **web 搜索双轨**（v2.5.4）：
  - **firecrawl = 独立源**（同时兼任二级 fallback）：配 `firecrawl_base_url`（如 `http://mcp:8000/firecrawl`）+ sources 含 `firecrawl`（已加入默认列表，未配 URL 静默跳过）。独立源走 mcpo `firecrawl_research_search_papers`（firecrawl 里唯一的学术文献专用端点，`firecrawl_search` 是通用 web 搜索）。**无域名限定**（research 接口不支持 include_domains），查询用 **core 变体**。结果 `source="firecrawl"`，`paper_id` 保留原 id（如 `arxiv:xxx`）。**元数据（v2.8）**：search 端点只回 title/abstract/id（abstract 上游 `/v2/search/research` **预截断 ~280 字符带"…"，请求参数无法改**，是 API 行为非解析问题）；authors/year/doi 由 `_research_inspect`（`firecrawl_research_inspect_paper`，GET `/v2/search/research/papers/{id}`）按 paper_id 富化——pmid/pmcid/arxiv 前缀覆盖好（实测 4/4 补上 authors+created 年份+doi），doi 前缀部分覆盖不到。**url 恒空**（research API 不回 url，read 时靠 paper_id 前缀推导落地页）。
  - **tavily = 主 fallback**（非独立源）：配 `tavily_base_url`（如 `http://api-key-rotator:8788/tavily`，经 api-key-rotator 代理 key 池轮转）才启用。任一**请求的源**出现连接/超时类错误（匹配 超时/timeout/ssl/eof/connection/502/503/504/429；0 命中或 400 参数错不触发）时触发，`POST {tavily_base_url}/search` 用 **original** 查询 + `include_domains` **动态限定**（`_fallback_domains` 映射自 失败源 ∪ 0结果源：`arxiv`→`arxiv.org`、`ieee`→`ieeexplore.ieee.org`、`semantic`→`semanticscholar.org` 等 21 个源映射；`backend` 聚合错误展开为全量学术域名；无映射源回退全量）。**返回量动态调整**：`N(1+K/3)` 上限 `min(3N, 20)`，N=原始 per-source limit，K=失败源数（`backend` 聚合按 4 计）——1 源失败≈1.33N，2 源≈1.67N，backend 聚合≈2.33N。结果 `source="tavily"`，`paper_id` 从 URL 提取（arxiv/ieee/pubmed/aclanthology/doi/biorxiv 模式），doi 另从 `raw_content` 正则回填。返回含 `fallback_domains`/`fallback_limit` 字段（LLM/用户可见）。**tavily `include_domains` 实测是硬过滤**（2026-08：pizza 查询+限定 arxiv.org 返回 5 条全在 arxiv.org 内）。api-key-rotator 原样透传请求体不干预。**tavily 元数据**：有 url（区别于 firecrawl）；无 authors，published_date 常空 → v2.8 起同样经 `_research_inspect` 按 paper_id 前缀富化 authors/year/doi（仅 arxiv/pubmed/doi 等可 inspect 的前缀，`web:` 前缀跳过）。
  - **firecrawl = 二级 fallback**（tavily 未配/调用失败/返回 0 条时落此）：用 `firecrawl_search`（通用 web 搜索，**非** research 端点）+ `includeDomains` 硬过滤（同 `_fallback_domains` 动态限定）。实测硬过滤：`includeDomains=["arxiv.org","ieeexplore.ieee.org"]` 返回 5 条全在限定域名内；`site:` 操作符返回空不可用。返回干净 JSON（url/title/description/position），比 research 端点的 Markdown 好解析。结果 `source="firecrawl"`。**fallback 链分工**：tavily 主（结构化+学术限定精准）→ firecrawl 备（同域名限定，覆盖 tavily 未配/失败/0 结果的场景）。
- **境外学术 API 间歇性不稳定**（2026-08 实测）：出口链路对突发并发 TLS 流不稳（疑似中间设备 RST/限速）。无 timeout 的源（pubmed.py，已 v2.6 直连绕过）会无限挂起；有 timeout 的源（arxiv 3×30s、google_scholar 3×(30s+重试等待)~240s、crossref/openalex/pmc/core/europepmc 各 30s）偶发挂起时**有界但仍慢**——后端 `asyncio.gather` 等齐最慢源，叠加 tool.py `_mcp_call` 超时重试 1 次（+3s）后整批最坏 ~360s 才返回。首批（DNS/连接冷 + 并发突发）更易触发。直连源均带 3 次退避。
- `all` 模式下后端源用 `_BACKEND_ALL_SOURCES`（排除直连源 hal/zhihuiya/patsnap/dblp/zenodo/ieee/openaire）；拆分时语义组用 `_SEMANTIC_ALL_SOURCES`（再排除字面组 doaj/iacr）。
- 截断发生时返回 `query_adapted` 字段，列出各字面源实际用的精简查询。

### read_paper 全文 fallback 链（v2.7）

`read_paper` 对无后端全文工具/后端仅元数据的源**自动链式降级，不返回死路错误**：

```
后端 read 工具（arxiv/biorxiv/medrxiv/iacr/semantic/doaj/hal）
  │ 返回"不支持"提示（_is_unsupported_msg 检测：>200字符+特征词
  │ "cannot be read directly"/"only metadata"/"metadata and abstracts are available"）
  ▼
pdf_url 直接下载 PDF → _pdf_to_text
  │ 失败（403付费墙/非PDF/扫描版）
  ▼
落地页推导：crossref→doi.org/{doi}、openalex→openalex.org/{id}、
           pubmed→pubmed.ncbi.nlm.nih.gov/{pmid}、pmc→pmc/articles/{pmcid}
  ▼
Unpaywall 查 OA 直链（有 DOI 时）→ 是PDF下载 / 非PDF落地页则网页抓取它
  ▼
网页抓取三级链 _web_read_fallback：jina reader（r.jina.ai，keyless 20RPM，
  配 jina_api_key 500RPM）→ tavily /extract（配 tavily_base_url）→
  firecrawl_scrape（配 firecrawl_base_url，onlyMainContent 去噪，能绕 acs.org 付费墙）
  反爬挑战页/占位页用 _is_web_junk 拦截（<500字符 或 开头含 just a moment/captcha/cloudflare 等）
```

- **前缀规范化** `_normalize_read_source`：firecrawl/tavily 的 paper_id 保留原始前缀（`arxiv:xxx`/`pmid:xxx`/`doi:xxx`），据此还原到对应源再处理（`pmid:40403180`→pubmed、`arxiv:xxx`→arxiv）。
- **`url` 参数**（v2.7.1）：read_paper 新增 `url` 参数，传 search 结果的出版商落地页 URL。链中**最先**尝试（质量最高）——jina 对 nature/cell/science 等出版商页面可抓 149k 全文。**google_scholar 必须传 url**：正常时其 search 结果 `url` 是出版商链接（cell.com/science.org/nature.com，容器实测），但限流降级时 Scholar 返回纯文本无链接标题（后端 `link['href']` 拿不到，`url=""`，容器直打 Scholar 实测 10/10 无 href）→ 此时 `gs_xxx` 是 hash 不可反推，报引导错误让 LLM 改走 download OA 链。
- **jina reader** 借鉴 reach-mcp `jina.py`：`https://r.jina.ai/{url}`，`Accept: text/plain` + `X-Retain-Images: none`，keyless 免费 20 RPM。对 pubmed/openalex/出版商落地页有效；对 acs.org/doi.org 付费墙返回挑战页（被 junk 拦截，落 firecrawl）。
- 成功时返回前缀标注来源：`[经 jina 网页抓取全文：url]` / `[经 firecrawl 网页抓取 OA 全文：url]`。
- **google_scholar 全文不靠"抓 Scholar 页面"补**：标题无链接只在请求被 Google 降级（指纹/IP 触发）时出现，此时 `url=""`、`gs_xxx` 是 hash 不可反推。cluster 版本页**无法**用 firecrawl/jina 抓（实测 firecrawl "The system can't perform the operation"、jina 403 automated queries）；标题反查不可靠（泛标题匹配错论文）。正确路径是 read_paper 传 `url`（正常时是出版商链接）；降级时改走 `download_paper_to_knowledge` 的 DOI/标题 OA 链。

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
| **Google Scholar** | ✅ | ❌ | ❌ | May return 403 without proxy；read 走 `url` 参数（出版商落地页，v2.7.1） |
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
