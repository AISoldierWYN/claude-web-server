# Android Planner 观测与长期优化计划

## 背景

当前 Planner 的 `prompt_chars` 表示实际构造并传给 Claude CLI 的完整 Prompt 字符数。前端阶段详情里展示的 `prompt_preview` / `data` 会被 debug trace 安全截断，因此用户从页面复制出来的字符数会明显小于 `prompt_chars`。

`ai_token_usage.input_tokens` 表示模型侧返回或本地估算的 token 数。字符数和 token 数不是同一单位，中文、JSON、路径、日志片段的换算比例都不同，因此会出现“282K 字符”和“107K 输入 token”同时存在的情况。若 `token_source=stream_usage`，优先相信 CLI 返回的真实 token；若为 `estimate`，只用于趋势观察。

第一版 Planner 确实会把构造后的完整 Planner Prompt 一次性交给 Claude CLI。它的成本大头通常来自 `file_tree`、`file_samples`、manifest 摘要和样本文本。后续优化目标是减少首轮交给 AI 的上下文，只在不确定时按需追加。

## 目标

1. 让 Planner 每次输入的组成可解释：能看出 instruction、manifest、file tree、samples、bundle summary 各自占多少字符/token。
2. 把 Planner 首轮输入控制在稳定预算内，避免大日志包触发 100K+ token 输入。
3. 保持准确率：RDM、崩溃、ANR、tombstone、安装失败等常见问题不能因为压缩上下文而明显漏判。
4. 建立长期观测：每次 Android 分析都能记录 token、耗时、命中率、最终状态和是否需要 Deep。

## 阶段 A：统计口径统一

- 在 `planner_input` trace 中新增 `prompt_component_chars`：
  - `instruction_chars`
  - `manifest_summary_chars`
  - `file_tree_chars`
  - `file_samples_chars`
  - `bundle_summary_chars`
  - `requested_bundle_chars`
- 前端对 `prompt_preview` 明确标注“预览已截断”，避免和 `prompt_chars` 混淆。
- `analysis_metrics.json` 增加 Planner 专属块：
  - `planner_prompt_chars`
  - `planner_input_tokens`
  - `planner_token_source`
  - `planner_component_chars`

## 阶段 B：首轮 Prompt 硬预算

- 增加配置：
  - `planner_prompt_budget_chars`
  - `planner_max_tree_nodes`
  - `planner_max_sample_files`
  - `planner_max_sample_chars`
- 超预算时按优先级裁剪：
  1. 保留用户问题、bundle 摘要、候选文件统计。
  2. 保留 top-K 采样文件。
  3. 文件树只保留目录统计、重要路径和异常路径，不传完整树。
  4. 样本文本按命中关键词和路径相关性排序截断。
- trace 中记录裁剪前后大小和被裁掉的条目数量。

## 阶段 C：本地预路由与候选检索

- 在 Planner 前增加本地预路由：
  - 从用户问题提取关键词、模块名、包名、时间词、错误类型。
  - 用 manifest/path/kind/keyword 命中给文件打分。
  - 建立轻量倒排索引或 BM25 风格评分，先选 top-N 文件。
- 只有 top-N 文件进入 AI Planner，低分文件只提供统计信息。
- RDM 等 bundle 命中时，优先把对应规则包、业务关键词、白名单路径摘要放入 Prompt。

## 阶段 D：AI Planner 按需调用

- 本地预路由高置信时跳过 AI Planner，直接输出 `planner_mode=local_high_confidence`。
- 中等置信时调用 AI Planner，但只给精简 Payload。
- 低置信或跨模块问题才允许较大 Payload。
- 记录跳过/调用 AI 的理由，便于后续评估成本收益。

## 阶段 E：多轮按需取样

- 将 Planner 从“一次性看完样本”改成“先看摘要，再请求更多样本”：
  1. 首轮只给 manifest 统计、top paths、少量关键词样本。
  2. Planner 输出 `need_more_samples`、`requested_paths`、`requested_keywords`。
  3. 后端定向采样后再调用一次轻量 Planner。
- 每轮都记录输入大小、输出、追加样本原因和最终置信度。

## 阶段 F：回归评估

- 建立 `tests/fixtures/android_analysis_cases/`，保存脱敏的小样本日志和期望 Planner 输出。
- 指标：
  - issue type 命中率
  - bundle id 命中率
  - rule pack 命中率
  - 平均 Planner input tokens
  - p95 Planner input tokens
  - 是否触发 Deep
- 每次优化后对比准确率和 token 成本，避免只降成本但牺牲分析质量。

## 第一优先级建议

优先做阶段 A + B。它们风险最低，能立刻解释“字符数/token 对不上”的问题，并先把 Planner 输入上限压住。阶段 C 之后再逐步引入更复杂的检索策略。

## 实施记录

### 2026-05-10：阶段 A + B 第一版完成

- 已新增 `planner_prompt_metrics.json`，记录 Planner 实际 prompt 字符数、预算、各组件字符数和裁剪结果。
- `planner_input` debug trace 已增加 `prompt_component_chars`、`prompt_budget_chars`、`prompt_clipping`、`prompt_preview_truncated`，用于解释前端预览和真实输入大小不一致的问题。
- `analysis_metrics.json` 已增加 `planner` 块，聚合 `planner_prompt_chars`、`planner_input_tokens`、`planner_token_source`、`planner_component_chars` 和 `planner_clipping`。
- 已新增配置项：`planner_prompt_budget_chars`、`planner_max_tree_nodes`、`planner_max_sample_files`、`planner_max_sample_chars`。
- Planner 首轮 prompt 改为预算化输入：manifest 保留统计和有限文件列表，file tree 改为 compact summary，samples 按用户问题、关键词命中和通用 Android 日志类型排序后截断。
- Bundle 命中逻辑改为通用 `id/title/description/keywords/acronym` 匹配，不再依赖 RDM 专属特判，便于后续 fwk、系统应用和普通 app 项目复用。

### 下一步

- 阶段 C：增加本地预路由评分，将 top-N 文件选择前置，并把低分文件只作为统计信息进入 Planner。
- 阶段 D：当本地预路由高置信时跳过 AI Planner，只记录跳过原因和低成本路由结果。
- 阶段 F：补充多项目回归 fixtures，分别覆盖 RDM、fwk、普通 app、ANR、tombstone、安装失败等场景，长期观察准确率和 token 成本。
