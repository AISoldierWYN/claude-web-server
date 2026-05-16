# 规则包 Schema

规则包文件支持单包格式：

```json
{
  "version": 1,
  "id": "rdm-generated",
  "title": "RDM Generated Signals",
  "description": "规则包说明",
  "source_bundle_ids": ["android-rdm"],
  "rules": []
}
```

自动生成规则包默认命名为 `<bundle-short-name>-generated`，例如 `android-rdm` 会生成 `rdm-generated`。生成脚本会把该 id 写入 `android_analysis_knowledge/bundles/<bundle_id>/bundle.json` 的 `rule_packs`，由 `bundle.json` 决定默认加载哪些规则包。

如果生成时传入 `--profile`，规则包的 `metadata.profiles` 会记录问题类型：

```json
{
  "metadata": {
    "generator": "skills/android-log-rule-builder",
    "project_preset": "app",
    "profiles": ["functional", "stability"],
    "deep_hints": {
      "version": 1,
      "code_search_terms": ["FileSyncService", "DeviceLock"],
      "preferred_paths": ["app/src/main/java", "CLAUDE.md"],
      "related_skills": ["android-log-rule-builder", "android-log-rule-builder:app"],
      "claude_md_candidates": ["CLAUDE.md"],
      "case_tags": ["functional", "sync"]
    }
  }
}
```

同时 `bundle.json` 会写入：

```json
{
  "supported_profiles": ["functional", "stability"],
  "profile_overrides": {
    "functional": {"rule_packs": ["rdm-generated"], "issue_type": "android_business_spec"},
    "stability": {"rule_packs": ["rdm-generated"], "issue_type": "android_app_crash"}
  }
}
```

这些字段用于后续 Android 分析路由，不改变单条规则的基本 schema。

自动生成规则包还会写入信号分层策略：

- `metadata.exact_logs`：1 类精确日志证据，来自源码中的真实日志调用，形如 `TAG + message`。
- `metadata.tier2_scope_terms`：2 类项目范围信号，来自真实 package、TAG、component、action、permission、native symbol/trace。
- `metadata.deep_hints`：3/4 类 Deep 辅助线索，不直接参与首轮规则匹配。

`metadata.deep_hints` 用于 Deep 分析阶段，不直接参与首轮规则匹配：

- `search_order`：Deep 必须遵守的 1/2/3/4 类逐层扩大顺序。
- `code_search_terms`：Deep 读代码时优先检索的类名、TAG、业务词、错误词、native symbol 等。
- `preferred_paths`：Deep 在白名单代码目录内优先读取的相对路径，支持目录或 `CLAUDE.md` / `AGENTS.md` 文件。
- `related_skills`：提示 Deep 先参考哪些 Skill 或 Skill 子能力。
- `claude_md_candidates`：可按需加载的项目指导文件候选，避免一次性读取所有知识。
- `case_tags`：历史案例召回或后续评测分组使用的标签。

也支持内置通用规则使用的多包格式：

```json
{
  "version": 1,
  "rule_packs": [
    {"id": "android-base", "rules": []}
  ]
}
```

单条规则字段：

```json
{
  "id": "rdm-generated-tier1-exact-log-1",
  "title": "RDM tier1 exact TAG/message logs #1",
  "issue_type": "android_business_spec",
  "severity": "medium",
  "source_bundle_ids": ["android-rdm"],
  "tags": ["tier1", "exact-log", "tag-message"],
  "match": {
    "regex": [
      "(?i)(?:(?<![A-Za-z0-9_])DeviceStateManager(?![A-Za-z0-9_])[\\s\\S]{0,240}Enforce\\s+policy\\s+success|Enforce\\s+policy\\s+success[\\s\\S]{0,240}(?<![A-Za-z0-9_])DeviceStateManager(?![A-Za-z0-9_]))"
    ]
  }
}
```

必填：`id`、`title`、`issue_type`、`match`。`match` 至少包含 `keywords`、`packages`、`regex`、`paths`、`kinds` 中的一个非空数组。

推荐 issue type：

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

推荐 severity：`fatal`、`high`、`medium`、`low`。
