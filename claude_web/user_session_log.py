"""按用户、会话追加 CLI stderr 与退出摘要。"""

import json
import time
from pathlib import Path


def cli_log_path(log_dir: Path, user_id: str, session_id: str) -> Path:
    return log_dir / 'users' / user_id / 'sessions' / f'{session_id}_cli.log'


def append_cli_line(log_dir: Path, user_id: str, session_id: str, line: str) -> None:
    p = cli_log_path(log_dir, user_id, session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    with open(p, 'a', encoding='utf-8', errors='replace') as f:
        f.write(f'{ts} {line}\n')


def append_cli_exit_summary(
    log_dir: Path,
    user_id: str,
    session_id: str,
    returncode: int,
    stderr_tail: str,
) -> None:
    rec = {
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'returncode': returncode,
        'stderr_tail': (stderr_tail or '')[-4000:],
    }
    append_cli_line(
        log_dir, user_id, session_id,
        '[exit] ' + json.dumps(rec, ensure_ascii=False),
    )
