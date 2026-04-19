"""删除会话前备份 cache 会话目录与对应 CLI 日志。"""

import shutil
import time
from pathlib import Path

from .paths import sanitize_ip_for_path


def _folder_segment(s: str, max_len: int = 120) -> str:
    out = []
    for c in (s or '')[:max_len]:
        if c.isalnum() or c in '._-':
            out.append(c)
        else:
            out.append('_')
    return ''.join(out) or 'x'


def backup_session_before_delete(
    backups_root: Path,
    cache_dir: Path,
    log_dir: Path,
    client_ip: str,
    user_id: str,
    session_id: str,
) -> Path | None:
    """
    将 cache/<ip>/<user>/<session>/ 复制到
    backups/<YYYY-MM-DD>/<ts>_<ip>_<user>_<session>/session_snapshot/
    并复制 CLI 日志为 cli.log（若存在）。
    """
    sip = sanitize_ip_for_path(client_ip)
    session_dir = cache_dir / sip / user_id / session_id
    if not session_dir.is_dir():
        return None

    date_str = time.strftime('%Y-%m-%d')
    ts = time.strftime('%Y%m%dT%H%M%S')
    folder_name = f'{ts}_{sip}_{_folder_segment(user_id)}_{_folder_segment(session_id)}'
    dest_root = backups_root / date_str / folder_name
    dest_snapshot = dest_root / 'session_snapshot'
    dest_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(session_dir, dest_snapshot, dirs_exist_ok=True)

    cli_src = log_dir / 'users' / user_id / 'sessions' / f'{session_id}_cli.log'
    if cli_src.is_file():
        shutil.copy2(cli_src, dest_root / 'cli.log')

    return dest_root
