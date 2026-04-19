"""Claude CLI 子进程与流式输出。"""

import json
import logging
import os
import queue
from collections import deque
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional


from . import config
from .filename_sanitize import safe_client_filename
from .session_manager import SESSION_MEMORY_FILENAME, ensure_session_memory_file
from .user_session_log import append_cli_exit_summary, append_cli_line

log = logging.getLogger('claude-web')


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
        mp = sd / SESSION_MEMORY_FILENAME
        max_inject = 4000 if shrink else 20000
        rules = (
            '【记忆规则 — Web 服务注入，请务必遵守】\n'
            '1. 本对话的「长期记忆」**仅**使用当前工作目录下的 `memory.md`（与 CLI 工作目录一致，已存在或可创建）。\n'
            '2. 用户同意记录名字、偏好等需跨轮记住的信息时：请用 **Read** 读取、**Edit**/**Write** 更新 `memory.md`；'
            '**不要**使用 Claude 内置「记忆」或向 ~/.claude 等全局路径写入（会无权限或不应写入）。\n'
            '3. 需要回忆已记内容时：优先查看下方「memory.md 当前内容」；若过长再单独 Read `memory.md`。\n'
            '4. 本服务已对 Claude CLI 使用 `--permission-mode`（默认 bypassPermissions，可选彻底跳过权限询问），'
            '以便在非交互模式下操作本会话目录；若仍见权限错误，请重试 Edit/Write `memory.md`。\n\n'
        )
        body = ''
        if mp.is_file():
            try:
                raw = mp.read_text(encoding='utf-8', errors='replace')
            except OSError:
                raw = ''
            if len(raw) > max_inject:
                body = (
                    f'【memory.md 内容（已截断，全文 {len(raw)} 字符，其余请用 Read 读 memory.md）】\n'
                    f'{raw[:max_inject]}…\n\n'
                )
            else:
                body = f'【memory.md 当前内容】\n{raw}\n\n'
        return rules + body
    except Exception as e:
        log.warning(f'[记忆] 处理 memory.md 失败: {e}')
        return ''


def _skill_bundles_instruction(bundles: Optional[List[Dict[str, Any]]]) -> str:
    """
    技能包：仅注入各包 id/title/summary 与路径索引，引导按需 Read/Grep，不内联文件内容。
    """
    if not bundles:
        return ''
    lines = [
        '【技能包索引 — Web 服务注入】',
        '以下为管理员在 `claude_web_paths.config.json` 的 `bundles` 中配置的**技能包**。',
        '**默认只需理解各包的摘要与适用场景**；**不要**在未确认需要时通读整个目录树。',
        '当用户问题与某包相关时，再对该包下列路径使用 Read/Grep/Glob **按需**加载具体文件（如 SKILL.md、源码）。',
        '下列路径已加入 `--add-dir`，工具可读；若上游对绝对路径敏感，可优先用相对路径或按包内目录结构描述。',
        '',
    ]
    for b in bundles:
        bid = str(b.get('id') or '?')
        title = str(b.get('title') or bid).strip()
        summary = (b.get('summary') or '').strip() or '（无摘要）'
        paths = b.get('paths') or []
        lines.append(f'### 包 `{bid}`：{title}')
        lines.append(f'- **摘要**：{summary}')
        if paths:
            lines.append('- **按需深入路径**（有明确需求时再 Read/Grep）：')
            for p in paths:
                lines.append(f'  - `{p}`')
        else:
            lines.append('- **路径**：（本包未配置有效目录）')
        lines.append('')
    lines.append('')
    return '\n'.join(lines)


def _sandbox_instruction(session_workspace: str, readonly_dirs: list, extra_notes: str = '') -> str:
    try:
        ws = Path(session_workspace).resolve().as_posix() if session_workspace else ''
    except OSError:
        ws = (session_workspace or '').strip()
    if not ws:
        ws = '（未指定）'
    lines = [
        '【沙箱与目录约束】',
        f'当前 CLI 工作目录（优先在此会话目录内创建/修改文件，以下路径为 POSIX/正斜杠形式）: {ws}',
        '全局 Claude Code 配置、API 凭证、skills 仍使用系统用户目录下的 ~/.claude（本服务默认不修改 HOME）。',
        f'长期记忆请只使用本目录下的 `{SESSION_MEMORY_FILENAME}`（见下方「记忆规则」）。',
        '',
        '【工具与路径权限策略 — Web 服务注入】',
        f'1. **本会话目录**（上述工作目录，含 uploads、memory.md 等）：视为你的「可写沙箱」。'
        '在此范围内可进行 Read/Write/Edit、Bash（含 python、解压、本目录内脚本等）；不要尝试把路径改写到沙箱之外。',
        '2. **只读目录**（见下列路径，来自环境变量 CLAUDE_WEB_READONLY_DIRS 与仓库根目录 `claude_web_paths.config.json`）：'
        '仅允许读取、搜索（Read/Grep/Glob 等），**禁止**写入、删除或修改其中文件。',
        '3. **未出现在「本会话目录」与「只读目录」中的任意路径**：禁止访问（不要 Read/不要 Bash 操作）。',
        '4. 服务端已为非交互模式配置 CLI 权限；请直接操作沙箱内文件，勿再等待「用户批准」类交互。',
        '5. **Read 的 file_path（必遵）**：只写**相对路径**，形如 `uploads/文件名.ext`，单段或多段均以 `uploads/` 开头、用 `/` 分隔。',
        '**禁止**使用绝对路径（含 `D:/`、`C:/`、`/home/` 等）；部分上游会对带盘符的 file_path 返回 invalid params（20024）。会话 cwd 即上述工作目录。',
        '6. 若用户消息中已内联某附件的全文或提取文本，**不要再对该附件调用 Read**；仅当未内联且确需读文件时再使用 Read＋相对路径。',
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
    if config.CLAUDE_WEB_PERMISSION_MODE:
        cmd.extend(['--permission-mode', config.CLAUDE_WEB_PERMISSION_MODE])
    if config.CLAUDE_WEB_DANGEROUSLY_SKIP_PERMISSIONS:
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
    full_message = (
        _skill_bundles_instruction(skill_bundles)
        + _sandbox_instruction(sw_str, readonly_dirs, extra_notes=readonly_dirs_notes or '')
        + _memory_prompt_block(sw_str, shrink=bool(file_paths))
        + _language_alignment_block(message)
        + full_message
    )

    cmd.append('--')

    log.info('[CLI] 执行命令: claude ... --print ... -- （prompt 经 stdin 传入）')
    log.info(f'[CLI] session_id={session_id}, claude_session_id={claude_session_id}')
    log.info(
        f'[CLI] session_workspace={session_workspace_dir}, add_dirs={add_dirs}, '
        f'upload_dir={upload_dir}, 文件数={len(file_paths) if file_paths else 0}, 消息长度={len(full_message)}'
    )

    cwd_kw = {}
    if session_workspace_dir:
        try:
            cwp = Path(session_workspace_dir).resolve()
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
                        yield f'data: {json.dumps({"type": "thinking_start"})}\n\n'
                    elif cb_type == 'text':
                        stream_open_block = 'text'
                        yield f'data: {json.dumps({"type": "text_start"})}\n\n'
                    elif cb_type in ('tool_use', 'tool_use_block', 'server_tool_use', 'tool_calls'):
                        stream_open_block = 'tool'
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
                            yield f'data: {json.dumps({"type": "thinking", "content": thinking_chunk})}\n\n'
                    elif delta_type == 'text_delta':
                        text_chunk = delta.get('text', '')
                        if text_chunk:
                            streamed_text_delta = True
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
                        yield f'data: {json.dumps({"type": "thinking_start"})}\n\n'
                        thinking_text = content.get('thinking', '')
                        if thinking_text:
                            yield f'data: {json.dumps({"type": "thinking", "content": thinking_text})}\n\n'
                        yield f'data: {json.dumps({"type": "thinking_stop"})}\n\n'
                    elif ctype == 'text':
                        if streamed_text_delta:
                            continue
                        yield f'data: {json.dumps({"type": "text", "content": content["text"]})}\n\n'
                    elif ctype in ('tool_use', 'tool_use_block', 'server_tool_use'):
                        yield f'data: {json.dumps(_tool_start_payload(content))}\n\n'
                        inp = content.get('input')
                        if isinstance(inp, dict):
                            yield f'data: {json.dumps({"type": "tool_input_delta", "partial": json.dumps(inp, ensure_ascii=False)})}\n\n'
                        elif isinstance(inp, str) and inp.strip():
                            yield f'data: {json.dumps({"type": "tool_input_delta", "partial": inp})}\n\n'
                        yield f'data: {json.dumps({"type": "tool_stop"})}\n\n'

            elif msg_type == 'result':
                last_result_payload = data
                sid_val = data.get('session_id')
                if sid_val and not claude_sid_returned:
                    claude_sid_returned = sid_val
                    log.info(f'[CLI] Claude session_id (from result): {sid_val}')
                    yield f'data: {json.dumps({"type": "session", "session_id": sid_val})}\n\n'
                if data.get('is_error'):
                    api_error_yet = True
                    friendly = _friendly_api_error_from_result(data)
                    yield f'data: {json.dumps({"type": "error", "message": friendly, "soft": True})}\n\n'
                yield f'data: {json.dumps({"type": "done", "ok": not bool(data.get("is_error")), "result": data.get("result", "")})}\n\n'
                log.info(f'[CLI] 对话完成, returncode={process.poll()}')

    except queue.Empty:
        yield f'data: {json.dumps({"type": "error", "message": "Claude CLI 响应超时"})}\n\n'
    except Exception as e:
        log.error(f'[CLI] 处理输出时出错: {e}')
        yield f'data: {json.dumps({"type": "error", "message": f"处理输出时出错: {str(e)}"})}\n\n'
    finally:
        try:
            stdin_thread.join(timeout=60)
        except Exception:
            pass
        try:
            stderr_thread.join(timeout=5)
        except Exception:
            pass
        process.wait()
        if process.returncode and process.returncode != 0:
            if api_error_yet:
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
