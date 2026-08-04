# 📚 OpenWebUI Academic Paper Search & Knowledge Base Integration

> **One prompt → 17 academic databases → full text → your RAG Knowledge Base.**
> Multi-source academic paper search, full-text reading, and automatic PDF ingestion into **OpenWebUI Knowledge Base** (RAG) — powered by `mcpo` + `paper-search-mcp`, now with direct **zhihuiya (智慧芽)** scientific-literature integration.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![OpenWebUI](https://img.shields.io/badge/OpenWebUI-Tool-blue)](https://github.com/open-webui/open-webui)
[![MCP](https://img.shields.io/badge/MCP-mcpo-green)](https://github.com/open-webui/mcpo)

---

## 🌟 Features

- 🔍 **Multi-Source Concurrent Search**: Aggregate & de-duplicate results across **16+ open academic platforms** (arXiv, PubMed, Semantic Scholar, OpenAlex, CORE…) — plus **zhihuiya (智慧芽)**, a premium scientific-literature MCP source enabled by apikey.
- 📖 **Full-Text Reading**: Instant full-text for Open Access platforms, automatic PDF-parsing fallback, and metadata-level reads (abstract + bibliographic record) for index-only sources.
- 📥 **Automated Knowledge Base Ingestion**: Download papers and auto-upload & index them into **OpenWebUI Knowledge Base** for RAG citation and retrieval.
- 🔄 **Built-in OA Fallback Chain**: Source-native download ➔ Open Access Repositories (OpenAIRE / CORE / Europe PMC / PMC) ➔ Unpaywall ➔ (Optional) Sci-Hub mirror.
- 🔑 **Optional zhihuiya Source**: Connects **directly** via streamable-http MCP (not through mcpo), enabled per-admin or per-user apikey — disabled entirely when no key is set.
- 🏛 **Patent Search & Full-Text (patsnap)**: First-ever patent source — semantic patent search (`search_patents`) plus full claims + description + legal status as Markdown (`read_patent`). Shares the same zhihuiya apikey, also direct-connected.

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

### Verified Active Sources (16 Selected)
`default_sources = "arxiv,pubmed,biorxiv,medrxiv,google_scholar,iacr,semantic,crossref,openalex,pmc,core,europepmc,dblp,openaire,doaj,hal"`

| Platform | Search | Read Tool | Native Download | Notes |
|---|---|---|---|---|
| **arXiv** | ✅ | `read_arxiv_paper` | ✅ | Open PDF, fast & reliable |
| **PubMed** | ✅ | ⚠️ metadata only | ❌ | Requires `NCBI_API_KEY` for rate limits |
| **bioRxiv / medRxiv** | ✅ | `read_biorxiv/medrxiv_paper` | ✅ | Open preprints (DOI based) |
| **Semantic Scholar** | ✅ | `read_semantic_paper` | ✅ (OA) | Supports `SEMANTIC_SCHOLAR_API_KEY` |
| **Crossref** | ✅ | ⚠️ metadata only | ❌ | Citation & DOI backbone |
| **OpenAlex** | ✅ | ⚠️ metadata only | ❌ | Open metadata backbone |
| **PMC / Europe PMC** | ✅ | ⚠️ Fallback to PDF | ✅ (OA) | High-quality biomedical full-text |
| **CORE** | ✅ | ⚠️ Fallback to PDF | ✅ (OA) | Global repository aggregator |
| **Google Scholar** | ✅ | ❌ | ❌ | May return 403 without proxy |
| **IACR** | ✅ | `read_iacr_paper` | ✅ | Cryptography ePrints |
| **OpenAIRE / DOAJ / HAL / dblp** | ✅ | Varies / Fallback | Record-dependent | Domain repositories |

### Optional Premium Source (apikey-gated, direct connection)

| Platform | Search | Read Tool | Native Download | Notes |
|---|---|---|---|---|
| **zhihuiya (智慧芽)** | ✅ `search_literature` | ⚠️ metadata via `literature_bibliography` | ❌ | Scientific-literature MCP. Connects **directly** (streamable-http), **not** via mcpo. Enabled only when an apikey is configured; search = `search_literature` + batched `literature_bibliography` (for abstracts); full text via DOI → OA fallback chain |
| **patsnap (智慧芽专利)** | ✅ `patsnap_search` | ✅ full text via `patsnap_fetch` | ❌ | **Patent** MCP (same company, same apikey). Dedicated tools `search_patents` / `read_patent` (independent of `search_papers`). `read_patent` returns **claims + description + legal status** as Markdown (default `module=['basic','legal']`) |

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

### 4. (Optional) Enable the zhihuiya (智慧芽) Source

zhihuiya is a premium scientific-literature source that connects **directly** via streamable-http MCP — it does **not** go through mcpo, so no `config.json` change is needed.

- **Admin (company) key** — set `Valves.zhihuiya_apikey` in the Tool's admin Valves. Applies to all users by default.
- **Per-user key / toggle** — users can set their own `UserValves.zhihuiya_apikey` (overrides the admin key) or flip `UserValves.zhihuiya_enabled` off.

The source is **enabled only when a non-empty apikey exists** (admin or user) **and** `zhihuiya_enabled` is on. With no key configured, the source issues no requests at all. Then add `zhihuiya` to `UserValves.default_sources` (or pass `sources="...,zhihuiya"` in a query) to include it in aggregated searches.

### 5. (Optional) Patent Tools — Same Key

The **patsnap patent tools** (`search_patents` / `read_patent`) use the **same** `zhihuiya_apikey` and the **same** `zhihuiya_enabled` toggle — no extra configuration. Once the key is set, patent search & full-text reading work immediately. They are independent of `search_papers` (patents are not merged into the literature aggregation).

---

## 🛠 Usage in OpenWebUI

Once installed, OpenWebUI models can call the following tools:

1. **`search_papers(query, sources, max_results_per_source)`**  
   Searches papers concurrently across sources and returns formatted metadata with DOI & PDF links.

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
