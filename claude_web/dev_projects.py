"""Mobile remote development project whitelist and helpers."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DEV_SESSION_FILENAME = 'dev_session.json'


class DevProjectError(RuntimeError):
    pass


def _safe_id(value: str) -> str:
    raw = (value or '').strip()
    out = []
    for ch in raw:
        if ch.isalnum() or ch in '._-':
            out.append(ch)
    return ''.join(out)[:80]


def _run_git(project_path: Path, args: List[str], timeout: int = 8) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['git', '-C', str(project_path), *args],
        text=True,
        encoding='utf-8',
        errors='replace',
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        shell=False,
    )


def git_status(project_path: Path) -> Dict[str, Any]:
    status = {
        'is_git_repo': False,
        'branch': '',
        'commit': '',
        'dirty': False,
        'changed_count': 0,
        'untracked_count': 0,
        'status_short': '',
        'error': '',
    }
    try:
        inside = _run_git(project_path, ['rev-parse', '--is-inside-work-tree'])
        if inside.returncode != 0 or inside.stdout.strip().lower() != 'true':
            status['error'] = (inside.stderr or inside.stdout).strip()
            return status

        status['is_git_repo'] = True
        branch = _run_git(project_path, ['rev-parse', '--abbrev-ref', 'HEAD'])
        commit = _run_git(project_path, ['rev-parse', '--short', 'HEAD'])
        short = _run_git(project_path, ['status', '--short'])
        status_text = short.stdout.strip()

        status['branch'] = branch.stdout.strip() if branch.returncode == 0 else ''
        status['commit'] = commit.stdout.strip() if commit.returncode == 0 else ''
        status['status_short'] = status_text
        lines = [line for line in status_text.splitlines() if line.strip()]
        status['changed_count'] = len(lines)
        status['untracked_count'] = sum(1 for line in lines if line.startswith('??'))
        status['dirty'] = bool(lines)
        return status
    except Exception as e:
        status['error'] = str(e)
        return status


def load_projects(config_path: Path) -> List[Dict[str, Any]]:
    if not config_path.is_file():
        return []
    try:
        data = json.loads(config_path.read_text(encoding='utf-8-sig'))
    except (OSError, json.JSONDecodeError) as e:
        raise DevProjectError(f'项目白名单配置读取失败: {e}') from e

    raw_projects = data.get('projects') if isinstance(data, dict) else None
    if not isinstance(raw_projects, list):
        raise DevProjectError('项目白名单配置必须包含 projects 数组')

    projects: List[Dict[str, Any]] = []
    seen = set()
    for raw in raw_projects:
        if not isinstance(raw, dict):
            continue
        project_id = _safe_id(str(raw.get('id') or ''))
        name = str(raw.get('name') or project_id).strip()
        path_raw = str(raw.get('path') or '').strip()
        if not project_id or not path_raw or project_id in seen:
            continue
        path = Path(path_raw).expanduser()
        try:
            path = path.resolve()
        except OSError:
            continue
        if not path.is_dir():
            continue
        tests = raw.get('default_tests') or []
        if isinstance(tests, str):
            tests = [tests]
        tests = [str(t).strip() for t in tests if str(t).strip()]
        projects.append(
            {
                'id': project_id,
                'name': name or project_id,
                'path': str(path),
                'default_tests': tests,
                'allow_git_commit': bool(raw.get('allow_git_commit')),
                'allow_git_push': bool(raw.get('allow_git_push')),
            }
        )
        seen.add(project_id)
    return projects


def project_public_info(project: Dict[str, Any]) -> Dict[str, Any]:
    path = Path(project['path'])
    return {
        'id': project['id'],
        'name': project.get('name') or project['id'],
        'path': str(path),
        'default_tests': list(project.get('default_tests') or []),
        'allow_git_commit': bool(project.get('allow_git_commit')),
        'allow_git_push': bool(project.get('allow_git_push')),
        'git': git_status(path),
    }


def find_project(projects: List[Dict[str, Any]], project_id: str) -> Optional[Dict[str, Any]]:
    for project in projects:
        if project.get('id') == project_id:
            return project
    return None


def load_dev_session(session_dir: Path) -> Optional[Dict[str, Any]]:
    path = session_dir / DEV_SESSION_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_dev_session(session_dir: Path, project: Dict[str, Any]) -> Dict[str, Any]:
    session_dir.mkdir(parents=True, exist_ok=True)
    data = {
        'mode': 'development',
        'project_id': project['id'],
        'project_name': project.get('name') or project['id'],
        'project_path': project['path'],
        'started_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'permissions': [],
    }
    (session_dir / DEV_SESSION_FILENAME).write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    return data


def clear_dev_session(session_dir: Path) -> bool:
    path = session_dir / DEV_SESSION_FILENAME
    try:
        if path.is_file():
            path.unlink()
            return True
    except OSError:
        return False
    return False


def diff_for_project(project_path: Path, max_chars: int = 60000) -> Dict[str, Any]:
    status = git_status(project_path)
    if not status.get('is_git_repo'):
        return {'status': status, 'files': [], 'diff': '', 'truncated': False}

    diff = _run_git(project_path, ['diff', '--'])
    files = []
    for line in (status.get('status_short') or '').splitlines():
        if not line.strip():
            continue
        files.append(line[3:].strip() if len(line) > 3 else line.strip())
    text = diff.stdout or ''
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + '\n...[diff truncated]\n'
    return {'status': status, 'files': files, 'diff': text, 'truncated': truncated}


def run_project_test(project: Dict[str, Any], command: str, timeout: int) -> Dict[str, Any]:
    command = (command or '').strip()
    if command not in (project.get('default_tests') or []):
        raise DevProjectError('只能运行项目白名单中配置的预设测试命令')
    started = time.time()
    try:
        proc = subprocess.run(
            command,
            cwd=project['path'],
            text=True,
            encoding='utf-8',
            errors='replace',
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=True,
            timeout=timeout,
        )
        return {
            'command': command,
            'returncode': proc.returncode,
            'ok': proc.returncode == 0,
            'stdout': (proc.stdout or '')[-20000:],
            'stderr': (proc.stderr or '')[-20000:],
            'duration_seconds': round(time.time() - started, 2),
        }
    except subprocess.TimeoutExpired as e:
        return {
            'command': command,
            'returncode': None,
            'ok': False,
            'stdout': (e.stdout or '')[-20000:] if isinstance(e.stdout, str) else '',
            'stderr': (e.stderr or '')[-20000:] if isinstance(e.stderr, str) else '',
            'duration_seconds': round(time.time() - started, 2),
            'timeout': True,
        }
