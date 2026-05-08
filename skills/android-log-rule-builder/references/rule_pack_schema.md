# 规则包 Schema

规则包文件支持单包格式：

```json
{
  "version": 1,
  "id": "rdm-base",
  "title": "RDM Base Signals",
  "description": "规则包说明",
  "source_bundle_ids": ["android-rdm"],
  "rules": []
}
```

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
  "id": "rdm-lock-failure",
  "title": "RDM lock failure",
  "issue_type": "android_business_spec",
  "severity": "medium",
  "source_bundle_ids": ["android-rdm"],
  "tags": ["rdm", "lock"],
  "match": {
    "kinds": ["android_main_log"],
    "paths": ["logcat", "main.log"],
    "keywords": ["RDM", "DeviceLock", "lock failed"],
    "packages": ["com.example.rdm"],
    "regex": ["(?i)DeviceLock.*(fail|error|exception)"]
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
