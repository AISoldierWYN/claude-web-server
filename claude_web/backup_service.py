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


def _copy2_best_effort(src, dst, *, follow_symlinks=True):
    """Copy a file for delete-backup snapshots, tolerating volatile CLI files."""
    try:
        return shutil.copy2(src, dst, follow_symlinks=follow_symlinks)
    except (FileNotFoundError, PermissionError, OSError):
        return dst


def _make_backup_ignore(session_dir: Path):
    volatile_debug_parts = ('.claude_web_home', '.claude', 'debug')

    def ignore(dir_path: str, names: list[str]) -> set[str]:
        current = Path(dir_path)
        try:
            rel_parts = current.relative_to(session_dir).parts
        except ValueError:
            rel_parts = ()

        ignored: set[str] = set()
        if rel_parts == volatile_debug_parts or rel_parts[:3] == volatile_debug_parts:
            ignored.update(names)
            return ignored
        if rel_parts == volatile_debug_parts[:2]:
            ignored.add('debug')

        for name in names:
            child = current / name
            if not child.exists() and not child.is_symlink():
                ignored.add(name)
        return ignored

    return ignore


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
    shutil.copytree(
        session_dir,
        dest_snapshot,
        dirs_exist_ok=True,
        ignore=_make_backup_ignore(session_dir),
        copy_function=_copy2_best_effort,
        ignore_dangling_symlinks=True,
    )

    cli_src = log_dir / 'users' / user_id / 'sessions' / f'{session_id}_cli.log'
    if cli_src.is_file():
        shutil.copy2(cli_src, dest_root / 'cli.log')

    return dest_root
