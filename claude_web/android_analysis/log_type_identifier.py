"""Log type identification for Android expert workbench Phase 7."""

from __future__ import annotations

import gzip
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Tuple

from .models import AndroidAnalysisError


DEFAULT_LOG_TYPES: List[Dict[str, Any]] = [
    {
        'id': 'android_log',
        'title': 'Android logcat text',
        'path_patterns': [r'(?i)(logcat|main|system|events|radio).*\.(txt|log)$', r'(?i).*\.log$'],
        'content_patterns': [
            r'(?m)^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+\s+\d+\s+\d+\s+[VDIWEF]\s+',
            r'\b(AndroidRuntime|ActivityManager|SystemServer|FATAL EXCEPTION)\b',
        ],
        'priority': 45,
        'source': 'builtin',
        'notes': 'Built-in fallback for ordinary logcat text files.',
    },
    {
        'id': 'logcat',
        'title': 'Android logcat',
        'path_patterns': [r'(?i)(logcat|main|system|events|radio).*\.(txt|log)$', r'(?i).*\.log$'],
        'content_patterns': [
            r'(?m)^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+\s+\d+\s+\d+\s+[VDIWEF]\s+',
            r'\b(AndroidRuntime|ActivityManager|SystemServer|FATAL EXCEPTION)\b',
        ],
        'priority': 44,
        'source': 'builtin',
        'notes': 'Alias for templates that use log_type=logcat.',
    },
    {
        'id': 'dropbox',
        'title': 'Android dropbox entry',
        'path_patterns': [r'(?i)(dropbox|system_app_crash|data_app_crash|system_app_anr|system_server_watchdog)'],
        'content_patterns': [r'(?i)Process:\s+|Package:\s+|Subject:\s+|Build fingerprint:'],
        'priority': 60,
        'source': 'builtin',
    },
    {
        'id': 'anr',
        'title': 'ANR trace',
        'path_patterns': [r'(?i)(^|/)(anr|traces?)(/|\.|_)', r'(?i)traces\.txt$'],
        'content_patterns': [r'(?m)^----- pid \d+ at ', r'\bDALVIK THREADS\b', r'\bCmd line:\s+'],
        'priority': 70,
        'source': 'builtin',
    },
    {
        'id': 'tombstone',
        'title': 'Native tombstone',
        'path_patterns': [r'(?i)tombstone|native_crash'],
        'content_patterns': [r'(?i)signal \d+ \(', r'(?m)^backtrace:', r'Build fingerprint:'],
        'priority': 70,
        'source': 'builtin',
    },
    {
        'id': 'xts_report',
        'title': 'XTS/CTS/GTS report',
        'path_patterns': [r'(?i)(test_result|cts|gts|xts|tradefed).*\.(xml|html|txt|log)$'],
        'content_patterns': [r'<TestResult\b|<Module\b|INSTRUMENTATION_STATUS|TestRunner|Compatibility Test'],
        'priority': 65,
        'source': 'builtin',
    },
    {
        'id': 'trace',
        'title': 'Perfetto or systrace',
        'path_patterns': [r'(?i)(perfetto|systrace|trace).*\.(txt|trace|html|json|pftrace)$'],
        'content_patterns': [r'(?i)perfetto|systrace|sched_switch|trace_event_clock_sync'],
        'priority': 55,
        'source': 'builtin',
    },
    {
        'id': 'meminfo',
        'title': 'Android memory report',
        'path_patterns': [r'(?i)(meminfo|smaps|hprof)'],
        'content_patterns': [r'Applications Memory Usage|TOTAL\s+PSS|Native Heap|Dalvik Heap'],
        'priority': 55,
        'source': 'builtin',
    },
]


def identify_log_types(
    extracted_dir: Path,
    artifacts_dir: Path,
    expert_knowledge_cache: Dict[str, Any] | None = None,
    *,
    manifest: Dict[str, Any] | None = None,
    debug_trace: Callable[[str, str, Dict[str, Any]], None] | None = None,
) -> Dict[str, Any]:
    """Identify log types for files in ``file_manifest.json`` and write artifacts.

    This phase reads the file tree and small content samples only. It does not
    run evidence-template regexes and does not make root-cause claims.
    """

    started = time.perf_counter()
    extracted_dir = Path(extracted_dir).resolve()
    artifacts_dir = Path(artifacts_dir).resolve()
    if not extracted_dir.is_dir():
        raise AndroidAnalysisError('extracted_dir_missing', 'Extracted directory does not exist.')
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    manifest = manifest or _read_manifest(artifacts_dir)
    files = manifest.get('files') if isinstance(manifest.get('files'), list) else []
    log_types = _collect_log_types(expert_knowledge_cache)

    if debug_trace:
        debug_trace(
            'identifying_log_types',
            'log_type_identification_input',
            {
                'manifest_file_count': len(files),
                'available_log_type_count': len(log_types),
                'log_type_ids': [item.get('id') for item in log_types],
            },
        )

    output_files: List[Dict[str, Any]] = []
    by_log_type: Dict[str, int] = {}
    skipped_content = 0
    sampled_content = 0
    missing_files = 0
    for item in files:
        rel = str(item.get('path') or '').replace('\\', '/')
        path = (extracted_dir / rel).resolve()
        try:
            path.relative_to(extracted_dir)
        except ValueError as exc:
            raise AndroidAnalysisError('log_type_path_escape', 'Manifest path escapes extracted directory.') from exc
        if not path.is_file():
            missing_files += 1
            output_files.append(_file_result(item, [], missing=True))
            continue

        matches, sample_state = _match_file(path, item, log_types)
        if sample_state == 'sampled':
            sampled_content += 1
        elif sample_state:
            skipped_content += 1
        for match in matches:
            lid = str(match.get('log_type_id') or '')
            if lid:
                by_log_type[lid] = by_log_type.get(lid, 0) + 1
        output_files.append(_file_result(item, matches))

    result = {
        'version': 1,
        'phase': 'log_type_identification',
        'policy': {
            'path_patterns_are_hints': True,
            'content_patterns_use_head_middle_tail_samples': True,
            'unrecognized_files_are_deep_only': True,
            'does_not_run_evidence_templates': True,
        },
        'log_type_count': len(log_types),
        'log_types': [_public_log_type(item) for item in log_types],
        'file_count': len(output_files),
        'matched_file_count': sum(1 for item in output_files if item.get('log_types')),
        'unrecognized_file_count': sum(1 for item in output_files if not item.get('log_types')),
        'files': output_files,
        'stats': {
            'by_log_type': dict(sorted(by_log_type.items())),
            'content_sampled_file_count': sampled_content,
            'content_skipped_file_count': skipped_content,
            'missing_file_count': missing_files,
        },
        'duration_seconds': round(time.perf_counter() - started, 3),
    }
    _write_json(artifacts_dir / 'log_type_manifest.json', result)
    _write_json(
        artifacts_dir / 'log_type_manifest_metrics.json',
        {
            'version': 1,
            'duration_seconds': result['duration_seconds'],
            'log_type_count': result['log_type_count'],
            'file_count': result['file_count'],
            'matched_file_count': result['matched_file_count'],
            'unrecognized_file_count': result['unrecognized_file_count'],
            **result['stats'],
        },
    )
    if debug_trace:
        debug_trace(
            'identifying_log_types',
            'log_type_manifest_result',
            {
                'matched_file_count': result['matched_file_count'],
                'unrecognized_file_count': result['unrecognized_file_count'],
                'by_log_type': result['stats']['by_log_type'],
                'sample_files': [
                    {
                        'path': item.get('path'),
                        'log_types': item.get('log_types') or [],
                        'match_modes': [m.get('match_mode') for m in item.get('matches') or []],
                    }
                    for item in output_files[:30]
                ],
            },
        )
    return result


def _read_manifest(artifacts_dir: Path) -> Dict[str, Any]:
    path = Path(artifacts_dir) / 'file_manifest.json'
    if not path.is_file():
        raise AndroidAnalysisError('manifest_missing', 'file_manifest.json does not exist.')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise AndroidAnalysisError('manifest_invalid', 'file_manifest.json is invalid.') from exc
    if not isinstance(data, dict):
        raise AndroidAnalysisError('manifest_invalid', 'file_manifest.json is invalid.')
    return data


def _collect_log_types(cache: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    for item in DEFAULT_LOG_TYPES:
        _merge_log_type(by_id, item)
    cache = cache or {}
    global_data = cache.get('global') if isinstance(cache.get('global'), dict) else {}
    for item in global_data.get('log_types') or []:
        _merge_log_type(by_id, item)
    module_index = cache.get('module_index') if isinstance(cache.get('module_index'), dict) else {}
    for module_id, loaded in module_index.items():
        if not isinstance(loaded, dict):
            continue
        for item in loaded.get('log_types') or []:
            enriched = dict(item)
            enriched['module_id'] = str(module_id)
            _merge_log_type(by_id, enriched)
    return sorted(by_id.values(), key=lambda item: (-int(item.get('priority') or 0), str(item.get('id') or '')))


def _merge_log_type(by_id: Dict[str, Dict[str, Any]], item: Dict[str, Any]) -> None:
    lid = str(item.get('id') or '').strip()
    if not lid:
        return
    incoming = {
        'id': lid,
        'title': str(item.get('title') or lid).strip(),
        'path_patterns': _list_str(item.get('path_patterns')),
        'content_patterns': _list_str(item.get('content_patterns')),
        'priority': _int_value(item.get('priority'), 50),
        'notes': str(item.get('notes') or '').strip(),
        'source': str(item.get('source') or 'project').strip(),
        'module_ids': _list_str(item.get('module_id')),
    }
    existing = by_id.get(lid)
    if not existing:
        incoming['sources'] = [incoming['source']]
        by_id[lid] = incoming
        return
    existing['title'] = incoming['title'] or existing.get('title') or lid
    existing['priority'] = max(_int_value(existing.get('priority'), 50), incoming['priority'])
    existing['path_patterns'] = _dedupe(list(existing.get('path_patterns') or []) + incoming['path_patterns'])
    existing['content_patterns'] = _dedupe(list(existing.get('content_patterns') or []) + incoming['content_patterns'])
    existing['module_ids'] = _dedupe(list(existing.get('module_ids') or []) + incoming['module_ids'])
    existing['sources'] = _dedupe(list(existing.get('sources') or []) + [incoming['source']])
    if incoming['notes'] and incoming['notes'] not in str(existing.get('notes') or ''):
        existing['notes'] = (str(existing.get('notes') or '') + ' ' + incoming['notes']).strip()


def _match_file(path: Path, manifest_item: Dict[str, Any], log_types: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str]:
    rel = str(manifest_item.get('path') or path.name).replace('\\', '/')
    haystack_path = f"{rel}\n{Path(rel).name}\n{manifest_item.get('kind') or ''}"
    sample_text = ''
    sample_state = ''
    matches: List[Dict[str, Any]] = []
    needs_content = any(item.get('content_patterns') for item in log_types)

    for log_type in log_types:
        path_hits = _pattern_hits(log_type.get('path_patterns') or [], haystack_path)
        content_hits: List[Dict[str, str]] = []
        if log_type.get('content_patterns'):
            if needs_content and not sample_state:
                sample_text, sample_state = _read_content_sample(path)
            if sample_text:
                content_hits = _content_hits(log_type.get('content_patterns') or [], sample_text)
        if not path_hits and not content_hits:
            continue
        confidence, mode = _match_confidence(path_hits, content_hits, bool(log_type.get('content_patterns')))
        matches.append(
            {
                'log_type_id': log_type.get('id'),
                'title': log_type.get('title'),
                'source': log_type.get('source') or ','.join(log_type.get('sources') or []),
                'sources': log_type.get('sources') or [log_type.get('source') or 'project'],
                'module_ids': log_type.get('module_ids') or [],
                'priority': log_type.get('priority'),
                'confidence': confidence,
                'match_mode': mode,
                'path_matches': path_hits,
                'content_matches': content_hits,
                'notes': log_type.get('notes') or '',
            }
        )
    matches.sort(key=lambda item: (-float(item.get('confidence') or 0), -int(item.get('priority') or 0), str(item.get('log_type_id') or '')))
    return matches, sample_state


def _file_result(item: Dict[str, Any], matches: List[Dict[str, Any]], *, missing: bool = False) -> Dict[str, Any]:
    log_type_ids = _dedupe([str(match.get('log_type_id') or '') for match in matches if match.get('log_type_id')])
    result = {
        'path': item.get('path'),
        'name': item.get('name'),
        'size': item.get('size'),
        'kind': item.get('kind'),
        'log_types': log_type_ids,
        'matches': matches,
        'unrecognized': not bool(log_type_ids),
        'deep_only': not bool(log_type_ids),
    }
    if missing:
        result['missing'] = True
        result['deep_only_reason'] = 'manifest_file_missing'
    elif not log_type_ids:
        result['deep_only_reason'] = 'no_log_type_matched'
    return result


def _match_confidence(path_hits: List[Dict[str, str]], content_hits: List[Dict[str, str]], has_content_patterns: bool) -> Tuple[float, str]:
    if path_hits and content_hits:
        return 0.95, 'path_and_content'
    if content_hits:
        return 0.85, 'content'
    if path_hits and has_content_patterns:
        return 0.65, 'path_only_unconfirmed'
    return 0.75, 'path'


def _pattern_hits(patterns: Iterable[str], text: str) -> List[Dict[str, str]]:
    hits: List[Dict[str, str]] = []
    for pattern in patterns:
        match = _safe_search(pattern, text)
        if match:
            hits.append({'pattern': pattern, 'match': match.group(0)[:160]})
    return hits


def _content_hits(patterns: Iterable[str], text: str) -> List[Dict[str, str]]:
    hits: List[Dict[str, str]] = []
    for pattern in patterns:
        match = _safe_search(pattern, text)
        if match:
            hits.append({'pattern': pattern, 'snippet': _snippet(text, match.start(), match.end())})
    return hits


def _safe_search(pattern: str, text: str) -> re.Match[str] | None:
    try:
        return re.search(pattern, text, re.MULTILINE)
    except re.error:
        return None


def _read_content_sample(path: Path) -> Tuple[str, str]:
    try:
        if path.suffix.lower() == '.gz':
            with gzip.open(path, 'rb') as f:
                raw = f.read(192 * 1024)
            return _decode_sample(raw), 'sampled'
        size = path.stat().st_size
        with open(path, 'rb') as f:
            head = f.read(min(80 * 1024, size))
            if _looks_binary(head):
                return '', 'binary'
            if size <= 192 * 1024:
                raw = head + f.read()
            else:
                f.seek(max(0, size // 2 - 24 * 1024))
                middle = f.read(48 * 1024)
                f.seek(max(0, size - 80 * 1024))
                tail = f.read(80 * 1024)
                raw = head + b'\n\n...[middle sample]...\n\n' + middle + b'\n\n...[tail sample]...\n\n' + tail
    except OSError:
        return '', 'read_error'
    if _looks_binary(raw):
        return '', 'binary'
    return _decode_sample(raw), 'sampled'


def _decode_sample(raw: bytes) -> str:
    if raw.startswith(b'\xff\xfe') or raw.startswith(b'\xfe\xff'):
        return raw.decode('utf-16', errors='replace')
    if _looks_like_utf16_text(raw):
        even_nulls = raw[:2048][0::2].count(0)
        odd_nulls = raw[:2048][1::2].count(0)
        encoding = 'utf-16-be' if even_nulls > odd_nulls else 'utf-16-le'
        return raw.decode(encoding, errors='replace')
    return raw.decode('utf-8', errors='replace')


def _looks_binary(raw: bytes) -> bool:
    if not raw:
        return False
    sample = raw[:4096]
    if b'\x00' not in sample:
        return False
    return not _looks_like_utf16_text(sample)


def _looks_like_utf16_text(raw: bytes) -> bool:
    if not raw:
        return False
    sample = raw[:2048]
    even_nulls = sample[0::2].count(0)
    odd_nulls = sample[1::2].count(0)
    pair_count = max(1, len(sample) // 2)
    if max(even_nulls, odd_nulls) / pair_count < 0.25:
        return False
    try:
        encoding = 'utf-16-be' if even_nulls > odd_nulls else 'utf-16-le'
        decoded = sample.decode(encoding, errors='strict')
    except UnicodeError:
        return False
    printable = sum(1 for ch in decoded if ch.isprintable() or ch in '\r\n\t')
    return printable / max(1, len(decoded)) > 0.8


def _snippet(text: str, start: int, end: int, radius: int = 90) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    return re.sub(r'\s+', ' ', text[left:right]).strip()[:240]


def _public_log_type(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'id': item.get('id'),
        'title': item.get('title'),
        'priority': item.get('priority'),
        'source': item.get('source'),
        'sources': item.get('sources') or [item.get('source') or 'project'],
        'module_ids': item.get('module_ids') or [],
        'path_pattern_count': len(item.get('path_patterns') or []),
        'content_pattern_count': len(item.get('content_patterns') or []),
        'notes': item.get('notes') or '',
    }


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _list_str(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in re.split(r'[;,]\s*|\n+', value) if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _dedupe(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        text = str(value or '').strip()
        if text and text not in out:
            out.append(text)
    return out
