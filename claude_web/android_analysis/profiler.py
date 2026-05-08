"""File tree and manifest generation for extracted Android logs."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

from .models import AndroidAnalysisError, ProfileLimits


_TEXT_SUFFIXES = {
    '.txt',
    '.log',
    '.out',
    '.trace',
    '.traces',
    '.anr',
    '.csv',
    '.json',
    '.xml',
    '.prop',
}


def profile_extracted_tree(
    extracted_dir: Path,
    artifacts_dir: Path,
    limits: ProfileLimits | None = None,
) -> Dict[str, Any]:
    limits = limits or ProfileLimits()
    extracted_dir = Path(extracted_dir).resolve()
    artifacts_dir = Path(artifacts_dir).resolve()
    if not extracted_dir.is_dir():
        raise AndroidAnalysisError('extracted_dir_missing', 'Extracted directory does not exist.')

    files: List[Dict[str, Any]] = []
    for path in sorted(extracted_dir.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(extracted_dir).as_posix()
        depth = len(Path(rel).parts)
        if depth > limits.max_depth:
            raise AndroidAnalysisError('profile_path_too_deep', 'Extracted file path exceeds the depth limit.')
        if len(files) >= limits.max_files:
            raise AndroidAnalysisError('profile_too_many_files', 'Extracted tree contains too many files.')
        stat = path.stat()
        files.append(
            {
                'path': rel,
                'name': path.name,
                'size': stat.st_size,
                'modified_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime)),
                'kind': detect_file_kind(rel),
            }
        )

    manifest = {
        'version': 1,
        'root': '.',
        'file_count': len(files),
        'total_size': sum(f['size'] for f in files),
        'files': files,
    }
    tree = {
        'version': 1,
        'root': build_tree(files),
    }

    artifacts_dir.mkdir(parents=True, exist_ok=True)
    _write_json(artifacts_dir / 'file_manifest.json', manifest)
    _write_json(artifacts_dir / 'file_tree.json', tree)
    return {'manifest': manifest, 'tree': tree}


def detect_file_kind(rel_path: str) -> str:
    p = rel_path.replace('\\', '/').lower()
    name = Path(p).name
    suffix = Path(p).suffix
    if 'tombstone' in p:
        return 'android_tombstone'
    if 'anr' in p or name in {'traces.txt', 'trace.txt'}:
        return 'android_anr_trace'
    if 'events' in p and (suffix == '.log' or 'logcat' in p):
        return 'android_events_log'
    if 'radio' in p and (suffix == '.log' or 'logcat' in p):
        return 'android_radio_log'
    if 'system' in p and (suffix == '.log' or 'logcat' in p):
        return 'android_system_log'
    if 'main' in p and (suffix == '.log' or 'logcat' in p):
        return 'android_main_log'
    if 'logcat' in p or suffix == '.log':
        return 'android_logcat'
    if 'bugreport' in p:
        return 'android_bugreport'
    if 'dumpsys' in p:
        return 'android_dumpsys'
    if 'crash' in p:
        return 'android_crash'
    if suffix in _TEXT_SUFFIXES:
        return 'text'
    return 'unknown'


def build_tree(files: List[Dict[str, Any]]) -> Dict[str, Any]:
    root: Dict[str, Any] = {'name': '.', 'type': 'directory', 'children': []}
    for item in files:
        current = root
        parts = item['path'].split('/')
        for part in parts[:-1]:
            child = _find_child(current, part, 'directory')
            if child is None:
                child = {'name': part, 'type': 'directory', 'children': []}
                current['children'].append(child)
            current = child
        current['children'].append(
            {
                'name': parts[-1],
                'type': 'file',
                'size': item['size'],
                'kind': item['kind'],
                'path': item['path'],
            }
        )
    return root


def _find_child(node: Dict[str, Any], name: str, child_type: str):
    for child in node.get('children') or []:
        if child.get('name') == name and child.get('type') == child_type:
            return child
    return None


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
