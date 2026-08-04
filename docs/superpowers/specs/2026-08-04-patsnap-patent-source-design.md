# 设计文档：patsnap（智慧芽）专利源接入

日期：2026-08-04
状态：已获用户批准（①②③④）

## 1. 背景与目标

为 paper-search 项目新增专利检索能力（第 18 个源，首个专利源）。patsnap 是智慧芽的
另一款 MCP 产品（专利+论文库），与已接入的科学文献源（eba075）同公司、同一把 apikey。

- 端点：`https://connect.zhihuiya.com/2b0355/logic-mcp?apikey=<KEY>`
- 工具：`patsnap_search`、`patsnap_fetch`
- **只接专利**（论文检索现有 17 源已覆盖，patsnap 论文仅比文献源多"作者机构"，无正文全文）。
- **独立工具**（不并入 search_papers 论文聚合；专利字段与论文不兼容）。

## 2. 关键实测结论（已验证）

- 与文献源**同一把 apikey**（ZHIHUIYA_API_KEY 即可用）。
- `patsnap_search`（source='patent'）返回 `data.docs[]`，字段：
  `id, patent_number, title, ipc, legal_status, application_date, publication_date,
  priority_date, granted_date, cited_count, url, view_url, jurisdiction, assignees[],
  inventors[], text(部分正文)`，及 `total_hits/returned_count`。
  - `search_strategy` 缺省时返回 hints（"semantic parameters were provided but
    search_strategy does not contain 'semantic'"）——用 semantic_query 时应显式传
    `search_strategy=['semantic']`。
- `patsnap_fetch`（key_type='pn'，module=['basic']）返回 **~91KB markdown**：
  著录项 + 权利要求 + 说明书 + 法律信息；`include_images=True` 可带附图 CDN 签名链接。
  - 对论文（source='paper' / paperId url）fetch 仅 ~5KB markdown（著录+abstract+作者机构），
    **无论文正文全文** —— 故不接 patsnap 论文。
- 无 pdf_url；专利"全文"即 fetch 的 markdown（优于 PDF），无需走 OA fallback 链。

## 3. 接入方式（复用 zhihuiya 直连机制）

不改 mcpo config，不新增 mcpo 挂载。复用现有 `_zhihuiya_call` 直连机制，仅扩展：
- 新增类常量 `_PATSNAP_MCP_URL = "https://connect.zhihuiya.com/2b0355/logic-mcp?apikey={key}"`
- `_zhihuiya_call` 增加可选 `url: str = None` 参数（None 时用 `_ZHIHUIYA_MCP_URL`），
  专利工具传 `_PATSNAP_MCP_URL`。

**为什么复用**：同一公司、同一把 key、同一 streamable-http MCP 协议；复用可继承
`_zhihuiya_enabled_key` 启用判定、`_redact_zhihuiya_key` 密钥脱敏、超时与错误隔离，
改动最小、风险最低、与现有架构一致。

## 4. 配置与启用条件

- **共用一把 key**：`Valves.zhihuiya_apikey`（admin）/ `UserValves.zhihuiya_apikey`（用户覆盖）。
- **共用一个开关**：`UserValves.zhihuiya_enabled`。
- 启用判定沿用 `_zhihuiya_enabled_key(__user__)`：任一 key 非空且开关为 True 才启用；
  否则 `search_patents`/`read_patent` 返回错误 JSON，不发请求。

## 5. 工具方法（新增 2 个，与现有 3 个并列）

### 5.1 `search_patents(query, limit=10, sort="relevance", filters=None, __user__={})`

```
启用检查（_zhihuiya_enabled_key）
  → _zhihuiya_call('patsnap_search',
        {semantic_query: query, search_strategy: ['semantic'],
         source: 'patent', limit: clamp(1..100), sort: sort, filters: filters or {}},
        key, url=_PATSNAP_MCP_URL)
  → 映射 data.docs[] 为结构化专利条目，返回 JSON
```

专利条目输出字段（映射自 data.docs[]）：
`patent_number, title, ipc, legal_status, application_date, publication_date,
cited_count, assignees, inventors, jurisdiction, url, view_url`
外加 `total_hits, returned_count`。返回 `json.dumps({query, total_hits, returned_count, patents:[...]})`。

### 5.2 `read_patent(patent_number, max_chars=25000, __user__={})`

```
启用检查
  → _zhihuiya_call('patsnap_fetch',
        {keys: [patent_number], key_type: 'pn', module: ['basic', 'legal']},
        key, url=_PATSNAP_MCP_URL, timeout=60)
  → 取 results[0].markdown（权利要求+说明书+法律状态）
  → 截断到 max_chars，超长追加 "[…专利文档截断…]"
```

- 默认 `module=['basic','legal']`（legal 含法律状态/预估到期日，用户指定重要）。
- fetch 单篇即可（keys 只传 1 个 pn）；markdown 大（~91KB），必须截断。
- 专利不存在/获取失败 → 返回 `{"error": ...}`。

## 6. 错误处理

- 复用 `_redact_zhihuiya_key`：专利工具的 key 同样可能泄在 URL/异常文本里，所有
  错误消息先脱敏。
- `_zhihuiya_call` 超时 30s（read_patent 传 60s，因 markdown 大）。
- 未启用（无 key 或开关关）→ 返回 `{"error": "智慧芽源未启用（未配 apikey 或已关闭）"}`，不发请求。
- `patsnap_search`/`patsnap_fetch` 返回 isError 或异常 → `_zhihuiya_call` 抛 RuntimeError，
  工具方法捕获后返回 `{"error": ...}`。

## 7. 防并发/防卡死

- 全程 async（mcp SDK async client），不新增同步阻塞。
- 每次调用新建 MCP 会话（`async with`），用 `asyncio.wait_for` 兜底。

## 8. 测试

1. mock `_zhihuiya_call`，测 `search_patents`：
   - 正确传参（source='patent'、search_strategy=['semantic']、limit clamp）
   - 输出结构含 patents 列表与字段
2. mock `_zhihuiya_call`，测 `read_patent`：
   - 返回 markdown 且截断到 max_chars
   - module 含 basic+legal
3. 未启用（无 key）→ 两个工具都返回错误 JSON，不发请求。
4. redact：`_zhihuiya_call` 抛含 `apikey=secret` 的异常时，错误消息含 `apikey=***`。
5. 真实端点验证（用 .env key）：
   - `search_patents('CRISPR gene editing', limit=2)` 返回 ≥1 条含 patent_number/title
   - `read_patent('US11530424B1')` 返回含 "Patent Details"/权利要求 的 markdown

## 9. 影响面

- **改动文件**：仅 `tool.py`（`_PATSNAP_MCP_URL`、`_zhihuiya_call` 加 url 参数、
  `search_patents`、`read_patent`）；`tests/test_zhihuiya.py` 追加测试；
  README/CLAUDE.md/tool.py docstring 同步（新增专利工具说明）。
- **不改**：mcpo config.json、docker-compose、后端 paper-search-mcp、现有 17 源逻辑。
- **依赖**：open-webui 容器已自带 `mcp`，无需新增。

## 10. 文档与发布

功能完成并验证后：更新 tool.py docstring（工具用法加 search_patents/read_patent）、
README.md（特性+架构+专利源说明）、CLAUDE.md（源矩阵加 patsnap 专利源）、版本号 2.3.0→2.4.0，
合并 main 并推送 GitHub。
