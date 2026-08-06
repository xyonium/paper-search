"""
title: Academic Paper Search
description: 学术论文搜索、全文阅读、PDF 下载入 Knowledge（RAG）。智慧芽文献/专利 + IEEE Xplore（有 key 自动启用）+ arXiv/PubMed/Semantic Scholar/OpenAlex/CORE/HAL/dblp/Zenodo/IACR/DOAJ/OpenAIRE/Europe PMC/Crossref/PMC，内置 OA fallback 下载链。

  【搜索源清单】（search_papers 的 sources 参数可用值，'all' 为全部）：
  · 预印本/开放获取（可搜可读全文）: arxiv, iacr, pmc, europepmc
  · 综合索引: semantic (Semantic Scholar), openalex, crossref, pubmed, core,
    openaire, doaj, hal, dblp (CS书目), zenodo (OA仓储，可选 zenodo_access_token 提额)
  · 学科新论文浏览（非关键词检索，默认不启用，sources+biorxiv_category 显式用）: biorxiv, medrxiv
  · 支持搜索但端点死/反爬/不稳（默认不启用，失败自动降级）: google_scholar, ssrn, base, citeseerx
  · 支持搜索但未实现（配 key 也报错，不可用）: acm
  · 仅 DOI 查询（不支持关键词搜索，用于 download fallback 链查 OA PDF）: unpaywall
  · 智慧芽（需配 zhihuiya_apikey，有 key 自动启用）: zhihuiya
  · IEEE Xplore（需配 ieee_apikey，有 key 自动启用，直连 REST API）: ieee
  · 智慧芽专利（独立工具 search_patents/read_patent，同 key 启用）: patsnap

  【查询适配】search_papers 自动按源分发查询变体（不损语义）：
  · 语义源（openalex/semantic/crossref/pmc/europepmc/pubmed/arxiv/openaire/core/patsnap）
    → 用原始完整查询
  · 字面源（zhihuiya/doaj/iacr）→ 自动去引号/裸露布尔/噪声词，精简为核心术语
    （长自然语言查询在这些源会 0 命中，精简后恢复）
  · hal/dblp/zenodo 已改直连（绕后端 bug），均用 original 查询
  · biorxiv/medrxiv 非关键词检索，返回"该学科近30天新论文"；
    可用 biorxiv_category/medrxiv_category 传学科（如 biochemistry）提高相关性

  【工具用法】
  1. search_papers(query)      → 多源并发搜索+去重，返回标题/作者/摘要/引用数/pdf_url
  2. read_paper(source, paper_id, pdf_url) → 读全文（后端工具 + pdf_url 自动 fallback）
  3. download_paper_to_knowledge(...)      → PDF 下载并加入 Knowledge 知识库
  4. search_patents(query)     → 智慧芽专利语义检索（需配 zhihuiya_apikey）
  5. read_patent(patent_number) → 读专利全文 markdown（权利要求+说明书+法律状态）
author: openags-bridge
requirements: requests, pymupdf, anyio
version: 2.5.4
license: MIT
"""

import asyncio
import json
import os
import re
import anyio
import requests
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel, Field


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
_QUERY_BOOL_RE = re.compile(r"\b(?:OR|AND|NOT)\b", re.IGNORECASE)
_QUERY_CJK_RE = re.compile(r"[一-鿿㐀-䶿]")
_QUERY_PREFIXES = (
    "what are the latest", "what are the", "what is the", "what are", "what is",
    "recent advances in", "latest advances in", "advances in", "progress in",
    "review of", "research on", "studies on",
)

LITERAL_SOURCES = frozenset({"zhihuiya", "doaj", "iacr"})
DIRECT_SOURCES = frozenset({"zhihuiya", "hal", "patsnap", "dblp", "zenodo", "ieee"})
# 后端可提供服务的全部源（排除直连源 hal/zhihuiya/patsnap；citeseerx/base/zenodo 等虽在后端但默认不启用）
_BACKEND_ALL_SOURCES = (
    "arxiv,biorxiv,medrxiv,iacr,semantic,crossref,openalex,pubmed,pmc,core,"
    "europepmc,openaire,doaj,google_scholar,ssrn,unpaywall,citeseerx,base,acm"
)
# all_mode 拆分时语义组使用的后端源（去掉字面源 doaj/iacr，留给 core 变体）
_SEMANTIC_ALL_SOURCES = ",".join(
    s for s in _BACKEND_ALL_SOURCES.split(",") if s not in LITERAL_SOURCES
)


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


# 泛化词（几乎每篇都有，会稀释字面源相关性，截断时优先砍掉）
_GENERIC_TERMS = frozenset({
    "sensor", "sensors", "coating", "coatings", "film", "films", "membrane",
    "membranes", "thin", "conformal", "room", "temperature", "biomedical",
    "medical", "process", "processes", "control", "uniformity", "principle",
    "measurement", "surface", "layer", "device", "devices", "system", "systems",
    "technique", "techniques", "technology", "application", "applications",
})


def _distill_core_terms(text: str, max_terms: int = 5) -> str:
    """对字面源（zhihuiya/doaj/iacr）在 core 基础上按术语区分度截断到 max_terms 词。
    保留专业/罕见词（含连字符/数字/括号、全大写缩写、长词），砍泛化词；保持原顺序。
    词数 ≤ max_terms 时原样返回。实测临界：>5 词在字面源易 0 命中。"""
    words = text.split()
    if len(words) <= max_terms:
        return text

    def _score(w: str) -> int:
        wl = w.lower()
        s = 0
        if re.search(r"[-()/0-9]", w):
            s += 3
        if len(w) > 1 and w.isupper():
            s += 3
        if len(w) >= 8:
            s += 2
        if wl in _GENERIC_TERMS:
            s -= 5
        return s

    ranked = sorted(words, key=lambda w: -_score(w))
    keep = set(ranked[:max_terms])
    # 保持原顺序（同一词出现多次只保留首次出现的标记，避免重复计数丢失）
    seen = []
    for w in words:
        if w in keep and w not in seen:
            seen.append(w)
    return " ".join(seen)


class Tools:
    class Valves(BaseModel):
        mcpo_url: str = Field(
            default="http://mcp:8000/papers",
            description="mcpo base URL（含 config.json 里 mcpServers 的 key 名）",
        )
        mcpo_api_key: str = Field(default="", description="mcpo --api-key")
        openwebui_url: str = Field(
            default="http://open-webui:8080", description="OpenWebUI 容器名:端口"
        )
        owui_api_key: str = Field(
            default="", description="fallback key（一般用不到，自动透传用户token）"
        )
        shared_download_dir: str = Field(
            default="/downloads",
            description="mcpo 与 openwebui 共享 volume 的挂载路径（两容器内需一致）",
        )
        zhihuiya_apikey: str = Field(
            default="",
            description="智慧芽(zhihuiya)科学文献 API key（管理员/公司级，留空则不启用该源）",
        )
        ieee_apikey: str = Field(
            default="",
            description="IEEE Xplore API key（管理员级，留空则不启用 ieee 源）",
        )
        zenodo_access_token: str = Field(
            default="",
            description="Zenodo Access Token（管理员级，可选；配了额度更高/可访问受限记录，留空走公共 API）",
        )

    class UserValves(BaseModel):
        default_sources: str = Field(
            default="arxiv,pubmed,iacr,semantic,crossref,openalex,pmc,core,europepmc,dblp,openaire,doaj,hal,zenodo",
            description="默认搜索源：'all'=全部21源（慢，30s+）；或逗号分隔子集。默认未包含的源：google_scholar,citeseerx,ssrn,base,acm,unpaywall；zhihuiya 需配 zhihuiya_apikey，ieee 需配 ieee_apikey；biorxiv/medrxiv 为学科近30天浏览（非关键词检索），需 sources+biorxiv_category 显式调用",
        )
        knowledge_id: str = Field(
            default="", description="下载 PDF 自动加入的 Knowledge 集合 ID"
        )
        allow_scihub: bool = Field(
            default=True,
            description="允许 download fallback 链在 OA 源全失败后使用 Sci-Hub（法律风险自担）",
        )
        scihub_url: str = Field(
            default="https://sci-hub.ee",
            description="Sci-Hub 镜像站 Base URL，例如 https://sci-hub.vg, https://sci-hub.mk, 或者去https://sci-hub.shop查看最新",
        )
        zhihuiya_apikey: str = Field(
            default="",
            description="智慧芽个人 API key（非空时覆盖管理员 key）",
            json_schema_extra={"input": {"type": "password"}},
        )
        zhihuiya_enabled: bool = Field(
            default=True,
            description="是否启用智慧芽文献源（需 admin 或个人已配 key）",
        )
        ieee_apikey: str = Field(
            default="",
            description="IEEE Xplore API key（个人级，非空时覆盖管理员 key）",
            json_schema_extra={"input": {"type": "password"}},
        )
        zenodo_access_token: str = Field(
            default="",
            description="Zenodo Access Token（个人级，非空时覆盖管理员 token）",
            json_schema_extra={"input": {"type": "password"}},
        )

    # 覆盖全部 21 个源 + 可选 IEEE/ACM（配 key 后动态注册）
    # None = 后端无 read 工具，直接走 pdf_url fallback
    _READ_TOOLS = {
        "arxiv": "read_arxiv_paper",
        "biorxiv": "read_biorxiv_paper",
        "medrxiv": "read_medrxiv_paper",
        "iacr": "read_iacr_paper",
        "semantic": "read_semantic_paper",
        "doaj": "read_doaj_paper",
        "hal": "read_hal_paper",
        "openaire": "read_openaire_paper",
        # ↓ 工具存在但设计上只返回"不支持"提示——尝试后检测降级
        "pubmed": "read_pubmed_paper",
        "crossref": "read_crossref_paper",
        # ↓ 后端无 read 工具
        "pmc": None,
        "core": None,
        "europepmc": None,
        "openalex": None,
        "google_scholar": None,
        "ssrn": None,
        "unpaywall": None,
        "dblp": None,    # 元数据库，无全文；走 DOI → OA fallback
        "zenodo": None,  # 直连搜索；read 走 pdf_url fallback（多数记录有 OA PDF）
        "base": None,    # 反爬（IP blocked），走 pdf_url fallback
        "citeseerx": None,  # 端点已死（archive.org redirect），走 pdf_url fallback
        # ↓ 可选付费源（配 key 后工具存在，未配时调用会 404，被异常处理兜住）
        "ieee": None,  # 直连搜索（metadata 级）；read 走 pdf_url fallback（OA 可下）
        "acm": "read_acm_paper",
        # ↓ 智慧芽：直连元数据级 read（literature_bibliography），非后端工具
        "zhihuiya": "zhihuiya_bibliography",
    }

    # 后端"不支持"提示的特征串（命中则视为无内容，降级 pdf_url fallback）
    _UNSUPPORTED_MARKERS = (
        "not supported",
        "doesn't provide",
        "does not provide",
        "metadata-only",
        "not implemented",
    )

    @classmethod
    def _is_unsupported_msg(cls, text) -> bool:
        if not isinstance(text, str):
            return True
        t = text.strip().lower()
        if len(t) < 200:
            return True
        return len(t) < 1200 and any(m in t for m in cls._UNSUPPORTED_MARKERS)

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False

    # ---------- 内部 ----------
    # ---------- 智慧芽 zhihuiya ----------
    _ZHIHUIYA_MCP_URL = "https://connect.zhihuiya.com/eba075/mcp?apikey={key}"
    _PATSNAP_MCP_URL = "https://connect.zhihuiya.com/2b0355/logic-mcp?apikey={key}"

    def _zhihuiya_enabled_key(self, __user__=None) -> tuple:
        uv = __user__.get("valves") if __user__ else None
        user_key = (getattr(uv, "zhihuiya_apikey", "") or "").strip()
        admin_key = (getattr(self.valves, "zhihuiya_apikey", "") or "").strip()
        key = user_key or admin_key
        enabled = bool(getattr(uv, "zhihuiya_enabled", True)) and bool(key)
        return enabled, key

    async def _zhihuiya_call(self, tool_name: str, args: dict, key: str,
                             timeout: int = 30, url: str = None) -> dict:
        """直连智慧芽 MCP 调用单个工具，返回解析后的 dict。失败抛 RuntimeError。
        url 为 None 时用文献源 _ZHIHUIYA_MCP_URL，否则用传入的端点（如 patsnap）。"""
        url = (url or self._ZHIHUIYA_MCP_URL).format(key=key)

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
            raise RuntimeError(
                f"智慧芽 {tool_name} 连接失败: {self._redact_zhihuiya_key(e)}"
            )

        if getattr(result, "isError", False):
            msg = ""
            for c in getattr(result, "content", []) or []:
                msg = getattr(c, "text", "") or msg
            raise RuntimeError(
                f"智慧芽 {tool_name} 返回错误: {self._redact_zhihuiya_key(msg)[:300]}"
            )

        for c in getattr(result, "content", []) or []:
            text = getattr(c, "text", None)
            if not text:
                continue
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return {"raw": text}
        return {}

    @staticmethod
    def _redact_zhihuiya_key(text: str) -> str:
        """错误信息脱敏：避免 httpx 把带 apikey 的 URL 拼进异常导致凭据泄露。"""
        import re
        return re.sub(r"apikey=[^&\s]+", "apikey=***", str(text))

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
            # 富化失败不丢弃搜索结果：降级为空 abstract，继续返回 title/author/doi
            try:
                bib_resp = await self._zhihuiya_call(
                    "literature_bibliography", {"paper_id": ",".join(ids[:100])}, key
                )
            except Exception:
                bib_resp = {}
            for b in (bib_resp or {}).get("data") or []:
                if isinstance(b, dict) and b.get("paper_id"):
                    bib_by_id[b["paper_id"]] = b

        return [
            self._zhihuiya_map_paper(r, bib_by_id.get(r.get("paper_id")))
            for r in results
        ]

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
            hal_id = d.get("halId_s", "")
            if not hal_id:
                continue
            year = d.get("publicationDateY_i") or d.get("producedDateY_i") or ""
            pub = str(year) if year else (str(d.get("submittedDate_s", "") or "")[:10])
            title = d.get("title_s") or [""]
            title = (title[0] if isinstance(title, list) else str(title)).strip()
            if not title:
                continue
            authors = d.get("authFullName_s") or []
            abstract = d.get("abstract_s") or [""]
            abstract = (
                " ".join(x for x in abstract if x) if isinstance(abstract, list)
                else str(abstract or "")
            ).strip()
            doi = d.get("doiId_s", "")
            if isinstance(doi, list):
                doi = doi[0] if doi else ""
            papers.append({
                "title": title,
                "authors": "; ".join(a for a in authors if a),
                "published_date": pub,
                "abstract": abstract,
                "paper_id": f"hal:{hal_id}",
                "doi": doi,
                "source": "hal",
                "pdf_url": d.get("fileMain_s") or "",
                "citations": 0,
                "url": d.get("uri_s") or "",
            })
        return papers

    _DBLP_SEARCH_URL = "https://dblp.org/search/publ/api"
    _UNPAYWALL_API = "https://api.unpaywall.org/v2"

    async def _dblp_search(self, query: str, limit: int) -> list:
        """直连 dblp JSON API，绕后端 dblp.py 的并发 ConnectionError + 无退避重试。
        退避策略：429/5xx/连接错误最多重试3次，间隔 2s/4s/8s。
        注意：dblp 是 CS 书目库，仅收录计算机科学文献，非 CS 查询返回空属正常。"""
        max_attempts = 3
        backoff = [2, 4, 8]

        def _fetch():
            last_exc = None
            for attempt in range(max_attempts):
                try:
                    r = requests.get(
                        self._DBLP_SEARCH_URL,
                        params={
                            "q": query,
                            "format": "json",
                            "h": max(1, min(int(limit), 100)),
                        },
                        headers={
                            "User-Agent": "paper-search-tool/2.5 (OpenWebUI academic search)",
                            "Accept": "application/json",
                        },
                        timeout=30,
                    )
                    if r.status_code == 200:
                        return r.json()
                    if r.status_code in (429, 500, 502, 503, 504):
                        raise RuntimeError(f"dblp HTTP {r.status_code}")
                    r.raise_for_status()
                except RuntimeError:
                    raise
                except Exception as e:
                    last_exc = e
                if attempt < max_attempts - 1:
                    import time
                    time.sleep(backoff[attempt])
            raise RuntimeError(f"dblp 检索失败（重试{max_attempts}次）: {last_exc}")

        try:
            data = await anyio.to_thread.run_sync(_fetch)
        except Exception as e:
            raise RuntimeError(f"dblp 检索失败: {e}")

        hits = ((data or {}).get("result") or {}).get("hits") or {}
        hit_list = hits.get("hit") or []
        if isinstance(hit_list, dict):
            hit_list = [hit_list]
        papers = []
        for hit in hit_list[:limit]:
            if not isinstance(hit, dict):
                continue
            info = hit.get("info") or {}
            title = str(info.get("title") or "").strip()
            if not title:
                continue
            authors_raw = info.get("authors") or {}
            author_list = authors_raw.get("author") if isinstance(authors_raw, dict) else []
            if isinstance(author_list, dict):
                author_list = [author_list]
            authors = []
            for a in (author_list or []):
                if isinstance(a, dict):
                    name = str(a.get("text") or a.get("#text") or a.get("__text") or "").strip()
                elif isinstance(a, str):
                    name = a.strip()
                else:
                    continue
                if name:
                    authors.append(name)
            year = str(info.get("year") or "")
            doi = str(info.get("doi") or "")
            dblp_url = str(info.get("url") or "")
            paper_id = str(info.get("key") or dblp_url)
            if not paper_id:
                paper_id = f"dblp:{abs(hash(title)) & 0xffffffff:08x}"
            papers.append({
                "title": title,
                "authors": "; ".join(authors),
                "published_date": year,
                "abstract": "",
                "paper_id": paper_id,
                "doi": doi,
                "source": "dblp",
                "pdf_url": str(info.get("ee") or ""),
                "citations": 0,
                "url": dblp_url,
            })
        return papers

    _IEEE_SEARCH_URL = "https://ieeexploreapi.ieee.org/api/v1/search/articles"

    def _ieee_enabled_key(self, __user__=None) -> tuple:
        """返回 (enabled, key)。UserValves key 优先，否则用 admin Valves key。"""
        uv = __user__.get("valves") if __user__ else None
        user_key = (getattr(uv, "ieee_apikey", "") or "").strip() if uv else ""
        admin_key = (self.valves.ieee_apikey or "").strip()
        key = user_key or admin_key
        return (bool(key), key)

    async def _ieee_search(self, query: str, limit: int, key: str) -> list:
        """直连 IEEE Xplore REST API（需 apikey）。返回 metadata 级结果（abstract+著录），
        pdf_url 为 ieeexplore stamp 页（需机构访问才能下载 PDF）。
        绕后端 ieee.py 骨架（raise NotImplementedError，无实际 API 调用）。
        实测（2026-08，host/容器均复现）：IEEE API 对含常见词的较长 querytext 会间歇性
        挂起连接（~80s 后 SSL EOF / Read timeout），同查询重试即恢复 → 与 dblp 一致，
        网络类错误最多重试3次，退避 2s/4s。"""
        max_attempts = 3
        backoff = [2, 4]

        def _fetch():
            last_exc = None
            for attempt in range(max_attempts):
                try:
                    r = requests.get(
                        self._IEEE_SEARCH_URL,
                        params={
                            "apikey": key,
                            "querytext": query,
                            "max_records": max(1, min(int(limit), 200)),
                            "format": "json",
                            "sort_order": "desc",
                            "sort_field": "relevance",
                        },
                        headers={"Accept": "application/json"},
                        timeout=30,
                    )
                    if r.status_code == 200:
                        return r.json()
                    if r.status_code in (429, 500, 502, 503, 504):
                        last_exc = RuntimeError(f"IEEE HTTP {r.status_code}")
                    else:
                        # 4xx（如 401 key 无效/403 超限）不重试，直接失败
                        r.raise_for_status()
                except Exception as e:
                    if isinstance(e, requests.exceptions.HTTPError):
                        raise  # 4xx 不重试
                    last_exc = e
                if attempt < max_attempts - 1:
                    import time
                    time.sleep(backoff[attempt])
            raise RuntimeError(f"IEEE 检索失败（重试{max_attempts}次）: {last_exc}")

        try:
            data = await anyio.to_thread.run_sync(_fetch)
        except Exception as e:
            # 错误信息可能包含 apikey，需脱敏
            err_msg = str(e)
            if "apikey=" in err_msg:
                err_msg = re.sub(r"apikey=[^&\s]+", "apikey=***", err_msg)
            raise RuntimeError(f"IEEE 检索失败: {err_msg}")

        articles = (data or {}).get("articles") or []
        papers = []
        for a in articles[:limit]:
            if not isinstance(a, dict):
                continue
            title = str(a.get("title") or "").strip()
            if not title:
                continue
            authors_raw = (a.get("authors") or {}).get("authors") or []
            authors = [
                str(au.get("full_name") or "").strip()
                for au in authors_raw if isinstance(au, dict) and au.get("full_name")
            ]
            pub_year = str(a.get("publication_year") or "")
            doi = str(a.get("doi") or "")
            article_number = str(a.get("article_number") or "")
            pdf_url = str(a.get("pdf_url") or "")
            abstract = str(a.get("abstract") or "").strip()
            access_type = str(a.get("access_type") or "")
            # OA 论文可直接下载，LOCKED 需机构访问
            is_oa = access_type.upper() == "OPEN_ACCESS" or "open" in access_type.lower()
            papers.append({
                "title": title,
                "authors": "; ".join(authors),
                "published_date": pub_year,
                "abstract": abstract,
                "paper_id": f"ieee:{article_number}",
                "doi": doi,
                "source": "ieee",
                "pdf_url": pdf_url if is_oa else "",
                "citations": int(a.get("citing_paper_count") or 0),
                "url": str(a.get("html_url") or a.get("abstract_url") or ""),
            })
        return papers

    _ZENODO_SEARCH_URL = "https://zenodo.org/api/records"

    def _zenodo_token(self, __user__=None) -> str:
        """返回 Zenodo token。UserValves 优先，否则用 admin Valves。"""
        uv = __user__.get("valves") if __user__ else None
        user_tok = (getattr(uv, "zenodo_access_token", "") or "").strip() if uv else ""
        admin_tok = (self.valves.zenodo_access_token or "").strip()
        return user_tok or admin_tok

    async def _zenodo_search(self, query: str, limit: int, __user__=None) -> list:
        """直连 Zenodo REST API，绕后端 zenodo.py 的 isoformat bug
        （published_date 传 str 给 Paper，Paper.to_dict() 调 .isoformat() 崩溃）。
        Zenodo 是开放获取仓储，多数记录有 PDF。可选 token 提高额度/访问受限记录。"""
        token = self._zenodo_token(__user__)
        headers = {
            "User-Agent": "paper-search-tool/2.5 (OpenWebUI academic search)",
            "Accept": "application/json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        def _fetch():
            r = requests.get(
                self._ZENODO_SEARCH_URL,
                params={
                    "q": query,
                    "size": max(1, min(int(limit), 200)),
                    "type": "publication",
                    "sort": "bestmatch",
                },
                headers=headers,
                timeout=60,  # Zenodo 公共 API 可能较慢，给足时间
            )
            r.raise_for_status()
            return r.json()

        try:
            data = await anyio.to_thread.run_sync(_fetch)
        except Exception as e:
            raise RuntimeError(f"Zenodo 检索失败: {e}")

        hits = ((data or {}).get("hits") or {}).get("hits") or []
        papers = []
        for h in hits[:limit]:
            if not isinstance(h, dict):
                continue
            meta = h.get("metadata") or {}
            title = str(meta.get("title") or "").strip()
            if not title:
                continue
            creators = meta.get("creators") or []
            authors = []
            for c in creators:
                if isinstance(c, dict):
                    name = c.get("name") or f"{c.get('given_name','')} {c.get('family_name','')}".strip()
                    if name:
                        authors.append(name)
            abstract = str(meta.get("description") or "")
            # 去 HTML 标签（Zenodo description 常含 HTML）
            abstract = re.sub(r"<[^>]+>", " ", abstract).strip()
            abstract = re.sub(r"\s+", " ", abstract)
            pub_date = str(meta.get("publication_date") or "")[:10]
            record_id = str(h.get("id") or "")
            doi = str(h.get("doi") or meta.get("doi") or "")
            # 从 files 找 PDF
            pdf_url = ""
            for f in (h.get("files") or []):
                if isinstance(f, dict) and str(f.get("key", "")).lower().endswith(".pdf"):
                    links = f.get("links") or {}
                    pdf_url = str(links.get("self") or links.get("download") or "")
                    break
            record_url = str((h.get("links") or {}).get("html") or f"https://zenodo.org/record/{record_id}")
            papers.append({
                "title": title,
                "authors": "; ".join(authors),
                "published_date": pub_date,
                "abstract": abstract,
                "paper_id": f"zenodo:{record_id}",
                "doi": doi,
                "source": "zenodo",
                "pdf_url": pdf_url,
                "citations": 0,
                "url": record_url,
            })
        return papers

    def _mcp_call(self, tool: str, args: dict, timeout: int = 180):
        headers = {}
        if self.valves.mcpo_api_key:
            headers["Authorization"] = f"Bearer {self.valves.mcpo_api_key}"
        try:
            resp = requests.post(
                f"{self.valves.mcpo_url.rstrip('/')}/{tool}",
                json=args,
                headers=headers,
                timeout=timeout,
            )
            resp.raise_for_status()
            try:
                data = resp.json()
            except ValueError:
                return resp.text
            if isinstance(data, dict) and set(data) == {"result"}:
                return data["result"]
            return data
        except requests.exceptions.Timeout:
            raise RuntimeError(f"后端 mcpo 调用超时 ({timeout}s)")
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"后端 mcpo 请求失败: {e}")

    def _owui_headers(self, __request__=None) -> dict:
        headers = {}
        if __request__ is not None and hasattr(__request__, "headers"):
            auth = __request__.headers.get("authorization") or __request__.headers.get("Authorization")
            if auth:
                headers["Authorization"] = auth
            cookie = __request__.headers.get("cookie") or __request__.headers.get("Cookie")
            if cookie:
                headers["Cookie"] = cookie
        if "Authorization" not in headers and self.valves.owui_api_key:
            headers["Authorization"] = f"Bearer {self.valves.owui_api_key}"
        if not headers.get("Authorization") and not headers.get("Cookie"):
            raise RuntimeError("无法获取 OpenWebUI 凭证，请在 Valves 配置 owui_api_key")
        return headers

    @staticmethod
    def _trim_paper(p: dict, max_abstract: int = 600) -> dict:
        authors = [a.strip() for a in (p.get("authors") or "").split(";") if a.strip()]
        if len(authors) > 3:
            authors = authors[:3] + ["et al."]
        abstract = (p.get("abstract") or "").strip()
        if len(abstract) > max_abstract:
            abstract = abstract[:max_abstract].rstrip() + "…"
        return {
            "title": p.get("title") or "",
            "authors": "; ".join(authors),
            "year": (p.get("published_date") or "")[:4],
            "source": p.get("source") or "",
            "paper_id": p.get("paper_id") or "",
            "doi": p.get("doi") or "",
            "citations": p.get("citations", 0),
            "pdf_url": p.get("pdf_url") or "",
            "url": p.get("url") or "",
            "abstract": abstract,
        }

    @staticmethod
    def _pdf_to_text(data: bytes) -> str:
        import fitz

        with fitz.open(stream=data, filetype="pdf") as doc:
            return "\n".join(page.get_text() for page in doc).strip()

    def _upload_pdf(
        self, data: bytes, title: str, knowledge_id: str, __request__
    ) -> str:
        headers = self._owui_headers(__request__)
        safe = "".join(c if c.isalnum() or c in " ._-" else "_" for c in title)[:80]
        try:
            files = {"file": (f"{safe}.pdf", data, "application/pdf")}
            form_data = {}
            if knowledge_id:
                form_data["metadata"] = json.dumps({"knowledge_id": knowledge_id})

            r = requests.post(
                f"{self.valves.openwebui_url.rstrip('/')}/api/v1/files/",
                headers=headers,
                files=files,
                data=form_data if form_data else None,
                timeout=120,
            )
            if not r.ok:
                err_detail = r.text
                try:
                    err_detail = r.json().get("detail", r.text)
                except Exception:
                    pass
                raise RuntimeError(
                    f"上传文件到 /api/v1/files/ 失败 ({r.status_code}): {err_detail}"
                )

            file_id = r.json().get("id")
            if not file_id:
                raise RuntimeError(f"上传成功但响应中无 id: {r.text}")

            if knowledge_id:
                add_url = f"{self.valves.openwebui_url.rstrip('/')}/api/v1/knowledge/{knowledge_id}/file/add"
                add_resp = requests.post(
                    add_url,
                    headers={**headers, "Content-Type": "application/json"},
                    json={"file_id": file_id},
                    timeout=60,
                )
                if not add_resp.ok:
                    add_err = add_resp.text
                    try:
                        add_err = add_resp.json().get("detail", add_resp.text)
                    except Exception:
                        pass
                    # 如果由于 metadata 已自动关联、重复内容或后台异步解析延迟导致 400/409，视作已成功
                    if add_resp.status_code in (400, 409) and any(
                        kw in str(add_err).lower()
                        for kw in [
                            "already",
                            "exist",
                            "duplicate",
                            "in knowledge",
                            "content provided is empty",
                            "empty",
                        ]
                    ):
                        pass
                    else:
                        raise RuntimeError(
                            f"关联文件到 Knowledge ({knowledge_id}) 失败 ({add_resp.status_code}): {add_err}"
                        )

                return f"✅ 已下载《{title}》并加入 Knowledge（file_id: {file_id}），可被 RAG 检索引用。"
            return f"✅ 已下载《{title}》并上传（file_id: {file_id}）。未配置 knowledge_id，未入库。"
        except Exception as e:
            raise RuntimeError(f"上传文件到 OpenWebUI 失败: {e}")

    # ---------- 暴露给 LLM ----------
    async def search_papers(
        self,
        query: str,
        max_results_per_source: int = 5,
        sources: str = "",
        biorxiv_category: str = "",
        medrxiv_category: str = "",
        __user__={},
    ) -> str:
        """
        搜索学术论文：多源并发查询 + 去重，返回 title/authors/year/source/paper_id/doi/citations/pdf_url/abstract。
        - source+paper_id → read_paper 读全文；doi/pdf_url → download_paper_to_knowledge 入库
        - 单源失败不影响整体（见返回的 errors 字段）
        :param query: 学术检索词，越具体越好（如 'CRISPR base editing off-target'）
        :param max_results_per_source: 每源条数（默认5，勿调大）
        :param sources: 留空用默认；或逗号分隔子集，可选值:
            arxiv, iacr, pmc, europepmc, semantic, openalex, crossref, pubmed,
            core, openaire, doaj, hal, zenodo, dblp（CS书目，非CS查询可能0结果）,
            zhihuiya（需配 zhihuiya_apikey）, ieee（需配 ieee_apikey）,
            google_scholar, ssrn, base, citeseerx（端点死/反爬，可能失败）,
            unpaywall（仅DOI查询，不支持关键词）, acm（骨架未实现）,
            biorxiv/medrxiv（学科近30天浏览，非关键词检索，需配 biorxiv_category）
        :param biorxiv_category: 可选 bioRxiv 学科分类（如 biochemistry, cell_biology,
            bioinformatics, neuroscience 等，空格转下划线）。biorxiv/medrxiv 非关键词检索，
            返回该学科近30天新论文；传学科可提高相关性。
        :param medrxiv_category: 可选 medRxiv 学科分类（如 cardiovascular_medicine,
            epidemiology, infectious_diseases 等）。
        """
        uv = __user__.get("valves") if __user__ else None
        src = (
            sources
            or (uv.default_sources if uv else None)
            or "arxiv,semantic,openalex,pubmed,pmc,core,europepmc"
        )
        src_set = {s.strip().lower() for s in src.split(",") if s.strip()}
        all_mode = src.strip().lower() == "all"

        variants = _make_query_variants(query)
        original, core = variants["original"], variants["core"]

        zh_enabled, zh_key = self._zhihuiya_enabled_key(__user__)
        want_zh = zh_enabled and ("zhihuiya" in src_set or all_mode)
        want_hal = "hal" in src_set or all_mode
        want_dblp = "dblp" in src_set or all_mode
        want_zenodo = "zenodo" in src_set or all_mode
        ieee_enabled, ieee_key = self._ieee_enabled_key(__user__)
        want_ieee = ieee_enabled and ("ieee" in src_set or all_mode)

        # 直连源不进后端 sources
        backend_set = src_set - DIRECT_SOURCES
        if all_mode:
            backend_set = None  # None 表示后端用 _BACKEND_ALL_SOURCES（不含直连源）

        # 后端按变体分组：字面组用 core，语义组用 original
        # all_mode 下字面组固定为 LITERAL_SOURCES - DIRECT_SOURCES = {doaj, iacr}（zhihuiya 直连单独处理）
        backend_literal = (
            (LITERAL_SOURCES - DIRECT_SOURCES)
            if all_mode
            else ((src_set & LITERAL_SOURCES) - DIRECT_SOURCES)
        )
        # 字面源（doaj/iacr/zhihuiya）长术语查询需进一步按区分度截断到 5 词，否则 0 命中
        literal_query = _distill_core_terms(core, max_terms=5)

        async def _backend_all():
            # core==original 或无需拆分时，一次调用（含全部后端源）
            if not all_mode and not backend_set:
                # 只请了直连源（hal/zhihuiya/patsnap）→ 不调后端
                return {"papers": [], "source_results": {}, "errors": {}}
            args = {
                "query": original,
                "max_results_per_source": max_results_per_source,
                "sources": (_BACKEND_ALL_SOURCES if all_mode else ",".join(sorted(backend_set))),
            }
            if biorxiv_category:
                args["biorxiv_category"] = biorxiv_category
            if medrxiv_category:
                args["medrxiv_category"] = medrxiv_category
            return await anyio.to_thread.run_sync(self._mcp_call, "search_papers", args)

        async def _backend_split():
            # core!=original：语义组 original + 字面组 core 两次并发
            sem_set = (backend_set - LITERAL_SOURCES) if backend_set is not None else None
            tasks = []
            labels = []
            if sem_set is None or sem_set:
                async def _sem():
                    args = {"query": original,
                            "max_results_per_source": max_results_per_source,
                            "sources": (_SEMANTIC_ALL_SOURCES if all_mode else ",".join(sorted(sem_set)))}
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

        async def _dblp():
            return await self._dblp_search(original, max_results_per_source)

        async def _zenodo():
            return await self._zenodo_search(original, max_results_per_source, __user__)

        async def _ieee():
            return await self._ieee_search(original, max_results_per_source, ieee_key)

        # 组装并发分支
        branches = {}
        branches["backend"] = _backend_split() if core != original else _backend_all()
        if want_zh:
            branches["zhihuiya"] = _zh()
        if want_hal:
            branches["hal"] = _hal()
        if want_dblp:
            branches["dblp"] = _dblp()
        if want_zenodo:
            branches["zenodo"] = _zenodo()
        if want_ieee:
            branches["ieee"] = _ieee()

        keys = list(branches)
        results = await asyncio.gather(*branches.values(), return_exceptions=True)
        outcome = dict(zip(keys, results))

        backend_result = outcome.get("backend")
        zh_result = outcome.get("zhihuiya")
        hal_result = outcome.get("hal")
        dblp_result = outcome.get("dblp")
        zenodo_result = outcome.get("zenodo")
        ieee_result = outcome.get("ieee")

        # 后端失败处理：若任一直连源有结果则保留，否则报错
        direct_ok = [r for r in (zh_result, hal_result, dblp_result, zenodo_result, ieee_result) if isinstance(r, list) and r]
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
        if want_dblp:
            if isinstance(dblp_result, Exception):
                source_results["dblp"] = 0
                errors["dblp"] = str(dblp_result)
            elif dblp_result is not None:
                dp = [self._trim_paper(p) for p in dblp_result]
                papers.extend(dp)
                source_results["dblp"] = len(dp)
        if want_zenodo:
            if isinstance(zenodo_result, Exception):
                source_results["zenodo"] = 0
                errors["zenodo"] = str(zenodo_result)
            elif zenodo_result is not None:
                zp2 = [self._trim_paper(p) for p in zenodo_result]
                papers.extend(zp2)
                source_results["zenodo"] = len(zp2)
        if want_ieee:
            if isinstance(ieee_result, Exception):
                source_results["ieee"] = 0
                errors["ieee"] = str(ieee_result)
            elif ieee_result is not None:
                ip = [self._trim_paper(p) for p in ieee_result]
                papers.extend(ip)
                source_results["ieee"] = len(ip)

        out = {"query": query, "total": len(papers),
               "source_results": source_results, "errors": errors, "papers": papers}
        # 字面源发生截断时给出提示（LLM/用户可见），避免误以为用了完整查询
        if literal_query != original:
            adapted = {}
            for s in (LITERAL_SOURCES - DIRECT_SOURCES) | {"zhihuiya", "hal", "dblp", "zenodo"}:
                if s in source_results or (s == "zhihuiya" and want_zh) or (s == "hal" and want_hal) or (s == "dblp" and want_dblp) or (s == "zenodo" and want_zenodo):
                    adapted[s] = literal_query
            if adapted:
                out["query_adapted"] = adapted
        return json.dumps(out, ensure_ascii=False, indent=2)

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
            return json.dumps(
                {"error": f"专利检索失败: {self._redact_zhihuiya_key(e)}"},
                ensure_ascii=False,
            )
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
            return json.dumps(
                {"error": f"专利获取失败: {self._redact_zhihuiya_key(e)}"},
                ensure_ascii=False,
            )
        results = (resp or {}).get("results") or []
        first = results[0] if results and isinstance(results[0], dict) else {}
        md = first.get("markdown", "") or ""
        if not md:
            return json.dumps(
                {"error": f"未获取到专利 {patent_number} 的内容"}, ensure_ascii=False
            )
        return md[:max_chars] + ("\n\n[…专利文档截断…]" if len(md) > max_chars else "")

    async def read_paper(
        self,
        source: str,
        paper_id: str = "",
        pdf_url: str = "",
        max_chars: int = 25000,
        __user__={},
    ) -> str:
        """
        阅读论文全文（截断到 max_chars）。
        - 后端直接可读: arxiv, biorxiv, medrxiv, iacr, semantic, doaj, hal, openaire
        - pubmed/crossref 后端仅返回元数据提示，会自动降级用 pdf_url 提取
        - dblp: 元数据库（无全文），自动用 DOI 查 ee 链接走 PDF fallback
        - zenodo: 直连搜索（已绕过后端 bug），多数记录有 pdf_url 可直接提取
        - base/citeseerx: 端点不可达（IP封/已下线），走 pdf_url fallback
        - pmc, core, europepmc, openalex, google_scholar, ssrn, unpaywall:
          请同时传 pdf_url，将自动下载提取全文
        - zhihuiya（智慧芽）: 元数据级 read（literature_bibliography 取 abstract+著录），
          全文请用 doi 走 download_paper_to_knowledge 的 OA fallback 链
        :param source: search 结果的 source 字段
        :param paper_id: search 结果的 paper_id 字段
        :param pdf_url: search 结果的 pdf_url 字段（强烈建议总是提供，作 fallback）
        :param max_chars: 最大返回字符数
        """
        try:
            max_chars = int(max_chars)
        except (TypeError, ValueError):
            max_chars = 25000

        src = (source or "").strip().lower()
        backend_tool = self._READ_TOOLS.get(src)
        backend_err = ""

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
                    backend_err = f"智慧芽读取失败: {self._redact_zhihuiya_key(e)}"

        if backend_tool and paper_id and src != "zhihuiya":
            try:
                text = await anyio.to_thread.run_sync(
                    self._mcp_call, backend_tool, {"paper_id": paper_id}, 300
                )
                if not self._is_unsupported_msg(text):
                    return text[:max_chars] + (
                        "\n\n[…全文截断…]" if len(text) > max_chars else ""
                    )
                backend_err = f"后端工具无可用全文（{src}）"
            except Exception as e:
                backend_err = f"后端读取失败: {e}"
        elif backend_tool is None and src in self._READ_TOOLS:
            backend_err = f"源 '{src}' 无后端全文工具"
        elif src not in self._READ_TOOLS:
            backend_err = f"未知源 '{src}'"

        if src == "dblp" and not pdf_url and paper_id:
            # dblp 是元数据库，无全文；尝试用 dblp record XML 查 ee/DOI → Unpaywall OA PDF
            dblp_key = paper_id.lstrip("dblp:")
            try:
                def _fetch_dblp_ee():
                    r = requests.get(
                        f"https://dblp.org/rec/{dblp_key}.xml",
                        headers={"Accept": "application/xml"},
                        timeout=15,
                    )
                    if r.status_code != 200:
                        return ""
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(r.text)
                    ee = root.find(".//ee")
                    return ee.text.strip() if ee is not None and ee.text else ""

                ee_url = await anyio.to_thread.run_sync(_fetch_dblp_ee)
                if ee_url:
                    # arXiv 托管的论文：直接从 arXiv ID 构建 PDF 链接
                    if "arxiv.org/abs/" in ee_url:
                        arxiv_id = ee_url.split("arxiv.org/abs/")[-1]
                        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
                    elif "doi.org/10.48550/arXiv." in ee_url:
                        # dblp 对 arXiv 论文的 DOI 格式
                        arxiv_id = ee_url.split("arXiv.")[-1]
                        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
                    elif "doi.org/" in ee_url:
                        doi = ee_url.split("doi.org/")[-1]
                        oa_url = await self._resolve_oa_pdf(doi)
                        if oa_url:
                            pdf_url = oa_url
                    elif ee_url.lower().endswith(".pdf"):
                        pdf_url = ee_url
            except Exception:
                pass  # 静默降级到下面的通用 pdf_url / 错误提示

        if pdf_url:
            try:
                def _fetch_pdf():
                    r = requests.get(
                        pdf_url,
                        timeout=180,
                        headers={"User-Agent": "Mozilla/5.0"},
                        allow_redirects=True,
                    )
                    r.raise_for_status()
                    if not r.content.startswith(b"%PDF"):
                        raise RuntimeError("返回非 PDF（可能付费墙页面）")
                    return self._pdf_to_text(r.content)

                text = await anyio.to_thread.run_sync(_fetch_pdf)
                if text:
                    return text[:max_chars] + (
                        "\n\n[…全文截断…]" if len(text) > max_chars else ""
                    )
                return json.dumps({"error": "PDF 提取为空（扫描版图片 PDF）"}, ensure_ascii=False)
            except Exception as e:
                return json.dumps(
                    {
                        "error": f"全文获取失败。{backend_err}；PDF fallback 失败: {e}",
                        "hint": "可尝试 download_paper_to_knowledge 走完整 OA fallback 链（含 Unpaywall/Sci-Hub）",
                    },
                    ensure_ascii=False,
                )

        return json.dumps(
            {
                "error": backend_err or "无可用全文途径",
                "hint": "请从 search_papers 结果中同时传入 pdf_url 重试",
            },
            ensure_ascii=False,
        )

    async def _resolve_oa_pdf(self, doi: str) -> str:
        """用 Unpaywall API 按 DOI 查开放获取 PDF 直链。查不到返回空字符串。"""
        email = self.valves.__dict__.get("unpaywall_email") or "paper-search@openwebui.local"
        try:
            def _fetch():
                r = requests.get(
                    f"{self._UNPAYWALL_API}/{doi}",
                    params={"email": email},
                    headers={"Accept": "application/json"},
                    timeout=20,
                )
                if r.status_code == 200:
                    return r.json()
                return {}
            data = await anyio.to_thread.run_sync(_fetch)
            best = data.get("best_oa_location") or {}
            return str(best.get("url_for_pdf") or best.get("url") or "")
        except Exception:
            return ""

    async def download_paper_to_knowledge(
        self,
        title: str,
        source: str = "",
        paper_id: str = "",
        doi: str = "",
        pdf_url: str = "",
        __request__=None,
        __user__={},
    ) -> str:
        """
        下载论文 PDF 并加入 Knowledge 知识库（RAG 可检索）。用户想"保存/收藏/入库"时调用。
        内置完整 OA fallback 链：源站 → OA仓储(OpenAIRE/CORE/EuropePMC/PMC) → Unpaywall → (可选)Sci-Hub。
        :param title: 论文标题（文件名 + fallback 检索用）
        :param source: search 结果的 source 字段（有则原生下载优先）
        :param paper_id: search 结果的 paper_id 字段
        :param doi: search 结果的 doi 字段（fallback 链的关键，尽量提供）
        :param pdf_url: search 结果的 pdf_url（有则先直连下载，最快）
        """
        uv = __user__.get("valves") if __user__ else None
        knowledge_id = (uv.knowledge_id if uv else "") or ""
        allow_scihub = uv.allow_scihub if uv else True
        scihub_url = (uv.scihub_url if uv else "https://sci-hub.ee") or "https://sci-hub.ee"

        # 路径1: 直接 pdf_url 下载（最快，不依赖共享卷）
        if pdf_url:
            try:
                def _direct_download():
                    r = requests.get(
                        pdf_url,
                        timeout=180,
                        headers={"User-Agent": "Mozilla/5.0"},
                        allow_redirects=True,
                    )
                    r.raise_for_status()
                    if r.content.startswith(b"%PDF"):
                        return self._upload_pdf(r.content, title, knowledge_id, __request__)
                    return None

                res = await anyio.to_thread.run_sync(_direct_download)
                if res:
                    return res
            except Exception:
                pass  # 静默落入 fallback 链

        # 路径2: 后端 download_with_fallback（OA链 + 可选Sci-Hub），落盘共享卷后读回
        if not (source and paper_id) and not doi:
            return json.dumps(
                {
                    "error": "信息不足：需提供 (source+paper_id) 或 doi 或 pdf_url 至少一组",
                    "hint": "从 search_papers 结果中取这些字段",
                },
                ensure_ascii=False,
            )

        try:
            result = await anyio.to_thread.run_sync(
                self._mcp_call,
                "download_with_fallback",
                {
                    "source": source
                    or "crossref",  # crossref 必失败 → 直接进 OA fallback 链（有意为之）
                    "paper_id": paper_id or doi or title,
                    "doi": doi,
                    "title": title,
                    "save_path": self.valves.shared_download_dir,
                    "use_scihub": allow_scihub,
                    "scihub_base_url": scihub_url,
                },
                600,
            )
        except Exception as e:
            return json.dumps(
                {
                    "error": "下载请求异常/超时",
                    "detail": str(e),
                    "hint": "可检查后端日志或手动确认该论文 DOI 是否在 Open Access 仓储中可用",
                },
                ensure_ascii=False,
            )

        if isinstance(result, str) and result.endswith(".pdf"):
            local_path = result
            if not os.path.exists(local_path):
                candidate = os.path.join(
                    self.valves.shared_download_dir, os.path.basename(local_path)
                )
                if os.path.exists(candidate):
                    local_path = candidate
                else:
                    return json.dumps(
                        {
                            "error": "后端报告下载成功但共享卷中找不到文件",
                            "backend_path": result,
                            "hint": "检查 mcpo 与 openwebui 容器的共享 volume 挂载路径是否一致",
                        },
                        ensure_ascii=False,
                    )
            try:
                def _read_and_upload():
                    with open(local_path, "rb") as f:
                        data = f.read()
                    try:
                        os.remove(local_path)  # 上传后清理，避免共享卷膨胀
                    except OSError:
                        pass
                    return self._upload_pdf(data, title, knowledge_id, __request__)

                msg = await anyio.to_thread.run_sync(_read_and_upload)
                if "scihub" in result.lower() or "sci-hub" in result.lower():
                    msg += "（来源：Sci-Hub fallback）"
                return msg
            except Exception as e:
                return json.dumps(
                    {
                        "error": "读取落盘 PDF 并上传到 OpenWebUI 失败",
                        "detail": str(e),
                    },
                    ensure_ascii=False,
                )

        return json.dumps(
            {
                "error": "完整 fallback 链均未获取到 PDF",
                "detail": str(result)[:500],
                "hint": "该文可能无 OA 版本；可告知用户手动获取，或检查 Sci-Hub 镜像可用性（也可在 Valves 设置 scihub_url 为可用镜像）",
            },
            ensure_ascii=False,
        )
