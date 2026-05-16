---
name: android-log-rule-builder
description: 为 claude-web-server 的 Android 问题分析前置工作流生成、校验、查询和维护项目日志规则包；当需要为某个 claude_web_paths.config.json bundle 扫描 Android 项目代码、抽取强项目相关的 Log/Timber/Slog/私有日志封装 TAG、TAG+日志文本、包名/组件名，创建 android_analysis_knowledge 规则包，或对规则包做 list/get/add/update/delete/test 时使用。该 skill 只生成前置轻量工作流使用的 1/2 类规则，不负责 Deep 阶段的自由代码分析。
---

# Android Log Rule Builder

使用本 skill 为特定 Android 项目生成和维护**前置工作流**使用的日志规则包。规则包会写入本机知识目录：

```text
android_analysis_knowledge/bundles/<bundle_id>/rules/<rule_pack_id>.json
```

该目录默认不提交 GitHub。生成的规则会被 `claude_web.android_analysis.rule_loader` 按需加载，并参与 Android 分析阶段 4 的本地规则匹配。

## 使用边界

本 skill 只服务“首轮/前置简单 AI 分析工作流”，目标是用低成本规则把日志范围缩小到项目强相关证据。它不应该把项目所有类名、业务摘要词、Gradle 依赖、自然语言联想词都放进首轮匹配规则。

Deep 分析需要更开放的代码阅读、项目指南、项目专属日志分析 skill、历史案例和 CLAUDE.md 约束；这些应由 `project-guide-writer` 生成的项目知识来引导，而不是塞进本规则包的首轮规则里。

## 信号分层

生成规则必须区分下列层级：

### 1 类：严格精确日志证据

用于首轮严格搜索。必须来自代码或样例日志中明确存在的日志输出：

- `Log.*(TAG, "...")`、`Timber.*("...")`、`Slog.*(TAG, "...")`、`EventLog.write*`。
- 项目私有日志封装，例如 `Logger.d/e`、`LogUtils.i/e`、`HiLog`、`HwLog`、`HLog`、`XLog` 等，只要能从源码识别出 TAG 和文本。
- Native 日志宏，例如 `__android_log_print`、`ALOGE`、`LOG_TAG`、项目自定义 native log wrapper。
- 精确异常类、Intent action、系统固定字段、trace section 或 manifest component action。
- 推荐形式：`TAG + message prefix/regex`，例如 `DeviceLockStateManagerImpl / Enforce current lock policy fail, result:`。

### 2 类：项目身份和功能范围信号

用于第二轮扩大范围。必须仍然强项目相关：

- 项目包名、applicationId、进程名。
- Activity/Service/Receiver/Provider 名。
- 真实日志 TAG 名。
- Binder/service/action/permission/settings key/system property。
- 对 native 项目，可包括 so 名、JNI symbol、trace section、thread name。

2 类信号只能用来判断“日志可能属于该 bundle/功能范围”，不能单独支撑根因结论。

### 3 类：Deep 代码检索线索

只写入 `metadata.deep_hints`、`bundle.json`、`code_index` 等 Deep 辅助字段，不参与首轮规则匹配：

- 类名、方法名、目录名、模块名。
- 业务流程词、状态机名称、配置名。
- CLAUDE.md 候选、preferred paths、相关 Skill、案例标签。

### 4 类：用户语义模糊关键词

本 skill 不生成 4 类规则。只有 Deep 分析在 1/2/3 类都不足时，才允许结合用户描述生成一层模糊关键词，并且必须限制在已命中的 TAG/包名/组件范围内。

## 标准流程

1. 从 `claude_web_paths.config.json` 确认目标 `bundle_id` 和项目路径。
2. 运行生成命令，扫描 Java/Kotlin/XML/Gradle/native 文件，抽取 1 类精确日志证据和 2 类项目身份/功能范围信号。
3. 运行 `validate`，确保 JSON、schema、正则、bundle id 和规则 id 合法。
4. 用样例日志或最近一次 Android 分析 artifacts 运行 `test`，确认规则可命中。
5. 如需人工微调，用 `list/get/add/update/delete` 做维护。

## 常用命令

在仓库根目录执行：

```bash
python skills/android-log-rule-builder/scripts/rule_pack_manager.py generate --bundle-id android-rdm
python skills/android-log-rule-builder/scripts/rule_pack_manager.py generate --bundle-id app-demo --project-preset app
python skills/android-log-rule-builder/scripts/rule_pack_manager.py generate --bundle-id app-demo --project-preset app --profile functional --profile stability
python skills/android-log-rule-builder/scripts/rule_pack_manager.py generate --bundle-id app-ndk-samples --project-preset native --profile stability --profile performance
python skills/android-log-rule-builder/scripts/rule_pack_manager.py validate --bundle-id android-rdm --rule-pack-id rdm-generated
python skills/android-log-rule-builder/scripts/rule_pack_manager.py list --bundle-id android-rdm
python skills/android-log-rule-builder/scripts/rule_pack_manager.py test --bundle-id android-rdm --rule-pack-id rdm-generated --log-path temp/PNM-N49-2.zip
python skills/android-log-rule-builder/scripts/rule_pack_manager.py evaluate --eval-root android_analysis_eval
python skills/android-log-rule-builder/scripts/bootstrap_eval_repos.py --repo android/nowinandroid
```

`test` 对压缩包只会做轻量文本扫描，不替代完整 Android 分析流程；需要真实链路验证时，应把日志包上传到 Web 页面再触发 Android 分析。

`generate` 未显式传 `--rule-pack-id` 时统一使用 `<bundle-short-name>-generated`，例如 `android-rdm` 会生成 `rdm-generated`。生成后脚本会自动把该规则包写入 `android_analysis_knowledge/bundles/<bundle_id>/bundle.json` 的 `rule_packs`，确保后续 Android 分析默认加载它。

`--profile` 用于按问题类型生成额外规则，可重复传入：`functional`、`stability`、`xts`、`memory`、`performance`。脚本会把 profile 写入规则包 `metadata.profiles`，并同步更新 `bundle.json` 的 `supported_profiles` 和 `profile_overrides`，供后续路由按问题类型选择规则包。

生成规则包可以写入 `metadata.deep_hints`，包括代码检索词、优先路径、相关 Skill、`CLAUDE.md` 候选和案例标签。Deep hints 不应被当作首轮匹配规则；Deep 分析阶段会先参考这些线索，再按项目指南逐层扩大到代码和日志上下文。

评测集位于 `android_analysis_eval/`。外部开源项目使用 `bootstrap_eval_repos.py` 下载到 `tests/github_apps/`，该目录不会提交 Git。网络不稳定时可传 `--proxy http://127.0.0.1:1080`。当前评测覆盖普通 App、native-heavy App 和复杂 App：Now in Android、AntennaPod、ndk-samples、Nextcloud、Thunderbird、Termux。

`--project-preset native` 用于 NDK/native-heavy 项目，会扫描 C/C++、CMake、Android.mk、JNI 入口、so 名、native log tag 和 trace/render 信号，并生成 1 类 native `TAG/message` 精确规则与 2 类 native scope 规则。

`--project-preset app` 会抽取 Manifest、permission、action、组件、包名、真实 TAG 和项目日志文本。同步/账号、后台任务、进程/终端等能力只进入 Deep hints 和 2 类真实 scope，不再把 `sync failed`、`service error` 这类语义短语写成首轮规则。

## 前置工作流关键词策略

前置工作流最多加载 1/2 类关键词：

1. 首轮严格搜索：只使用 1 类精确日志证据，以及少量 Android 基础规则，例如 lifecycle、crash、ANR、tombstone、permission denial、XTS failure、trace/slow log。
2. 第二轮扩大搜索：加入 2 类项目身份和功能范围信号，用于扩大候选日志窗口。
3. 不加载 3 类代码检索词作为日志关键词。它们只给 Deep 读代码使用。
4. 不生成 4 类模糊语义关键词。模糊关键词必须由 Deep 在低置信度时按需产生，并记录触发原因。

## 维护原则

- 规则必须带 `source_bundle_ids`，避免误把无关项目日志当成业务证据。
- 首轮证据规则优先使用 1 类 `TAG + 日志文本/regex`；扩大范围规则只能使用 2 类项目强相关信号。
- 不要把业务摘要词、中文功能描述、`error`、`fail`、`exception`、`lock`、`sync`、`network` 等泛词单独放入首轮规则。
- 如果只能抽到类名/目录名/业务词，把它们写入 `metadata.deep_hints`，不要写成 `match.keywords`。
- Regex 要尽量短且可解释；所有 regex 必须通过 `validate` 编译检查。
- 删除或改写规则前，先用 `get` 查看当前规则，再用 `test` 验证修改后的命中。

## 参考

- 规则 schema：读取 `references/rule_pack_schema.md`
- 代码扫描策略：读取 `references/android_source_scan_patterns.md`
- 评测 case 结构：读取 `references/evaluation_cases.md`
