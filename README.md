# OpenWebUI Academic Paper Search & Knowledge Base Integration

Multi-source academic paper search, full-text reading, and automatic PDF ingestion into **OpenWebUI Knowledge Base** (RAG) powered by `mcpo` and `paper-search-mcp`.

---

## 🌟 Features

- 🔍 **Multi-Source Concurrent Search**: Aggregate search results across **16+ verified active academic platforms** with automatic de-duplication.
- 📖 **Full-Text Reading**: Instant full-text extraction for supported Open Access platforms and direct fallback to PDF parsing.
- 📥 **Automated Knowledge Base Ingestion**: Download papers and automatically upload & index them into **OpenWebUI Knowledge Base** for RAG-based citation and retrieval.
- 🔄 **Built-in OA Fallback Chain**: Sequential resolution path: Source-native download ➔ Open Access Repositories (OpenAIRE / CORE / Europe PMC / PMC) ➔ Unpaywall ➔ (Optional) Sci-Hub mirror.

---

## 🏛 Architecture

```
[User / OpenWebUI UI]
        │
        ├── OpenWebUI Native Python Tool (Bridge & Interceptor Layer)
                 │
                 ├── search_papers()  ──► POST http://mcp:8000/papers/search_papers
                 │                        (Aggregates 16+ active academic platforms)
                 │
                 ├── read_paper()     ──► Backend Tool (_READ_TOOLS) ──► PDF Direct Fallback
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

---

## 🛠 Usage in OpenWebUI

Once installed, OpenWebUI models can call the following tools:

1. **`search_papers(query, sources, max_results_per_source)`**  
   Searches papers concurrently across sources and returns formatted metadata with DOI & PDF links.

2. **`read_paper(source, paper_id, pdf_url)`**  
   Reads the full text of a target paper (with automatic PDF fallback).

3. **`download_paper_to_knowledge(title, source, paper_id, doi, pdf_url)`**  
   Downloads the paper via direct URL or OA fallback chain, uploads it to OpenWebUI, and links it directly into your RAG Knowledge Base.

---

## 📜 License

Distributed under the [MIT License](./LICENSE).
