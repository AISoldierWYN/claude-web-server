# Gemini CLI 支持开发计划（V3 - 稳健接入版）

本计划用于在 `claude-web-server` 中增加 Gemini CLI 支持。核心原则是：默认关闭、最小影响现有 Claude 路径、保证多用户多会话隔离、先稳定接入普通聊天，再评估开发模式等高风险能力。

## 实施状态

- 阶段 0：已完成本机 Gemini CLI 能力验证。
- 阶段 1：已完成配置项、feature flag、`/api/features` 暴露。
- 阶段 2：已完成会话 `provider` 与 `provider_session_ids` 数据兼容；Gemini 会话创建受 feature flag 保护。
- 阶段 3：已完成 `gemini_runner.py`，支持 Gemini CLI `stream-json` 到现有 SSE 的转换、代理注入、超时与停止清理。
- 阶段 4：已完成路由/编排分发，Claude 默认路径不变，Gemini 会话可走现有外环、Tavily、上传内联与会话恢复。
- 阶段 5：已完成前端 provider 选择、会话 C/G 标识、Gemini 会话禁用开发模式入口与无 thinking 展示策略。
- 阶段 6：已完成离线 fake Gemini CLI 测试；真实 Gemini 手动验收待按步骤执行。

## 阶段 0 验证结论

验证环境：

- Gemini CLI 版本：`0.40.1`
- 本机直连请求会超时或触发认证/网络问题；使用代理 `HTTP_PROXY=http://127.0.0.1:1080` 和 `HTTPS_PROXY=http://127.0.0.1:1080` 后 headless 请求成功。

已确认：

- `gemini --help` 确认支持 `--prompt/-p`、`--resume/-r`、`--list-sessions`、`--include-directories`、`--approval-mode`、`--sandbox`、`--skip-trust`、`--output-format stream-json`。
- `--prompt` 会和 stdin 输入拼接；后端 runner 应继续采用 Python `subprocess.Popen(..., stdin=PIPE, encoding='utf-8')` 写入 prompt，避免 Windows 命令行长度限制。
- PowerShell 管道传中文时出现过编码损坏，不能以 PowerShell 管道结果判断 Gemini 对中文 stdin 的能力；后端必须显式使用 UTF-8。
- `stream-json` 输出至少包含：
  - `init`：包含 `session_id` 和 `model`
  - `message`：包含 `role`、`content`、`delta`
  - `tool_use`：包含 `tool_name`、`tool_id`、`parameters`
  - `tool_result`：包含 `tool_id`、`status`
  - `result`：包含 `status`、`stats`
- 首轮调用应从 `init.session_id` 提取并保存 Gemini 会话 ID。
- `--resume <session_id>` 可恢复指定会话，恢复后 `init.session_id` 保持为同一个 ID，模型可读取上一轮上下文。
- `--approval-mode default` 对只读目录 listing 没有阻塞，但写文件测试在隔离临时目录中超时，说明涉及写入/高风险工具时会等待交互确认。

阶段 0 对实现的约束：

- 禁止使用 `--resume latest`。
- Web 第一版不要使用 `approval_mode=default` 承担写操作场景，否则容易让 SSE 长时间卡住。
- Gemini 第一版暂不接入“移动端远程开发控制”的真实项目写入能力。
- Gemini runner 必须接入运行中进程注册和停止机制，避免阻塞或超时时留下不可控子进程。
- 如果服务端环境需要代理，应支持 `[gemini] proxy` 或复用通用代理环境变量注入。

## 总体边界

第一版 Gemini 支持目标：

- 支持新建 Gemini 普通聊天会话。
- 支持 Gemini 会话持久化和指定会话恢复。
- 支持现有 Markdown 渲染、历史消息加载、导出 HTML。
- 支持现有上传文件的“服务端内联文本”方式。
- 支持 Tavily 搜索结果注入后交给 Gemini 整理。
- 支持工具调用事件展示，但不承诺 thinking 面板。

第一版暂不做：

- 不支持 Gemini 开发模式真实项目写入。
- 不支持 Gemini 每用户 Google 凭证隔离。
- 不支持中途切换一个已有会话的 provider。
- 不做 `AIDriver` 大重构。
- 不自动 push、commit 或执行高风险写操作。

## 阶段 1：功能开关与配置

在 `config.ini` 中新增：

```ini
[features]
gemini_support = false

[gemini]
cli_path =
model =
approval_mode = plan
sandbox = false
skip_trust = true
proxy =
request_timeout_seconds = 300
```

说明：

- `gemini_support` 默认关闭，关闭时不影响任何现有 Claude 功能。
- `approval_mode` 第一版建议默认 `plan`，避免写操作等待交互确认。若后续明确支持写操作，再评估 `auto_edit` 或自定义确认 UI。
- `proxy` 可选；为空时继承服务端环境变量。若配置为 `http://127.0.0.1:1080`，启动 Gemini 子进程时注入 `HTTP_PROXY` 和 `HTTPS_PROXY`。
- `skip_trust` 默认 true，减少 headless 模式下的信任交互。

`/api/features` 应增加：

```json
{
  "gemini_support": true,
  "gemini_configured": true
}
```

## 阶段 2：会话数据兼容

在 `sessions.json` 中为新会话增加：

```json
{
  "provider": "gemini",
  "provider_session_ids": {
    "gemini": "55deaf42-881a-4a2d-976a-e04d93a82a3a"
  }
}
```

兼容规则：

- 旧会话没有 `provider` 时默认 `claude`。
- 保留现有 `claude_session_id` 字段，Claude 路径继续优先读写它，避免影响历史数据。
- 新增 `provider_session_ids` 作为多 provider 的扩展字段。
- 新建会话时允许传入 `provider`，只接受 `claude` 或已启用的 `gemini`。
- 会话创建后 provider 不可变。

## 阶段 3：新增 `gemini_runner.py`

不要重命名或大改 `claude_runner.py`。新增 `claude_web/gemini_runner.py`，提供和当前编排层可对接的函数，例如：

```python
stream_gemini_output(...)
stop_gemini_session_process(session_id)
```

### CLI 参数映射

Gemini CLI 调用建议：

```bash
gemini --output-format stream-json \
  --approval-mode plan \
  --skip-trust \
  --include-directories <readonly_or_workspace_dirs> \
  --resume <gemini_session_id>
```

首轮没有 `gemini_session_id` 时不传 `--resume`。

模型配置：

```bash
--model <model>
```

目录配置：

- Claude 的 `--add-dir` 对应 Gemini 的 `--include-directories`。
- 普通聊天模式仍以 session cache 作为 cwd。
- Gemini 第一版不进入真实项目开发 cwd。

### 输入策略

- 使用 stdin 写入完整 prompt。
- 不把长 prompt 放到 `-p "..."` 中。
- 可以传一个短 `-p "Use the stdin prompt."` 作为 headless 触发参数，但真正内容必须来自 stdin。
- Python 子进程必须设置 `encoding='utf-8'` 和 `errors='replace'`。

### 输出映射

将 Gemini JSONL 转换为现有 SSE：

| Gemini 事件 | SSE 事件 | 处理 |
|---|---|---|
| `init.session_id` | `session` | 保存到 `provider_session_ids.gemini` |
| `message.role=assistant` | `text` | `delta=true` 时直接追加 |
| `tool_use` | `tool_start` + `tool_input_delta` | `parameters` 转 JSON |
| `tool_result` | `tool_stop` | 可附带状态摘要 |
| `result.status=success` | `done` | `ok=true` |
| `result.status!=success` 或 `error` | `error` / `done` | 给出友好错误 |

第一版不映射 thinking。

### 进程停止

Gemini runner 必须像 Claude runner 一样注册运行中的子进程：

- key 使用 Web `session_id`
- stop API 能 terminate/kill 对应 Gemini CLI
- 超时、异常、用户停止后必须清理进程表

## 阶段 4：编排层和路由接入

`orchestrator.py` 或路由层根据会话 `provider` 分发：

- `claude` -> `claude_runner.stream_claude_output`
- `gemini` -> `gemini_runner.stream_gemini_output`

要求：

- Claude 默认路径不改变。
- Gemini 支持现有外环编排的基本重试和暂停逻辑。
- Gemini 的 API/认证错误要转换成用户可读错误，避免直接暴露超长堆栈。
- 如果 Gemini 未认证或需要浏览器 OAuth，Web 请求应快速返回明确错误，不要把 prompt 写入认证交互 stdin。

## 阶段 5：前端 UI

新增 provider 选择只影响“新建会话”：

- 默认仍为 Claude。
- 当 `gemini_support=true` 时显示 provider 选择。
- 新建会话时带上 `provider`。
- 会话列表显示 C/G 标识。
- 已有会话不允许切换 provider。

Gemini 会话中：

- 不显示 thinking 面板或显示“当前 provider 不提供思考过程”。
- 开发项目按钮默认禁用或提示“Gemini 第一版暂不支持开发模式”。
- Tavily、上传内联、导出 HTML 继续沿用现有逻辑。

## 阶段 6：测试

离线测试优先：

- 新增 fake Gemini CLI 脚本，输出固定 JSONL。
- 测试 `init`、`message delta`、`tool_use`、`tool_result`、`result success/error` 的解析。
- 测试首轮保存 `provider_session_ids.gemini`。
- 测试后续轮次使用 `--resume <id>`，禁止出现 `--resume latest`。
- 测试 `gemini_support=false` 时，前端和后端不暴露 Gemini 入口。
- 测试旧 session 没有 provider 时仍按 Claude 处理。

真实 Gemini 手动验收：

- 配置代理后可完成 headless 请求。
- 普通 Gemini 会话可连续两轮恢复上下文。
- `approval_mode=plan` 不会因写操作等待确认。
- 断网、认证过期、模型 429 时能给出可读错误。

建议手动验收步骤：

1. 在服务端 PC 终端确认 Gemini CLI 可用：
   ```powershell
   gemini --version
   ```
2. 若需要代理，先用代理做一次 headless 自检：
   ```powershell
   $env:HTTP_PROXY='http://127.0.0.1:1080'
   $env:HTTPS_PROXY='http://127.0.0.1:1080'
   '只回复 GEMINI_OK' | gemini --output-format stream-json --approval-mode plan --skip-trust -p 'Use stdin prompt.'
   ```
3. 在本地未提交的 `config.ini` 中启用：
   ```ini
   [features]
   gemini_support = true

   [gemini]
   approval_mode = plan
   skip_trust = true
   proxy = http://127.0.0.1:1080
   ```
4. 重启 `python server.py`，打开 `http://127.0.0.1:8080`。
5. 侧边栏新建会话处选择 `Gemini`，发送一句普通问题，确认：
   - 会话列表显示 `G` 标识；
   - 回复能流式显示；
   - 不显示 thinking 面板，显示“Gemini 会话不提供可见思考过程”。
6. 在同一个 Gemini 会话中发送第二个问题，让它引用上一轮内容，确认可以恢复上下文。
7. 开启“联网”，问一个需要最新信息的问题，确认 Tavily 搜索结果会交给 Gemini 整理。
8. 上传一个 `.txt` 或 `.md` 文件并提问，确认附件内容被 Gemini 正确使用。
9. 在 Gemini 会话点击“开发项目”，确认会提示第一版暂不支持开发模式，不会进入真实项目写入。
10. 临时关闭代理或让 Gemini 认证失效，确认页面能返回可读错误，而不是无限重试或卡住。

## 关键差异映射表

| 功能 | Claude CLI | Gemini CLI |
|---|---|---|
| 会话恢复 | `--resume <id>` | `--resume <id>`，禁止 `latest` |
| 权限控制 | `--permission-mode` | `--approval-mode` |
| 目录放行 | `--add-dir` | `--include-directories` |
| 沙箱模式 | permission mode + Web prompt 约束 | `--sandbox` |
| 信任提示 | Claude 配置/权限模式 | `--skip-trust` |
| 流式输出 | `stream-json` | `stream-json` |
| 思考过程 | `thinking` 事件 | 第一版不支持 |
| 代理 | 继承服务端环境 | 需要显式支持代理注入 |

## 剩余风险

- Gemini CLI 认证状态依赖服务端 PC 的全局 Google 登录，局域网用户会共享该身份和额度。
- Gemini `approval-mode=default` 在写操作时会阻塞 Web 流；第一版必须避免。
- Gemini 默认模型可能出现 429 或容量不足，需要友好降级错误。
- Gemini CLI 可能输出 warning 到 stderr，runner 需要区分 warning 和真正失败。
- Gemini session 存储位置与 HOME 隔离还需在实现时验证，防止跨 Web 会话污染。
