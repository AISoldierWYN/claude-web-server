# 自动化测试建设计划

## 实施状态

第一阶段已落地：

- 后端冒烟测试：`tests/test_smoke_backend.py`
- 前端静态/函数级冒烟测试：`tests/smoke_frontend.mjs`
- 测试命令已补充到 `README.md`

当前测试命令：

```powershell
python -m compileall -q server.py claude_web
python -m unittest tests.test_smoke_backend
node tests/smoke_frontend.mjs
```

## 目标

为当前 Claude Web Server 建立一套轻量但稳定的自动化验证流程，优先覆盖用户最常用、也最容易在后续开发中回归的功能：服务启动、会话收发、Markdown 渲染、上传、联网搜索、导出、移动端布局。

## 范围

第一阶段先做端到端冒烟测试，不追求覆盖全部边界条件，目标是每次改动后能快速回答两个问题：

- 服务是否还能正常启动并响应核心 API。
- 前端关键交互是否还能完成一轮真实用户流程。

## 测试分层

### 1. 后端 API 冒烟测试

建议新增 `tests/smoke_backend.py` 或等价脚本，使用临时 cache/config 启动 Flask 测试客户端或本地服务，覆盖：

- `/api/features` 返回功能开关。
- `/sessions` 能创建、列出、删除会话。
- `/upload` 能接受合法文件，并拒绝超过 100 MB 的文件。
- `/chat` 在未配置 Tavily 时，勾选联网会返回 `tavily_config_required`。
- `settings_loader` 能读取带 UTF-8 BOM 的 `config.ini`。

### 2. 前端浏览器冒烟测试

建议使用 Playwright，新增 `tests/smoke_frontend.spec.*` 或脚本，覆盖桌面与移动视口：

- 打开首页后能看到会话列表、输入框和发送按钮。
- 创建新会话，发送一条普通消息。
- 渲染 Markdown 标题、列表、代码块和表格。
- 切换会话或刷新页面后，历史消息仍按 Markdown 渲染。
- 勾选 Markdown 输入预览时，用户输入区域能展示预览。
- iPhone 视口下顶部按钮不进入安全区，底部输入区不被 Markdown/联网开关压扁。
- 会话菜单存在“导出 HTML”和“删除”入口。

### 3. 联网搜索可选测试

联网测试默认不在 CI 或普通本地测试里强制运行，避免消耗 Tavily 额度。仅在环境变量 `TAVILY_API_KEY` 存在时启用：

- 勾选联网后，服务端先返回搜索状态事件。
- 搜索成功后，SSE 继续进入 Claude 整理阶段。
- 搜索失败时，前端能展示软错误，不阻塞页面继续使用。

### 4. 导出 HTML 验证

导出功能涉及浏览器文件保存能力，第一阶段可以先拆成纯函数测试或浏览器测试：

- 导出的 HTML 只包含当前会话正文。
- AI 思考过程保留。
- Markdown 内容在离线 HTML 中已渲染为表格、代码块、列表等结构。
- 导出文件不依赖在线资源。

## 推荐落地顺序

1. 新增后端 smoke 脚本，保证不依赖真实 Claude CLI 和真实 Tavily。
2. 新增前端 Playwright 基础用例，先验证页面加载、移动端布局和 Markdown 渲染。
3. 把 Markdown 渲染、导出 HTML 这类前端逻辑抽成可测试函数，减少只能靠浏览器截图判断的问题。
4. 增加可选联网测试，用环境变量显式开启。
5. 在 README 中补充 `python` / `playwright` 的测试命令。

## 验收标准

- 一条命令可以跑完后端冒烟测试。
- 一条命令可以跑完前端桌面和移动端冒烟测试。
- 未配置 Tavily、未安装 Claude CLI 的开发环境也能跑完大部分测试。
- 真实 Claude CLI 和 Tavily 相关测试通过环境变量显式开启。
- 测试失败时能指出具体失败功能，而不是只给出模糊的页面超时。

## 待确认事项

- 前端测试使用 Node/Playwright 还是 Python Playwright。
- 是否引入 pytest 作为后端测试框架。
- 是否需要在 GitHub Actions 中运行，或仅作为本地开发脚本。
- 是否为测试增加专用 `config.test.ini` 和临时 cache 目录。
