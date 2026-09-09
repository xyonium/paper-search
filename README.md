# 📚 OpenWebUI Academic Paper Search & Knowledge Base Integration

> **One prompt → 18+ academic databases → full text → your RAG Knowledge Base.**
> Multi-source academic paper search, full-text reading, and automatic PDF ingestion into **OpenWebUI Knowledge Base** (RAG) — powered by the self-hosted **papers-service** (`papers_service_url` valve), with direct **zhihuiya (智慧芽)** literature/patent and **IEEE Xplore** integration.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![OpenWebUI](https://img.shields.io/badge/OpenWebUI-Tool-blue)](https://github.com/open-webui/open-webui)
[![Backend](https://img.shields.io/badge/Backend-papers--service-blue)](#-architecture-v298)

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

## 🏛 Architecture (v2.9.8)

```
[User / OpenWebUI UI]
        │
        └── OpenWebUI Native Python Tool (tool.py — dispatch, query adaptation, merge & dedup)
                 │
                 ├── search_papers()
                 │     │
                 │     ├─► Direct REST/HTTP (no backend): arxiv, hal, pubmed, pmc, semantic,
                 │     │    openalex, crossref, europepmc, core, zenodo, openaire, ieee,
                 │     │    biorxiv/medrxiv (subject browse), iacr (HTML regex)
                 │     │
                 │     ├─► Direct MCP (streamable-http, zhihuiya_apikey): zhihuiya, patsnap
                 │     │
                 │     ├─► Via firecrawl/tavily (configured base URLs):
                 │     │    · firecrawl as a standalone web-search source
                 │     │    · google_scholar chain: firecrawl scrape → tavily extract(advanced)
                 │     │      → Apify actor (johnvc/google-scholar-api) → backend (last resort)
                 │     │    · dblp Anubis anti-bot fallback → firecrawl (headless solves JS PoW)
                 │     │
                 │     └─► papers-service (self-hosted FastAPI, papers_service_url valve;
                 │          mirrors the retired mcpo/paper-search-mcp endpoint shapes):
                 │          search safety net (doaj, scholar chain backend);
                 │          ssrn/base/citeseerx/acm unimplemented (off by default)
                 │
                 ├── read_paper() ──► papers-service read tools (fast lane: 12 sources —
                 │                     arxiv/semantic/hal/pubmed/crossref/biorxiv full text…)
                 │                     → 404 → pdf_url direct → jina reader fallback
                 │
                 ├── search_patents() / read_patent() ──► patsnap MCP (direct, streamable-http)
                 │
                 └── download_paper_to_knowledge()
                        ├─► [1] direct pdf_url download (+ title identity gate)
                        └─► [2] papers-service download_with_fallback (in-memory bytes):
                                  native (arxiv/iacr/biorxiv) → OA repos (openaire/core/
                                  europepmc/pmc) → Unpaywall → optional Sci-Hub,
                                  every step title-gated → OWUI /api/v1/files → RAG
                                  (no shared volume needed since v2.9.7)
```

**Why the papers backend is only a safety net**: every source with a healthy public API was moved to direct connect (v2.9 series) — the original paper-search-mcp's synchronous `requests` without timeouts once hung whole search batches, its scholar/ssrn/base adapters hit anti-bot walls, and its OR-query semantics returned irrelevant papers. papers-service keeps the same valve-driven safety net (doaj, the scholar chain's last resort) plus a real full-text read lane (12 sources) and the OA download chain; ssrn/base/citeseerx/acm remain unimplemented upstream.

---

## 📊 Supported Data Sources

### Verified Active Sources
`default_sources = "arxiv,pubmed,semantic,crossref,openalex,pmc,core,europepmc,dblp,openaire,doaj,hal,zenodo,google_scholar,zhihuiya,ieee,firecrawl"`

| Platform | Search | Read Tool | Native Download | Notes |
|---|---|---|---|---|
| **arXiv** | ✅ (direct) | `read_arxiv_paper` | ✅ | Open PDF, fast & reliable |
| **PubMed** | ✅ | ⚠️ metadata only | ❌ | Requires `NCBI_API_KEY` for rate limits |
| **Semantic Scholar** | ✅ (direct) | `read_semantic_paper` | ✅ (OA) | Optional `semantic_api_key` valve (anonymous shared pool 429s often) |
| **Crossref** | ✅ (direct) | ⚠️ metadata only | ❌ | Citation & DOI backbone |
| **OpenAlex** | ✅ (direct) | ⚠️ metadata only | ❌ | Open metadata backbone |
| **PMC / Europe PMC** | ✅ | ⚠️ Fallback to PDF | ✅ (OA) | High-quality biomedical full-text |
| **CORE** | ✅ (direct) | ⚠️ Fallback to PDF | ✅ (OA) | Global repository aggregator; optional `core_api_key` valve |
| **IACR** | ✅ (direct) | `read_iacr_paper` | ✅ | Cryptography ePrints; direct HTML parsing (no JSON API exists) |
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
| **Google Scholar (fallback chain)** | ✅ firecrawl → tavily → Apify actor | ❌ (use url) | Tier 1: `firecrawl_base_url` (e.g. mcpo → official firecrawl cloud) — scrapes the Scholar result page via stealth cloud egress, parses titles/authors/year/citations/PDF links from markdown. Tier 2: `tavily_base_url` — `/extract` with `extract_depth=advanced` (basic only returns bare titles). Tier 3: `apify_rotator_base_url` — the `johnvc/google-scholar-api` actor through the rotator's key pool (PAY_PER_EVENT; free tier works with reduced counts). None configured → scholar stays on the backend (anti-bot prone) |

### Sources NOT in default (grouped by keyword-search capability)

| Source | Keyword search | Why not default |
|---|---|---|
| **bioRxiv / medRxiv** | ❌ (subject-category browse) | Return latest ~30 days in a subject, **not** keyword search — would inject irrelevant results. Use explicitly via `sources="biorxiv"` + `biorxiv_category` |
| **Google Scholar** | ✅ | Anti-bot 403 without help — set `firecrawl_base_url` (tier 1), `tavily_base_url` (tier 2) and/or `apify_rotator_base_url` (tier 3); see Key-gated Sources |
| **SSRN** | ✅ | Search endpoints retired (soft-404/empty) + Cloudflare interactive challenge on api.ssrn.com (2026-09 verified). Backend silently returns 0. Marginal value for bio/CS (overlaps bioRxiv/medRxiv/arXiv) — not worth fixing |
| **BASE** | ✅ | Anubis JS-PoW anti-bot (same family as dblp). firecrawl with `waitFor≥12s` penetrates (verified 2026-09) — fixable, but heavy overlap with OpenAlex/CORE/OpenAIRE makes it low priority |
| **CiteSeerX** | ✅ (code) | Endpoint dead (redirects to archive.org 404) |
| **ACM** | ⚠️ skeleton | `search is not yet implemented`, no public REST API |
| **Unpaywall** | ❌ | **DOI lookup only** — used in the download fallback chain to find OA PDFs, not a search source |

---

## 🎯 Query Adaptation (per-source)

Different sources have very different query tolerances. `search_papers` automatically picks the right query shape per source — no configuration needed:

| Source class | Sources | Query sent |
|---|---|---|
| **Semantic / tokenizing** | openalex, semantic, crossref, pmc, europepmc, pubmed, openaire, core, patsnap | Your **original** full natural-language query (semantics preserved) |
| **Literal keyword** | zhihuiya, doaj | A **cleaned core-keyword** variant — quotes, bare `OR/AND/NOT`, and filler words stripped, then distilled to ≤5 high-specificity terms |
| **Direct (bypasses backend)** | hal, zhihuiya, patsnap, dblp, zenodo, ieee, pubmed, pmc, arxiv, semantic, openalex, crossref, europepmc, core, biorxiv, medrxiv, iacr (+ google_scholar when any of firecrawl/tavily/apify valves set) | hal/arxiv use core; zhihuiya uses distilled; the rest use original. The papers-service backend serves the search safety net (doaj, google_scholar chain) — paper-search-mcp is retired |

> `bioRxiv` / `medRxiv` are **not keyword search** — they return the latest ~30 days of papers in a subject category, so they're **excluded from `default_sources`** (a keyword query would inject irrelevant results). To browse a subject's new papers, call explicitly: `sources="biorxiv"` + `biorxiv_category="biochemistry"` (or `medrxiv_category="cardiovascular_medicine"`).

---

## 🚀 Setup & Installation

### 1. Backend: papers-service

The retired `mcpo` + `paper-search-mcp` backend is replaced by a self-hosted
**papers-service** (FastAPI, port 3200) that mirrors the same endpoint shapes
(`search_{source}` / `read_{source}_paper` / `download_with_fallback`). Reference
deployment lives in the
[firecrawl-portainer](https://github.com/xyonium/firecrawl/tree/portainer-stack/papers-service)
branch (source + GH Actions image build + digest-pinned compose). Point the tool at it:

```yaml
services:
  papers-service:
    image: ghcr.io/xyonium/firecrawl-papers-service  # digest-pinned in production
    environment:
      SEMANTIC_SCHOLAR_API_KEY: "s2k-xxx"   # optional, higher S2 quota
      UNPAYWALL_EMAIL: "your_email@example.com"
    networks: [open-webui]
```

Since v2.9.7 downloads are streamed as **in-memory bytes** — no shared Docker
volume between the backend and open-webui is needed anymore.

### 2. OpenWebUI Tool Configuration
1. Open **OpenWebUI** -> **Workspace** -> **Tools**.
2. Create a new Tool and copy the contents of [`tool.py`](./tool.py).
3. Save the tool and optionally configure the **Valves** / **UserValves**:
   - `papers_service_url`: papers-service base URL (default `http://papers-service:3200/papers`).
   - `knowledge_id`: Default Knowledge Base ID to automatically store downloaded papers.
   - `allow_scihub`: Set to `True` / `False` for Sci-Hub fallback.
   - `scihub_url`: Custom Sci-Hub mirror URL (e.g. `https://sci-hub.ee`).

### 3. (Optional) Enable Key-gated Sources

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

## 🧪 Testing

**Offline unit tests** (mocked HTTP, no network):

```bash
python3 -m pytest tests/ -q
```

**Per-source live smoke test** — tells network/rate-limit/anti-bot problems apart from code bugs:

```bash
python3 scripts/live_sources.py            # all key-free sources
python3 scripts/live_sources.py hal dblp   # specific sources only
SEMANTIC_API_KEY=... IEEE_APIKEY=... CORE_API_KEY=... ZENODO_ACCESS_TOKEN=... \
    python3 scripts/live_sources.py        # include key-gated sources
```

Each source is queried with a known-stable term; the report shows PASS/FAIL/EMPTY + hit count + latency. FAIL containing `429` = rate limit (configure a key or retry later), `504`/timeout = transient network, `反爬/非 JSON` = IP blocked by anti-bot (e.g. dblp's Anubis challenge) — none of these are code bugs.

---

## 🙏 Acknowledgments & Credits

Special thanks to the open-source projects that make this integration possible:

- **[paper-search-mcp](https://github.com/openags/paper-search-mcp)**: The original MCP backend (now retired from this stack); its OA download chain design lives on in papers-service `download_with_fallback`.
- **[mcpo](https://github.com/open-webui/mcpo)**: The OpenAPI-to-MCP bridge — still hosting the firecrawl gateway in this stack.
- **[OpenWebUI](https://github.com/open-webui/open-webui)**: The open-source AI user interface and RAG ecosystem.
- **[zhihuiya (智慧芽)](https://www.zhihuiya.com/)**: Premium scientific-literature data, connected via its streamable-http MCP endpoint.

---

## 📜 License

Distributed under the [MIT License](./LICENSE).
