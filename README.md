# 📚 OpenWebUI Academic Paper Search & Knowledge Base Integration

> **One prompt → 18+ academic databases → full text → your RAG Knowledge Base.**
> Multi-source academic paper search, full-text reading, and automatic PDF ingestion into **OpenWebUI Knowledge Base** (RAG) — powered by `mcpo` + `paper-search-mcp`, with direct **zhihuiya (智慧芽)** literature/patent and **IEEE Xplore** integration.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![OpenWebUI](https://img.shields.io/badge/OpenWebUI-Tool-blue)](https://github.com/open-webui/open-webui)
[![MCP](https://img.shields.io/badge/MCP-mcpo-green)](https://github.com/open-webui/mcpo)

---

## 🌟 Features

- 🔍 **Multi-Source Concurrent Search**: Aggregate & de-duplicate results across **16+ open academic platforms** (arXiv, PubMed, Semantic Scholar, OpenAlex, CORE…) — plus **zhihuiya (智慧芽)**, a premium scientific-literature MCP source enabled by apikey.
- 📖 **Full-Text Reading**: Instant full-text for Open Access platforms, automatic PDF-parsing fallback, and metadata-level reads (abstract + bibliographic record) for index-only sources.
- 📥 **Automated Knowledge Base Ingestion**: Download papers and auto-upload & index them into **OpenWebUI Knowledge Base** for RAG citation and retrieval.
- 🔄 **Built-in OA Fallback Chain**: Source-native download ➔ Open Access Repositories (OpenAIRE / CORE / Europe PMC / PMC) ➔ Unpaywall ➔ (Optional) Sci-Hub mirror.
- 🔑 **Key-gated sources (auto-on/off)**: zhihuiya literature + patents and IEEE Xplore are enabled **automatically when their apikey is set** and skipped silently when not — no source list changes needed.
- 🏛 **Patent Search & Full-Text (patsnap)**: First-ever patent source — semantic patent search (`search_patents`) plus full claims + description + legal status as Markdown (`read_patent`). Shares the same zhihuiya apikey, also direct-connected.
- 🔬 **IEEE Xplore**: Direct REST API search (bypasses backend skeleton). Metadata-level results with abstract + citation count; OA papers include pdf_url.
- 🎯 **Smart Query Adaptation**: Automatically adapts your query per source — semantic sources (OpenAlex, Semantic Scholar, PubMed…) get the full natural-language query, while literal keyword sources (zhihuiya, DOAJ, IACR) get a cleaned core-keyword variant (quotes/boolean/noise stripped, then distilled to ≤5 high-specificity terms). Recovers hits that would otherwise return zero, without losing semantics.
- 🇫🇷 **HAL via Direct Connect**: HAL is queried directly (bypassing a backend date-parsing bug) so it reliably returns results.
- 🗄 **dblp & Zenodo via Direct Connect**: dblp (CS bibliography) and Zenodo (OA repository) are queried directly, bypassing backend bugs (concurrency ConnectionError, isoformat crash). Zenodo records often include direct PDF links.

---

## 🏛 Architecture

```
[User / OpenWebUI UI]
        │
        ├── OpenWebUI Native Python Tool (Bridge & Interceptor Layer)
                 │
                 ├── search_papers()  ──┬─► POST http://mcp:8000/papers/search_papers
                 │                      │    (16+ open platforms via paper-search-mcp)
                 │                      └─► zhihuiya MCP (direct, streamable-http, apikey)
                 │                           search_literature + literature_bibliography
                 │
                 ├── read_paper()     ──► Backend Tool (_READ_TOOLS) / zhihuiya bibliography
                 │                        ──► PDF Direct Fallback
                 │
                 ├── search_patents() ──► patsnap MCP (direct, streamable-http, apikey)
                 │                         patsnap_search (source=patent, semantic)
                 ├── read_patent()    ──► patsnap_fetch → claims+description+legal (Markdown)
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
                             Push to OpenWebUI `/api/v1/files/` with metadata
                                   │
                             Automatic RAG Vectorization & Knowledge Association
```

---

## 📊 Supported Data Sources

### Verified Active Sources
`default_sources = "arxiv,pubmed,iacr,semantic,crossref,openalex,pmc,core,europepmc,dblp,openaire,doaj,hal,zenodo"`

| Platform | Search | Read Tool | Native Download | Notes |
|---|---|---|---|---|
| **arXiv** | ✅ | `read_arxiv_paper` | ✅ | Open PDF, fast & reliable |
| **PubMed** | ✅ | ⚠️ metadata only | ❌ | Requires `NCBI_API_KEY` for rate limits |
| **Semantic Scholar** | ✅ | `read_semantic_paper` | ✅ (OA) | Supports `SEMANTIC_SCHOLAR_API_KEY` |
| **Crossref** | ✅ | ⚠️ metadata only | ❌ | Citation & DOI backbone |
| **OpenAlex** | ✅ | ⚠️ metadata only | ❌ | Open metadata backbone |
| **PMC / Europe PMC** | ✅ | ⚠️ Fallback to PDF | ✅ (OA) | High-quality biomedical full-text |
| **CORE** | ✅ | ⚠️ Fallback to PDF | ✅ (OA) | Global repository aggregator |
| **IACR** | ✅ | `read_iacr_paper` | ✅ | Cryptography ePrints |
| **HAL** | ✅ (direct) | ⚠️ Fallback to PDF | ✅ (OA) | Direct-connected (bypasses a backend date bug) |
| **OpenAIRE / DOAJ** | ✅ | Varies / Fallback | Record-dependent | Domain repositories |
| **dblp** | ✅ (direct) | ⚠️ ee/DOI → OA fallback | Record-dependent | CS bibliography (CS papers only); direct-connected (bypasses backend concurrency bug) |
| **Zenodo** | ✅ (direct) | ⚠️ pdf_url fallback | ✅ (mostly OA) | OA repository; direct-connected (bypasses backend isoformat bug); most records have PDFs |

### Key-gated Sources (auto-enabled when key is set, auto-skipped when not)

| Platform | Search | Read Tool | Notes |
|---|---|---|---|
| **zhihuiya (智慧芽)** | ✅ `search_literature` | ⚠️ metadata via `literature_bibliography` | Scientific-literature MCP, direct streamable-http. Enabled when `zhihuiya_apikey` (admin or user) is non-empty; skipped silently when no key |
| **patsnap (智慧芽专利)** | ✅ `patsnap_search` | ✅ full text via `patsnap_fetch` | Patent MCP (same key as zhihuiya). `read_patent` returns **claims + description + legal status** as Markdown |
| **IEEE Xplore** | ✅ REST API | ⚠️ pdf_url (OA only) | Direct REST API. Enabled when `ieee_apikey` is non-empty; skipped silently when no key. Metadata-level (abstract+bibliographic); OA papers have pdf_url |

### Sources NOT in default (grouped by keyword-search capability)

| Source | Keyword search | Why not default |
|---|---|---|
| **bioRxiv / medRxiv** | ❌ (subject-category browse) | Return latest ~30 days in a subject, **not** keyword search — would inject irrelevant results. Use explicitly via `sources="biorxiv"` + `biorxiv_category` |
| **Google Scholar** | ✅ | Anti-bot 403 without proxy |
| **SSRN** | ✅ | Cloudflare 403 |
| **BASE** | ✅ | IP blocked (403 Access denied) |
| **CiteSeerX** | ✅ (code) | Endpoint dead (redirects to archive.org 404) |
| **ACM** | ⚠️ skeleton | `search is not yet implemented`, no public REST API |
| **Unpaywall** | ❌ | **DOI lookup only** — used in the download fallback chain to find OA PDFs, not a search source |

---

## 🎯 Query Adaptation (per-source)

Different sources have very different query tolerances. `search_papers` automatically picks the right query shape per source — no configuration needed:

| Source class | Sources | Query sent |
|---|---|---|
| **Semantic / tokenizing** | openalex, semantic, crossref, pmc, europepmc, pubmed, arxiv, openaire, core, patsnap | Your **original** full natural-language query (semantics preserved) |
| **Literal keyword** | zhihuiya, doaj, iacr | A **cleaned core-keyword** variant — quotes, bare `OR/AND/NOT`, and filler words stripped, then distilled to ≤5 high-specificity terms |
| **Direct (bypasses backend)** | hal, zhihuiya, patsnap, dblp, zenodo, ieee | hal uses core; zhihuiya uses distilled; dblp/zenodo/ieee use original |

> `bioRxiv` / `medRxiv` are **not keyword search** — they return the latest ~30 days of papers in a subject category, so they're **excluded from `default_sources`** (a keyword query would inject irrelevant results). To browse a subject's new papers, call explicitly: `sources="biorxiv"` + `biorxiv_category="biochemistry"` (or `medrxiv_category="cardiovascular_medicine"`).

---

## 🚀 Setup & Installation

### 1. Docker Compose Integration
Mount the shared volume `paper-downloads` at `/downloads` between the `mcp` (`mcpo`) and `open-webui` containers:

```yaml
version: '3.8'

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

### 2. mcpo Server Configuration (`config.json`)
Configure your `config.json` file for `mcpo`:

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

### 3. OpenWebUI Tool Configuration
1. Open **OpenWebUI** -> **Workspace** -> **Tools**.
2. Create a new Tool and copy the contents of [`tool.py`](./tool.py).
3. Save the tool and optionally configure the **Valves** / **UserValves**:
   - `knowledge_id`: Default Knowledge Base ID to automatically store downloaded papers.
   - `allow_scihub`: Set to `True` / `False` for Sci-Hub fallback.
   - `scihub_url`: Custom Sci-Hub mirror URL (e.g. `https://sci-hub.ee`).

### 4. (Optional) Enable Key-gated Sources

Key-gated sources are **enabled automatically when their key is set** and **skipped silently when not** — no need to add them to `default_sources`.

**zhihuiya (智慧芽) + patsnap:**
- Set `Valves.zhihuiya_apikey` (admin, company-wide) or `UserValves.zhihuiya_apikey` (per-user, overrides admin)
- Enables both `zhihuiya` (literature) in `search_papers` and `search_patents`/`read_patent` (patent tools)

**IEEE Xplore:**
- Set `Valves.ieee_apikey` (admin) or `UserValves.ieee_apikey` (per-user, overrides admin)
- Enables `ieee` in `search_papers` when key is present
- Get a free key at [developer.ieee.org](https://developer.ieee.org/)

---

## 🛠 Usage in OpenWebUI

Once installed, OpenWebUI models can call the following tools:

1. **`search_papers(query, sources, max_results_per_source)`**  
   Searches papers concurrently across sources and returns formatted metadata with DOI & PDF links.  
   - Key-gated sources (zhihuiya, ieee) are **auto-enabled when their key is set**, auto-skipped otherwise — no need to list them in `sources` or `default_sources`.

2. **`read_paper(source, paper_id, pdf_url)`**  
   Reads the full text of a target paper (with automatic PDF fallback).

3. **`download_paper_to_knowledge(title, source, paper_id, doi, pdf_url)`**  
   Downloads the paper via direct URL or OA fallback chain, uploads it to OpenWebUI, and links it directly into your RAG Knowledge Base.

4. **`search_patents(query, limit, sort, filters)`**  *(requires zhihuiya_apikey)*  
   Semantic patent search — returns patent_number / title / IPC / legal_status / dates / assignees / cited_count.

5. **`read_patent(patent_number, max_chars)`**  *(requires zhihuiya_apikey)*  
   Reads a patent's full text as Markdown — bibliographic data, **claims**, **description**, and legal status.

---

## 🙏 Acknowledgments & Credits

Special thanks to the open-source projects that make this integration possible:

- **[paper-search-mcp](https://github.com/openags/paper-search-mcp)**: The underlying Model Context Protocol (MCP) server providing multi-platform academic search and paper retrieval capabilities.
- **[mcpo](https://github.com/open-webui/mcpo)**: The OpenAPI-to-MCP bridge by OpenWebUI for exposing MCP servers over HTTP.
- **[OpenWebUI](https://github.com/open-webui/open-webui)**: The open-source AI user interface and RAG ecosystem.
- **[zhihuiya (智慧芽)](https://www.zhihuiya.com/)**: Premium scientific-literature data, connected via its streamable-http MCP endpoint.

---

## 📜 License

Distributed under the [MIT License](./LICENSE).
