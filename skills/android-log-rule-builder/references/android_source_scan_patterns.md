# Android 项目扫描策略

第一版脚本只做轻量静态扫描，不构建项目。

扫描文件：

```text
.java .kt .kts .gradle .gradle.kts .xml .properties .aidl
```

忽略目录：

```text
.git .gradle .idea build out target node_modules .cxx .externalNativeBuild
```

抽取内容：

- Java/Kotlin package、Gradle namespace/applicationId、AndroidManifest package。
- `TAG = "..."`、`Log.d/i/w/e/wtf(...)`、`Slog.*(...)`、`Timber.*(...)`。
- Manifest 中 activity/service/receiver/provider 名称。
- 类名、枚举/常量名、包含业务词的路径片段。
- 配置 bundle 的 `keywords`、`title`、`summary` 会作为业务词种子。

生成规则时，应把业务词和项目身份词组合使用。比如 RDM 项目更应该生成 `RDM + lock/unlock/provision/device policy` 相关规则，而不是泛泛匹配所有 `Exception`。
