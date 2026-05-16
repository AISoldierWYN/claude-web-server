# Android Rule Builder Skill 长期优化与评测计划

## 背景

`skills/android-log-rule-builder` 负责为不同 Android 项目生成 Module Bundle 规则包。它不是辅助工具，而是 Android 分析链路的核心前置能力。

规则生成得好，后续流程可以更快过滤日志噪声、召回关键证据、减少 AI 盲目 grep；规则生成得差，首轮工作流和 Deep 分析都会被无关日志带偏。

因此需要为该 Skill 建立长期优化和评测闭环。

## 目标

1. 让 Skill 能按项目类型生成不同规则。
2. 让 Skill 能按问题类型 Profile 生成不同规则。
3. 让生成结果不仅能通过 schema，还能在真实/半真实日志里命中正确证据。
4. 让每次 Skill 优化都有评测集验证，避免只优化某一个项目。
5. 让生成的规则包主要服务首轮前置工作流，只输出 1/2 类项目强相关日志规则；Deep 分析所需的 Skill、CLAUDE.md、案例和代码范围索引写入 metadata / bundle 索引，不混入首轮匹配规则。

## 信号分层原则

`android-log-rule-builder` 需要严格区分“前置工作流规则”和“Deep 分析线索”：

| 层级 | 名称 | 用途 | 允许进入首轮规则 |
|---|---|---|---|
| 1 类 | 精确日志证据 | 首轮严格搜索，快速缩小日志窗口 | 是 |
| 2 类 | 项目身份/功能范围信号 | 第二轮扩大搜索，确认日志属于哪个 bundle/功能 | 是，但低置信度 |
| 3 类 | Deep 代码检索线索 | Deep 读代码、读 CLAUDE.md、找入口 | 否 |
| 4 类 | 用户语义模糊关键词 | Deep 低置信度兜底 | 否 |

1 类必须来自源码或真实样例日志里的明确输出，包括：

- Android `Log.*`。
- Timber / Logger / LogUtils / HiLog / HwLog / HLog / XLog 等业务私有日志封装。
- FWK `Slog`、`EventLog`、`StatsLog`、`Trace`、`dumpsys` 固定字段。
- Native `__android_log_print`、`ALOG*`、`LOG_TAG`、项目 native log wrapper。
- Intent action、异常类、manifest component action、trace section。

2 类包括项目包名、进程名、真实日志 TAG、Activity/Service/Receiver/Provider、binder/service name、permission、settings/system property、native so/symbol/thread name。2 类只做范围扩大和归属判断，不能单独支撑结论。

3 类包括类名、方法名、目录名、模块名、业务流程词、preferred paths、CLAUDE.md 候选、相关 Skill、案例标签。它们应该进入 `metadata.deep_hints`、`bundle.json` 或未来 `code_index.json`，不应进入 `match.keywords`。

4 类只能由 Deep 在 1/2/3 类证据不足时按需生成，并且必须限制在真实 TAG/包名/组件范围内，不允许一开始就对全量日志 grep 模糊关键词。

## 项目类型预设

第一版不建议拆多个 Skill，先在同一个 Skill 里引入 project preset：

| Preset | 适用对象 | 重点扫描内容 |
|---|---|---|
| `app` | 普通 Android App | package、Activity/Service/Receiver/Provider、Log/Timber tag、业务状态词、网络/数据库/WorkManager/Notification |
| `framework` | FWK/系统服务/AOSP 模块 | service name、binder interface、system property、settings key、Slog/EventLog、dumpsys、权限/AppOps/SELinux 相关状态 |
| `native` | NDK/C++/Rust/native-heavy App | JNI、native library、tombstone symbol、C/C++ log macro、signal、thread name、trace section |
| `library` | SDK/公共库 | public API、异常类型、回调名、配置 key、依赖方 tag、集成错误 |

后续如果某个 preset 明显变复杂，再拆成独立子 Skill 或独立 reference 文档。

## Profile 预设

Skill 生成规则时应知道目标问题类型：

| Profile | 规则生成重点 |
|---|---|
| `functional` | 业务流程、状态机、接口失败、配置/权限/安装导致的功能不可用 |
| `stability` | Java crash、ANR、native crash、tombstone、关键异常类、栈帧定位 |
| `xts` | 测试 case、assert/failure message、tradefed log、模块/用例映射 |
| `memory` | 对象/缓存/图片/数据库/文件句柄、OOM、LMKD、meminfo/smaps/hprof 入口 |
| `performance` | trace section、耗时日志、binder latency、主线程阻塞、startup/baseline profile |

## 生成产物

长期目标不是只生成 `rules/<rule_pack_id>.json`，而是一组可被分析链路消费的知识包：

```text
android_analysis_knowledge/bundles/<bundle_id>/
  bundle.json
  rules/
    <short-name>-generated.json
  indexes/
    log_glossary.json
    code_index.json
    case_cards.jsonl
  references/
    claude_md_summary.md
    skill_selection.md
```

第一版仍可只强制生成规则包，但规则包内部也要分清用途：

- `rules[]`：仅放 1/2 类前置规则。
- `metadata.deep_hints`：放 3 类 Deep 线索。
- 不生成 4 类模糊规则。

## 规则质量指标

每次生成和修改规则，都应尽量记录以下指标：

| 指标 | 含义 |
|---|---|
| `schema_ok` | 是否通过 JSON/schema/regex 校验 |
| `hit_rate` | 在目标日志里是否能命中预期证据 |
| `noise_rate` | 命中的无关日志比例 |
| `top_evidence_precision` | Top N 证据里正确证据占比 |
| `wrong_bundle_hits` | 是否错误关联到无关 bundle |
| `generic_term_ratio` | `error/fail/exception` 等泛词占比 |
| `tier1_precision` | 1 类规则命中后 Top 证据是否为真实项目日志证据 |
| `tier2_noise_rate` | 2 类扩大范围后引入的无关日志比例 |
| `deep_hint_leakage` | 3 类 Deep hints 是否错误进入首轮 match 规则 |
| `fuzzy_rule_count` | 4 类模糊关键词是否被错误写入规则包，目标为 0 |
| `profile_coverage` | 覆盖了哪些问题类型 |
| `deep_usefulness` | Deep 是否能从规则线索进一步读到相关代码 |

## 测试集策略

建议使用“开源真实项目 + 小型可控 demo”的混合策略：

- 开源真实项目负责复杂度和泛化能力。
- 小型 demo 负责可控异常、稳定复现、CI 自动化。

只用 demo 容易过于简单；只用真实项目又很难稳定构造指定异常。因此两者都需要。

外部 GitHub 项目代码下载位置统一放在：

```text
tests/github_apps/<owner>__<repo>/
```

示例：

```text
tests/github_apps/android__nowinandroid/
tests/github_apps/AntennaPod__AntennaPod/
tests/github_apps/android__ndk-samples/
```

这些目录只用于本地评测、编译和生成日志，已经加入 `.gitignore`，不要提交到 GitHub。大日志、测试设备导出的压缩包、trace、hprof 等放到：

```text
tests/android_eval_artifacts/
```

该目录同样不提交。仓库内只保留轻量的 case 元数据、预期结果和必要的小型脱敏 fixture。

## 候选开源项目池

第一批建议以 APP 为主，FWK 后续再接入。原因是当前本机没有厂商定制 FWK，直接用 AOSP 验证 OEM 经验价值有限。

| 优先级 | 仓库 | 类型 | 适合验证 |
|---|---|---|---|
| P0 | [android/nowinandroid](https://github.com/android/nowinandroid) | Kotlin / Compose / 官方架构样例 | 功能、性能、模块化、baseline profile、现代 App 结构 |
| P0 | [AntennaPod/AntennaPod](https://github.com/AntennaPod/AntennaPod) | Java / 播客 App | 下载、播放、后台任务、数据库、网络、真实业务流程 |
| P0 | [android/ndk-samples](https://github.com/android/ndk-samples) | NDK 样例 | native crash、tombstone、JNI、CMake、轻量可控 native 场景 |
| P1 | [nextcloud/android](https://github.com/nextcloud/android) | Kotlin + Java / 同步网盘 | 网络、同步、文件、账号、后台任务、日志抓取 |
| P1 | [thunderbird/thunderbird-android](https://github.com/thunderbird/thunderbird-android) | Kotlin / 邮件 App | 多账号、同步、OAuth、数据库、复杂模块边界 |
| P1 | [termux/termux-app](https://github.com/termux/termux-app) | Java / 终端与进程 | 进程、终端、Android 12+ 进程限制、功能/性能/稳定性 |
| P2 | [TeamNewPipe/NewPipe](https://github.com/TeamNewPipe/NewPipe) | Java / 流媒体前端 | 网络解析、播放、下载、复杂业务，但维护分支状态需要注意 |
| P2 | [videolan/vlc-android](https://github.com/videolan/vlc-android) | Kotlin/Java + LibVLC/native | 媒体、native、性能、网络文件系统，构建成本较高 |
| P2 | [organicmaps/organicmaps](https://github.com/organicmaps/organicmaps) | Android + C++ | 地图、导航、native-heavy、性能/内存，项目较大 |
| P2 | [firebase/quickstart-android](https://github.com/firebase/quickstart-android) | Firebase 样例 | Crashlytics/Performance/Analytics 等场景，但部分需要 Firebase 配置 |

建议第一轮先选 3 个：

1. `android/nowinandroid`：现代 Kotlin/Compose/模块化基线。
2. `AntennaPod/AntennaPod`：真实 Java App 和复杂业务流。
3. `android/ndk-samples`：可控 native crash/tombstone。

第二轮再加入 `nextcloud/android` 或 `thunderbird/thunderbird-android`。

## 测试用例结构

评测代码和大日志不建议提交到 GitHub。建议只提交元数据和小型脱敏 fixture，大型仓库和日志放到 gitignored 目录。

```text
android_analysis_eval/
  README.md
  repos.json
  cases/
    nia-functional-navigation/
      case.json
      question.md
      expected.json
      notes.md
    antennapod-stability-playback-crash/
      case.json
      question.md
      expected.json
      notes.md
    ndk-native-tombstone/
      case.json
      question.md
      expected.json
      notes.md
```

`case.json` 示例：

```json
{
  "repo": "android/nowinandroid",
  "commit": "<pinned-commit>",
  "project_preset": "app",
  "bundle_id": "app-nowinandroid",
  "profiles": ["functional", "performance"],
  "log_archive": "local://android_analysis_eval_artifacts/nia-functional.zip",
  "generated_rule_pack": "nowinandroid-generated"
}
```

`expected.json` 示例：

```json
{
  "expected_profile": "functional",
  "expected_bundle_ids": ["app-nowinandroid"],
  "expected_keywords": ["ForYou", "Topic", "Repository"],
  "must_hit_rule_tags": ["identity", "business"],
  "must_not_conclude": ["system_server crash", "unrelated app crash"],
  "should_trigger_deep": false
}
```

## 评测流程

```text
clone/open-source repo
  -> 放入 tests/github_apps/<owner>__<repo>/
  -> 配置 claude_web_paths.config.json bundle
  -> rule_pack_manager.py generate
  -> validate
  -> 用合成/真实日志 test
  -> 跑完整 Android 分析
  -> 比对 expected.json
  -> 输出 scorecard
  -> 反哺 Skill 扫描策略和规则 schema
```

## 阶段计划

### 阶段 1：评测骨架

- 状态：已完成第一版。
- 新增评测目录规范和 `case.json` / `expected.json` schema。
- 新增 `evaluate` 命令或独立脚本，能批量运行 generate/validate/test。
- 输出 `scorecard.json`。

### 阶段 2：APP preset

- 状态：已完成第一版。
- 优化 Skill 对普通 App 的扫描：
  - Gradle module。
  - Manifest component。
  - package/namespace/applicationId。
  - Log/Timber tag。
  - WorkManager、Service、Receiver、Provider。
  - 网络、数据库、文件、通知、权限、配置关键词。
- 增加泛词降噪策略，避免只靠 `error/fail/exception` 命中。

### 阶段 3：Profile-aware 规则生成

- 状态：已完成第一版。
- `--profile functional/stability/xts/memory/performance`。
- 每个 Profile 生成不同类别规则。
- `bundle.json` 自动写入 `supported_profiles` 和 `profile_overrides`。
- 当前实现保持通用，不绑定 RDM：profile 规则复用项目包名、TAG、组件、业务词、APP capability 和通用问题类型入口词。

### 阶段 4：开源项目池验证

- 状态：已完成第一版。
- 先接入 `nowinandroid`、`AntennaPod`、`ndk-samples`。
- 每个项目至少准备 2 个 case。
- 每次 Skill 修改后跑回归，记录噪声和误判。
- 外部仓库通过 sparse checkout 下载到 `tests/github_apps/`，不提交 Git。
- 评测 case 位于 `android_analysis_eval/cases/`，当前使用小型合成日志验证规则生成和命中；真实设备日志后续放入 `tests/android_eval_artifacts/`。

### 阶段 5：Native preset

- 状态：已完成第一版。
- 扫描 JNI、CMake、native library 名、C/C++ log macro。
- 从 tombstone 中验证 symbol、so、thread name 是否能关联到正确 bundle。
- 当前实现覆盖 C/C++/Header、CMakeLists、Android.mk/Application.mk、JNI_OnLoad、Java_*、RegisterNatives、ANativeActivity_onCreate、android_main、LOG_TAG、__android_log_print、ATrace、AChoreographer、ANativeWindow 和 lib*.so。
- `android/ndk-samples` 的 `hello-jni` 与 `native-activity` case 已切到 native preset 并通过评测。

### 阶段 6：复杂 App 扩展

- 状态：已完成第一版。
- 加入 `nextcloud/android`、`thunderbird/thunderbird-android`、`termux/termux-app`。
- `repos.json` 已配置 sparse checkout，外部代码仍放在 `tests/github_apps/`，不提交到 Git。
- 新增 6 个复杂 App case：
  - Nextcloud：账号鉴权 + 文件同步，后台上传/下载 worker 稳定性。
  - Thunderbird：账号创建 + IMAP/mail sync，数据库升级启动崩溃。
  - Termux：`RUN_COMMAND` / shell process 功能问题，terminal session 性能问题。
- App preset 增加复杂能力分组规则：
  - `sync-account-signals`：同步、上传/下载、账号、OAuth、Token、WebDAV、IMAP/SMTP。
  - `background-task-signals`：Worker、Job、Receiver、Service、Foreground/Alarm。
  - `process-terminal-signals`：Termux、terminal、shell、process、command、execution、session。
- 新规则默认要求命中项目/能力关键词，避免只靠 `sync failed` / `service error` 这类泛化正则造成噪声。
- 当前评测：12 个 case 全部通过。

### 阶段 7：FWK / AOSP / Pixel 验证

- 状态：暂缓，作为遗留项保留。
- 暂缓原因：当前本机缺少 Pixel/AOSP/OEM FWK 环境，直接用通用 AOSP 做结论验证价值有限。
- 有 Pixel 或 AOSP 环境后再接入。
- 优先验证通用 FWK 能力，不把结果误认为 OEM 定制能力。
- 后续公司环境可加入厂商定制 FWK bundle。

### 阶段 8：Deep 联动

- 状态：已完成第一版。
- 让生成规则输出 Deep 可用线索：
  - code search terms。
  - preferred paths。
  - related skills。
  - CLAUDE.md candidates。
  - case tags。
- 验证 Deep 是否优先使用 Skill，再使用规则线索和代码上下文。
- 当前实现：
  - `generate` 会把 `metadata.deep_hints` 写入规则包，并同步到 `bundle.json`。
  - 规则匹配会把规则包级别和命中事件级别的 Deep hints 写入 `matched_rules.json`。
  - Deep evidence pack 会合并这些 hints，优先传给代码范围解析、关键词检索和 Deep Prompt。
  - Deep Prompt 明确要求先参考相关 Skill/项目指南，再使用规则线索、`preferred_paths`、`CLAUDE.md` 候选和更广泛代码/日志上下文。

### 阶段 9：真实项目 Deep 验证与项目知识补齐

- 状态：待开始。
- 目标：用真实 Android 项目代码和真实/半真实日志验证 Deep 链路是否真的减少盲搜，并能给出更稳定的结论。
- 输入准备：
  - 至少选择 2 个 APP 项目和 1 个 native-heavy 项目。
  - 每个项目在 `claude_web_paths.config.json` 中配置独立 bundle id、title、summary、keywords、paths。
  - 使用 `android-log-rule-builder` 为每个 bundle 生成 `<bundle-short-name>-generated` 规则包。
  - 每个项目至少准备 1 个 `CLAUDE.md` 或 `AGENTS.md`，描述模块边界、关键流程、常见日志 TAG、排查入口。
- 验证重点：
  - 首轮工作流是否能命中正确 bundle 和规则包。
  - Deep 是否优先读取 `metadata.deep_hints.preferred_paths` 和 `CLAUDE.md candidates`。
  - Deep 是否先使用项目知识和规则线索，再扩大到通用代码/日志搜索。
  - Deep evidence pack 是否包含可解释的代码上下文、项目指南片段和日志证据。
  - 最终报告是否比首轮报告减少误判、提升置信度或明确指出证据不足。
- 是否需要项目专属 Skill：
  - 第一版不强制为每个项目创建独立 Skill。
  - 如果某项目有稳定复用的领域知识、专门工具命令、日志解码脚本或多步骤排查流程，再沉淀项目专属 Skill。
  - 项目专属 Skill 应引用规则包、`CLAUDE.md`、案例库和脚本，不应复制大段源码。
- 产出：
  - 每个项目的规则包质量记录。
  - 每个 case 的首轮/Deep 对比记录。
  - Deep 使用 hints、`CLAUDE.md`、代码路径和案例召回的命中情况。
  - 后续需要补充的项目知识、规则缺口和 Skill 候选清单。

### 阶段 10：规则包分层重构

- 状态：已完成第一版。
- 目标：把当前 generated 规则包从“混合信号索引”收敛为前置工作流规则包。
- 任务：
  - 扫描 `Log.*`、Timber、私有日志封装、FWK `Slog/EventLog`、native log macro，抽取 1 类 `TAG + message`。
  - 抽取 2 类 package/component/action/service/permission/settings/system property/native symbol。
  - 把类名、方法名、目录名、业务词、preferred paths、CLAUDE.md 候选移动到 `metadata.deep_hints`。
  - 删除或降级 `business-flow`、`functional-flow` 中由 summary/业务语义拆出的泛词规则。
  - 评测 `rdm-generated`、复杂 App、native case，确保 Top 证据不再被模糊词污染。
- 验收：
  - 已通过：生成规则拆为 `tier1-exact-log-*`、`tier2-*`、profile scoped 规则。
  - 已通过：`match.keywords` 中不再出现未绑定 TAG/组件的泛词，如 `lock`、`unlock`、`sync`、`error`、`fail`。
  - 已通过：`metadata.deep_hints` 保留 `search_order`、`exact_logs`、`tier2_scope_terms`、代码检索词、优先路径和 CLAUDE.md 候选。
  - 已通过：12 个评测 case 全部通过，scorecard 输出到 `temp/rule_builder_scorecard.json`。

### 阶段 11：项目专属日志分析 Skill 评测

- 状态：已完成第一版。
- 目标：验证 `project-guide-writer` 生成的项目专属日志分析 Skill 是否能指导 Deep 按层级扩大搜索。
- 任务：
  - 已为 RDM 生成 `D:/AndroidCode/RealtimeDeviceManager/skills/rdm-log-analysis/SKILL.md`。
  - 已为 Now in Android、AntennaPod、NDK Samples、Nextcloud、Termux、Thunderbird 生成项目专属 Skill。
  - 已在 `claude_web_paths.config.json` 中为各业务 bundle 挂载对应项目专属 Skill。
  - Deep 分析时记录是否遵守：1 类精确日志 -> 2 类功能范围 -> 3 类代码入口 -> 4 类 TAG+语义兜底。
  - 失败 case 反哺到 `project-guide-writer` 和 rule builder 扫描策略。
- 验收：
  - Deep debug trace 能展示每层扩展的输入、输出、命中、置信度变化。
  - Deep 不再直接从用户描述泛词开始 grep。

## 成功标准

短期成功标准：

- Skill 能为 3 个不同项目生成可验证规则包。
- 每个项目至少 2 个 case 能跑完整链路。
- 规则命中能稳定进入 Evidence Pack。
- Top 证据不被无关 crash/noise 淹没。

长期成功标准：

- 每个 Profile 至少有 5 个以上回归 case。
- APP / Native / FWK preset 都有独立评测。
- Skill 修改前后能看到量化 scorecard。
- Deep 能利用规则包输出的检索线索、Skill、CLAUDE.md 和代码路径。
