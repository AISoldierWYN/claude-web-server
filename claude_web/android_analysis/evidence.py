"""Evidence pack generation for Android issue analysis."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from .models import AndroidAnalysisError


def generate_first_evidence_pack(artifacts_dir: Path, question: str = '') -> Dict[str, Any]:
    # Evidence Pack 是模型首轮报告的唯一主要输入之一，只放入排序后的少量证据。
    # 这样既控制 token 成本，也避免无关高严重日志淹没用户真正关心的业务问题。
    artifacts_dir = Path(artifacts_dir)
    planner = _read_json(artifacts_dir / 'planner_result.json')
    matched = _read_json(artifacts_dir / 'matched_rules.json')
    events = matched.get('events') or []
    top_events = events[:12]
    md = _render_markdown(question, planner, matched, top_events)
    (artifacts_dir / 'first_evidence_pack.md').write_text(md, encoding='utf-8')
    summary = {
        'version': 1,
        'event_count': len(events),
        'top_event_count': len(top_events),
        'issue_types': planner.get('issue_types') or [],
        'candidate_bundle_ids': planner.get('candidate_bundle_ids') or [],
        'has_evidence': bool(top_events),
    }
    with open(artifacts_dir / 'first_evidence_pack.json', 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return summary


def _render_markdown(
    question: str,
    planner: Dict[str, Any],
    matched: Dict[str, Any],
    events: List[Dict[str, Any]],
) -> str:
    lines: List[str] = []
    lines.append('# Android Issue First Evidence Pack')
    lines.append('')
    lines.append('## User Question')
    lines.append((question or '').strip() or '(empty)')
    lines.append('')
    lines.append('## Planner Route')
    lines.append(f"- Issue types: {', '.join(planner.get('issue_types') or ['unknown'])}")
    lines.append(f"- Candidate bundles: {', '.join(planner.get('candidate_bundle_ids') or []) or '(none)'}")
    lines.append(f"- Candidate rule packs: {', '.join(planner.get('candidate_rule_packs') or []) or '(none)'}")
    lines.append(f"- Candidate keywords: {', '.join(planner.get('candidate_keywords') or []) or '(none)'}")
    lines.append(f"- Planner confidence: {planner.get('confidence', 0)}")
    lines.append('')
    lines.append('## Local Rule Summary')
    lines.append(f"- Rule packs loaded: {matched.get('rule_pack_count', 0)}")
    lines.append(f"- Matched events: {matched.get('event_count', 0)}")
    if not events:
        lines.append('- Evidence status: no local rule evidence found yet')
        lines.append('')
        lines.append('## Next Step')
        lines.append('补充更明确的时间窗口、包名、功能模块或复现现象，或进入 Deep 分析扩大证据范围。')
        return '\n'.join(lines).rstrip() + '\n'

    lines.append('')
    lines.append('## Top Evidence')
    for idx, event in enumerate(events, start=1):
        relevance = event.get('relevance') or {}
        reasons = relevance.get('reasons') or []
        line_range = event.get('line_range') or []
        if len(line_range) >= 2:
            loc = f"{event.get('path')}:{line_range[0]}-{line_range[1]}"
        else:
            loc = str(event.get('path') or '')
        lines.append(f"### {idx}. {event.get('rule_title') or event.get('rule_id')}")
        lines.append(f"- Issue type: {event.get('issue_type')}")
        lines.append(f"- Severity: {event.get('severity')}")
        lines.append(f"- Location: {loc}")
        lines.append(f"- Relevance: {relevance.get('score', 0)} ({'; '.join(reasons)})")
        if event.get('source_bundle_ids'):
            lines.append(f"- Source bundles: {', '.join(event.get('source_bundle_ids') or [])}")
        if event.get('matched_terms'):
            lines.append(f"- Matched terms: {', '.join(event.get('matched_terms') or [])}")
        lines.append('')
        lines.append('```text')
        lines.append(str(event.get('snippet') or '').strip())
        lines.append('```')
        lines.append('')
    lines.append('## Analysis Boundary')
    lines.append('This pack is generated from bounded local samples and rule matches. It is evidence for the first AI pass, not a final root-cause conclusion.')
    return '\n'.join(lines).rstrip() + '\n'


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise AndroidAnalysisError('evidence_artifact_missing', f'{path.name} does not exist.')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise AndroidAnalysisError('evidence_artifact_invalid', f'{path.name} is invalid JSON.') from exc
    if not isinstance(data, dict):
        raise AndroidAnalysisError('evidence_artifact_invalid', f'{path.name} must be a JSON object.')
    return data
