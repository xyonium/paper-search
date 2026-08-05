# 设计文档：字面源术语优先级截断（修复长专业查询 0 命中）

日期：2026-08-05
状态：已获用户批准（术语优先级截断到 5 词）

## 1. 问题

现有 `_make_query_variants` 只去引号/裸露布尔/噪声词，**不截断词数**。但用户的真实查询是
"多专业术语并列"（如 `initiated chemical vapor deposition iCVD conformal polymer film
room temperature biosensor coating`，11 词），全是有效术语、无噪声可去 → core 仍 11 词 →
字面源（zhihuiya/doaj/iacr）0 命中。

实测（真实 key，zhihuiya）：
- 11 词查询 → 0
- 减到 6 词 → 5 条；4 词 → 5 条；3 词 → 5 条
- zhihuiya 是相关性匹配（非严格 AND）：词过多相关性稀释/无共现文档 → 0

## 2. 根因

字面源需要**更短的核心术语集**，而不仅是"去噪声"。需要按术语区分度截断：
保留**专业/罕见词**（更能锁定目标文档），砍掉**泛化词**（sensor/coating/film 等几乎每篇都有，
稀释相关性）。

## 3. 方案（术语优先级截断到 5 词）

新增 `_distill_core_terms(text, max_terms=5)`，在 `_make_query_variants` 的 core 基础上，
**仅当词数 > max_terms 时**截断：

1. 对每个词打分：
   - 含连字符/数字/括号（`poly(4-vinylpyridine)`, `p4vp`）→ +3
   - 全大写缩写（`iCVD`, `P4VP`, `CVD`）→ +3
   - 长词（≥8 字符，多为专业术语）→ +2
   - 泛化词（sensor/coating/film/membrane/thin/conformal/room/temperature/biomedical/
     medical/process/control/uniformity/principle/measurement/surface 等）→ -5
2. 按分数降序取前 max_terms 个，再按**原顺序**重排（保持可读性）。
3. 词数 ≤ max_terms 时不截断（原样返回 core）。

应用到字面源：`zhihuiya`（直连）、`doaj`、`iacr` 用 `distilled` 而非 `core`。
语义源不受影响（仍用 original）。

**截断提示**：当 distilled ≠ core（发生了截断）时，在 search_papers 返回的 JSON 增加
`query_adapted` 字段，列出每个字面源实际使用的精简查询，让 LLM/用户知晓，例如：
`"query_adapted": {"zhihuiya": "icvd conformal polymer film biosensor", "doaj": "...", "iacr": "..."}`。
未截断（core==original）时不加该字段。

## 4. 边界

- 极罕见主题（如 `poly(4-vinylpyridine)` 全名）即使截断也可能 0——该库确实没收录，属正常。
- max_terms=5 是实测临界点（6 词有结果、5 词稳），取 5 保守且命中率最高。

## 5. 测试

- `_distill_core_terms`：>5 词截断到 5、≤5 词不动、专业词优先保留、泛化词被砍、原顺序保持。
- search_papers：zhihuiya/doaj/iacr 收到 distilled（≤5 词）变体。
- 真实验证：用户的 7 条长专业查询，zhihuiya 从全 0 → 多数恢复有结果。

## 6. 影响面

- 仅 `tool.py`（新增 `_distill_core_terms` + search_papers 字面组改用 distilled）+ 测试。
- 版本 2.5.1→2.5.2。
