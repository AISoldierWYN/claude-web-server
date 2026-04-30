# Codex Web Server V2 - 项目指南

本文档专为 Codex 助手设计，帮助快速理解项目架构与开发约定。

## 项目概述

局域网多用户 AI 对话服务，基于本地 Codex CLI，支持流式输出、文件上传、会话持久化。服务端可在 Windows 或 Linux 上运行，路径与子进程行为按 `sys.platform` 自动适配。

## 架构总览

```
server.py (入口)
    └── claude_web/app_factory.py (Flask 应用工厂)
            ├── config.py (配置加载)
            ├── session_manager.py (会话管理)
            ├── routes.py (HTTP 路由)
            ├── orchestrator.py (外环编排)
            ├── claude_runner.py (CLI 子进程)
            └── user_claude_credentials.py (V2 用户 API 凭证)
```

## 核心模块职责

| 模块 | 职责 |
|------|------|
| `config.py` | 配置加载与合并，优先级：命令行 > 环境变量 > config.ini > 默认值 |
| `session_manager.py` | 多用户多会话 CRUD，数据目录为 `cache/<规范化IP>/<user_id>/<session_id>/` |
| `routes.py` | 所有 HTTP API：`/chat`、`/sessions`、`/upload`、`/feedback` 等 |
| `claude_runner.py` | 构建 prompt、启动 Codex CLI 子进程、解析流式 JSON 输出并转发为 SSE |
| `orchestrator.py` | 外环编排（ReAct 风格）：失败重试、暂停/继续/总结 |
| `host_scope.py` | 判断请求是否来自本机（127.0.0.1/localhost），用于 V2 多用户 API 策略 |
| `user_claude_credentials.py` | V2 功能：按用户保存 Codex API 环境变量与 model |
| `paths.py` | 客户端 IP 解析与路径规范化 |
| `backup_service.py` | 删除会话前备份到 `backups/<日期>/...` |

## 数据目录结构

```
cache/
└── <规范化IP>/           # IPv4 点换下划线，IPv6 冒号换下划线
    └── <user_id>/
        ├── sessions.json
        ├── claude_api_credentials.json  # V2 用户 API 凭证
        └── <session_id>/
            ├── messages.json
            ├── memory.md                 # 长期记忆文件
            └── uploads/
```

## 关键数据流

### `/chat` 请求流程

1. `routes.py:chat()` 接收请求，通过 `client_ip + user_id + session_id` 定位会话
2. `resolve_claude_runtime_for_request()` 判断是否使用 V2 用户 API
3. `orchestrator.stream_orchestrated_turns()` 启动外环编排
4. `claude_runner.stream_claude_output()` 构建完整 prompt 并启动 CLI 子进程
5. 流式 JSON 输出被解析为 SSE 事件转发给前端
6. 对话完成后通过回调异步保存消息到 `messages.json`

### Prompt 构建顺序

`claude_runner.py` 中 `stream_claude_output()` 按以下顺序拼接到用户消息前：

1. **技能包索引**：`_skill_bundles_instruction()` - 注入各包摘要与路径
2. **沙箱约束**：`_sandbox_instruction()` - 会话目录可写、只读目录仅读
3. **记忆规则**：`_memory_prompt_block()` - 指定使用 `memory.md` 作为长期记忆
4. **语言对齐**：`_language_alignment_block()` - 引导与用户语言一致
5. **附件内容**：`_turn_attachment_instruction()` + 文件内容内联

### SSE 事件类型

| type | 说明 |
|------|------|
| `session` | Codex CLI 返回的 session_id |
| `thinking` | AI 思考过程片段 |
| `text` | AI 回复文本片段 |
| `tool_start` / `tool_stop` | 工具调用开始/结束 |
| `error` | 错误（含 `soft: true` 表示 API 软错误可重试） |
| `done` | 单轮完成 |
| `orchestration_round` | 外环轮次信息 |
| `needs_continue` | 达到轮数上限，需用户选择继续或总结 |

## CLI 子进程参数

`stream_claude_output()` 构建的命令示例：

```bash
Codex --output-format stream-json --include-partial-messages --verbose --print \
    --permission-mode bypassPermissions \
    --session-id <session_id> \
    --add-dir <session_workspace> --add-dir <readonly_dir1> ... \
    -- <prompt via stdin>
```

若配置了 `--resume <claude_session_id>`，则复用 Codex 的对话上下文。

## 配置要点

### 环境变量（优先级高于 config.ini）

| 变量 | 说明 |
|------|------|
| `CLAUDE_WEB_TOKEN` | 访问令牌 |
| `CLAUDE_WEB_PERMISSION_MODE` | CLI `--permission-mode`（默认 `bypassPermissions`） |
| `CLAUDE_WEB_DANGEROUSLY_SKIP_PERMISSIONS` | 彻底跳过权限询问（可信环境） |
| `CLAUDE_WEB_READONLY_DIRS` | 只读目录（分号/逗号/换行分隔） |
| `CLAUDE_WEB_V2_MULTI_USER_API` | 启用局域网每用户 API |
| `CLAUDE_WEB_ISOLATE_HOME` | 子进程 HOME 指向会话目录（隔离记忆） |

### config.ini 关键配置

```ini
[Codex]
permission_mode = bypassPermissions
dangerously_skip_permissions = false
orch_max_rounds = 20

[features]
v2_multi_user_api = false
v3_linux_deploy = false
```

## 开发约定

### 路径规范

- 所有路径在传递给 Codex CLI 前转换为 POSIX 格式（正斜杠）
- 会话目录作为 CLI `cwd`，文件操作相对路径 `uploads/文件名`
- Read 工具的 `file_path` **禁止**使用绝对路径（含盘符）

### 文件名处理

- `filename_sanitize.py`: 安全处理上传文件名，支持 Unicode
- 非 ASCII 文件名存储为 `f_<uuid>.<ext>`，但返回给前端用原文件名

### 错误处理

- API 软错误（如 `invalid params`）会触发外环重试
- CLI 非零退出时记录到 `logs/users/<user_id>/sessions/<session_id>_cli.log`

### 线程安全

- `SessionManager` 使用 `threading.Lock` 按 `client_ip|user_id` 粒度加锁
- `/chat` 流结束后通过 `threading.Thread` 异步保存消息

## 常见任务

### 添加新 API 端点

在 `routes.py` 的 `register_routes()` 函数中添加，使用 `@optional_token` 装饰器处理认证。

### 修改 Prompt 注入逻辑

编辑 `claude_runner.py` 中的 `_*_instruction()` 函数，注意控制总长度。

### 调整外环编排策略

修改 `orchestrator.py` 中的 `stream_orchestrated_turns()` 和相关提示词函数。

### 支持新文件类型提取

在 `claude_runner.py` 的 `_ATTACHMENT_INLINE_SKIP_SUFFIX` 和 `_read_attachment_for_prompt()` 中添加逻辑。

## 依赖

```
flask>=3.0.0
pypdf>=4.0.0
```

## 启动命令

```bash
# Windows
python server.py

# Linux/macOS
python server.py
```

启动后监听 `0.0.0.0:8080`（可通过 `config.ini` 修改）。
