"""Verifier for Android analysis reports."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .models import AndroidAnalysisError
from .planner import _run_claude_cli, parse_planner_json


_PROMPT_PATH = Path(__file__).resolve().parent / 'prompts' / 'verifier.md'


def run_verifier(
    artifacts_dir: Path,
    report_name: str = 'deep_report.md',
    cli_path: str = '',
    timeout_seconds: int = 45,
    enable_ai: bool = True,
    ai_runner: Optional[Callable[[str], str]] = None,
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
        if enable_ai:
            prompt = build_verifier_prompt(report, evidence, planner, matched)
            raw = ai_runner(prompt) if ai_runner else _run_claude_cli(prompt, cli_path, timeout_seconds, artifacts_dir)
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
