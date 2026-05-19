"""Single-line evidence template matching for Android expert workbench Phase 8."""

from __future__ import annotations

import gzip
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Tuple

from .models import AndroidAnalysisError


MAX_HITS_PER_TEMPLATE = 20
MAX_HITS_PER_FILE_TEMPLATE = 5
MAX_SCAN_BYTES_PER_FILE = 128 * 1024 * 1024

_SEVERITY_SCORE = {
    'critical': 0.95,
    'fatal': 0.95,
    'high': 0.86,
    'warning': 0.72,
    'medium': 0.62,
    'suspicious': 0.58,
    'low': 0.42,
    'info': 0.25,
}


def run_evidence_template_matching(
    extracted_dir: Path,
    artifacts_dir: Path,
    *,
    debug_trace: Callable[[str, str, Dict[str, Any]], None] | None = None,
) -> Dict[str, Any]:
    """Search selected single-line templates in files identified by log type."""

    started = time.perf_counter()
    extracted_dir = Path(extracted_dir).resolve()
    artifacts_dir = Path(artifacts_dir).resolve()
    if not extracted_dir.is_dir():
        raise AndroidAnalysisError('extracted_dir_missing', 'Extracted directory does not exist.')
    selected = _read_json(artifacts_dir / 'selected_evidence_templates.json')
    log_manifest = _read_json(artifacts_dir / 'log_type_manifest.json')
    templates = [t for t in selected.get('templates') or [] if isinstance(t, dict)]
    files_by_log_type = _files_by_log_type(log_manifest)

    if debug_trace:
        debug_trace(
            'searching_evidence_templates',
            'single_line_evidence_search_input',
            {
                'template_count': len(templates),
                'log_type_count': len(files_by_log_type),
                'available_log_types': sorted(files_by_log_type.keys()),
            },
        )

    events: List[Dict[str, Any]] = []
    timeline_events: List[Dict[str, Any]] = []
    skipped_templates: List[Dict[str, Any]] = []
    stats = {
        'template_count': len(templates),
        'searchable_template_count': 0,
        'skipped_template_count': 0,
        'file_template_search_count': 0,
        'searched_file_count': 0,
        'matched_event_count': 0,
        'invalid_regex_count': 0,
        'missing_log_type_file_count': 0,
        'truncated_file_count': 0,
        'read_error_count': 0,
    }
    searched_files: set[str] = set()
    truncated_files: set[str] = set()
    read_error_files: set[str] = set()

    for template in templates:
        skip_reason = _template_skip_reason(template)
        if skip_reason:
            stats['skipped_template_count'] += 1
            skipped_templates.append(_skipped_template(template, skip_reason))
            continue
        pattern = str(template.get('expanded_regex') or template.get('regex') or '').strip()
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            stats['skipped_template_count'] += 1
            stats['invalid_regex_count'] += 1
            skipped_templates.append(_skipped_template(template, f'invalid_regex: {exc}'))
            continue
        log_type = str(template.get('log_type') or '').strip()
        candidate_files = files_by_log_type.get(log_type) or []
        if not candidate_files:
            stats['skipped_template_count'] += 1
            stats['missing_log_type_file_count'] += 1
            skipped_templates.append(_skipped_template(template, f'no_files_for_log_type:{log_type}'))
            continue
        stats['searchable_template_count'] += 1
        template_hits = 0
        for file_item in candidate_files:
            if template_hits >= MAX_HITS_PER_TEMPLATE:
                break
            rel_path = str(file_item.get('path') or '').replace('\\', '/')
            path = (extracted_dir / rel_path).resolve()
            try:
                path.relative_to(extracted_dir)
            except ValueError:
                continue
            if not path.is_file():
                continue
            stats['file_template_search_count'] += 1
            searched_files.add(rel_path)
            per_file_hits = 0
            for line_number, line, state in _iter_text_lines(path):
                if state == 'truncated':
                    truncated_files.add(rel_path)
                    break
                if state == 'read_error':
                    read_error_files.add(rel_path)
                    break
                match = regex.search(line)
                if not match:
                    continue
                event = _make_event(template, file_item, rel_path, line_number, line, match)
                events.append(event)
                timeline_events.append(_timeline_event(event))
                template_hits += 1
                per_file_hits += 1
                if template_hits >= MAX_HITS_PER_TEMPLATE or per_file_hits >= MAX_HITS_PER_FILE_TEMPLATE:
                    break
    stats['searched_file_count'] = len(searched_files)
    stats['matched_event_count'] = len(events)
    stats['truncated_file_count'] = len(truncated_files)
    stats['read_error_count'] = len(read_error_files)
    timeline_events.sort(key=_timeline_sort_key)
    result = {
        'version': 1,
        'phase': 'single_line_evidence_search',
        'policy': {
            'uses_selected_evidence_templates': True,
            'uses_log_type_manifest': True,
            'single_line_only': True,
            'max_hits_per_template': MAX_HITS_PER_TEMPLATE,
            'max_hits_per_file_template': MAX_HITS_PER_FILE_TEMPLATE,
            'max_scan_bytes_per_file': MAX_SCAN_BYTES_PER_FILE,
        },
        'stats': stats,
        'events': timeline_events,
        'skipped_templates': skipped_templates,
        'duration_seconds': round(time.perf_counter() - started, 3),
    }
    _write_json(artifacts_dir / 'annotated_evidence_timeline.json', result)
    (artifacts_dir / 'annotated_evidence_timeline.md').write_text(_render_timeline_markdown(result), encoding='utf-8')
    _merge_into_matched_rules(artifacts_dir, events)

    if debug_trace:
        debug_trace(
            'searching_evidence_templates',
            'single_line_evidence_search_result',
            {
                **stats,
                'top_events': timeline_events[:20],
                'skipped_templates': skipped_templates[:20],
            },
        )
    return result


def _template_skip_reason(template: Dict[str, Any]) -> str:
    if template.get('enabled', True) is False:
        return 'template_disabled'
    if template.get('search_enabled') is False:
        return 'search_disabled'
    if str(template.get('status') or 'ready') != 'ready':
        return f"status:{template.get('status')}"
    if not str(template.get('log_type') or '').strip():
        return 'missing_log_type'
    if not str(template.get('expanded_regex') or template.get('regex') or '').strip():
        return 'missing_regex'
    return ''


def _skipped_template(template: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {
        'id': template.get('id'),
        'module_id': template.get('module_id'),
        'submodule_id': template.get('submodule_id'),
        'log_type': template.get('log_type'),
        'status': template.get('status'),
        'reason': reason,
    }


def _files_by_log_type(log_manifest: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for item in log_manifest.get('files') or []:
        if not isinstance(item, dict):
            continue
        for log_type in item.get('log_types') or []:
            lid = str(log_type or '').strip()
            if lid:
                out.setdefault(lid, []).append(item)
    return out


def _iter_text_lines(path: Path) -> Iterator[Tuple[int, str, str]]:
    bytes_seen = 0
    try:
        with _open_text(path) as f:
            for line_number, line in enumerate(f, start=1):
                bytes_seen += len(line.encode('utf-8', errors='ignore'))
                if bytes_seen > MAX_SCAN_BYTES_PER_FILE:
                    yield line_number, '', 'truncated'
                    return
                yield line_number, line.rstrip('\r\n'), 'line'
    except OSError:
        yield 0, '', 'read_error'


def _open_text(path: Path):
    if path.suffix.lower() == '.gz':
        return gzip.open(path, 'rt', encoding='utf-8', errors='replace')
    encoding = _detect_text_encoding(path)
    return open(path, 'r', encoding=encoding, errors='replace')


def _detect_text_encoding(path: Path) -> str:
    try:
        with open(path, 'rb') as f:
            chunk = f.read(4096)
    except OSError:
        return 'utf-8'
    if chunk.startswith(b'\xff\xfe') or chunk.startswith(b'\xfe\xff'):
        return 'utf-16'
    if _looks_like_utf16_text(chunk):
        even_nulls = chunk[0::2].count(0)
        odd_nulls = chunk[1::2].count(0)
        return 'utf-16-be' if even_nulls > odd_nulls else 'utf-16-le'
    return 'utf-8'


def _looks_like_utf16_text(chunk: bytes) -> bool:
    if not chunk:
        return False
    sample = chunk[:1024]
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


def _make_event(
    template: Dict[str, Any],
    file_item: Dict[str, Any],
    rel_path: str,
    line_number: int,
    line: str,
    match: re.Match[str],
) -> Dict[str, Any]:
    severity = str(template.get('severity') or 'info').lower()
    matched_text = match.group(0)[:240]
    timestamp = _extract_timestamp(line)
    score = _SEVERITY_SCORE.get(severity, 0.25)
    reasons = [
        'single_line_evidence_template',
        f"log_type:{template.get('log_type')}",
        f"template:{template.get('id')}",
    ]
    if template.get('time_anchor'):
        score = min(1.0, score + 0.03)
        reasons.append('time_anchor')
    return {
        'id': f"evidence-template::{template.get('id')}::{rel_path}:{line_number}",
        'rule_id': template.get('id'),
        'rule_title': template.get('meaning') or template.get('id'),
        'issue_type': template.get('profile') or 'functional',
        'severity': severity,
        'source_bundle_ids': _dedupe([template.get('module_id')]),
        'source_rule_pack_id': 'project-evidence-template',
        'path': rel_path,
        'kind': file_item.get('kind') or '',
        'line_range': [line_number, line_number],
        'sample_type': 'single_line_full_log_search',
        'matched_terms': _dedupe([matched_text, template.get('id'), template.get('log_type')]),
        'regex_hits': [template.get('expanded_regex') or template.get('regex')],
        'snippet': _trim_line(line),
        'relevance': {'score': round(score, 3), 'reasons': reasons},
        'source_type': 'evidence_template',
        'template_id': template.get('id'),
        'template_meaning': template.get('meaning') or '',
        'code_location': template.get('code_location') or '',
        'next_steps': template.get('next_steps') or [],
        'log_type': template.get('log_type') or '',
        'module_id': template.get('module_id') or '',
        'submodule_id': template.get('submodule_id') or '',
        'profile': template.get('profile') or '',
        'time_anchor': bool(template.get('time_anchor')),
        'timestamp': timestamp,
        'match_text': matched_text,
        'match_span': [match.start(), match.end()],
        'deep_hints': {
            'exact_logs': [
                {
                    'tag': str(template.get('id') or ''),
                    'message': _trim_line(line, 500),
                    'path': f'{rel_path}:{line_number}',
                    'logger': str(template.get('log_type') or 'evidence_template'),
                    'line': str(line_number),
                }
            ],
            'code_search_terms': _dedupe([template.get('code_location'), template.get('id'), matched_text])[:8],
            'search_order': template.get('next_steps') or [],
        },
    }


def _timeline_event(event: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'id': event.get('id'),
        'template_id': event.get('template_id'),
        'module_id': event.get('module_id'),
        'submodule_id': event.get('submodule_id'),
        'profile': event.get('profile'),
        'log_type': event.get('log_type'),
        'severity': event.get('severity'),
        'time_anchor': event.get('time_anchor'),
        'timestamp': event.get('timestamp'),
        'path': event.get('path'),
        'line_number': (event.get('line_range') or [None])[0],
        'match_text': event.get('match_text'),
        'line': event.get('snippet'),
        'meaning': event.get('template_meaning'),
        'code_location': event.get('code_location'),
        'next_steps': event.get('next_steps') or [],
        'relevance': event.get('relevance') or {},
    }


def _extract_timestamp(line: str) -> str:
    patterns = [
        r'\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?\b',
        r'\b\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?\b',
        r'\[\s*\d+(?:\.\d+)?\]',
    ]
    for pattern in patterns:
        match = re.search(pattern, line)
        if match:
            return match.group(0).strip()
    return ''


def _timeline_sort_key(event: Dict[str, Any]) -> Tuple[int, str, str, int, str]:
    timestamp = str(event.get('timestamp') or '')
    line_number = int(event.get('line_number') or 0)
    return (0 if timestamp else 1, timestamp, str(event.get('path') or ''), line_number, str(event.get('template_id') or ''))


def _merge_into_matched_rules(artifacts_dir: Path, events: List[Dict[str, Any]]) -> None:
    path = Path(artifacts_dir) / 'matched_rules.json'
    matched = _read_json_optional(path, default={'version': 1, 'rule_pack_count': 0, 'events': [], 'event_count': 0})
    existing = list(matched.get('events') or [])
    seen = {str(item.get('id') or '') for item in existing if isinstance(item, dict)}
    merged = existing[:]
    for event in events:
        if event.get('id') and event.get('id') in seen:
            continue
        merged.append(event)
    merged.sort(key=lambda e: (-float((e.get('relevance') or {}).get('score') or 0), str(e.get('path') or ''), str(e.get('rule_id') or '')))
    matched['events'] = merged
    matched['event_count'] = len(merged)
    matched['evidence_template_event_count'] = len(events)
    matched['evidence_template_source'] = 'annotated_evidence_timeline.json'
    if 'rule_pack_count' not in matched:
        matched['rule_pack_count'] = 0
    _write_json(path, matched)


def _render_timeline_markdown(result: Dict[str, Any]) -> str:
    stats = result.get('stats') or {}
    events = result.get('events') or []
    lines = [
        '# Annotated Evidence Timeline',
        '',
        '## Summary',
        f"- Templates: {stats.get('template_count', 0)}",
        f"- Searchable templates: {stats.get('searchable_template_count', 0)}",
        f"- Matched events: {stats.get('matched_event_count', 0)}",
        f"- Searched files: {stats.get('searched_file_count', 0)}",
        '',
    ]
    if not events:
        lines.extend(
            [
                '## Confirmed Single-Line Evidence',
                'No selected single-line evidence templates matched the typed log files.',
                '',
            ]
        )
    else:
        lines.append('## Confirmed Single-Line Evidence')
        for idx, event in enumerate(events, start=1):
            loc = f"{event.get('path')}:{event.get('line_number')}"
            title = event.get('meaning') or event.get('template_id')
            prefix = f"{event.get('timestamp')} " if event.get('timestamp') else ''
            lines.append(f"### {idx}. {prefix}{title}")
            lines.append(f"- Template: {event.get('template_id')}")
            lines.append(f"- Severity: {event.get('severity')}")
            lines.append(f"- Log type: {event.get('log_type')}")
            lines.append(f"- Location: {loc}")
            if event.get('code_location'):
                lines.append(f"- Code location: {event.get('code_location')}")
            if event.get('next_steps'):
                lines.append(f"- Next steps: {'; '.join(event.get('next_steps') or [])}")
            lines.append('')
            lines.append('```text')
            lines.append(str(event.get('line') or '').strip())
            lines.append('```')
            lines.append('')
    skipped = result.get('skipped_templates') or []
    if skipped:
        lines.append('## Skipped Templates')
        for item in skipped[:30]:
            lines.append(f"- `{item.get('id')}` ({item.get('log_type') or 'no-log-type'}): {item.get('reason')}")
        lines.append('')
    lines.append('## Boundary')
    lines.append('Only single-line, log-type-scoped evidence templates are searched here. Multi-line stack traces, ANR/tombstone reasoning, and cross-file causality remain Deep analysis work.')
    return '\n'.join(lines).rstrip() + '\n'


def _trim_line(line: str, max_chars: int = 2000) -> str:
    text = (line or '').strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + '...'


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise AndroidAnalysisError('evidence_template_artifact_missing', f'{path.name} does not exist.')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise AndroidAnalysisError('evidence_template_artifact_invalid', f'{path.name} is invalid JSON.') from exc
    if not isinstance(data, dict):
        raise AndroidAnalysisError('evidence_template_artifact_invalid', f'{path.name} must be a JSON object.')
    return data


def _read_json_optional(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else default
    except (OSError, json.JSONDecodeError):
        return dict(default)
    return data if isinstance(data, dict) else dict(default)


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _dedupe(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        text = str(value or '').strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out
