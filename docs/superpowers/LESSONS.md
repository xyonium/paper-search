# 改动前必读：这个项目的既定设计、上游硬限与"必须说出来"的边界

> 来源：2026-11-18 一轮"静默失败"审计（含两次误判）＋ `.superpowers/sdd/` 历史记录
> ＋ `docs/superpowers/specs/` 设计文档。目的只有一个：**别再把手艺当 bug 改坏**。
> 与 `CLAUDE.md` 冲突时，以代码 + 设计文档为准，并回来修正本文件。

## 0. 判断流程（顺序别跳）

1. 这行为在 `CLAUDE.md` 的 `vX.Y.Z` 条目或 `docs/superpowers/specs/` 里有解释吗？
   → 有 = 既定设计，改之前先问"为什么当初这么写"。
2. 代码里是否有对应分支/常量成员？（例：`DIRECT_SOURCES` 的每个成员都该有 `want_*` 分支）
   → 常量与实现不一致 = 真缺陷。
3. 症状是"少给了结果"还是"多给了/给了错的"？
   → **请求了却不告知**才必须回报；放宽/截断/兜底属既定策略。

## 1. 既定设计（不要当缺陷改）

| 行为 | 理由 | 出处 |
|---|---|---|
| AND 源逐级放宽：>6 词先蒸馏 → 取前 5 词 → 0 命中砍尾词 | hal/europepmc/pubmed/pmc/openaire/ieee 把空格当 AND；11 词自然语言实测全 0（hal 0→61、europepmc 0→55） | `CLAUDE.md:141`（v2.9.1）、`tool.py:191-206` |
| 字面源截断到 5 词（zhihuiya/doaj） | 长自然语言 0 命中；doaj 5 词是硬需求（3 词 11 命中 → 5 词 0 的实测） | `docs/superpowers/specs/2026-08-05-literal-source-term-distillation-design.md`、`CLAUDE.md:103-110` |
| biorxiv/medrxiv 忽略 query（近 30 天学科浏览） | 上游 API 只支持按学科分类浏览，非关键词检索；显式点名才启用 | `CLAUDE.md:128`、`:204`、`2026-08-05-biorxiv-medrxiv-repositioning-design.md` |
| key 门控源未配 key 时跳过 | "配 key 是必要非充分条件"；v2.9.11 起**显式请求时会回报**，默认路径只在 `notes` 汇总 | `CLAUDE.md:101`、`tool.py` 的 `KEY_GATED_SOURCES` |
| `_is_web_junk` 判废过短/反爬页（<500 字符或含 cloudflare 等） | 抓回来的是 "Just a moment…" 挑战页时不能当全文 | `tool.py` 的 `_WEB_JUNK_MARKERS` |
| 单源失败不影响整体 | 多源并发契约；失败进 `errors`，不抛 | `tool.py` search_papers docstring |
| 后端挂掉仍保留直连结果；bibliography 挂只降级为空 abstract（search 自身错误才传播） | 最小化错误边界，别因为补元数据失败丢掉已拿到的论文 | `.superpowers/sdd/final-fix-report.md` Fix 2/3 |
| 直连源不进后端 `sources` | 后端（自托管 papers-service）不含这些源；混发会被忽略或报错 | `tool.py` `DIRECT_SOURCES` 注释、`qa task-3-report.md:79` |

## 2. 上游硬限（改不了，只能文档化）

- 每源结果数：firecrawl research/search `min(limit,10)`、scholar actor 20、dblp/openaire 100、arxiv 100…（`tool.py:2019/2180/2280/2352`）
- firecrawl research 的 abstract 上游预截断 ~280 字符，请求参数改不了
- google_scholar 拿不到稳定 url；`gs_xxx` 是 hash 不可反推（read 必须传 url）
- 中文/长尾查询在 doaj 上是 Lucene AND 语义，词越多越窄（不是"词数上限"）
- 智慧芽端点约 50% 瞬时抖动（偶发空结果），错误已隔离进 `errors`（`progress.md` zhihuiya Task 7）

## 3. 必须"说出来"的静默失败

判据：调用方请求了 → 本轮没做 → 返回 `total=0 / errors={}` → LLM 误判"没有相关论文"。

已修（v2.9.11）：
- `patsnap` 在 `DIRECT_SOURCES` 但无分支（专利源误传）→ `ignored_sources` + 指引 `search_patents`
- 未知/拼错源名 → 校验并回报，且不再发给后端
- key 门控源显式点名但未配置 → 回报缺哪个 key/url
- 同 DOI 跨源重复（文档承诺"去重"却没实现）→ 按 DOI 精确去重 + `duplicates_removed`
- `download` 端点硬编码容器地址 → 跟 `papers_service_url`
- `read_paper` 的三级网页抓取全落空时，错误里说明原因（页面过短 <500 字符 / 被判为反爬挑战页），
  而不是笼统的"网页抓取失败"

评估后**不改**（记录在此，避免下次又被当缺陷）：
- `_is_web_junk` 的 <500 字符阈值：放松会放进挑战页，代价更大；只补了失败原因文案
- `_download_via_endpoint` 只给 doi 时 `source` 兜底成 `"crossref"`：v2.9.7 有意为之
  （后端 native 下载失败会落 OA 链，crossref 只是占位），改空值反而可能破坏后端契约

## 4. 接口漂移要冒泡（v2.9.11）

| 错误类别 | 处理 |
|---|---|
| 连接/限流（超时/timeout/ssl/eof/connection/502/503/504/429） | 走 tavily→firecrawl 兜底，**不告警** |
| 反爬 / 未实现 / "不支持" / 兜底服务自身失败 | **不告警**（已知类别，避免狼来了） |
| 其余（400/404/字段缺失/解析失败/返回结构变化/后端异常） | `admin_alert{sources, message}`，提示转告管理员核对 |

实现：`Tools._admin_drift_errors()`；`errors` 里的 `sem`/`lit` 子分支标签归并为 `backend`。
**不要把提示塞进 `errors`**：`errors` 会触发 web 兜底，且文案内插源名可能误命中连接类关键词。

## 5. 踩过的坑（血泪，别重犯）

1. **把既定设计当 bug**：`_relax_and` 放宽、biorxiv 学科浏览都被我标成缺陷，实际 `CLAUDE.md:141/128`
   写得很清楚。加运行时提示也是噪声，已回退。
2. **只看常量不看分支**：`patsnap` 在 `DIRECT_SOURCES` 里"看起来"是直连源，实际只在
   `backend_set` 的减法里存在 —— 于是 `sources=patsnap` 既不调后端也无分支，静默 0。
3. **计划文档会过期**：`2026-08-05-query-adaptation-layer-design.md:38` 写"patsnap 本就在
   tool.py 直连"（错）；同文档把 iacr 归入字面组，而 v2.6 已移出（`CLAUDE.md:110`）。照抄计划会踩坑。
4. **标题模糊去重会误杀**：预印本/正式版/勘误同题不同年，用"标题+年"合并会把不同条目吞掉 →
   改成**只按 DOI 精确匹配**，无 DOI 一律保留。
5. **默认路径别刷屏**：显式 `sources=` 才逐条回报；落回 valve 默认值时只在 `notes` 汇总一句。
6. **sentinel 会真的被调用**：`_READ_TOOLS` 里给 zhihuiya 放的假工具名曾击穿到 mcpo（300s 超时）
   并覆盖真实错误 → 加了 `src != "zhihuiya"` 守卫。给映射表放假值必须同时加调用侧守卫。
7. **改 mapper 的键要和 `_trim_paper` 的契约同步**：`_trim_paper` 只认 `published_date`，
   若源适配器给的是 `year`，字段会被静默丢掉（测试构造数据时尤其容易写错）。

## 6. 测试与提交惯例

- **RED → GREEN**：先写会失败的测试，再改代码（`final-fix-report.md` 每个 Fix 都记录了 RED/GREEN）
- 新测试要在基线代码上真的失败（本次 24 个新用例在 `bf87580` 上 18 个失败）
- **离线复现**：`requests` 全打桩并断言零网络调用；真实端点验证只在收尾跑
  `scripts/live_sources.py`（key 从环境变量注入）
- `pytest.ini` 用 `asyncio_mode=auto`
- 提交：`type(scope): 中文摘要` + 「改动」「验证」两段；`tool.py` 顶部 docstring 的
  `· vX.Y.Z：` 条目与 `version:` 同步
- 每条源分支的测试至少覆盖：有结果 / 抛异常 / 未启用三类
- 阈值与边界值必须测（历史 Minor 都出在这里：`timeout=60` 未断言、`max_chars=0` 下限）
- 实现要逐字对齐 brief；必须偏离时在报告里写 "necessary deviation" 及理由
- 错误消息先脱敏（apikey）再截断（`_redact_zhihuiya_key` 的顺序不能反）

## 7. 当前状态

- 分支 `fix/silent-source-reporting`（未推送）：`tool.py` + `README.md` + `CLAUDE.md` +
  `tests/test_sources_reporting.py`
- `.superpowers/sdd/` 是本地未跟踪记录（`.gitignore: *`），不会进仓库；要留档就往
  `docs/superpowers/` 写
