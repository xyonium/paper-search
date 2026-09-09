#!/usr/bin/env python3
"""逐源实况冒烟：区分「网络波动/限流/反爬」与「代码 bug」。

对每个直连源发一个已知稳定的查询，报告 OK/FAIL + 命中数 + 耗时。
用法：
    python3 scripts/live_sources.py              # 全部免 key 源
    python3 scripts/live_sources.py hal dblp     # 只测指定源
    SEMANTIC_API_KEY=... CORE_API_KEY=... IEEE_APIKEY=... \\
        ZENODO_ACCESS_TOKEN=... ZHIHUIYA_APIKEY=... \\
        python3 scripts/live_sources.py          # 连带 key 源

退出码：全部 PASS 为 0，任一 FAIL 为 1。FAIL 若含「反爬/429/504/超时」字样，
多为环境问题（换网络/换时段/配 key）而非代码问题。
"""
import asyncio
import importlib.util
import os
import sys
import time

SPEC = importlib.util.spec_from_file_location(
    "tool", os.path.join(os.path.dirname(__file__), "..", "tool.py")
)
tool_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool_mod)


def _mk():
    t = tool_mod.Tools()
    v = t.valves
    # 从环境变量注入 key（不存在则保持空 → 对应源标记 SKIP）
    v.semantic_api_key = os.environ.get("SEMANTIC_API_KEY", "")
    v.core_api_key = os.environ.get("CORE_API_KEY", "")
    v.ieee_apikey = os.environ.get("IEEE_APIKEY", "")
    v.zenodo_access_token = os.environ.get("ZENODO_ACCESS_TOKEN", "")
    v.zhihuiya_apikey = os.environ.get("ZHIHUIYA_APIKEY", "")
    v.ncbi_api_key = os.environ.get("NCBI_API_KEY", "")
    v.firecrawl_base_url = os.environ.get("FIRECRAWL_BASE_URL", "")
    v.apify_rotator_base_url = os.environ.get("APIFY_ROTATOR_BASE_URL", "")
    return t


async def _scholar_live(t):
    """镜像 search_papers 的 scholar 三级链：firecrawl 首选，actor 兜底。"""
    if t.valves.firecrawl_base_url:
        try:
            papers = await t._google_scholar_firecrawl_search("graph neural network", 3)
            if papers or not t.valves.apify_rotator_base_url:
                return papers
        except Exception:
            if not t.valves.apify_rotator_base_url:
                raise
    return await t._google_scholar_actor_search("graph neural network", 3)


# (源名, 协程工厂(t)->list, 是否需要 key)
CASES = {
    # --- 免 key ---
    "arxiv":     (lambda t: t._arxiv_search("graph neural network", 3), False),
    "hal":       (lambda t: t._hal_search("deep learning", 3), False),
    "dblp":      (lambda t: t._dblp_search("zero knowledge proof", 3), False),
    "zenodo":    (lambda t: t._zenodo_search("machine learning", 3), False),
    "openaire":  (lambda t: t._openaire_search("climate change", 3), False),
    "pubmed":    (lambda t: t._pubmed_search("glucose biosensor", 3), False),
    "pmc":       (lambda t: t._pmc_search("glucose biosensor", 3), False),
    "semantic":  (lambda t: t._semantic_search("graph neural network", 3), False),
    "openalex":  (lambda t: t._openalex_search("graph neural network", 3), False),
    "crossref":  (lambda t: t._crossref_search("graph neural network", 3), False),
    "europepmc": (lambda t: t._europepmc_search("glucose biosensor", 3), False),
    "core":      (lambda t: t._core_search("machine learning", 3), False),
    "biorxiv":   (lambda t: t._rxiv_search("biorxiv", "bioinformatics", 3), False),
    "medrxiv":   (lambda t: t._rxiv_search("medrxiv", "epidemiology", 3), False),
    "iacr":      (lambda t: t._iacr_search("zero knowledge proof", 3), False),
    # --- 需 key（未配置则 SKIP） ---
    "ieee":      (lambda t: t._ieee_search("neural network", 3, t.valves.ieee_apikey), True),
    "zhihuiya":  (lambda t: t._zhihuiya_search("葡萄糖 传感器", 3, t.valves.zhihuiya_apikey), True),
    # google_scholar 三级链（v2.9.4）：FIRECRAWL_BASE_URL 首选 → APIFY_ROTATOR_BASE_URL 兜底
    "google_scholar": (_scholar_live, True),
}


async def run_one(name, factory):
    t = _mk()
    fn, needs_key = CASES[name]
    if needs_key:
        key_map = {"ieee": t.valves.ieee_apikey,
                   "zhihuiya": t.valves.zhihuiya_apikey,
                   "google_scholar": (t.valves.firecrawl_base_url
                                      or t.valves.apify_rotator_base_url)}
        if not key_map.get(name):
            return (name, "SKIP", 0, 0.0, "未配置 key（设环境变量后重跑）")
    start = time.monotonic()
    try:
        papers = await fn(t)
        dt = time.monotonic() - start
        status = "PASS" if papers else "EMPTY"
        return (name, status, len(papers), dt, "")
    except Exception as e:
        dt = time.monotonic() - start
        return (name, "FAIL", 0, dt, str(e)[:200])


async def main(names):
    names = names or list(CASES)
    unknown = [n for n in names if n not in CASES]
    if unknown:
        print(f"未知源: {unknown}；可选: {sorted(CASES)}")
        return 2
    # 逐源串行（避免并发触发限流，干扰诊断）
    results = []
    for n in names:
        r = await run_one(n, None)
        results.append(r)
        name, status, count, dt, err = r
        line = f"{name:<10} {status:<5} {count:>3} 篇  {dt:>5.1f}s"
        if err:
            line += f"  | {err}"
        print(line, flush=True)
    n_fail = sum(1 for r in results if r[1] == "FAIL")
    n_pass = sum(1 for r in results if r[1] == "PASS")
    n_empty = sum(1 for r in results if r[1] == "EMPTY")
    n_skip = sum(1 for r in results if r[1] == "SKIP")
    print(f"\n合计: {n_pass} PASS / {n_empty} EMPTY(0命中) / {n_fail} FAIL / {n_skip} SKIP")
    if n_fail:
        print("提示：FAIL 含 429=限流（配 key 或换时段）、504/超时=网络波动、"
              "反爬/非 JSON=IP 被拦（换网络）；这些都不是代码 bug。")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
