"""Code scope validation for Android issue analysis Deep mode."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Iterable, List


CODE_SUFFIXES = {
    '.java',
    '.kt',
    '.kts',
    '.xml',
    '.gradle',
    '.properties',
    '.json',
    '.md',
}

SKIP_DIR_NAMES = {
    '.git',
    '.gradle',
    '.idea',
    'build',
    'out',
    'node_modules',
    '__pycache__',
}


def collect_candidate_bundle_ids(planner: Dict[str, Any], matched_rules: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    _append_unique(out, planner.get('candidate_bundle_ids') or [])
    for event in matched_rules.get('events') or []:
        _append_unique(out, event.get('source_bundle_ids') or [])
    return out


def resolve_code_scopes(
    bundle_ids: Iterable[str],
    configured_bundles: Iterable[Dict[str, Any]],
    preferred_paths: Dict[str, List[str]] | None = None,
) -> Dict[str, Any]:
    configured = {str(b.get('id') or ''): b for b in configured_bundles or [] if b.get('id')}
    scopes: List[Dict[str, Any]] = []
    denied: List[Dict[str, str]] = []

    for bundle_id in _dedupe(bundle_ids):
        bundle = configured.get(bundle_id)
        if not bundle:
            denied.append({'bundle_id': bundle_id, 'reason': 'bundle_not_configured_in_paths_config'})
            continue

        roots = []
        for raw in bundle.get('paths') or []:
            try:
                root = Path(str(raw)).expanduser().resolve()
            except OSError:
                continue
            if root.is_dir():
                roots.append(str(root))
        if not roots:
            denied.append({'bundle_id': bundle_id, 'reason': 'bundle_has_no_readable_paths'})
            continue

        preferred = _resolve_preferred_paths(roots, (preferred_paths or {}).get(bundle_id) or [])
        scopes.append(
            {
                'bundle_id': bundle_id,
                'title': str(bundle.get('title') or bundle.get('name') or bundle_id),
                'roots': roots,
                'preferred_paths': preferred,
            }
        )

    return {'version': 1, 'allowed': bool(scopes), 'scopes': scopes, 'denied': denied}


def collect_code_context(
    scope_result: Dict[str, Any],
    keywords: Iterable[str] | None = None,
    max_files: int = 20,
    max_chars_per_file: int = 12000,
) -> List[Dict[str, Any]]:
    keyword_list = _keyword_list(keywords or [])
    candidates: List[tuple[float, Dict[str, Any]]] = []
    for scope in scope_result.get('scopes') or []:
        roots = [Path(p) for p in (scope.get('preferred_paths') or scope.get('roots') or [])]
        for root in roots:
            for path in _iter_code_files(root):
                rel = _relative_to_any(path, [Path(p) for p in (scope.get('roots') or [])])
                score = _score_code_file(path, rel, keyword_list)
                if score <= 0:
                    continue
                candidates.append(
                    (
                        score,
                        {
                            'bundle_id': scope.get('bundle_id'),
                            'path': rel,
                            'suffix': path.suffix,
                            'size': _safe_size(path),
                            '_abs_path': path,
                        },
                    )
                )
    candidates.sort(key=lambda item: (item[0], -item[1].get('size', 0)), reverse=True)

    out: List[Dict[str, Any]] = []
    seen = set()
    for _, item in candidates:
        key = (item.get('bundle_id'), item.get('path'))
        if key in seen:
            continue
        seen.add(key)
        path = item.pop('_abs_path')
        snippet, truncated = _read_code_snippet(path, max_chars_per_file)
        item['snippet'] = snippet
        item['truncated'] = truncated
        out.append(item)
        if len(out) >= max_files:
            break
    return out


def _iter_code_files(root: Path):
    if root.is_file():
        if root.suffix.lower() in CODE_SUFFIXES:
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES and not d.startswith('.')]
        base = Path(dirpath)
        for filename in filenames:
            path = base / filename
            if path.suffix.lower() in CODE_SUFFIXES:
                yield path


def _resolve_preferred_paths(roots: List[str], preferred: Iterable[str]) -> List[str]:
    out: List[str] = []
    root_paths = [Path(p).resolve() for p in roots]
    for raw in preferred or []:
        rel = str(raw or '').strip().replace('\\', '/')
        if not rel or rel.startswith('/') or '..' in rel.split('/') or ':' in rel.split('/')[0]:
            continue
        for root in root_paths:
            try:
                candidate = (root / rel).resolve()
                candidate.relative_to(root)
            except (OSError, ValueError):
                continue
            if candidate.exists():
                out.append(str(candidate))
                break
    return _dedupe(out)


def _score_code_file(path: Path, rel: str, keywords: List[str]) -> float:
    text = rel.lower()
    score = 0.1
    if any(part in text for part in ('src/', 'main/', 'java/', 'kotlin/', 'res/')):
        score += 0.15
    for keyword in keywords:
        if keyword and keyword in text:
            score += 0.4
    try:
        head = path.read_text(encoding='utf-8', errors='ignore')[:6000].lower()
    except OSError:
        return 0
    for keyword in keywords:
        if keyword and keyword in head:
            score += 0.25
    return score


def _read_code_snippet(path: Path, max_chars: int) -> tuple[str, bool]:
    try:
        text = path.read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return '', False
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars].rstrip() + '\n...[code truncated]\n', True


def _keyword_list(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    for raw in values:
        value = str(raw or '').strip().lower()
        if not value or len(value) > 80:
            continue
        if value not in out:
            out.append(value)
        for part in value.replace('.', ' ').replace('-', ' ').replace('_', ' ').split():
            if len(part) >= 3 and part not in out:
                out.append(part)
    return out[:80]


def _append_unique(out: List[str], values: Iterable[str]) -> None:
    for value in values:
        s = str(value or '').strip()
        if s and s not in out:
            out.append(s)


def _dedupe(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        s = str(value or '').strip()
        if s and s not in out:
            out.append(s)
    return out


def _relative_to_any(path: Path, roots: List[Path]) -> str:
    resolved = path.resolve()
    for root in roots:
        try:
            return resolved.relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
    return path.name


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
