"""按需技能包索引工具。

该模块只做本地静态索引：解析配置里的 skill/resource 路径，发现
SKILL.md 与 CLAUDE.md。真正的按需选择仍在路由层完成，prompt 注入在
runner 层完成，避免配置加载阶段读取大文件。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SKILL_FILENAME = 'SKILL.md'
CLAUDE_MD_FILENAME = 'CLAUDE.md'


def as_posix(path: Path) -> str:
    """统一输出给 CLI/prompt 使用的 POSIX 风格路径。"""
    try:
        return path.resolve().as_posix()
    except OSError:
        return str(path).replace('\\', '/')


def resolve_existing_path(raw: str, log, *, require_dir: bool = False) -> Optional[Path]:
    """解析用户配置路径；不存在或类型不匹配时只记录 warning，不中断启动。"""
    value = (raw or '').strip()
    if not value:
        return None
    try:
        path = Path(value).expanduser().resolve()
    except Exception as exc:
        log.warning('[Config] 路径无效 %s: %s', value, exc)
        return None
    if require_dir and not path.is_dir():
        log.warning('[Config] 忽略（非目录）: %s', value)
        return None
    if not path.exists():
        log.warning('[Config] 路径不存在，已忽略: %s', value)
        return None
    return path


def configured_path(item: Any) -> str:
    """从 string/dict 配置项中取 path 字段。"""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(item.get('path') or '').strip()
    return ''


def _frontmatter_field(text: str, key: str) -> str:
    pattern = re.compile(rf'^\s*{re.escape(key)}\s*:\s*(.+?)\s*$', re.IGNORECASE | re.MULTILINE)
    match = pattern.search(text or '')
    if not match:
        return ''
    return match.group(1).strip().strip('"\'')


def read_skill_metadata(skill_path: Path) -> Dict[str, str]:
    """轻量读取 SKILL.md 元数据；失败时仍返回基于目录名的兜底信息。"""
    fallback_id = skill_path.parent.name if skill_path.name.lower() == SKILL_FILENAME.lower() else skill_path.stem
    out = {
        'id': fallback_id,
        'title': fallback_id,
        'summary': '',
    }
    try:
        text = skill_path.read_text(encoding='utf-8', errors='replace')[:6000]
    except OSError:
        return out

    name = _frontmatter_field(text, 'name')
    desc = _frontmatter_field(text, 'description')
    heading = ''
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('#'):
            heading = stripped.lstrip('#').strip()
            break
    if name:
        out['id'] = re.sub(r'\s+', '-', name.strip()).lower()
        out['title'] = name.strip()
    elif heading:
        out['title'] = heading
    if desc:
        out['summary'] = desc
    elif heading and heading != out['title']:
        out['summary'] = heading
    return out


def _candidate_skill_files(path: Path) -> Iterable[Path]:
    """发现标准 skill 文件。

    兼容三种常见配置：
    - 直接指向 SKILL.md
    - 指向单个 skill 目录
    - 指向 .cursor/skills 这类 skill 根目录，扫描其第一层子目录
    """
    if path.is_file() and path.name.lower() == SKILL_FILENAME.lower():
        yield path
        return
    if not path.is_dir():
        return
    direct = path / SKILL_FILENAME
    if direct.is_file():
        yield direct
    try:
        children = sorted(path.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return
    for child in children:
        candidate = child / SKILL_FILENAME
        if child.is_dir() and candidate.is_file():
            yield candidate


def _dedupe_by_path(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for item in items:
        path = item.get('path') or ''
        key = path.lower()
        if not path or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def discover_skills(explicit_items: Any, search_roots: List[Path], log, bundle_id: str) -> List[Dict[str, Any]]:
    """从显式 skills 配置和 paths/resources 根目录中发现 SKILL.md。"""
    out: List[Dict[str, Any]] = []

    items = explicit_items if isinstance(explicit_items, list) else []
    for item in items:
        raw_path = configured_path(item)
        path = resolve_existing_path(raw_path, log) if raw_path else None
        if not path:
            continue
        for skill_file in _candidate_skill_files(path):
            metadata = read_skill_metadata(skill_file)
            if isinstance(item, dict):
                sid = str(item.get('id') or '').strip() or metadata['id']
                title = str(item.get('title') or item.get('name') or '').strip() or metadata['title']
                summary = str(item.get('summary') or item.get('description') or '').strip() or metadata['summary']
                keywords = item.get('keywords') if isinstance(item.get('keywords'), list) else []
            else:
                sid = metadata['id']
                title = metadata['title']
                summary = metadata['summary']
                keywords = []
            out.append(
                {
                    'id': sid,
                    'title': title,
                    'summary': summary,
                    'keywords': keywords,
                    'path': as_posix(skill_file),
                    'bundle_id': bundle_id,
                    'source': 'explicit',
                }
            )

    for root in search_roots:
        for skill_file in _candidate_skill_files(root):
            metadata = read_skill_metadata(skill_file)
            out.append(
                {
                    'id': metadata['id'],
                    'title': metadata['title'],
                    'summary': metadata['summary'],
                    'keywords': [],
                    'path': as_posix(skill_file),
                    'bundle_id': bundle_id,
                    'source': 'discovered',
                }
            )

    return _dedupe_by_path(out)


def discover_claude_md(search_roots: List[Path]) -> List[str]:
    """发现被按需挂载目录根部的 CLAUDE.md。"""
    out: List[str] = []
    seen = set()
    for root in search_roots:
        if not root.is_dir():
            continue
        candidate = root / CLAUDE_MD_FILENAME
        if not candidate.is_file():
            continue
        path = as_posix(candidate)
        key = path.lower()
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def normalize_resource_items(raw: Any, log) -> List[Dict[str, Any]]:
    """解析 resources 配置；目前只有目录会进入 --add-dir，文件只保留索引。"""
    items = raw if isinstance(raw, list) else []
    resources: List[Dict[str, Any]] = []
    for i, item in enumerate(items):
        raw_path = configured_path(item)
        path = resolve_existing_path(raw_path, log) if raw_path else None
        if not path:
            continue
        meta = item if isinstance(item, dict) else {}
        rid = str(meta.get('id') or '').strip() or f'resource-{i + 1}'
        resources.append(
            {
                'id': rid,
                'kind': str(meta.get('kind') or 'generic').strip() or 'generic',
                'summary': str(meta.get('summary') or meta.get('description') or '').strip(),
                'keywords': meta.get('keywords') if isinstance(meta.get('keywords'), list) else [],
                'path': as_posix(path),
                'is_dir': path.is_dir(),
            }
        )
    return resources
