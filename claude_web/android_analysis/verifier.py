"""Verifier for Android analysis reports."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .models import AndroidAnalysisError
from .planner import _run_claude_cli, _trace_ai_stream, _trace_ai_token_usage, parse_planner_json


_PROMPT_PATH = Path(__file__).resolve().parent / 'prompts' / 'verifier.md'


def run_verifier(
    artifacts_dir: Path,
    report_name: str = 'deep_report.md',
    cli_path: str = '',
    timeout_seconds: int = 45,
    enable_ai: bool = True,
    ai_runner: Optional[Callable[[str], str]] = None,
    debug_trace: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    # Verifier 是防过度推断的最后一道门槛；即使 AI 校验失败，也会用本地相关性分数做保底判断。
    artifacts_dir = Path(artifacts_dir)
    report = _read_text(artifacts_dir / report_name)
    matched = _read_json(artifacts_dir / 'matched_rules.json')
    planner = _read_json(artifacts_dir / 'planner_result.json')
    evidence = _read_text_optional(artifacts_dir / 'deep_evidence_pack.md') or _read_text_optional(artifacts_dir / 'first_evidence_pack.md')

    errors: List[Dict[str, str]] = []
    mode = 'fallback'
    try:
        if debug_trace:
            debug_trace(
                'verifying',
                'verifier_input',
                {
                    'report_name': report_name,
                    'enable_ai': enable_ai,
                    'report_chars': len(report),
                    'evidence_chars': len(evidence),
                    'planner_result': planner,
                    'matched_event_count': matched.get('event_count', 0),
                    'top_events': (matched.get('events') or [])[:20],
                    'strong_claim_detected': _has_strong_claim(report),
                },
            )
        if enable_ai:
            prompt = build_verifier_prompt(report, evidence, planner, matched)
            if debug_trace:
                debug_trace(
                    'verifying',
                    'verifier_prompt',
                    {'prompt_chars': len(prompt), 'prompt_preview': prompt},
                )
            if ai_runner:
                raw = ai_runner(prompt)
                _trace_ai_token_usage(debug_trace, 'verifying', f'verifier:{report_name}', prompt, raw)
            else:
                raw = _run_claude_cli(
                    prompt,
                    cli_path,
                    timeout_seconds,
                    artifacts_dir,
                    stream_callback=lambda item: _trace_ai_stream(debug_trace, 'verifying', f'verifier:{report_name}', item),
                    usage_callback=lambda usage: _trace_ai_token_usage(
                        debug_trace,
                        'verifying',
                        f'verifier:{report_name}',
                        prompt,
                        usage.get('output_text', ''),
                        usage=usage,
                    ),
                )
            if debug_trace:
                debug_trace(
                    'verifying',
                    'verifier_raw_output',
                    {'output_chars': len(raw or ''), 'output_preview': raw or ''},
                )
            result = _normalize_verifier_result(parse_planner_json(raw))
            mode = 'ai'
        else:
            result = fallback_verify(report, planner, matched)
    except AndroidAnalysisError as e:
        errors.append({'code': e.code, 'message': e.message})
        result = fallback_verify(report, planner, matched)
    except Exception as e:
        errors.append({'code': 'verifier_unexpected_error', 'message': str(e)})
        result = fallback_verify(report, planner, matched)

    result['version'] = 1
    result['verifier_mode'] = mode
    result['errors'] = errors
    with open(artifacts_dir / 'verifier_result.json', 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    (artifacts_dir / 'verified_report.md').write_text(_render_verified_report(report, result), encoding='utf-8')
    if debug_trace:
        debug_trace('verifying', 'verifier_result', {**result, 'report_name': report_name})
    return result


def build_verifier_prompt(
    report: str,
    evidence: str,
    planner: Dict[str, Any],
    matched_rules: Dict[str, Any],
) -> str:
    instruction = _PROMPT_PATH.read_text(encoding='utf-8')
    payload = {
        'report_markdown': report[:30000],
        'evidence_pack_markdown': evidence[:50000],
        'planner_result': planner,
        'matched_rule_summary': {
            'event_count': matched_rules.get('event_count', 0),
            'events': (matched_rules.get('events') or [])[:20],
        },
    }
    return instruction + '\n\nInput JSON:\n' + json.dumps(payload, ensure_ascii=False, indent=2)


def fallback_verify(report: str, planner: Dict[str, Any], matched_rules: Dict[str, Any]) -> Dict[str, Any]:
    # 本地校验故意保守：最高证据相关性低时，宁可要求补证据，也不输出确定根因。
    events = matched_rules.get('events') or []
    best_score = 0.0
    if events:
        best_score = max(float((e.get('relevance') or {}).get('score') or 0) for e in events)
    strong_claim = _has_strong_claim(report)
    unsupported: List[str] = []
    warnings: List[str] = []
    status = 'supported'
    risk = 'low'

    if not events:
        status = 'needs_more_evidence'
        risk = 'high'
        unsupported.append('报告缺少本地规则证据支撑。')
    elif best_score < 0.55:
        status = 'needs_more_evidence'
        risk = 'high'
        unsupported.append('最高相关性证据低于 0.55，不能支撑明确根因。')
    elif strong_claim and best_score < 0.75:
        status = 'partially_supported'
        risk = 'medium'
        warnings.append('报告包含较强结论措辞，但最高相关性证据未达到高置信阈值。')

    if planner.get('need_user_clarification') and status == 'supported':
        status = 'partially_supported'
        risk = 'medium'
        warnings.append('Planner 标记需要用户补充信息，结论应保留不确定性。')

    return {
        'status': status,
        'overclaim_risk': risk,
        'best_evidence_score': round(best_score, 3),
        'supported_claims': ['当前报告可作为阶段性分析参考。'] if status in {'supported', 'partially_supported'} else [],
        'unsupported_claims': unsupported,
        'warnings': warnings,
        'recommended_next_action': _recommended_next_action(status),
    }


def _normalize_verifier_result(data: Dict[str, Any]) -> Dict[str, Any]:
    status = str(data.get('status') or '').strip()
    if status not in {'supported', 'partially_supported', 'needs_more_evidence'}:
        status = 'needs_more_evidence'
    risk = str(data.get('overclaim_risk') or '').strip()
    if risk not in {'low', 'medium', 'high'}:
        risk = 'high' if status == 'needs_more_evidence' else 'medium'
    return {
        'status': status,
        'overclaim_risk': risk,
        'best_evidence_score': _float(data.get('best_evidence_score')),
        'supported_claims': _str_list(data.get('supported_claims')),
        'unsupported_claims': _str_list(data.get('unsupported_claims')),
        'warnings': _str_list(data.get('warnings')),
        'recommended_next_action': str(data.get('recommended_next_action') or _recommended_next_action(status))[:800],
    }


def _render_verified_report(report: str, result: Dict[str, Any]) -> str:
    status = result.get('status')
    if status == 'supported':
        title = 'Verifier：当前报告未发现明显过度推断。'
    elif status == 'partially_supported':
        title = 'Verifier：当前报告部分受证据支撑，请保留不确定性。'
    else:
        title = 'Verifier：当前证据不足以支撑明确结论。'
    lines = [
        f'> {title}',
        f'> 状态：`{status}`；过度推断风险：`{result.get("overclaim_risk")}`；最高证据相关性：`{result.get("best_evidence_score", 0)}`。',
    ]
    if result.get('warnings'):
        lines.append('> 提醒：' + '；'.join(str(x) for x in result.get('warnings') or []))
    if result.get('unsupported_claims'):
        lines.append('> 未支撑点：' + '；'.join(str(x) for x in result.get('unsupported_claims') or []))
    lines.append('')
    lines.append((report or '').strip())
    return '\n'.join(lines).rstrip() + '\n'


def _recommended_next_action(status: str) -> str:
    if status == 'supported':
        return '可以将报告作为阶段性结论，并按需生成案例草稿。'
    if status == 'partially_supported':
        return '建议补充时间窗口、业务操作步骤或进入更细粒度日志检索后再确认根因。'
    return '建议补充更明确的日志、时间点、包名或业务模块信息，不要直接沉淀为正式案例。'


def _has_strong_claim(report: str) -> bool:
    patterns = [
        r'根因是',
        r'结论是',
        r'确定',
        r'必然',
        r'不是.+而是',
        r'root cause',
        r'confirmed',
    ]
    text = report or ''
    return any(re.search(p, text, flags=re.IGNORECASE | re.DOTALL) for p in patterns)


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise AndroidAnalysisError('verifier_artifact_missing', f'{path.name} does not exist.')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise AndroidAnalysisError('verifier_artifact_invalid', f'{path.name} is invalid JSON.') from exc
    if not isinstance(data, dict):
        raise AndroidAnalysisError('verifier_artifact_invalid', f'{path.name} must be a JSON object.')
    return data


def _read_text(path: Path) -> str:
    if not path.is_file():
        raise AndroidAnalysisError('verifier_artifact_missing', f'{path.name} does not exist.')
    return path.read_text(encoding='utf-8')


def _read_text_optional(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8') if path.is_file() else ''
    except OSError:
        return ''


def _str_list(value: Any) -> List[str]:
    return [str(x)[:800] for x in value if str(x).strip()] if isinstance(value, list) else []


def _float(value: Any) -> float:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return 0.0


# 重新定义 fallback_verify，覆盖上方历史实现。这样可以在不大范围重写旧文件的情况下，
# 把命中词可见性、泛化词降权、报告/证据冲突检查纳入本地校验。
def fallback_verify(report: str, planner: Dict[str, Any], matched_rules: Dict[str, Any]) -> Dict[str, Any]:
    events = matched_rules.get('events') or []
    best_score = max((_event_verifier_score(e) for e in events), default=0.0)
    strong_claim = _has_strong_claim(report) or _has_strong_claim_cn(report)
    unsupported: List[str] = []
    warnings: List[str] = []
    status = 'supported'
    risk = 'low'
    hidden_hits = _events_with_hidden_hits(events)
    generic_hits = _events_with_only_generic_hits(events)
    conflicts = _report_evidence_conflicts(report, events)

    if not events:
        status = 'needs_more_evidence'
        risk = 'high'
        unsupported.append('报告缺少本地规则证据支撑。')
    elif best_score < 0.55:
        status = 'needs_more_evidence'
        risk = 'high'
        unsupported.append('最高相关性证据低于 0.55，不能支撑明确根因。')
    elif strong_claim and best_score < 0.75:
        status = 'partially_supported'
        risk = 'medium'
        warnings.append('报告包含较强结论推断，但最高相关性证据未达到高置信阈值。')

    if hidden_hits:
        if status == 'supported':
            status = 'partially_supported'
            risk = 'medium'
        warnings.append(f'存在 {len(hidden_hits)} 条规则命中词未出现在最终证据片段中，相关性已降权。')
    if generic_hits and best_score < 0.75:
        if status == 'supported':
            status = 'partially_supported'
            risk = 'medium'
        warnings.append(f'存在 {len(generic_hits)} 条仅由 lock/device/check 等泛化词触发的命中，需要结合更强证据确认。')
    if conflicts:
        if status == 'supported':
            status = 'partially_supported'
        if risk == 'low':
            risk = 'medium'
        warnings.extend(conflicts)

    if planner.get('need_user_clarification') and status == 'supported':
        status = 'partially_supported'
        risk = 'medium'
        warnings.append('Planner 标记需要用户补充信息，结论应保留不确定性。')

    return {
        'status': status,
        'overclaim_risk': risk,
        'best_evidence_score': round(best_score, 3),
        'supported_claims': ['当前报告可作为阶段性分析参考。'] if status in {'supported', 'partially_supported'} else [],
        'unsupported_claims': unsupported,
        'warnings': warnings,
        'recommended_next_action': _recommended_next_action(status),
    }


def _has_strong_claim_cn(report: str) -> bool:
    patterns = [r'根因是', r'结论是', r'确定', r'必然', r'不是.+而是']
    text = report or ''
    return any(re.search(p, text, flags=re.IGNORECASE | re.DOTALL) for p in patterns)


def _event_verifier_score(event: Dict[str, Any]) -> float:
    score = float((event.get('relevance') or {}).get('score') or 0)
    visibility = event.get('hit_visibility') if isinstance(event.get('hit_visibility'), dict) else {}
    if visibility and not visibility.get('snippet_contains_hit'):
        score *= 0.45
    if _event_has_only_generic_hits(event):
        score = min(score, 0.62)
    return round(score, 3)


def _events_with_hidden_hits(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        event
        for event in events
        if isinstance(event.get('hit_visibility'), dict) and not event['hit_visibility'].get('snippet_contains_hit')
    ]


def _events_with_only_generic_hits(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [event for event in events if _event_has_only_generic_hits(event)]


def _event_has_only_generic_hits(event: Dict[str, Any]) -> bool:
    terms = [str(x).lower() for x in (event.get('matched_terms') or []) if str(x)]
    if not terms:
        return False
    strong_markers = {'rdm', 'realtimedevicemanager', 'devicelock', 'hnlock', 'lockactivity'}
    visibility = event.get('hit_visibility') if isinstance(event.get('hit_visibility'), dict) else {}
    event_text = ' '.join(
        str(x or '')
        for x in [
            event.get('path'),
            event.get('snippet'),
            *(visibility.get('visible_terms') or []),
        ]
    ).lower()
    if any(any(marker in term for marker in strong_markers) for term in terms) or any(
        marker in event_text for marker in strong_markers
    ):
        return False
    generic = {'lock', 'unlock', 'clear', 'check', 'policy', 'device', 'owner', 'bind', 'signal'}
    normalized = {re.sub(r'[^a-z0-9]+', '', term) for term in terms}
    return bool(normalized) and all(term in generic for term in normalized)


def _report_evidence_conflicts(report: str, events: List[Dict[str, Any]]) -> List[str]:
    text = report or ''
    if not text:
        return []
    terms: List[str] = []
    for event in events:
        terms.extend(str(x) for x in (event.get('matched_terms') or []) if str(x))
        visibility = event.get('hit_visibility') if isinstance(event.get('hit_visibility'), dict) else {}
        terms.extend(str(x) for x in (visibility.get('visible_terms') or []) if str(x))
    strong_terms = []
    for term in terms:
        lower = term.lower()
        if any(marker in lower for marker in ['rdm', 'realtimedevicemanager', 'devicelock', 'hnlock', 'lockactivity']):
            strong_terms.append(term)
    conflicts = []
    for term in sorted(set(strong_terms), key=len, reverse=True)[:20]:
        if _has_report_term_conflict(text, term):
            conflicts.append(f'报告声称未发现 `{term}`，但本地证据命中了该信号，请复核报告表述。')
    return conflicts[:5]


def _has_report_term_conflict(text: str, term: str) -> bool:
    """判断报告是否明确否定一个已被本地证据命中的强信号。

    这里故意只捕获“未发现/未找到 X”这类直接否定。像“LockActivity 已启动，
    但无后续日志确认持续前台”属于合理的不确定性表达，不应被判为证据冲突。
    """
    if not term:
        return False
    term_re = re.escape(term)
    direct_negative_patterns = [
        rf'(?:未发现|没有发现|未捕获|未检索到|未找到|找不到).{{0,40}}{term_re}',
        rf'{term_re}.{{0,40}}(?:未发现|没有发现|未捕获|未检索到|未找到|找不到|不存在)',
    ]
    if not any(re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL) for pattern in direct_negative_patterns):
        return False

    positive_patterns = [
        rf'(?:已发现|发现|已命中|命中|已确认|确认|已记录|记录|已启动|启动|存在|显示).{{0,80}}{term_re}',
        rf'{term_re}.{{0,80}}(?:已发现|发现|已命中|命中|已确认|确认|已记录|记录|已启动|启动|存在|显示)',
    ]
    has_positive_evidence = any(re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL) for pattern in positive_patterns)
    if not has_positive_evidence:
        return True

    # 如果同一份报告已经明确承认该信号存在，只把“未发现 X 本身”的强否定视为冲突。
    strict_negative_patterns = [
        rf'(?:未发现|没有发现|未捕获|未检索到|未找到|找不到)\s*[`"\']?{term_re}[`"\']?',
        rf'[`"\']?{term_re}[`"\']?\s*(?:未发现|没有发现|未捕获|未检索到|未找到|找不到|不存在)',
    ]
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in strict_negative_patterns)


def _recommended_next_action(status: str) -> str:
    if status == 'supported':
        return '可以将报告作为阶段性结论，并按需生成案例草稿。'
    if status == 'partially_supported':
        return '建议补充时间窗口、业务操作步骤或进入更细粒度日志检索后再确认根因。'
    return '建议补充更明确的日志、时间点、包名或业务模块信息，不要直接沉淀为正式案例。'


def _render_verified_report(report: str, result: Dict[str, Any]) -> str:
    status = result.get('status')
    if status == 'supported':
        title = 'Verifier：当前报告未发现明显过度推断。'
    elif status == 'partially_supported':
        title = 'Verifier：当前报告仅部分受证据支撑，请保留不确定性。'
    else:
        title = 'Verifier：当前证据不足以支撑明确结论。'
    lines = [
        f'> {title}',
        f'> 状态：`{status}`；过度推断风险：`{result.get("overclaim_risk")}`；最高证据相关性：`{result.get("best_evidence_score", 0)}`。',
    ]
    if result.get('warnings'):
        lines.append('> 提醒：' + '；'.join(str(x) for x in result.get('warnings') or []))
    if result.get('unsupported_claims'):
        lines.append('> 未支撑点：' + '；'.join(str(x) for x in result.get('unsupported_claims') or []))
    lines.append('')
    lines.append((report or '').strip())
    return '\n'.join(lines).rstrip() + '\n'
