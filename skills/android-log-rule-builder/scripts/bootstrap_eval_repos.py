#!/usr/bin/env python3
"""下载 Android Rule Builder 评测用开源仓库。

外部仓库只放在 tests/github_apps/，该目录已被 .gitignore 排除。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[3]
DEFAULT_REPOS_FILE = REPO_ROOT / 'android_analysis_eval' / 'repos.json'


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='下载 Android 评测用开源项目到 tests/github_apps')
    parser.add_argument('--repos-file', default=str(DEFAULT_REPOS_FILE), help='repos.json 路径')
    parser.add_argument('--repo', action='append', default=[], help='只下载指定 owner/name，可重复')
    parser.add_argument('--depth', type=int, default=1, help='git clone depth')
    parser.add_argument('--proxy', default='', help='可选 HTTP/HTTPS 代理，例如 http://127.0.0.1:1080')
    parser.add_argument('--dry-run', action='store_true', help='只打印命令，不执行')
    args = parser.parse_args(argv)

    repos = load_repos(Path(args.repos_file))
    selected = set(args.repo or [])
    if selected:
        repos = [repo for repo in repos if str(repo.get('repo') or '') in selected]
    if not repos:
        print('没有匹配的仓库', file=sys.stderr)
        return 2

    env = os.environ.copy()
    if args.proxy:
        env['HTTP_PROXY'] = args.proxy
        env['HTTPS_PROXY'] = args.proxy

    for repo in repos:
        rc = ensure_repo(repo, depth=args.depth, env=env, dry_run=args.dry_run)
        if rc != 0:
            return rc
    return 0


def load_repos(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding='utf-8'))
    repos = data.get('repositories') or []
    if not isinstance(repos, list):
        raise SystemExit(f'repositories 必须是数组：{path}')
    return [repo for repo in repos if isinstance(repo, dict)]


def ensure_repo(repo: Dict[str, Any], depth: int, env: Dict[str, str], dry_run: bool) -> int:
    name = str(repo.get('repo') or '').strip()
    url = str(repo.get('url') or '').strip()
    local_dir = REPO_ROOT / str(repo.get('local_dir') or '').strip()
    if not name or not url or not str(repo.get('local_dir') or '').strip():
        print(f'跳过无效 repo 配置：{repo}', file=sys.stderr)
        return 2
    sparse_paths = [str(x) for x in (repo.get('sparse_paths') or []) if str(x).strip()]
    if (local_dir / '.git').is_dir():
        print(f'[skip] {name}: {local_dir} 已存在')
        rc = configure_repo(local_dir, env, dry_run)
        if rc != 0:
            return rc
        return apply_sparse(local_dir, sparse_paths, env, dry_run)
    local_dir.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        'git',
        'clone',
        '--depth',
        str(max(depth, 1)),
        '--filter=blob:none',
        '--sparse',
        url,
        str(local_dir),
    ]
    print('[clone] ' + ' '.join(cmd))
    if dry_run:
        return 0
    rc = subprocess.call(cmd, cwd=REPO_ROOT, env=env)
    if rc != 0:
        return rc
    rc = configure_repo(local_dir, env, dry_run)
    if rc != 0:
        return rc
    return apply_sparse(local_dir, sparse_paths, env, dry_run)


def configure_repo(local_dir: Path, env: Dict[str, str], dry_run: bool) -> int:
    if not sys.platform.startswith('win'):
        return 0
    cmd = ['git', '-C', str(local_dir), 'config', 'core.longpaths', 'true']
    print('[config] ' + ' '.join(cmd))
    if dry_run:
        return 0
    return subprocess.call(cmd, cwd=REPO_ROOT, env=env)


def apply_sparse(local_dir: Path, sparse_paths: List[str], env: Dict[str, str], dry_run: bool) -> int:
    if sparse_paths:
        sparse_cmd = ['git', '-C', str(local_dir), 'sparse-checkout', 'set', '--cone', '--skip-checks', *sparse_paths]
        print('[sparse] ' + ' '.join(sparse_cmd))
        if not dry_run:
            return subprocess.call(sparse_cmd, cwd=REPO_ROOT, env=env)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
