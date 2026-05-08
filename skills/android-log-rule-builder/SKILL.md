---
name: android-log-rule-builder
description: 为 claude-web-server 的 Android 问题分析功能生成、校验、查询和维护项目日志规则包；当需要为某个 claude_web_paths.config.json bundle 扫描 Android 项目代码、抽取日志 TAG/业务关键词/包名/组件名、创建 android_analysis_knowledge 规则包，或对规则包做 list/get/add/update/delete/test 时使用。
---

# Android Log Rule Builder

使用本 skill 为特定 Android 项目生成和维护日志规则包。规则包会写入本机知识目录：

```text
android_analysis_knowledge/bundles/<bundle_id>/rules/<rule_pack_id>.json
```

该目录默认不提交 GitHub。生成的规则会被 `claude_web.android_analysis.rule_loader` 按需加载，并参与 Android 分析阶段 4 的本地规则匹配。

## 标准流程

1. 从 `claude_web_paths.config.json` 确认目标 `bundle_id` 和项目路径。
2. 运行生成命令，扫描 Java/Kotlin/XML/Gradle 等文件，抽取日志 TAG、包名、组件名、业务词、错误词。
3. 运行 `validate`，确保 JSON、schema、正则、bundle id 和规则 id 合法。
4. 用样例日志或最近一次 Android 分析 artifacts 运行 `test`，确认规则可命中。
5. 如需人工微调，用 `list/get/add/update/delete` 做维护。

## 常用命令

在仓库根目录执行：

```bash
python skills/android-log-rule-builder/scripts/rule_pack_manager.py generate --bundle-id android-rdm --rule-pack-id rdm-generated
python skills/android-log-rule-builder/scripts/rule_pack_manager.py validate --bundle-id android-rdm --rule-pack-id rdm-generated
python skills/android-log-rule-builder/scripts/rule_pack_manager.py list --bundle-id android-rdm
python skills/android-log-rule-builder/scripts/rule_pack_manager.py test --bundle-id android-rdm --rule-pack-id rdm-generated --log-path temp/PNM-N49-2.zip
```

`test` 对压缩包只会做轻量文本扫描，不替代完整 Android 分析流程；需要真实链路验证时，应把日志包上传到 Web 页面再触发 Android 分析。

## 维护原则

- 规则必须带 `source_bundle_ids`，避免误把无关项目日志当成业务证据。
- 规则应优先匹配“业务词 + 项目包名/TAG/组件名”，不要只用 `error`、`fail` 这类泛词做强结论。
- Regex 要尽量短且可解释；所有 regex 必须通过 `validate` 编译检查。
- 删除或改写规则前，先用 `get` 查看当前规则，再用 `test` 验证修改后的命中。

## 参考

- 规则 schema：读取 `references/rule_pack_schema.md`
- 代码扫描策略：读取 `references/android_source_scan_patterns.md`
