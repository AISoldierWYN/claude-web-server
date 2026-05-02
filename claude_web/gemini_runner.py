"""Gemini CLI subprocess and stream-json to SSE adapter."""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config
from .claude_runner import (
    _conversation_history_prompt_block,
    _format_one_attachment_block,
    _language_alignment_block,
    _memory_prompt_block,
    _read_attachment_for_prompt,
    _sandbox_instruction,
    _skill_bundles_instruction,
    _turn_attachment_instruction,
    _web_search_prompt_block,
)
from .session_manager import SESSION_MEMORY_FILENAME
from .user_session_log import append_cli_exit_summary, append_cli_line

log = logging.getLogger('claude-web')

_RUNNING_GEMINI_PROCESSES: Dict[str, subprocess.Popen] = {}
_RUNNING_GEMINI_PROCESSES_LOCK = threading.Lock()


def _sse(obj: dict) -> str:
    return 'data: ' + json.dumps(obj, ensure_ascii=False) + '\n\n'


def stop_gemini_session_process(session_id: str) -> bool:
    sid = (session_id or '').strip()
    if not sid:
        return False
    with _RUNNING_GEMINI_PROCESSES_LOCK:
        proc = _RUNNING_GEMINI_PROCESSES.get(sid)
    if not proc or proc.poll() is not None:
        return False
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return True
    except Exception as e:
        log.warning('[Gemini CLI] 停止会话进程失败 session=%s: %s', sid, e)
        return False


def _gemini_child_env(child_env_extra: Optional[Dict[str, str]] = None) -> dict:
    env = os.environ.copy()
    env.setdefault('PYTHONIOENCODING', 'utf-8')
    proxy = (getattr(config, 'GEMINI_PROXY', '') or '').strip()
    if proxy:
        env['HTTP_PROXY'] = proxy
        env['HTTPS_PROXY'] = proxy
        env['http_proxy'] = proxy
        env['https_proxy'] = proxy
    if child_env_extra:
        for k, v in child_env_extra.items():
            if k:
                env[str(k)] = str(v)
    return env


def _resolved_dirs(readonly_dirs: List[str], session_workspace_dir: Optional[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for d in list(readonly_dirs or []) + ([session_workspace_dir] if session_workspace_dir else []):
        if not d:
            continue
        try:
            p = Path(d).resolve()
            if p.is_dir():
                s = str(p)
                if s not in seen:
                    seen.add(s)
                    out.append(s)
        except OSError:
            pass
    return out


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r'[\u3400-\u9fff]', text or ''))


_GEMINI_PROCESS_WORD_RE = re.compile(
    r'\b(providing|synthesi[sz]ing|analy[sz]ing|thinking|planning|i\s+will|i[\'’]m|i\s+am)\b',
    re.IGNORECASE,
)


def _clean_gemini_text_chunk(text: str, user_message: str) -> str:
    """Remove Gemini CLI process narration that is emitted as normal text."""
    if not text:
        return ''
    out = re.sub(r'\[Thought:[^\]]*\]\s*', '', str(text))
    if not _contains_cjk(user_message):
        return out
    if not _contains_cjk(out) and _GEMINI_PROCESS_WORD_RE.search(out):
        return ''
    first_cjk = None
    for i, ch in enumerate(out):
        if '\u3400' <= ch <= '\u9fff':
            first_cjk = i
            break
    if first_cjk and _GEMINI_PROCESS_WORD_RE.search(out[:first_cjk]):
        out = out[first_cjk:]
    return out


def _is_noisy_gemini_stderr(line: str) -> bool:
    low = (line or '').lower()
    return (
        '256-color support not detected' in low
        or 'ripgrep is not available' in low
    )


def _is_capacity_gemini_stderr(line: str) -> bool:
    low = (line or '').lower()
    return (
        'status 429' in low
        or '"code": 429' in low
        or 'too many requests' in low
        or 'resource_exhausted' in low
        or 'model_capacity_exhausted' in low
        or 'no capacity available' in low
        or 'ratelimitexceeded' in low
    )


def _is_stacky_gemini_stderr(line: str) -> bool:
    low = (line or '').strip().lower()
    return (
        low.startswith('at ')
        or low.startswith('config:')
        or low.startswith('response:')
        or low.startswith('headers:')
        or low.startswith('request:')
        or low.startswith('agent:')
        or low.startswith('proxy:')
        or low.startswith('body:')
        or low.startswith('data:')
        or low in ('{', '}', '},', '],', ']')
    )


def _resolve_memory_tool_path(params: Dict[str, Any], session_workspace_dir: Optional[str]) -> Optional[Path]:
    if not session_workspace_dir or not isinstance(params, dict):
        return None
    try:
        session_dir = Path(session_workspace_dir).resolve()
        memory_path = (session_dir / SESSION_MEMORY_FILENAME).resolve()
        raw_file_path = str(params.get('file_path') or params.get('path') or '').strip()
        if not raw_file_path:
            return None
        normalized_file_path = raw_file_path.replace('\\', '/')
        if normalized_file_path in {SESSION_MEMORY_FILENAME, './' + SESSION_MEMORY_FILENAME}:
            requested_path = memory_path
        else:
            requested_path = Path(raw_file_path).expanduser().resolve()
        if requested_path != memory_path or memory_path.parent != session_dir:
            return None
        return memory_path
    except Exception as e:
        log.warning('[Gemini memory] 解析 memory.md 工具路径失败: %s', e)
        return None


def _extract_tool_params(data: dict) -> Any:
    for key in ('parameters', 'args', 'arguments', 'input'):
        val = data.get(key)
        if val is not None:
            return val
    return None


def _apply_memory_write_fallback(tool_name: str, params: Any, session_workspace_dir: Optional[str]) -> bool:
    """Gemini plan-mode may announce write/replace without executing it; only mirror memory.md."""
    if not isinstance(params, dict):
        return False
    tool = str(tool_name or '').lower()
    if tool not in {'write_file', 'writefile', 'write', 'replace'}:
        return False
    memory_path = _resolve_memory_tool_path(params, session_workspace_dir)
    if not memory_path:
        log.info('[Gemini memory] 忽略非本会话 memory.md 的工具写入: tool=%s path=%s', tool, params.get('file_path') or params.get('path'))
        return False
    try:
        if tool == 'replace':
            old_text = params.get('old_string')
            if old_text is None:
                old_text = params.get('old')
            if old_text is None:
                old_text = params.get('target')
            new_text = params.get('new_string')
            if new_text is None:
                new_text = params.get('new')
            if new_text is None:
                new_text = params.get('replacement')
            if not isinstance(old_text, str) or not isinstance(new_text, str):
                log.warning('[Gemini memory] replace 缺少 old/new 字符串，无法兜底写入 memory.md')
                return False
            current = memory_path.read_text(encoding='utf-8') if memory_path.exists() else ''
            if old_text in current:
                content = current.replace(old_text, new_text, 1)
            elif new_text.lstrip().startswith('#') or '用户' in new_text or '称呼' in new_text or '偏好' in new_text:
                content = new_text
            else:
                log.warning('[Gemini memory] replace 的 old_string 未匹配 memory.md，且 new_string 不像完整记忆内容')
                return False
        else:
            content = params.get('content')
            if not isinstance(content, str):
                log.warning('[Gemini memory] write_file 缺少 content 字符串，无法兜底写入 memory.md')
                return False
        memory_path.write_text(content, encoding='utf-8')
        log.info('[Gemini memory] 已由服务端兜底写入 %s', memory_path)
        return True
    except Exception as e:
        log.warning('[Gemini memory] 兜底写入 memory.md 失败: %s', e)
        return False


def _build_full_prompt(
    message: str,
    *,
    file_paths: Optional[List[str]],
    session_workspace_dir: Optional[str],
    readonly_dirs: List[str],
    readonly_dirs_notes: str,
    skill_bundles: Optional[List[Dict[str, Any]]],
    conversation_history: Optional[List[Dict[str, Any]]],
    web_search_context: str,
) -> str:
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
            user_text = message.strip() or '（用户未输入文字，仅上传了附件，请根据附件内容理解意图并回答。）'
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

    gemini_note = (
        '【Gemini CLI 模式 — Web 服务注入】\n'
        '当前会话使用 Gemini CLI。请按普通聊天助手方式回答；第一版不提供可见 thinking，'
        '也不支持移动端远程开发模式中的真实项目写入。\n'
        '不要输出思考过程、[Thought: ...]、Planning/Providing/Synthesizing 等过程性说明；'
        '用户使用中文提问时，最终可见回复必须使用中文。\n\n'
    )
    return (
        gemini_note
        + _skill_bundles_instruction(skill_bundles)
        + _sandbox_instruction(sw_str, readonly_dirs, extra_notes=readonly_dirs_notes or '')
        + _memory_prompt_block(sw_str, shrink=bool(file_paths))
        + _conversation_history_prompt_block(conversation_history)
        + _web_search_prompt_block(web_search_context)
        + _language_alignment_block(message)
        + full_message
    )


def _friendly_gemini_error(data: dict, stderr_tail: str = '') -> str:
    raw = data.get('message') or data.get('error') or data.get('status') or ''
    if isinstance(raw, (dict, list)):
        raw = json.dumps(raw, ensure_ascii=False)
    msg = str(raw or '').strip()
    low = (msg + '\n' + stderr_tail).lower()
    if 'auth' in low or 'oauth' in low or 'login' in low:
        return 'Gemini CLI 未完成认证或认证已过期。请先在服务端 PC 上完成 Gemini CLI 登录后再试。'
    if (
        '429' in low
        or 'quota' in low
        or 'rate limit' in low
        or 'ratelimit' in low
        or 'resource_exhausted' in low
        or 'capacity' in low
    ):
        return 'Gemini CLI 当前触发额度、频率限制或模型容量不足，请稍后再试，或切换模型/账号。'
    if msg:
        return 'Gemini CLI 执行失败：' + msg[:1200]
    if stderr_tail:
        return 'Gemini CLI 执行失败：' + stderr_tail[:1200]
    return 'Gemini CLI 执行失败，未返回可读错误详情。'


def stream_gemini_output(
    message,
    session_id=None,
    claude_session_id=None,
    gemini_session_id=None,
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
    """Call Gemini CLI and convert stream-json JSONL into the existing SSE protocol."""
    readonly_dirs = readonly_dirs if readonly_dirs is not None else []
    resume_id = (gemini_session_id or claude_session_id or '').strip()
    if resume_id == 'latest':
        yield _sse({'type': 'error', 'message': 'Gemini resume id 不允许使用 latest。', 'soft': True, 'fatal': True})
        yield _sse({'type': 'done', 'ok': False})
        return
    if development_context or cli_cwd_dir:
        yield _sse({'type': 'error', 'message': 'Gemini 第一版暂不支持开发模式真实项目写入。', 'soft': True, 'fatal': True})
        yield _sse({'type': 'done', 'ok': False})
        return

    exe = (getattr(config, 'GEMINI_CLI_PATH', '') or 'gemini').strip() or 'gemini'
    cmd = [
        exe,
        '--output-format', 'stream-json',
        '--approval-mode', (getattr(config, 'GEMINI_APPROVAL_MODE', '') or 'plan').strip() or 'plan',
        '--prompt', 'Use the UTF-8 prompt from stdin.',
    ]
    model_eff = (model_override or '').strip() if model_override else (getattr(config, 'GEMINI_MODEL', '') or '').strip()
    if model_eff:
        cmd.extend(['--model', model_eff])
    if getattr(config, 'GEMINI_SKIP_TRUST', True):
        cmd.append('--skip-trust')
    if getattr(config, 'GEMINI_SANDBOX', False):
        cmd.append('--sandbox')
    if resume_id:
        cmd.extend(['--resume', resume_id])

    include_dirs = _resolved_dirs(readonly_dirs, session_workspace_dir)
    for d in include_dirs:
        cmd.extend(['--include-directories', d])

    full_message = _build_full_prompt(
        message,
        file_paths=file_paths,
        session_workspace_dir=session_workspace_dir,
        readonly_dirs=readonly_dirs,
        readonly_dirs_notes=readonly_dirs_notes,
        skill_bundles=skill_bundles,
        conversation_history=conversation_history,
        web_search_context=web_search_context,
    )

    cwd_kw = {}
    if session_workspace_dir:
        try:
            cwp = Path(session_workspace_dir).resolve()
            if cwp.is_dir():
                cwd_kw['cwd'] = str(cwp)
        except OSError:
            pass

    log.info('[Gemini CLI] 执行命令: gemini ... --output-format stream-json')
    log.info(
        '[Gemini CLI] session_id=%s, gemini_session_id=%s, include_dirs=%s, upload_dir=%s, 消息长度=%s',
        session_id, resume_id, include_dirs, upload_dir, len(full_message),
    )

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
            env=_gemini_child_env(child_env_extra),
            **cwd_kw,
        )
        if session_id:
            with _RUNNING_GEMINI_PROCESSES_LOCK:
                _RUNNING_GEMINI_PROCESSES[str(session_id)] = process
    except FileNotFoundError:
        yield _sse({'type': 'error', 'message': f'Gemini CLI 未找到: {exe}', 'soft': True, 'fatal': True})
        yield _sse({'type': 'done', 'ok': False})
        return
    except Exception as e:
        yield _sse({'type': 'error', 'message': f'启动 Gemini CLI 失败: {e}', 'soft': True, 'fatal': True})
        yield _sse({'type': 'done', 'ok': False})
        return

    def stdin_writer():
        try:
            process.stdin.write(full_message)
            process.stdin.close()
        except BrokenPipeError:
            pass
        except Exception as e:
            log.warning('[Gemini CLI stdin] 写入失败: %s', e)
            try:
                process.stdin.close()
            except Exception:
                pass

    stdin_thread = threading.Thread(target=stdin_writer, daemon=True)
    stdin_thread.start()

    line_queue: queue.Queue = queue.Queue()
    stderr_lines: List[str] = []
    stdout_recent = deque(maxlen=60)
    noisy_stderr_seen = set()
    capacity_stderr_logged = [False]

    log_dir = cli_log_context.get('log_dir') if cli_log_context else None
    uid = cli_log_context.get('user_id') if cli_log_context else None
    sid = cli_log_context.get('session_id') if cli_log_context else None

    def stdout_reader():
        try:
            for line in process.stdout:
                line_queue.put(line)
        finally:
            line_queue.put(None)
            process.stdout.close()

    def stderr_reader():
        try:
            for line in process.stderr:
                stripped = line.strip()
                if stripped:
                    stderr_lines.append(stripped)
                    if _is_noisy_gemini_stderr(stripped):
                        noise_key = 'ripgrep' if 'ripgrep' in stripped.lower() else 'terminal-color'
                        if noise_key not in noisy_stderr_seen:
                            noisy_stderr_seen.add(noise_key)
                            log.info('[Gemini CLI stderr] %s', stripped[:300])
                    elif _is_capacity_gemini_stderr(stripped):
                        if not capacity_stderr_logged[0]:
                            capacity_stderr_logged[0] = True
                            log.warning('[Gemini CLI stderr] Gemini 服务端容量或限流: %s', stripped[:500])
                        else:
                            log.debug('[Gemini CLI stderr] %s', stripped[:500])
                    elif capacity_stderr_logged[0] and _is_stacky_gemini_stderr(stripped):
                        log.debug('[Gemini CLI stderr] %s', stripped[:500])
                    else:
                        log.warning('[Gemini CLI stderr] %s', stripped[:500])
                    if log_dir and uid and sid:
                        append_cli_line(log_dir, uid, sid, f'[gemini stderr] {stripped}')
        finally:
            process.stderr.close()

    stdout_thread = threading.Thread(target=stdout_reader, daemon=True)
    stderr_thread = threading.Thread(target=stderr_reader, daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    done_seen = False
    error_seen = False

    try:
        while True:
            try:
                line = line_queue.get(timeout=getattr(config, 'GEMINI_REQUEST_TIMEOUT_SECONDS', 300))
            except queue.Empty:
                error_seen = True
                yield _sse({'type': 'error', 'message': 'Gemini CLI 响应超时', 'soft': True, 'fatal': True})
                yield _sse({'type': 'done', 'ok': False})
                done_seen = True
                break
            if line is None:
                break
            line = line.strip()
            if not line:
                continue
            stdout_recent.append(line if len(line) < 8000 else line[:4000] + '…[truncated]')
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                log.debug('[Gemini CLI] 非 JSON 输出: %s', line[:300])
                continue

            msg_type = data.get('type')
            if msg_type == 'init':
                sid_val = data.get('session_id')
                if sid_val:
                    yield _sse({'type': 'session', 'session_id': sid_val, 'provider': 'gemini'})
            elif msg_type == 'message':
                if data.get('role') == 'assistant':
                    content = data.get('content')
                    if isinstance(content, str) and content:
                        cleaned = _clean_gemini_text_chunk(content, message)
                        if cleaned:
                            yield _sse({'type': 'text', 'content': cleaned})
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get('text'):
                                cleaned = _clean_gemini_text_chunk(str(part.get('text')), message)
                                if cleaned:
                                    yield _sse({'type': 'text', 'content': cleaned})
            elif msg_type == 'tool_use':
                name = data.get('tool_name') or data.get('name') or ''
                tid = data.get('tool_id') or data.get('id') or ''
                yield _sse({'type': 'tool_start', 'name': name, 'id': tid})
                params = _extract_tool_params(data)
                _apply_memory_write_fallback(name, params, session_workspace_dir)
                if params is not None:
                    partial = json.dumps(params, ensure_ascii=False) if isinstance(params, (dict, list)) else str(params)
                    yield _sse({'type': 'tool_input_delta', 'partial': partial})
            elif msg_type == 'tool_result':
                yield _sse({
                    'type': 'tool_stop',
                    'id': data.get('tool_id') or data.get('id') or '',
                    'status': data.get('status') or '',
                })
            elif msg_type == 'error':
                error_seen = True
                yield _sse({
                    'type': 'error',
                    'message': _friendly_gemini_error(data, '\n'.join(stderr_lines[-200:])),
                    'soft': True,
                    'fatal': True,
                })
                yield _sse({'type': 'done', 'ok': False})
                done_seen = True
            elif msg_type == 'result':
                done_seen = True
                status = str(data.get('status') or '').lower()
                ok = status in ('success', 'ok', 'completed', 'complete') or not status
                if not ok:
                    error_seen = True
                    yield _sse({
                        'type': 'error',
                        'message': _friendly_gemini_error(data, '\n'.join(stderr_lines[-200:])),
                        'soft': True,
                        'fatal': True,
                    })
                yield _sse({'type': 'done', 'ok': ok, 'result': data})

        if not done_seen and not error_seen:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            rc = process.poll()
            if rc not in (None, 0):
                yield _sse({
                    'type': 'error',
                    'message': _friendly_gemini_error({}, '\n'.join(stderr_lines[-200:])),
                    'soft': True,
                    'fatal': True,
                })
                yield _sse({'type': 'done', 'ok': False})
            else:
                yield _sse({'type': 'done', 'ok': True})
    except Exception as e:
        log.error('[Gemini CLI] 处理输出时出错: %s', e)
        yield _sse({'type': 'error', 'message': f'处理 Gemini CLI 输出时出错: {e}'})
    finally:
        if process.poll() is None and (error_seen and not done_seen):
            try:
                process.terminate()
            except Exception:
                pass
        try:
            stdin_thread.join(timeout=10)
        except Exception:
            pass
        try:
            stderr_thread.join(timeout=5)
        except Exception:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        if session_id:
            with _RUNNING_GEMINI_PROCESSES_LOCK:
                if _RUNNING_GEMINI_PROCESSES.get(str(session_id)) is process:
                    _RUNNING_GEMINI_PROCESSES.pop(str(session_id), None)
        if process.returncode and process.returncode != 0 and not error_seen:
            stderr_summary = '\n'.join(stderr_lines[-200:]) if stderr_lines else ''
            stdout_tail = '\n'.join(list(stdout_recent)[-20:])
            if log_dir and uid and sid:
                append_cli_exit_summary(
                    log_dir, uid, sid, process.returncode,
                    (stderr_summary + '\n' + stdout_tail)[:8000],
                )
