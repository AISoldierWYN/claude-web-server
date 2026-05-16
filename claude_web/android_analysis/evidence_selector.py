"""Evidence-template selection for the Android expert workbench.

Phase 5 connects question classification and parameter resolution to the later
log-search phases. It deliberately does not read uploaded log files. The output
is a small, explainable set of templates that later stages may search against
typed log files.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


SUBMODULE_CONFIDENCE_THRESHOLD = 0.6
TOP_CANDIDATE_GAP_THRESHOLD = 0.15
MODULE_CANDIDATE_THRESHOLD = 0.3
PLACEHOLDER_RE = re.compile(r'\$([A-Za-z_][A-Za-z0-9_]*)')


def run_evidence_template_selection(
    artifacts_dir: Path,
    expert_knowledge_cache: Dict[str, Any] | None,
    *,
    classification: Dict[str, Any] | None = None,
    parameter_resolution: Dict[str, Any] | None = None,
    debug_trace=None,
) -> Dict[str, Any]:
    """Select evidence templates and write Phase 5 artifacts.

    The selector uses only cached expert knowledge, ``classification_result``,
    and ``parameter_resolution``. It never opens extracted logs or source files.
    """

    artifacts_dir = Path(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    classification = classification or _read_json(artifacts_dir / 'classification_result.json')
    parameter_resolution = parameter_resolution or _read_json(artifacts_dir / 'parameter_resolution.json')
    cache = expert_knowledge_cache or {}
    module_index = cache.get('module_index') if isinstance(cache.get('module_index'), dict) else {}
    parameter_values = _parameter_values(parameter_resolution)
    module_selections = _select_modules(classification, module_index)

    if debug_trace:
        debug_trace(
            'selecting_evidence_templates',
            'evidence_template_selection_input',
            {
                'classification': _classification_summary(classification),
                'parameter_resolution': _parameter_summary(parameter_resolution),
                'module_selection_count': len(module_selections),
                'available_module_count': len(module_index),
            },
        )

    templates: List[Dict[str, Any]] = []
    xml_state_templates: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    seen_template_keys: set[Tuple[str, str]] = set()
    seen_xml_keys: set[Tuple[str, str]] = set()

    for selection in module_selections:
        loaded = module_index.get(selection['module_id']) or {}
        for template in loaded.get('evidence_templates') or []:
            selected, reasons = _template_selected(template, selection, classification, template_kind='evidence')
            if not selected:
                continue
            key = (str(template.get('module_id') or selection['module_id']), str(template.get('id') or ''))
            if key in seen_template_keys:
                continue
            seen_template_keys.add(key)
            templates.append(
                _build_selected_template(
                    template,
                    parameter_values,
                    reasons,
                    warnings,
                    pattern_fields=('regex',),
                    expanded_field='expanded_regex',
                    template_kind='evidence',
                )
            )
        for template in loaded.get('xml_state_templates') or []:
            selected, reasons = _template_selected(template, selection, classification, template_kind='xml_state')
            if not selected:
                continue
            key = (str(template.get('module_id') or selection['module_id']), str(template.get('id') or ''))
            if key in seen_xml_keys:
                continue
            seen_xml_keys.add(key)
            xml_state_templates.append(
                _build_selected_template(
                    template,
                    parameter_values,
                    reasons,
                    warnings,
                    pattern_fields=('path_patterns', 'key_regex', 'value_regex'),
                    expanded_field='expanded_patterns',
                    template_kind='xml_state',
                )
            )

    experience_hints = _select_experience_hints(cache, module_selections)
    result: Dict[str, Any] = {
        'schema_version': 1,
        'selection_policy': {
            'submodule_confidence_threshold': SUBMODULE_CONFIDENCE_THRESHOLD,
            'top_candidate_gap_threshold': TOP_CANDIDATE_GAP_THRESHOLD,
            'module_candidate_threshold': MODULE_CANDIDATE_THRESHOLD,
            'unresolved_placeholder_behavior': 'mark_needs_parameters_and_do_not_search',
            'reads_logs': False,
        },
        'classification': _classification_summary(classification),
        'parameter_resolution': _parameter_summary(parameter_resolution),
        'module_selections': module_selections,
        'templates': templates,
        'xml_state_templates': xml_state_templates,
        'experience_hints': experience_hints,
        'counts': _counts(templates, xml_state_templates, experience_hints),
        'warnings': warnings,
        'duration_seconds': round(time.perf_counter() - started, 3),
    }
    _write_json(artifacts_dir / 'selected_evidence_templates.json', result)
    _write_json(
        artifacts_dir / 'selected_evidence_templates_metrics.json',
        {
            'version': 1,
            'duration_seconds': result['duration_seconds'],
            **result['counts'],
            'warning_count': len(warnings),
            'module_selection_count': len(module_selections),
        },
    )
    if debug_trace:
        debug_trace(
            'selecting_evidence_templates',
            'evidence_template_selection_result',
            {
                'module_selections': module_selections,
                'counts': result['counts'],
                'warnings': warnings[:20],
                'selected_template_ids': [item.get('id') for item in templates[:50]],
                'selected_xml_template_ids': [item.get('id') for item in xml_state_templates[:50]],
            },
        )
    return result


def _select_modules(classification: Dict[str, Any], module_index: Dict[str, Any]) -> List[Dict[str, Any]]:
    selections: List[Dict[str, Any]] = []
    primary_module_id = str(classification.get('module_id') or '').strip()
    primary_score = _confidence(classification.get('module_confidence'))
    if primary_module_id in module_index and primary_score >= 0.5:
        selections.append(
            _module_selection(
                primary_module_id,
                classification.get('submodule_id'),
                _confidence(classification.get('submodule_confidence')),
                primary_score,
                'primary_classification',
                '分类器主结果命中该模块。',
                classification,
            )
        )
    for candidate in classification.get('top_candidates') or []:
        if not isinstance(candidate, dict):
            continue
        module_id = str(candidate.get('module_id') or '').strip()
        if module_id not in module_index:
            continue
        score = _confidence(candidate.get('score'))
        if score < MODULE_CANDIDATE_THRESHOLD:
            continue
        if any(item['module_id'] == module_id for item in selections):
            continue
        selections.append(
            _module_selection(
                module_id,
                candidate.get('submodule_id'),
                score,
                score,
                'top_candidate',
                str(candidate.get('reason') or '分类器候选模块达到加载阈值。')[:300],
                classification,
            )
        )
    return selections


def _module_selection(
    module_id: str,
    submodule_id: Any,
    submodule_confidence: float,
    module_score: float,
    source: str,
    reason: str,
    classification: Dict[str, Any],
) -> Dict[str, Any]:
    submodule_id = str(submodule_id or 'unknown').strip() or 'unknown'
    specific = _specific_submodule_allowed(module_id, submodule_id, submodule_confidence, classification)
    return {
        'module_id': module_id,
        'module_score': round(module_score, 3),
        'source': source,
        'reason': reason,
        'submodule_policy': 'specific' if specific else 'module_all',
        'selected_submodule_ids': [submodule_id] if specific else [],
        'profile': str(classification.get('profile') or 'unknown'),
    }


def _specific_submodule_allowed(
    module_id: str,
    submodule_id: str,
    submodule_confidence: float,
    classification: Dict[str, Any],
) -> bool:
    if not submodule_id or submodule_id == 'unknown' or submodule_confidence < SUBMODULE_CONFIDENCE_THRESHOLD:
        return False
    candidates = [
        item
        for item in classification.get('top_candidates') or []
        if isinstance(item, dict) and str(item.get('module_id') or '') == module_id
    ]
    scores = sorted([_confidence(item.get('score')) for item in candidates], reverse=True)
    if len(scores) >= 2 and scores[0] - scores[1] < TOP_CANDIDATE_GAP_THRESHOLD:
        return False
    return True


def _template_selected(
    template: Dict[str, Any],
    selection: Dict[str, Any],
    classification: Dict[str, Any],
    *,
    template_kind: str,
) -> Tuple[bool, List[str]]:
    if template.get('enabled', True) is False:
        return False, []
    template_module = str(template.get('module_id') or selection.get('module_id') or '')
    if template_module != selection.get('module_id'):
        return False, []
    template_sub = str(template.get('submodule_id') or '').strip()
    template_profile = str(template.get('profile') or 'unknown').strip() or 'unknown'
    profile = str(classification.get('profile') or 'unknown').strip() or 'unknown'
    reasons = [
        f"module:{selection.get('module_id')} selected by {selection.get('source')}",
        f"kind:{template_kind}",
    ]
    if selection.get('submodule_policy') == 'specific':
        selected_submodules = set(selection.get('selected_submodule_ids') or [])
        if template_sub in selected_submodules:
            reasons.append(f"subcategory:{template_sub} matches classification")
            return True, _profile_reason(reasons, profile, template_profile)
        if not template_sub and _profile_matches(profile, template_profile):
            reasons.append('profile基础模板：模板未绑定小类但 profile 匹配。')
            return True, _profile_reason(reasons, profile, template_profile)
        return False, []
    reasons.append('小类不明确或候选接近：加载该模块全量模板去重集合。')
    return True, _profile_reason(reasons, profile, template_profile)


def _profile_matches(profile: str, template_profile: str) -> bool:
    return profile in {'unknown', '', template_profile} or template_profile in {'unknown', ''}


def _profile_reason(reasons: List[str], profile: str, template_profile: str) -> List[str]:
    if profile == template_profile:
        reasons.append(f"profile:{profile} matches")
    elif profile == 'unknown' or template_profile == 'unknown':
        reasons.append(f"profile not constrained ({profile} / {template_profile})")
    else:
        reasons.append(f"profile differs but retained for module evidence ({profile} / {template_profile})")
    return reasons


def _build_selected_template(
    template: Dict[str, Any],
    parameter_values: Dict[str, List[str]],
    reasons: List[str],
    warnings: List[Dict[str, Any]],
    *,
    pattern_fields: Iterable[str],
    expanded_field: str,
    template_kind: str,
) -> Dict[str, Any]:
    out = dict(template)
    out['template_kind'] = template_kind
    out['selection_reasons'] = reasons
    placeholders = sorted(_template_placeholders(template, pattern_fields))
    explicit_parameters = [_normalize_param_name(item) for item in template.get('parameters') or []]
    parameters = sorted({p for p in placeholders + explicit_parameters if p})
    resolved = {name: parameter_values.get(name, []) for name in parameters if parameter_values.get(name)}
    unresolved = [name for name in parameters if not resolved.get(name)]
    out['parameters'] = parameters
    out['resolved_parameters'] = resolved
    out['unresolved_parameters'] = unresolved
    out['search_enabled'] = not unresolved
    out['status'] = 'ready' if not unresolved else 'needs_parameters'
    if unresolved:
        out['selection_reasons'].append('存在未解析占位符，前置工作流不盲目搜索，交给 Deep 或用户澄清。')

    expanded = _expand_template_patterns(template, parameter_values, pattern_fields)
    out[expanded_field] = expanded
    if template_kind == 'evidence':
        expanded_regex = str(expanded.get('regex') or template.get('regex') or '')
        if out['status'] == 'ready':
            try:
                re.compile(expanded_regex)
            except re.error as exc:
                out['status'] = 'invalid_expanded_regex'
                out['search_enabled'] = False
                warnings.append({'template_id': out.get('id'), 'code': 'invalid_expanded_regex', 'message': str(exc)})
        out['expanded_regex'] = expanded_regex
    return out


def _template_placeholders(template: Dict[str, Any], fields: Iterable[str]) -> set[str]:
    placeholders: set[str] = set()
    for field in fields:
        value = template.get(field)
        values = value if isinstance(value, list) else [value]
        for item in values:
            placeholders.update(PLACEHOLDER_RE.findall(str(item or '')))
    return placeholders


def _expand_template_patterns(
    template: Dict[str, Any],
    parameter_values: Dict[str, List[str]],
    fields: Iterable[str],
) -> Dict[str, Any]:
    expanded: Dict[str, Any] = {}
    for field in fields:
        value = template.get(field)
        if isinstance(value, list):
            expanded[field] = [_expand_placeholders(str(item or ''), parameter_values) for item in value]
        else:
            expanded[field] = _expand_placeholders(str(value or ''), parameter_values)
    return expanded


def _expand_placeholders(pattern: str, parameter_values: Dict[str, List[str]]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        values = parameter_values.get(name) or []
        if not values:
            return match.group(0)
        escaped = [re.escape(str(value)) for value in values if str(value)]
        if not escaped:
            return match.group(0)
        return escaped[0] if len(escaped) == 1 else '(?:' + '|'.join(escaped) + ')'

    return PLACEHOLDER_RE.sub(replace, pattern or '')


def _select_experience_hints(cache: Dict[str, Any], module_selections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    hints: List[Dict[str, Any]] = []
    module_index = cache.get('module_index') if isinstance(cache.get('module_index'), dict) else {}
    for selection in module_selections:
        loaded = module_index.get(selection['module_id']) or {}
        for item in loaded.get('experience_logs') or []:
            hint = dict(item)
            hint['selection_reason'] = f"模块 {selection['module_id']} 被选中，加载模块经验日志。"
            hints.append(hint)
    global_data = cache.get('global') if isinstance(cache.get('global'), dict) else {}
    for item in global_data.get('oem_experience') or []:
        hint = dict(item)
        hint['selection_reason'] = '全局 OEM 经验日志，作为厂商定制日志释义候选。'
        hints.append(hint)
    return _dedupe_by_id(hints)[:50]


def _parameter_values(parameter_resolution: Dict[str, Any]) -> Dict[str, List[str]]:
    resolved = parameter_resolution.get('resolved_parameters') if isinstance(parameter_resolution.get('resolved_parameters'), dict) else {}
    out: Dict[str, List[str]] = {}
    for key, value in resolved.items():
        values = value if isinstance(value, list) else [value]
        cleaned = []
        for item in values:
            text = str(item or '').strip()
            if text and text not in cleaned:
                cleaned.append(text)
        out[_normalize_param_name(key)] = cleaned
    return out


def _normalize_param_name(name: Any) -> str:
    return str(name or '').strip().lstrip('$')


def _classification_summary(classification: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'module_id': classification.get('module_id') or 'unknown',
        'module_confidence': _confidence(classification.get('module_confidence')),
        'submodule_id': classification.get('submodule_id') or 'unknown',
        'submodule_confidence': _confidence(classification.get('submodule_confidence')),
        'profile': classification.get('profile') or 'unknown',
        'candidate_count': len(classification.get('top_candidates') or []),
        'need_user_clarification': bool(classification.get('need_user_clarification')),
    }


def _parameter_summary(parameter_resolution: Dict[str, Any]) -> Dict[str, Any]:
    resolved = parameter_resolution.get('resolved_parameters') if isinstance(parameter_resolution.get('resolved_parameters'), dict) else {}
    return {
        'module_id': parameter_resolution.get('module_id') or 'unknown',
        'need_package_resolution': bool(parameter_resolution.get('need_package_resolution')),
        'need_user_clarification': bool(parameter_resolution.get('need_user_clarification')),
        'resolved_parameters': resolved,
        'package_candidate_count': len(parameter_resolution.get('package_candidates') or []),
    }


def _counts(templates: List[Dict[str, Any]], xml_templates: List[Dict[str, Any]], experience_hints: List[Dict[str, Any]]) -> Dict[str, int]:
    all_templates = templates + xml_templates
    return {
        'template_count': len(templates),
        'xml_state_template_count': len(xml_templates),
        'experience_hint_count': len(experience_hints),
        'ready_count': sum(1 for item in all_templates if item.get('status') == 'ready'),
        'needs_parameters_count': sum(1 for item in all_templates if item.get('status') == 'needs_parameters'),
        'search_enabled_count': sum(1 for item in all_templates if item.get('search_enabled')),
    }


def _dedupe_by_id(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    seen = set()
    for item in items:
        key = str(item.get('id') or item.get('pattern') or item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _confidence(value: Any) -> float:
    try:
        return round(min(1.0, max(0.0, float(value))), 3)
    except (TypeError, ValueError):
        return 0.0


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
