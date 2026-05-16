# Android 问题类型优先路由与 Deep 分析计划

## 背景

Android 问题分析不应该只按“模块 bundle”拆规则。FWK、系统 App、普通 App、Native 模块和厂商服务都可能遇到相同类型的问题，而不同问题类型需要优先查看的证据源完全不同。

因此第一层应按“问题类型 Profile”路由，第二层再按模块 bundle 聚焦。模块 bundle 负责告诉系统“这个模块有哪些代码路径、log tag、服务名、包名、厂商经验日志语义、CLAUDE.md、Skill 和历史案例”，但不负责决定稳定性问题先看 dropbox、XTS 先看测试报告、性能先看 trace。

目标是把重复的证据发现、文件筛选、格式解析、规则匹配和代码范围控制工具化，减少 AI 在 grep、试错和大范围阅读上的 token 消耗。

## 首批问题类型

先只考虑以下 Profile，后续遇到新类型再扩展：

| Profile | 首要证据 | 辅助证据 | 典型问题 |
|---|---|---|---|
| `functional` | android_log/logcat、events、业务状态文件、dumpsys、shared_prefs/settings | 相关模块代码、厂商经验日志字典、历史案例 | 接口调用失败、状态不一致、流程被拦截、权限/安装/配置导致的功能失败 |
| `stability` | dropbox、Java crash、ANR、tombstone、system_server crash、调用栈 | logcat、events、源码栈帧、历史案例 | 崩溃、ANR、native crash、system_server 异常 |
| `xts` | XTS/CTS/GTS 测试报告 HTML/XML、failed case、tradefed log | 对应用例 logcat、设备状态、模块代码 | 测试失败、断言失败、环境/设备状态异常 |
| `memory` | dumpsys meminfo、smaps、hprof、LMKD/psi、GC log | logcat、process stats、代码对象/缓存路径 | 内存泄漏、PSS/RSS 异常、OOM、低内存杀进程 |
| `performance` | perfetto/systrace/trace、slow log、binder latency、sched 信息 | logcat、dumpsys、模块代码、历史案例 | 卡顿、耗时、线程阻塞、binder 慢调用 |

安装、权限、SELinux、AppOps 等不作为第一批顶层 Profile。它们通常是 `functional` 或 `stability` 的失败原因/证据信号，例如接口调用抛异常、调用被拦截、安装链路失败、权限拒绝导致功能不可用。

## 三层模型

### 1. Profile

全局共享，定义“这个问题类型应该先看什么”：

- aliases：用户可能如何描述该问题。
- evidence_sources：首要/辅助证据源。
- preferred_file_kinds：优先文件类型。
- default_keywords：基础关键词。
- parser_chain：应调用的解析器。
- sampling_policy：首轮采样预算和排序规则。
- report_template：报告结构。
- verifier_policy：如何判断证据强弱和是否需要 Deep。

### 2. Module Bundle

模块 bundle 定义“在证据里关注谁”：

- 模块名称、别名、包名、service name、binder interface、log tag。
- AOSP 原生代码路径和厂商定制代码路径。
- 关键类、方法、常量、feature flag、system property、settings、xml/config。
- 厂商经验日志语义字典：晦涩 tag/code/message 的含义、正常/异常状态、误判边界。
- `supported_profiles`：该模块常见的问题类型。
- `profile_overrides`：该模块对全局 Profile 的补充关键词、重点文件、解析提示。
- 关联 Skill、CLAUDE.md、历史案例索引。

### 3. Evidence Parser

按证据类型实现工具化解析，输出结构化摘要：

- `dropbox_parser`：进程、异常、cause、栈顶、时间。
- `anr_parser`：subject、blocked/main thread、锁等待、CPU。
- `tombstone_parser`：signal、backtrace、so、线程。
- `xts_report_parser`：suite/module/case、failure message、堆栈、附件路径。
- `logcat_profile_sampler`：按 Profile + 模块关键词采样。
- `meminfo_parser` / `smaps_parser` / `hprof_indexer`：内存维度摘要。
- `trace_metadata_indexer`：slice、thread、binder transaction、long task、sched latency。

## 首轮工作流

```text
用户问题 + 上传日志
  -> Profile Router：判断问题类型候选
  -> Evidence Discovery：按 Profile 找文件和格式
  -> Module Router：结合用户描述、证据摘要、bundle 关键词选择模块
  -> Parser Chain：调用对应解析器生成结构化摘要
  -> Module Focus Filter：用模块 tag/包名/类名/方法名/厂商语义再过滤
  -> Rule Matching / Case Recall
  -> Evidence Pack
  -> AI 首轮报告 / Verifier
```

首轮工作流应尽量少让 AI 自由搜索。AI 输入主要是结构化摘要、Top 证据、规则命中、案例召回结果和必要的少量日志窗口。

### 前置工作流搜索分层

前置工作流只允许使用 1/2 类关键词：

1. **首轮严格搜索**：只使用 1 类精确日志证据，例如 `TAG + message`、私有日志封装输出、`Slog/EventLog`、native log macro、Intent action、异常类和 Android 基础规则（lifecycle/crash/ANR/tombstone/permission/XTS/trace）。
2. **第二轮扩大范围**：如果首轮不足，再加入 2 类项目身份/功能范围信号，例如 package、process、component、service、binder、permission、settings/system property、真实 TAG。

首轮不应加载：

- 3 类代码检索线索：类名、方法名、目录、业务流程词、preferred paths。
- 4 类用户语义模糊词：从用户描述理解出来的 `锁机`、`解锁`、`同步失败`、`网络异常` 等泛词。

这些内容只能进入 Deep，并且必须逐层放宽。

## Deep 分析定位

Deep 和首轮工作流的差别不只是“多给一点日志”。它应该是有边界、有计划地放大证据和能力：

| 能力 | 首轮工作流 | Deep 分析 |
|---|---|---|
| 日志输入 | Profile 过滤后的 Top 证据和窗口 | 可扩大到更多原始日志窗口、相邻时间段、辅助证据源 |
| 代码读取 | 默认不读或只读极少量结构化代码摘要 | 可在白名单代码目录内 grep/Read，围绕栈帧、tag、类名、方法名检索 |
| Skill | 只使用命中 Profile/模块的摘要级指导 | 注入命中的 Skill 工作流，引导 AI 按专门排查步骤执行 |
| CLAUDE.md | 首轮只注入命中 bundle 的必要摘要 | Deep 可加载命中代码根目录的 CLAUDE.md/项目规则，仍按需截断 |
| 历史案例 | 首轮召回少量高相关案例 | Deep 可扩大案例召回范围，比较相似/反例案例 |
| AI 自由度 | 低，主要整理结构化证据 | 中等，可在工具化边界内提出二次检索和代码阅读请求 |

Deep 不应变成“把所有日志和所有代码交给 AI”。它应先生成 `deep_plan`，再按计划执行：

```text
首轮结论 / Verifier
  -> 判断是否需要 Deep
  -> Deep Plan：要扩大哪些证据源、读取哪些代码范围、加载哪些 Skill/CLAUDE.md/案例
  -> 验证 claude_web_paths.config.json 白名单
  -> 采集更多日志窗口 + 代码上下文 + Skill/CLAUDE.md 摘要 + 历史案例
  -> Deep Evidence Pack
  -> AI Deep 报告 / Verifier
```

## Deep 升级优先级

Deep 分析按明确优先级逐步扩大证据范围：

1. **优先使用相关 Skill**：先加载命中的 Profile Skill、模块 Skill 或项目专属日志分析 Skill，让 AI 按专门排查流程组织证据链。例如稳定性先按 crash/ANR/tombstone 流程，性能先按 trace/thread/binder latency 流程，模块 Skill 再补充业务特有状态机和日志语义。
2. **功能对应的 1 类日志**：根据用户描述的功能，从 Skill / CLAUDE.md / 规则包中找到对应 `TAG + message` 精确日志，先做窄搜索。
3. **功能对应的 2 类范围信号**：1 类不够时，再扩大到功能相关 TAG、包名、组件名、service、binder、action、permission、settings key。
4. **项目代码范围和 3 类 Deep hints**：仍不足时，读取 `claude_web_paths.config.json` 命中的白名单代码、CLAUDE.md、规则包 `metadata.deep_hints`、历史案例，找新的功能入口和真实日志输出。
5. **TAG + 用户语义模糊兜底**：只有当前几层都无法形成高置信度结论时，才允许 AI 基于用户描述和已有证据生成一层模糊关键词；这些关键词必须与真实 TAG/包名/组件范围组合使用，不允许全量日志直接 grep。
6. **发散也必须受边界约束**：AI 新生成的关键词只能用于白名单日志和白名单代码范围内的二次检索；每次扩展的关键词、命中文件、读取片段、输入输出大小和置信度变化都必须记录到 debug trace。

Deep Evidence Pack 应显式分区记录：

- 命中的 Skill 及使用原因。
- 规则包提取出的 1/2 类日志线索和 3 类 Deep hints。
- 加载的 CLAUDE.md 摘要及路径。
- 白名单代码读取结果。
- AI 发散关键词及触发原因。
- 每一轮检索后置信度是否提升。

## `claude_web_paths.config.json` 的职责

该文件在 Android 分析里应是“能力白名单 + 模块知识索引”，而不是单纯路径列表。

它应承担：

1. 代码读权限白名单：Deep 只能读取命中 bundle 下配置的 paths。
2. 模块索引：提供 bundle id、title、summary、keywords、profiles。
3. 代码范围提示：提供 AOSP 原生路径和厂商定制路径。
4. Skill 索引：声明模块相关 Skill 的路径、摘要、关键词。
5. CLAUDE.md 索引：声明哪些根目录的 CLAUDE.md 可在命中时按需加载。
6. 厂商经验资料索引：声明可读的经验文档、日志语义字典、历史案例目录。

安全边界：

- 未命中 bundle 的 paths 不加入 Deep 代码范围。
- 前端不能直接传任意本机路径给 Deep。
- `preferred_paths` 必须在 bundle roots 内校验通过。
- Skill/CLAUDE.md 只读摘要或截断内容，避免一次性填满上下文。

## Rule Builder Skill 与评测闭环

`skills/android-log-rule-builder` 是该计划的关键基础设施。规则生成质量越高，首轮工作流越能过滤噪声、召回正确证据、降低 AI token 消耗。

该 Skill 需要长期演进，不能只生成“能通过 schema 的 JSON”。它应该逐步支持：

- 按项目类型生成不同规则：普通 App、FWK/系统服务、Native/NDK、SDK/Library。
- 按问题类型 Profile 生成不同规则：functional、stability、xts、memory、performance。
- 输出分层规则：1 类精确日志证据、2 类项目身份/功能范围信号；3 类 code/search/CLAUDE.md/Skill 线索只写入 metadata，不进入首轮规则。
- 不输出 4 类用户语义模糊规则；模糊扩展只能由 Deep 在低置信度时按边界生成。
- 输出配套知识：bundle manifest、log glossary、code index、CLAUDE.md 摘要建议、项目专属日志分析 Skill 关联建议。
- 用评测集衡量规则质量：命中率、误命中率、噪声比、Top 证据准确率、是否错误关联无关模块。

详细长期计划见：

[android-rule-builder-skill-evaluation-plan.md](android-rule-builder-skill-evaluation-plan.md)

## 开发阶段

### 阶段 1：Profile 元数据

- 新增 `profiles/`，先内置 `functional/stability/xts/memory/performance`。
- `bundle.json` 增加 `supported_profiles`、`profile_overrides`、`skills`、`claude_md`、`case_tags`。
- 更新规则生成 Skill，让它能按 Profile 生成模块补充规则。

### 阶段 2：Profile Router

- 在现有 Planner 前增加本地 Profile Router。
- 输入：用户问题、文件树、文件名、样本关键词、bundle 摘要。
- 输出：候选 Profile、证据源优先级、Profile 分数和原因。
- debug trace 记录命中/未命中规则。

### 阶段 3：证据发现与解析器

- 按 Profile 对文件分层，不同 Profile 走不同 parser chain。
- 首批解析器：
  1. `dropbox_parser`
  2. `xts_report_parser`
  3. `logcat_profile_sampler`
  4. `meminfo_parser`
  5. `trace_metadata_indexer`

### 阶段 4：Module Focus Filter

- 用模块 bundle 的 tag、包名、service、类名、方法名、厂商语义对解析器输出二次过滤。
- 稳定性问题支持从 stack frame 反查模块代码。
- XTS/功能问题支持从 case/assert/log tag 反查模块。
- 性能问题支持从 trace slice/thread/binder descriptor 反查模块。

### 阶段 5：Deep Plan 与白名单代码读取

- 在首轮 Verifier 后生成 `deep_plan`。
- Deep Plan 包含：
  - 扩大哪些证据源。
  - 读取哪些白名单代码范围。
  - 注入哪些 Skill。
  - 加载哪些 CLAUDE.md 摘要。
  - 召回哪些案例类型。
- 复用现有 `resolve_code_scopes()`，所有代码读取都必须通过 `claude_web_paths.config.json` 校验。

### 阶段 6：Deep Evidence Pack 重构

- `deep_evidence_pack.md` 拆成：
  - profile route
  - evidence expansion
  - selected skills
  - selected CLAUDE.md summaries
  - code context
  - case comparison
  - unresolved questions
- Deep debug trace 记录每个部分的输入大小、token、文件数量、命中原因。

### 阶段 7：回归数据集

为每个 Profile 准备脱敏 fixture：

- `functional`：接口失败、状态不一致、权限/安装/配置导致的功能失败。
- `stability`：Java crash、ANR、native tombstone。
- `xts`：失败 HTML/XML 报告 + 对应 logcat。
- `memory`：meminfo/smaps/hprof 示例。
- `performance`：trace metadata 或小 trace。

每个 fixture 记录：

- 期望 Profile。
- 期望证据源。
- 期望候选模块。
- 必须包含的证据。
- 不允许过度推断的点。
- 是否应该触发 Deep。

### 阶段 8：Rule Builder Skill 长期评测

- 建立开源项目池和本地 demo 项目池。
- 对每个项目固定 commit、bundle 配置和预期 profile 覆盖范围。
- 用 Skill 生成规则包，再跑 `validate`、`test` 和完整 Android 分析链路。
- 记录规则质量指标，并把失败样例反哺到 Skill 扫描策略和 schema。

### 阶段 9：前置规则分层落地

- 状态：已完成第一版。
- 已重构 rule builder 输出，让首轮只加载 1/2 类规则：
  - 1 类：`tier1-exact-log-*`，真实 `TAG + message` 正则。
  - 2 类：`tier2-project-scope`、`tier2-permission-scope`、`tier2-android-components`、`tier2-native-scope`。
  - profile scoped：稳定性、XTS、内存、性能类规则必须把通用现象与项目 scope 写进同一 regex。
- 已从 generated 规则包中剥离业务泛词、类名、目录名和 summary 拆词。
- 已将 3/4 类线索迁移到 `metadata.deep_hints`。
- 12 个评测 case 已通过，后续 debug trace 仍需继续观察首轮严格搜索和第二轮扩大搜索的噪声比例。

### 阶段 10：项目专属 Deep 日志分析 Skill

- 状态：已完成第一版。
- 已用 `project-guide-writer` 为目标项目生成 `<PROJECT_ROOT>/skills/<project-log-analysis>/SKILL.md`。
- Skill 按模块/功能组织：
  - 功能入口。
  - 1 类精确日志。
  - 2 类范围信号。
  - 3 类代码/文档/案例 hints。
  - 4 类模糊兜底边界。
- 已在 `claude_web_paths.config.json` 中按业务 bundle 挂载项目专属 Skill。
- Deep 分析时优先使用该 Skill；如果没有对应问题类型，先读 CLAUDE.md 找代码入口，再从源码中补充真实日志输出。
- 2026-05-11 补充落地：Deep Evidence Pack 已不再只列出 `related_skills` 名称，而是按命中的 bundle 白名单实际加载：
  - `claude_web_paths.config.json` 中显式配置的项目专属 `SKILL.md`。
  - 项目根目录下 `skills/*/SKILL.md`。
  - 项目根目录下 `.claude/skills/*/SKILL.md`。
  - 命中项目根目录的 `CLAUDE.md`、`AGENTS.md`、规则包 `claude_md_candidates` 指向的指南文件。
- Deep 报告提示词已明确要求先应用 `Selected Project Skills`，再应用 `Selected Project Guidance`，之后才使用规则包 exact logs / tier2 scope terms / 代码和日志上下文。
- 2026-05-12 补充落地：基于 `D:\Code\FWK\test_cases` 的 DLC / XTS 样例做了一轮离线回归：
  - fallback Planner 在 Claude Planner 不可用时，先按用户问题识别 XTS / DLC / DPM 等问题类型，再收敛到 `fwk-devicepolicy-generated`、`fwk-pms-generated`、`fwk-devicelock-generated` 等相关规则包，避免被样本里的 `Watchdog`、`system_server`、`ActivityManager` 噪声带偏。
  - Deep Skill 选择过滤掉 `android-log-rule-builder:*` 这类生成器 profile 标签，改为按问题关键词、规则包、Skill 名称/路径/摘要做相关性排序；DLC 样例优先加载 `dlc-issue-analyzer` / `dlc-log-filter`，XTS 样例优先加载 `cts-issue-analyzer` / `devicepolicy-framework`。
  - `android-base` 的 `package-install-failure` 规则已收窄，不再把普通 `PackageManager` 日志当作安装失败强证据，避免 XTS/包可见性问题被基础规则抢占。
- 后续仍需观察：项目 Skill 内容过长时的截断策略，以及多 bundle 同时命中时的 Skill 排序和 token 消耗。

## 与现有代码的关系

- 当前 `planner_prompt_budget_chars` 解决“输入不能无限膨胀”。
- 本计划解决“哪些内容值得进入输入”。
- 当前 `build_deep_evidence_pack()` 已通过 `claude_web_paths.config.json` 的 bundle paths 控制代码读取范围，后续应在此基础上加入 Profile、Skill、CLAUDE.md、历史案例。
- 当前 `collect_code_context()` 只按关键词扫描白名单代码，后续应由 Deep Plan 提供更明确的检索词和优先路径。
- 当前案例召回已存在，后续需要加 Profile/模块/证据源标签，便于 Deep 做更精准对比。

## 风险

- Profile Router 误判可能漏看关键证据，因此允许多 Profile 并行候选。
- 厂商经验日志字典需要持续维护，否则工具化解析会不完整。
- trace/hprof 解析复杂，第一版先做 metadata/index，不追求完整解析。
- Deep 如果没有明确 plan，仍可能回到漫无目的 grep，所以必须记录每次代码、Skill、CLAUDE.md 加载原因。
- Rule Builder Skill 如果缺少评测集，容易生成过宽泛规则，导致噪声增加、AI 误判和 token 浪费。
