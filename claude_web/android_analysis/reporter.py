"""First-pass report generation for Android issue analysis."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .models import AndroidAnalysisError
from .planner import _run_claude_cli, _trace_ai_stream, _trace_ai_token_usage


_PROMPT_PATH = Path(__file__).resolve().parent / 'prompts' / 'first_pass.md'


def generate_first_report(
    artifacts_dir: Path,
    question: str = '',
    cli_path: str = '',
    timeout_seconds: int = 45,
    enable_ai: bool = True,
    ai_runner: Optional[Callable[[str], str]] = None,
    debug_trace: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    artifacts_dir = Path(artifacts_dir)
    evidence = _read_text(artifacts_dir / 'first_evidence_pack.md')
    matched = _read_json(artifacts_dir / 'matched_rules.json')
    planner = _read_json(artifacts_dir / 'planner_result.json')
    case_cards = _read_json_optional(artifacts_dir / 'case_cards.json', default={'cards': []})

    errors: List[Dict[str, str]] = []
    mode = 'fallback'
    try:
        if debug_trace:
            debug_trace(
                'generating_report',
                'first_report_input',
                {
                    'question': question,
                    'enable_ai': enable_ai,
                    'evidence_chars': len(evidence),
                    'matched_event_count': matched.get('event_count', 0),
                    'top_events': (matched.get('events') or [])[:12],
                    'planner_result': planner,
                    'case_card_count': len(case_cards.get('cards') or []),
                    'case_cards': (case_cards.get('cards') or [])[:5],
                },
            )
        if enable_ai:
            prompt = build_first_report_prompt(question, evidence, matched, planner, case_cards)
            if debug_trace:
                debug_trace(
                    'generating_report',
                    'first_report_prompt',
                    {
                        'prompt_chars': len(prompt),
                        'prompt_preview': prompt,
                    },
                )
            if ai_runner:
                report = ai_runner(prompt)
                _trace_ai_token_usage(debug_trace, 'generating_report', 'first_report', prompt, report)
            else:
                report = _run_claude_cli(
                    prompt,
                    cli_path,
                    timeout_seconds,
                    artifacts_dir,
                    stream_callback=lambda item: _trace_ai_stream(debug_trace, 'generating_report', 'first_report', item),
                    usage_callback=lambda usage: _trace_ai_token_usage(
                        debug_trace,
                        'generating_report',
                        'first_report',
                        prompt,
                        usage.get('output_text', ''),
                        usage=usage,
                    ),
                )
            if debug_trace:
                debug_trace(
                    'generating_report',
                    'first_report_raw_output',
                    {
                        'output_chars': len(report or ''),
                        'output_preview': report or '',
                    },
                )
            report = _clean_report(report)
            if not report:
                raise AndroidAnalysisError('report_empty_output', 'Claude returned empty first-pass report.')
            mode = 'ai'
        else:
            report = build_fallback_report(question, planner, matched, case_cards)
    except AndroidAnalysisError as e:
        errors.append({'code': e.code, 'message': e.message})
        report = build_fallback_report(question, planner, matched, case_cards)
    except Exception as e:
        errors.append({'code': 'report_unexpected_error', 'message': str(e)})
        report = build_fallback_report(question, planner, matched, case_cards)

    (artifacts_dir / 'final_report.md').write_text(report, encoding='utf-8')
    meta = {
        'version': 1,
        'report_mode': mode,
        'errors': errors,
        'has_report': bool(report.strip()),
        'event_count': matched.get('event_count', 0),
        'case_card_count': len(case_cards.get('cards') or []),
    }
    with open(artifacts_dir / 'first_report_meta.json', 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    if debug_trace:
        debug_trace(
            'generating_report',
            'first_report_result',
            {
                **meta,
                'report_preview': report,
            },
        )
    return meta


def build_first_report_prompt(
    question: str,
    evidence_pack: str,
    matched_rules: Dict[str, Any],
    planner: Dict[str, Any],
    case_cards: Dict[str, Any],
) -> str:
    instruction = _PROMPT_PATH.read_text(encoding='utf-8')
    payload = {
        'question': question or '',
        'planner_result': planner,
        'matched_rule_summary': {
            'event_count': matched_rules.get('event_count', 0),
            'events': (matched_rules.get('events') or [])[:20],
        },
        'case_cards': (case_cards.get('cards') or [])[:5],
        'evidence_pack_markdown': evidence_pack[:30000],
    }
    return instruction + '\n\nInput JSON:\n' + json.dumps(payload, ensure_ascii=False, indent=2)


def build_fallback_report(
    question: str,
    planner: Dict[str, Any],
    matched_rules: Dict[str, Any],
    case_cards: Dict[str, Any],
) -> str:
    events = matched_rules.get('events') or []
    top = events[:8]
    lines: List[str] = []
    lines.append('# Android 问题首轮分析报告')
    lines.append('')
    lines.append('## 结论')
    if top:
        issue_types = ', '.join(planner.get('issue_types') or ['unknown'])
        lines.append(f'本地规则已命中 {len(events)} 条候选证据，首轮问题类型倾向：`{issue_types}`。当前报告是基于采样日志和规则命中的初筛结果，不等同于最终根因。')
    else:
        lines.append('当前采样日志中没有命中足够明确的本地规则证据，建议补充时间窗口、包名、复现步骤，或进入 Deep 分析扩大证据范围。')
    lines.append('')
    lines.append('## 用户问题')
    lines.append((question or '').strip() or '未提供')
    lines.append('')
    lines.append('## 关键证据')
    if not top:
        lines.append('- 暂无明确证据。')
    for idx, event in enumerate(top, start=1):
        rel = event.get('relevance') or {}
        loc = _event_location(event)
        terms = ', '.join(event.get('matched_terms') or []) or '无'
        lines.append(f'{idx}. `{event.get("rule_title") or event.get("rule_id")}`，类型 `{event.get("issue_type")}`，位置 `{loc}`，相关性 `{rel.get("score", 0)}`，命中 `{terms}`。')
    lines.append('')
    lines.append('## 置信度')
    if top:
        best = max(float((e.get('relevance') or {}).get('score') or 0) for e in top)
        level = '中' if best >= 0.55 else '低'
        lines.append(f'{level}。证据来自有限采样和规则匹配，仍需要结合用户描述的时间点和业务现象确认。')
    else:
        lines.append('低。缺少足够直接的日志证据。')
    lines.append('')
    lines.append('## 建议下一步')
    lines.append('- 优先确认问题发生时间窗口，并和上述证据位置对齐。')
    lines.append('- 如果是 RDM 业务问题，补充锁机、解锁、provision 或 device policy 的具体操作步骤。')
    lines.append('- 若首轮证据不足，进入 Deep 分析读取更完整日志片段和白名单代码目录。')
    cards = case_cards.get('cards') or []
    if cards:
        lines.append('')
        lines.append('## 相似案例')
        for card in cards[:5]:
            lines.append(f'- `{card.get("title") or card.get("id")}`：{card.get("summary") or card.get("root_cause_summary") or ""}')
    return '\n'.join(lines).rstrip() + '\n'


def _read_text(path: Path) -> str:
    if not path.is_file():
        raise AndroidAnalysisError('report_artifact_missing', f'{path.name} does not exist.')
    return path.read_text(encoding='utf-8')


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise AndroidAnalysisError('report_artifact_missing', f'{path.name} does not exist.')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise AndroidAnalysisError('report_artifact_invalid', f'{path.name} is invalid JSON.') from exc
    if not isinstance(data, dict):
        raise AndroidAnalysisError('report_artifact_invalid', f'{path.name} must be a JSON object.')
    return data


def _read_json_optional(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.is_file():
        return default
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        return default
    return data if isinstance(data, dict) else default


def _clean_report(text: str) -> str:
    raw = (text or '').strip()
    if raw.startswith('```'):
        raw = raw.strip('`').strip()
        if raw.lower().startswith('markdown'):
            raw = raw[8:].strip()
    return raw


def _event_location(event: Dict[str, Any]) -> str:
    line_range = event.get('line_range') or []
    if len(line_range) >= 2:
        return f'{event.get("path")}:{line_range[0]}-{line_range[1]}'
    return str(event.get('path') or '')
