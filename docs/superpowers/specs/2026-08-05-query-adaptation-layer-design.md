# 设计文档：查询词适配层 + hal 直连 + biorxiv 学科参数

日期：2026-08-05
状态：已获用户批准（②④ 经实测修正，其余各节 OK）

## 1. 背景与目标

提升 `search_papers` 多源聚合的命中率。实测发现：不同源对查询格式容忍度差异巨大，
同一查询在不同源上表现从 0 到满额不等。目标是用**最少干预**实现**高命中率且不损语义**。

参考 reach-mcp `query_core.py` 的确定性按源适配思路（噪声词剥离，不用 LLM）。

## 2. 关键实测结论（真实环境 open-webui→mcpo 已验证）

### 各源对查询格式的容错（基准短词均通，排除"无文章"）

| 源 | 短关键词 | 引号短语 | 长自然语言 | 结论 |
|---|---|---|---|---|
| openalex/semantic/crossref/pmc/europepmc/pubmed/arxiv | ✅ | 容忍 | 容忍 | 语义源，原样传递 |
| openaire | 3 | 3 | **3** | 容错好 → 语义组 |
| core | 3 | 3 | **3** | 容错好 → 语义组 |
| **doaj** | 3 | 3 | **0** | 长自然语言 0，精简核心词→恢复3 → **字面组** |
| **iacr** | 3 | 3 | **0** | 长自然语言 0，精简核心词→恢复3 → **字面组** |
| **zhihuiya** | 3 | — | **0** | very_long 0，核心词→恢复 → **字面组**（直连） |
| patsnap | 3 | 3 | 3 | 真语义 → 原样（直连） |
| **dblp** | 3 | 3 | 3 | **无 bug**；之前全 0 是偶发 500 + 并发超时，不在适配层 |
| **hal** | 报错 | 报错 | 报错 | **真 bug**：后端 hal.py `published_date` 传 str，`Paper.to_dict` 调 `.isoformat()` 炸（`'str' object has no attribute 'isoformat'`）→ **直连绕过** |
| biorxiv/medrxiv | 5 | — | — | **语义误用**：把查询当学科分类名，返回"该分类近30天新论文"，非关键词检索 |

### 用户真实查询模式印证
- 引号短语 `"early signal drop"` → pubmed/pmc/core/europepmc/openalex 全 0；semantic/openaire 容忍
- 多词 + 裸露 `OR` → pubmed/pmc/core/europepmc/openalex 好；semantic/openaire 归零
- 没有一种查询对所有源最优 → 按源分发变体

## 3. 接入方式（全部在 tool.py 编排层，不改第三方后端）

后端 `search_papers(query, sources)` 是一个 query 打所有源，无法按源适配 → tool.py
把 sources 按变体分组，分别调后端再合并。zhihuiya/patsnap 本就在 tool.py 直连，各自适配；
hal 直连绕过有 bug 的后端。

## 4. 查询变体生成器 `_make_query_variants`（确定性）

```
_make_query_variants(query) -> {"original": str, "core": str}
  original = query（原样返回）
  core = query 经:
    - 去引号字符（"..."内容保留，只去引号本身）
    - 去裸露布尔运算符（独立成词的 OR/AND/NOT，参考 reach-mcp strip_boolean）
    - 去噪声词（问句前缀/元词，中英，参考 reach-mcp BASE_NOISE + CN_NOISE）
    - CJK 感知（中文字符按字计）
    - 不截断词数（论文术语都是有效限定，截断损精度）
```

**源分组**（实测驱动）：
- **语义组**（用 original）：openalex, semantic, crossref, pmc, europepmc, pubmed,
  arxiv, biorxiv, medrxiv, openaire, core, dblp, hal, patsnap
- **字面组**（用 core）：**zhihuiya, doaj, iacr**（仅此三源实测确认长自然语言会 0）
- `core == original`（查询本无引号/裸露布尔/噪声）时只调一次后端，零开销。

## 5. search_papers 数据流

```
search_papers(query, max_results_per_source, sources,
              biorxiv_category="", medrxiv_category="", __user__)
  ├─ variants = _make_query_variants(query)          # original / core
  ├─ 语义组 = sources ∩ 语义源（剔除 zhihuiya/hal/patsnap，它们直连）
  ├─ 字面组 = sources ∩ {doaj, iacr}                  # zhihuiya 直连单独处理；hal 直连
  │
  ├─ 分支1: 后端 search_papers(original, 语义组 + 字面组,        # core==original 时单调用
  │          biorxiv_category, medrxiv_category)                # 否则拆成 original/core 两次
  ├─ 分支2: zhihuiya 直连 _zhihuiya_search(core)      # 现有
  └─ 分支3: hal 直连 _hal_search(core)                # 新增
  → asyncio.gather 并发，合并 papers / source_results / errors
```

- 后端分组：core==original 时一次调用（语义组+字面组+dblp+core+openaire 全用 original）；
  不等时拆两次（语义组用 original、字面组{doaj,iacr}用 core），并发执行后合并。
- biorxiv_category/medrxiv_category：LLM 可选传入学科分类（如 "biochemistry"、"cell_biology"），
  传给后端（后端 biorxiv.py 的 query 正是 category 语义）；空则维持现状。
  docstring 列出可选学科 + 标注"biorxiv/medrxiv 返回该学科近30天新论文，非关键词检索"。

## 6. hal 直连 `_hal_search`

hal 是普通 HTTPS JSON API（非 MCP），用 `anyio.to_thread.run_sync` 包同步 requests
（线程池，不阻塞事件循环/网站），加超时，错误进 errors['hal']：

```
_hal_search(core_query, limit) -> [paper dict]
  GET https://api.archives-ouvertes.fr/search/
      ?q=<core>&fl=halId_s,title_s,authFullName_s,abstract_s,doiId_s,
         publicationDateY_i,producedDateY_i,submittedDate_s,fileMain_s,uri_s,docType_s
      &rows=<limit>&wt=json&sort=score desc   timeout=20
  → map 到 _trim_paper 字段:
      paper_id = "hal:" + halId_s
      title = title_s[0], authors = authFullName_s join "; "
      published_date = str(publicationDateY_i or producedDateY_i or submittedDate_s[:10])
      abstract = abstract_s, doi = doiId_s, pdf_url = fileMain_s, url = uri_s
      source = "hal", citations = 0
```

## 7. biorxiv/medrxiv 学科分类（参考列表）

bioRxiv 26 个学科（docstring 列出，空格转下划线传给后端）：
Animal Behavior and Cognition, Biochemistry, Bioengineering, Bioinformatics, Biophysics,
Cancer Biology, Cell Biology, Developmental Biology, Ecology, Evolutionary Biology, Genetics,
Genomics, Immunology, Microbiology, Molecular Biology, Neuroscience, Paleontology, Pathology,
Pharmacology and Toxicology, Physiology, Plant Biology, Scientific Communication and Education,
Synthetic Biology, Systems Biology, Zoology
medRxiv 学科不同（Cardiovascular Medicine, Epidemiology, Infectious Diseases 等），
docstring 给出常见示例 + 完整列表链接。

## 8. 错误处理

- 各后端分组、zhihuiya、hal 分支独立 `asyncio.wait_for` 超时，异常进 `errors[source]`，
  `source_results[source]=0`，不影响其它源。
- `_make_query_variants` 为纯函数无 I/O，不失败；core 为空时回退 original。
- hal 直连失败 → errors['hal']。
- 同步 requests（hal）一律 `anyio.to_thread.run_sync` 包线程池，**绝不阻塞事件循环/网站**。

## 9. 测试

1. `_make_query_variants`：引号短语、裸露 OR/AND/NOT、英文噪声词、中文噪声词、混合、
   纯术语（core==original）、全噪声回退 original。
2. 源分组：各源归对组（字面组={zhihuiya,doaj,iacr}）。
3. search_papers：core≠original 时拆两次后端调用并合并；core==original 时单调用；
   biorxiv_category 透传；zhihuiya/hal 直连走 core。
4. `_hal_search`：mock requests，字段 map 正确，错误隔离，published_date 为字符串年份。
5. 真实端点验证（open-webui→mcpo + 直连）：
   - doaj/iacr 用长自然语言查询从 0 → 有结果（经 core 变体）
   - hal 从 isoformat 报错 → 有结果
   - zhihuiya 长查询从 0 → 有结果

## 10. 影响面

- **改动文件**：仅 `tool.py`（`_make_query_variants`、源分组映射、`search_papers` 改造、
  `_hal_search`、biorxiv/medrxiv 学科参数、docstring）；`tests/test_zhihuiya.py` 追加
  （或新建 `tests/test_query_adapt.py`）；README/CLAUDE.md 同步。
- **不改**：mcpo config.json、docker-compose、第三方后端 paper-search-mcp。
- **依赖**：无新增（hal 用 requests，已装）。

## 11. 文档与发布

功能完成并验证后：更新 tool.py docstring（查询适配说明 + biorxiv 学科参数）、
README.md、CLAUDE.md（各源查询特性矩阵），版本号 2.4.0→2.5.0，合并 main 并推送。
