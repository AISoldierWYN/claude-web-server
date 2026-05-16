"""Match project-local XML/SP state templates against extracted log archives."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Dict, List


_SEVERITY_SCORE = {
    'critical': 90,
    'high': 75,
    'warning': 60,
    'medium': 55,
    'low': 35,
    'info': 20,
}


def run_xml_state_matching(
    extracted_dir: Path,
    artifacts_dir: Path,
    expert_knowledge_cache: Dict[str, Any] | None,
    planner_result: Dict[str, Any] | None = None,
    *,
    debug_trace: Callable[[str, str, Dict[str, Any]], None] | None = None,
) -> Dict[str, Any]:
    """Match XML/SP state templates and append observations to matched_rules.json."""

    extracted_dir = Path(extracted_dir).resolve()
    artifacts_dir = Path(artifacts_dir).resolve()
    cache = expert_knowledge_cache or {}
    planner_result = planner_result or _read_json(artifacts_dir / 'planner_result.json', default={})
    manifest = _read_json(artifacts_dir / 'file_manifest.json', default={})
    selected_modules = _select_modules(cache, planner_result)
    events: List[Dict[str, Any]] = []
    stats = {
        'module_count': len(selected_modules),
        'template_count': 0,
        'xml_file_count': 0,
        'xml_like_file_count': 0,
        'path_fallback_match_count': 0,
        'matched_event_count': 0,
        'parse_error_count': 0,
    }

    files = list(manifest.get('files') or [])
    xml_files = [
        f for f in files
        if _is_xml_like_file((extracted_dir / str(f.get('path') or '')).resolve(), extracted_dir, f)
    ]
    stats['xml_file_count'] = len(xml_files)
    stats['xml_like_file_count'] = len(xml_files)
    for module in selected_modules:
        templates = [t for t in module.get('xml_state_templates') or [] if t.get('enabled', True) is not False]
        stats['template_count'] += len(templates)
        if not templates:
            continue
        for file_item in xml_files:
            rel = str(file_item.get('path') or '').replace('\\', '/')
            matched_templates = [t for t in templates if _path_matches(rel, t.get('path_patterns') or [])]
            path = (extracted_dir / rel).resolve()
            try:
                path.relative_to(extracted_dir)
            except ValueError:
                continue
            states, parse_error = _extract_xml_state_values(path)
            if parse_error:
                stats['parse_error_count'] += 1
            if not states:
                continue
            path_match_mode = 'path_pattern'
            for template in matched_templates:
                events.extend(_match_template(module, template, rel, states, path_match_mode=path_match_mode))

            if matched_templates:
                continue

            # 用户手动改名、压缩工具改后缀、或日志系统把 XML 包成 .txt 时，路径正则会失效。
            # 这里只在“内容已经确认像 XML”后按严格 key/value 正则兜底，命中会降低分数并记录 fallback。
            fallback_events = []
            for template in templates:
                fallback_events.extend(_match_template(module, template, rel, states, path_match_mode='content_fallback'))
            stats['path_fallback_match_count'] += len(fallback_events)
            events.extend(fallback_events)

    existing = _read_json(artifacts_dir / 'matched_rules.json', default={'version': 1, 'events': [], 'event_count': 0})
    original_events = list(existing.get('events') or [])
    merged_events = _sort_events(original_events + events)
    existing['events'] = merged_events
    existing['event_count'] = len(merged_events)
    existing['xml_state_event_count'] = len(events)
    existing['xml_state_template_count'] = stats['template_count']
    if 'rule_pack_count' not in existing:
        existing['rule_pack_count'] = 0
    (artifacts_dir / 'matched_rules.json').write_text(json.dumps(existing, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    stats['matched_event_count'] = len(events)
    result = {'version': 1, 'stats': stats, 'events': events}
    (artifacts_dir / 'xml_state_matches.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if debug_trace:
        debug_trace(
            'matching_xml_state',
            'xml_state_matching_result',
            {
                **stats,
                'top_events': events[:8],
            },
        )
    return result


def _select_modules(cache: Dict[str, Any], planner_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    module_index = cache.get('module_index') or {}
    wanted = set(str(x) for x in (planner_result.get('candidate_bundle_ids') or []) if str(x).strip())
    if not wanted:
        return list(module_index.values())
    selected = []
    for module in module_index.values():
        info = module.get('module') or {}
        ids = {str(info.get('id') or ''), str(module.get('bundle_id') or '')}
        if ids & wanted:
            selected.append(module)
    return selected


def _path_matches(path: str, patterns: List[str]) -> bool:
    if not patterns:
        return False
    for pattern in patterns:
        if not str(pattern).strip():
            continue
        try:
            if re.search(pattern, path):
                return True
        except re.error:
            continue
    return False


def _is_xml_like_file(path: Path, extracted_dir: Path, file_item: Dict[str, Any]) -> bool:
    rel = str(file_item.get('path') or '').lower()
    kind = str(file_item.get('kind') or '').lower()
    if rel.endswith('.xml') or 'xml' in kind:
        return True
    try:
        path.relative_to(extracted_dir)
    except ValueError:
        return False
    if not path.is_file():
        return False
    try:
        head = path.read_bytes()[:4096]
    except OSError:
        return False
    text = ''
    for encoding in ('utf-8-sig', 'utf-16', 'utf-16-le', 'gb18030'):
        try:
            text = head.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        text = head.decode('utf-8', errors='ignore')
    stripped = text.lstrip().lower()
    return (
        stripped.startswith('<?xml')
        or stripped.startswith('<map')
        or '<string name=' in stripped
        or '<boolean name=' in stripped
        or '<int name=' in stripped
        or '<long name=' in stripped
    )


def _extract_xml_state_values(path: Path) -> tuple[List[Dict[str, str]], bool]:
    text = _read_text(path)
    if not text.strip():
        return [], False
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return _extract_xml_state_values_by_regex(text), True
    out: List[Dict[str, str]] = []
    for elem in root.iter():
        key = elem.attrib.get('name') or elem.attrib.get('key')
        if not key:
            continue
        value = elem.attrib.get('value')
        if value is None:
            value = (elem.text or '').strip()
        out.append(
            {
                'key': str(key),
                'value': str(value or ''),
                'tag': str(elem.tag),
            }
        )
    return out, False


def _extract_xml_state_values_by_regex(text: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    pattern = re.compile(
        r'<(?P<tag>\w+)\b[^>]*\bname="(?P<key>[^"]+)"[^>]*(?:\bvalue="(?P<value>[^"]*)")?[^>]*>(?P<text>[^<]*)',
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        value = match.group('value')
        if value is None:
            value = (match.group('text') or '').strip()
        out.append({'key': match.group('key'), 'value': value or '', 'tag': match.group('tag')})
    return out


def _match_template(
    module: Dict[str, Any],
    template: Dict[str, Any],
    rel_path: str,
    states: List[Dict[str, str]],
    *,
    path_match_mode: str,
) -> List[Dict[str, Any]]:
    try:
        key_re = re.compile(str(template.get('key_regex') or ''))
    except re.error:
        return []
    value_regex = str(template.get('value_regex') or '').strip()
    try:
        value_re = re.compile(value_regex) if value_regex else None
    except re.error:
        value_re = None
    events: List[Dict[str, Any]] = []
    module_info = module.get('module') or {}
    for state in states:
        key = str(state.get('key') or '')
        value = str(state.get('value') or '')
        if not key_re.search(key):
            continue
        if value_re and not value_re.search(value):
            continue
        severity = str(template.get('severity') or 'info').lower()
        score = _SEVERITY_SCORE.get(severity, 20)
        reasons = [
            'xml_state_template',
            f"subcategory:{template.get('submodule_id') or template.get('subcategory_id') or ''}",
            f'path_match:{path_match_mode}',
        ]
        if path_match_mode == 'content_fallback':
            score = max(1, score - 10)
        title = f"XML state: {key}"
        events.append(
            {
                'id': f"xml-state::{template.get('id')}::{rel_path}::{key}",
                'rule_id': template.get('id'),
                'rule_title': title,
                'issue_type': template.get('profile') or 'functional',
                'severity': severity,
                'source_bundle_ids': [x for x in [module_info.get('id'), module.get('bundle_id')] if x],
                'source_rule_pack_id': 'project-xml-state',
                'path': rel_path,
                'kind': template.get('source_type') or 'shared_prefs_xml',
                'line_range': [],
                'matched_terms': [key, value],
                'regex_hits': [template.get('key_regex'), template.get('value_regex')],
                'snippet': f'{key}={value}',
                'relevance': {
                    'score': score,
                    'reasons': reasons,
                },
                'source_type': 'xml_state_template',
                'path_match_mode': path_match_mode,
                'submodule_id': template.get('submodule_id') or template.get('subcategory_id') or '',
                'template_meaning': template.get('meaning') or '',
                'code_location': template.get('code_location') or '',
                'next_steps': template.get('next_steps') or [],
                'deep_hints': {
                    'exact_logs': [f'{key}={value}'],
                    'code_search_terms': [key, str(template.get('code_location') or '')],
                    'search_order': template.get('next_steps') or [],
                },
            }
        )
    return events


def _sort_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        events,
        key=lambda e: (
            -int((e.get('relevance') or {}).get('score') or 0),
            str(e.get('path') or ''),
            str(e.get('rule_id') or ''),
        ),
    )


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ('utf-8-sig', 'utf-16', 'utf-16-le', 'gb18030'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace')


def _read_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.is_file():
        return dict(default)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return dict(default)
    return data if isinstance(data, dict) else dict(default)
