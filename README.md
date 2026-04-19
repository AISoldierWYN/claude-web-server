# Claude Web Server V2 - 局域网 AI 对话服务

## 项目简介

局域网可访问的 AI 对话服务，基于本地 Claude CLI，支持多用户多会话管理、流式输出、文件上传。

## 架构

```
┌─────────────────────────────────────────────────────┐
│                  局域网设备                           │
│   手机/平板/电脑 ──▶ http://192.168.x.x:8080        │
└───────────────────────┬─────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────┐
│              Windows 服务器 (你的电脑)                │
│   前端页面 ◀──▶ Flask API ◀──▶ Claude CLI          │
│                                                     │
│   cache/<规范化IP>/<user_id>/     ← 按客户端 IP + 用户隔离 │
│     ├── sessions.json                               │
│     └── <session_id>/                               │
│         ├── messages.json                           │
│         ├── memory.md                               │
│         └── uploads/                                │
└─────────────────────────────────────────────────────┘
```

**说明**：`规范化IP` 由服务端根据请求解析（见 `CLAUDE_WEB_TRUST_X_FORWARDED`）。**换 IP 即新目录**，不提供从旧版 `cache/<user_id>/` 单层结构的自动迁移；升级后请自行清空旧 `cache/` 或仅保留备份。

## 功能特性

| 功能 | 说明 |
|------|------|
| 多用户隔离 | 每个浏览器自动分配 user_id，数据按 **IP + user_id** 分目录存储 |
| 多会话管理 | 每个用户可创建多个对话，对话间记忆不共享 |
| 会话持久化 | 聊天记录保存到磁盘，支持加载历史会话继续聊 |
| 流式输出 | AI 回复逐字显示，思考过程可折叠展示 |
| 文件上传 | 支持拖拽/按钮上传，文件按会话缓存 |
| CLI 日志 | 子进程 stderr 与异常退出摘要写入 `logs/users/<user_id>/sessions/<session_id>_cli.log` |
| 删除备份 | 删除会话前将目录备份到 `backups/<日期>/...`，接口可返回 `backed_up_to` |
| 意见反馈 | 前端提交至 `POST /feedback`，落盘 `feedback/<日期>/...` |
| 局域网访问 | 监听 0.0.0.0:8080，手机/平板可直接访问 |
| 可选认证 | 支持简单 Token 认证 |

## 快速开始

详见 **`快速开始.md`**（依赖安装、最小配置、启动与防火墙）。

摘要：复制或保留项目根目录 **`config.ini`**，执行 `pip install -r requirements.txt` 后运行 `python server.py`；默认 **`http://127.0.0.1:8080`**（端口见 `config.ini` 中 `[server] port`）。

## 配置文件详解（`config.ini`）

主配置位于 **`项目根目录/config.ini`**。若文件不存在，将使用内置默认值（仍可通过环境变量覆盖）。

**优先级**：`命令行参数（仅 Token）` > `环境变量` > `config.ini` > `内置默认值`。

### `[server]` — HTTP 服务

| 键 | 默认值 | 说明 |
|----|--------|------|
| `host` | `0.0.0.0` | 监听地址。环境变量：`CLAUDE_WEB_HOST` |
| `port` | `8080` | 监听端口。环境变量：`CLAUDE_WEB_PORT` |

### `[auth]` — 访问令牌

| 键 | 默认值 | 说明 |
|----|--------|------|
| `token` | 空 | 非空则需在 URL 带 `?token=`。也可用 `python server.py <token>` 或环境变量 `CLAUDE_WEB_TOKEN` 覆盖 |

### `[claude]` — Claude CLI 与子进程行为

| 键 | 默认值 | 说明 |
|----|--------|------|
| `cli_path` | 空 | 可执行文件路径；空则自动在 PATH / Windows npm 下查找 `claude`。环境变量：`CLAUDE_WEB_CLI_PATH` |
| `model` | 空 | 非空则附加 `--model <值>`（需当前 CLI 版本支持）。环境变量：`CLAUDE_WEB_MODEL` |
| `permission_mode` | `bypassPermissions` | 传给 CLI 的 `--permission-mode`。环境变量：`CLAUDE_WEB_PERMISSION_MODE` |
| `dangerously_skip_permissions` | `false` | 为 `true` 时追加彻底跳过权限相关参数（仅可信环境）。环境变量：`CLAUDE_WEB_DANGEROUSLY_SKIP_PERMISSIONS` |
| `isolate_home` | `false` | 为 `true` 时会话子进程 HOME 指向会话目录，隔离 `~/.claude`，有副作用，见下文。环境变量：`CLAUDE_WEB_ISOLATE_HOME` |
| `orch_max_rounds` | `20` | 外环编排单轮用户消息最多启动多少次完整 `claude` 子进程。环境变量：`CLAUDE_WEB_ORCH_MAX_ROUNDS` |
| `extra_args` | 空 | 附加 CLI 参数（按 shell 规则拆分），例如需传递其它官方开关时。环境变量：`CLAUDE_WEB_EXTRA_CLI_ARGS` |

### `[paths]` — 目录与技能包 JSON

| 键 | 默认值 | 说明 |
|----|--------|------|
| `paths_config_file` | `claude_web_paths.config.json` | 技能包/只读路径 JSON，相对项目根；也可配绝对路径。环境变量：`CLAUDE_WEB_PATHS_CONFIG_FILE` |
| `cache_dir` / `log_dir` / `backups_dir` / `feedback_dir` | 空 | 空则分别为项目根下 `cache`、`logs`、`backups`、`feedback`。可填绝对路径或相对项目根的路径。对应环境变量：`CLAUDE_WEB_CACHE_DIR` 等 |

### `[upload]` — 上传

| 键 | 默认值 | 说明 |
|----|--------|------|
| `max_size_mb` | `10` | 单文件上传上限（MB）。环境变量：`CLAUDE_WEB_UPLOAD_MAX_MB` |

### `[proxy]` — 反向代理

| 键 | 默认值 | 说明 |
|----|--------|------|
| `trust_x_forwarded` | `false` | 为 `true` 时使用 `X-Forwarded-For` 首跳作为客户端 IP。环境变量：`CLAUDE_WEB_TRUST_X_FORWARDED` |

### `[readonly]` — 额外只读目录（扁平）

| 键 | 默认值 | 说明 |
|----|--------|------|
| `dirs` | 空 | 分号或英文逗号分隔的目录列表；与 `claude_web_paths.config.json` 及环境变量 `CLAUDE_WEB_READONLY_DIRS` 合并去重后进入 `--add-dir`。环境变量优先于本项 |

### 技能包 JSON（`claude_web_paths.config.json`）

与 `config.ini` 并列，用于 **分类技能包**（`bundles`）及补充 `readonly_dirs`，详见后文「Skills 从哪加载」一节。

---

以下为 **行为说明**（与旧版 README 一致，部分已迁移到 `config.ini` / 环境变量）。

## API 文档

| 接口 | 方法 | 说明 |
|------|------|------|
| `GET /` | GET | 聊天页面 |
| `POST /chat` | POST | 发送消息（SSE 流式响应） |
| `GET /sessions?user_id=xxx` | GET | 获取会话列表 |
| `POST /sessions` | POST | 创建新会话 |
| `DELETE /sessions/<id>?user_id=xxx` | DELETE | 删除会话（先备份再删，成功时 JSON 可含 `backed_up_to`） |
| `GET /sessions/<id>/messages?user_id=xxx` | GET | 获取聊天记录 |
| `POST /upload` | POST | 上传文件（multipart/form-data） |
| `GET /sessions/<id>/files?user_id=xxx` | GET | 获取文件列表 |
| `POST /feedback` | POST | 意见反馈（multipart：`text`、`contact`、`user_id` 可选、`images` 多文件） |

### POST /chat 请求体

```json
{
  "message": "用户消息",
  "user_id": "u-xxxxx",
  "session_id": "sess-uuid",
  "files": ["file1.txt"]
}
```

### SSE 事件类型

| type | 说明 |
|------|------|
| `session` | 返回 Claude session_id |
| `thinking` | AI 思考过程片段 |
| `text` | AI 回复文本片段 |
| `done` | 对话完成 |
| `error` | 发生错误 |

## 目录结构

```
claude-web-server/
├── server.py              # 薄入口，创建 Flask app
├── config.ini             # 主配置（可复制 config.example.ini 后修改）
├── config.example.ini     # 配置模板（与默认项一致，便于版本管理）
├── claude_web_paths.config.json   # 技能包 / 只读路径（可选）
├── 快速开始.md            # 最少步骤与最小配置范围
├── claude_web/            # 应用包（配置、路径、会话、CLI、路由等）
├── static/
│   └── index.html         # 前端页面
├── cache/                 # 运行时数据（cache/<规范化IP>/<user_id>/...）
├── backups/               # 删除会话前的备份（按日期分子目录）
├── feedback/              # 用户反馈落盘
├── logs/                  # server.log；logs/users/... 下为按会话 CLI 日志
├── requirements.txt
├── start.bat
└── README.md
```

## 防火墙配置

放行端口需与 **`config.ini` → `[server]` → `port`** 一致（默认 **8080**）。示例（管理员 PowerShell）：

```powershell
netsh advfirewall firewall add rule name="Claude Web Server" dir=in action=allow protocol=tcp localport=8080
```

## 环境变量与权限策略

本节中的 **`CLAUDE_WEB_*` 环境变量** 与 **`config.ini` 对应键** 等价；已在上一节「配置文件详解」表中列出时，**任选其一**即可（环境变量优先级更高）。下列小节保留 **PowerShell 示例** 与 **行为说明**，便于在不改 INI 的情况下临时覆盖。

Claude Code 通过 **`--add-dir`** 限定工具可访问的目录，通过 **`--permission-mode`** 等控制在 **非交互 `--print` 模式** 下是否还要在终端里逐项确认。服务端会为每个会话设置 **工作目录（cwd）** 为 `cache/<规范化IP>/<user_id>/<session_id>/`，并把 **本会话目录**、**`CLAUDE_WEB_READONLY_DIRS` / `config.ini` `[readonly] dirs`** 与 **`claude_web_paths.config.json`** 中的 **`readonly_dirs`**（合并去重后）一并加入 `--add-dir`。

### `CLAUDE_WEB_TRUST_X_FORWARDED`

设为 **`1` / `true` / `yes` / `on`** 时，优先使用请求头 **`X-Forwarded-For` 的第一跳** 作为客户端 IP（用于 `cache/` 分目录）。**仅在反向代理已正确设置该头、且你信任上游网络时使用**；否则伪造该头可能影响目录隔离。

### 长期记忆 `memory.md`

每个会话目录下会自动维护 **`memory.md`**。服务端在每条请求中注入规则：要求模型通过 **Read / Edit / Write** 更新该文件来保存长期记忆，**不要**依赖 Claude 内置「记忆」写全局 `~/.claude`（易在无交互环境下失败）。

### `CLAUDE_WEB_PERMISSION_MODE`

控制传给 Claude CLI 的 **`--permission-mode`**，影响非交互场景下工具是否仍被拦截。

| 取值 | 说明 |
|------|------|
| **`bypassPermissions`**（**默认**） | 在 CLI 允许的目录内尽量少拦工具，适合 Web 无终端确认的场景 |
| `acceptEdits` | 偏保守，主要自动接受编辑类操作 |
| `auto` / `dontAsk` / `plan` | 见 Claude Code 官方说明 |
| `default` | 易在 `--print` 下出现「需用户批准」而失败，一般不推荐 |

示例（PowerShell）：

```powershell
$env:CLAUDE_WEB_PERMISSION_MODE = "acceptEdits"
python server.py
```

### `CLAUDE_WEB_DANGEROUSLY_SKIP_PERMISSIONS`

设为 **`1` / `true` / `yes` / `on`** 时，会为 CLI 追加 **`--allow-dangerously-skip-permissions`** 与 **`--dangerously-skip-permissions`**，**彻底跳过**权限询问（包括 Bash 等）。**仅限可信环境**，启动日志会打出 WARNING。

```powershell
$env:CLAUDE_WEB_DANGEROUSLY_SKIP_PERMISSIONS = "1"
python server.py
```

### `CLAUDE_WEB_READONLY_DIRS`

附加的 **只读** 工程目录列表（服务端通过 `--add-dir` 放行；**请勿**在提示词外依赖系统级「只读锁」）。分隔符支持 **换行**、英文 **分号 `;`**、**逗号**（路径中勿含逗号）。

```powershell
$env:CLAUDE_WEB_READONLY_DIRS = "D:\Code\my-project;D:\docs"
python server.py
```

### Skills 从哪加载？`claude_web_paths.config.json`

- **Claude Code 默认**：在本机未启用 `CLAUDE_WEB_ISOLATE_HOME` 时，**skills、配置、凭证** 仍来自当前系统用户下的 **`~/.claude`**（Windows 一般为 `%USERPROFILE%\.claude`）。这是 **Claude CLI 自身行为**，不是本仓库单独实现的。
- **本服务额外放行目录**：在**仓库根目录**放置 **`claude_web_paths.config.json`**（可复制 `claude_web_paths.config.example.json` 改名）。
  - **`readonly_dirs`**：扁平路径列表（与旧版兼容），与 **`CLAUDE_WEB_READONLY_DIRS`** 合并去重后全部进入 **`--add-dir`**。
  - **`bundles`（技能包）**：按类分组；每包含 **`id`**、**`title`**、**`summary`**、**`paths`**。所有 **`paths`** 同样合并进 **`--add-dir`**；**默认注入模型的主要是各包的 `summary` 与路径列表**，便于先理解「哪类问题用哪包」，需要时再 Read/Grep 具体文件，而不是默认加载整目录内容。
- 可选 **`notes`**：全局字符串，追加在沙箱提示末尾（与各包的 `summary` 不同）。

示例（节选）：

```json
{
  "version": 2,
  "notes": "全局补充说明（可选）。",
  "readonly_dirs": [],
  "bundles": [
    {
      "id": "my-team",
      "title": "团队技能与文档",
      "summary": "本包涵盖某某业务；用户问到相关问题时再深入下列路径。",
      "paths": ["D:/Tools/skills", "D:/Docs/api"]
    }
  ]
}
```

配置文件缺失或 JSON 无效时不会报错，仅使用环境变量中的目录。

### `CLAUDE_WEB_ISOLATE_HOME`（一般不推荐）

设为 **`1` / `true` / `yes` / `on`** 时，子进程会将 **`HOME` / `USERPROFILE`**（及 Windows 下部分 AppData 变量）指到 **当前会话的 cache 目录**，使 `~/.claude` 落在会话内，从而与「全局记忆」隔离。

**副作用**：子进程 **不再** 使用系统用户主目录，**可能无法继承** 本机已有的全局 Claude 配置、API、skills，甚至导致 CLI 异常退出。**默认关闭**；仅在明确需要「记忆文件物理隔离」且可接受上述代价时开启。

### 其它

| 变量 | 说明 |
|------|------|
| `CLAUDE_WEB_TOKEN` | 与 `python server.py <token>` 类似，用于鉴权 |
| `CLAUDE_WEB_HOST` / `CLAUDE_WEB_PORT` | 监听地址与端口，同 `config.ini` `[server]` |
| `CLAUDE_WEB_CLI_PATH` / `CLAUDE_WEB_MODEL` / `CLAUDE_WEB_EXTRA_CLI_ARGS` | 同 `config.ini` `[claude]` |
| `CLAUDE_WEB_CACHE_DIR` 等 | 数据目录，同 `config.ini` `[paths]` |

## 注意事项

- 需要本机已安装 Claude CLI（或于 `config.ini` 的 `cli_path` 指定路径）；若未在 PATH 中，请配置 `cli_path`。
- 文件上传大小上限见 **`config.ini` → `[upload]` → `max_size_mb`**（默认 10MB）。
- 局域网内无认证时，任何人都可使用你的 Claude API 额度；生产或公网前请设置 `token`。
- `cache/`、`backups/`、`feedback/`、`logs/` 可能包含用户数据，注意备份与权限。
