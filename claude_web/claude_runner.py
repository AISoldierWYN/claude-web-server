"""Claude CLI 子进程与流式输出。"""

import json
import logging
import os
import queue
import shutil
from collections import deque
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional


from . import config
from .filename_sanitize import safe_client_filename
from .session_manager import (
    SESSION_MEMORY_FILENAME,
    USER_GLOBAL_MEMORY_FILENAME,
    ensure_session_global_memory_file,
    ensure_session_memory_file,
)
from .user_session_log import append_cli_exit_summary, append_cli_line

log = logging.getLogger('claude-web')

_RUNNING_PROCESSES: Dict[str, subprocess.Popen] = {}
_RUNNING_PROCESSES_LOCK = threading.Lock()


def stop_session_process(session_id: str) -> bool:
    sid = (session_id or '').strip()
    if not sid:
        return False
    with _RUNNING_PROCESSES_LOCK:
        proc = _RUNNING_PROCESSES.get(sid)
    if not proc or proc.poll() is not None:
        return False
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        return True
    except Exception as e:
        log.warning('[CLI] 停止会话进程失败 session=%s: %s', sid, e)
        return False


def _terminate_process(process: subprocess.Popen, *, reason: str) -> None:
    if not process or process.poll() is not None:
        return
    try:
        log.info('[CLI] 终止子进程 reason=%s pid=%s', reason, getattr(process, 'pid', None))
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    except Exception as e:
        log.warning('[CLI] 终止子进程失败 reason=%s: %s', reason, e)


def _friendly_api_error_from_result(data: dict) -> str:
    """从 Claude Code result 行提取可读 API 错误（避免把整段 stdout 塞给用户）。"""
    r = data.get('result')
    if r is None:
        return '上游返回失败（无 result 详情）'
    if isinstance(r, dict):
        s = json.dumps(r, ensure_ascii=False)
    else:
        s = str(r)
    if len(s) > 800:
        s_trim = s[:800] + '…'
    else:
        s_trim = s
    low = s.lower()
    if 'api error' in low or '"error"' in s:
        idx = s.find('{')
        if idx >= 0:
            try:
                j = json.loads(s[idx:])
                err = j.get('error')
                if isinstance(err, dict):
                    code = err.get('code')
                    msg = err.get('message') or err.get('type')
                    if msg:
                        extra = f'（code: {code}）' if code else ''
                        return f'API：{msg}{extra}'
                elif isinstance(err, str):
                    return f'API：{err}'
            except Exception:
                pass
    if 'invalid params' in low or '20024' in s:
        return (
            'API：invalid params（code 20024）。'
            '常见原因：① Read 的 file_path 使用了**绝对路径**（含盘符），请改为仅用 `uploads/文件名`；'
            '② 对 PDF 使用 Read 时部分上游会直接失败——已在本服务侧尽量改为注入提取文本，避免再 Read PDF；'
            '③ 会话上下文过大。详情：'
            + s_trim
        )
    return '执行失败：' + s_trim


def _is_recoverable_resume_failure(data: dict, claude_session_id: str, visible_output_seen: bool) -> bool:
    """判断旧 Claude CLI session 恢复失败是否适合后台重建。"""
    if not claude_session_id or visible_output_seen or not data.get('is_error'):
        return False
    result = data.get('result')
    if result is None:
        return True
    try:
        haystack = json.dumps(data, ensure_ascii=False).lower()
    except Exception:
        haystack = str(data).lower()
    markers = (
        'session',
        'resume',
        'expired',
        'not found',
        'does not exist',
        'invalid session',
        'conversation',
        '会话',
        '过期',
        '不存在',
    )
    return any(m in haystack for m in markers)


def build_api_error_retry_user_message(friendly_error: str) -> str:
    """
    在 API 软错误后作为第二轮 user 消息注入（配合 --resume），引导模型换策略而非重复失败操作。
    """
    err = (friendly_error or '').strip() or '（无详情）'
    return (
        '【Web 服务注入 — 上一轮上游 API 已返回错误，请阅读下列摘要并换可行策略，不要重复导致失败的操作】\n'
        f'{err}\n\n'
        '可尝试：① 若与 PDF/Read 有关：在会话目录用 Bash 运行 `python` + `pypdf` 或系统 `pdftotext` 等提取文本；'
        '② Read 的 file_path 仅用 `uploads/文件名` 相对路径；③ 扫描件需 OCR 或请用户先导出文本。'
    )


# 供 routes / server 等引用，与 config 同步
CLAUDE_CLI_PATH = config.CLAUDE_CLI_PATH


def _resolve_session_upload_paths(upload_dir: Path, filenames: list) -> list:
    out = []
    if not filenames:
        return out
    try:
        base = upload_dir.resolve()
    except OSError:
        base = upload_dir
    for raw in filenames:
        if not raw or not isinstance(raw, str):
            continue
        name = safe_client_filename(Path(raw).name)
        if not name:
            continue
        fp = (base / name).resolve()
        try:
            fp.relative_to(base)
        except ValueError:
            log.warning(f'[Chat] 拒绝越界附件路径: {raw!r}')
            continue
        if fp.is_file():
            # POSIX 路径供提示词与模型填入工具参数，避免 Windows 反斜杠触发上游 invalid params（如 code 20024）
            out.append(fp.resolve().as_posix())
        else:
            log.warning(f'[Chat] 会话 uploads 中不存在文件: {name} (期望目录 {base})')
    return out


def _memory_prompt_block(session_workspace: str, shrink: bool = False) -> str:
    if not session_workspace:
        return ''
    try:
        sd = Path(session_workspace).resolve()
        ensure_session_memory_file(sd)
        ensure_session_global_memory_file(sd)
        mp = sd / SESSION_MEMORY_FILENAME
        gp = sd / USER_GLOBAL_MEMORY_FILENAME
        max_inject = 3000 if shrink else 12000
        rules = (
            '【记忆规则 — Web 服务注入，请务必遵守】\n'
            f'1. 当前工作目录下有两个记忆文件：`{SESSION_MEMORY_FILENAME}` 用于**本对话私有记忆**，'
            f'`{USER_GLOBAL_MEMORY_FILENAME}` 用于**同一 IP + user_id 的用户全局记忆**，会在所有对话之间共享。\n'
            f'2. 用户明确要求“以后/所有对话/长期/记住我/我的称呼/我的偏好”等跨对话记忆时，'
            f'请用 Read 读取并用 Edit/Write 更新 `{USER_GLOBAL_MEMORY_FILENAME}`；只属于当前会话的上下文写入 `{SESSION_MEMORY_FILENAME}`。\n'
            '3. 不要使用 Claude/Codex 内置全局记忆，也不要写入 ~/.claude、HOME、其它用户或其它会话目录。\n'
            f'4. 需要回忆已记内容时，优先使用下方已注入的 `{USER_GLOBAL_MEMORY_FILENAME}` 和 `{SESSION_MEMORY_FILENAME}` 当前内容；'
            '若内容被截断，再单独 Read 对应文件。\n'
            '5. 本服务已对 CLI 使用非交互权限模式，若仍见权限错误，请重试 Edit/Write 当前工作目录下的记忆文件。\n\n'
        )
        body_parts = []

        def append_memory_file(path: Path, title: str) -> None:
            if not path.is_file():
                return
            try:
                raw = path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                raw = ''
            if len(raw) > max_inject:
                body_parts.append(
                    f'【{title} 内容（已截断，全文 {len(raw)} 字符，其余请用 Read 读 `{path.name}`）】\n'
                    f'{raw[:max_inject]}…\n'
                )
            else:
                body_parts.append(f'【{title} 当前内容】\n{raw}\n')

        append_memory_file(gp, USER_GLOBAL_MEMORY_FILENAME)
        append_memory_file(mp, SESSION_MEMORY_FILENAME)
        return rules + '\n'.join(body_parts) + ('\n' if body_parts else '')
    except Exception as e:
        log.warning(f'[记忆] 处理记忆文件失败: {e}')
        return ''


def _truncate_for_prompt(text: str, limit: int) -> str:
    s = (text or '').strip()
    if len(s) <= limit:
        return s
    return s[:limit] + f'\n...[已截断，原文约 {len(s)} 字符]'


def _conversation_history_prompt_block(messages: Optional[List[Dict[str, Any]]], max_chars: int = 14000) -> str:
    """
    从 Web 侧持久化的 messages.json 注入一份紧凑历史，让久未打开的会话也能先恢复上下文。
    不替代 Claude --resume，只作为服务端可控的会话级记忆输入。
    """
    if not messages:
        return ''
    items = []
    total = 0
    usable = [m for m in messages if isinstance(m, dict) and (m.get('content') or '').strip()]
    if not usable:
        return ''
    tail = usable[-16:]
    omitted = max(0, len(usable) - len(tail))
    for m in tail:
        role = '用户' if m.get('role') == 'user' else '助手'
        ts = (m.get('timestamp') or '').strip()
        files = m.get('files') or []
        file_hint = ''
        if files:
            names = []
            for f in files[:6]:
                names.append(str(f.get('display_name') or f.get('name') or f) if isinstance(f, dict) else str(f))
            file_hint = f'（附件: {", ".join(names)}）'
        content = _truncate_for_prompt(str(m.get('content') or ''), 1200)
        line = f'[{role}{(" " + ts) if ts else ""}]{file_hint}\n{content}'
        total += len(line)
        if total > max_chars:
            items.append('[历史摘要截断] 更早或更长的消息已省略，请结合当前问题回答。')
            break
        items.append(line)
    prefix = ''
    if omitted:
        prefix = f'（已省略更早的 {omitted} 条消息；下方为最近 {len(tail)} 条）\n'
    return (
        '【会话历史快照 — Web 服务从 messages.json 注入】\n'
        '请先基于下列历史在心里做一个快速摘要，恢复本对话上下文，再回答用户最新问题。'
        '除非用户要求，不要机械复述这段历史；只把相关信息用于回答。\n'
        f'{prefix}'
        + '\n\n'.join(items)
        + '\n\n'
    )


def _web_search_prompt_block(web_search_context: str) -> str:
    if not (web_search_context or '').strip():
        return ''
    return web_search_context.strip() + '\n\n'


def _development_prompt_block(development_context: Optional[Dict[str, Any]]) -> str:
    if not development_context:
        return ''
    project_name = development_context.get('project_name') or development_context.get('project_id') or ''
    project_path = development_context.get('project_path') or ''
    cache_dir = development_context.get('session_cache_dir') or ''
    git = development_context.get('git') or {}
    tests = development_context.get('default_tests') or []
    lines = [
        '【开发模式 — Web 服务注入】',
        '当前会话已绑定到 PC 本地白名单代码项目。手机端只是展示与控制台，真实读写发生在服务端 PC 的项目目录中。',
        f'- 项目：{project_name}',
        f'- 项目目录（真实可写）：{project_path}',
        f'- 会话 cache（记忆/附件/日志）：{cache_dir}',
        f'- Git 分支：{git.get("branch") or "unknown"}',
        f'- Git 提交：{git.get("commit") or "unknown"}',
        f'- 当前是否有未提交改动：{"是" if git.get("dirty") else "否"}',
        '',
        '开发模式规则：',
        '1. 你可以读取、搜索并修改上述项目目录内的文件；修改会直接落到 PC 本地真实项目。',
        '2. 不要访问或修改白名单项目目录之外的路径，除非它已经作为只读目录列出。',
        '3. 长期记忆写入会话 cache 下的 AGENT.md（用户全局）或 memory.md（本对话私有），不要在项目中创建会话记忆文件。',
        '4. 高风险命令不要静默执行：不要执行 git reset --hard、git clean -fd、删除项目根目录、push、修改全局配置或系统环境变量。',
        '5. 需要运行测试时，优先建议用户点击页面里的“运行测试”；不要自行发起安装依赖、迁移数据库、push 等高风险操作。',
    ]
    if tests:
        lines.append('项目预设测试命令（用户可在页面触发）：')
        for t in tests:
            lines.append(f'- {t}')
    lines.append('')
    return '\n'.join(lines) + '\n'


def _read_prompt_file_excerpt(path_str: str, max_chars: int) -> str:
    """读取 prompt 注入用的本地文本片段，失败时返回空串。"""
    if not path_str:
        return ''
    try:
        path = Path(path_str)
        if not path.is_file():
            return ''
        text = path.read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return ''
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f'\n...[已截断，完整文件约 {len(text)} 字符，可按需 Read 该文件]'


def _format_skill_brief(skill: Dict[str, Any]) -> str:
    sid = str(skill.get('id') or '?')
    title = str(skill.get('title') or sid).strip()
    summary = (skill.get('summary') or '').strip()
    path = str(skill.get('path') or '').strip()
    parts = [f'`{sid}`']
    if title and title != sid:
        parts.append(title)
    if summary:
        parts.append(summary[:240])
    if path:
        parts.append(f'path={path}')
    return ' - '.join(parts)


def _append_selected_skill_blocks(lines: List[str], skills: List[Dict[str, Any]]) -> None:
    if not skills:
        return
    lines.append('- **本轮优先 Skill**（先按这些工作流分析，再考虑通用搜索）：')
    for skill in skills[:3]:
        sid = str(skill.get('id') or '?')
        title = str(skill.get('title') or sid).strip()
        path = str(skill.get('path') or '').strip()
        reason = (skill.get('match_reason') or '').strip()
        summary = (skill.get('summary') or '').strip()
        lines.append(f'  - `{sid}`：{title}')
        if reason:
            lines.append(f'    - 命中原因：{reason}')
        if summary:
            lines.append(f'    - 摘要：{summary}')
        if path:
            lines.append(f'    - 文件：`{path}`')
            excerpt = _read_prompt_file_excerpt(path, 6000)
            if excerpt:
                lines.append('    - SKILL.md 摘要/工作流（已按长度限制注入）：')
                lines.append('```markdown')
                lines.append(excerpt)
                lines.append('```')
            else:
                lines.append('    - SKILL.md 内容未能预读；如需要请优先 Read 该文件，再读其它资料/代码。')


def _append_claude_md_blocks(lines: List[str], claude_md_paths: List[str]) -> None:
    if not claude_md_paths:
        return
    lines.append('- **本轮相关 CLAUDE.md**（`--add-dir` 下的 CLAUDE.md 默认不保证自动加载，本服务显式注入摘要）：')
    for path in claude_md_paths[:3]:
        lines.append(f'  - `{path}`')
        excerpt = _read_prompt_file_excerpt(path, 4000)
        if excerpt:
            lines.append('```markdown')
            lines.append(excerpt)
            lines.append('```')


def _skill_bundles_instruction(bundles: Optional[List[Dict[str, Any]]]) -> str:
    """
    技能包：默认仅注入摘要；对本轮已挂载的包，额外注入命中的 SKILL.md
    与根目录 CLAUDE.md 摘要，确保模型先按技能流程处理，再按需读代码/资料。
    """
    if not bundles:
        return ''
    lines = [
        '【按需技能包与 Skill 优先级 — Web 服务注入】',
        '以下为管理员在 `claude_web_paths.config.json` 的 `bundles` 中配置的**技能包**。',
        '处理顺序必须遵守：',
        '1. 先判断本轮是否有可用 `SKILL.md`；只要命中或高度相关，就必须优先按该 SKILL 的工作流、限制和输出格式处理。',
        '2. 若没有直接命中的 Skill，先看本轮已挂载包的 Skill 索引；判断需要时优先 Read 对应 `SKILL.md`，再继续分析。',
        '3. 若 Skill 不足，再使用已挂载技能包中的 CLAUDE.md、资源索引、只读资料和代码路径获取证据。',
        '4. 最后才使用你的通用能力做补充推理、提问和归纳；不要在未检查 Skill/技能包前直接发散搜索。',
        '5. 未挂载的包仅作为摘要索引；不要访问其路径。若判断需要额外包，可在回答中说明需要追加哪个 bundle/skill。',
        '说明：Claude CLI 对 `--add-dir` 目录下 `CLAUDE.md` 的自动加载不是默认可靠行为，本服务会对命中的包显式注入其根目录 `CLAUDE.md` 摘要。',
        '',
    ]
    for b in bundles:
        bid = str(b.get('id') or '?')
        title = str(b.get('title') or bid).strip()
        summary = (b.get('summary') or '').strip() or '（无摘要）'
        paths = b.get('paths') or []
        resources = b.get('resources') or []
        skills = b.get('skills') or []
        selected_skills = b.get('selected_skills') or []
        claude_md_paths = b.get('claude_md_paths') or []
        rule_packs = b.get('rule_packs') or []
        mounted = bool(b.get('mounted'))
        reason = (b.get('mount_reason') or '').strip()
        lines.append(f'### 包 `{bid}`：{title}')
        lines.append(f'- **状态**：{"本轮已挂载，可按需读取" if mounted else "仅摘要，本轮未挂载路径"}')
        if mounted and reason:
            lines.append(f'- **命中原因**：{reason}')
        lines.append(f'- **摘要**：{summary}')
        if rule_packs:
            lines.append(f'- **关联规则包**：{", ".join(str(x) for x in rule_packs[:8])}')
        if mounted:
            _append_selected_skill_blocks(lines, selected_skills)
            if skills:
                lines.append('- **Skill 索引**（若优先 Skill 不足，先从这里挑选并 Read 对应 SKILL.md）：')
                for skill in skills[:12]:
                    lines.append(f'  - {_format_skill_brief(skill)}')
                if len(skills) > 12:
                    lines.append(f'  - ... 其余 {len(skills) - 12} 个 skill 已省略，可在对应 skills 目录中按需查看')
            _append_claude_md_blocks(lines, claude_md_paths)
            if resources:
                lines.append('- **资源索引**（仅在 Skill 指示需要证据/源码时再读）：')
                for res in resources[:20]:
                    rid = str(res.get('id') or '?')
                    kind = str(res.get('kind') or 'generic')
                    desc = (res.get('summary') or '').strip()
                    path = str(res.get('path') or '').strip()
                    suffix = f'：{desc}' if desc else ''
                    lines.append(f'  - `{rid}` ({kind}) `{path}`{suffix}')
                if len(resources) > 20:
                    lines.append(f'  - ... 其余 {len(resources) - 20} 个 resource 已省略')
        if mounted and paths:
            lines.append('- **按需深入路径**（最后再 Read/Grep/Glob，避免无目标扫全仓）：')
            for p in paths:
                lines.append(f'  - `{p}`')
        elif mounted:
            lines.append('- **路径**：（本包未配置有效目录）')
        lines.append('')
    lines.append('')
    return '\n'.join(lines)


def _sandbox_instruction(
    session_workspace: str,
    readonly_dirs: list,
    extra_notes: str = '',
    cli_cwd_dir: str = '',
    writable_dirs: Optional[List[str]] = None,
) -> str:
    try:
        ws = Path(session_workspace).resolve().as_posix() if session_workspace else ''
    except OSError:
        ws = (session_workspace or '').strip()
    if not ws:
        ws = '（未指定）'
    cwd = (cli_cwd_dir or '').strip() or ws
    lines = [
        '【沙箱与目录约束】',
        f'当前 CLI 工作目录（以下路径为 POSIX/正斜杠形式）: {cwd}',
        f'本会话 cache 目录（用于 uploads、memory.md、AGENT.md、对话记录和运行状态）: {ws}',
        'Claude CLI 能力来自服务端父机配置（skills、API、model、环境变量等），但父机全局记忆必须隔离；用户全局记忆只通过当前目录下的 AGENT.md 使用。',
        f'记忆请只使用本目录下的 `{USER_GLOBAL_MEMORY_FILENAME}`（用户全局）和 `{SESSION_MEMORY_FILENAME}`（本对话私有，见下方「记忆规则」）。',
        '',
        '【工具与路径权限策略 — Web 服务注入】',
        f'1. **本会话目录**（上述工作目录，含 uploads、AGENT.md、memory.md 等）：视为你的「可写沙箱」。'
        '在此范围内可进行 Read/Write/Edit、Bash（含 python、解压、本目录内脚本等）；不要尝试把路径改写到沙箱之外。',
        '2. **只读目录**（见下列路径，来自环境变量 CLAUDE_WEB_READONLY_DIRS 与仓库根目录 `claude_web_paths.config.json`）：'
        '仅允许读取、搜索（Read/Grep/Glob 等），**禁止**写入、删除或修改其中文件。',
        '3. **未出现在「本会话目录」与「只读目录」中的任意路径**：禁止访问（不要 Read/不要 Bash 操作）。',
        '4. 服务端已为非交互模式配置 CLI 权限；请直接操作沙箱内文件，勿再等待「用户批准」类交互。',
        '5. **Read 的 file_path（必遵）**：只写**相对路径**，形如 `uploads/文件名.ext`，单段或多段均以 `uploads/` 开头、用 `/` 分隔。',
        '**禁止**使用绝对路径（含 `D:/`、`C:/`、`/home/` 等）；部分上游会对带盘符的 file_path 返回 invalid params（20024）。会话 cwd 即上述工作目录。',
        '6. 若用户消息中已内联某附件的全文或提取文本，**不要再对该附件调用 Read**；仅当未内联且确需读文件时再使用 Read＋相对路径。',
        '7. **记忆隔离**：不要读取、引用或写入父机/全局 Claude 记忆（例如用户 HOME 下的 CLAUDE.md、全局 memory、其它会话 cache）；跨对话用户记忆仅使用当前目录下的 AGENT.md。',
    ]
    if readonly_dirs:
        lines.append('下列为只读目录（仅可读）：')
        for d in readonly_dirs:
            try:
                d2 = Path(d).resolve().as_posix()
            except OSError:
                d2 = d
            lines.append(f'  - {d2}')
    else:
        lines.append(
            '当前未配置额外只读目录（环境变量与 claude_web_paths.config.json 均为空）：'
            '除本会话目录外不要访问其它会话或其它用户的 cache。'
        )
    if writable_dirs:
        lines.append('下列为本轮额外可写目录（仅在服务端明确启用的模式下出现）：')
        for d in writable_dirs:
            try:
                d2 = Path(d).resolve().as_posix()
            except OSError:
                d2 = d
            lines.append(f'  - {d2}')
    lines.append('不同浏览器会话（不同 session_id）彼此隔离；不要假设能访问其它对话的目录。')
    if (extra_notes or '').strip():
        lines.append('')
        lines.append('【管理员附加说明（来自 claude_web_paths.config.json 的 notes）】')
        lines.append(extra_notes.strip())
    return '\n'.join(lines) + '\n\n'


def _language_alignment_block(user_message: str) -> str:
    """引导模型在可见思考/工具说明中与用户语言一致。"""
    if not (user_message or '').strip():
        return ''
    t = user_message.strip()
    cjk = sum(1 for c in t if '\u4e00' <= c <='\u9fff' or '\u3000' <= c <= '\u303f')
    if cjk >= max(3, len(t) * 0.12):
        return (
            '【语言规范 — Web 服务注入】\n'
            '检测到用户使用中文。请在本轮中：**中间思考（含 extended thinking）说明、工具调用目的与步骤说明、'
            '面向用户的解释**均优先使用**中文**，与用户的语言保持一致。\n\n'
        )
    return (
        '【语言规范 — Web 服务注入】\n'
        '请将与用户可见的中间思考说明、工具调用说明、步骤摘要与最终回复使用的语言，'
        '与用户本轮提问语言保持一致（用户主要使用中文则用中文，其它语言同理）。\n\n'
    )


def _turn_attachment_instruction(file_paths: list) -> str:
    names = [Path(fp).name for fp in file_paths if fp]
    if not names:
        return ''
    joined = '、'.join(names)
    return (
        '【本轮附件说明】用户在本条消息中一并上传了以下文件。当用户说「这个/这份/该文件/这个 md」'
        '等指代时，均指本轮下列附件；请直接基于下方【附件内容】作答，不要再去工作区里搜索或猜测其它文件。\n'
        '下方【附件内容】即本轮随消息附带的全文；即使某文件正文为空，也视为已附带该文件，不要回答「消息里未附带文件」。\n'
        f'本轮附件：{joined}\n\n'
    )


def _format_one_attachment_block(p: Path, content: str) -> str:
    try:
        size = p.stat().st_size
    except OSError:
        size = -1
    if size >= 0:
        header = f'--- 文件: {p.name}（{size} 字节） ---'
    else:
        header = f'--- 文件: {p.name} ---'
    body = content
    if not body.strip():
        body = (
            '[该文件已作为本轮附件出现在本条消息中；当前可读正文为空（可能为 0 字节空文件）。'
            '请直接说明内容为空或无法从正文获取信息，不要声称用户未附带文件。]'
        )
    return f'{header}\n{body}\n--- 文件结束 ---'


def _read_file_content(file_path: str, max_size: int = 500000) -> str:
    try:
        p = Path(file_path)
        if not p.exists():
            return f'[文件不存在: {file_path}]'
        if p.stat().st_size > max_size:
            return f'[文件过大，已跳过: {p.name} ({p.stat().st_size // 1024}KB)]'
        with open(p, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except Exception as e:
        return f'[读取文件失败: {e}]'


_ATTACHMENT_INLINE_SKIP_SUFFIX = frozenset({
    '.pdf', '.zip', '.gz', '.tar', '.tgz', '.rar', '.7z',
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.bmp',
    '.mp3', '.mp4', '.m4a', '.webm', '.mov', '.avi',
    '.exe', '.dll', '.so', '.dylib',
    '.woff', '.woff2', '.ttf', '.eot',
})


def _bytes_look_binary(data: bytes) -> bool:
    if not data:
        return False
    if b'\x00' in data[:8192]:
        return True
    return False


def _pdf_extracted_sidecar(p: Path) -> Path:
    return p.parent / (p.name + '.extracted.txt')


def _try_extract_pdf_text(p: Path, max_chars: int) -> Optional[str]:
    """提取 PDF 文本供内联；失败或无可读文本时返回 None。会写入 sidecar 缓存以加速后续轮次。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        log.warning('[附件] 未安装 pypdf，无法提取 PDF 文本：请执行 pip install pypdf')
        return None
    try:
        side = _pdf_extracted_sidecar(p)
        try:
            pm = p.stat().st_mtime
            if side.is_file() and side.stat().st_mtime >= pm:
                raw = side.read_text(encoding='utf-8', errors='replace')
                if raw.strip():
                    log.info(f'[附件] 使用 PDF 文本缓存: {side.name}')
                    return raw[:max_chars] if len(raw) > max_chars else raw
        except OSError:
            pass
        reader = PdfReader(str(p))
        parts = []
        for page in reader.pages:
            try:
                t = page.extract_text() or ''
            except Exception:
                t = ''
            if t:
                parts.append(t)
        text = '\n\n'.join(parts).strip()
        if not text:
            return None
        try:
            side.write_text(text, encoding='utf-8')
        except OSError as e:
            log.warning(f'[附件] 写入 PDF 文本缓存失败（可忽略）: {e}')
        log.info(f'[附件] 已从 PDF 提取文本: {p.name}，约 {len(text)} 字符')
        return text[:max_chars] if len(text) > max_chars else text
    except Exception as e:
        log.warning(f'[附件] PDF 文本提取失败 {p.name}: {e}')
        return None


def _read_only_relative_read_hint(p: Path, kind: str) -> str:
    rel = f'uploads/{p.name}'
    return (
        f'[本附件为{kind}，不在消息中内联全文。请使用 Read 工具，**file_path 仅填** `{rel}`（相对路径、正斜杠）；'
        f'**禁止**使用含盘符的绝对路径（如 D:/…），否则上游可能返回 invalid params。]')


def _read_attachment_for_prompt(file_path: str, max_text: int = 500000) -> str:
    try:
        p = Path(file_path)
        if not p.exists():
            return f'[文件不存在: {file_path}]'
        if p.suffix.lower() == '.pdf':
            extracted = _try_extract_pdf_text(p, max_text)
            if extracted:
                return (
                    '[本附件为 PDF；服务端已提取文本如下。**请勿再调用 Read 读取该 PDF**：'
                    '部分上游对 Read+PDF 或绝对路径会返回 invalid params（code 20024）。请仅根据下列文本作答。]\n\n'
                    + extracted
                )
            return _read_only_relative_read_hint(p, 'PDF（无法提取文本，可能为扫描件）')
        if p.suffix.lower() in _ATTACHMENT_INLINE_SKIP_SUFFIX:
            return _read_only_relative_read_hint(p, f'二进制/非纯文本（{p.suffix}）')
        try:
            sz = p.stat().st_size
        except OSError:
            sz = 0
        if sz > max_text:
            with open(p, 'rb') as bf:
                head = bf.read(8192)
            if _bytes_look_binary(head):
                return _read_only_relative_read_hint(p, '大文件或疑似二进制')
        with open(p, 'rb') as bf:
            head = bf.read(8192)
        if _bytes_look_binary(head):
            return _read_only_relative_read_hint(p, '二进制（含零字节等）')
        return _read_file_content(file_path, max_size=max_text)
    except Exception as e:
        return f'[读取文件失败: {e}]'


def _isolate_home_enabled() -> bool:
    return bool(config.CLAUDE_WEB_ISOLATE_HOME)


def _fork_claude_home_enabled() -> bool:
    return bool(getattr(config, 'CLAUDE_WEB_FORK_CLAUDE_HOME', True))


_PARENT_CLAUDE_MEMORY_NAMES = frozenset({
    'claude.md',
    'memory.md',
    'memories',
    'projects',
    'todos',
    'shell-snapshots',
    'logs',
    'statsig',
})


def _is_parent_memory_entry(path: Path) -> bool:
    name = path.name.strip().lower()
    if name in _PARENT_CLAUDE_MEMORY_NAMES:
        return True
    return name.startswith('memory') or name.endswith('.memory')


def _mirror_parent_claude_config(parent_claude: Path, session_claude: Path) -> None:
    """
    继承父机 Claude 能力，但刻意不继承全局记忆。
    优先符号链接目录，失败时复制；普通文件按 mtime 刷新复制。
    """
    if not parent_claude.is_dir():
        return
    session_claude.mkdir(parents=True, exist_ok=True)
    for src in parent_claude.iterdir():
        if _is_parent_memory_entry(src):
            continue
        dst = session_claude / src.name
        try:
            if src.is_dir():
                if dst.exists():
                    continue
                try:
                    dst.symlink_to(src, target_is_directory=True)
                except OSError:
                    shutil.copytree(
                        src, dst, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('CLAUDE.md', 'memory*', 'projects', 'todos', 'logs'),
                    )
            elif src.is_file():
                if dst.exists():
                    try:
                        if dst.stat().st_mtime >= src.stat().st_mtime and dst.stat().st_size == src.stat().st_size:
                            continue
                    except OSError:
                        pass
                shutil.copy2(src, dst)
        except OSError as e:
            log.warning(f'[CLI] 继承父 Claude 配置项失败（跳过） {src}: {e}')


def _copy_parent_home_claude_files(parent_home: Path, session_home: Path) -> None:
    for name in ('.claude.json', '.claude.json.backup'):
        src = parent_home / name
        dst = session_home / name
        try:
            if not src.is_file():
                continue
            if dst.exists():
                try:
                    if dst.stat().st_mtime >= src.stat().st_mtime and dst.stat().st_size == src.stat().st_size:
                        continue
                except OSError:
                    pass
            shutil.copy2(src, dst)
        except OSError as e:
            log.warning(f'[CLI] 继承父 Claude 根配置失败（跳过） {src}: {e}')


def _build_claude_child_env_fork_parent_config(session_workspace_dir: str) -> dict:
    env = os.environ.copy()
    parent_home = Path.home()
    parent_claude = parent_home / '.claude'
    sw = Path(session_workspace_dir).resolve()
    session_home = sw / '.claude_web_home'
    session_claude = session_home / '.claude'
    session_home.mkdir(parents=True, exist_ok=True)
    _mirror_parent_claude_config(parent_claude, session_claude)
    _copy_parent_home_claude_files(parent_home, session_home)
    guard = session_claude / 'CLAUDE.md'
    if not guard.exists():
        guard.write_text(
            '# Session-local Claude memory guard\n\n'
            'This HOME is created by Claude Web Server for one chat session. '
            'Do not store or read long-term memory here; use the working directory memory.md only.\n',
            encoding='utf-8',
        )

    env['HOME'] = str(session_home)
    env['USERPROFILE'] = str(session_home)
    if sys.platform == 'win32':
        local_app = session_home / 'AppData' / 'Local'
        roaming = session_home / 'AppData' / 'Roaming'
        local_app.mkdir(parents=True, exist_ok=True)
        roaming.mkdir(parents=True, exist_ok=True)
        env['LOCALAPPDATA'] = str(local_app)
        env['APPDATA'] = str(roaming)
    else:
        xdg_config = session_home / '.config'
        xdg_cache = session_home / '.cache'
        xdg_config.mkdir(parents=True, exist_ok=True)
        xdg_cache.mkdir(parents=True, exist_ok=True)
        env['XDG_CONFIG_HOME'] = str(xdg_config)
        env['XDG_CACHE_HOME'] = str(xdg_cache)
    return env


def _build_claude_child_env_isolate_home(session_workspace_dir: str) -> dict:
    env = os.environ.copy()
    sw = str(Path(session_workspace_dir).resolve())
    Path(sw).mkdir(parents=True, exist_ok=True)
    (Path(sw) / '.claude').mkdir(parents=True, exist_ok=True)
    env['HOME'] = sw
    env['USERPROFILE'] = sw
    if sys.platform == 'win32':
        local_app = Path(sw) / 'AppData' / 'Local'
        roaming = Path(sw) / 'AppData' / 'Roaming'
        local_app.mkdir(parents=True, exist_ok=True)
        roaming.mkdir(parents=True, exist_ok=True)
        env['LOCALAPPDATA'] = str(local_app)
        env['APPDATA'] = str(roaming)
    else:
        # Linux/macOS：部分 CLI 会读 XDG_*，与 HOME 下的会话目录对齐
        xdg_config = Path(sw) / '.config'
        xdg_cache = Path(sw) / '.cache'
        xdg_config.mkdir(parents=True, exist_ok=True)
        xdg_cache.mkdir(parents=True, exist_ok=True)
        env['XDG_CONFIG_HOME'] = str(xdg_config)
        env['XDG_CACHE_HOME'] = str(xdg_cache)
    return env


def stream_claude_output(
    message,
    session_id=None,
    claude_session_id=None,
    upload_dir=None,
    file_paths=None,
    session_workspace_dir=None,
    readonly_dirs=None,
    readonly_dirs_notes: str = '',
    skill_bundles: Optional[List[Dict[str, Any]]] = None,
    cli_log_context: Optional[Dict[str, Any]] = None,
    child_env_extra: Optional[Dict[str, str]] = None,
    model_override: Optional[str] = None,
    conversation_history: Optional[List[Dict[str, Any]]] = None,
    web_search_context: str = '',
    cli_cwd_dir: Optional[str] = None,
    permission_mode_override: Optional[str] = None,
    dangerously_skip_permissions_override: Optional[bool] = None,
    development_context: Optional[Dict[str, Any]] = None,
):
    """
    调用 Claude CLI 并流式转发输出。
    cli_log_context: {'user_id', 'session_id', 'log_dir': Path} 时追加 stderr 与会话退出摘要。
    """
    readonly_dirs = readonly_dirs if readonly_dirs is not None else []
    exe = (config.CLAUDE_CLI_PATH or 'claude').strip() or 'claude'
    cmd = [
        exe,
        '--output-format', 'stream-json',
        '--include-partial-messages',
        '--verbose', '--print',
    ]
    model_eff = (model_override or '').strip() if model_override else (config.CLAUDE_MODEL or '').strip()
    if model_eff:
        cmd.extend(['--model', model_eff])
    if config.CLAUDE_EXTRA_CLI_ARGS:
        cmd.extend(list(config.CLAUDE_EXTRA_CLI_ARGS))
    permission_mode = (
        (permission_mode_override or '').strip()
        if permission_mode_override is not None
        else (config.CLAUDE_WEB_PERMISSION_MODE or '').strip()
    )
    if permission_mode:
        cmd.extend(['--permission-mode', permission_mode])
    dangerous_skip = (
        bool(dangerously_skip_permissions_override)
        if dangerously_skip_permissions_override is not None
        else bool(config.CLAUDE_WEB_DANGEROUSLY_SKIP_PERMISSIONS)
    )
    if dangerous_skip:
        cmd.append('--allow-dangerously-skip-permissions')
        cmd.append('--dangerously-skip-permissions')

    if claude_session_id:
        cmd.extend(['--resume', claude_session_id])
    else:
        cmd.extend(['--session-id', session_id])

    add_dirs = []
    seen = set()
    for d in readonly_dirs:
        if not d:
            continue
        try:
            rp = Path(d).resolve()
            if rp.is_dir():
                s = str(rp)
                if s not in seen:
                    seen.add(s)
                    add_dirs.append(s)
        except OSError:
            pass
    if session_workspace_dir:
        try:
            sw = Path(session_workspace_dir).resolve()
            if sw.is_dir():
                s = str(sw)
                if s not in seen:
                    seen.add(s)
                    add_dirs.append(s)
        except OSError:
            pass
    if cli_cwd_dir:
        try:
            wd = Path(cli_cwd_dir).resolve()
            if wd.is_dir():
                s = str(wd)
                if s not in seen:
                    seen.add(s)
                    add_dirs.append(s)
        except OSError:
            pass
    if add_dirs:
        cmd.append('--add-dir')
        cmd.extend(add_dirs)

    full_message = message
    if file_paths:
        preamble = _turn_attachment_instruction(file_paths)
        file_parts = []
        for fp in file_paths:
            p = Path(fp)
            if p.exists():
                content = _read_attachment_for_prompt(str(fp))
                file_parts.append(_format_one_attachment_block(p, content))
            else:
                file_parts.append(f'[附件文件不存在: {fp}]')
        if file_parts:
            user_text = message.strip()
            if not user_text:
                user_text = '（用户未输入文字，仅上传了附件，请根据附件内容理解意图并回答。）'
            full_message = (
                f'{preamble}【用户问题】\n{user_text}\n\n'
                f'---\n【附件内容】\n' + '\n\n'.join(file_parts)
            )

    sw_str = ''
    if session_workspace_dir:
        try:
            sw_str = Path(session_workspace_dir).resolve().as_posix()
        except OSError:
            sw_str = str(session_workspace_dir)
    cwd_str = ''
    if cli_cwd_dir:
        try:
            cwd_str = Path(cli_cwd_dir).resolve().as_posix()
        except OSError:
            cwd_str = str(cli_cwd_dir)
    writable_dirs = []
    if cli_cwd_dir:
        writable_dirs.append(cli_cwd_dir)
    full_message = (
        _skill_bundles_instruction(skill_bundles)
        + _sandbox_instruction(
            sw_str,
            readonly_dirs,
            extra_notes=readonly_dirs_notes or '',
            cli_cwd_dir=cwd_str,
            writable_dirs=writable_dirs,
        )
        + _development_prompt_block(development_context)
        + _memory_prompt_block(sw_str, shrink=bool(file_paths))
        + _conversation_history_prompt_block(conversation_history)
        + _web_search_prompt_block(web_search_context)
        + _language_alignment_block(message)
        + full_message
    )

    cmd.append('--')

    mounted_bundles = [b for b in (skill_bundles or []) if b.get('mounted')]
    selected_skill_count = sum(len(b.get('selected_skills') or []) for b in mounted_bundles)
    injected_claude_md_count = sum(len(b.get('claude_md_paths') or []) for b in mounted_bundles)
    log.info('[CLI] 执行命令: claude ... --print ... -- （prompt 经 stdin 传入）')
    log.info(f'[CLI] session_id={session_id}, claude_session_id={claude_session_id}')
    log.info(
        '[CLI] prompt_chars_total=%s, user_message_chars=%s, mounted_bundles=%s, selected_skills=%s, injected_claude_md=%s',
        len(full_message),
        len(message or ''),
        [b.get('id') for b in mounted_bundles],
        selected_skill_count,
        injected_claude_md_count,
    )
    log.info(
        f'[CLI] session_workspace={session_workspace_dir}, cli_cwd={cli_cwd_dir}, add_dirs={add_dirs}, '
        f'upload_dir={upload_dir}, 文件数={len(file_paths) if file_paths else 0}, 消息长度={len(full_message)}'
    )

    cwd_kw = {}
    cwd_candidate = cli_cwd_dir or session_workspace_dir
    if cwd_candidate:
        try:
            cwp = Path(cwd_candidate).resolve()
            if cwp.is_dir():
                cwd_kw['cwd'] = str(cwp)
        except OSError:
            pass

    popen_kw = dict(cwd_kw)
    if session_workspace_dir and _isolate_home_enabled():
        try:
            popen_kw['env'] = _build_claude_child_env_isolate_home(session_workspace_dir)
            log.info('[CLI] 已启用 CLAUDE_WEB_ISOLATE_HOME：记忆将写入会话目录，但全局 Claude 配置不再继承')
        except Exception as e:
            log.warning(f'[CLI] 构建隔离 HOME 环境失败，使用默认环境: {e}')
    elif session_workspace_dir and _fork_claude_home_enabled():
        try:
            popen_kw['env'] = _build_claude_child_env_fork_parent_config(session_workspace_dir)
            log.info('[CLI] 已启用 fork Claude HOME：共享父机 Claude 能力，隔离父机全局记忆')
        except Exception as e:
            log.warning(f'[CLI] 构建 fork Claude HOME 环境失败，使用默认环境: {e}')
    if 'env' not in popen_kw:
        popen_kw['env'] = os.environ.copy()
    popen_kw['env'].setdefault('PYTHONIOENCODING', 'utf-8')
    if child_env_extra:
        for k, v in child_env_extra.items():
            if k:
                popen_kw['env'][str(k)] = str(v)

    # POSIX 上 argv 列表应使用 shell=False；Windows 下 list + shell=True 易与 shell 解析不一致，同样用 False。
    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            shell=False,
            **popen_kw,
        )
        if session_id:
            with _RUNNING_PROCESSES_LOCK:
                _RUNNING_PROCESSES[str(session_id)] = process
    except FileNotFoundError:
        yield f'data: {json.dumps({"type": "error", "message": f"Claude CLI 未找到: {exe}"})}\n\n'
        return
    except Exception as e:
        yield f'data: {json.dumps({"type": "error", "message": f"启动 Claude CLI 失败: {str(e)}"})}\n\n'
        return

    def stdin_writer():
        try:
            process.stdin.write(full_message)
            process.stdin.close()
        except BrokenPipeError:
            pass
        except Exception as e:
            log.warning(f'[CLI stdin] 写入失败: {e}')
            try:
                process.stdin.close()
            except Exception:
                pass

    stdin_thread = threading.Thread(target=stdin_writer, daemon=True)
    stdin_thread.start()

    line_queue = queue.Queue()
    stderr_lines = []
    stdout_recent = deque(maxlen=60)
    last_result_payload = None
    successful_result_seen = False

    log_dir = None
    uid = None
    sid = None
    if cli_log_context:
        log_dir = cli_log_context.get('log_dir')
        uid = cli_log_context.get('user_id')
        sid = cli_log_context.get('session_id')

    def stdout_reader():
        try:
            for line in process.stdout:
                line_queue.put(('stdout', line))
        finally:
            line_queue.put(('stdout', None))
            process.stdout.close()

    def stderr_reader():
        try:
            for line in process.stderr:
                stripped = line.strip()
                stderr_lines.append(stripped)
                log.warning(f'[CLI stderr] {stripped[:500]}')
                if log_dir and uid and sid:
                    append_cli_line(log_dir, uid, sid, f'[stderr] {stripped}')
        finally:
            process.stderr.close()

    stdout_thread = threading.Thread(target=stdout_reader, daemon=True)
    stderr_thread = threading.Thread(target=stderr_reader, daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    claude_sid_returned = None
    stream_open_block = None  # None | 'thinking' | 'text' | 'tool'
    api_error_yet = False
    # stream_event 已用 delta 推过思考/正文时，勿再转发 assistant 汇总里的同一块（否则前端会重复展示）
    streamed_thinking_delta = False
    streamed_text_delta = False
    streamed_tool_block = False
    visible_output_seen = False
    client_disconnected = False

    def _tool_start_payload(cb: dict) -> dict:
        name = cb.get('name') or cb.get('tool_name') or ''
        tid = cb.get('id') or ''
        return {'type': 'tool_start', 'name': name, 'id': tid}

    try:
        while True:
            source, line = line_queue.get(timeout=300)
            if line is None:
                break

            line = line.strip()
            if not line:
                continue

            if len(line) < 8000:
                stdout_recent.append(line)
            else:
                stdout_recent.append(line[:4000] + '…[truncated]')

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                log.debug(f'[CLI] 非JSON输出: {line[:200]}')
                continue

            msg_type = data.get('type')

            if msg_type == 'system':
                sid_val = data.get('session_id')
                if sid_val:
                    claude_sid_returned = sid_val
                    log.info(f'[CLI] Claude session_id: {sid_val}')
                    yield f'data: {json.dumps({"type": "session", "session_id": sid_val})}\n\n'

            elif msg_type == 'stream_event':
                event = data.get('event', {})
                event_type = event.get('type')

                if event_type == 'content_block_start':
                    cb = event.get('content_block', {})
                    cb_type = (cb.get('type') or '').strip()
                    if cb_type == 'thinking' or cb_type == 'redacted_thinking':
                        stream_open_block = 'thinking'
                        visible_output_seen = True
                        yield f'data: {json.dumps({"type": "thinking_start"})}\n\n'
                    elif cb_type == 'text':
                        stream_open_block = 'text'
                        visible_output_seen = True
                        yield f'data: {json.dumps({"type": "text_start"})}\n\n'
                    elif cb_type in ('tool_use', 'tool_use_block', 'server_tool_use', 'tool_calls'):
                        stream_open_block = 'tool'
                        streamed_tool_block = True
                        visible_output_seen = True
                        yield f'data: {json.dumps(_tool_start_payload(cb))}\n\n'
                    elif cb_type:
                        log.debug(f'[CLI] content_block_start 未识别类型: {cb_type}')

                elif event_type == 'content_block_delta':
                    delta = event.get('delta', {})
                    delta_type = (delta.get('type') or '').strip()
                    if delta_type == 'thinking_delta':
                        thinking_chunk = delta.get('thinking', '')
                        if thinking_chunk:
                            streamed_thinking_delta = True
                            visible_output_seen = True
                            yield f'data: {json.dumps({"type": "thinking", "content": thinking_chunk})}\n\n'
                    elif delta_type == 'text_delta':
                        text_chunk = delta.get('text', '')
                        if text_chunk:
                            streamed_text_delta = True
                            visible_output_seen = True
                            yield f'data: {json.dumps({"type": "text", "content": text_chunk})}\n\n'
                    elif delta_type in ('input_json_delta', 'input_json'):
                        partial = (
                            delta.get('partial_json')
                            or delta.get('partial')
                            or delta.get('input_json_delta')
                            or ''
                        )
                        if isinstance(partial, dict):
                            partial = json.dumps(partial, ensure_ascii=False)
                        if partial:
                            visible_output_seen = True
                            yield f'data: {json.dumps({"type": "tool_input_delta", "partial": partial})}\n\n'

                elif event_type == 'content_block_stop':
                    kind = stream_open_block
                    stream_open_block = None
                    if kind == 'tool':
                        yield f'data: {json.dumps({"type": "tool_stop"})}\n\n'
                    elif kind == 'thinking':
                        yield f'data: {json.dumps({"type": "thinking_stop"})}\n\n'
                    yield f'data: {json.dumps({"type": "content_block_stop"})}\n\n'

                elif event_type == 'message_start':
                    streamed_thinking_delta = False
                    streamed_text_delta = False
                    streamed_tool_block = False
                    yield f'data: {json.dumps({"type": "message_start"})}\n\n'

                elif event_type == 'message_stop':
                    yield f'data: {json.dumps({"type": "message_stop"})}\n\n'

            elif msg_type == 'assistant':
                msg = data.get('message', {})
                for content in msg.get('content', []):
                    ctype = content.get('type')
                    if ctype in ('thinking', 'redacted_thinking'):
                        if streamed_thinking_delta:
                            continue
                        visible_output_seen = True
                        yield f'data: {json.dumps({"type": "thinking_start"})}\n\n'
                        thinking_text = content.get('thinking', '')
                        if thinking_text:
                            yield f'data: {json.dumps({"type": "thinking", "content": thinking_text})}\n\n'
                        yield f'data: {json.dumps({"type": "thinking_stop"})}\n\n'
                    elif ctype == 'text':
                        if streamed_text_delta:
                            continue
                        visible_output_seen = True
                        yield f'data: {json.dumps({"type": "text", "content": content["text"]})}\n\n'
                    elif ctype in ('tool_use', 'tool_use_block', 'server_tool_use'):
                        if streamed_tool_block:
                            continue
                        visible_output_seen = True
                        yield f'data: {json.dumps(_tool_start_payload(content))}\n\n'
                        inp = content.get('input')
                        if isinstance(inp, dict):
                            yield f'data: {json.dumps({"type": "tool_input_delta", "partial": json.dumps(inp, ensure_ascii=False)})}\n\n'
                        elif isinstance(inp, str) and inp.strip():
                            yield f'data: {json.dumps({"type": "tool_input_delta", "partial": inp})}\n\n'
                        yield f'data: {json.dumps({"type": "tool_stop"})}\n\n'

            elif msg_type == 'result':
                last_result_payload = data
                successful_result_seen = not bool(data.get('is_error'))
                sid_val = data.get('session_id')
                if sid_val and not claude_sid_returned:
                    claude_sid_returned = sid_val
                    log.info(f'[CLI] Claude session_id (from result): {sid_val}')
                    yield f'data: {json.dumps({"type": "session", "session_id": sid_val})}\n\n'
                if data.get('is_error'):
                    api_error_yet = True
                    friendly = _friendly_api_error_from_result(data)
                    recoverable_rebuild = _is_recoverable_resume_failure(data, claude_session_id, visible_output_seen)
                    error_payload = {"type": "error", "message": friendly, "soft": True}
                    if recoverable_rebuild:
                        error_payload["recoverable_session_rebuild"] = True
                    yield f'data: {json.dumps(error_payload)}\n\n'
                done_payload = {"type": "done", "ok": not bool(data.get("is_error")), "result": data.get("result", "")}
                if data.get('is_error') and _is_recoverable_resume_failure(data, claude_session_id, visible_output_seen):
                    done_payload["recoverable_session_rebuild"] = True
                yield f'data: {json.dumps(done_payload)}\n\n'
                log.info(f'[CLI] 对话完成, returncode={process.poll()}')

                break

    except queue.Empty:
        if successful_result_seen:
            log.warning('[CLI] already received successful result; suppressing timeout error')
            return
        yield f'data: {json.dumps({"type": "error", "message": "Claude CLI 响应超时"})}\n\n'
    except GeneratorExit:
        client_disconnected = True
        _terminate_process(process, reason='client_disconnected')
        raise
    except Exception as e:
        log.error(f'[CLI] 处理输出时出错: {e}')
        yield f'data: {json.dumps({"type": "error", "message": f"处理输出时出错: {str(e)}"})}\n\n'
    finally:
        if client_disconnected:
            _terminate_process(process, reason='generator_closed')
        try:
            stdin_thread.join(timeout=60)
        except Exception:
            pass
        try:
            stderr_thread.join(timeout=5)
        except Exception:
            pass
        try:
            stdout_thread.join(timeout=5)
        except Exception:
            pass
        if process.poll() is None:
            if successful_result_seen:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _terminate_process(process, reason='stream_finally_after_success_result')
            else:
                _terminate_process(process, reason='stream_finally')
        else:
            process.wait()
        if session_id:
            with _RUNNING_PROCESSES_LOCK:
                if _RUNNING_PROCESSES.get(str(session_id)) is process:
                    _RUNNING_PROCESSES.pop(str(session_id), None)
        if not client_disconnected and process.returncode and process.returncode != 0:
            if successful_result_seen:
                log.warning(
                    '[CLI] process exited with code=%s after success result; suppressing duplicate CLI error',
                    process.returncode,
                )
            elif api_error_yet:
                log.warning(
                    '[CLI] 子进程 exit=%s，已在 result 流中上报 API/软错误，不再推送重复的 CLI stderr/stdout 错误',
                    process.returncode,
                )
            else:
                stderr_summary = '\n'.join(stderr_lines[-20:]) if stderr_lines else ''
                stdout_tail = '\n'.join(list(stdout_recent)[-20:])
                extra = ''
                if last_result_payload is not None:
                    try:
                        extra = '\n[last_result] ' + json.dumps(last_result_payload, ensure_ascii=False)[:2500]
                        log.error('[CLI] last_result: %s', json.dumps(last_result_payload, ensure_ascii=False)[:4000])
                    except Exception:
                        extra = '\n[last_result] <无法序列化>'
                if not stderr_summary and stdout_tail:
                    log.error(f'[CLI] stderr 为空，近期 stdout 行:\n{stdout_tail[:3000]}')
                if log_dir and uid and sid:
                    append_cli_exit_summary(
                        log_dir, uid, sid, process.returncode,
                        (stderr_summary + '\n' + stdout_tail + extra)[:8000],
                    )
                err_msg = f'Claude CLI 异常退出 (code: {process.returncode})'
                if stderr_summary:
                    err_msg += f'\n{stderr_summary[:800]}'
                elif stdout_tail:
                    err_msg += f'\n{stdout_tail[:1200]}'
                if extra and len(err_msg) < 2000:
                    err_msg += extra[:1500]
                yield f'data: {json.dumps({"type": "error", "message": err_msg})}\n\n'


# 供路由使用
resolve_session_upload_paths = _resolve_session_upload_paths
