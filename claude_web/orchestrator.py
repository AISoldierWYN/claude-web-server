"""外环编排（ReAct 风格）：多轮完整 claude 子进程、轮数上限、暂停/继续/总结。

职责：编排循环、暂停文件读写、继续/总结用的提示词；不负责 HTTP 与消息持久化。
"""

from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .claude_runner import build_api_error_retry_user_message, stream_claude_output

log = logging.getLogger('claude-web')

PAUSE_FILENAME = '.orchestration_pause.json'


def _sse(obj: dict) -> str:
    return 'data: ' + json.dumps(obj, ensure_ascii=False) + '\n\n'


def build_continue_segment_prompt() -> str:
    return (
        '【Web 服务注入 — 用户已点击「继续任务」】\n'
        '请在本段轮次内继续完成先前未达成的目标，换用尚未尝试过的可行方法（如 Bash、Python、pypdf、系统 OCR 等）。'
    )


def build_summarize_after_pause_prompt() -> str:
    return (
        '【Web 服务注入 — 多轮编排已暂停，用户选择「结束并总结」】\n'
        '请用简洁中文总结：当前任务进度、已尝试的方法、遇到的错误与可能原因、用户接下来可如何操作。'
    )


def _retry_message_from_last_error(last_soft_error: Optional[str]) -> str:
    err = (last_soft_error or '').strip()
    if err:
        return build_api_error_retry_user_message(err)
    return (
        '【Web 服务注入 — 上一轮执行未成功结束，请换策略】\n'
        '（无详细错误文本；请检查工具路径、上游限制，或尝试 Bash/Python 处理附件。）'
    )


def write_pause_state(
    session_dir: Path,
    *,
    claude_session_id: str,
    max_rounds_per_segment: int,
    rounds_used_segment: int,
    total_rounds_all_segments: int,
    last_error_preview: str,
) -> str:
    token = secrets.token_urlsafe(24)
    data = {
        'continuation_token': token,
        'claude_session_id': claude_session_id,
        'max_rounds_per_segment': max_rounds_per_segment,
        'rounds_used_segment': rounds_used_segment,
        'total_rounds_all_segments': total_rounds_all_segments,
        'last_error_preview': (last_error_preview or '')[:2000],
    }
    path = session_dir / PAUSE_FILENAME
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    log.info('[Orchestrator] 已写入暂停状态: %s', path)
    return token


def read_pause_state(session_dir: Path) -> Optional[dict]:
    p = session_dir / PAUSE_FILENAME
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as e:
        log.warning('[Orchestrator] 读取暂停状态失败: %s', e)
        return None


def clear_pause_state(session_dir: Path) -> None:
    p = session_dir / PAUSE_FILENAME
    try:
        if p.is_file():
            p.unlink()
            log.info('[Orchestrator] 已清除暂停状态: %s', p)
    except OSError as e:
        log.warning('[Orchestrator] 清除暂停状态失败: %s', e)


def validate_continuation_token(state: Optional[dict], token: str) -> bool:
    return bool(state and token and state.get('continuation_token') == token)


def stream_orchestrated_turns(
    *,
    first_message: str,
    file_paths: Optional[List[str]],
    session_id: str,
    initial_claude_session_id: Optional[str],
    max_rounds: int,
    upload_dir: str,
    session_workspace_dir: str,
    readonly_dirs: List[str],
    cli_log_context: Dict[str, Any],
    readonly_dirs_notes: str = '',
    skill_bundles: Optional[List[Dict[str, Any]]] = None,
    total_rounds_offset: int = 0,
) -> Iterator[str]:
    """
    外环：最多 max_rounds 次完整 claude 子进程；任一轮成功则结束。
    用尽仍失败：若 max_rounds>1 则写入暂停并 yield needs_continue；若 max_rounds==1 则仅结束（不暂停）。
    """
    resume_id = initial_claude_session_id
    sw = Path(session_workspace_dir).resolve()
    current_message = first_message
    current_files = file_paths
    total_rounds = total_rounds_offset

    base_kw: Dict[str, Any] = dict(
        session_id=session_id,
        upload_dir=upload_dir,
        session_workspace_dir=session_workspace_dir,
        readonly_dirs=readonly_dirs,
        readonly_dirs_notes=readonly_dirs_notes,
        skill_bundles=skill_bundles,
        cli_log_context=cli_log_context,
    )

    for round_idx in range(1, max_rounds + 1):
        total_rounds += 1
        yield _sse(
            {
                'type': 'orchestration_round',
                'round': round_idx,
                'max_rounds': max_rounds,
                'total_rounds': total_rounds,
            }
        )
        if round_idx > 1:
            yield _sse(
                {
                    'type': 'info',
                    'message': f'外环第 {round_idx}/{max_rounds} 轮：根据上一轮结果换策略重试…',
                }
            )

        inner = stream_claude_output(
            current_message,
            claude_session_id=resume_id,
            file_paths=current_files,
            **base_kw,
        )

        last_ok = True
        last_soft = None
        last_sid = None
        done_seen = False

        for event_str in inner:
            yield event_str
            if not event_str.startswith('data: '):
                continue
            try:
                evt = json.loads(event_str[6:].strip())
            except json.JSONDecodeError:
                continue
            t = evt.get('type')
            if t == 'session':
                sid = evt.get('session_id')
                if sid:
                    last_sid = sid
            elif t == 'error' and evt.get('soft'):
                last_soft = evt.get('message') or ''
            elif t == 'done':
                done_seen = True
                last_ok = evt.get('ok') is not False

        if not done_seen:
            last_ok = False

        if last_sid:
            resume_id = last_sid

        if last_ok:
            yield _sse(
                {
                    'type': 'orchestration_complete',
                    'rounds_used': round_idx,
                    'total_rounds': total_rounds,
                    'ok': True,
                }
            )
            return

        if round_idx >= max_rounds:
            if max_rounds > 1:
                token = write_pause_state(
                    sw,
                    claude_session_id=resume_id or session_id,
                    max_rounds_per_segment=max_rounds,
                    rounds_used_segment=round_idx,
                    total_rounds_all_segments=total_rounds,
                    last_error_preview=last_soft or '',
                )
                yield _sse(
                    {
                        'type': 'needs_continue',
                        'continuation_token': token,
                        'rounds_used_segment': round_idx,
                        'max_rounds_per_segment': max_rounds,
                        'total_rounds': total_rounds,
                        'message': '已达到本段最大外环轮数，仍未能成功。可选择继续任务或结束并总结。',
                    }
                )
                yield _sse({'type': 'done', 'ok': False, 'paused': True, 'orchestration': True})
            else:
                yield _sse(
                    {
                        'type': 'orchestration_complete',
                        'rounds_used': 1,
                        'total_rounds': total_rounds,
                        'ok': False,
                    }
                )
            return

        current_message = _retry_message_from_last_error(last_soft)
        current_files = None


def stream_summarize_only(
    *,
    message: str,
    session_id: str,
    claude_session_id: str,
    upload_dir: str,
    session_workspace_dir: str,
    readonly_dirs: List[str],
    cli_log_context: Dict[str, Any],
    readonly_dirs_notes: str = '',
    skill_bundles: Optional[List[Dict[str, Any]]] = None,
) -> Iterator[str]:
    """单轮总结（用户选择「结束并总结」）。"""
    yield _sse({'type': 'info', 'message': '正在生成结束总结…'})
    yield from stream_claude_output(
        message,
        session_id=session_id,
        claude_session_id=claude_session_id,
        file_paths=None,
        upload_dir=upload_dir,
        session_workspace_dir=session_workspace_dir,
        readonly_dirs=readonly_dirs,
        readonly_dirs_notes=readonly_dirs_notes,
        skill_bundles=skill_bundles,
        cli_log_context=cli_log_context,
    )
