# Android 分析专家工作台重构计划

## 背景

当前 Android 问题分析链路已经具备日志上传、解压、采样、规则匹配、案例召回、首轮报告、Deep 分析、可视化过程和测试集评测能力。但最近基于 `D:\Code\FWK\testcase.xlsx` 的人工标注集评测暴露出一个核心问题：

规则链路如果试图直接“诊断根因”，复杂度会快速上升，并且容易在新问题、新模块、厂商定制日志、跨模块问题上失效。相反，直接给 AI 相关源码、模块 skill、全量日志和问题描述，让 AI 按开发人员排查方式发散分析，效果反而更好。

因此后续架构要从“规则诊断系统”调整为“AI 专家排查工作台”：

- 前置工作流负责分类、证据召回、日志释义、历史案例召回和上下文整理。
- Deep 分析负责完整专家推理，允许读相关源码、日志、模块 skill、CLAUDE.md/AGENTS.md 和经验库。
- 规则不再追求覆盖根因，只负责把 AI 带到正确现场。

## 总目标

1. 每次 Android 分析都执行前置工作流和 Deep 分析。
2. 前置工作流只做“导诊和证据整理”，不承担最终根因诊断。
3. Deep 分析基于前置 Evidence Pack、模块源码、模块 skill、项目指南、全量日志和历史案例进行专家式分析。
4. 通过模块/小类定义、证据模板、经验库和历史案例沉淀开发人员知识，降低新增问题的规则维护成本。
5. 保留可观测性：每一步的输入、输出、命中关键词、召回案例、token、耗时都可记录和回放。

## 非目标

- 不继续把规则引擎扩展成完整专家系统。
- 不把所有日志全文直接塞进前置 prompt。
- 不要求前置工作流给出高置信最终根因。
- 不要求第一版实现复杂 UI 管理页面；知识库先用本地 JSON/Markdown/YAML 文件维护。
- 不改变普通聊天、Gemini、移动开发等现有功能逻辑。

## 架构原则

### 1. 分类先行，且只看用户描述

问题分类阶段只使用用户原始问题描述，不加载日志、源码、skill、历史案例，避免被噪声带偏。

分类输出固定为结构化 JSON，并且必须返回候选列表，不只返回单一结果：

```json
{
  "module_id": "framework",
  "module_confidence": 0.78,
  "submodule_id": "activity_start",
  "submodule_confidence": 0.64,
  "profile": "functional",
  "top_candidates": [
    {
      "module_id": "framework",
      "submodule_id": "activity_start",
      "score": 0.64,
      "reason": "用户描述涉及应用启动、Activity 跳转或前后台切换"
    },
    {
      "module_id": "app",
      "submodule_id": "unknown",
      "score": 0.31,
      "reason": "可能是应用自身崩溃或兼容问题"
    }
  ],
  "need_submodule": true,
  "need_user_clarification": false,
  "package_candidates": [],
  "time_window_hint": null
}
```

分类规则：

- `module_confidence < 0.5` 时归为 `others/unknown`。
- `submodule_confidence < 0.6` 时只保留大类，`submodule_id = "unknown"`。
- 如果 `top1/top2` 分差过小，例如小于 `0.15`，不要强行收敛到单一小类，应保留多个候选并让后续模板选择按候选并集加载。
- Android 问题经常跨模块，分类结果必须保留候选和置信度，避免过早单分类导致后续模板、源码和 skill 加载错误。

### 2. 证据模板替代模糊关键词

原来的“关键词”概念升级为“证据模板”。每条模板必须是可解释、可回溯、可维护的结构化证据。

建议字段：

| 字段 | 说明 |
|---|---|
| `id` | 稳定 ID |
| `module_id` | 所属模块 |
| `subcategory_id` | 所属问题小类，可为空 |
| `profile` | functional / stability / xts / memory / performance |
| `log_type` | logcat / dropbox / xts_report / anr / trace / meminfo 等 |
| `regex` | 精确正则，优先 `TAG + message` |
| `code_location` | 代码位置，如类、方法、文件路径 |
| `meaning` | 命中后的中文含义 |
| `severity` | info / suspicious / warning / critical |
| `time_anchor` | 是否可作为时间线锚点 |
| `next_steps` | 命中后建议继续查什么 |
| `parameters` | 可选参数占位符，如 `$package_name`、`$uid`、`$component` |

证据模板应避免只写“lock/unlock/网络异常”等纯语义模糊词。若需要语义扩展，只能在 Deep 阶段作为兜底扩展，不进入前置严格搜索。

第一版前置工作流只处理**单行即可确定含义**的证据模板。例如 `TAG + message`、明确错误码、明确状态字段、明确成功/失败回调。需要向下读取堆栈、结合上下文多行判断、推断调用链的复杂场景，不放在前置工作流中强行解析，交给 Deep 分析读取全量日志和源码处理。

日志搜索必须先判断日志类型，再在对应类型的文件中搜索。相同正则在不同日志文件中可能含义不同，因此证据模板只声明 `log_type`，由日志类型识别器决定哪些文件属于该类型。

证据模板允许使用参数占位符，例如 `$package_name`。占位符必须在“包名/参数提取”阶段解析后才能执行搜索。如果某个参数有多个候选值，搜索时需要展开为多条具体正则，或用安全的正则分组 `(?:pkg1|pkg2)` 组合；每次展开都要记录参数来源和置信度。

为了方便人工维护，证据模板必须支持 JSONL 与 CSV/XLSX 双向转换。人优先维护表格，代码启动或构建时转换成 JSONL/缓存使用。

### 3. 历史案例只做参考，不直接套结论

案例召回基于用户问题描述、模块分类、小类分类和关键证据做相似度计算，返回 Top K。

前置工作流可以从历史案例中提取“曾经使用过的证据模板/日志 TAG/排查路径”，加入候选搜索范围。但报告中必须区分：

- 本次日志已证实。
- 历史案例提示。
- Deep 仍需验证。

### 4. Deep 必须可推翻前置结论

前置分析摘要只作为参考，Deep prompt 必须明确允许推翻：

> 初步分析仅供参考。如果全量日志、源码或 skill 证据不支持该结论，应明确推翻并说明原因。

### 5. 模块 skill 是长期维护核心

每个模块维护一个或多个分析 skill，用来承载开发人员经验：

- 模块业务流程。
- 常见问题小类。
- 精确日志含义。
- 正常/异常状态链。
- 先查什么，后查什么。
- 什么结论不能轻易下。
- 非本模块问题时应该交给哪个领域继续分析。

规则包只负责召回证据；skill 负责指导 AI 如何像开发人员一样分析。

### 6. 项目强相关知识放在项目目录内

模块分类、小类、证据模板、模块经验日志、模块 skill、CLAUDE.md/AGENTS.md 都应尽量放在对应项目代码根目录下，形成固定格式的项目知识包。`claude_web_server` 只负责按 `claude_web_paths.config.json` 中配置的项目路径扫描、校验、缓存和调用。

全局知识只保留跨项目通用内容，例如 Android 基础 profile、OEM 厂商经验日志、通用日志类型识别模板和通用测试集评估脚本。

## 新知识库结构

项目强相关知识优先放在对应项目根目录，采用固定、简单、可打包的文件树。`claude_web_server` 在启动时根据 `claude_web_paths.config.json` 的项目路径扫描这些文件，校验后加载到内存缓存；后续分析任务只从缓存读取，避免每次重新扫盘。

推荐项目内结构：

```text
<project_root>/
  CLAUDE.md                         # 项目总体指南，可选
  AGENTS.md                         # Agent/Codex 约定，可选
  skills/
    <module-id>-analysis/
      SKILL.md                      # 模块问题分析 skill，可选但推荐
  .claude-web/
    android-analysis/
      module.json                   # 模块大类定义
      subcategories.json            # 模块内部问题小类
      evidence_templates.jsonl      # 单行确定证据模板
      evidence_templates.csv        # 证据模板表格维护格式，可选
      evidence_templates.xlsx       # 证据模板表格维护格式，可选
      experience_logs.jsonl         # 模块经验日志，可人工补充
      log_types.json                # 项目特殊日志类型识别，可选
      cases/
        case_cards.jsonl            # 历史案例摘要
      README.md                     # 给维护者看的说明，可选
```

全局共享结构仍放在 `claude_web_server` 本地知识目录中，主要承载跨项目内容：

```text
android_analysis_knowledge/
  global/
    profiles/
      functional.json
      stability.json
      xts.json
      memory.json
      performance.json
    log_types/
      android_logcat.json
      dropbox.json
      xts_report.json
      anr_trace.json
      tombstone.json
      trace.json
      meminfo.json
    oem_experience/
      honor_iaware.jsonl
      trustsbase.jsonl
    app_inventory/
      app_inventory.schema.json
```

启动缓存建议：

```text
server start
  -> 读取 claude_web_paths.config.json
  -> 扫描每个项目的 .claude-web/android-analysis/
  -> 加载 module/subcategory/evidence/case/log_type/experience
  -> 合并全局 profiles/log_types/oem_experience
  -> 写入只读内存缓存
  -> 提供 list/debug API 便于验收
```

第一版不迁移旧规则包。旧规则包只作为人工参考，不自动导入新知识包，避免把历史混乱规则继续带入新架构。

### `module.json`

```json
{
  "id": "android-rdm",
  "title": "RealtimeDeviceManager",
  "description": "设备实时锁定、激活、check-in、EULA、push token、云侧指令响应相关模块。",
  "source_roots": ["."],
  "skill_paths": ["skills/rdm-log-analysis/SKILL.md"],
  "guide_paths": ["CLAUDE.md", "AGENTS.md"],
  "default_package_names": ["com.hihonor.realtimedevicemanager"],
  "package_resolution": {
    "required": false,
    "reason": "RDM 模块自己的业务日志默认使用固定包名，不需要每次从用户描述中提取应用包名。"
  },
  "profiles": ["functional", "stability"]
}
```

### `subcategories.json`

```json
[
  {
    "id": "activation_eula",
    "title": "激活/EULA 协议页",
    "description": "设备在线激活、声明页、协议页、EULA 配置和 check-in 响应处理相关问题。",
    "aliases": ["协议页缺失", "声明页后无协议", "EULA", "激活页面异常"]
  }
]
```

### `evidence_templates.jsonl`

前置工作流只使用单行确定规则。每行一个 JSON，命中后可以直接翻译成自然语言证据。

```json
{"id":"rdm-checkin-eula-missing","module_id":"android-rdm","subcategory_id":"activation_eula","profile":"functional","log_type":"logcat","regex":"\\bDeviceLockSchedulerImpl\\b.*\\bhas eula:false\\b","code_location":"DeviceLockSchedulerImpl#processCheckInResult","meaning":"check-in 结果中没有 EULA 配置，协议页无法展示。","severity":"critical","time_anchor":true,"next_steps":["检查同一时间点 RDMQueryService checkin onSuccess 的服务端响应","确认 UI 层是否因此跳过协议页"]}
```

约束：

- `regex` 必须尽量绑定真实 TAG、错误码、状态字段或稳定 message。
- 不写纯语义词，如 `锁机失败`、`网络异常`、`没有响应`。
- 不写需要多行堆栈才能判断的规则。`FATAL EXCEPTION`、ANR trace、tombstone 等多行分析交给 Deep。
- `log_type` 必须来自全局或项目 `log_types.json`，搜索时只在匹配该类型的文件中执行。

### `evidence_templates.csv` / `evidence_templates.xlsx`

人工维护优先使用表格格式，工具负责和 JSONL 相互转换。建议列：

| 列名 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 稳定模板 ID |
| `module_id` | 是 | 模块 ID |
| `submodule_id` | 否 | 小类 ID，未知可空 |
| `profile` | 是 | functional / stability / xts / memory / performance |
| `log_type` | 是 | 只在该类型日志中搜索 |
| `regex` | 是 | 单行正则，可包含 `$package_name` 等占位符 |
| `parameters` | 否 | 逗号分隔的占位符名称 |
| `code_location` | 否 | 类/方法/文件 |
| `meaning` | 是 | 命中后的中文释义 |
| `severity` | 是 | info / suspicious / warning / critical |
| `time_anchor` | 否 | true/false |
| `next_steps` | 否 | 分号分隔 |
| `enabled` | 否 | false 时不加载 |

转换要求：

- `xlsx/csv -> jsonl` 时做 schema 校验、正则编译校验、参数占位符校验。
- `jsonl -> xlsx/csv` 用于导出审阅。
- 如果同目录同时存在表格和 JSONL，以表格为人工源，启动时可校验两者是否一致；不一致时在日志中提示。
- 模板选择阶段读取缓存中的 JSON 结构，不直接读 Excel；Excel 只作为人工维护格式。

### `log_types.json`

项目可以补充特殊日志类型；全局已有的 logcat/dropbox/xts/anr/tombstone/trace 等不需要重复写。

```json
[
  {
    "id": "rdm_business_log",
    "title": "RDM 业务日志",
    "path_patterns": ["(?i)(rdm|realtimedevicemanager|pid_\\d+).*\\.(txt|log)$"],
    "content_patterns": ["\\bDeviceLockSchedulerImpl\\b", "\\bRDMQueryService\\b"],
    "priority": 80,
    "notes": "RDM 业务运行日志，优先用于 check-in、EULA、push token、provision 状态分析。"
  }
]
```

日志类型识别规则：

1. 先按路径/文件名正则粗分。
2. 再抽样文件头、中段、尾部内容做内容正则确认。
3. 同一文件可归入多个类型，但搜索时优先使用证据模板声明的 `log_type`。
4. 无法识别的文件只进入 Deep 可读范围，不参与前置单行证据扫描。

### `cases/case_cards.jsonl`

历史案例为后续 RAG 做准备，必须有摘要字段。第一版可以用简单文本相似度，后续只对 `embedding_text` 计算 embedding。

```json
{"case_id":"rdm-eula-missing-001","module_id":"android-rdm","subcategory_id":"activation_eula","profile":"functional","summary":"恢厂后在线激活，声明页后无协议页，根因是服务端未配置 EULA。","embedding_text":"RDM 在线激活 协议页缺失 EULA has eula false 服务端未配置","key_evidence":["DeviceLockSchedulerImpl has eula:false","RDMQueryService checkin onSuccess"],"root_cause":"服务端未配置 EULA，check-in 返回 CommonConfig.getEula 为空。","used_template_ids":["rdm-checkin-eula-missing"],"handoff_domains":["RDM 服务端配置"]}
```

### `experience_logs.jsonl`

用于厂商特殊日志和开发经验沉淀。

```json
{"id":"honor-iaware-appstart-alw0","pattern":"AwareLog: AppStart:.*alw:0","meaning":"荣耀 iaware 后台启动拦截，alw:0 表示不允许后台拉起目标进程或组件。","typical_impact":"广播冷启动失败、服务拉起失败、XTS 等待事件超时。","owner_domain":"FWK/OEM iaware","next_steps":["确认被拦截的 package/component","对齐 TestRunner started/failed 时间线","检查是否为冷启动场景"]}
```

## 前置工作流设计

### 输入

- 用户原始问题描述。
- 上传日志路径。
- 用户可选模块提示。
- 全局模块/小类定义摘要。

每个环节都必须有独立产物，便于单独验收和定位问题：

| 环节 | 输入 | 输出 | 独立验收点 |
|---|---|---|---|
| 问题分类 | 用户原始描述、模块/小类摘要 | `classification.json` | 不读日志/源码也能给出候选模块、小类和置信度 |
| 包名/参数提取 | 分类结果、模块默认包名、应用清单、用户描述 | `parameter_resolution.json` | 判断是否需要包名，并提取一个或多个候选包名 |
| 证据模板选择 | 分类结果、模块知识缓存 | `selected_evidence_templates.json` | 能解释为什么选这些模板 |
| 案例召回 | 用户描述、分类、可选证据模板 | `case_recall.json` | Top K 案例有摘要、相似度和使用过的模板 |
| 日志类型识别 | 文件树、全局/项目 log_types | `log_type_manifest.json` | 能说明每个文件属于哪些日志类型 |
| 日志搜索 | 日志类型清单、证据模板 | `annotated_evidence_timeline.md/json` | 只做单行确定匹配，输出原文和释义 |
| 工作流初步分析 | 分类、证据时间线、案例 | `workflow_report.md/json` | 区分已证实、案例提示和待 Deep 验证 |
| Deep 分析 | 前置产物、全量日志、源码、skill、指南 | `deep_report.md/json` | 可推翻前置结论，并记录搜索阶梯 |

### 阶段 1：模块和小类分类

只调用一次轻量 AI，不读日志和代码。

Prompt 结构：

```text
用户原始的问题描述为：
{{question}}

总共支持的问题模块包括：
{{module summaries}}

每个模块包含的问题小类包括：
{{subcategory summaries}}

请只根据用户描述返回最可能的问题模块、大类 profile 和问题小类。
如果小类不明确，请返回 unknown。
请输出 JSON。
```

### 阶段 2：包名/参数提取

包名/参数提取是可选阶段，由模块大类和小类决定是否需要。

典型规则：

- RDM 这类模块默认包名固定，可以从 `module.json.default_package_names` 直接获得，不需要额外语义提取。
- AMS/Activity 启动、PMS 安装、权限、跨应用调用等框架问题通常强依赖包名，需要从用户描述、应用清单和日志候选中提取一个或多个包名。
- 部分证据模板包含 `$package_name`、`$uid`、`$component` 等占位符，必须等参数解析完成后才能搜索。

数据来源：

1. 用户上传的设备应用清单 `app_inventory.json`。
2. 日志中的包名、进程名、Activity/Service/Provider 名称。
3. 历史经验库中的包名别名。
4. `module.json.default_package_names`。
5. 用户描述中的中文应用名、英文应用名或业务别名。

第一版只支持导入 JSON。后续可实现两个采集方式：

- ADB + `pm list packages` + `dumpsys package`。
- Android helper APK 使用 `PackageManager` 导出 `packageName / label / version / uid / launcherActivity`。

推荐长期方案是 helper APK，因为中文 label、launcherActivity、uid、版本信息最稳定。

输出 `parameter_resolution.json`：

```json
{
  "need_package_resolution": true,
  "default_package_names": [],
  "package_candidates": [
    {
      "package_name": "com.example.target",
      "label": "示例应用",
      "confidence": 0.82,
      "source": "app_inventory_semantic_match",
      "reason": "用户描述中的中文应用名与应用清单 label 匹配"
    }
  ],
  "resolved_parameters": {
    "package_name": ["com.example.target"],
    "uid": [],
    "component": []
  },
  "need_user_clarification": false
}
```

如果 `package_candidates` 有多个高置信候选，后续模板展开时按候选分别搜索，并在证据中记录命中了哪个包名。

### 阶段 3：证据模板选择

根据分类结果选择证据模板：

1. 如果 `subcategory_id` 明确：加载该小类模板。
2. 如果小类不明确：加载模块下所有小类模板去重集合。
3. 同时加载全局 profile 基础模板，如 crash、ANR、XTS、reboot、permission、trace 等。
4. 同时加载命中的 OEM 经验模板，如 iaware、trustsbase 等。

输出 `selected_evidence_templates.json`：

```json
{
  "module_id": "android-rdm",
  "subcategory_id": "activation_eula",
  "templates": [
    {
      "id": "rdm-checkin-eula-missing",
      "log_type": "logcat",
      "regex": "\\bDeviceLockSchedulerImpl\\b.*\\bhas eula:false\\b",
      "meaning": "check-in 结果中没有 EULA 配置，协议页无法展示。",
      "selected_reason": "小类 activation_eula 的高优先级模板"
    }
  ]
}
```

### 阶段 4：历史案例召回

召回 Top 3 案例：

- 输入：用户描述、模块 ID、小类 ID、profile、已识别包名。
- 输出：案例描述、关键日志、根因、曾经使用的证据模板 ID、接力领域。

案例中的证据模板可加入搜索候选，但历史根因只能作为参考。

### 阶段 5：日志类型识别

先根据全局和项目 `log_types.json` 识别每个日志文件的类型。证据模板必须只在匹配其 `log_type` 的文件里搜索。

输出 `log_type_manifest.json`：

```json
{
  "files": [
    {
      "path": "pid_5327_logs.txt",
      "log_types": ["logcat", "rdm_business_log"],
      "matched_by": ["path_patterns", "content_patterns"],
      "confidence": 0.92
    }
  ],
  "unknown_files": ["large_binary.dump"]
}
```

### 阶段 6：日志过滤和释义

使用证据模板扫描日志，生成按时间排序的证据列表。

第一版只做单行确定规则匹配：

- 命中一行即可解释含义。
- 可记录上下 1-2 行作为显示上下文，但不在前置阶段做堆栈/调用链判断。
- `FATAL EXCEPTION`、ANR、tombstone、trace 等需要多行上下文或结构化解析的内容，只生成“需要 Deep 阅读”的提示，不在前置阶段强行下结论。

输出格式：

```text
// 释义：check-in 成功，但服务端返回的 EULA 配置为空，后续协议页无法展示。
01-30 15:02:54.773 I DeviceLockSchedulerImpl: processCheckInResult:1, has eula:false
```

每条证据保留：

- 原始日志。
- 中文释义。
- 模板 ID。
- 日志文件路径。
- 行号。
- 时间戳。
- 严重程度。
- 是否来自历史案例扩展。

### 阶段 7：前置工作流 AI 报告

输入给 Claude CLI 的前置 prompt 形态：

```text
用户描述的问题是：
{{question}}

该问题分类为：
{{module title}} / {{subcategory title}} / {{profile}}
模块说明：
{{module description}}
问题小类说明：
{{subcategory description}}

目前从日志里发现以下关键日志及释义：
{{annotated evidence timeline}}

可供参考的历史案例有：
1. 问题描述：...
   关键日志：...
   问题根因：...

请根据以上信息，分析可能原因。
要求：
1. 区分“本次日志已证实”和“历史案例提示”。
2. 如果证据不足，请明确说明还需要 Deep 分析。
3. 用一句话总结初步可能原因，供 Deep 分析参考。
```

前置报告输出：

```json
{
  "summary": "一句话初步结论",
  "evidence_supported": true,
  "confidence": 0.62,
  "needs_deep": true,
  "handoff_domains": ["FWK/OEM iaware"],
  "known_gaps": ["缺少 iaware 源码", "需要检查广播冷启动链路"]
}
```

## Deep 分析设计

### Deep 输入

Deep 复用前置阶段产物：

- 用户原始问题。
- 模块/小类/profile 分类。
- 模块和小类说明。
- 关键日志 + 中文释义。
- 历史案例 Top K。
- 前置工作流一句话总结。
- Evidence Pack 文件。

Deep 额外获得：

- 全量日志目录 `--add-dir`。
- 命中模块源码目录 `--add-dir`。
- 命中模块 skill 路径/摘要。
- 命中模块 CLAUDE.md/AGENTS.md。
- OEM 经验库摘要。
- 应用清单 `app_inventory.json`。

### Deep prompt 形态

```text
用户描述的问题是：
{{question}}

该问题分类为：
{{module title}} / {{subcategory title}} / {{profile}}

目前从日志里发现以下关键日志及释义：
{{annotated evidence timeline}}

可供参考的历史案例有：
{{top cases}}

目前初步分析的可能原因为：
{{workflow summary}}
注意：以上初步分析仅供参考。如果全量日志、源码或 skill 证据不支持该结论，应明确推翻。

你可以读取以下路径：
- 日志目录：{{log_dir}}
- 模块源码目录：{{source_paths}}
- 模块 skill：{{skill_paths}}
- 项目指南：{{guide_paths}}

请优先按以下顺序分析：
1. 阅读并遵循命中模块的 skill。
2. 阅读模块 CLAUDE.md/AGENTS.md 中与该问题相关的部分。
3. 基于关键日志的 TAG/代码位置阅读源码。
4. 在全量日志中围绕已命中证据的时间线扩展搜索。
5. 如果证据仍不足，再基于用户描述做受限发散搜索。

请输出完整分析报告。
如果判断非本模块问题，请说明证据、接力领域、下一步需要谁继续分析。
```

### Deep 搜索阶梯

Deep 必须按层扩展，不能一上来全局模糊 grep：

1. 模块 skill 指定的排查步骤。
2. 证据模板命中的 `TAG + message`。
3. 小类内所有模板的 TAG/包名/组件名。
4. 模块全量真实 TAG、包名、组件名、类名。
5. 代码阅读后新增的真实日志输出。
6. 用户语义关键词 + 已知 TAG/包名/组件名组合搜索。

每一层都记录：

- 执行原因。
- 搜索关键词。
- 命中文件。
- 命中数量。
- 新增证据。
- 是否提升置信度。

## 配置和开关

建议保留现有 `android_issue_analysis`，新增实验开关：

```ini
[features]
android_issue_analysis = true
android_issue_analysis_expert_workbench = false
```

第一阶段双轨运行：

- 旧链路继续可用。
- 新链路只在开关开启时启用。
- 测试集可以同时跑旧链路和新链路，比较效果。

建议新增配置：

```ini
[android_issue_analysis]
expert_workbench_enabled = false
project_knowledge_relative_path = .claude-web/android-analysis
global_profile_path = android_analysis_knowledge/global/profiles
global_log_type_path = android_analysis_knowledge/global/log_types
oem_experience_path = android_analysis_knowledge/global/oem_experience
case_top_k = 3
evidence_max_lines = 120
deep_force_enabled = true
deep_add_full_log_dir = true
deep_add_source_dirs = true
deep_load_guides = true
deep_load_skills = true
```

## 产物文件

每个 job 生成：

```text
artifacts/
  classification.json
  parameter_resolution.json
  selected_evidence_templates.json
  case_recall.json
  log_type_manifest.json
  annotated_evidence_timeline.json
  annotated_evidence_timeline.md
  workflow_prompt.md
  workflow_report.md
  workflow_result.json
  deep_prompt.md
  deep_report.md
  deep_trace.jsonl
  final_report.md
  token_usage.json
```

## 数据模型

### 分类输出 `classification.json`

```json
{
  "module_id": "framework",
  "module_title": "Android Framework",
  "profile": "functional",
  "submodule_id": "activity_start",
  "module_confidence": 0.78,
  "submodule_confidence": 0.64,
  "candidate_modules": [
    {
      "module_id": "framework",
      "submodule_id": "activity_start",
      "score": 0.64,
      "reason": "用户描述涉及应用启动、Activity 跳转或前后台切换"
    },
    {
      "module_id": "app",
      "submodule_id": "unknown",
      "score": 0.31,
      "reason": "可能是应用自身崩溃或兼容问题"
    }
  ],
  "need_submodule": true,
  "need_user_clarification": false,
  "package_candidates": [],
  "time_window_hint": null
}
```

### 参数解析输出 `parameter_resolution.json`

```json
{
  "need_package_resolution": true,
  "package_candidates": [
    {
      "package_name": "com.example.target",
      "label": "示例应用",
      "confidence": 0.82,
      "source": "app_inventory_semantic_match",
      "reason": "用户描述中的中文应用名与应用清单 label 匹配"
    }
  ],
  "resolved_parameters": {
    "package_name": ["com.example.target"],
    "uid": [],
    "component": []
  },
  "need_user_clarification": false
}
```

### 证据模板 `evidence_templates.jsonl`

```json
{"id":"rdm-checkin-eula-missing","module_id":"android-rdm","subcategory_id":"activation_eula","profile":"functional","log_type":"logcat","regex":"\\bDeviceLockSchedulerImpl\\b.*\\bhas eula:false\\b","code_location":"DeviceLockSchedulerImpl#processCheckInResult","meaning":"check-in 结果中没有 EULA 配置，协议页无法展示。","severity":"critical","time_anchor":true,"next_steps":["检查 RDMQueryService checkin onSuccess 响应","确认协议页 UI 是否因 EULA 为空被跳过"]}
```

### 日志类型定义 `log_types.json`

```json
{
  "id": "logcat",
  "title": "Android logcat",
  "path_patterns": ["(?i)(logcat|android_log|applogcat|main).*\\.(txt|log)$"],
  "content_patterns": ["^\\d{2}-\\d{2}\\s+\\d{2}:\\d{2}:\\d{2}\\.\\d{3}\\s+\\d+\\s+\\d+\\s+[VDIWEF]\\s+"],
  "priority": 100
}
```

### 释义证据 `annotated_evidence_timeline.json`

```json
{
  "template_id": "rdm-checkin-has-eula",
  "log_type": "logcat",
  "file": "pid_5327_logs.txt",
  "line": 1832,
  "timestamp": "01-30 15:02:54.773",
  "raw": "I DeviceLockSchedulerImpl: processCheckInResult:1, has eula:false",
  "meaning": "check-in 结果显示服务端未下发 EULA 配置，协议页无法展示。",
  "severity": "critical",
  "time_anchor": true,
  "context_before": [],
  "context_after": [],
  "source": "template"
}
```

### 历史案例 `case_cards.jsonl`

```json
{
  "case_id": "rdm-eula-missing-001",
  "module_id": "android-rdm",
  "subcategory_id": "activation_eula",
  "profile": "functional",
  "similarity": 0.86,
  "summary": "恢厂后在线激活，声明页后无协议页，根因是服务端未配置 EULA。",
  "embedding_text": "RDM 在线激活 协议页缺失 EULA has eula false 服务端未配置",
  "key_evidence": ["DeviceLockSchedulerImpl has eula:false"],
  "root_cause": "服务端未配置 EULA",
  "used_template_ids": ["rdm-checkin-eula-missing"],
  "handoff_domains": ["RDM 服务端配置"]
}
```

后续如果引入 RAG 向量库，只需要对 `embedding_text` 或 `summary + key_evidence + root_cause` 计算 embedding，原始案例全文仍可保留在文件中，不必全部塞入向量库。

## UI 调整

第一版 UI 保持轻量：

1. Android 分析过程展示分类结果。
2. 展示关键日志及中文释义时间线。
3. 展示历史案例 Top 3。
4. 展示前置工作流报告。
5. 展示 Deep 分析报告。
6. 支持展开 Deep 搜索阶梯和每层命中结果。
7. 导出 HTML 时包含以上全部内容。

## 测试策略

所有 Phase 都必须可独立验收，不能只依赖端到端结果判断好坏。端到端失败时，应能从产物文件判断是分类错、模板选择错、案例召回误导、日志类型识别错、单行证据未召回、前置 prompt 误导，还是 Deep 搜索阶梯执行不充分。

每个 Phase 完成后，都要用当前人工标注集 `D:\Code\FWK\testcase.xlsx` 跑对应阶段的最小评测，不必等完整链路完成。比如分类 Phase 只评测分类 JSON，日志类型 Phase 只评测文件类型识别，证据搜索 Phase 只评测关键单行证据召回。该 Phase 达到验收标准后，再进入下一阶段。

### 人工标注集回归

使用 `D:\Code\FWK\testcase.xlsx` 作为第一批回归集。

指标：

- 前置分类准确率。
- 关键证据召回率。
- 历史案例召回是否有帮助。
- 前置报告是否误导。
- Deep 是否能推翻错误前置结论。
- 最终结论与人工结论的对齐等级：`excellent / good / partial / miss`。

### 模块 skill 质量测试

每个模块 skill 至少验证：

- 能否从用户描述定位小类。
- 能否给出正确证据模板。
- 能否指导 Deep 先读正确代码入口。
- 是否避免直接模糊 grep。
- 是否能识别非本模块接力领域。

### 用例覆盖方向

继续补充：

- APP 功能异常。
- FWK 功能异常。
- XTS/CTS/GTS。
- Java crash。
- ANR/watchdog/死锁。
- native crash/tombstone。
- 内存 meminfo/smaps/hprof。
- 性能 trace/perfetto。
- OEM 定制策略日志，如 iaware/trustsbase。
- RDM check-in/EULA/IMEI/push token/provision。

## 阶段计划

### Phase 0：冻结现状和评测基线

- [ ] 固化当前旧链路评测结果。
- [ ] 保存 `testcase.xlsx` 评测 CSV 和报告。
- [ ] 确认新链路开关默认关闭。

验收：

- 旧链路不受影响。
- 有一份可重复跑的基线评测脚本和结果。

### Phase 1：知识库数据结构落地

- [x] 定义项目内知识包文件树：`.claude-web/android-analysis/`。
- [x] 新增 module/subcategory/evidence_template/log_type/experience_log/case schema。
- [x] 新增 schema 校验和 list/debug API。
- [x] 服务启动时按 `claude_web_paths.config.json` 扫描项目路径并缓存知识包。
- [x] 不迁移当前旧规则包，旧规则只作为人工参考。

验收：

- 能 list 模块、小类、证据模板、日志类型、经验日志和历史案例。
- schema 校验通过。
- 重启服务后缓存可用，且普通聊天不受影响。

### Phase 2：项目知识包生成流程

- [x] 给 Claude 一个空白证据模板和用户定义的模块/小类，让 Claude 深入阅读项目代码填充模板。
- [x] 生成 `module.json`、`subcategories.json`、`evidence_templates.jsonl`、`experience_logs.jsonl` 草稿。
- [x] 支持 `evidence_templates.csv/xlsx` 与 `evidence_templates.jsonl` 双向转换。
- [x] 生成模块分析 `SKILL.md` 草稿，重点写业务流程和排查步骤。
- [x] 用户根据经验补充厂商特殊日志、误判边界和历史案例。
- [x] 生成内容写入项目根目录 `.claude-web/android-analysis/`，不写入旧中心规则包。

实现备注：

- 新增 `expert_knowledge_builder.py`，负责创建项目知识包脚手架、生成 Claude 填表提示词、写入模块分析 skill 草稿，并提供 CSV/XLSX/JSONL 互转能力。
- 新增 `/api/android-analysis/expert-knowledge/scaffold` 和 `/api/android-analysis/expert-knowledge/evidence-templates/convert`，只允许对 `claude_web_paths.config.json` 白名单内的项目路径写入。
- 生成的证据模板默认为空白审阅模板，不从旧规则包迁移模糊关键词；真实模板需由 Claude 读代码后填写，再由人工确认。
- 2026-05-13 新增低 token 证据模板生成流水线 `evidence_template_pipeline.py`：
  - 默认 `prefiltered` 模式先扫描源码中的真实日志调用，只把候选 `TAG/message/code_location` 交给 Claude 转成证据模板。
  - 保留 `full_read` 模式，允许 Claude CLI 读取项目源码作为全量阅读对照实验。
  - 每次生成都会落盘候选日志、prompt、draft、normalized JSONL、CSV、XLSX、notes、metrics，便于审阅和复盘。
  - 新增 `/api/android-analysis/expert-knowledge/evidence-templates/generate`，支持 `dry_run` 只生成候选与 prompt，不调用 Claude。
  - 对 RDM `device_identification_failed` 真实跑过 `prefiltered`：候选 50 条，prompt 约 1.9 万字符，生成 12 条归一化模板，校验错误 0；保留 `metrics` 中的 Claude token/cost 作为与 full-read 的对比基线。
  - 新增 `batch-prefiltered` 批量生成能力：对一个模块的多个小类先本地扫描候选日志，再用一次 Claude CLI 调用批量生成模板，生成中间产物默认放到 server 的 `android_analysis_knowledge/expert_workbench/`，避免污染项目知识包目录。
  - RDM 全量小类已跑通：19 个小类，正式模板 89 条，正式文件保留在 `D:\Code\RealtimeDeviceManager\.claude-web\android-analysis\evidence_templates.*`，中间产物归档在 `android_analysis_knowledge/expert_workbench/android-rdm/RealtimeDeviceManager/`。

验收：

- 对 RDM 或 FWK 单模块能生成一份可读、可校验的项目知识包。
- 证据模板只包含真实代码/日志输出，不出现纯语义模糊关键词。
- 证据模板可导出成 Excel/CSV 给人审阅，也可从 Excel/CSV 重新导入。
- 用户可以单独打开文件审阅和修改。

### Phase 3：模块和小类分类器

状态：已完成第一版实现和离线自测。

- [x] 实现只看用户描述的分类器。
- [x] 输出固定 JSON，包含 top candidates、置信度、是否需要用户澄清、包名候选和时间窗口提示。
- [x] 记录 prompt、输出、耗时、token。
- [x] 在 `testcase.xlsx` 上做第一轮离线评测。

实现记录：

- 新增 `claude_web/android_analysis/classifier.py`，分类器只接收用户问题与专家知识缓存里的模块/小类摘要。
- Android 分析 job 会先生成 `classification_prompt.md`、`classification_result.json`、`classification_metrics.json`，暂不替代旧 Planner 决策，避免影响现有首轮工作流。
- AI 不可用或关闭时会退回本地启发式分类，保证离线可测。
- 当前 `D:\Code\FWK\testcase.xlsx` 12 条样本均可输出稳定分类；部分问题小类仍偏粗，后续交给 Phase 4/5/6 通过参数、模板与案例闭环修正。

验收：

- 对 12 条人工样本输出稳定分类。
- 不能加载日志、代码或 skill。
- `module_confidence < 0.5` 时归为 unknown；小类置信度不足或 top1/top2 接近时不强行判小类。

### Phase 4：包名/参数提取

状态：已完成第一版实现和离线自测。

- [x] 根据模块配置判断是否需要包名解析。
- [x] 支持 `module.json.default_package_names`。
- [x] 支持从 `app_inventory.json` 中按中文 label、别名、包名做语义匹配。
- [x] 支持提取多个包名候选和 `$package_name` 等参数。
- [x] 输出 `parameter_resolution.json`。

实现记录：

- 新增 `claude_web/android_analysis/parameter_resolver.py`，只读取 Phase 3 分类结果、模块元数据和可选应用清单，不读取上传日志或源码。
- 项目知识包支持在 `.claude-web/android-analysis/app_inventory.json` 中维护应用清单；全局知识目录也支持 `global/app_inventory/*.json` 或 `global/app_inventory.json`。
- Android 分析 job 会在分类后、解压日志前生成 `parameter_resolution.json` 和 `parameter_resolution_metrics.json`。
- 解析结果会保留多个包名候选、来源、置信度、label、uid、launcherActivity 等信息；是否需要用户澄清由模块是否强依赖包名且是否解析成功决定。

验收：

- RDM 这类固定包名模块不额外提问也能填入默认包名。
- AMS/Activity 启动类问题能从应用清单中提取一个或多个包名候选。
- 多包名候选会保留置信度和来源，不强行只选一个。

### Phase 5：证据模板选择

- [x] 根据分类选择证据模板。
- [x] 小类明确时只选该小类模板 + profile 基础模板 + OEM 经验模板。
- [x] 小类不明确时选模块全量模板去重集合。
- [x] 根据 `parameter_resolution.json` 展开 `$package_name` 等占位符。
- [x] 输出 `selected_evidence_templates.json`。

验收：

- 能解释每个模板为什么被选中。
- 模板选择阶段不读日志文件。
- 占位符未解析时，相关模板标记为待 Deep 或待用户澄清，不盲目搜索。

### Phase 6：历史案例召回

- [x] 定义轻量 case card 召回结构。
- [x] 实现 Top K 历史案例召回。
- [x] 只读取 `summary` 和 `embedding_text` 等轻量摘要，不加载完整案例正文。
- [x] 根据分类模块、小类、profile 和证据模板 ID 等信号计算匹配分。
- [x] 输出案例召回结果并写入 debug trace。

验收：

- 默认最多返回 3 张案例卡。
- 历史案例只作为提示，不直接套用结论。
- 召回结果能解释命中原因。

## Phase 6 实施记录（2026-05-16）
状态：已完成第一版实现与离线自测。

- `casebook.recall_case_cards()` 升级为 v2，默认返回 Top 3 轻量 Case Card，不加载完整案例正文，避免历史案例挤占当前证据上下文。
- 同时支持旧中心知识目录 `android_analysis_knowledge/bundles/*/indexes/case_cards.jsonl` 和项目知识包 `.claude-web/android-analysis/cases/case_cards.jsonl`。
- 召回时会消费 Phase 5 的 `selected_evidence_templates.json`，把模块、小类、profile、模板 ID 和日志类型作为排序信号。
- project-local case card 会归一化为 `id/title/module_id/submodule_id/profile/summary/embedding_text/key_evidence/root_cause_summary/used_template_ids/log_types/source_bundle_ids/tags`。
- Debug trace 会记录 selected module/submodule/template ids、候选案例分数、命中原因和最终选中案例。
- 已新增 RDM 样例案例 `rdm-eula-missing-001`，可通过 `rdm-checkin-eula-missing` 模板 ID 命中召回。

验收命令：
```bash
python -m py_compile claude_web/android_analysis/casebook.py claude_web/routes.py
python -m unittest tests.test_android_analysis.AndroidAnalysisPhaseOneTests
```

### Phase 7：日志类型识别

- [x] 实现全局和项目 `log_types.json` 加载。
- [x] 根据路径正则和内容抽样识别日志类型。
- [x] 输出 `log_type_manifest.json`。
- [x] 未识别文件只进入 Deep 可读范围，不参与前置证据扫描。

验收：

- 同一关键词只在模板声明的 `log_type` 文件中搜索。
- 能解释每个文件为什么属于某个日志类型。

实现记录：

- 新增 `claude_web/android_analysis/log_type_identifier.py`，合并内置基础日志类型、全局 `global/log_types/*.json` 和项目 `.claude-web/android-analysis/log_types.json`。
- 日志类型识别会读取 `file_manifest.json`，对每个文件执行路径正则匹配和 head/middle/tail 内容抽样匹配，输出 `log_type_manifest.json` 与 `log_type_manifest_metrics.json`。
- 输出中每个文件会记录 `log_types`、匹配模式、命中的 path/content pattern、置信度和 Deep-only 状态；未识别文件标记为 `deep_only=true`。
- Android 分析 job 已在 profiling 后、sampling 前接入 Phase 7，并把产物登记到 job artifacts 与 debug trace。
- 当前阶段只做日志类型识别，不执行证据模板搜索；Phase 8 将消费 `selected_evidence_templates.json` 与 `log_type_manifest.json`。

### Phase 8：单行证据搜索和日志释义

- [x] 使用 `selected_evidence_templates.json` 和 `log_type_manifest.json` 搜索日志。
- [x] 只处理单行即可确定含义的日志证据。
- [x] 生成 `annotated_evidence_timeline.md/json`。
- [x] 复杂多行上下文只记录“建议 Deep 阅读”，不在前置阶段下结论。

实现备注：

- 新增 `claude_web/android_analysis/evidence_template_matcher.py`，按日志类型清单把证据模板限定到对应文件内扫描，跳过 disabled、未 ready、未解析参数、缺少日志类型文件或正则非法的模板。
- 搜索策略保持前置边界：只做单行正则命中，不向下读取堆栈、ANR、tombstone 或跨文件因果；每个模板与文件有命中上限，超大文件有扫描上限。
- 输出 `annotated_evidence_timeline.json` 与 `annotated_evidence_timeline.md`，并把命中的模板证据合并进 `matched_rules.json`，供首轮 Evidence Pack、案例召回和 Deep hints 继续消费。
- Android 分析 job 已在规则匹配后、XML 状态匹配前接入 Phase 8，并把产物登记到 job artifacts、process details 和 debug trace。

验收：

- RDM EULA、IMEI、push token、DLC check-in 等单行明确证据能被召回。
- FATAL/ANR/tombstone 不被前置工作流错误简化成单行根因。

验收命令：

```bash
python -m py_compile claude_web/android_analysis/evidence_template_matcher.py claude_web/routes.py tests/test_android_analysis.py
python -m unittest tests.test_android_analysis.AndroidAnalysisPhaseOneTests
python -m unittest discover tests
```

### Phase 9：前置工作流初步分析

- [ ] 用分类结果、证据时间线、案例召回生成新 prompt。
- [ ] 输出 workflow summary、confidence、needs_deep、handoff domains。
- [ ] 前置报告不直接使用全量日志或源码。
- [ ] 明确标注“本次日志已证实”“历史案例提示”“需要 Deep 验证”。

验收：

- 前置报告重点变成“证据整理 + 初步判断”。
- 不因证据不足硬编根因。

### Phase 10：Deep 分析重构

- [ ] Deep 复用前置产物。
- [ ] 通过 `--add-dir` 加载日志目录和模块源码目录。
- [ ] prompt 明确列出命中 skill 和 CLAUDE.md/AGENTS.md。
- [ ] Deep 必须允许推翻前置结论。
- [ ] 记录 Deep 搜索阶梯。

验收：

- Deep 能读取相关源码、日志和 skill。
- Deep trace 能看到每一步搜索关键词、命中文件、新增证据和置信度变化。

### Phase 11：应用清单支持

- [ ] 定义 `app_inventory.json` schema。
- [ ] 支持用户上传/导入应用清单。
- [ ] Deep prompt 中注入相关包名和中文 label。
- [ ] 规划 Android helper APK 或 ADB 采集脚本。

验收：

- FWK 与 APP 交互问题能显示包名对应中文应用名。

### Phase 12：UI 和导出调整

- [ ] Android 分析过程展示分类、证据时间线、案例、前置报告、Deep 报告。
- [ ] 支持展开 Deep 搜索阶梯。
- [ ] 导出 HTML 保留全部阶段信息。

验收：

- 切换会话、刷新、导出 HTML 后结构不丢失。

### Phase 13：回归评测

- [ ] 使用 `testcase.xlsx` 跑新旧链路对比。
- [ ] 输出对比 CSV。
- [ ] 输出改进报告。
- [ ] 标记仍需补充的模块 skill 和经验日志。

验收：

- 至少明显改善当前 miss 案例中的关键证据召回。
- Deep 对前置误判具备纠偏能力。

### Phase 14：模块 skill 长期维护流程

- [ ] 更新 `project-guide-writer` 和 `android-log-rule-builder` 的职责边界。
- [ ] 新增/更新“模块问题分析 skill”生成流程。
- [ ] 支持从源码和已有日志中生成小类、证据模板、经验日志和排查步骤草稿。
- [ ] 人工确认后写入模块知识库。

验收：

- 新模块接入时，不需要手写大量规则。
- 生成结果更接近开发人员排查手册，而不是模糊关键词集合。

## 待扩展证据源 TODO

当前第一版已支持 `android_log` 单行日志证据和 `xml_state_templates`（SharedPreferences / Settings / XML key-value 状态）。后续其它类型也按“项目知识包定义模板 + 本地解析器抽取结构化证据 + 前置工作流只处理确定证据 + Deep 处理复杂上下文”的方式扩展。

### XML / 状态文件第一版约定

- `path_patterns` 是相对解压目录路径的正则过滤器，用来限定模板应该优先在哪些状态文件里生效，例如 `shared_prefs/user_state.xml`、`settings/global.xml`。
- 当路径匹配成功时，按 `path_pattern` 模式命中，置信度正常。
- 当用户改名、改后缀或日志系统把 XML 包成 `.txt` 时，解析器可以通过文件内容识别 XML-like 文件，再用严格 `key_regex/value_regex` 兜底命中，并标记为 `content_fallback`，置信度略降。
- FWK 或模块自定义 XML 不强行归类为 SharedPreferences。后续可以通过 `source_type` 增加 `framework_xml`、`config_xml`、`settings_xml` 等，并使用对应 `path_patterns` 和 key/value 解析规则。

### 后续证据源

- [ ] `framework_xml/config_xml`：处理 FWK 中手写 XML、资源 XML、device policy / package policy / overlay 配置等，规则结构沿用 `path_patterns + key_regex/value_regex + meaning + next_steps`，但 source_type 独立。
- [ ] `settings_xml` 强化：区分 Global/Secure/System 导出文件，支持动态 key 前缀与用户 ID / subId 后缀。
- [ ] `shared_prefs_xml` 强化：支持多用户路径、应用包名路径、手动改名 fallback 的误报控制和路径命中解释。
- [ ] `sqlite_db`：先只支持指定表/列/where 模板的轻量查询，输出关键行摘要；复杂 SQL 和多表推理交给 Deep。
- [ ] `dumpsys_text`：按模块维护 section/字段模板，前置只抽取确定字段，例如状态机当前状态、最近时间戳、开关值。
- [ ] `xts_report`：解析 HTML/XML 报告中的 suite/module/case/failure/stack，并和对应 logcat 时间窗口关联。
- [ ] `dropbox_crash/anr/tombstone`：前置只提取进程、异常类型、栈顶、关键包名和时间戳，不在前置阶段做完整根因推理。
- [ ] `meminfo/smaps/hprof_index`：抽取进程、PSS/RSS、对象/类名高占用摘要；详细泄漏链交给 Deep。
- [ ] `perfetto/systrace`：抽取 trace 元数据、线程、slice、binder latency、long task 摘要；详细时间线归因交给 Deep。
- [ ] 所有新证据源都需要补齐：schema、csv/xlsx 维护格式、生成 prompt、校验器、解析器、debug trace、单测和至少 1 个真实样例。

## 风险和对策

| 风险 | 对策 |
|---|---|
| 分类器误判模块 | Deep prompt 允许跨模块接力；分类输出保留 top candidates |
| 证据模板维护成本上升 | 只维护高价值精确证据；模糊语义留给 Deep |
| 历史案例误导 | 明确标注案例只是参考；Deep 必须用本次日志验证 |
| Deep 读取过多导致 token 高 | 搜索阶梯、白名单路径、命中 evidence 限制和 token trace |
| 厂商日志不可理解 | 建立 OEM experience，并支持人工持续追加 |
| skill 未被 CLI 自动加载 | prompt 显式列出 SKILL.md 路径和使用顺序，必要时注入 skill 摘要 |
| 包名识别错误导致模板展开错误 | 参数解析输出多个候选和置信度；低置信时不展开或请求 Deep 验证 |
| Excel/CSV 与 JSONL 不一致 | 启动校验并提示；约定表格为人工源，JSONL 为运行缓存 |

## 第一版完成标准

1. 新链路通过 feature flag 可开关。
2. 12 条人工标注样本可完整跑通。
3. 项目内 `.claude-web/android-analysis/` 知识包可被服务启动扫描并缓存。
4. 每条样本都有分类 JSON、参数解析 JSON、模板选择 JSON、案例召回 JSON、日志类型清单、证据时间线、前置报告、Deep 报告。
5. RDM EULA、IMEI、push token、DLC check-in 等单行确定证据至少能召回关键日志。
6. XTS iaware、UserController、AccountManagerService 这类跨模块问题至少能给出正确接力领域。
7. Deep 分析能明确引用 skill、源码、日志路径和证据，并可推翻前置结论。
8. 证据模板支持 Excel/CSV 人工维护和 JSONL 运行格式互转。
9. 导出 HTML 能展示完整分析过程。
## Phase 5 实施记录（2026-05-16）

状态：已完成第一版实现与离线自测。

- 新增 `claude_web/android_analysis/evidence_selector.py`，根据 `classification_result.json` 与 `parameter_resolution.json` 选择证据模板，不读取日志文件或源码。
- 输出 `selected_evidence_templates.json` 与 `selected_evidence_templates_metrics.json`，记录模块选择、小类策略、模板选择理由、占位符展开结果、待参数模板和 OEM/模块经验提示。
- 小类明确时只选择该小类模板，并保留未绑定小类但 profile 匹配的基础模板；小类不明确时加载模块全量模板去重集合。
- 支持 `$package_name`、`$uid`、`$component` 等占位符展开；未解析的占位符会标记为 `needs_parameters` 且 `search_enabled=false`，交给 Deep 或用户澄清，不在前置流程盲搜。
- Android 分析流水线已在 Phase 4 参数解析后接入 Phase 5，并把产物登记到 job artifacts。
- 新增单测覆盖：明确小类选择、XML 状态模板选择、占位符展开、占位符未解析阻断、模糊小类加载模块全量模板。

验收命令：

```bash
python -m py_compile claude_web/android_analysis/evidence_selector.py claude_web/routes.py
python -m unittest tests.test_android_analysis.AndroidAnalysisPhaseOneTests
```

后续 Phase 6 可以直接消费 `selected_evidence_templates.json` 中的模板 ID、模块/小类/profile 和经验提示，进行历史案例召回。
