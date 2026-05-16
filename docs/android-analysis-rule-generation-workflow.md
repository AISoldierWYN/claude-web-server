# Android 日志规则生成流程

本文档沉淀 Phase 2 的规则生成方法，后续给 APP、FWK、Native 或厂商定制模块生成前置工作流规则时，按这里执行。

## 目标

日志规则包只服务于“前置简单工作流”，目标是快速过滤强相关、单行即可理解的证据，降低首轮 AI 分析的日志噪声和 token 消耗。

不要把所有能想到的排查经验都塞进 `evidence_templates.jsonl`。需要多行上下文、堆栈、trace、ANR/tombstone、meminfo、smaps、hprof 或代码发散分析的问题，交给 Deep 分析和模块专属 skill。

## 知识包结构

单项目单模块时：

```text
<project>/.claude-web/android-analysis/
├── module.json
├── subcategories.json
├── log_types.json
├── evidence_templates.jsonl
├── evidence_templates.csv
├── evidence_templates.xlsx
├── xml_state_templates.jsonl
├── xml_state_templates.csv
├── xml_state_templates.xlsx
├── experience_logs.jsonl
├── cases/
│   └── case_cards.jsonl
├── generation_prompt.md
└── README.md
```

一个大仓库包含多个业务模块时，推荐：

```text
<project>/.claude-web/android-analysis/modules/
├── android-fwk-ams/
├── android-fwk-pms/
├── android-fwk-dpm/
├── android-fwk-mdm/
└── android-fwk-dlc/
```

服务启动时会扫描 `claude_web_paths.config.json` 中配置的项目根目录，并加载根知识包和 `modules/*/module.json` 子知识包。这样 `D:/Code/FWK` 一个路径即可暴露多个可路由模块。

## 核心文件

`module.json`：模块元信息、源码范围、默认包名、是否需要解析包名。

关键字段：

```json
{
  "id": "android-fwk-ams",
  "title": "Android AMS / ActivityManager",
  "description": "ActivityManagerService、进程管理、广播、Activity/Service 生命周期、adj 与 Honor AMS 插装。",
  "source_roots": ["frameworks/base/services/core/java/com/android/server/am"],
  "default_package_names": [],
  "package_resolution": {
    "required": true,
    "reason": "AMS 问题常需要从应用中文名或现象中解析目标包名/进程名/组件名。"
  },
  "skill_paths": ["skills/android-fwk-ams-analysis/SKILL.md"],
  "guide_paths": ["CLAUDE.md", "AGENTS.md"]
}
```

`subcategories.json`：模块内问题小类。小类不是最终结论，只用于选择更窄的日志证据模板。

`aliases` 很重要。中文标题本身不适合源码扫描，建议给每个小类补英文 API 名、类名、TAG、业务关键词。

```json
{
  "id": "activity_start_reason",
  "module_id": "android-fwk-ams",
  "title": "activity启动原因",
  "description": "Activity 启动来源、intent 解析、启动链路和阻塞原因。",
  "aliases": ["startActivity", "ActivityRecord", "ActivityTaskManager", "intent", "launch", "START"]
}
```

`evidence_templates.jsonl`：前置工作流日志证据模板，每行一个 JSON。

```json
{
  "id": "ams-start-activity",
  "module_id": "android-fwk-ams",
  "subcategory_id": "activity_start_reason",
  "profile": "functional",
  "log_type": "android_log",
  "regex": "ActivityTaskManager.*START",
  "parameters": ["package_name"],
  "code_location": "frameworks/base/services/core/java/com/android/server/am/ActivityManagerService.java:1234",
  "meaning": "AMS 收到 Activity 启动请求。",
  "severity": "info",
  "time_anchor": true,
  "next_steps": ["按 package_name 对齐后续 ActivityRecord/ProcessRecord 日志"],
  "enabled": true
}
```

`xml_state_templates.jsonl`：可选状态证据模板，用于 SharedPreferences、Settings XML 等导出状态文件。

第一版只做声明式匹配，`path_patterns` 是弱过滤；如果日志包内文件被改名或移动，匹配器会在 XML-like 文件里做内容 fallback，再用 `key_regex/value_regex` 命中状态。

## 生成流水线

1. 维护模块种子。

   对 FWK，本轮种子文件放在 `docs/android-analysis-fwk-module-seed.json`，包含 DPM、MDM、AMS、PMS、DLC 的源码路径和问题小类。

2. 创建知识包脚手架。

   使用 `create_project_knowledge_scaffold()` 写入 `module.json`、`subcategories.json`、空规则表、README、generation prompt 和模块 skill 草稿。

3. 扫描真实日志候选。

   `scan_source_log_candidates()` 只扫描 Java/Kotlin 源码中的真实日志调用，包括 `Log`、`Slog`、`HiLog`、`Hilog`、`Logger`、`RLog`、`MLog`、`Timber`、`LogUtils`、`RdmLog`。

   支持 `source_roots` 写目录，也支持写单文件，例如 `frameworks/base/core/java/android/app/ActivityManager.java`。

4. Claude 生成模板。

   推荐使用 `run_evidence_template_batch_generation_pipeline()`，每个模块一次批量生成：

   - 输入：每个问题小类的候选日志调用。
   - 输出：`evidence_templates.all.prefiltered.draft.jsonl`。
   - 标准化输出：`evidence_templates.all.prefiltered.normalized.jsonl/csv/xlsx`。
   - 过程记录：`evidence_generation.all.prefiltered.prompt.md`、`notes.md`、`metrics.json`。

   生成提示词的核心约束：

   - 只能从候选日志调用中选证据。
   - 不允许凭语义臆造关键词。
   - `regex` 必须能匹配候选的 `TAG + ": " + message`。
   - 每个小类优先 2 到 6 条高价值模板，没有证据可以不生成。
   - `meaning` 解释日志含义，不写最终根因。

5. 校验并落盘正式规则。

   `validate_generated_templates()` 会校验：

   - regex 可编译。
   - code_location 可读。
   - code_location 是受支持的真实日志调用。
   - prefiltered 模式下，模板必须来自候选列表。
   - regex 能匹配候选日志。

   只把通过校验的模板合并到知识包根目录的 `evidence_templates.jsonl/csv/xlsx`。

6. 人工审阅。

   人更适合维护 CSV/XLSX，代码运行时使用 JSONL。审阅后可用转换接口在 CSV/XLSX 和 JSONL 间同步。

## 相关代码

- `claude_web/android_analysis/expert_knowledge.py`：加载根知识包和 `modules/*` 子知识包。
- `claude_web/android_analysis/expert_knowledge_builder.py`：创建脚手架、CSV/XLSX/JSONL 转换、生成提示词。
- `claude_web/android_analysis/evidence_template_pipeline.py`：日志候选扫描、Claude 生成、标准化、校验。
- `claude_web/android_analysis/xml_state_template_pipeline.py`：XML/SP/Settings 状态候选扫描和模板生成。
- `claude_web/android_analysis/xml_state_matcher.py`：运行时匹配 XML/SP 状态证据。

## API 入口

这些入口用于前端专家工作台或脚本调用：

- `POST /api/android-analysis/expert-knowledge/scaffold`
- `POST /api/android-analysis/expert-knowledge/evidence-templates/generate`
- `POST /api/android-analysis/expert-knowledge/evidence-templates/convert`
- `POST /api/android-analysis/expert-knowledge/xml-state-templates/generate`
- `POST /api/android-analysis/expert-knowledge/xml-state-templates/convert`

## 注意事项

- 不要把“失败、异常、卡住、无响应”这类纯语义词直接作为前置 regex，除非它们就是源码日志里稳定输出的字面量。
- `aliases` 只用于源码候选扫描和问题分类辅助，不等于最终日志正则。
- 首轮工作流只处理单行确定性强的日志。Crash 堆栈、ANR、tombstone、trace 等多行证据放到 Deep 分析。
- FWK 类问题常需要包名、组件名、权限名、Intent action 等参数。模块配置里 `package_resolution.required=true` 时，前置流程应先做参数提取。
- 中文内容不要通过 Windows 非 UTF-8 控制台直接 inline 写入 Python。推荐放到 UTF-8 文件里再读取，避免变成 `???`。
- 生成结果先看 `metrics.json` 和 `notes.md`，再看 `normalized.xlsx`。不要直接信任 Claude 初稿。

## TODO

- 给 `experience_logs.jsonl` 和历史案例库补充 CSV/XLSX 双向转换。
- 为 `log_type` 扩展更多单行证据源，例如 `xts_report_text`、`dropbox_header`、`dumpsys_text`。
- 为多行证据源建立 Deep 专用解析器，例如 Java crash、native crash、ANR、tombstone、perfetto/trace。
- 增加规则质量评分：覆盖率、重复率、模糊度、命中噪声率。
- 用 `tests/test_case标注.xlsx` 和后续真实日志集持续回归每个 Phase。
