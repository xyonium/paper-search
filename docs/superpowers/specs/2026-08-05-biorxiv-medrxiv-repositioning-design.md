# 设计文档：biorxiv/medrxiv 定位修正（消除误导）

日期：2026-08-05
状态：已获用户批准（方案 A）

## 1. 问题

bioRxiv/medRxiv API **没有关键词检索端点**——只有"按日期区间 + 学科分类"拉取。
后端 paper-search-mcp 的 `biorxiv.py`/`medrxiv.py` 把传入的关键词 query 当作**学科分类名**
（`query.lower().replace(' ','_')` → `?category=...`），在**近30天**里按分类拉论文。

实测（真实环境 open-webui→mcpo）：查询 `glucose sensor` 传给 biorxiv，返回的是
"X chromosome inactivation"、"CAR T cell therapy" 等**完全不相关**论文——因为
`glucose_sensor` 不是合法分类名，匹配不到任何分类时返回**未过滤的近30天新论文**。

**危害**：这 5 条无关结果会被 LLM 误当作"glucose sensor 的命中"推荐给用户——误导。

## 2. 根因

biorxiv/medrxiv 本质是"**学科新论文浏览**"工具，不是"论文检索"工具。把它当关键词源
塞进 `search_papers` 聚合，语义不匹配，必然误导。

## 3. 方案（A：移出默认源 + 标注引导）

1. **从 `UserValves.default_sources` 移除** `biorxiv,medrxiv`——默认聚合不再调用，
   彻底消除误导。
2. **保留显式调用能力**：LLM/用户想"浏览某学科近30天新论文"时，仍可显式
   `sources="biorxiv"` + `biorxiv_category="biochemistry"`（已实现的学科参数透传）。
3. **docstring + 文档明确标注**：biorxiv/medrxiv 是"该学科近30天新论文"浏览，非关键词检索；
   查具体主题请用其它源；浏览学科新论文时显式传源+学科。

## 4. 改动点

- `tool.py`：
  - `UserValves.default_sources` 去掉 `biorxiv,medrxiv`
  - 模块 docstring 与 `search_papers` docstring：更新源清单说明（biorxiv/medrxiv 标注为
    "学科近30天新论文浏览，非关键词检索，默认不启用；显式 sources+biorxiv_category 使用"）
- `README.md`、`CLAUDE.md`：源矩阵/查询特性表中标注 biorxiv/medrxiv 的真实定位与用法。
- 版本号 2.5.0→2.5.1。

## 5. 不改

- 后端 paper-search-mcp（第三方包）不动；biorxiv/medrxiv 的学科参数透传逻辑保留。
- 其它源逻辑不动。

## 6. 测试

- 单测：`default_sources` 不含 biorxiv/medrxiv；显式 `sources="biorxiv"` + category 仍可用。
- 真实验证：默认搜索不再出现 biorxiv/medrxiv 的无关结果。

## 7. 影响面

- 仅 `tool.py` + 文档。默认搜索结果中不再混入 biorxiv/medrxiv 的无关论文（这是目的）。
