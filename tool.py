"""
title: Academic Paper Search
description: 学术论文搜索、全文阅读、PDF 下载入 Knowledge（RAG）。智慧芽文献/专利 + IEEE Xplore + arXiv/PubMed/Semantic Scholar/OpenAlex/CORE/HAL/dblp/Zenodo/IACR/DOAJ/OpenAIRE/Europe PMC/Crossref/PMC + firecrawl（web文献检索），web 搜索兜底 tavily→firecrawl。单源失败不影响整体，源连接错误时自动补位。

  【搜索源清单】（search_papers 的 sources 参数可用值，'all' 为全部）：
  · 预印本/开放获取（可搜可读全文）: arxiv, iacr, pmc, europepmc
  · 综合索引: semantic, openalex, crossref, pubmed, core, openaire, doaj, hal,
    dblp (CS书目), zenodo (OA仓储)
  · 需配 key/url 才启用（未配静默跳过）: zhihuiya (zhihuiya_apikey),
    ieee (ieee_apikey), firecrawl (firecrawl_base_url，web文献检索)
  · 学科新论文浏览（非关键词检索，需 sources+biorxiv_category 显式用）: biorxiv, medrxiv
  · 不稳定（可能 403/超时，失败自动降级）: google_scholar, ssrn, base, citeseerx
  · 不可用: acm（未实现）, unpaywall（仅DOI查询，用于下载 fallback）
  · v2.9 起 semantic/openalex/crossref/europepmc/core/biorxiv/medrxiv/iacr 转直连
  · v2.9.1：AND 语义源（hal/pubmed/pmc/europepmc/openaire/ieee）长查询 0 命中时
    自动逐级砍尾词放宽；dblp 反爬拦截页（200+HTML）明确报错不再误报 JSON 解析失败
  · v2.9.3：dblp 被 Anubis 拦截时走 firecrawl（headless 浏览器自动解 JS 质询）兜底；
    配 apify_rotator_base_url 后 google_scholar 改走 Apify actor（绕 Google CAPTCHA）
  · v2.9.4/2.9.5：google_scholar 直连链——firecrawl 抓搜索页首选（官方云 stealth
    出口稳定穿透）→ tavily extract(advanced) 次选 → Apify actor 兜底
    → 三个都没配才走 papers 服务
  · v2.9.8：后端全面切自托管 papers-service（papers_service_url valve）——
    paper-search-mcp/mcpo papers 服务退役；搜索安全网与 read 快车道同形状迁移；
    download 走 papers-service 的 download_with_fallback（OA 链+身份闸字节直传）

  【查询适配】search_papers 按源自动分发查询变体（不损语义，LLM 无需处理）：
  · 大多数源用原始完整查询；zhihuiya/doaj 对长自然语言会 0 命中，
    自动精简为核心术语后恢复（返回含 query_adapted 字段说明）

  【web 搜索兜底】源出现连接/超时错误时自动触发，tavily 主 → firecrawl 备，
  域名限定动态映射自失败源（如 arxiv 失败只补 arxiv.org）。配 tavily_base_url /
  firecrawl_base_url 启用，返回含 fallback_domains/fallback_limit 字段。

  【工具用法】
  1. search_papers(query)      → 多源并发搜索+去重，返回标题/作者/摘要/引用数/pdf_url
  2. read_paper(source, paper_id, pdf_url) → 读全文（pdf_url 作 fallback，建议总传）
  3. download_paper_to_knowledge(...)      → PDF 下载并加入 Knowledge 知识库
  4. search_patents(query)     → 智慧芽专利语义检索（需配 zhihuiya_apikey）
  5. read_patent(patent_number) → 读专利全文 markdown（权利要求+说明书+法律状态）
author: openags-bridge
requirements: requests, pymupdf, anyio
version: 2.9.8
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

LITERAL_SOURCES = frozenset({"zhihuiya", "doaj"})
# 直连源（绕 papers 服务）。pubmed/pmc 直连原因（2026-08 实测）：旧后端 pubmed.py 走 HTTPS 且
# requests.get 无 timeout，境外出口对突发并发 TLS 不稳（SSL EOF）时会无限挂起，asyncio.gather
# 等齐所有源 → 整批 180s 超时，首批尤甚（DNS/连接冷 + 并发突发）；改走 HTTP + timeout=20 + 3次退避。
# arxiv 直连原因（v2.8）：去 paper-search-mcp 依赖的第一步——后端适配器同步无超时是共同风险，
# arxiv 是搜索量最大的源，先接管。注意 arxiv 走 https（http 会 301），与 NCBI 相反。
# v2.9：semantic/openalex/crossref/europepmc/core/biorxiv/medrxiv/iacr 全部转直连
# v2.9.8 起安全网/快车道后端是自托管 papers-service（papers_service_url）。
DIRECT_SOURCES = frozenset({
    "arxiv", "zhihuiya", "hal", "patsnap", "dblp", "zenodo", "ieee", "openaire",
    "firecrawl", "pubmed", "pmc",
    "semantic", "openalex", "crossref", "europepmc", "core",
    "biorxiv", "medrxiv", "iacr",
})
# 后端可提供服务的全部源（v2.9 起仅剩未直连的：doaj 字面源 + 不稳定源 + 特殊用途源；
# citeseerx/base/ssrn/unpaywall/acm 等虽在后端但默认不启用）
_BACKEND_ALL_SOURCES = (
    "doaj,google_scholar,ssrn,unpaywall,citeseerx,base,acm"
)
# all_mode 拆分时语义组使用的后端源（去掉字面源 doaj，留给 core 变体）
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
    """对字面源（zhihuiya/doaj）在 core 基础上按术语区分度截断到 max_terms 词。
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


def _relax_and(query: str, try_fn, max_terms: int = 6):
    """AND 语义源（空格=AND：hal/europepmc/pubmed/pmc/openaire/ieee）的逐级放宽：
    词数 >max_terms 先按区分度蒸馏；然后 AND 前 min(len,5) 词，0 命中砍尾词重试，
    直到 1 词。try_fn(q)->list，返回首个非空结果；全空返回 []。
    动机（2026-09 实测）：11 词自然语言查询在这些源全 AND → 0 命中，砍到 2-3 词即恢复
    （hal 0→61、europepmc 0→55）。短查询首次即中，行为不变。"""
    terms = (query or "").split()
    if len(terms) > max_terms:
        terms = _distill_core_terms(" ".join(terms), max_terms=max_terms).split()
    if not terms:
        return []
    for k in range(min(len(terms), 5), 0, -1):
        papers = try_fn(" ".join(terms[:k]))
        if papers:
            return papers
    return []


class _AntiBotBlocked(RuntimeError):
    """源明确返回反爬拦截页（如 Anubis 质询、人机验证）。
    区别于普通网络错误：触发 firecrawl（headless 浏览器自动解 JS 质询）兜底重试一次。"""


class Tools:
    class Valves(BaseModel):
        papers_service_url: str = Field(
            default="http://papers-service:3200/papers",
            description="papers 服务 base URL（自托管 papers-service：search_*/read_*/*_paper/"
            "download_with_fallback 端点，同网络容器名直连）。检索 19 源 + read 12 源 + OA 下载链都在这",
        )
        mcpo_api_key: str = Field(
            default="",
            description="mcpo --api-key（papers-service 内网直连一般不需要；仅当 papers-service 前面挂了带 key 的网关时填）",
        )
        download_fallback_url: str = Field(
            default="http://papers-service:3200/papers/download_with_fallback",
            description="download_paper_to_knowledge 的 OA 下载链端点：papers-service 在 "
            "HTTP 响应体里直接回 PDF 字节（内存流转，不落盘）。链=native→OA 仓储→Unpaywall"
            "→可选 Sci-Hub，每步过标题身份闸。留空仅剩历史兼容场景（旧落盘模式，"
            "paper-search-mcp 已退役，实际不可用），保持默认即可",
        )
        openwebui_url: str = Field(
            default="http://open-webui:8080", description="OpenWebUI 容器名:端口"
        )
        owui_api_key: str = Field(
            default="", description="fallback key（一般用不到，自动透传用户token）"
        )
        shared_download_dir: str = Field(
            default="/downloads",
            description="（历史遗留，当前架构用不到）旧 mcpo/paper-search-mcp 落盘模式需要 "
            "open-webui 与后端挂同一共享卷才能读回 PDF；papers-service 字节直传后无需任何共享卷",
        )
        zhihuiya_apikey: str = Field(
            default="",
            description="智慧芽(zhihuiya)科学文献 API key（管理员/公司级）。需同时在 default_sources 含 zhihuiya 才启用，留空则该源静默跳过",
        )
        ieee_apikey: str = Field(
            default="",
            description="IEEE Xplore API key（管理员级）。需同时在 default_sources 含 ieee 才启用，留空则该源静默跳过",
        )
        zenodo_access_token: str = Field(
            default="",
            description="Zenodo Access Token（管理员级，可选；配了额度更高/可访问受限记录，留空走公共 API）",
        )
        ncbi_api_key: str = Field(
            default="",
            description="NCBI E-utilities API key（管理员级，可选）。用于 pubmed/pmc 直连检索：配了 10 req/s、否则 3 req/s。2026-08 实测后端 paper-search-mcp 的 pubmed.py 走 HTTPS 且无 timeout，境外链路 SSL EOF 后会无限挂起拖垮整批（180s 超时），故 pubmed/pmc 改直连 HTTP",
        )
        firecrawl_base_url: str = Field(
            default="",
            description="mcpo firecrawl 服务 base URL（如 http://mcpo:8000/firecrawl）。两个用途：(a) 独立源——需在 default_sources 含 firecrawl；(b) 二级 web 兜底——tavily 未配/失败时自动用。留空则两者都不启用",
        )
        tavily_base_url: str = Field(
            default="",
            description="tavily 代理 base URL（如 http://api-key-rotator:8788/tavily）。两个用途：(a) 二级 web 兜底——其他源出现连接/超时错误时用 include_domains 限定学术站检索补位；(b) google_scholar 直连链第二级——firecrawl 失败时用 /extract(advanced) 抓 scholar 搜索页。留空则两者都不启用",
        )
        jina_api_key: str = Field(
            default="",
            description="Jina Reader API key（管理员级，可选）。read_paper 的网页全文 fallback 用 r.jina.ai：不配 key 免费 20 RPM，配了 500 RPM。留空走 keyless",
        )
        semantic_api_key: str = Field(
            default="",
            description="Semantic Scholar Graph API key（管理员级，可选）。用于 semantic 直连检索：配了独立配额（1 RPS），留空走匿名共享池（易被限流 429，已自动退避重试）。403 说明 key 失效，会自动降级匿名重试一次",
        )
        core_api_key: str = Field(
            default="",
            description="CORE API key（管理员级，可选）。用于 core 直连检索：留空走匿名（配额低），401/403 时自动降级匿名重试一次",
        )
        apify_rotator_base_url: str = Field(
            default="",
            description="api-key-rotator 的 Apify 转发基址（管理员级，可选），如 http://api-key-rotator:8788"
            "（转发 /v2/acts → api.apify.com，key 池自动轮转）。google_scholar 的最终兜底通路："
            "首选 firecrawl_base_url 抓搜索页，次选 tavily_base_url 的 extract(advanced)，"
            "最后才落 Apify actor（johnvc/google-scholar-api，PAY_PER_EVENT 付费按次计费）；"
            "三个都没配则 google_scholar 仍走后端",
        )

    class UserValves(BaseModel):
        default_sources: str = Field(
            default="arxiv,pubmed,semantic,crossref,openalex,pmc,core,europepmc,dblp,openaire,doaj,hal,zenodo,google_scholar,zhihuiya,ieee,firecrawl",
            description="默认搜索源：'all'=全部21源（慢，30s+）；或逗号分隔子集。默认未包含的源：iacr（密码学 ePrint 细分库，需要时 sources=iacr 显式调用）、citeseerx,ssrn,base,acm,unpaywall；biorxiv/medrxiv 为学科近30天浏览（非关键词检索），需 sources+biorxiv_category 显式调用。zhihuiya/ieee/firecrawl 在默认列表中，但仅当配了对应 key/url 才真正启用（未配静默跳过）",
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
        firecrawl_base_url: str = Field(
            default="",
            description="firecrawl 独立源的 base URL（个人级，非空时覆盖管理员；留空用管理员配置）",
        )
        tavily_base_url: str = Field(
            default="",
            description="tavily 兜底的 base URL（个人级，非空时覆盖管理员；留空用管理员配置）",
        )
        ncbi_api_key: str = Field(
            default="",
            description="NCBI E-utilities API key（个人级，非空时覆盖管理员 key）",
            json_schema_extra={"input": {"type": "password"}},
        )
        jina_api_key: str = Field(
            default="",
            description="Jina Reader API key（个人级，非空时覆盖管理员 key）",
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
        "cannot be read directly",
        "cannot be read",
        "only metadata",
        "no full text",
        "no full-text",
        "full text is not available",
        "full-text is not available",
        "metadata and abstracts are available",
    )

    @classmethod
    def _is_unsupported_msg(cls, text) -> bool:
        if not isinstance(text, str):
            return True
        t = text.strip().lower()
        if len(t) < 200:
            return True
        # 后端"无全文"提示通常很短且含特征词；放宽到 <3000 兜底（真实全文远超此长度）
        return len(t) < 3000 and any(m in t for m in cls._UNSUPPORTED_MARKERS)

    # ---------- 下载身份闸（v2.9.6）：防 OA 链下错文档进 RAG ----------
    # 事故（2026-09）：搜 sensor 论文，OA fallback 下到一个几百 MB 的临床验证
    # 数据"论文集"直接入库，把 OWUI RAG 卡死。闸 = 全文 token 与标题匹配；
    # 无大小限制（OA 链拿不到 Content-Length，且合集含目标文即放行）。
    _TITLE_STOP = frozenset(
        (
            "a an the of in on for with and or to is are was were be by at as from "
            "its their his her our your via using based study research analysis"
        ).split()
    )

    @classmethod
    def _title_tokens(cls, s: str) -> set:
        """小写字母数字 token，去停用词。中文按字拆（标题无空格），单字也保留。"""
        import re as _re

        out = set()
        for w in _re.findall(r"[0-9a-z]+|[一-鿿]", (s or "").lower()):
            if len(w) > 1 or w.isdigit() or _re.match(r"[一-鿿]", w):
                if w not in cls._TITLE_STOP:
                    out.add(w)
        return out

    @classmethod
    def _title_in_pdf(cls, pdf_text: str, title: str) -> tuple[bool, float]:
        """标题 token 在 PDF 全文页的覆盖率；>=0.6 视为同一文档（论文集含目标文也算）。"""
        want = cls._title_tokens(title)
        if not want:
            return True, 1.0  # 无可校验 token（纯符号标题）→ 不拦
        got = cls._title_tokens((pdf_text or "")[:6000])  # 首部足够；只取样控制成本
        if not got:
            return True, 0.0  # 提取不出 token（扫描版）→ 无法校验，不拦
        hit = len(want & got) / len(want)
        return hit >= 0.6, hit

    def _verify_downloaded_pdf(self, data: bytes, title: str, via: str) -> None:
        """下载后核对全文与标题的匹配度，不匹配拒绝入库（防止 OA 链下错文档）。
        提取失败（扫描版）不拦——宁可放过不可误杀。"""
        try:
            text = self._pdf_to_text(data)
        except Exception:
            return
        if not text:
            return
        ok, ratio = self._title_in_pdf(text, title)
        if ok:
            return
        head = " ".join(text[:120].split())
        raise RuntimeError(
            f"身份校验未通过（标题 token 覆盖率 {ratio:.0%} < 60%）：{via} 下载的内容与"
            f"《{title}》不匹配，疑似下错文档（开头为：{head[:100]}）。已拒绝入库。"
        )

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
        绕过第三方后端 hal.py 的 isoformat bug。anyio 线程池包装，不阻塞事件循环。
        Solr 空格=AND：长查询 0 命中时经 _relax_and 逐级砍尾词放宽（v2.9.1）。"""
        def _try(q):
            r = requests.get(
                self._HAL_SEARCH_URL,
                params={
                    "q": q,
                    "fl": self._HAL_FIELDS,
                    "rows": max(1, min(int(limit), 100)),
                    "wt": "json",
                    "sort": "score desc",
                },
                headers={"User-Agent": "paper-search-mcp/1.0", "Accept": "application/json"},
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
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

        try:
            return await anyio.to_thread.run_sync(lambda: _relax_and(query, _try))
        except Exception as e:
            raise RuntimeError(f"HAL 检索失败: {e}")

    _DBLP_SEARCH_URL = "https://dblp.org/search/publ/api"
    _UNPAYWALL_API = "https://api.unpaywall.org/v2"

    async def _dblp_search(self, query: str, limit: int, __user__=None) -> list:
        """直连 dblp JSON API，绕后端 dblp.py 的并发 ConnectionError + 无退避重试。
        退避策略：429/5xx/连接错误最多重试3次，间隔 2s/4s/8s。
        反爬（v2.9.3）：200 但非 JSON = Anubis 质询页 → _AntiBotBlocked，配了
        firecrawl_base_url 则走 firecrawl（headless Chrome 自动解 JS PoW，
        2026-09 实测穿透，返回完整 JSON）兜底重试一次。
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
                        # 200 但非 JSON = 反爬拦截页（2026-09 实测 dblp 上 Anubis
                        # "Making sure you're not a bot" 质询页，requests 解不了 PoW）
                        # —— 同一路径重试无意义，抛 _AntiBotBlocked 交外层走浏览器兜底
                        ct = r.headers.get("content-type", "")
                        if "json" not in ct:
                            raise _AntiBotBlocked(
                                f"dblp 返回非 JSON（{ct or 'unknown'}），疑似反爬拦截页"
                                "（Anubis 质询）")
                        return r.json()
                    if r.status_code in (429, 500, 502, 503, 504):
                        raise RuntimeError(f"dblp HTTP {r.status_code}")
                    r.raise_for_status()
                except RuntimeError:
                    raise  # 含 _AntiBotBlocked：立即上抛，不重试
                except Exception as e:
                    last_exc = e
                if attempt < max_attempts - 1:
                    import time
                    time.sleep(backoff[attempt])
            raise RuntimeError(f"dblp 检索失败（重试{max_attempts}次）: {last_exc}")

        try:
            data = await anyio.to_thread.run_sync(_fetch)
        except _AntiBotBlocked as ab:
            if not self._firecrawl_base(__user__):
                raise RuntimeError(
                    f"dblp 检索失败: {ab}，当前 IP 无法直连；"
                    "配 firecrawl_base_url 可用浏览器引擎自动解质询兜底")
            data = await self._dblp_via_firecrawl(query, limit, __user__)
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

    async def _dblp_via_firecrawl(self, query: str, limit: int, __user__=None) -> dict:
        """Anubis 拦截时走 firecrawl 兜底：headless Chrome 自动解 JS PoW
        （2026-09 实测穿透，rawHtml 的 <pre> 里是完整 JSON）。返回解析后的 dict。"""
        from urllib.parse import urlencode
        api_url = f"{self._DBLP_SEARCH_URL}?{urlencode({'q': query, 'format': 'json', 'h': max(1, min(int(limit), 100))})}"
        base = self._firecrawl_base(__user__)
        raw = await anyio.to_thread.run_sync(
            self._mcp_call_service_url, base, "firecrawl_scrape",
            {"url": api_url, "formats": ["rawHtml"], "waitFor": 8000,
             "onlyMainContent": False}, 90)
        html_text = (raw or {}).get("rawHtml") or ""
        # 两种形态：HTML 包装（Chrome 把 JSON 塞进 <pre>，hosted 侧实测）或
        # 直接返回 JSON body（Content-Type=application/json 时 firecrawl 原样透传，mcpo 侧实测）
        m = re.search(r"<pre[^>]*>(.*?)</pre>", html_text, re.S)
        cand = m.group(1) if m else html_text.strip()
        try:
            import html as _html
            return json.loads(_html.unescape(cand))
        except Exception as e:
            raise RuntimeError(f"firecrawl 兜底未取到 JSON（可能质询未解开）: {e}")

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
        网络类错误最多重试3次，退避 2s/4s。
        querytext 空格=AND：长查询 0 命中时经 _relax_and 逐级砍尾词放宽（v2.9.1）。"""
        max_attempts = 3
        backoff = [2, 4]

        def _try(q: str) -> list:
            last_exc = None
            data = None
            for attempt in range(max_attempts):
                try:
                    r = requests.get(
                        self._IEEE_SEARCH_URL,
                        params={
                            "apikey": key,
                            "querytext": q,
                            "max_records": max(1, min(int(limit), 200)),
                            "format": "json",
                            "sort_order": "desc",
                            "sort_field": "relevance",
                        },
                        headers={"Accept": "application/json"},
                        timeout=30,
                    )
                    if r.status_code == 200:
                        data = r.json()
                        break
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
            if data is None:
                raise RuntimeError(f"IEEE 检索失败（重试{max_attempts}次）: {last_exc}")

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

        try:
            return await anyio.to_thread.run_sync(lambda: _relax_and(query, _try))
        except Exception as e:
            # 错误信息可能包含 apikey，需脱敏
            err_msg = str(e)
            if "apikey=" in err_msg:
                err_msg = re.sub(r"apikey=[^&\s]+", "apikey=***", err_msg)
            raise RuntimeError(f"IEEE 检索失败: {err_msg}")

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

    _OPENAIRE_SEARCH_URL = "https://api.openaire.eu/search/publications"

    async def _openaire_search(self, query: str, limit: int) -> list:
        """直连 OpenAIRE search/publications API（正确参数是 keywords，不是 query）。
        绕后端 openaire.py 双 bug（2026-08 实测 100% 必现）：
        路径1 researchProducts 端点 404（已废弃，Tomcat 报错）；路径2 legacy fallback
        用 query= 参数 → OpenAIRE 只认 keywords → 400 Bad Request。
        与 dblp/zenodo 同模式，网络类错误 3 次退避（2s/4s），4xx 不重试。
        keywords= 是 AND 语义：长查询 0 命中时经 _relax_and 逐级砍尾词放宽（v2.9.1）。"""
        max_attempts = 3
        backoff = [2, 4]

        def _try(q):
            last_exc = None
            for attempt in range(max_attempts):
                try:
                    r = requests.get(
                        self._OPENAIRE_SEARCH_URL,
                        params={
                            "keywords": q,
                            "format": "json",
                            "size": max(1, min(int(limit), 100)),
                            "page": 1,
                        },
                        headers={
                            "User-Agent": "paper-search-tool/2.5 (OpenWebUI academic search)",
                            "Accept": "application/json",
                        },
                        timeout=30,
                    )
                    if r.status_code == 200:
                        data = r.json()
                        break
                    if r.status_code in (429, 500, 502, 503, 504):
                        last_exc = RuntimeError(f"OpenAIRE HTTP {r.status_code}")
                    else:
                        r.raise_for_status()
                except Exception as e:
                    if isinstance(e, requests.exceptions.HTTPError):
                        raise
                    last_exc = e
                if attempt < max_attempts - 1:
                    import time
                    time.sleep(backoff[attempt])
            else:
                raise RuntimeError(f"OpenAIRE 检索失败（重试{max_attempts}次）: {last_exc}")

            # OpenAIRE json: response.results.result[]，每条 metadata.oaf:entity.oaf:result
            # 实测结构（2026-08）：title/creator/pid 是 dict 或 dict 列表，文本在 "$" 键
            resp = (data or {}).get("response") or {}
            results = (resp.get("results") or {}).get("result") or []
            if isinstance(results, dict):
                results = [results]

            papers = []

            def _text(node):
                """dict -> node['$']；list -> 第一个 dict 的 '$'；str -> 原样"""
                if isinstance(node, dict):
                    return str(node.get("$") or "")
                if isinstance(node, list):
                    for n in node:
                        t = _text(n)
                        if t:
                            return t
                    return ""
                return str(node or "")

            for r in results[:limit]:
                if not isinstance(r, dict):
                    continue
                ent = ((r.get("metadata") or {}).get("oaf:entity") or {}).get("oaf:result") or {}
                title = _text(ent.get("title")).strip()
                if not title:
                    continue
                creators = ent.get("creator") or []
                if isinstance(creators, (str, dict)):
                    creators = [creators]
                authors = [_text(c).strip() for c in creators]
                authors = [a for a in authors if a]
                pub_date = _text(ent.get("dateofacceptance"))[:10]
                pids = ent.get("pid") or []
                if isinstance(pids, dict):
                    pids = [pids]
                doi = next((_text(p) for p in pids
                            if isinstance(p, dict) and p.get("@classid") == "doi"), "")
                bar = ent.get("bestaccessright") or {}
                oa = "open" in str(bar.get("@classid", "")).lower() if isinstance(bar, dict) else False
                pdf_url = ""
                ch = (ent.get("children") or {}).get("instance") or []
                if isinstance(ch, dict):
                    ch = [ch]
                for inst in ch:
                    url = _text((inst.get("webresource") or {}).get("url") if isinstance(inst, dict) else "")
                    if url and ".pdf" in url.lower():
                        pdf_url = url
                        break
                obj_id = _text((r.get("header") or {}).get("dri:objIdentifier"))
                papers.append({
                    "title": title,
                    "authors": "; ".join(authors),
                    "published_date": pub_date,
                    "abstract": "",
                    "paper_id": f"openaire:{obj_id[:50]}",
                    "doi": doi,
                    "source": "openaire",
                    "pdf_url": pdf_url if oa else "",
                    "citations": 0,
                    "url": f"https://doi.org/{doi}" if doi else "",
                })
            return papers

        try:
            return await anyio.to_thread.run_sync(lambda: _relax_and(query, _try))
        except Exception as e:
            raise RuntimeError(f"OpenAIRE 检索失败: {e}")

    # ---------- NCBI 直连（pubmed/pmc）----------
    # 2026-08 实测根因：后端 paper-search-mcp 的 pubmed.py 走 HTTPS 且 requests.get 无
    # timeout，境外出口对突发并发 TLS 不稳（SSL: UNEXPECTED_EOF_WHILE_READING，urllib3 对
    # SSL EOF 默认不重试）→ 偶发无限挂起，后端 asyncio.gather 等齐所有源 → 整批 180s 超时。
    # 每次 tool 调用的首个后端批次必现（DNS/连接冷 + 语义/字面拆分同刻并发），后续复现看网络。
    # 直连改走 HTTP（绕开境外 TLS 中间设备，NCBI 与 pmc/europepmc 等同 host 实测零异常）+
    # timeout=20 + 3 次退避（2s/4s），429/5xx/连接错误重试，4xx 不重试。
    _NCBI_EUTILS = "http://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def _ncbi_key(self, __user__=None) -> str:
        """NCBI API key：UserValves 优先，否则 admin Valves；都没有返回 ""。"""
        uv = __user__.get("valves") if __user__ else None
        user_key = (getattr(uv, "ncbi_api_key", "") or "").strip() if uv else ""
        return user_key or (getattr(self.valves, "ncbi_api_key", "") or "").strip()

    def _eutils_get(self, path: str, params: dict, api_key: str = ""):
        """E-utilities GET（HTTP），3 次退避；返回 response。"""
        import time
        if api_key:
            params = {**params, "api_key": api_key}
        last_exc = None
        for attempt in range(3):
            try:
                r = requests.get(
                    f"{self._NCBI_EUTILS}/{path}",
                    params=params,
                    headers={"User-Agent": "paper-search-tool/2.6 (OpenWebUI academic search)"},
                    timeout=20,
                )
                if r.status_code == 200:
                    return r
                if r.status_code in (429, 500, 502, 503, 504):
                    last_exc = RuntimeError(f"NCBI HTTP {r.status_code}")
                else:
                    r.raise_for_status()
            except Exception as e:
                if isinstance(e, requests.exceptions.HTTPError):
                    raise
                last_exc = e
            if attempt < 2:
                time.sleep([2, 4][attempt])
        raise RuntimeError(f"NCBI E-utilities 失败（重试3次）: {last_exc}")

    @staticmethod
    def _xml_text(elem) -> str:
        """ElementTree 节点 → 拼接全部文本（AbstractText/ArticleTitle 常含子标签）。"""
        return "".join(elem.itertext()).strip() if elem is not None else ""

    def _parse_pubmed_xml(self, root, source: str, limit: int) -> list:
        """PubmedArticleSet → paper dict 列表。pubmed 与 pmc 共用同一 XML 格式；
        source 决定 paper_id 前缀与 url（pmc 用 PMCID，pubmed 用 PMID）。"""
        papers = []
        for article in root.findall(".//PubmedArticle"):
            if len(papers) >= limit:
                break
            medline = article.find("MedlineCitation")
            art = medline.find("Article") if medline is not None else None
            if medline is None or art is None:
                continue
            pmid = self._xml_text(medline.find("PMID"))
            if not pmid:
                continue
            title = self._xml_text(art.find("ArticleTitle"))
            if not title:
                continue
            authors = []
            for au in art.findall("AuthorList/Author"):
                last = self._xml_text(au.find("LastName"))
                init = self._xml_text(au.find("Initials"))
                coll = self._xml_text(au.find("CollectiveName"))
                if last:
                    authors.append(f"{last} {init}".strip())
                elif coll:
                    authors.append(coll)
            abstract = " ".join(
                t for t in (self._xml_text(a) for a in art.findall("Abstract/AbstractText")) if t
            )
            ids = {}
            for aid in article.findall("PubmedData/ArticleIdList/ArticleId"):
                id_type = aid.get("IdType", "")
                if id_type and aid.text:
                    ids[id_type] = aid.text.strip()
            doi = ids.get("doi", "")
            pmcid = ids.get("pmc", "")
            year = (art.findtext("Journal/JournalIssue/PubDate/Year")
                    or art.findtext("Journal/JournalIssue/PubDate/MedlineDate") or "")[:4]
            if source == "pmc":
                if not pmcid:  # PMC 库正常都有 PMCID，防御性跳过
                    continue
                paper_id, url = f"pmc:{pmcid}", f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/"
            else:
                paper_id, url = f"pubmed:{pmid}", f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            papers.append({
                "title": title,
                "authors": "; ".join(authors),
                "published_date": year,
                "abstract": abstract,
                "paper_id": paper_id,
                "doi": doi,
                "source": source,
                "pdf_url": f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/" if pmcid else "",
                "citations": 0,
                "url": url,
            })
        return papers

    @staticmethod
    def _first_text(elem, paths) -> str:
        """按 xpath 列表取第一个非空文本（兼容命名空间变体）。"""
        for p in paths:
            t = elem.findtext(p)
            if t:
                return t.strip()
        return ""

    def _parse_pmc_article(self, article, limit_count: int) -> dict:
        """JATS <article>（PMC efetch 格式）→ paper dict。"""
        front = article.find("front")
        ameta = front.find("article-meta") if front is not None else None
        if ameta is None:
            return None
        ids = {}
        for aid in ameta.findall("article-id"):
            id_type = aid.get("pub-id-type", "")
            if id_type and aid.text:
                ids[id_type] = aid.text.strip()
        # 真实 PMC JATS 的 pub-id-type 是 "pmcid"（值带 PMC 前缀）/"pmcaid"（纯数字），不是 "pmc"
        pmcid = ids.get("pmcid") or ids.get("pmc") or ids.get("pmcaid", "")
        if pmcid and not pmcid.upper().startswith("PMC"):
            pmcid = f"PMC{pmcid}"
        if not pmcid:
            return None
        title_node = ameta.find("title-group/article-title")
        title = self._xml_text(title_node)
        if not title:
            return None
        authors = []
        for contrib in ameta.findall("contrib-group/contrib"):
            if contrib.get("contrib-type", "author") != "author":
                continue
            surname = self._xml_text(contrib.find("name/surname"))
            given = self._xml_text(contrib.find("name/given-names"))
            coll = self._xml_text(contrib.find("collab"))
            if surname:
                authors.append(f"{surname} {given}".strip())
            elif coll:
                authors.append(coll)
        abstract = " ".join(
            t for t in (self._xml_text(a) for a in ameta.findall("abstract")) if t
        )
        year = (self._first_text(ameta, ["pub-date/year"])
                or self._first_text(ameta, ["pub-date/date"])
                or self._first_text(ameta, ["history/date/year"]))[:4]
        return {
            "title": title,
            "authors": "; ".join(authors),
            "published_date": year,
            "abstract": abstract,
            "paper_id": f"pmc:{pmcid}",
            "doi": ids.get("doi", ""),
            "source": "pmc",
            "pdf_url": f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/",
            "citations": 0,
            "url": f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/",
        }

    async def _pubmed_search(self, query: str, limit: int, __user__=None) -> list:
        """直连 NCBI E-utilities 搜 PubMed（esearch+efetch），绕后端 pubmed.py 无 timeout 挂起 bug。
        term 空格=AND：长查询 0 命中时经 _relax_and 逐级砍尾词放宽（v2.9.1）。"""
        from xml.etree import ElementTree as ET

        def _fetch():
            key = self._ncbi_key(__user__)

            def _try(q):
                r1 = self._eutils_get("esearch.fcgi", {
                    "db": "pubmed", "term": q, "retmax": max(1, min(int(limit), 200)),
                    "retmode": "xml", "sort": "relevance"}, key)
                ids = [e.text for e in ET.fromstring(r1.content).findall(".//Id") if e.text]
                if not ids:
                    return []
                r2 = self._eutils_get("efetch.fcgi", {
                    "db": "pubmed", "id": ",".join(ids), "retmode": "xml"}, key)
                return self._parse_pubmed_xml(ET.fromstring(r2.content), "pubmed", limit)

            return _relax_and(query, _try)

        try:
            return await anyio.to_thread.run_sync(_fetch)
        except Exception as e:
            raise RuntimeError(f"PubMed 检索失败: {e}")

    async def _pmc_search(self, query: str, limit: int, __user__=None) -> list:
        """直连 NCBI E-utilities 搜 PMC 全文库（esearch+efetch），绕后端 pmc.py 同 host HTTPS 风险。
        PMC efetch 返回 JATS 全文 XML（pmc-articleset），非 PubmedArticleSet → 用 JATS 解析。
        term 空格=AND：长查询 0 命中时经 _relax_and 逐级砍尾词放宽（v2.9.1）。"""
        from xml.etree import ElementTree as ET

        def _fetch():
            key = self._ncbi_key(__user__)

            def _try(q):
                r1 = self._eutils_get("esearch.fcgi", {
                    "db": "pmc", "term": q, "retmax": max(1, min(int(limit), 200)),
                    "retmode": "xml", "sort": "relevance"}, key)
                ids = [e.text for e in ET.fromstring(r1.content).findall(".//Id") if e.text]
                if not ids:
                    return []
                r2 = self._eutils_get("efetch.fcgi", {
                    "db": "pmc", "id": ",".join(ids), "retmode": "xml"}, key)
                root = ET.fromstring(r2.content)
                papers = []
                for art in root.findall(".//article"):
                    p = self._parse_pmc_article(art, len(papers))
                    if p:
                        papers.append(p)
                    if len(papers) >= limit:
                        break
                return papers

            return _relax_and(query, _try)

        try:
            return await anyio.to_thread.run_sync(_fetch)
        except Exception as e:
            raise RuntimeError(f"PMC 检索失败: {e}")

    # ---------- arXiv 直连（v2.8：替代旧后端 paper-search-mcp 的 arxiv 源）----------
    _ARXIV_API = "https://export.arxiv.org/api/query"
    _ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom",
                 "arxiv": "http://arxiv.org/schemas/atom"}

    def _arxiv_get(self, params: dict):
        """arXiv Atom API GET，3 次退避；返回 response。注意 export.arxiv.org 的
        http 会 301 到 https，必须直连 https（与 NCBI 刻意走 http 相反）。"""
        import time
        last_exc = None
        for attempt in range(3):
            try:
                r = requests.get(
                    self._ARXIV_API,
                    params=params,
                    headers={"User-Agent": "paper-search-tool/2.8 (OpenWebUI academic search)"},
                    timeout=20,
                )
                if r.status_code == 200:
                    return r
                if r.status_code in (429, 500, 502, 503, 504):
                    last_exc = RuntimeError(f"arXiv HTTP {r.status_code}")
                else:
                    r.raise_for_status()
            except Exception as e:
                if isinstance(e, requests.exceptions.HTTPError):
                    raise
                last_exc = e
            if attempt < 2:
                time.sleep([2, 4][attempt])
        raise RuntimeError(f"arXiv API 失败（重试3次）: {last_exc}")

    def _parse_arxiv_atom(self, root, limit: int) -> list:
        """arXiv Atom feed → paper dict 列表。id 去掉 vN 版本后缀（ canonical id，
        read/download 链按无版本 id 解析）。"""
        papers = []
        for entry in root.findall("atom:entry", self._ARXIV_NS):
            if len(papers) >= limit:
                break
            id_url = self._xml_text(entry.find("atom:id", self._ARXIV_NS))
            m = re.search(r"/abs/([^/\s]+)", id_url)
            if not m:
                continue
            aid = re.sub(r"v\d+$", "", m.group(1))
            title = re.sub(r"\s+", " ", self._xml_text(entry.find("atom:title", self._ARXIV_NS)))
            if not title:
                continue
            authors = "; ".join(
                n for n in (self._xml_text(a.find("atom:name", self._ARXIV_NS))
                            for a in entry.findall("atom:author", self._ARXIV_NS)) if n
            )
            abstract = re.sub(r"\s+", " ", self._xml_text(entry.find("atom:summary", self._ARXIV_NS)))
            published = self._xml_text(entry.find("atom:published", self._ARXIV_NS))[:10]
            doi = self._xml_text(entry.find("arxiv:doi", self._ARXIV_NS))
            papers.append({
                "title": title,
                "authors": authors,
                "published_date": published,
                "abstract": abstract,
                "paper_id": f"arxiv:{aid}",
                "doi": doi,
                "source": "arxiv",
                "pdf_url": f"https://arxiv.org/pdf/{aid}",
                "citations": 0,
                "url": f"https://arxiv.org/abs/{aid}",
            })
        return papers

    async def _arxiv_search(self, query: str, limit: int) -> list:
        """直连 arXiv Atom API。arXiv 查询语法是字段级布尔组合：全词 AND 过严
        （2026-09 实测 3 个专精词相交即 0 命中），全词 OR 则被高频词灌入噪声
        （sortBy=relevance 也压不住）。因此做逐级放宽：先 AND 全部词（≤5 个），
        0 命中就砍尾词重试，直到剩 1 个最高区分度词——专精查询（如 iCVD）最终
        落在稀有词上结果仍精准，常见查询通常首次即中。调用方传 core 变体。
        单次请求即含 title/authors/abstract/doi 全部元数据，无需二次 fetch。"""
        from xml.etree import ElementTree as ET

        terms = [re.sub(r'["():\\]', "", t) for t in (query or "").split()]
        terms = [t for t in terms if t]
        if len(terms) > 6:
            terms = _distill_core_terms(" ".join(terms), max_terms=6).split()
        if not terms:
            return []

        def _fetch():
            n = int(limit)
            for k in range(min(len(terms), 5), 0, -1):
                r = self._arxiv_get({
                    "search_query": " AND ".join(f"all:{t}" for t in terms[:k]),
                    "start": 0,
                    "max_results": max(1, min(n, 100)),
                    "sortBy": "relevance",
                    "sortOrder": "descending",
                })
                papers = self._parse_arxiv_atom(ET.fromstring(r.content), n)
                if papers:
                    return papers
            return []

        try:
            return await anyio.to_thread.run_sync(_fetch)
        except Exception as e:
            raise RuntimeError(f"arXiv 检索失败: {e}")

    # ---------- v2.9 直连批次二：semantic/openalex/crossref/europepmc/core/biorxiv/medrxiv/iacr ----------
    # 共同模式：_http_get 统一重试（429/5xx 退避 3 次，其余 4xx 立即失败）→ 源专属解析。
    # 全部用 original 查询变体（这些 API 原生支持自然语言/全文检索，不需要 arxiv 式字段布尔）。

    def _http_get(self, url: str, params: dict, name: str, headers: dict = None,
                  honor_retry_after: bool = False):
        """JSON/HTML API GET，3 次退避；429/5xx 重试，其余 4xx 立即失败（RuntimeError
        含 "HTTP <code>"，供调用方识别 401/403 做降级）。honor_retry_after=True 时
        429 优先遵守 Retry-After 头（上限 10s）。返回 response 对象。"""
        import time
        last_exc = None
        for attempt in range(3):
            try:
                r = requests.get(
                    url,
                    params=params,
                    headers={"User-Agent": "paper-search-tool/2.9 (OpenWebUI academic search)",
                             **(headers or {})},
                    timeout=20,
                )
                if r.status_code == 200:
                    return r
                if r.status_code in (429, 500, 502, 503, 504):
                    last_exc = RuntimeError(f"{name} HTTP {r.status_code}")
                    if r.status_code == 429 and honor_retry_after:
                        ra = r.headers.get("Retry-After", "")
                        if ra.isdigit() and attempt < 2:
                            time.sleep(min(int(ra), 10))
                            continue
                else:
                    raise RuntimeError(f"{name} HTTP {r.status_code}")
            except RuntimeError:
                raise
            except Exception as e:
                last_exc = e
            if attempt < 2:
                time.sleep([2, 4][attempt])
        raise RuntimeError(f"{name} API 失败（重试3次）: {last_exc}")

    # ---------- Semantic Scholar 直连 ----------
    _S2_API = "https://api.semanticscholar.org/graph/v1/paper/search"
    _S2_FIELDS = ("title,abstract,year,citationCount,authors,url,"
                  "publicationDate,externalIds,openAccessPdf")

    def _semantic_key(self, __user__=None) -> str:
        uv = __user__.get("valves") if __user__ else None
        return ((getattr(uv, "semantic_api_key", "") or "").strip()
                or (getattr(self.valves, "semantic_api_key", "") or "").strip())

    async def _semantic_search(self, query: str, limit: int, __user__=None) -> list:
        """直连 S2 Graph API。匿名共享池极易 429（实测同 IP 有 shim 在用更甚），
        遵守 Retry-After 重试；配 key 后 403 说明 key 被拒，自动降级匿名重试一次。"""
        def _fetch():
            key = self._semantic_key(__user__)
            params = {"query": query, "limit": min(max(1, int(limit)), 100),
                      "fields": self._S2_FIELDS}
            try:
                data = self._http_get(
                    self._S2_API, params, "Semantic Scholar",
                    headers={"x-api-key": key} if key else None,
                    honor_retry_after=True).json()
            except RuntimeError as e:
                if key and "HTTP 403" in str(e):
                    data = self._http_get(self._S2_API, params, "Semantic Scholar",
                                          honor_retry_after=True).json()
                else:
                    raise
            papers = []
            for it in (data.get("data") or []):
                if len(papers) >= limit:
                    break
                pid = it.get("paperId") or ""
                title = (it.get("title") or "").strip()
                if not pid or not title:
                    continue
                ext = it.get("externalIds") or {}
                oapdf = it.get("openAccessPdf") or {}
                pdf_url = oapdf.get("url") or ""
                if not pdf_url and oapdf.get("disclaimer"):
                    # openAccessPdf.url 为空时 disclaimer 里常嵌直链（doi.org/arxiv）
                    m = re.search(r"https?://[^\s,)]+", oapdf["disclaimer"])
                    pdf_url = m.group(0) if m else ""
                papers.append({
                    "title": title,
                    "authors": "; ".join(a.get("name", "") for a in (it.get("authors") or [])
                                         if a.get("name")),
                    "published_date": it.get("publicationDate") or str(it.get("year") or ""),
                    "abstract": it.get("abstract") or "",
                    "paper_id": f"semantic:{pid}",
                    "doi": ext.get("DOI") or "",
                    "source": "semantic",
                    "pdf_url": pdf_url,
                    "citations": it.get("citationCount") or 0,
                    "url": it.get("url") or f"https://www.semanticscholar.org/paper/{pid}",
                })
            return papers

        try:
            return await anyio.to_thread.run_sync(_fetch)
        except Exception as e:
            raise RuntimeError(f"Semantic Scholar 检索失败: {e}")

    # ---------- OpenAlex 直连 ----------
    _OPENALEX_API = "https://api.openalex.org/works"

    @staticmethod
    def _openalex_abstract(inverted_index: dict) -> str:
        """OpenAlex 用倒排索引存摘要（省空间），按位置重建原文。"""
        if not inverted_index:
            return ""
        try:
            pos_words = [(p, w) for w, ps in inverted_index.items() for p in ps]
            pos_words.sort()
            return " ".join(w for _, w in pos_words)
        except Exception:
            return ""

    async def _openalex_search(self, query: str, limit: int) -> list:
        def _fetch():
            data = self._http_get(self._OPENALEX_API,
                                  {"search": query, "per-page": min(max(1, int(limit)), 200)},
                                  "OpenAlex").json()
            papers = []
            for it in (data.get("results") or []):
                if len(papers) >= limit:
                    break
                wid = (it.get("id") or "").replace("https://openalex.org/", "")
                title = (it.get("title") or "").strip()
                if not wid or not title:
                    continue
                loc = it.get("primary_location") or {}
                oa = it.get("open_access") or {}
                pdf_url = loc.get("pdf_url") or ""
                if not pdf_url and oa.get("is_oa"):
                    pdf_url = oa.get("oa_url") or ""
                papers.append({
                    "title": title,
                    "authors": "; ".join(
                        a.get("author", {}).get("display_name", "")
                        for a in (it.get("authorships") or [])
                        if a.get("author", {}).get("display_name")),
                    "published_date": it.get("publication_date") or "",
                    "abstract": self._openalex_abstract(it.get("abstract_inverted_index")),
                    "paper_id": f"openalex:{wid}",
                    "doi": (it.get("doi") or "").replace("https://doi.org/", ""),
                    "source": "openalex",
                    "pdf_url": pdf_url,
                    "citations": it.get("cited_by_count") or 0,
                    "url": loc.get("landing_page_url") or it.get("id") or "",
                })
            return papers

        try:
            return await anyio.to_thread.run_sync(_fetch)
        except Exception as e:
            raise RuntimeError(f"OpenAlex 检索失败: {e}")

    # ---------- Crossref 直连 ----------
    _CROSSREF_API = "https://api.crossref.org/works"

    async def _crossref_search(self, query: str, limit: int) -> list:
        """直连 Crossref。注意 title 是 list；abstract 可能带 JATS 标签需剥离；
        日期在 published/issued/created 的 date-parts[[y,m,d]]（允许只有年）。"""
        def _fetch():
            data = self._http_get(self._CROSSREF_API,
                                  {"query": query, "rows": min(max(1, int(limit)), 100)},
                                  "Crossref").json()
            papers = []
            for it in (((data.get("message") or {}).get("items")) or []):
                if len(papers) >= limit:
                    break
                t = it.get("title") or []
                title = (t[0] if isinstance(t, list) and t else str(t or "")).strip()
                doi = it.get("DOI") or ""
                if not title or not doi:
                    continue
                authors = []
                for a in (it.get("author") or []):
                    if isinstance(a, dict):
                        nm = " ".join(x for x in (a.get("given", ""), a.get("family", "")) if x)
                        if nm:
                            authors.append(nm)
                date = ""
                for fld in ("published", "issued", "created"):
                    parts = (((it.get(fld) or {}).get("date-parts")) or [[]])[0]
                    if parts:
                        date = str(parts[0]) + "".join(
                            f"-{str(p).zfill(2)}" for p in parts[1:3])
                        break
                pdf_url = ""
                for ln in (it.get("link") or []):
                    if (isinstance(ln, dict)
                            and ln.get("content-type") == "application/pdf"
                            and ln.get("URL")):
                        pdf_url = ln["URL"]
                        break
                cit = it.get("is-referenced-by-count")
                papers.append({
                    "title": title,
                    "authors": "; ".join(authors),
                    "published_date": date,
                    "abstract": re.sub(r"\s+", " ",
                                       re.sub(r"<[^>]+>", " ", it.get("abstract") or "")).strip(),
                    "paper_id": f"crossref:{doi}",
                    "doi": doi,
                    "source": "crossref",
                    "pdf_url": pdf_url,
                    "citations": cit if isinstance(cit, int) else 0,
                    "url": it.get("URL") or f"https://doi.org/{doi}",
                })
            return papers

        try:
            return await anyio.to_thread.run_sync(_fetch)
        except Exception as e:
            raise RuntimeError(f"Crossref 检索失败: {e}")

    # ---------- Europe PMC 直连 ----------
    _EUPMC_API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

    async def _europepmc_search(self, query: str, limit: int) -> list:
        """直连 Europe PMC。id 按 source 字段区分：MED→pmid:、PMC→pmc:（补 PMC 前缀）、
        其他→europepmc:。pdf_url 从 fullTextUrlList 挑 documentStyle=pdf。
        query 空格=AND：长查询 0 命中时经 _relax_and 逐级砍尾词放宽（v2.9.1）。"""
        def _try(q):
            data = self._http_get(self._EUPMC_API,
                                  {"query": q, "format": "json",
                                   "pageSize": min(max(1, int(limit)), 100)},
                                  "Europe PMC").json()
            papers = []
            for it in (((data.get("resultList") or {}).get("result")) or []):
                if len(papers) >= limit:
                    break
                rid = str(it.get("id") or "")
                title = (it.get("title") or "").strip()
                if not rid or not title:
                    continue
                kind = it.get("source") or ""
                if kind == "MED":
                    pid = f"pmid:{rid}"
                elif kind == "PMC":
                    pmcid = rid if rid.startswith("PMC") else f"PMC{rid}"
                    pid = f"pmc:{pmcid}"
                else:
                    pmcid = ""
                    pid = f"europepmc:{rid}"
                authors = []
                al = (it.get("authorList") or {}).get("author") or []
                for a in al:
                    if isinstance(a, dict) and a.get("fullName"):
                        authors.append(a["fullName"])
                    elif isinstance(a, str):
                        authors.append(a)
                doi = it.get("doi") or ""
                y = str(it.get("pubYear") or "")
                mo, dy = str(it.get("pubMonth") or ""), str(it.get("pubDay") or "")
                date = y
                if y and mo.isdigit():
                    date = f"{y}-{mo.zfill(2)}" + (f"-{dy.zfill(2)}" if dy.isdigit() else "")
                landing, pdf_url = "", ""
                ftl = (it.get("fullTextUrlList") or {}).get("fullTextUrl") or []
                if isinstance(ftl, dict):
                    ftl = [ftl]
                for u in ftl:
                    if not isinstance(u, dict):
                        continue
                    style, uval = u.get("documentStyle"), (u.get("url") or "")
                    if style == "pdf" and not pdf_url:
                        pdf_url = uval
                    elif style == "html" and not landing:
                        landing = uval
                if not landing:
                    if doi:
                        landing = f"https://doi.org/{doi}"
                    elif kind == "MED":
                        landing = f"https://pubmed.ncbi.nlm.nih.gov/{rid}/"
                    elif kind == "PMC":
                        landing = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/"
                papers.append({
                    "title": title,
                    "authors": "; ".join(authors),
                    "published_date": date,
                    "abstract": it.get("abstractText") or "",
                    "paper_id": pid,
                    "doi": doi,
                    "source": "europepmc",
                    "pdf_url": pdf_url,
                    "citations": 0,
                    "url": landing,
                })
            return papers

        try:
            return await anyio.to_thread.run_sync(lambda: _relax_and(query, _try))
        except Exception as e:
            raise RuntimeError(f"Europe PMC 检索失败: {e}")

    # ---------- CORE 直连 ----------
    _CORE_API = "https://api.core.ac.uk/v3/search/works"

    def _core_key(self, __user__=None) -> str:
        uv = __user__.get("valves") if __user__ else None
        return ((getattr(uv, "core_api_key", "") or "").strip()
                or (getattr(self.valves, "core_api_key", "") or "").strip())

    async def _core_search(self, query: str, limit: int, __user__=None) -> list:
        """直连 CORE v3。匿名可用但配额低；配 key 后 401/403 自动降级匿名重试一次。"""
        def _fetch():
            key = self._core_key(__user__)
            params = {"q": query, "limit": min(max(1, int(limit)), 100), "offset": 0}
            try:
                data = self._http_get(
                    self._CORE_API, params, "CORE",
                    headers={"Authorization": f"Bearer {key}"} if key else None).json()
            except RuntimeError as e:
                if key and ("HTTP 401" in str(e) or "HTTP 403" in str(e)):
                    data = self._http_get(self._CORE_API, params, "CORE").json()
                else:
                    raise
            papers = []
            for it in (data.get("results") or []):
                if len(papers) >= limit:
                    break
                cid = it.get("id")
                title = (it.get("title") or "").strip()
                if not cid or not title:
                    continue
                authors = []
                for a in (it.get("authors") or []):
                    nm = a.get("name", "") if isinstance(a, dict) else str(a)
                    if nm:
                        authors.append(nm)
                doi = it.get("doi") or ""
                pdf_url = ""
                dl = it.get("downloadUrl")
                if isinstance(dl, str) and dl.lower().endswith(".pdf"):
                    pdf_url = dl
                else:
                    for u in (it.get("fullTextUrls") or []):
                        if isinstance(u, str) and u.lower().endswith(".pdf"):
                            pdf_url = u
                            break
                papers.append({
                    "title": title,
                    "authors": "; ".join(authors),
                    "published_date": str(it.get("publishedDate") or "")[:10],
                    "abstract": it.get("abstract") or "",
                    "paper_id": f"core:{cid}",
                    "doi": doi,
                    "source": "core",
                    "pdf_url": pdf_url,
                    "citations": 0,
                    "url": it.get("url") or (f"https://doi.org/{doi}" if doi else ""),
                })
            return papers

        try:
            return await anyio.to_thread.run_sync(_fetch)
        except Exception as e:
            raise RuntimeError(f"CORE 检索失败: {e}")

    # ---------- bioRxiv / medRxiv 直连（学科浏览，非关键词检索）----------
    async def _rxiv_search(self, server: str, category: str, limit: int) -> list:
        """bioRxiv/medRxiv 直连（两站共用 api.biorxiv.org/details/{server}/...）。
        注意：这不是关键词检索——API 只支持按学科分类浏览近 30 天新论文；
        category 为空时返回全学科最新（噪声大，仅显式点名该源时才这么干）。"""
        from datetime import datetime, timedelta
        cat = (category or "").strip().lower().replace(" ", "_")
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

        def _fetch():
            data = self._http_get(
                f"https://api.biorxiv.org/details/{server}/{start}/{end}/0",
                {"category": cat} if cat else None, server).json()
            papers = []
            for it in (data.get("collection") or []):
                if len(papers) >= limit:
                    break
                doi = (it.get("doi") or "").strip()
                title = re.sub(r"\s+", " ", (it.get("title") or "").strip())
                if not doi or not title:
                    continue
                base = f"https://www.{server}.org/content/{doi}v{it.get('version') or '1'}"
                papers.append({
                    "title": title,
                    "authors": re.sub(r"\s*;\s*", "; ", it.get("authors") or ""),
                    "published_date": it.get("date") or "",
                    "abstract": it.get("abstract") or "",
                    "paper_id": f"{server}:{doi}",
                    "doi": doi,
                    "source": server,
                    "pdf_url": base + ".full.pdf",
                    "citations": 0,
                    "url": base,
                })
            return papers

        try:
            return await anyio.to_thread.run_sync(_fetch)
        except Exception as e:
            raise RuntimeError(f"{server} 检索失败: {e}")

    # ---------- IACR ePrint 直连（HTML 正则解析，无 JSON API）----------
    _IACR_API = "https://eprint.iacr.org/search"

    async def _iacr_search(self, query: str, limit: int) -> list:
        """直连 IACR ePrint 搜索页。页面用 Xapian 把自然语言查询词干化后 AND 组合，
        结果按 ID 倒序（最新优先）。没有 JSON API，只能解析 HTML——工具依赖只有
        requests/pymupdf/anyio（不引入 bs4），用正则提取固定 class 标记。"""
        import html as _html

        def _clean(s: str) -> str:
            return re.sub(r"\s+", " ",
                          _html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()

        def _fetch():
            r = self._http_get(self._IACR_API, {"q": query}, "IACR")
            papers = []
            for blk in r.text.split('<div class="mb-4">')[1:]:
                if len(papers) >= limit:
                    break
                m = re.search(r'class="paperlink" href="(/\d+/\d+)"[^>]*>([^<]+)</a>', blk)
                t = re.search(r"<strong>(.*?)</strong>", blk, re.S)
                if not m or not t:
                    continue
                pid = m.group(2).strip()  # 形如 2026/1892
                am = re.search(r'<span class="fst-italic">(.*?)</span>', blk, re.S)
                ab = re.search(r'<p class="mb-0 mt-1 search-abstract">(.*?)</p>', blk, re.S)
                dt = re.search(r"Last updated:\s*([\d-]+)", blk)
                authors = "; ".join(a.strip() for a in _clean(am.group(1)).split(",")
                                    if a.strip()) if am else ""
                papers.append({
                    "title": _clean(t.group(1)),
                    "authors": authors,
                    "published_date": dt.group(1) if dt else "",
                    "abstract": _clean(ab.group(1)) if ab else "",
                    "paper_id": f"iacr:{pid}",
                    "doi": "",
                    "source": "iacr",
                    "pdf_url": f"https://eprint.iacr.org/{pid}.pdf",
                    "citations": 0,
                    "url": f"https://eprint.iacr.org/{pid}",
                })
            return papers

        try:
            return await anyio.to_thread.run_sync(_fetch)
        except Exception as e:
            raise RuntimeError(f"IACR 检索失败: {e}")

    # ---------- Google Scholar 直连（v2.9.4）：firecrawl 首选 → Apify actor 兜底 ----------
    _SCHOLAR_ACTOR = "johnvc~google-scholar-api"

    def _apify_rotator_base(self) -> str:
        return (getattr(self.valves, "apify_rotator_base_url", "") or "").strip().rstrip("/")

    @staticmethod
    def _clean_md_text(s: str) -> str:
        """清 scholar markdown 文本：bold 标记换成空格再折叠（tavily 会吃掉 ** 两侧的
        原有空格，如 "models**with" → 若直接删 ** 会变 "modelswith"）。"""
        return re.sub(r"\s+", " ", re.sub(r"\*+", " ", s or "")).strip()

    @staticmethod
    def _parse_scholar_markdown(md: str, limit: int) -> list:
        """解析 firecrawl/tavily 抓回的 scholar 搜索页 markdown（2026-09 实测结构）：
        每条结果一个 ### [title](url) 块；标题下首个含年份+分隔符的行为出版信息行
        （作者部分两种形态：citations 用户链接 或 纯文本，逗号分隔）；之后到
        Save/Cited by 行动线之间是 snippet（可多行）；[Cited by N]、cluster= 版本链接、
        [\\[PDF\\] domain](pdf)（tavily 形态 [[PDF] domain]）可选。
        被拦（unusual traffic CAPTCHA 页）抛 _AntiBotBlocked；解析不到条目返回 []。"""
        if "unusual traffic" in (md or "").lower():
            raise _AntiBotBlocked("scholar 返回 unusual traffic 验证页（IP 被 Google 标记）")
        # 分隔符变体多：firecrawl 带 NBSP（"作者…\xa0- 期刊"）、tavily 无空格（"作者…- 期刊"），
        # NBSP 归一化后统一用 \s*[-–]\s 类正则切
        md = (md or "").replace("\xa0", " ")
        papers = []
        for blk in re.split(r"\n###\s+", "\n" + md)[1:]:
            if len(papers) >= limit:
                break
            # 标题链接：行首可带 [PDF]/[HTML] 字面标签（转义形式 \[HTML\]）
            m = re.match(r"\s*(?:\\?\[(?:PDF|HTML)\\?\]\s*)*\[([^\]]+)\]\((https?://[^)\s]+)\)", blk)
            if not m:
                continue
            title = Tools._clean_md_text(m.group(1))
            url = m.group(2)
            if not title or "scholar.google.com" in url:
                continue  # 页眉/页脚导航块
            authors = year = ""
            snip_lines = []
            info_seen = False
            for line in (l.strip() for l in blk.split("\n")[1:]):
                if not line:
                    continue
                if not info_seen:
                    if re.search(r"[-–]", line) and re.search(r"\b(?:19|20)\d{2}\b", line):
                        info_seen = True
                        # 切首个 dash 分隔（两侧空格可有可元）：authors - venue, year - domain
                        parts = re.split(r"\s*[-–]\s+|\s+[-–]\s*", line, maxsplit=1)
                        a = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", parts[0])  # 链接取文本
                        a = Tools._clean_md_text(a).rstrip("…").strip()
                        authors = "; ".join(x.strip().rstrip(",")
                                            for x in a.split(",") if x.strip())
                        rest = parts[1] if len(parts) > 1 else line
                        ym = re.search(r"\b(?:19|20)\d{2}\b", rest)
                        year = ym.group(0) if ym else ""
                    continue
                if re.match(r"\[?Save|\[Cited by", line):
                    break  # 行动线：SaveCite / [Save](..) [Cite](..) [Cited by N](..)
                snip_lines.append(line)
            cm = re.search(r"Cited by (\d+)", blk)
            cl = re.search(r"scholar\?cluster=(\d+)", blk)
            pm = re.search(r"\[\\?\[\\?PDF\\?\][^\]]*\]\((https?://[^)\s]+)\)", blk)
            papers.append({
                "title": title,
                "authors": authors,
                "published_date": year,
                "abstract": Tools._clean_md_text(" ".join(snip_lines)),
                "paper_id": f"scholar:{cl.group(1)}" if cl else "",
                "doi": "",
                "source": "google_scholar",
                "pdf_url": pm.group(1) if pm else "",
                "citations": int(cm.group(1)) if cm else 0,
                "url": url,
            })
        return papers

    async def _google_scholar_firecrawl_search(self, query: str, limit: int, __user__=None) -> list:
        """firecrawl 抓 scholar 搜索页（官方云 stealth 出口，2026-09 实测 2.3s 穿透
        无 CAPTCHA；自托管实例看服务器 IP 运气）。CAPTCHA 页抛 _AntiBotBlocked 交外层落 actor。"""
        base = self._firecrawl_base(__user__)
        if not base:
            raise RuntimeError("未配 firecrawl_base_url")
        from urllib.parse import urlencode
        url = f"https://scholar.google.com/scholar?{urlencode({'q': query, 'hl': 'en', 'num': max(1, min(int(limit), 20))})}"
        raw = await anyio.to_thread.run_sync(
            self._mcp_call_service_url, base, "firecrawl_scrape",
            {"url": url, "formats": ["markdown"], "onlyMainContent": False}, 90)
        return self._parse_scholar_markdown((raw or {}).get("markdown", ""), limit)

    async def _google_scholar_tavily_search(self, query: str, limit: int, __user__=None) -> list:
        """tavily /extract（extract_depth=advanced）抓 scholar 搜索页（v2.9.5 实测：
        basic 只出标题+链接，advanced 出完整结构；便宜但会吃掉 ** 两侧空格，
        由 _parse_scholar_markdown 的 _clean_md_text 修复）。"""
        base = self._tavily_base(__user__)
        if not base:
            raise RuntimeError("未配 tavily_base_url")
        from urllib.parse import urlencode
        url = f"https://scholar.google.com/scholar?{urlencode({'q': query, 'hl': 'en', 'num': max(1, min(int(limit), 20))})}"

        def _extract():
            try:
                resp = requests.post(f"{base}/extract",
                                     json={"urls": [url], "extract_depth": "advanced",
                                           "format": "markdown"},
                                     headers={"Content-Type": "application/json"},
                                     timeout=90)
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.Timeout:
                raise RuntimeError("tavily extract 超时 (90s)")
            except requests.exceptions.RequestException as e:
                raise RuntimeError(f"tavily extract 失败: {e}")

        data = await anyio.to_thread.run_sync(_extract)
        results = (data or {}).get("results") or []
        if not results:
            fr = (data or {}).get("failed_results") or []
            why = (fr[0].get("error") if fr and isinstance(fr[0], dict) else "") or "无结果"
            raise RuntimeError(f"tavily extract 未取到内容: {why}")
        return self._parse_scholar_markdown(str(results[0].get("raw_content") or ""), limit)

    async def _google_scholar_actor_search(self, query: str, limit: int) -> list:
        """经 api-key-rotator 转发调 Apify google-scholar actor（PAY_PER_EVENT；
        免费层可用但结果数/字段受限，见返回项 _tier_notice）。Scholar 的 CAPTCHA/IP
        封锁由 actor 侧解决。同步端点 run-sync-get-dataset-items，最坏 ~120s。"""
        base = self._apify_rotator_base()
        if not base:
            raise RuntimeError("未配 apify_rotator_base_url")

        def _fetch():
            r = requests.post(
                f"{base}/v2/acts/{self._SCHOLAR_ACTOR}/run-sync-get-dataset-items",
                json={"q": query, "maxResults": max(1, min(int(limit), 20)),
                      "mode": "search"},
                headers={"Content-Type": "application/json"},
                timeout=150,
            )
            if r.status_code not in (200, 201):
                raise RuntimeError(f"scholar actor HTTP {r.status_code}")
            return r.json()

        try:
            items = await anyio.to_thread.run_sync(_fetch)
        except Exception as e:
            raise RuntimeError(f"Google Scholar(actor) 检索失败: {e}")

        papers = []
        for it in (items or []):
            if not isinstance(it, dict) or it.get("error"):
                continue
            title = str(it.get("paper_title") or "").strip()
            if not title:
                continue
            pub = it.get("publication_info") or {}
            authors = "; ".join(str(a.get("name") or "").strip()
                                for a in (pub.get("authors") or [])
                                if isinstance(a, dict) and a.get("name"))
            summary = str(pub.get("summary") or "")
            ym = re.search(r"\b(19|20)\d{2}\b", summary)
            links = it.get("inline_links") or {}
            rid = str(it.get("result_id") or "")
            papers.append({
                "title": title,
                "authors": authors,
                "published_date": ym.group(0) if ym else "",
                "abstract": str(it.get("snippet") or "").strip(),
                "paper_id": f"scholar:{rid}" if rid else "",
                "doi": "",
                "source": "google_scholar",
                "pdf_url": "",
                "citations": int(links.get("cited_by_total") or 0),
                "url": str(it.get("link") or ""),
            })
            if len(papers) >= limit:
                break
        return papers

    # ---------- web 搜索兜底（tavily 优先，firecrawl 备选；配了 base_url 才启用）----------
    _FC_NET_ERR_MARKERS = (
        "超时", "timed out", "timeout", "ssl", "eof", "connection", "refused",
        "reset", "unreachable", "dns", "502", "503", "504", "429",
    )
    # 源 → 学术域名映射（fallback 的 include_domains 动态限定用）。
    # 每个源对应其官方/主站点域名；未映射的源不参与域名限定（fallback 用全量域名）。
    _SOURCE_TO_DOMAINS = {
        "arxiv": ["arxiv.org"],
        "biorxiv": ["biorxiv.org"],
        "medrxiv": ["medrxiv.org"],
        "iacr": ["eprint.iacr.org"],
        "semantic": ["semanticscholar.org"],
        "crossref": ["doi.org"],
        "openalex": ["openalex.org"],
        "pubmed": ["pubmed.ncbi.nlm.nih.gov"],
        "pmc": ["ncbi.nlm.nih.gov"],
        "europepmc": ["europepmc.org"],
        "core": ["core.ac.uk"],
        "openaire": ["explore.openaire.eu"],
        "doaj": ["doaj.org"],
        "hal": ["hal.science"],
        "zenodo": ["zenodo.org"],
        "dblp": ["dblp.org"],
        "ieee": ["ieeexplore.ieee.org"],
        "google_scholar": ["scholar.google.com"],
        "zhihuiya": ["zhihuiya.com"],
        "ssrn": ["ssrn.com"],
        "base": ["base-search.net"],
        "acm": ["dl.acm.org"],
    }
    # 全量学术域名（无源映射信息时的兜底集合）
    _ACADEMIC_DOMAINS = sorted({d for ds in _SOURCE_TO_DOMAINS.values() for d in ds} | {
        "aclanthology.org", "openreview.net", "link.springer.com",
        "sciencedirect.com", "nature.com",
    })

    def _fallback_domains(self, failed: list, zero: list) -> list:
        """根据失败/0结果的源动态算出 include_domains。
        "backend" 是聚合错误（后端 search_papers 整批失败），展开为主要后端源的域名。
        有映射的源 → 其域名并集；全部无映射 → 全量 _ACADEMIC_DOMAINS。"""
        srcs = list(dict.fromkeys((failed or []) + (zero or [])))  # 去重保序
        domains = []
        for s in srcs:
            if s == "backend":
                # 后端批量失败 → 展开为主要后端学术源的域名（arxiv/semantic/pubmed 等）
                domains.extend(self._ACADEMIC_DOMAINS)
                continue
            domains.extend(self._SOURCE_TO_DOMAINS.get(s, []))
        domains = sorted(set(domains))
        return domains or self._ACADEMIC_DOMAINS

    def _net_failed_sources(self, errors: dict) -> list:
        """从 errors dict 挑出连接/超时类失败的源（0 命中/400 参数错不算）。"""
        out = []
        for src, msg in (errors or {}).items():
            m = str(msg).lower()
            if any(k in m for k in self._FC_NET_ERR_MARKERS):
                out.append(src)
        return out

    def _web_fallback_backend(self, __user__=None) -> str:
        """fallback 专用后端：只认 tavily（配了 tavily_base_url 才返回 "tavily"，否则 ""）。
        firecrawl 是独立源（want_firecrawl 控制），不做 fallback——它没有域名限定，
        作为主链源参与检索更合适；tavily 有 include_domains 学术限定，做兜底更精准。"""
        uv = __user__.get("valves") if __user__ else None
        tavily = (getattr(uv, "tavily_base_url", "") or "").strip() if uv else ""
        tavily = tavily or (getattr(self.valves, "tavily_base_url", "") or "").strip()
        return "tavily" if tavily else ""

    def _tavily_base(self, __user__=None) -> str:
        uv = __user__.get("valves") if __user__ else None
        u = (getattr(uv, "tavily_base_url", "") or "").strip() if uv else ""
        return (u or (getattr(self.valves, "tavily_base_url", "") or "").strip()).rstrip("/")

    def _firecrawl_base(self, __user__=None) -> str:
        uv = __user__.get("valves") if __user__ else None
        u = (getattr(uv, "firecrawl_base_url", "") or "").strip() if uv else ""
        return (u or (getattr(self.valves, "firecrawl_base_url", "") or "").strip()).rstrip("/")

    async def _web_search_fallback(self, query: str, limit: int, __user__=None, domains: list = None) -> tuple:
        """web 搜索兜底链：tavily 主 → firecrawl 备。
        domains 由 _fallback_domains 根据失败/0结果源动态限定；为 None 时用全量学术域名。
        返回 (backend_name, papers)，backend_name 用于 source 标注。"""
        domains = domains or self._ACADEMIC_DOMAINS
        if self._web_fallback_backend(__user__) == "tavily":
            try:
                papers = await self._tavily_search_papers(query, limit, __user__, domains)
                if papers:
                    return "tavily", papers
            except Exception:
                pass  # tavily 失败 → 落 firecrawl
        if self._firecrawl_base(__user__):
            papers = await self._firecrawl_search_domains(query, limit, __user__, domains)
            if papers:
                return "firecrawl", papers
        return "", []

    def _tavily_call(self, body: dict, __user__=None, timeout: int = 60):
        """POST tavily /search（经 api-key-rotator 代理，key 池轮转）。"""
        base = self._tavily_base(__user__)
        try:
            resp = requests.post(f"{base}/search", json=body,
                                 headers={"Content-Type": "application/json"},
                                 timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            raise RuntimeError(f"tavily 搜索超时 ({timeout}s)")
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"tavily 搜索失败: {e}")

    async def _tavily_search_papers(self, query: str, limit: int, __user__=None, domains: list = None) -> list:
        """tavily /search，include_domains 限定学术站点，返回结构化 paper dict。"""
        data = await anyio.to_thread.run_sync(
            self._tavily_call,
            {"query": query, "max_results": max(1, min(int(limit), 10)),
             "search_depth": "advanced", "include_domains": domains or self._ACADEMIC_DOMAINS},
            __user__, 60)
        papers = []
        for r in (data or {}).get("results") or []:
            if not isinstance(r, dict):
                continue
            title = str(r.get("title") or "").strip()
            url = str(r.get("url") or "")
            if not title or not url:
                continue
            # raw_content 里常有 doi（arXiv/IEEE/出版社页面），有则回填
            raw = str(r.get("raw_content") or "")
            doi = ""
            m = re.search(r"\bdoi[.:]\s*(10\.\d{4,9}/[^\s\"<>]+)", raw, re.I) or \
                re.search(r"doi\.org/(10\.\d{4,9}/[^\s\"<>]+)", raw)
            if m:
                doi = m.group(1).rstrip(".,;)")
            papers.append({
                "title": title,
                "authors": "",
                "published_date": str(r.get("published_date") or ""),
                "abstract": str(r.get("content") or "").strip(),
                "paper_id": self._paper_id_from_url(url),
                "doi": doi,
                "source": "tavily",
                "pdf_url": "",
                "citations": 0,
                "url": url,
            })
        # 不在此逐篇 inspect 补 authors/year/doi（N 次调用易限流/计费）；
        # tavily 有 url，元数据补全挪到 read_paper 按需
        return papers

    @staticmethod
    def _paper_id_from_url(url: str) -> str:
        """从学术 URL 提取 paper_id。arXiv/ieee/pubmed/aclanthology/springer/doi 优先。"""
        m = re.search(r"arxiv\.org/(?:abs|html|pdf)/([0-9]{4}\.[0-9]{4,5})", url)
        if m:
            return f"arxiv:{m.group(1)}"
        m = re.search(r"ieeexplore\.ieee\.org/document/(\d+)", url)
        if m:
            return f"ieee:{m.group(1)}"
        m = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", url)
        if m:
            return f"pubmed:{m.group(1)}"
        m = re.search(r"aclanthology\.org/([A-Za-z0-9.\-]+?)(?:\.pdf)?/?$", url)
        if m and not m.group(1).lower() in ("", "anthology"):
            return f"aclanthology:{m.group(1)}"
        m = re.search(r"doi\.org/(10\.\d{4,9}/[^\s?#]+)", url)
        if m:
            return f"doi:{m.group(1)}"
        m = re.search(r"biorxiv\.org/content/(10\.\d{4,9}/[^\s?#]+?)(?:\.full)?(?:\.pdf)?$", url)
        if m:
            return f"biorxiv:{m.group(1)}"
        return f"web:{url[-60:]}"

    def _papers_base(self) -> str:
        """papers 服务 base URL（papers_service_url，缺省 http://papers-service:3200/papers）。
        顺带兼容旧 valve 值仍指向 mcpo 的情况（mcpo_url 时代遗留配置）。"""
        return (getattr(self.valves, "papers_service_url", "") or "").strip().rstrip("/")

    def _mcp_call_service(self, service: str, tool: str, args: dict, timeout: int = 90):
        """调 mcpo 上非 papers 的服务（firecrawl 网关）。base 从 firecrawl_base_url 取。"""
        base = self._firecrawl_base()
        if not base:
            raise RuntimeError("未配 firecrawl_base_url")
        base = base.rstrip("/")
        if base.endswith("/firecrawl"):
            base = base[: -len("/firecrawl")]
        return self._mcp_call_service_url(f"{base}/{service}", tool, args, timeout)

    def _mcp_call_service_url(self, base_url: str, tool: str, args: dict, timeout: int = 90):
        """调指定 base URL 的 mcpo 工具端点：POST {base_url}/{tool}。"""
        headers = {}
        if self.valves.mcpo_api_key:
            headers["Authorization"] = f"Bearer {self.valves.mcpo_api_key}"
        try:
            resp = requests.post(f"{base_url.rstrip('/')}/{tool}", json=args,
                                 headers=headers, timeout=timeout)
            resp.raise_for_status()
            try:
                data = resp.json()
            except ValueError:
                return resp.text
            if isinstance(data, dict) and set(data) == {"result"}:
                return data["result"]
            return data
        except requests.exceptions.Timeout:
            raise RuntimeError(f"mcpo {base_url}/{tool} 超时 ({timeout}s)")
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"mcpo {base_url}/{tool} 失败: {e}")

    async def _firecrawl_search_domains(self, query: str, limit: int, __user__=None, domains: list = None) -> list:
        """firecrawl_search（通用 web 搜索）+ includeDomains 硬过滤学术站，fallback 用。
        实测（2026-08）：includeDomains 是硬过滤（5 条全在限定域名内）；site: 操作符返回空不可用。
        返回干净 JSON（url/title/description/position），description 含摘要/著录片段。"""
        base = self._firecrawl_base(__user__)
        raw = await anyio.to_thread.run_sync(
            self._mcp_call_service_url, base, "firecrawl_search",
            {"query": query, "limit": max(1, min(int(limit), 10)),
             "includeDomains": domains or self._ACADEMIC_DOMAINS}, 60)
        data = raw if isinstance(raw, dict) else {}
        web = (data.get("data") or {}).get("web") or []
        papers = []
        for w in web[:limit]:
            if not isinstance(w, dict):
                continue
            title = str(w.get("title") or "").strip()
            url = str(w.get("url") or "")
            if not title or not url:
                continue
            desc = str(w.get("description") or "").strip()
            # description 常含 "# Title:" 前缀/markdown 残留，去噪
            desc = re.sub(r"^#\s*Title:\s*", "", desc)
            desc = re.sub(r"\s+", " ", desc)
            papers.append({
                "title": title,
                "authors": "",
                "published_date": "",
                "abstract": desc,
                "paper_id": self._paper_id_from_url(url),
                "doi": "",
                "source": "firecrawl",
                "pdf_url": "",
                "citations": 0,
                "url": url,
            })
        return papers

    async def _research_inspect(self, paper_id: str, __user__=None) -> dict:
        """firecrawl_research_inspect_paper：按 paperId 取完整元数据（authors/dates/doi/ids）。
        用于富化 firecrawl/tavily 结果（它们 search 只回 title/abstract/id）。解析
        fmtPaperMetadata 的 Markdown：'IDs: doi:.., pmid:..'、'Authors: a; b'、'Dates: created YYYY-MM-DD'。
        失败/无数据返回 {}。"""
        base = self._firecrawl_base(__user__)
        if not base or not paper_id or ":" not in paper_id:
            return {}
        try:
            raw = await anyio.to_thread.run_sync(
                self._mcp_call_service_url, base, "firecrawl_research_inspect_paper",
                {"paperId": paper_id}, 30)
        except Exception:
            return {}
        text = raw if isinstance(raw, str) else ""
        if not text or "not found" in text.lower():
            return {}
        out = {}
        m = re.search(r"^IDs?:\s*(.+)$", text, re.M)
        if m:
            dm = re.search(r"doi:(10\.\d{4,9}/[^\s,]+)", m.group(1))
            if dm:
                out["doi"] = dm.group(1).rstrip(".,;)")
        m = re.search(r"^Authors:\s*(.+)$", text, re.M)
        if m:
            out["authors"] = re.sub(r";\s*\+\d+ more$", "", m.group(1).strip())
        m = re.search(r"^Dates?:\s*(.+)$", text, re.M)
        if m:
            ym = re.search(r"(19|20)\d{2}", m.group(1))
            if ym:
                out["year"] = ym.group(0)
        return out

    async def _firecrawl_search_papers(self, query: str, limit: int, __user__=None) -> list:
        """firecrawl_research_search_papers 兜底：返回 Markdown 文本，解析成 paper dict。
        base URL 用 Valves/UserValves.firecrawl_base_url（配了才走到这里）。
        search 端点只回 title/abstract/id（abstract 上游预截断，无法用参数改）。
        不在此逐篇 inspect 补 authors/year/doi——N 篇即 N 次调用易触发限流/计费；
        元数据补全挪到 read_paper 按需（单篇一次 inspect）。"""
        base = self._firecrawl_base(__user__)
        raw = await anyio.to_thread.run_sync(
            self._mcp_call_service_url, base, "firecrawl_research_search_papers",
            {"query": query, "k": max(1, min(int(limit), 10))}, 90)
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        papers = []
        # 条目格式：## [arxiv:2010.03192] Title\nabstract...
        for m in re.finditer(r"##\s*\[([^\]]+)\]\s*(.+?)(?=\n##\s*\[|\Z)", text, re.S):
            pid, block = m.group(1).strip(), m.group(2)
            head, _, body = block.partition("\n")
            title = head.strip().rstrip("\\")
            if not title:
                continue
            papers.append({
                "title": title,
                "authors": "",
                "published_date": "",
                "abstract": body.strip(),
                "paper_id": pid if ":" in pid else f"firecrawl:{pid}",
                "doi": "",
                "source": "firecrawl",
                "pdf_url": "",
                "citations": 0,
                "url": "",
            })
        return papers

    def _papers_call(self, tool: str, args: dict, timeout: int = 180, _retried: bool = False):
        headers = {}
        if self.valves.mcpo_api_key:
            headers["Authorization"] = f"Bearer {self.valves.mcpo_api_key}"
        try:
            resp = requests.post(
                f"{self._papers_base()}/{tool}",
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
            # 实测（2026-08）：境外学术 API 在突发并发下会间歇性 SSL EOF/慢响应，
            # 偶发触发 180s 超时；同请求立即重试通常成功 → 超时才重试 1 次。
            # search_papers 幂等（只读检索），重试安全。
            if _retried:
                raise RuntimeError(f"papers 服务调用超时 ({timeout}s，已重试1次)")
            import time
            time.sleep(3)
            return self._papers_call(tool, args, timeout, _retried=True)
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"papers 服务请求失败: {e}")

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
        - 单源失败不影响整体（见返回的 errors 字段）；源连接错误自动触发 tavily→firecrawl 兜底
        :param query: 学术检索词，越具体越好（如 'CRISPR base editing off-target'）
        :param max_results_per_source: 每源条数（默认5）。调大会显著拖慢响应（后端多源并发，
            每源都要等最慢的那个），一般不建议超过10
        :param sources: 留空用默认；或逗号分隔子集。可选值见模块顶部【搜索源清单】，
            常用: arxiv, semantic, openalex, pubmed, pmc, core, europepmc, zhihuiya, ieee,
            firecrawl, dblp, hal, zenodo, openaire, doaj, iacr, crossref, google_scholar
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
            or "arxiv,semantic,openalex,pubmed,pmc,core,europepmc,google_scholar"
        )
        src_set = {s.strip().lower() for s in src.split(",") if s.strip()}
        all_mode = src.strip().lower() == "all"

        variants = _make_query_variants(query)
        original, core = variants["original"], variants["core"]

        # 各分支耗时诊断（用户可见，用于定位慢/超时源）
        import time as _time
        _t0 = _time.monotonic()
        _timings = {}  # 分支名 → 耗时秒（一级分支 + backend 的 sem/lit 子调用）

        async def _timed(name, coro_fn):
            """包裹一个分支函数，记录耗时。coro_fn 是返回协程的无参函数。"""
            t = _time.monotonic()
            try:
                return await coro_fn()
            finally:
                _timings[name] = round(_time.monotonic() - t, 1)

        zh_enabled, zh_key = self._zhihuiya_enabled_key(__user__)
        want_zh = zh_enabled and ("zhihuiya" in src_set or all_mode)
        want_hal = "hal" in src_set or all_mode
        want_dblp = "dblp" in src_set or all_mode
        want_zenodo = "zenodo" in src_set or all_mode
        want_openaire = "openaire" in src_set or all_mode
        want_pubmed = "pubmed" in src_set or all_mode
        want_pmc = "pmc" in src_set or all_mode
        want_arxiv = "arxiv" in src_set or all_mode
        want_semantic = "semantic" in src_set or all_mode
        want_openalex = "openalex" in src_set or all_mode
        want_crossref = "crossref" in src_set or all_mode
        want_europepmc = "europepmc" in src_set or all_mode
        want_core = "core" in src_set or all_mode
        want_iacr = "iacr" in src_set or all_mode
        # biorxiv/medrxiv 是学科浏览（非关键词检索）：显式点名才启用；
        # all 模式下不传分类就是全学科噪声，故 all 需带对应 category 才启用
        want_biorxiv = "biorxiv" in src_set or (all_mode and bool(biorxiv_category))
        want_medrxiv = "medrxiv" in src_set or (all_mode and bool(medrxiv_category))
        # firecrawl 是独立源：配了 firecrawl_base_url 且在 sources 里才启用
        want_firecrawl = bool(self._firecrawl_base(__user__)) and ("firecrawl" in src_set or all_mode)
        ieee_enabled, ieee_key = self._ieee_enabled_key(__user__)
        want_ieee = ieee_enabled and ("ieee" in src_set or all_mode)
        # google_scholar 直连链（v2.9.5）：firecrawl 首选（官方云 stealth 出口，
        # 实测稳定穿透 CAPTCHA）→ tavily extract(advanced) 次选（便宜但会吃 ** 两侧
        # 空格，解析器已兼容）→ Apify actor 兜底（PAY_PER_EVENT 付费，放最后）→
        # 三个都没配才保持后端（后端 google_scholar.py 靠 GOOGLE_SCHOLAR_PROXY_URL 撞运气）
        _scholar_req = "google_scholar" in src_set or all_mode
        want_scholar_actor = bool(self._apify_rotator_base()) and _scholar_req
        want_scholar_fc = bool(self._firecrawl_base(__user__)) and _scholar_req
        want_scholar_tav = bool(self._tavily_base(__user__)) and _scholar_req
        want_scholar_direct = want_scholar_fc or want_scholar_tav or want_scholar_actor

        # 直连源不进后端 sources；scholar 有任一直接通路时也从后端剔除
        backend_set = src_set - DIRECT_SOURCES
        if want_scholar_direct:
            backend_set = backend_set - {"google_scholar"}
        if all_mode:
            backend_set = None  # None 表示后端用 _BACKEND_ALL_SOURCES（不含直连源）

        # 后端按变体分组：字面组用 core，语义组用 original
        # all_mode 下字面组固定为 LITERAL_SOURCES - DIRECT_SOURCES = {doaj}（zhihuiya 直连单独处理）
        backend_literal = (
            (LITERAL_SOURCES - DIRECT_SOURCES)
            if all_mode
            else ((src_set & LITERAL_SOURCES) - DIRECT_SOURCES)
        )
        # 字面源（doaj/zhihuiya）长术语查询需进一步按区分度截断到 5 词，否则 0 命中
        literal_query = _distill_core_terms(core, max_terms=5)

        # all_mode 下后端常量源列表：scholar 有直接通路时剔除
        _bk_all = ",".join(s for s in _BACKEND_ALL_SOURCES.split(",")
                           if not (want_scholar_direct and s == "google_scholar"))
        _sem_all = ",".join(s for s in _SEMANTIC_ALL_SOURCES.split(",")
                            if not (want_scholar_direct and s == "google_scholar"))

        async def _backend_all():
            # core==original 或无需拆分时，一次调用（含全部后端源）
            if not all_mode and not backend_set:
                # 只请了直连源（hal/zhihuiya/patsnap）→ 不调后端
                return {"papers": [], "source_results": {}, "errors": {}}
            t = _time.monotonic()
            try:
                args = {
                    "query": original,
                    "max_results_per_source": max_results_per_source,
                    "sources": (_bk_all if all_mode else ",".join(sorted(backend_set))),
                }
                if biorxiv_category:
                    args["biorxiv_category"] = biorxiv_category
                if medrxiv_category:
                    args["medrxiv_category"] = medrxiv_category
                return await anyio.to_thread.run_sync(self._papers_call, "search_papers", args)
            finally:
                _timings["backend_sem"] = round(_time.monotonic() - t, 1)

        async def _backend_split():
            # core!=original：语义组 original + 字面组 core 两次并发
            sem_set = (backend_set - LITERAL_SOURCES) if backend_set is not None else None
            tasks = []
            labels = []
            if sem_set is None or sem_set:
                async def _sem():
                    t = _time.monotonic()
                    try:
                        args = {"query": original,
                                "max_results_per_source": max_results_per_source,
                                "sources": (_sem_all if all_mode else ",".join(sorted(sem_set)))}
                        if biorxiv_category: args["biorxiv_category"] = biorxiv_category
                        if medrxiv_category: args["medrxiv_category"] = medrxiv_category
                        return await anyio.to_thread.run_sync(self._papers_call, "search_papers", args)
                    finally:
                        _timings["backend_sem"] = round(_time.monotonic() - t, 1)
                tasks.append(_sem()); labels.append("sem")
            if backend_literal:
                async def _lit():
                    t = _time.monotonic()
                    try:
                        return await anyio.to_thread.run_sync(
                            self._papers_call, "search_papers",
                            {"query": literal_query,
                             "max_results_per_source": max_results_per_source,
                             "sources": ",".join(sorted(backend_literal))})
                    finally:
                        _timings["backend_lit"] = round(_time.monotonic() - t, 1)
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
            return await self._dblp_search(original, max_results_per_source, __user__)

        async def _zenodo():
            return await self._zenodo_search(original, max_results_per_source, __user__)

        async def _openaire():
            return await self._openaire_search(original, max_results_per_source)

        async def _pubmed():
            return await self._pubmed_search(original, max_results_per_source, __user__)

        async def _pmc():
            return await self._pmc_search(original, max_results_per_source, __user__)

        async def _arxiv():
            # arxiv 字段布尔语法：自然语言整句全词 AND 会 0 命中，用 core 变体
            return await self._arxiv_search(core, max_results_per_source)

        async def _semantic():
            return await self._semantic_search(original, max_results_per_source, __user__)

        async def _openalex():
            return await self._openalex_search(original, max_results_per_source)

        async def _crossref():
            return await self._crossref_search(original, max_results_per_source)

        async def _europepmc():
            return await self._europepmc_search(original, max_results_per_source)

        async def _core():
            return await self._core_search(original, max_results_per_source, __user__)

        async def _biorxiv():
            return await self._rxiv_search("biorxiv", biorxiv_category, max_results_per_source)

        async def _medrxiv():
            return await self._rxiv_search("medrxiv", medrxiv_category, max_results_per_source)

        async def _iacr():
            return await self._iacr_search(original, max_results_per_source)

        async def _fc():
            # firecrawl 内部有查询处理，但保守起见用 core（去噪声词，保语义不截断）
            return await self._firecrawl_search_papers(core, max_results_per_source, __user__)

        async def _ieee():
            return await self._ieee_search(original, max_results_per_source, ieee_key)

        async def _gscholar():
            # 链式尝试（v2.9.5）：firecrawl → tavily(advanced) → Apify actor；
            # 前级被拦/失败/解析为空且有后级时自动下落；全失败抛最后一个异常
            attempts = []
            if want_scholar_fc:
                attempts.append(lambda: self._google_scholar_firecrawl_search(
                    original, max_results_per_source, __user__))
            if want_scholar_tav:
                attempts.append(lambda: self._google_scholar_tavily_search(
                    original, max_results_per_source, __user__))
            if want_scholar_actor:
                attempts.append(lambda: self._google_scholar_actor_search(
                    original, max_results_per_source))
            last_exc = None
            for i, fn in enumerate(attempts):
                try:
                    papers = await fn()
                    if papers or i == len(attempts) - 1:
                        return papers
                except Exception as e:
                    last_exc = e
            if last_exc is not None:
                raise last_exc
            return []

        # 组装并发分支（每个分支计时，写入 _timings）
        branches = {}
        branches["backend"] = _timed("backend", _backend_split if core != original else _backend_all)
        if want_zh:
            branches["zhihuiya"] = _timed("zhihuiya", _zh)
        if want_hal:
            branches["hal"] = _timed("hal", _hal)
        if want_dblp:
            branches["dblp"] = _timed("dblp", _dblp)
        if want_zenodo:
            branches["zenodo"] = _timed("zenodo", _zenodo)
        if want_openaire:
            branches["openaire"] = _timed("openaire", _openaire)
        if want_pubmed:
            branches["pubmed"] = _timed("pubmed", _pubmed)
        if want_pmc:
            branches["pmc"] = _timed("pmc", _pmc)
        if want_arxiv:
            branches["arxiv"] = _timed("arxiv", _arxiv)
        if want_semantic:
            branches["semantic"] = _timed("semantic", _semantic)
        if want_openalex:
            branches["openalex"] = _timed("openalex", _openalex)
        if want_crossref:
            branches["crossref"] = _timed("crossref", _crossref)
        if want_europepmc:
            branches["europepmc"] = _timed("europepmc", _europepmc)
        if want_core:
            branches["core"] = _timed("core", _core)
        if want_biorxiv:
            branches["biorxiv"] = _timed("biorxiv", _biorxiv)
        if want_medrxiv:
            branches["medrxiv"] = _timed("medrxiv", _medrxiv)
        if want_iacr:
            branches["iacr"] = _timed("iacr", _iacr)
        if want_firecrawl:
            branches["firecrawl"] = _timed("firecrawl", _fc)
        if want_ieee:
            branches["ieee"] = _timed("ieee", _ieee)
        if want_scholar_direct:
            branches["google_scholar"] = _timed("google_scholar", _gscholar)

        keys = list(branches)
        results = await asyncio.gather(*branches.values(), return_exceptions=True)
        outcome = dict(zip(keys, results))

        backend_result = outcome.get("backend")
        zh_result = outcome.get("zhihuiya")
        hal_result = outcome.get("hal")
        dblp_result = outcome.get("dblp")
        zenodo_result = outcome.get("zenodo")
        openaire_result = outcome.get("openaire")
        firecrawl_result = outcome.get("firecrawl")
        ieee_result = outcome.get("ieee")
        pubmed_result = outcome.get("pubmed")
        pmc_result = outcome.get("pmc")
        arxiv_result = outcome.get("arxiv")
        semantic_result = outcome.get("semantic")
        openalex_result = outcome.get("openalex")
        crossref_result = outcome.get("crossref")
        europepmc_result = outcome.get("europepmc")
        core_result = outcome.get("core")
        biorxiv_result = outcome.get("biorxiv")
        medrxiv_result = outcome.get("medrxiv")
        iacr_result = outcome.get("iacr")
        gscholar_result = outcome.get("google_scholar")

        # 后端失败处理：若任一直连源有结果则保留，否则报错
        direct_ok = [r for r in (zh_result, hal_result, dblp_result, zenodo_result, openaire_result, firecrawl_result, ieee_result, pubmed_result, pmc_result, arxiv_result, semantic_result, openalex_result, crossref_result, europepmc_result, core_result, biorxiv_result, medrxiv_result, iacr_result, gscholar_result) if isinstance(r, list) and r]
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
        if want_openaire:
            if isinstance(openaire_result, Exception):
                source_results["openaire"] = 0
                errors["openaire"] = str(openaire_result)
            elif openaire_result is not None:
                op = [self._trim_paper(p) for p in openaire_result]
                papers.extend(op)
                source_results["openaire"] = len(op)
        if want_pubmed:
            if isinstance(pubmed_result, Exception):
                source_results["pubmed"] = 0
                errors["pubmed"] = str(pubmed_result)
            elif pubmed_result is not None:
                pp = [self._trim_paper(p) for p in pubmed_result]
                papers.extend(pp)
                source_results["pubmed"] = len(pp)
        if want_pmc:
            if isinstance(pmc_result, Exception):
                source_results["pmc"] = 0
                errors["pmc"] = str(pmc_result)
            elif pmc_result is not None:
                mp = [self._trim_paper(p) for p in pmc_result]
                papers.extend(mp)
                source_results["pmc"] = len(mp)
        if want_arxiv:
            if isinstance(arxiv_result, Exception):
                source_results["arxiv"] = 0
                errors["arxiv"] = str(arxiv_result)
            elif arxiv_result is not None:
                ap = [self._trim_paper(p) for p in arxiv_result]
                papers.extend(ap)
                source_results["arxiv"] = len(ap)
        if want_semantic:
            if isinstance(semantic_result, Exception):
                source_results["semantic"] = 0
                errors["semantic"] = str(semantic_result)
            elif semantic_result is not None:
                sp = [self._trim_paper(p) for p in semantic_result]
                papers.extend(sp)
                source_results["semantic"] = len(sp)
        if want_openalex:
            if isinstance(openalex_result, Exception):
                source_results["openalex"] = 0
                errors["openalex"] = str(openalex_result)
            elif openalex_result is not None:
                oap = [self._trim_paper(p) for p in openalex_result]
                papers.extend(oap)
                source_results["openalex"] = len(oap)
        if want_crossref:
            if isinstance(crossref_result, Exception):
                source_results["crossref"] = 0
                errors["crossref"] = str(crossref_result)
            elif crossref_result is not None:
                crp = [self._trim_paper(p) for p in crossref_result]
                papers.extend(crp)
                source_results["crossref"] = len(crp)
        if want_europepmc:
            if isinstance(europepmc_result, Exception):
                source_results["europepmc"] = 0
                errors["europepmc"] = str(europepmc_result)
            elif europepmc_result is not None:
                eup = [self._trim_paper(p) for p in europepmc_result]
                papers.extend(eup)
                source_results["europepmc"] = len(eup)
        if want_core:
            if isinstance(core_result, Exception):
                source_results["core"] = 0
                errors["core"] = str(core_result)
            elif core_result is not None:
                cop = [self._trim_paper(p) for p in core_result]
                papers.extend(cop)
                source_results["core"] = len(cop)
        if want_biorxiv:
            if isinstance(biorxiv_result, Exception):
                source_results["biorxiv"] = 0
                errors["biorxiv"] = str(biorxiv_result)
            elif biorxiv_result is not None:
                brp = [self._trim_paper(p) for p in biorxiv_result]
                papers.extend(brp)
                source_results["biorxiv"] = len(brp)
        if want_medrxiv:
            if isinstance(medrxiv_result, Exception):
                source_results["medrxiv"] = 0
                errors["medrxiv"] = str(medrxiv_result)
            elif medrxiv_result is not None:
                mrp = [self._trim_paper(p) for p in medrxiv_result]
                papers.extend(mrp)
                source_results["medrxiv"] = len(mrp)
        if want_iacr:
            if isinstance(iacr_result, Exception):
                source_results["iacr"] = 0
                errors["iacr"] = str(iacr_result)
            elif iacr_result is not None:
                iap = [self._trim_paper(p) for p in iacr_result]
                papers.extend(iap)
                source_results["iacr"] = len(iap)
        if want_firecrawl:
            if isinstance(firecrawl_result, Exception):
                source_results["firecrawl"] = 0
                errors["firecrawl"] = str(firecrawl_result)
            elif firecrawl_result is not None:
                fp = [self._trim_paper(p) for p in firecrawl_result]
                papers.extend(fp)
                source_results["firecrawl"] = len(fp)
        if want_ieee:
            if isinstance(ieee_result, Exception):
                source_results["ieee"] = 0
                errors["ieee"] = str(ieee_result)
            elif ieee_result is not None:
                ip = [self._trim_paper(p) for p in ieee_result]
                papers.extend(ip)
                source_results["ieee"] = len(ip)
        if want_scholar_direct:
            # 直接通路（firecrawl/actor）时覆盖后端同名字段（后端本轮未请 scholar）
            if isinstance(gscholar_result, Exception):
                source_results["google_scholar"] = 0
                errors["google_scholar"] = str(gscholar_result)
            elif gscholar_result is not None:
                gp = [self._trim_paper(p) for p in gscholar_result]
                papers.extend(gp)
                source_results["google_scholar"] = len(gp)

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

        # ---- 各分支耗时诊断（用户可见）：定位慢/超时源，决定后续优化 ----
        _total_s = round(_time.monotonic() - _t0, 1)
        _timed_srcs = sorted(_timings.items(), key=lambda kv: -kv[1])
        out["diagnostics"] = {
            "total_seconds": _total_s,
            "branch_seconds": dict(_timed_srcs),  # 各分支耗时（降序）
            "slowest_branch": _timed_srcs[0][0] if _timed_srcs else "",
            "slowest_seconds": _timed_srcs[0][1] if _timed_srcs else 0,
        }

        # ---- web 搜索兜底链：tavily 主 → firecrawl 备：
        #      触发：任一请求的源出现连接/超时类错误（0 命中/400 参数错不算）。
        #      域名限定：动态映射自 失败源 ∪ 0结果源（_fallback_domains），非一刀切全量。
        #      返回量：N(1+K/3) 上限 min(3N, 20)，K=失败源数（backend 聚合按 4 计），
        #              避免大面积失败时补位量不足或拉太多。----
        failed_net = self._net_failed_sources(errors)
        if failed_net and (self._web_fallback_backend(__user__) or self._firecrawl_base(__user__)):
            zero_srcs = [s for s, n in source_results.items()
                         if n == 0 and s not in ("tavily", "firecrawl")]
            fb_domains = self._fallback_domains(failed_net, zero_srcs)
            # K：失败源数；"backend" 是聚合错误（内含多源），按 4 计
            k = sum(4 if s == "backend" else 1 for s in failed_net)
            n = max_results_per_source
            fb_limit = min(int(round(n * (1 + k / 3))), min(3 * n, 20))
            try:
                fb_name, wp = await self._web_search_fallback(
                    original, fb_limit, __user__, fb_domains)
                if wp:
                    papers.extend(wp)
                    source_results[fb_name] = len(wp)
                    out["total"] = len(papers)
                    out["source_results"] = source_results
                    out["papers"] = papers
                    out["fallback_domains"] = fb_domains  # LLM/用户可见：补位限定了哪些站点
                    out["fallback_limit"] = fb_limit      # LLM/用户可见：补位请求了多少条
            except Exception as e:
                errors["web_fallback"] = f"web 兜底失败: {str(e)[:200]}"
                out["errors"] = errors

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

    _PREFIX_TO_SOURCE = {
        "arxiv": "arxiv", "doi": "crossref", "pubmed": "pubmed", "pmid": "pubmed",
        "pmc": "pmc", "biorxiv": "biorxiv", "medrxiv": "medrxiv", "hal": "hal",
        "zenodo": "zenodo", "ieee": "ieee", "dblp": "dblp", "openaire": "openaire",
        "aclanthology": "web", "web": "web",
    }

    def _normalize_read_source(self, source: str, paper_id: str) -> tuple:
        """web 源（firecrawl/tavily）的 paper_id 保留了原始前缀（如 arxiv:xxx、pmid:xxx），
        据此还原到对应源再处理；返回 (规范源, 规范 paper_id)。"""
        src = (source or "").strip().lower()
        pid = (paper_id or "").strip()
        if src in ("firecrawl", "tavily") and ":" in pid:
            prefix, _, rest = pid.partition(":")
            mapped = self._PREFIX_TO_SOURCE.get(prefix.lower())
            if mapped and rest:
                return mapped, rest
        return src, pid

    async def read_paper(
        self,
        source: str,
        paper_id: str = "",
        pdf_url: str = "",
        url: str = "",
        max_chars: int = 25000,
        __user__={},
    ) -> str:
        """
        阅读论文全文（截断到 max_chars）。**务必把 search 结果的 pdf_url 和 url 都传上**——
        多数源无后端全文工具，需靠 fallback 链自动提取；url 是出版商落地页时抓取质量最高。
        - 后端直接可读: arxiv, biorxiv, medrxiv, iacr, semantic, doaj, hal
        - 其余源（openalex/crossref/pmc/core/europepmc/google_scholar/pubmed 等）:
          后端无全文或仅元数据 → 自动走 fallback 链：pdf_url 下载 PDF → url 出版商落地页
          抓取 → 推导落地页(doi/pmid) → Unpaywall OA → jina/tavily/firecrawl，全程无需人工干预
        - google_scholar: search 结果的 url 通常是出版商链接（nature/cell/science 等），
          务必传 url；仅 paper_id(gs_xxx) 时无法反推，会报错
        - zhihuiya: 元数据级 read（abstract+著录），全文请用 doi 走 download_paper_to_knowledge
        - tavily/firecrawl: web 搜索结果，paper_id 含原始前缀（arxiv:xxx/pmid:xxx）时自动按对应源处理
        :param source: search 结果的 source 字段
        :param paper_id: search 结果的 paper_id 字段
        :param pdf_url: search 结果的 pdf_url 字段（有就必须传，PDF 提取是最高质量 fallback）
        :param url: search 结果的 url 字段（出版商落地页，有就传；jina/firecrawl 可直接抓取）
        :param max_chars: 最大返回字符数
        """
        try:
            max_chars = int(max_chars)
        except (TypeError, ValueError):
            max_chars = 25000

        raw_pid_orig = (paper_id or "").strip()  # 规范化前的原始 id（含 pmid:/arxiv: 前缀）
        src, paper_id = self._normalize_read_source(source, paper_id)
        backend_tool = self._READ_TOOLS.get(src)
        backend_err = ""

        # ---- web 源（firecrawl/tavily）按需 inspect：单篇一次调用补著录+doi ----
        # search 阶段不逐篇 inspect（N 次调用易限流/计费）；read 单篇时一次 inspect 划算：
        # 补 authors/year 头部 + doi（供 Unpaywall/OA 链定位全文）。仅 web 源且原 id 可 inspect 时用。
        web_meta_header = ""
        web_doi = ""
        if (source or "").strip().lower() in ("firecrawl", "tavily") and ":" in raw_pid_orig:
            meta = await self._research_inspect(raw_pid_orig, __user__)
            if meta:
                parts = []
                if meta.get("authors"):
                    parts.append(f"Authors: {meta['authors']}")
                if meta.get("year"):
                    parts.append(f"Year: {meta['year']}")
                if parts:
                    web_meta_header = "[" + " | ".join(parts) + "]\n\n"
                web_doi = meta.get("doi", "")

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
            # 直连源的 paper_id 带自家前缀（arxiv:1706.03762 / semantic:hash /
            # biorxiv:10.1101/... / iacr:2026/1892），后端 read 工具只要裸 id
            call_pid = paper_id
            if ":" in call_pid:
                _pfx, _, _rest = call_pid.partition(":")
                if _pfx.lower() == src and _rest:
                    call_pid = _rest
            try:
                text = await anyio.to_thread.run_sync(
                    self._papers_call, backend_tool, {"paper_id": call_pid}, 300
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
                backend_err = f"{backend_err}；PDF 提取为空（扫描版图片 PDF）".strip("；")
            except Exception as e:
                backend_err = f"{backend_err}；PDF fallback 失败: {e}".strip("；")

        # ---- 网页抓取 fallback：出版商 url → 落地页 → Unpaywall OA PDF → jina/tavily/firecrawl ----
        # 对无后端全文工具 / 后端返回"不支持"提示 / PDF 付费墙的源自动兜底，不返回死路错误。
        # 优先用 search 结果传入的出版商 url（google_scholar/其他源的 url 多为出版商链接，质量最高）。
        if url and url.startswith("http"):
            ch, web = await self._web_read_fallback(url, __user__)
            if web:
                note = f"[经 {ch} 网页抓取全文：{url}]"
                return f"{note}\n\n" + web[:max_chars] + (
                    "\n\n[…全文截断…]" if len(web) > max_chars else ""
                )
            backend_err = f"{backend_err}；出版商 url 抓取失败".strip("；")

        landing = ""
        doi = web_doi  # web 源 inspect 补到的 doi 优先（供 Unpaywall/OA 链）
        if src == "crossref" and paper_id:
            doi = doi or paper_id.lstrip("doi:")
        elif src == "openalex" and paper_id:
            landing = f"https://openalex.org/{paper_id}"
        elif src == "pubmed" and paper_id:
            landing = f"https://pubmed.ncbi.nlm.nih.gov/{paper_id}/"
        elif src == "pmc" and paper_id:
            landing = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{paper_id}/"
        elif src == "web" and paper_id:
            landing = paper_id if paper_id.startswith("http") else ""

        # Unpaywall：有 DOI 就查 OA PDF 直链（pdf_url 缺失或已失败都回退到这里）
        if doi:
            oa = await self._resolve_oa_pdf(doi)
            if oa and oa != pdf_url:  # 与已失败的 pdf_url 不同才重试
                oa_text = ""
                try:
                    def _fetch_oa():
                        r = requests.get(oa, timeout=120, headers={"User-Agent": "Mozilla/5.0"})
                        r.raise_for_status()
                        if not r.content.startswith(b"%PDF"):
                            raise RuntimeError("OA 链接非 PDF")
                        return self._pdf_to_text(r.content)
                    oa_text = await anyio.to_thread.run_sync(_fetch_oa)
                except Exception:
                    oa_text = ""
                if oa_text:
                    return oa_text[:max_chars] + (
                        "\n\n[…全文截断…]" if len(oa_text) > max_chars else ""
                    )
                # OA 链接是 HTML 落地页（如 sciencedirect /pdf 反爬）→ 网页抓取它
                ch, oa_web = await self._web_read_fallback(oa, __user__)
                if oa_web:
                    note = f"[经 {ch} 网页抓取 OA 全文：{oa}]"
                    return f"{web_meta_header}{note}\n\n" + oa_web[:max_chars] + (
                        "\n\n[…全文截断…]" if len(oa_web) > max_chars else ""
                    )

        # 网页抓取三级链（jina → tavily → firecrawl），目标 URL：落地页或 doi.org 跳转
        if not landing and doi:
            landing = f"https://doi.org/{doi}"
        if landing:
            channel, text = await self._web_read_fallback(landing, __user__)
            if text:
                note = f"[经 {channel} 网页抓取全文：{landing}]"
                return f"{web_meta_header}{note}\n\n" + text[:max_chars] + (
                    "\n\n[…全文截断…]" if len(text) > max_chars else ""
                )
            backend_err = f"{backend_err}；网页抓取（jina/tavily/firecrawl）失败".strip("；")

        # google_scholar 仅 paper_id(gs_xxx，hash 不可反推) 且无 url 时的专属引导
        if src == "google_scholar" and not url and not pdf_url:
            return json.dumps(
                {
                    "error": "google_scholar 全文需要出版商落地页 URL（paper_id 是 Scholar 内部 hash，无法反推）",
                    "hint": "请把该条 search_papers 结果的 url 字段（出版商链接）作为 url 参数传入重试；"
                            "若 search 结果 url 为空（Scholar 限流降级），改用 download_paper_to_knowledge 走 OA fallback 链",
                },
                ensure_ascii=False,
            )

        return json.dumps(
            {
                "error": backend_err or "无可用全文途径",
                "hint": "请从 search_papers 结果中同时传入 pdf_url（和 url）重试；或 download_paper_to_knowledge 走完整 OA fallback 链",
            },
            ensure_ascii=False,
        )

    # ---------- 网页全文抓取 fallback（read_paper 用）----------
    # 三级链：jina reader（keyless 20RPM/有 key 500RPM，无需配置即可用，对付费墙页面常能
    # 拿到摘要/全文）→ tavily /extract（配 tavily_base_url 才启用）→ firecrawl_scrape
    # （配 firecrawl_base_url 才启用，onlyMainContent 去噪）。借鉴 reach-mcp 的 jina.py。
    def _jina_key(self, __user__=None) -> str:
        uv = __user__.get("valves") if __user__ else None
        user_key = (getattr(uv, "jina_api_key", "") or "").strip() if uv else ""
        return user_key or (getattr(self.valves, "jina_api_key", "") or "").strip()

    async def _jina_read(self, url: str, __user__=None, timeout: int = 45) -> str:
        """r.jina.ai 网页转 markdown 文本。失败/过短返回 ""。"""
        key = self._jina_key(__user__)
        def _fetch():
            headers = {"Accept": "text/plain", "X-Retain-Images": "none"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            r = requests.get(f"https://r.jina.ai/{url}", headers=headers, timeout=timeout)
            if r.status_code != 200:
                return ""
            return r.text or ""
        try:
            text = await anyio.to_thread.run_sync(_fetch)
        except Exception:
            return ""
        return text.strip()

    async def _tavily_extract_read(self, url: str, __user__=None, timeout: int = 45) -> str:
        """tavily /extract 抓单 URL，返回 results[0].raw_content。失败返回 ""。"""
        base = self._tavily_base(__user__)
        if not base:
            return ""
        def _fetch():
            r = requests.post(
                f"{base}/extract", json={"urls": [url]},
                headers={"Content-Type": "application/json"}, timeout=timeout,
            )
            if r.status_code != 200:
                return ""
            return r.json()
        try:
            data = await anyio.to_thread.run_sync(_fetch)
        except Exception:
            return ""
        for item in (data or {}).get("results") or []:
            if isinstance(item, dict) and item.get("raw_content"):
                return str(item["raw_content"]).strip()
        return ""

    async def _firecrawl_scrape_read(self, url: str, __user__=None, timeout: int = 60) -> str:
        """firecrawl_scrape 抓单 URL（formats=["markdown"]），返回 data.markdown。失败返回 ""。
        proxy="auto"：正常页走 basic（1 credit），遇 Cloudflare 挑战页自动升级 Enhanced
        反爬管道（5 credits/页）救回——只在需要时付费，不盲目全开。"""
        base = self._firecrawl_base(__user__)
        if not base:
            return ""
        def _fetch():
            return self._mcp_call_service_url(
                base, "firecrawl_scrape",
                {"url": url, "formats": ["markdown"], "onlyMainContent": True, "proxy": "auto"},
                timeout,
            )
        try:
            data = await anyio.to_thread.run_sync(_fetch)
        except Exception:
            return ""
        if isinstance(data, dict):
            md = (data.get("data") or {}).get("markdown") or data.get("markdown") or ""
            return str(md).strip()
        return str(data).strip() if isinstance(data, str) else ""

    # 抓取占位/反爬挑战页特征（命中视为失败，继续下一级）
    _WEB_JUNK_MARKERS = (
        "just a moment", "checking your browser", "verify you are human",
        "attention required", "cloudflare", "access denied", "request blocked",
        "are you a robot", "captcha",
    )

    def _is_web_junk(self, text: str) -> bool:
        """抓取结果是否为反爬挑战页/占位页（短或含特征词）。"""
        if not text:
            return True
        t = text.strip().lower()
        if len(t) < 500:
            return True
        head = t[:1500]  # 挑战页特征词都在开头
        return any(m in head for m in self._WEB_JUNK_MARKERS)

    async def _web_read_fallback(self, url: str, __user__=None) -> tuple:
        """三级网页抓取链。返回 (渠道名, 文本)；全失败返回 ("", "")。"""
        if not url or not url.startswith("http"):
            return "", ""
        text = await self._jina_read(url, __user__)
        if not self._is_web_junk(text):
            return "jina", text
        text = await self._tavily_extract_read(url, __user__)
        if not self._is_web_junk(text):
            return "tavily", text
        text = await self._firecrawl_scrape_read(url, __user__)
        if not self._is_web_junk(text):
            return "firecrawl", text
        return "", ""

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
                        # v2.9.6 身份闸：核对全文与标题匹配，防 OA 链下错文档
                        self._verify_downloaded_pdf(r.content, title, f"pdf_url({pdf_url[:80]})")
                        return self._upload_pdf(r.content, title, knowledge_id, __request__)
                    return None

                res = await anyio.to_thread.run_sync(_direct_download)
                if res:
                    return res
            except Exception as gate_err:
                if "身份校验未通过" in str(gate_err):
                    return json.dumps(
                        {"error": str(gate_err),
                         "hint": "可改用 read_paper 先读摘要确认论文身份，或换 pdf_url/doi 重试"},
                        ensure_ascii=False,
                    )
                pass  # 其他错误静默落入 fallback 链

        # 路径2: OA 下载链端点（v2.9.7 默认自托管 papers-service：
        # native→仓储 openaire/core/europepmc/pmc→Unpaywall→可选Sci-Hub，
        # 服务端已过标题身份闸；留空回退 papers_service_url 的落盘共享卷模式）
        if not (source and paper_id) and not doi:
            return json.dumps(
                {
                    "error": "信息不足：需提供 (source+paper_id) 或 doi 或 pdf_url 至少一组",
                    "hint": "从 search_papers 结果中取这些字段",
                },
                ensure_ascii=False,
            )

        dl_endpoint = (self.valves.download_fallback_url or "").strip()
        if dl_endpoint:
            data, via = await self._download_via_endpoint(
                dl_endpoint, source, paper_id, doi, title, allow_scihub, scihub_url
            )
        else:
            data, via = await self._download_via_backend(
                source, paper_id, doi, title, allow_scihub, scihub_url
            )
        if data is None:
            return json.dumps(
                {
                    "error": "完整 fallback 链均未获取到 PDF",
                    "detail": (via or "")[:500],
                    "hint": "该文可能无 OA 版本；可告知用户手动获取，或检查 Sci-Hub 镜像可用性（也可在 Valves 设置 scihub_url 为可用镜像）",
                },
                ensure_ascii=False,
            )
        try:
            msg = await anyio.to_thread.run_sync(
                self._upload_pdf, data, title, knowledge_id, __request__
            )
            if "scihub" in via.lower():
                msg += "（来源：Sci-Hub fallback）"
            return msg
        except Exception as e:
            return json.dumps(
                {"error": "下载成功但上传到 OpenWebUI 失败", "detail": str(e)},
                ensure_ascii=False,
            )

    async def _download_via_endpoint(self, endpoint, source, paper_id, doi, title,
                                     use_scihub, scihub_url):
        """POST 自托管 papers-service /papers/download_with_fallback，返回 (bytes|None, via|错误串)。
        服务端身份闸已拦错文档（404 + attempts），这里不重复校验。"""
        def _f():
            headers = {}
            if self.valves.mcpo_api_key:
                headers["Authorization"] = f"Bearer {self.valves.mcpo_api_key}"
            resp = requests.post(
                endpoint,
                json={
                    "source": source or "crossref",
                    "paper_id": paper_id or doi or title,
                    "doi": doi,
                    "title": title,
                    "use_scihub": use_scihub,
                    "scihub_base_url": scihub_url,
                },
                headers=headers,
                timeout=180,
            )
            if resp.status_code == 200 and resp.content.startswith(b"%PDF"):
                return resp.content, resp.headers.get("X-Download-Via", "papers-service")
            try:
                detail = resp.json()
            except ValueError:
                detail = resp.text[:300]
            return None, f"http {resp.status_code}: {json.dumps(detail, ensure_ascii=False)[:400]}"
        try:
            return await anyio.to_thread.run_sync(_f)
        except Exception as e:
            return None, f"下载请求异常: {e}"

    async def _download_via_backend(self, source, paper_id, doi, title,
                                    use_scihub, scihub_url):
        """回退路径（download_fallback_url 留空时）：papers 服务的 download_with_fallback
        落盘共享卷模式（旧 mcpo/paper-search-mcp 形态；papers-service 字节直传不走此路）。"""
        try:
            result = await anyio.to_thread.run_sync(
                self._papers_call,
                "download_with_fallback",
                {
                    "source": source
                    or "crossref",  # crossref 必失败 → 直接进 OA fallback 链（有意为之）
                    "paper_id": paper_id or doi or title,
                    "doi": doi,
                    "title": title,
                    "save_path": self.valves.shared_download_dir,
                    "use_scihub": use_scihub,
                    "scihub_base_url": scihub_url,
                },
                600,
            )
        except Exception as e:
            return None, f"下载请求异常/超时: {e}"

        if isinstance(result, str) and result.endswith(".pdf"):
            local_path = result
            if not os.path.exists(local_path):
                candidate = os.path.join(
                    self.valves.shared_download_dir, os.path.basename(local_path)
                )
                if os.path.exists(candidate):
                    local_path = candidate
                else:
                    return None, f"后端报告下载成功但共享卷中找不到文件: {result}"
            try:
                with open(local_path, "rb") as f:
                    data = f.read()
                # v2.9.6 身份闸：落盘回退路径服务端无闸，本地补一道
                self._verify_downloaded_pdf(data, title, f"fallback 链({os.path.basename(local_path)})")
                try:
                    os.remove(local_path)  # 读回后清理，避免共享卷膨胀
                except OSError:
                    pass
                return data, result
            except Exception as e:
                if "身份校验未通过" in str(e):
                    try:
                        os.remove(local_path)  # 拒收的文档也不留落盘
                    except OSError:
                        pass
                return None, str(e)
        return None, str(result)[:500]
