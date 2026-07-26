"""
title: Academic Paper Search
description: |
  学术论文搜索、全文阅读、PDF 下载入 Knowledge（RAG）。
  后端: mcpo(config 模式, /papers) 桥接 paper-search-mcp，聚合 21+ 学术源，
  内置 OA fallback 下载链（源站 → OpenAIRE/CORE/EuropePMC/PMC → Unpaywall → 可选 Sci-Hub）。

  【搜索源清单】（search_papers 的 sources 参数可用值，'all' 为全部）：
  · 预印本/开放获取（可搜可读全文）: arxiv, biorxiv, medrxiv, iacr, pmc, europepmc
  · 综合索引: semantic (Semantic Scholar), openalex, crossref, pubmed, core,
    openaire, doaj, base, zenodo, hal, citeseerx, dblp (CS书目), unpaywall (仅DOI查询)
  · 不稳定源（反爬/间歇故障，失败自动降级不影响整体）: google_scholar, ssrn
  · 可选付费源（需在 mcpo env 配 key 才注册）: ieee, acm

  【工具用法】
  1. search_papers(query)      → 多源并发搜索+去重，返回标题/作者/摘要/引用数/pdf_url
  2. read_paper(source, paper_id, pdf_url) → 读全文（后端工具 + pdf_url 自动 fallback）
  3. download_paper_to_knowledge(...)      → PDF 下载并加入 Knowledge 知识库
author: openags-bridge
requirements: requests, pymupdf
version: 2.1.0
license: MIT
"""

import json
import os
import requests
from pydantic import BaseModel, Field


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

    class UserValves(BaseModel):
        default_sources: str = Field(
            default="arxiv,pubmed,biorxiv,medrxiv,iacr,semantic,crossref,openalex,pmc,core,europepmc,dblp,openaire,doaj,halc",
            description="默认搜索源：'all'=全部21源（慢，30s+）；或逗号分隔子集如 google_scholar,citeseerx,ssrn,base,ieee,zenodo,unpaywall",
        )
        knowledge_id: str = Field(
            default="", description="下载 PDF 自动加入的 Knowledge 集合 ID"
        )
        allow_scihub: bool = Field(
            default=True,
            description="允许 download fallback 链在 OA 源全失败后使用 Sci-Hub（法律风险自担）",
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
        "base": "read_base_paper",
        "zenodo": "read_zenodo_paper",
        "hal": "read_hal_paper",
        "openaire": "read_openaire_paper",
        "citeseerx": "read_citeseerx_paper",
        # ↓ 工具存在但设计上只返回"不支持"提示——尝试后检测降级
        "pubmed": "read_pubmed_paper",
        "crossref": "read_crossref_paper",
        "dblp": "read_dblp_paper",
        # ↓ 后端无 read 工具
        "pmc": None,
        "core": None,
        "europepmc": None,
        "openalex": None,
        "google_scholar": None,
        "ssrn": None,
        "unpaywall": None,
        # ↓ 可选付费源（配 key 后工具存在，未配时调用会 404，被异常处理兜住）
        "ieee": "read_ieee_paper",
        "acm": "read_acm_paper",
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
    def _mcp_call(self, tool: str, args: dict, timeout: int = 180):
        headers = {}
        if self.valves.mcpo_api_key:
            headers["Authorization"] = f"Bearer {self.valves.mcpo_api_key}"
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

    def _owui_headers(self, __request__=None) -> dict:
        token = None
        if __request__ is not None:
            token = __request__.headers.get("Authorization")
        if not token and self.valves.owui_api_key:
            token = f"Bearer {self.valves.owui_api_key}"
        if not token:
            raise RuntimeError("无法获取 OpenWebUI 凭证，请在 Valves 配置 owui_api_key")
        return {"Authorization": token}

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
        r = requests.post(
            f"{self.valves.openwebui_url.rstrip('/')}/api/v1/files/",
            headers=headers,
            files={"file": (f"{safe}.pdf", data, "application/pdf")},
            timeout=120,
        )
        r.raise_for_status()
        file_id = r.json()["id"]
        if knowledge_id:
            r = requests.post(
                f"{self.valves.openwebui_url.rstrip('/')}/api/v1/knowledge/{knowledge_id}/file/add",
                headers={**headers, "Content-Type": "application/json"},
                json={"file_id": file_id},
                timeout=60,
            )
            r.raise_for_status()
            return f"✅ 已下载《{title}》并加入 Knowledge（file_id: {file_id}），可被 RAG 检索引用。"
        return f"✅ 已下载《{title}》并上传（file_id: {file_id}）。未配置 knowledge_id，未入库。"

    # ---------- 暴露给 LLM ----------
    def search_papers(
        self,
        query: str,
        max_results_per_source: int = 5,
        sources: str = "",
        __user__={},
    ) -> str:
        """
        搜索学术论文：多源并发查询 + 去重，返回 title/authors/year/source/paper_id/doi/citations/pdf_url/abstract。
        - source+paper_id → read_paper 读全文；doi/pdf_url → download_paper_to_knowledge 入库
        - 单源失败不影响整体（见返回的 errors 字段）
        :param query: 学术检索词，越具体越好（如 'CRISPR base editing off-target'）
        :param max_results_per_source: 每源条数（默认5，勿调大）
        :param sources: 留空用默认（'all'）；或逗号分隔子集，可选值:
            arxiv, biorxiv, medrxiv, iacr, pmc, europepmc, semantic, openalex,
            crossref, pubmed, core, openaire, doaj, base, zenodo, hal, citeseerx,
            dblp, unpaywall, google_scholar, ssrn (+ieee, acm 若已配 key)
        """
        uv = __user__.get("valves") if __user__ else None
        src = (
            sources
            or (uv.default_sources if uv else None)
            or "arxiv,semantic,openalex,pubmed,pmc,core,europepmc"
        )
        result = self._mcp_call(
            "search_papers",
            {
                "query": query,
                "max_results_per_source": max_results_per_source,
                "sources": src,
            },
        )
        if not isinstance(result, dict):
            return json.dumps({"error": "backend 返回异常", "raw": str(result)[:500]})
        papers = [self._trim_paper(p) for p in result.get("papers", [])]
        return json.dumps(
            {
                "query": query,
                "total": len(papers),
                "source_results": result.get("source_results", {}),
                "errors": result.get("errors", {}),
                "papers": papers,
            },
            ensure_ascii=False,
            indent=2,
        )

    def read_paper(
        self,
        source: str,
        paper_id: str = "",
        pdf_url: str = "",
        max_chars: int = 25000,
        __user__={},
    ) -> str:
        """
        阅读论文全文（截断到 max_chars）。支持全部 21 个搜索源 + IEEE/ACM。
        - 后端直接可读: arxiv, biorxiv, medrxiv, iacr, semantic, doaj, base,
          zenodo, hal, openaire, citeseerx（ieee/acm 需配 key）
        - pubmed/crossref/dblp 后端仅返回元数据提示，会自动降级用 pdf_url 提取
        - pmc, core, europepmc, openalex, google_scholar, ssrn, unpaywall:
          请同时传 pdf_url，将自动下载提取全文
        :param source: search 结果的 source 字段
        :param paper_id: search 结果的 paper_id 字段
        :param pdf_url: search 结果的 pdf_url 字段（强烈建议总是提供，作 fallback）
        :param max_chars: 最大返回字符数
        """
        src = (source or "").strip().lower()
        backend_tool = self._READ_TOOLS.get(src)
        backend_err = ""

        if backend_tool and paper_id:
            try:
                text = self._mcp_call(backend_tool, {"paper_id": paper_id}, timeout=300)
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

        if pdf_url:
            try:
                r = requests.get(
                    pdf_url,
                    timeout=180,
                    headers={"User-Agent": "Mozilla/5.0"},
                    allow_redirects=True,
                )
                r.raise_for_status()
                if not r.content.startswith(b"%PDF"):
                    raise RuntimeError("返回非 PDF（可能付费墙页面）")
                text = self._pdf_to_text(r.content)
                if text:
                    return text[:max_chars] + (
                        "\n\n[…全文截断…]" if len(text) > max_chars else ""
                    )
                return json.dumps({"error": "PDF 提取为空（扫描版图片 PDF）"})
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

    def download_paper_to_knowledge(
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

        # 路径1: 直接 pdf_url 下载（最快，不依赖共享卷）
        if pdf_url:
            try:
                r = requests.get(
                    pdf_url,
                    timeout=180,
                    headers={"User-Agent": "Mozilla/5.0"},
                    allow_redirects=True,
                )
                r.raise_for_status()
                if r.content.startswith(b"%PDF"):
                    return self._upload_pdf(r.content, title, knowledge_id, __request__)
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

        result = self._mcp_call(
            "download_with_fallback",
            {
                "source": source
                or "crossref",  # crossref 必失败 → 直接进 OA fallback 链（有意为之）
                "paper_id": paper_id or doi or title,
                "doi": doi,
                "title": title,
                "save_path": self.valves.shared_download_dir,
                "use_scihub": allow_scihub,
            },
            timeout=600,
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
            with open(local_path, "rb") as f:
                data = f.read()
            try:
                os.remove(local_path)  # 上传后清理，避免共享卷膨胀
            except OSError:
                pass
            msg = self._upload_pdf(data, title, knowledge_id, __request__)
            if "scihub" in result.lower() or "sci-hub" in result.lower():
                msg += "（来源：Sci-Hub fallback）"
            return msg

        return json.dumps(
            {
                "error": "完整 fallback 链均未获取到 PDF",
                "detail": str(result)[:500],
                "hint": "该文可能无 OA 版本；可告知用户手动获取，或检查 Sci-Hub 镜像可用性",
            },
            ensure_ascii=False,
        )
