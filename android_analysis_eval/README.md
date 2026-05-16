# Android 分析评测集

这个目录保存 Android Rule Builder Skill 的轻量评测元数据。外部 GitHub 项目代码、大日志、trace、hprof 等不要提交到仓库。

## 目录约定

```text
android_analysis_eval/
  repos.json
  cases/
    <case-id>/
      case.json
      expected.json
      question.md
      notes.md
      sample.log
```

外部项目下载到：

```text
tests/github_apps/<owner>__<repo>/
```

大日志和设备导出物放到：

```text
tests/android_eval_artifacts/
```

这两个目录已经在 `.gitignore` 中排除。

## 运行方式

先准备外部仓库。可以手动 shallow clone，例如：

```bash
git clone --depth 1 --filter=blob:none https://github.com/android/nowinandroid tests/github_apps/android__nowinandroid
git clone --depth 1 --filter=blob:none https://github.com/AntennaPod/AntennaPod tests/github_apps/AntennaPod__AntennaPod
git clone --depth 1 --filter=blob:none https://github.com/android/ndk-samples tests/github_apps/android__ndk-samples
```

也可以使用辅助脚本：

```bash
python skills/android-log-rule-builder/scripts/bootstrap_eval_repos.py
```

如果 GitHub 访问不稳定，可显式使用本机代理：

```bash
python skills/android-log-rule-builder/scripts/bootstrap_eval_repos.py --proxy http://127.0.0.1:1080
```

然后运行：

```bash
python skills/android-log-rule-builder/scripts/rule_pack_manager.py --json evaluate --eval-root android_analysis_eval --knowledge-dir tests/android_eval_artifacts/generated_knowledge
```

`case.json` 自带 bundle 元数据，所以评测不要求先修改本机 `claude_web_paths.config.json`。如果本机配置里已经有相同 bundle id，会优先使用本机配置。

## 当前范围

当前第一版接入 6 个开源项目：

- `android/nowinandroid`：功能、性能。
- `AntennaPod/AntennaPod`：功能、稳定性。
- `android/ndk-samples`：native preset、JNI/so/tombstone/trace、性能入口。
- `nextcloud/android`：账号鉴权、WebDAV/RemoteOperation、文件同步、后台上传/下载。
- `thunderbird/thunderbird-android`：账号创建、OAuth/IMAP mail sync、数据库升级、消息列表。
- `termux/termux-app`：`RUN_COMMAND`、shell/process、terminal session、前台服务。

这些 case 目前使用小型合成日志做轻量规则命中验证。`ndk-samples` 两个 case 已切到 `project_preset = native`，用于验证 C/C++/CMake/JNI/so/trace 信号。阶段 6 后，评测集共 12 个 case，复杂 App case 重点验证 `sync-account-signals`、`background-task-signals`、`process-terminal-signals` 三类分组规则。后续可以把真实设备日志放到 `tests/android_eval_artifacts/`，再在 case 中把 `log_path` 指向对应文件。
