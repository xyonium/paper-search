# 设计文档：智慧芽 (zhihuiya) 科学文献源接入

日期：2026-08-04
状态：已获用户批准（①~⑦）

## 1. 背景与目标

为 paper-search 项目新增第 17 个文献检索源：智慧芽 (zhihuiya) 科学文献库。
该服务本身是 streamable-http MCP 端点，按 apikey 鉴权，**不配 key 则不启用**。

- 端点：`https://connect.zhihuiya.com/eba075/mcp?apikey=<KEY>`
- 可用工具：`search_literature`、`literature_bibliography`、`literature_journal`、
  `literature_author_affiliation`、`literature_citation`

## 2. 关键实测结论（已验证）

- **无 key 也能完成 MCP 握手**（HTTP 200），仅真正调工具时返回 401
  `{"error_code":67200008,"http_status":401,"message":"apikey not Pass and call failed!"}`。
- `search_literature` 必填 `type`（合法值 `title/abstract/author/publication/title/abstract/all`），
  返回 `data.results[]`，字段仅 `paper_id / doi / title[](list) / author[](list)`，
  **不返回 abstract / year / pdf_url**。
- `literature_journal` 返回的是**期刊元数据**（journal_name/issn/eissn/出版方/频率/地址等），
  **不是论文全文**，不能用于 read。
- `literature_bibliography` 返回**著录信息 + abstract**（title/author/publication/卷期页/
  publication_year/abstract[]），且 `paper_id` 支持**批量逗号分隔（≤100）**。
- open-webui 容器内已装 `mcp 1.27.2`，`mcp.client.streamable_http.streamablehttp_client` 可用
  （OpenWebUI 自身连 streamable-http MCP 即用此 SDK）。

## 3. 接入方式（方案 2：tool.py 直连）

不改动 mcpo `config.json`，不新增 mcpo 挂载。tool.py 作为编排层，用官方 `mcp` SDK
直连智慧芽，把它作为"需按请求带 key 的特例源"。mcpo 继续统一管理其余 16 个无 key 源。

**为什么不用 mcpo 挂载（方案 1）**：mcpo 的 OpenAPI 路由无状态，URL 锁死在 config 里，
不支持按请求注入/覆盖 apikey；若 key 不落 config 则方案 1 走不通，除非 fork mcpo，违背
"不改动 mcpo" 的约束。

## 4. 配置与启用条件

- `Tools.Valves.zhihuiya_apikey: str = ""` — 管理员配的公司 key（admin 级）
- `Tools.UserValves.zhihuiya_apikey: str = ""` — 用户个人 key，**非空时覆盖 admin key**
- `Tools.UserValves.zhihuiya_enabled: bool = True` — 用户级开关，False 时强制关闭

启用判定（在 search/read 入口）：

```python
key = (user_valves.zhihuiya_apikey or valves.zhihuiya_apikey).strip()
enabled = user_valves.zhihuiya_enabled and bool(key)
```

即：**任一 key 非空且开关打开**才启用；都不配 → 完全不发请求（真·不启用）。

## 5. search_papers 集成（search + bibliography 两步）

现有其它源经 `_trim_paper` 返回带 `abstract`（后端 `Paper.abstract`，semantic/core/zenodo
等均填充，截断 600 字符）。智慧芽 `search_literature` 不返回 abstract，为保持聚合结果
对称、让 LLM 能据此判断价值，zhihuiya 源采用两步：

```
search_papers(query, sources, max_results_per_source, __user__)
   ├─ anyio.to_thread → _mcp_call → POST mcp:8000/papers/search_papers   (现有16源)
   └─ asyncio task    → _zhihuiya_branch(query, limit, key)              (新增, 并发)
        1. search_literature(text=query, type='all', limit=N)
           → [{paper_id, doi, title[], author[]}]
        2. literature_bibliography(paper_id="id1,id2,...,idN")   (批量, 1次调用)
           → [{paper_id, abstract[], publication_year, publication, ...}]
        3. 按 paper_id 合并 → 每篇含 title/author/abstract/year/doi
        4. map 到统一字段，source='zhihuiya'
   → merge papers / source_results / errors → 统一 JSON 返回
```

字段映射（→ `_trim_paper` 期望）：

| 统一字段 | 智慧芽来源 |
|---|---|
| `title` | `title[].text` join（bibliography 多语言 title 取 EN 或 join） |
| `authors` | `author[]` → `"; "` join |
| `published_date` | `publication_year` 或 `publication_date` |
| `abstract` | `abstract[].text` join（EN 优先） |
| `paper_id` | `paper_id` |
| `doi` | `doi` |
| `source` | 固定 `"zhihuiya"` |
| `pdf_url` | 空（智慧芽不提供） |
| `citations` | 0（search 不返回；如需可走 `literature_citation`，本期不做） |

## 6. read_paper 集成

- `_READ_TOOLS["zhihuiya"]` **不走 mcpo**，由 tool.py 直连 `literature_bibliography` 实现
  "元数据级 read"（与 pubmed/crossref 同级）。
- 若返回 abstract 非空 → 返回 abstract + 提示"全文请用 doi 走 download_paper_to_knowledge
  的 OA fallback 链"；若空 → 命中 `_is_unsupported_msg` 逻辑降级。
- **真·全文**：依赖 `doi` 走 `download_paper_to_knowledge` 的 fallback 链
  （OA 仓储 → Unpaywall → 可选 Sci-Hub）。智慧芽本身不提供 pdf_url。
- `literature_journal` 只是期刊元数据，**不纳入 read**。

## 7. 错误处理

- 智慧芽分支独立 `asyncio.timeout` 兜底（如 30s），超时/401/网络错误/解析失败 →
  写入返回 `errors["zhihuiya"]`，`source_results["zhihuiya"]=0`，不影响其它 16 源。
- 未启用（无 key 或开关关闭）→ 不发请求，不出现在 errors。
- 原生 async（mcp SDK 的 streamablehttp_client 本身是 async），在 async `search_papers`
  里直接 `await`，杜绝"同步调用卡死事件循环"的历史问题。

## 8. 防并发/防卡死要点

- 不新增同步阻塞调用；智慧芽全程 async。
- 每次调用新建 MCP 会话（官方 SDK 标准用法），用 `async with` 确保会话关闭。
- zhihuiya 分支与其余源并发（asyncio.gather 或 task 并发），内部 search→bibliography
  为必要顺序依赖。

## 9. 测试

1. 用 `.env` 中 `ZHIHUIYA_API_KEY` 本地起 tool.py，调
   `search_papers("CRISPR base editing off-target", sources="arxiv,zhihuiya")`：
   - 断言 `source_results` 含 `zhihuiya` 且 >0
   - 断言 zhihuiya 条目 `title/authors/abstract/doi/paper_id` 非空、`source=="zhihuiya"`
2. key 置空或 `zhihuiya_enabled=False`：断言返回无 `zhihuiya`、未发请求。
3. `read_paper(source="zhihuiya", paper_id=<id>)`：断言返回含 abstract 或正确降级提示。
4. （可选）`literature_bibliography` 批量：传 2~3 个逗号分隔 id，断言一次调用全部返回。

## 10. 文档与发布（最后一项任务）

功能完成并验证后：
- 重写 `README.md`（有吸引力的项目介绍、架构图、源清单含 zhihuiya、配置说明）
- 撰写 GitHub About（repo description + topics）
- 确认 `LICENSE` 为 MIT（已存在）
- 推送到 GitHub 远程

## 11. 影响面

- **改动文件**：仅 `tool.py`（Valves/UserValves、`_zhihuiya_*` 直连、`_READ_TOOLS`、
  `search_papers`/`read_paper` 适配）；README.md；CLAUDE.md 源清单同步
- **不改**：mcpo `config.json`、docker-compose、后端 `paper-search-mcp`
- **依赖**：open-webui 容器已自带 `mcp 1.27.2`，无需新增
