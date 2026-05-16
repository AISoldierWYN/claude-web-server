---
name: project-guide-writer
description: 给指定代码目录生成或更新 CLAUDE.md / AGENTS.md 项目指南，并可在项目根目录生成项目专属日志分析 Skill；适用于要求 Claude CLI 先阅读本地项目源码、构建文件、目录结构和已有文档，再沉淀 AI 后续协作、Deep 分析、模块边界、常用命令、精确日志入口、逐层日志搜索策略、项目专属排查流程与长期维护约定。
---

# Project Guide Writer

Use this skill when the user wants a project-level `CLAUDE.md`, `AGENTS.md`, or both, for a specific source tree.

## Workflow

1. Confirm the target root directory and whether to write `CLAUDE.md`, `AGENTS.md`, or both.
2. Inspect the project before writing:
   - root files: `README*`, `settings.gradle*`, `build.gradle*`, `gradle.properties`, `package.json`, `pyproject.toml`, existing `CLAUDE.md`, existing `AGENTS.md`
   - top-level directories and major modules
   - build/test scripts and CI files if present
   - source files that reveal app entry points, services, workers, receivers, native code, logging tags, and domain keywords
3. Summarize only durable knowledge. Do not paste large source snippets.
4. Write concise Markdown in the project root.
5. If both files are requested:
   - `CLAUDE.md` should focus on Claude CLI behavior, investigation strategy, and project-specific code/log guidance.
   - `AGENTS.md` should focus on general agent rules, architecture map, commands, testing, and maintenance constraints.
6. Preserve any clearly project-specific existing guidance unless it is obsolete. Replace placeholder or tiny stub files.
7. If the user asks for a project-specific log analysis skill, create it under the target project root, for example `<PROJECT_ROOT>/skills/<project-log-analysis>/SKILL.md`.

## Scripted Generation

Prefer the bundled generator when the target is a source tree that can be scanned locally:

```bash
python skills/project-guide-writer/scripts/project_guide_manager.py generate \
  --project-root <PROJECT_ROOT> \
  --project-id <project-id> \
  --title "<Project Title>" \
  --bundle-id <claude-web-bundle-id> \
  --project-preset app \
  --profile functional --profile stability
```

The script writes `CLAUDE.md`, `AGENTS.md`, and `<PROJECT_ROOT>/skills/<project-id>-log-analysis/SKILL.md`.
It reuses `android-log-rule-builder` scanning so the generated guide only lists exact log entries that are present in source code.
Use manual edits after generation to add human-confirmed business knowledge or remove stale guidance.

## Log Guidance Rules

When writing Android or backend troubleshooting guidance, keep code-entry guidance separate from log-search guidance.

- `代码入口` may list modules, classes, packages, manifest components, workers, services, receivers, or source files that should be read first.
- `精确日志入口` may list only patterns verified from source code, manifest actions, constants, generated rule packs, or real sample logs.
- Prefer `TAG + message regex` or `component/action + exact string` forms, for example `DeviceLockStateManagerImpl / Enforce current lock policy fail, result:` or `Intent action / com.example.action.SYNC`.
- Do not invent broad semantic keywords from business meaning. Words like `lock`, `unlock`, `sync`, `fail`, `error`, `policy`, `network`, or Chinese translations are not valid log-search advice unless the project really emits them and the guide names the exact emitting TAG/component.
- If exact log patterns are not confirmed, write that no reliable log keyword is provided yet, and direct future agents to read the code entry or generated rule pack first.
- Do not recommend sweeping grep searches over generic words. Generic grep is acceptable only as a fallback after exact patterns and code/rule-pack guidance fail.

## Deep Analysis Guidance

`CLAUDE.md` must clearly constrain Deep analysis. Deep should not start by grepping fuzzy user words. It should expand evidence in this order:

1. **Feature-specific 1 类日志**：for the user-described function, use verified `TAG + message` patterns from the project guide or project-specific Skill.
2. **Feature-related 2 类范围信号**：if 1 类 does not hit, expand to TAG/package/component/action/binder/service names that belong to the same feature.
3. **Project-wide 2/3 类 code/log scope**：expand to project-wide real TAGs, packages, components, classes, and paths from `CLAUDE.md` and rule pack `deep_hints`.
4. **TAG + fuzzy semantic fallback**：only when the first three levels fail, combine real project TAG/package/component scopes with one layer of fuzzy terms derived from the user question and AI domain knowledge.

Every broader layer should be justified: why the previous layer was insufficient, what was added, which files/logs were searched, and whether confidence improved.

If the question is unclear or not covered by the project-specific Skill, Deep should first read `CLAUDE.md`, find the likely code entry, inspect that code for real logging calls, then return to layer 1 with newly found `TAG + message` patterns.

## Project-Specific Log Analysis Skill

When requested, generate a standard Skill inside the target project root:

```text
<PROJECT_ROOT>/skills/<project-id>-log-analysis/
  SKILL.md
```

The Skill should be concise and project-specific. It should not copy source code. It should describe:

- Supported problem areas or submodules. For FWK, allow one skill to cover a subdirectory such as `services/core/java/com/android/server/am`; for an App, one skill may cover the whole app.
- Feature-to-code-entry map.
- Feature-to-1 类 exact log map: verified `TAG + message regex`, private log wrapper calls, `Slog`, `EventLog`, native log macros, Intent actions, exceptions.
- 2 类 expansion map: TAG/package/component/service/binder/action/permission/settings/system property.
- 3 类 Deep hints: classes, methods, preferred source paths, dumpsys/trace/meminfo/xts artifacts.
- Strict search order: 1 类 exact logs -> 2 类 feature scope -> project-wide TAG/package/class/component scope -> TAG plus one layer of fuzzy semantic terms.
- Explicit warning: never start with fuzzy grep terms from user wording.

Project-specific Skills should be referenced from `CLAUDE.md` and, when appropriate, from `claude_web_paths.config.json` as an on-demand skill. They are Deep guidance, not a replacement for the front workflow rule pack.

## Suggested Claude CLI Prompt

Use this prompt shape when delegating the actual guide writing to Claude CLI:

```text
你正在为本地代码项目生成 AI 协作指南。目标路径：
<PROJECT_ROOT>

请先阅读项目根目录和关键源码，不要直接凭文件名猜测。至少检查：
- README / existing CLAUDE.md / AGENTS.md
- settings.gradle / build.gradle / gradle.properties
- app 或核心模块下的 AndroidManifest、主要 Activity/Service/Receiver/Worker
- 常见 Log/TAG/Timber/Slog 调用、异常类、业务关键词
- native/CMake/JNI 相关入口（如果存在）

请在项目根目录生成或更新：
- CLAUDE.md
- AGENTS.md

写作要求：
- 使用中文。
- 内容要具体到本项目，不写泛泛而谈的 Android 常识。
- 标出模块职责、关键流程、日志排查入口、常用构建/测试命令、代码修改注意事项。
- 对 Android 项目，额外写清：重要 package、组件、Worker/Service/Receiver、数据/网络/存储模块、稳定性/功能/性能问题优先看哪些目录。
- Android / 日志排查入口必须拆成“代码入口”和“精确日志入口”：代码入口可以列源码类/目录；精确日志入口只能列代码中实际存在的 TAG、日志文本、Intent action、异常类、系统日志固定字段或已验证规则包中的模式。
- 不要把业务语义词直接写成日志关键词。除非代码确实输出了该词，并且能写出对应 TAG/组件，否则不要写“搜索 lock / unlock / fail / error / policy / network”等泛词。
- Deep 分析约束必须写清：先 1 类精确日志，再 2 类 TAG/包名/组件名，再 3 类项目代码范围，最后才允许“真实 TAG + 用户问题语义”模糊兜底。
- 如用户要求，额外在项目根目录创建 `skills/<project-log-analysis>/SKILL.md`，沉淀项目专属日志分析流程。
- 如果没有确认到精确日志模式，只写“暂无可靠精确日志关键词，先阅读代码入口/规则包”，不要编造。
- 如果证据不足，请写“需要后续补充”的清单，不要编造。
- 不要包含 API key、账号、token、机器私密路径之外的敏感信息。
- 文件应短而有用，优先帮助后续 AI 少走弯路。
```

## Output Outline

`CLAUDE.md`:

```markdown
# <Project> Claude Guide

## 项目定位
## 优先阅读
## 关键模块
## Android / 代码入口与精确日志入口
## 常用命令
## 修改注意事项
## 需要后续补充
```

`AGENTS.md`:

```markdown
# <Project> Agent Guide

## 项目概览
## 目录结构
## 构建与测试
## 代码约定
## 问题排查
## 安全与边界
```
