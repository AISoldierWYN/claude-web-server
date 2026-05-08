# 按需技能包与 Skill 加载优化计划

## 背景

当前 `claude_web_paths.config.json` 的 `bundles[].paths` 只会在服务端关键词命中后加入 Claude CLI 的 `--add-dir`，并在 prompt 中展示“可读取路径”。这解决了目录授权，但并不等价于 Claude CLI 原生加载这些目录里的 `SKILL.md`。

因此实际使用中会出现：

- 用户明明配置了 `.cursor/skills` 或其它技能资料目录，Claude 仍主要通过 Grep/Read 代码和日志定位问题。
- 技能包里的工作流、领域规则、排查顺序没有成为模型优先策略。
- 大量反复 grep 造成 token、时间和工具调用成本偏高。
- 当前 bundle 匹配是简单 substring 命中，没有相似度阈值、技能优先级、二次扩展机制。

## 当前实现诊断

相关代码：

- `claude_web/config.py::load_paths_config_file`
  - 读取 `readonly_dirs` 和 `bundles[].paths`。
  - 不读取 `skills`、`skill_paths`、`resources` 等结构。
- `claude_web/routes.py::_select_skill_bundles`
  - 使用 bundle 的 `id/title/summary/keywords` 做简单字符串包含匹配。
  - 命中后把整个 bundle 的 paths 加入 `--add-dir`。
- `claude_web/claude_runner.py::_skill_bundles_instruction`
  - 只注入 bundle 摘要和路径索引。
  - 没有读取/注入 `SKILL.md`，也没有要求模型按 skill 工作流执行。

结论：目前的“技能包”更像“按需只读目录包”，不是 Claude CLI 原生 skill，也不是 Web 服务层的 prompt-level skill。

## 目标

1. 支持在 `claude_web_paths.config.json` 中明确声明 bundle 内的 skills、资料、代码路径。
2. 每轮只加载与用户问题相似度足够高的 bundle 和 skills，避免一股脑挂载。
3. 对命中的 bundle，优先把 skill 摘要和必要的 `SKILL.md` 工作流交给 Claude，再考虑普通资料/代码路径。
4. 支持分析过程中发现跨模块依赖时，按需追加 bundle/skill 后继续当前任务。
5. 尽量不依赖 Claude CLI 是否原生支持任意外部 skill 目录；先做 Web 服务层稳定可控的 prompt-level skill，后续再评估会话 HOME 中临时注册原生 skills。

## 第一版决策

- 第一版只实现 **方案 A：Prompt-level Skill**。
- 不把外部 skill 复制/同步到会话 HOME；这不是长期方案，也会额外占用空间并带来版本同步问题。
- 命中技能包后，服务端显式读取并截断注入相关 `SKILL.md`，并在 prompt 中强制处理顺序：**先 Skill 工作流** → **通用能力** → **命中包的 `CLAUDE.md` 项目规则** → **普通资料/代码 Read/Grep**。
- `--add-dir` 目录下的 `CLAUDE.md` 不作为默认可靠行为依赖；服务端只会对**本轮命中的包**读取并显式注入其资源根目录 `CLAUDE.md` 摘要，未命中的包不读取内容，确保规则可见且长度可控。
- 原生 session skill 注册/复制方案保留为远期研究项，不进入第一版开发范围。

## 设计原则

- **按需**：默认只注入 bundle 摘要，不挂载路径、不读取 skill 内容。
- **技能优先**：命中 bundle 后，优先展示该 bundle 的 skill 列表和已选 skill 的核心工作流。
- **资料其次**：只有 skill 无法完成、需要证据或源码时，才读取普通 docs/code 路径。
- **可恢复**：外环继续/总结时保留已挂载 bundle ids 和 selected skill ids。
- **可解释**：每轮记录匹配分数、命中原因、挂载 bundle、注入 skill，便于调试。
- **安全边界不变**：bundle 内路径仍只读；只有会话 cache 和开发模式白名单项目可写。

## 配置结构建议

保留旧字段 `paths` 兼容旧配置，同时增加结构化字段：

```json
{
  "version": 3,
  "bundle_match_threshold": 0.35,
  "skill_match_threshold": 0.4,
  "bundles": [
    {
      "id": "android-fwk",
      "title": "Android Framework 相关",
      "summary": "AMS、Activity、Service、进程、system_server 相关问题。",
      "keywords": ["android", "framework", "ams", "activity", "service"],
      "always_mount": false,
      "skills": [
        {
          "id": "ams-triage",
          "path": "D:/AndroidCode/fwk/.cursor/skills/ams-triage/SKILL.md",
          "summary": "Activity/AMS 启动、进程调度、system_server 异常排查流程。",
          "keywords": ["ams", "activity start", "startActivity", "system_server"]
        }
      ],
      "resources": [
        {
          "id": "framework-app-src",
          "kind": "code",
          "path": "D:/AndroidCode/fwk/base/core/java/android/app",
          "summary": "android.app API 源码，只在需要查具体实现时读取。"
        },
        {
          "id": "system-server-src",
          "kind": "code",
          "path": "D:/AndroidCode/fwk/base/services/core/java/com/android/server",
          "summary": "system_server 服务源码。"
        }
      ],
      "paths": []
    }
  ]
}
```

字段含义：

| 字段 | 说明 |
| --- | --- |
| `bundle_match_threshold` | 全局 bundle 匹配阈值，未配置时使用默认值 |
| `skill_match_threshold` | 全局 skill 匹配阈值 |
| `bundles[].skills` | 明确声明可按需使用的 skill；优先于普通 resource/code |
| `bundles[].resources` | 普通资料/代码路径，按需授权读取 |
| `bundles[].paths` | 旧版兼容字段，等价于 `resources.kind=generic` |
| `skills[].path` | 可指向 `SKILL.md` 或 skill 目录；目录时自动查找 `SKILL.md` |
| `skills[].summary/keywords` | 用于匹配和 prompt 摘要，不命中时不读取全文 |

## 技术方案

### 方案 A：Prompt-level Skill（第一版推荐）

服务端识别命中的 skill 后，读取其 `SKILL.md` 的 frontmatter、标题和核心流程片段，注入到 prompt 的“优先使用技能”区。

优点：

- 不依赖 Claude CLI 原生 skill 注册机制。
- 对 `.cursor/skills`、标准 `SKILL.md`、普通 Markdown 技能说明都可兼容。
- 可严格控制注入长度和读取时机。

缺点：

- 不是 Claude CLI 原生 skill，不能自动出现在 CLI 的 skill registry 中。
- 需要服务端负责摘要、截断和冲突处理。

### 方案 B：会话 HOME 临时注册原生 Skills（暂缓，不进入第一版）

当 `fork_claude_home = true` 时，服务端可把本轮命中的标准 skill 目录复制或链接到：

```text
cache/<ip>/<user_id>/<session_id>/.claude_web_home/.claude/skills/<skill-id>/
```

再启动 Claude CLI，使其像本机 skill 一样发现这些能力。

优点：

- 更接近 Claude CLI 原生 skill 行为。

风险：

- Windows symlink 权限、路径冲突、CLI 版本兼容性不确定。
- Cursor 风格 skill 未必等价于 Claude 标准 skill。
- 需要避免把未命中的 skill 全量复制进会话 HOME。

用户已明确第一版不采用该方案。后续只有在 Claude CLI 原生外部 skill 注册机制稳定、且能避免复制/空间浪费时再评估。

## 匹配策略

新增 `bundle_selector.py`，替换当前简单 substring 逻辑。

评分输入：

- 用户当前 message。
- 最近会话摘要或最近 N 条消息。
- 当前已挂载 bundle ids。
- 可选：上一轮工具输出中的关键实体、路径、包名、模块名。

评分特征：

- exact keyword 命中。
- bundle/skill id、title、summary token overlap。
- 中英文 token + 中文 2/3/4-gram。
- 文件名/包名/错误码/业务词命中。
- 用户显式提到 bundle id 时强制命中。
- `always_mount` 强制命中。

输出结构：

```json
{
  "selected_bundles": [
    {
      "id": "android-rdm",
      "score": 0.82,
      "reason": ["keyword: rdm", "keyword: 锁机"]
    }
  ],
  "selected_skills": [
    {
      "bundle_id": "android-rdm",
      "id": "rdm-log-triage",
      "score": 0.76,
      "path": "..."
    }
  ],
  "mounted_paths": ["..."]
}
```

## Prompt 优化

`_skill_bundles_instruction()` 改成四层：

1. **本轮已选 Skill（优先）**
   - 注入 selected skills 的名称、适用场景、核心流程。
   - 明确要求：先按这些 skill 的排查顺序判断，不要直接大范围 grep。
2. **本轮已挂载 Bundle**
   - 展示 bundle 摘要、命中原因、可读 resources。
   - 明确资料/代码路径只有在 skill 指示需要证据时读取。
3. **未挂载 Bundle 摘要**
   - 只展示 id/title/summary，不展示路径。
   - 告诉模型如判断需要额外 bundle，输出结构化请求。
4. **二次扩展协议**
   - 模型可输出一个特殊 marker 请求额外挂载：

```text
CLAUDE_WEB_NEED_BUNDLE: android-fwk
CLAUDE_WEB_NEED_SKILL: android-fwk/ams-triage
REASON: 当前日志涉及 ActivityTaskManager，需要 AMS 排查流程。
```

服务端在外环中识别后，重新选择 bundle/skill 并继续下一轮。

## 二次扩展机制

当前外环已经支持多轮 orchestration。可在 `orchestrator.py` 或 SSE 解析层增加：

1. 捕获 assistant 文本中的 `CLAUDE_WEB_NEED_BUNDLE` / `CLAUDE_WEB_NEED_SKILL`。
2. 验证请求的 id 必须存在于配置中。
3. 把新增 ids 合并到 `mounted_bundle_ids` / `selected_skill_ids`。
4. 下一轮 CLI 启动时把新 bundle/resource 加入 `--add-dir`，并注入对应 skill。
5. 前端可显示“已追加技能包：android-fwk / ams-triage”。

为避免循环：

- 同一 bundle/skill 每个用户请求最多追加一次。
- 最多追加 3 个 bundle 或 5 个 skill。
- 如果请求不存在的 id，记录事件但不挂载。

## 开发阶段

### 阶段 0：审计与兼容设计

- 梳理当前 `claude_web_paths.config.json` 的字段和实际挂载逻辑。
- 明确旧版 `paths` 兼容策略。
- 明确标准 `SKILL.md`、目录型 skill、`.cursor/skills` 的兼容规则。

### 阶段 1：配置 schema 扩展

- `config.py::load_paths_config_file` 支持 `skills`、`resources`、全局阈值。
- skill path 支持文件或目录；目录时查找 `SKILL.md`。
- 旧版 `paths` 自动转成 `resources.kind=generic`。
- 启动日志打印 bundle、skill、resource 数量。

### 阶段 2：Skill 索引器

- 新增 `claude_web/skill_bundle_index.py`。
- 读取 `SKILL.md` frontmatter 的 `name/description`。
- 读取正文前若干字符，抽取 headings 和核心流程。
- 为每个 skill 生成轻量索引：`id/title/summary/keywords/path/source_bundle_id/token_set`。
- 对不存在或不可读 skill 给 warning，不中断启动。

### 阶段 3：相似度选择器

- 新增 `claude_web/bundle_selector.py`。
- 计算 bundle 和 skill 分数，支持阈值。
- 返回 selected bundles、selected skills、mounted resources、命中原因。
- 增加单元测试覆盖中文关键词、英文包名、跨模块、多 bundle 命中、阈值过滤。

### 阶段 4：Prompt 注入重构

- 重写 `_skill_bundles_instruction()`：
  - 已选 skill 在最前，带使用顺序。
  - 已挂载 resource 其次。
  - 未挂载 bundle 只展示摘要。
  - 明确“先 skill，再通用能力，再资料/代码”的优先级。
- 显式注入命中包根目录 `CLAUDE.md` 摘要；不依赖 `--add-dir` 的默认加载行为。
- 限制每个 skill 注入长度，避免长文档吞掉上下文。
- 对 skill 正文过长时只注入 frontmatter + headings + 前 N 字符，并提示可按需 Read。

### 阶段 5：二次扩展协议

- 在外环中识别 `CLAUDE_WEB_NEED_BUNDLE` / `CLAUDE_WEB_NEED_SKILL`。
- 合并新增 bundle/skill 到下一轮挂载。
- SSE 中增加 `bundle_mount` 或 `skill_mount` 事件，便于前端观察。
- 防止重复追加和无限循环。

### 阶段 6：保留项，暂不实施原生 Skill 注册

- 第一版不复制 skill 到会话 `.claude_web_home/.claude/skills/`。
- 不增加 `native_session_skills` 开关。
- 若后续 Claude CLI 提供稳定的外部 skill 只读注册机制，再重新设计；原则是不得复制大目录、不得污染会话 HOME。

### 阶段 7：前端与日志可观测性

- 后端日志记录：
  - bundle 分数、skill 分数、阈值、挂载原因。
  - 本轮注入的 skill 列表。
  - 每次 Claude CLI 交互的 `prompt_chars_total`、`mounted_bundles`、`selected_skills`、`injected_claude_md`。
- 前端思考过程或状态区可选展示：
  - “本轮已加载技能包：android-rdm”
  - “本轮优先 Skill：rdm-log-triage”

### 阶段 8：文档与迁移

- 更新 `README.md` 的 `claude_web_paths.config.json` 章节。
- 新增 `claude_web_paths.config.example.json` 的 v3 示例。
- 标注旧版 `paths` 仍可用，但建议迁移到 `skills/resources`。

### 阶段 9：测试与验收

- 单元测试：
  - config schema 兼容。
  - skill index 解析。
  - bundle/skill selector 评分。
  - prompt 注入顺序。
  - 二次扩展 marker 解析。
- 冒烟测试：
  - 用户问 RDM 锁机，优先命中 RDM skill。
  - 用户问 AMS 启动，优先命中 framework skill。
  - 普通闲聊不挂载任何 bundle path。
  - 跨模块问题可追加第二个 bundle。

## 验收标准

1. 普通聊天不加载任何本地代码/资料/skill 路径。
2. 命中 RDM 问题时，prompt 中优先出现 RDM 相关 skill，而不是直接要求 grep 代码。
3. 命中 framework/AMS 问题时，优先出现 framework skill。
4. 未命中 bundle 的路径不会加入 `--add-dir`。
5. 已命中 bundle 的普通代码路径仍只读。
6. 模型可通过结构化 marker 请求追加 bundle/skill，服务端可在下一轮按需挂载。
7. 所有挂载和注入都有日志可查，便于排查“为什么没加载 skill”。

## 风险与注意事项

- 外部 `.cursor/skills` 不一定是 Claude 标准 skill；第一版应当把它当 Markdown 工作流资料处理。
- 过度注入 skill 全文会增加 token 成本；必须有长度上限和摘要策略。
- 相似度阈值过低会误挂载，过高会漏挂载；需要日志和测试调参。
- 二次扩展协议可能被模型误触发；必须验证 id 存在，并限制次数。
- 原生 session skill 注册存在 CLI 版本兼容风险，第一版不默认启用。
