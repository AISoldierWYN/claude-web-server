# Android 问题分析能力开发计划

## 已确认决策

1. 第一批模块沉淀从 `D:/AndroidCode/RealtimeDeviceManager` 开始，对应 `claude_web_paths.config.json` 中的 bundle id 为 `android-rdm`。
2. 上传大小暂时沿用当前 100 MB 限制，不单独扩容。
3. 第一版只接 Claude CLI，Gemini 后续再作为可选 provider。
4. Deep 模式可以读取代码，但只能读取 `claude_web_paths.config.json` 中配置的代码目录。规则包、案例和分析任务必须通过 bundle id 关联代码目录，不能写死任意本地路径。
5. 规则库和案例库是全用户共享知识，不放在单个会话 cache 里。先放在仓库根目录的本地目录 `android_analysis_knowledge/`，并加入 `.gitignore`，不上传 GitHub。

## Feature 开关

```ini
[features]
android_issue_analysis = false
```

环境变量：

```bash
CLAUDE_WEB_ANDROID_ISSUE_ANALYSIS=true
```

默认关闭。关闭时不显示 Android 分析入口，后端相关接口返回禁用状态或 404，不改变 `/chat`、上传、会话、Gemini、移动开发等现有行为。

## 背景

当前项目已有局域网 Web 对话、文件上传、会话缓存、流式输出、Claude/Gemini CLI 调用、Tavily 搜索和移动端远程开发能力。Android 问题分析应复用这些基础设施，但不能把大日志包直接塞给大模型。正确方向是先程序化压缩信息，再让模型基于证据包判断。

## 目标

1. 提升 Android 日志问题分析速度：先安全解压、扫描、采样、规则匹配，再调用模型。
2. 控制成本：避免模型读取完整 bugreport、logcat、tombstone、ANR trace 和压缩包全文。
3. 降低误判：基础 Android 规则只生成候选事件，必须结合用户问题、业务规则、时间窗口、包名、TAG、代码模块后才进入证据包。
4. 支持沉淀：分析完成后可生成案例草稿和规则候选，经人工确认后写入共享知识目录。
5. 兼容现有架构：普通聊天继续走原路径，Android 分析任务只写自己的 job 目录和共享知识目录。

## 非目标

第一版不做全自动修代码，不做全量代码仓库检索，不做无边界日志全文 AI 阅读，不做复杂知识库 UI。案例库和规则库先用本地文件和索引承载。

## 一版验收标准

1. 开关关闭时，前端不出现 Android 分析入口，后端相关接口返回禁用状态或 404。
2. 开关开启后，用户可以上传 Android 日志压缩包或选择当前会话上传文件创建分析任务。
3. 后端完成安全解压、文件树扫描、文件类型识别、采样和 manifest 生成，全程不调用模型。
4. 轻量 AI Planner 只读取用户问题、文件树、样本、规则包摘要，并输出受 schema 校验的 JSON。
5. 规则引擎根据 Planner 选择的候选路径和规则包生成 `first_evidence_pack.md`。
6. Claude CLI 首轮只读取 Evidence Pack、命中规则和 Case Cards，输出结构化报告。
7. Deep 模式只有在规则或 Planner 关联到 `claude_web_paths.config.json` 的 bundle id 后，才允许读取对应代码目录。
8. 证据不足时进入“需要补充信息”或“建议 Deep 分析”状态，不硬编根因。
9. 前端通过 SSE 展示进度、当前阶段、最终报告和可下载 artifacts。
10. 至少覆盖安全解压、扫描采样、Planner JSON 校验、规则匹配、开关关闭行为的自动化测试。

## 总体流程

```text
用户问题 + 日志压缩包 + 可选规则包
  -> [0] Job 初始化
  -> [1] 安全解压 + 文件树扫描 + 样本采集
  -> [2] 轻量 AI Planner 路由
  -> [3] 按需加载规则包
  -> [4] 非 AI 规则匹配 + 相关性过滤
  -> [5] 案例库轻量召回
  -> [6] Claude CLI 首轮结论
  -> [7] 置信度门控
  -> [8] Deep 模式扩展证据，可按 bundle id 受控读取代码
  -> [9] Claude CLI 深度推理
  -> [10] Verifier 校验 + Case Draft 生成
  -> 最终报告 / 证据包 / 案例草稿 / 规则候选
```

## 共享知识目录

共享规则、案例、索引放在仓库根目录：

```text
android_analysis_knowledge/
  bundles/
    android-rdm/
      bundle.json
      rules/
      cases/
      drafts/
      indexes/
  global/
    android_base/
      rules/
      cases/
      indexes/
```

该目录已加入 `.gitignore`，用于本机长期沉淀，不提交到 GitHub。后续如果要迁移到别的机器，可以单独打包或同步这个目录。

`bundle.json` 建议字段：

```json
{
  "id": "android-rdm",
  "title": "RealtimeDeviceManager",
  "source_path": "D:/AndroidCode/RealtimeDeviceManager",
  "source_bundle_id": "android-rdm",
  "description": "自研锁机 APK 相关问题分析规则和案例",
  "rule_packs": ["rdm-base"],
  "case_indexes": ["cases/indexes/case_cards.jsonl"]
}
```

注意：`source_path` 只保存在被 gitignore 的本地知识目录里；提交到仓库的代码和文档只依赖 `source_bundle_id`。

## 任务数据目录

每个分析任务仍然独立落在当前用户和会话目录下：

```text
cache/<ip>/<user_id>/<session_id>/android_analysis/<job_id>/
  input/
  extracted/
  artifacts/
    file_manifest.json
    file_tree.json
    file_samples.json
    planner_result.json
    matched_rules.json
    first_evidence_pack.md
    case_cards.json
    final_report.md
    verifier_result.json
    case_draft.json
  job.json
  events.jsonl
```

`extracted/` 只允许安全解压写入。后续阶段只写 `artifacts/` 和 `events.jsonl`。正式规则和案例不写入 session cache，只从共享知识目录按需读取。

## `claude_web_paths.config.json` 关联规则

Deep 模式的代码读取依赖配置文件中的 bundle id，例如当前的 `android-rdm`。规则包和案例都应记录 `source_bundle_ids`：

```yaml
id: rdm-lock-flow
title: RDM 锁机流程问题
source_bundle_ids:
  - android-rdm
match:
  packages:
    - com.example.rdm
  keywords:
    - lock
    - unlock
    - provision
deep:
  allow_code_read: true
  max_files: 20
  preferred_paths:
    - app/src
```

执行 Deep 前必须校验：

1. `source_bundle_ids` 是否存在于 `claude_web_paths.config.json`。
2. 对应 bundle 的路径是否可访问。
3. 读取范围是否落在该 bundle 的 paths 内。
4. `preferred_paths` 只能是 bundle 内相对路径。

若校验失败，Deep 只能继续分析日志 artifacts，不读取代码。

## 模块设计

建议新增包：

```text
claude_web/android_analysis/
  __init__.py
  models.py
  jobs.py
  archive.py
  profiler.py
  sampler.py
  planner.py
  knowledge_store.py
  rule_loader.py
  rule_engine.py
  evidence.py
  casebook.py
  verifier.py
  code_scope.py
  prompts/
    planner.md
    first_pass.md
    deep_pass.md
    verifier.md
  rules/
    android_base.yaml
    README.md
```

| 模块 | 职责 |
| --- | --- |
| `jobs.py` | 创建、读取、更新 job 状态和 events |
| `archive.py` | 安全解压、路径穿越防护、大小和数量限制 |
| `profiler.py` | 文件树、文件类型、大小、时间、候选日志识别 |
| `sampler.py` | 每类候选文件提取小样本，避免全文进入模型 |
| `planner.py` | 调用轻量 Planner，校验 JSON，失败时 fallback |
| `knowledge_store.py` | 管理 `android_analysis_knowledge/` 的 bundle、规则、案例和索引 |
| `rule_loader.py` | 读取基础规则和业务规则摘要，按需加载全文 |
| `rule_engine.py` | regex/exact/tag/package/pid/tid/time-window 匹配 |
| `evidence.py` | 相关性评分、证据去噪、Evidence Pack 生成 |
| `casebook.py` | Case Card 召回、案例草稿生成和索引维护 |
| `code_scope.py` | 根据 bundle id 校验 Deep 模式可读代码范围 |
| `verifier.py` | 校验报告是否有证据支撑，发现过度推断 |

## API 草案

所有接口都受 feature flag 控制：

```text
GET  /api/android-analysis/status
GET  /api/android-analysis/bundles
POST /api/android-analysis/jobs
GET  /api/android-analysis/jobs/<job_id>
POST /api/android-analysis/jobs/<job_id>/start
GET  /api/android-analysis/jobs/<job_id>/events
GET  /api/android-analysis/jobs/<job_id>/artifacts/<artifact_name>
POST /api/android-analysis/jobs/<job_id>/deep
POST /api/android-analysis/jobs/<job_id>/case-draft
```

第一版可以先把 `start` 做成单 job 串行后台线程。后续任务量增加后，再引入队列、取消和并发限制。

## Planner 约束

Planner 是路由器，不是分析器。它只回答“该看哪里、该加载哪些规则、该搜哪些关键词、是否需要关联哪个 bundle id”。

输入：

1. 用户问题原文。
2. `file_tree.json` 摘要。
3. `file_manifest.json` 摘要。
4. 每个候选文件的短样本。
5. 规则包摘要和 bundle 摘要，不含完整规则和完整案例。

禁止：

1. 不挂载日志全文目录。
2. 不挂载代码目录。
3. 不加载完整 skill 或规则全文。
4. 不输出 Markdown。
5. 不输出根因结论。

输出 schema：

```json
{
  "issue_types": [],
  "candidate_bundle_ids": [],
  "candidate_rule_packs": [],
  "candidate_log_paths": [],
  "candidate_keywords": [],
  "candidate_entities": {},
  "exclude_paths": [],
  "confidence": 0,
  "need_user_clarification": false
}
```

首批 issue type：

```text
android_app_crash
android_system_server_crash
android_anr
android_native_crash
android_permission_denial
android_package_install
android_boot
android_framework_behavior
android_business_spec
android_test_failure
generic_log_error
unknown
```

Planner 失败或 JSON 校验失败时，退回基础规则扫描：优先查 crash、ANR、tombstone、fatal exception、watchdog、permission denial、install failure。

## 规则与证据

基础规则不要直接变成结论，只生成候选事件。例如：

```json
{
  "event_type": "android_app_crash",
  "time": "05-07 10:12:31.123",
  "process": "com.example.app",
  "package": "com.example.app",
  "pid": 12345,
  "tid": 12345,
  "exception": "java.lang.IllegalStateException",
  "top_frame": "com.example.Foo.bar(Foo.java:123)",
  "file": "main.log",
  "line_range": [1200, 1280],
  "source_bundle_ids": ["android-rdm"]
}
```

进入 Evidence Pack 前必须做相关性评分：

1. 是否命中用户描述中的包名、模块、功能词、错误现象。
2. 是否落在用户提供或 Planner 推断的时间窗口。
3. 是否命中业务规则包的 TAG、package、进程名、特征签名。
4. 是否关联到 `android-rdm` 等白名单 bundle id。
5. 是否和同一时间附近其他日志互相印证。
6. 是否是历史噪声、后台无关进程、重复低价值 warning。

Evidence Pack 应包含原始日志短片段、文件名、行号范围、命中规则、相关性原因和不确定性，不包含无边界全文。

## 案例库

案例库分两层：

1. Case Card：首轮召回使用，字段短，包含标题、signature、issue_type、module、tags、package、source_bundle_ids、根因摘要、修复摘要、证据摘要。
2. Full Case：Deep 或人工查看时加载，包含完整分析过程、关键证据、排查路径、误判风险。

案例生成来源：

```text
导出的对话 HTML / final_report.md / evidence_pack.md
  -> Case Generator Prompt
  -> case_draft.json
  -> schema 校验
  -> 人工确认
  -> 写入 android_analysis_knowledge/bundles/<bundle_id>/
  -> 重建索引
```

第一版只生成草稿，不自动进入正式案例库。

## 成本与性能控制

1. 安全解压、扫描、采样、基础匹配全部本地完成。
2. Planner 输入严格限制，默认超时短，失败可 fallback。
3. 每个 Evidence Pack 设置最大文件数、最大片段数、最大字符数。
4. 规则包按 Planner 结果加载，最多加载 5 个候选包。
5. Case 召回首轮只给 Case Card，不给完整案例。
6. Deep 模式必须由置信度门控或用户手动触发。
7. 对同一上传包按内容 hash 缓存 manifest/sample/规则匹配结果。
8. Claude CLI 默认不允许自由读取原始日志目录；Deep 模式读代码也必须通过 bundle id 校验。

## 安全边界

1. 解压防 Zip Slip，拒绝绝对路径、`..`、盘符路径和符号链接逃逸。
2. 限制压缩包大小、解压后总大小、文件数量、目录深度、单文件大小。
3. 默认跳过二进制和嵌套压缩包；需要支持时单独设白名单和限制。
4. artifacts 中不要保存用户 PC 绝对路径到可分享报告。
5. 所有 job 写入限制在当前 session 的 `android_analysis/<job_id>/`。
6. 共享知识目录不上传 GitHub，案例草稿需要脱敏本地路径、账号、token、设备唯一标识等敏感信息。
7. Deep 代码读取只允许 `claude_web_paths.config.json` 中的 bundle paths，不接受前端传入任意路径。

## 前端形态

第一版在现有聊天界面增加一个入口即可：

1. feature 开启后显示“Android 分析”按钮或模式切换。
2. 创建任务时填写问题描述、选择上传日志包、可选规则包或 bundle。
3. 默认可选 bundle 包含 `android-rdm`，但只有本地配置存在该 id 时才显示为可用。
4. 任务页展示阶段进度、SSE 事件、最终报告、证据包下载。
5. 普通聊天 UI 不因该功能开启而改变默认发送路径。
6. 移动端优先保证可查看进度和报告，复杂规则配置放到桌面端。

## 开发阶段

### 阶段 0：计划、开关、本地知识目录（已完成，2026-05-07）

- 已增加 `android_issue_analysis` feature flag。
- 已通过 `/api/features` 返回该开关。
- 已在 `config.example.ini` 增加示例配置。
- 已写入本计划文档。
- 已在 `.gitignore` 忽略 `android_analysis_knowledge/`。
- 已初始化 `android_analysis_knowledge/bundles/android-rdm/` 本地目录结构。

### 阶段 1：Job 与安全解压（已完成，2026-05-07）

- 已新增 `android_analysis` 包和 job 存储模型。
- 已实现安全解压、限制策略和 artifacts 目录；RAR 通过本机 7-Zip 后端受控解压，先列表校验再 staging 解压和二次校验。
- 已输出 `file_manifest.json`、`file_tree.json`。
- 已添加单元测试覆盖路径穿越、大小限制、job layout、开关关闭和最小 API 流程。

### 阶段 2：采样与类型识别（已完成，2026-05-08）

- 已识别 logcat、main/system/events/radio log、bugreport、ANR trace、tombstone、dumpsys、crash 文本。
- 已生成 `file_samples.json`。
- 已支持候选文本文件的 head/tail/关键词附近采样。
- 已跳过二进制文件，并限制每个文件扫描和采样字符量。

### 阶段 3：轻量 Planner（已完成，2026-05-08）

- 已实现 Planner prompt、Claude CLI 调用、JSON schema 校验。
- 已让 Planner 输出 `candidate_bundle_ids`，用于规则和 Deep 代码范围绑定。
- 已实现 fallback 策略：CLI 不可用、超时、输出非 JSON 或 schema 不合格时回退本地启发式 Planner。
- 已记录 `planner_result.json`。
- 已使用 `temp/PNM-N49.zip` 做真实 RDM 日志验证：314 个文件、约 21.7 MB 解压内容，真实 Claude Planner 可输出 `planner_mode=ai`，并命中 `android-rdm` / `rdm-base`。
- 已使用 `temp/android_logs.rar`、`temp/PNM-N49.rar`、`temp/PNM-N49-2.zip` 做格式兼容验证，三者均可走到 `planned`，且 Planner 为 `ai`。

### 阶段 4：规则引擎与 Evidence Pack（已完成，2026-05-08）

- 已实现内置 `android-base` 规则包，覆盖 Java crash、ANR、native tombstone、permission denial、install failure、system_server watchdog 和 boot 信号。
- 已初始化本机 `android_analysis_knowledge/bundles/android-rdm/rules/rdm-base.json`，用于 RDM 锁机、解锁、provision、device policy 等本地业务信号。
- 已实现 JSON 规则包加载、按 Planner 的 `candidate_rule_packs` / `candidate_bundle_ids` 按需选择规则包。
- 已实现 keyword、regex、package、path、kind 匹配；pid/tid/time-window 在第一版保留 schema 入口，后续结合结构化日志解析增强。
- 已实现相关性评分，综合 Planner issue type、候选日志路径、候选 bundle、关键词和规则 severity。
- 已补充 bundle/question focus 降权策略：当用户聚焦 RDM/锁机类问题时，无 RDM 或锁机关键词重叠的第三方应用 crash 只能作为背景噪声，不应压过 RDM 业务证据。
- 已生成 `matched_rules.json`、`first_evidence_pack.md`、`first_evidence_pack.json`，job 完成状态更新为 `evidence_ready`。
- 已使用 `temp/android_logs.rar`、`temp/PNM-N49.rar`、`temp/PNM-N49-2.zip` 做真实日志离线验证，三种格式均能生成规则命中和 Evidence Pack。

### 阶段 5：首轮报告与 UI（已完成，2026-05-08）

- 已实现 `casebook.py`，从本地知识目录的 `case_cards.jsonl` 召回轻量 Case Cards，并写入 `case_cards.json`。
- 已实现 `reporter.py` 和 `prompts/first_pass.md`，首轮报告优先调用 Claude CLI，只读取 Evidence Pack、Matched Rules、Planner Result 和 Case Cards；测试环境或 CLI 失败时使用本地 fallback 报告。
- 已生成 `final_report.md` 和 `first_report_meta.json`，job 完成状态更新为 `report_ready`。
- 已补齐 `GET /api/android-analysis/jobs/<job_id>`、`/events`、`/artifacts/<artifact_name>`，支持前端查看进度和下载报告、证据包、规则命中。
- 已在前端增加「Android分析」入口，可选择当前会话已上传日志包、填写问题、选择 bundle，完成后在聊天区渲染 Markdown 首轮报告。
- 已用 `temp/android_logs.rar`、`temp/PNM-N49.rar`、`temp/PNM-N49-2.zip` 验证到 `final_report.md` 生成完成，三者均有报告产物。

### 阶段 6：Deep 模式与 Verifier（已完成，2026-05-08）

- 已新增 `code_scope.py`，Deep 代码读取只允许命中 `claude_web_paths.config.json` 中配置的 bundle id 和路径；未配置、不可读、越界的 bundle 会被记录为 denied。
- 已新增 `deep_analysis.py` 和 `prompts/deep_pass.md`，生成 `deep_evidence_pack.md/json`，合并扩展日志片段和受控代码上下文，再生成 `deep_report.md`。
- 已新增 `verifier.py` 和 `prompts/verifier.md`，输出 `verifier_result.json` 与带 Verifier 提示的 `verified_report.md`，用于标记证据不足、部分支撑或过度推断风险。
- 已新增 `POST /api/android-analysis/jobs/<job_id>/deep`，前端可在首轮报告后手动触发 Deep 分析和 Verifier。

### 阶段 7：案例草稿与规则沉淀（已完成，2026-05-08）

- 已从 final/deep/verified report、Evidence Pack、Matched Rules、Verifier 结果生成 `case_draft.json`。
- 已生成 `rule_candidates.json`，规则候选只作为草稿保存，默认不写入正式 rules、不自动启用。
- 已新增 `POST /api/android-analysis/jobs/<job_id>/case-draft` 生成草稿。
- 已新增 `POST /api/android-analysis/jobs/<job_id>/case-draft/confirm`，人工确认后写入 `android_analysis_knowledge/bundles/<bundle_id>/cases/`，并更新 `indexes/case_cards.jsonl`。
- 前端报告操作区已增加 Deep 分析、案例草稿生成、案例草稿下载和确认入库入口。

### 阶段 8：性能与成本测试（已完成，2026-05-08）

- 已补充大噪声日志、无明确证据、bundle 聚焦降权、后台 job SSE 等自动化测试场景。
- 已验证采样器对大日志输入做字符上限控制，避免噪声日志或超长文件被整体交给模型。
- 已在每个分析 job 的 `artifacts/analysis_metrics.json` 记录阶段耗时、artifact 大小、Planner/首轮报告/Deep/Verifier 输入字符量估算。
- 已在同步首轮、后台首轮和 Deep 分析路径中写入性能指标，并通过 smoke 测试验证指标文件和 `analysis_metrics_recorded` 事件存在。

### 阶段 9：分析过程流式化与交互优化（已完成，2026-05-08）

- 已将 Android 首轮分析升级为后台 job + SSE 增量事件；前端优先消费 `/events/stream`，失败时回退轮询 `/events`。
- 已在聊天区增加可折叠「分析过程」面板，实时展示解压、扫描、采样、Planner、规则匹配、Evidence Pack、Case Recall、首轮报告、Verifier 和指标记录等阶段。
- 已复用现有 streaming lock，在 Android 分析、Deep、案例草稿生成和案例入库期间锁定输入区、侧栏、会话切换、删除、开发项目和 Android 分析入口，避免打断当前 job。
- 已将 `events.jsonl` 作为可恢复进度源；完成后的分析气泡会重新读取事件列表并渲染最终过程面板，刷新/切换回来后依旧能看到过程记录。
- 已在首轮报告后增加低成本本地 Verifier 门槛：若最高证据相关性不足，会生成 `verified_report.md` 风险提示，并把 job 状态降为 `needs_review`，避免无关高严重 crash 抢占结论。

### 阶段 10：项目日志规则生成 Skill（已完成，2026-05-08）

- 已在仓库根目录新增标准 Skill：`skills/android-log-rule-builder/SKILL.md`，用于指导用户或任意 AI 为特定代码项目生成、维护和验证 Android 日志规则包。
- Skill 目录采用标准结构：

```text
skills/android-log-rule-builder/
  SKILL.md
  scripts/
    rule_pack_manager.py
  references/
    rule_pack_schema.md
    android_source_scan_patterns.md
```

- `SKILL.md` 已只保留核心流程：读取 `claude_web_paths.config.json` 的 bundle id、扫描对应项目代码、抽取日志 TAG/包名/业务关键字/错误码/状态机词汇、生成候选规则、写入本地知识目录。
- `scripts/rule_pack_manager.py` 已提供命令行接口，供人和 AI 稳定调用：
  - `generate`：从项目代码生成或刷新规则包草稿。
  - `validate`：校验规则包 schema、正则合法性、bundle id 是否存在、规则 id 是否重复。
  - `list`：列出某个 bundle 的规则包和规则摘要。
  - `get`：查看指定规则包或规则详情。
  - `add`：新增规则。
  - `update`：更新规则。
  - `delete`：删除规则或规则包。
  - `test`：使用样例日志或最近一次 Android 分析 artifacts 验证规则能否命中。
- 规则输出路径已固定为 `android_analysis_knowledge/bundles/<bundle_id>/rules/<rule_pack_id>.json`，不写入 session cache，不提交 GitHub。
- 第一版生成策略已实现：
  - Java/Kotlin：扫描 `Log.d/i/w/e/wtf`、`Slog`、`Timber`、自定义 logger、`TAG` 常量、异常打印、错误码和状态枚举。
  - Android 工程元数据：扫描 `AndroidManifest.xml`、Gradle namespace/applicationId、包名、service/receiver/provider/activity 名称。
  - 业务词汇：从类名、方法名、目录名和常量中抽取 lock/unlock/provision/device policy 等领域词。
  - 输出规则必须带 `source_bundle_ids`，并尽量包含 `tags`、`match.keywords`、`match.regex`、`match.paths`、`issue_type`、`severity`。
- 与当前分析链路的验收已完成：
  - 已使用 `D:/AndroidCode/RealtimeDeviceManager` 生成本地 `rdm-generated` 规则包。
  - `validate` 通过：JSON 可解析、schema 合法、正则可编译、规则 id 唯一、bundle id 命中 `claude_web_paths.config.json`。
  - `list/get/add/update/delete` 命令已有自动化测试覆盖。
  - `test` 已用 `temp/PNM-N49-2.zip` 证明生成规则可命中 RDM 日志信号。
  - Android 分析阶段 4 的 `rule_loader` 可加载该规则包；本地规则列表可同时看到 `rdm-base` 与 `rdm-generated`。

### 阶段 11：维护注释与文档更新（已完成，2026-05-08）

- 已为 Android 分析链路中安全解压、采样、Planner、规则加载、规则匹配、证据生成、Deep/Verifier、案例沉淀、状态流、Skill 脚本等后续高频维护点补充中文注释；注释只解释非显而易见的约束、边界和设计原因。
- 已更新根目录 `README.md`，补充 Android 问题分析、规则知识目录、规则包结构、项目规则生成 Skill、常用命令、7-Zip/RAR 配置、性能指标和测试说明。
- 已更新 `config.example.ini` 的 Android 分析配置注释，说明 `android_issue_analysis`、`android_analysis_knowledge/` 和 7-Zip 配置用途。
- 已增加 `tests/test_android_log_rule_builder_skill.py`，覆盖 Skill 脚本帮助信息、规则包生成、schema 校验、list/get/add/update/delete/test。

## 暂缓问题

1. Gemini provider 接入时机。
2. 共享知识目录的备份/同步方式。
3. 是否需要为超过 100 MB 的 Android 日志单独做分片上传。
4. 规则和案例的可视化管理 UI。
5. Android 分析任务取消按钮与后端取消信号。
