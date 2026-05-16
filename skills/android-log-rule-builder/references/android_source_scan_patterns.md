# Android 项目扫描策略

第一版脚本只做轻量静态扫描，不构建项目。

扫描文件：

```text
.java .kt .kts .gradle .gradle.kts .xml .properties .aidl CLAUDE.md AGENTS.md
```

忽略目录：

```text
.git .gradle .idea build out target node_modules .cxx .externalNativeBuild
```

## 通用抽取内容

- Java/Kotlin package、Gradle namespace/applicationId、AndroidManifest package。
- `TAG = "..."`、`LOG_TAG`、`Log.d/i/w/e/wtf(...)`、`Slog.*(...)`、`Timber.*(...)`、`EventLog.writeEvent(...)`、私有日志封装、native `__android_log_print` / `ALOG*` / `LOG*`。
- Manifest 中 activity/service/receiver/provider 名称。
- 类名、枚举/常量名、包含业务词的路径片段只进入 Deep hints，不作为首轮日志关键词。
- 配置 bundle 的 `keywords`、`title`、`summary` 会作为业务词种子。

生成规则时，前置工作流只生成两层：

1. 1 类：真实日志调用中的 `TAG + message` 正则。
2. 2 类：真实 package、TAG、component、action、permission、native symbol/trace 范围信号。

业务词、中文功能描述、类名和目录名进入 `metadata.deep_hints`，由 Deep 分析在读取 `CLAUDE.md` / 项目专属 Skill / 源码后再使用，不能直接写成首轮 `match.keywords`。

## APP Preset

`generate --project-preset app` 会额外抽取普通 Android App 结构信号：

- Gradle module、namespace、applicationId。
- Manifest permission、intent action、provider authorities、meta-data。
- WorkManager / Worker / WorkRequest。
- Retrofit / OkHttp / HttpURLConnection / WebSocket。
- Room / SQLite / DataStore / SharedPreferences / Repository。
- FileProvider / ContentResolver / MediaStore / DocumentFile。
- NotificationManager / NotificationCompat / NotificationChannel。
- BuildConfig / RemoteConfig / FeatureFlag / Preference / Setting / Config。
- 复杂 App 信号：
  - SyncAdapter / SyncResult / RemoteOperation / upload / download。
  - AccountManager / Authenticator / OAuth / Token / Login / WebDAV。
  - JobScheduler / ForegroundService / BroadcastReceiver / Worker。
  - ProcessBuilder / exec / TermuxService / RunCommandService / ExecutionCommand / TerminalSession / shell。
  - IMAP / SMTP / MailStore / MessageList / Folder / Mailbox。

APP preset 生成的规则包应包含：

- `tier1-exact-log-*`：源码日志调用中的精确 `TAG + message`。
- `tier2-project-scope`：package、TAG、component、action、permission、Gradle module 等范围信号。
- `tier2-permission-scope` / `tier2-android-components`：按需生成的更窄范围规则。
- profile 规则：必须把稳定性、XTS、内存、性能等通用现象与项目 scope 写进同一 regex，避免单独命中泛词。

复杂 App 的同步/账号、后台任务、进程/终端能力会进入 Deep hints 与项目专属 Skill，默认不再单独生成 `sync failed` 这类语义规则。

## Native Preset

`generate --project-preset native` 会额外扫描 native-heavy Android 项目：

- C/C++/Header：`.c`、`.cc`、`.cpp`、`.cxx`、`.h`、`.hh`、`.hpp`、`.hxx`。
- 构建文件：`CMakeLists.txt`、`.cmake`、`Android.mk`、`Application.mk`、Gradle。
- JNI 入口：`JNI_OnLoad`、`Java_*`、`RegisterNatives`、`ANativeActivity_onCreate`、`android_main`。
- native library：`add_library(...)`、`target_link_libraries(...)`、`LOCAL_MODULE`、`lib*.so`。
- native log tag：`LOG_TAG`、`__android_log_print(...)`。
- trace/render 信号：`ATrace_beginSection`、`ATRACE_NAME`、`AChoreographer_*`、`ANativeWindow_*`。
- 崩溃入口词：`SIGSEGV`、`SIGABRT`、`tombstone`、`backtrace`、abort/error/failure 相关符号。

Native preset 会额外生成 1 类 native 精确日志和 2 类 native scope：

- `<rule-pack-id>-tier1-exact-log-*`
- `<rule-pack-id>-tier2-native-scope`

这些规则用于把 tombstone、native crash、trace/perfetto 或帧率日志关联回项目的 so、JNI 方法、trace section 和 native log tag。后续可继续增强 `nm`/符号表、tombstone frame 归一化和 ABI 维度。

## Profile-aware 规则

`generate --profile ...` 会在基础规则之外追加面向问题类型的规则：

| Profile | 额外规则重点 |
|---|---|
| `functional` | 2 类项目 scope，配合 1 类 exact logs 做首轮功能候选 |
| `stability` | 项目 scope 与 `FATAL EXCEPTION`、`AndroidRuntime`、`ANR`、`Caused by`、tombstone 等共现 |
| `xts` | 项目 scope 与 `CTS/GTS/XTS`、Tradefed、AssertionError、测试失败文本共现 |
| `memory` | 项目 scope 与 OOM、LMKD、meminfo、smaps、hprof、PSS/RSS、GC 信号共现 |
| `performance` | 项目 scope 与 Choreographer、Skipped frames、slow/jank、trace/Perfetto、binder latency 共现 |

Profile 规则要复用项目 scope，但不能只靠泛词命中。比如稳定性规则可以包含 `FATAL EXCEPTION`，但必须与项目包名、TAG、组件或 native symbol 在同一 regex 中共现，避免把无关应用 crash 当作当前项目证据。

## Deep hints

生成脚本会额外输出 `metadata.deep_hints`，给 Deep 分析阶段使用：

- `code_search_terms` 来自 TAG、组件、业务词、错误词、App capability、native symbol/trace section。
- `preferred_paths` 来自 Manifest、Gradle、`src/main/java`、`src/main/kotlin`、`src/main/cpp`、模块根目录以及 `CLAUDE.md` / `AGENTS.md`。
- `related_skills` 默认包含 `android-log-rule-builder`，并追加 project preset 与 profile，例如 `android-log-rule-builder:app`、`android-log-rule-builder:stability`。
- `claude_md_candidates` 只记录候选路径，由 Deep 阶段按命中 bundle 和问题相关性按需加载。
- `case_tags` 用于案例召回和后续评测分组，既包含 profile，也包含同步、账号、后台任务、native 等能力标签。

Deep 阶段的优先级是：相关 Skill / 项目指南优先，其次使用规则包 hints 和白名单代码路径，再扩大到更通用的日志/代码上下文。
