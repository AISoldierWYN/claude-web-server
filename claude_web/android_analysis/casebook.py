"""Case card recall for Android issue analysis."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .models import AndroidAnalysisError


def recall_case_cards(
    knowledge_dir: Path,
    planner: Dict[str, Any],
    matched_rules: Dict[str, Any],
    max_cards: int = 5,
) -> Dict[str, Any]:
    # 首轮只召回轻量 Case Card，不加载完整案例正文，避免历史案例把当前证据挤出上下文。
    cards = _load_cards(Path(knowledge_dir))
    scored = []
    for card in cards:
        score, reasons = _score_card(card, planner, matched_rules)
        if score <= 0:
            continue
        public = dict(card)
        public['score'] = round(score, 3)
        public['match_reasons'] = reasons
        public.pop('full_case_path', None)
        scored.append(public)
    scored.sort(key=lambda c: c.get('score', 0), reverse=True)
    return {
        'version': 1,
        'card_count': len(scored[:max_cards]),
        'cards': scored[:max_cards],
    }


def write_case_cards(artifacts_dir: Path, cards: Dict[str, Any]) -> None:
    with open(Path(artifacts_dir) / 'case_cards.json', 'w', encoding='utf-8') as f:
        json.dump(cards, f, ensure_ascii=False, indent=2)


def generate_case_draft(
    artifacts_dir: Path,
    source_job_id: str = '',
) -> Dict[str, Any]:
    # 案例和规则候选先以 draft 形式落在 job artifacts，必须人工确认后才进入共享知识目录。
    # 这能防止一次误判永久污染后续所有用户的规则/案例库。
    artifacts_dir = Path(artifacts_dir)
    planner = _read_json(artifacts_dir / 'planner_result.json')
    matched = _read_json(artifacts_dir / 'matched_rules.json')
    verifier = _read_json_optional(artifacts_dir / 'verifier_result.json', default={})
    report = _read_text_optional(artifacts_dir / 'verified_report.md') or _read_text_optional(artifacts_dir / 'deep_report.md') or _read_text_optional(artifacts_dir / 'final_report.md')
    evidence = _read_text_optional(artifacts_dir / 'deep_evidence_pack.md') or _read_text_optional(artifacts_dir / 'first_evidence_pack.md')
    top_events = (matched.get('events') or [])[:5]
    bundle_ids = _bundle_ids(planner, matched)
    issue_type = (planner.get('issue_types') or ['unknown'])[0]
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    draft_id = 'case-' + time.strftime('%Y%m%d-%H%M%S')
    title = _derive_title(report, issue_type)
    tags = _derive_tags(top_events, planner)
    rule_candidates = _build_rule_candidates(draft_id, bundle_ids, issue_type, top_events, planner)

    draft = {
        'version': 1,
        'status': 'draft',
        'id': draft_id,
        'source_job_id': source_job_id,
        'created_at': now,
        'title': title,
        'signature': _derive_signature(top_events, planner),
        'issue_type': issue_type,
        'source_bundle_ids': bundle_ids,
        'tags': tags,
        'summary': _summarize_report(report),
        'root_cause_summary': _root_cause_summary(report, verifier),
        'fix_summary': '待人工确认后补充修复方式。',
        'evidence_summary': _evidence_summary(top_events, evidence),
        'verifier': {
            'status': verifier.get('status') or 'not_run',
            'overclaim_risk': verifier.get('overclaim_risk') or '',
            'best_evidence_score': verifier.get('best_evidence_score', 0),
        },
        'report_artifacts': _available_report_artifacts(artifacts_dir),
        'rule_candidates': rule_candidates,
    }
    with open(artifacts_dir / 'case_draft.json', 'w', encoding='utf-8') as f:
        json.dump(draft, f, ensure_ascii=False, indent=2)
    with open(artifacts_dir / 'rule_candidates.json', 'w', encoding='utf-8') as f:
        json.dump({'version': 1, 'enabled': False, 'candidates': rule_candidates}, f, ensure_ascii=False, indent=2)
    return draft


def confirm_case_draft(
    knowledge_dir: Path,
    artifacts_dir: Path,
    bundle_id: str,
    reviewer_note: str = '',
) -> Dict[str, Any]:
    knowledge_dir = Path(knowledge_dir)
    artifacts_dir = Path(artifacts_dir)
    draft = _read_json(artifacts_dir / 'case_draft.json')
    bundle_id = (bundle_id or '').strip() or next(iter(draft.get('source_bundle_ids') or []), '')
    if not bundle_id:
        raise AndroidAnalysisError('case_bundle_required', 'bundle_id is required to confirm a case draft.')
    bundle_dir = knowledge_dir / 'bundles' / bundle_id
    if not bundle_dir.is_dir():
        raise AndroidAnalysisError('case_bundle_missing', f'Bundle knowledge directory does not exist: {bundle_id}')

    cases_dir = bundle_dir / 'cases'
    indexes_dir = bundle_dir / 'indexes'
    drafts_dir = bundle_dir / 'drafts'
    for path in (cases_dir, indexes_dir, drafts_dir):
        path.mkdir(parents=True, exist_ok=True)

    case_id = _safe_id(str(draft.get('id') or 'case'))
    case_path = _unique_path(cases_dir / f'{case_id}.json')
    case_id = case_path.stem
    confirmed = dict(draft)
    confirmed.update(
        {
            'id': case_id,
            'status': 'confirmed',
            'confirmed_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'confirmed_bundle_id': bundle_id,
            'reviewer_note': reviewer_note[:1000],
        }
    )
    with open(case_path, 'w', encoding='utf-8') as f:
        json.dump(confirmed, f, ensure_ascii=False, indent=2)

    rule_candidates = confirmed.get('rule_candidates') or []
    rule_draft_path = drafts_dir / f'{case_id}_rule_candidates.json'
    with open(rule_draft_path, 'w', encoding='utf-8') as f:
        json.dump({'version': 1, 'enabled': False, 'candidates': rule_candidates}, f, ensure_ascii=False, indent=2)

    card = _case_card_from_confirmed(confirmed, case_path.relative_to(bundle_dir).as_posix())
    index_path = indexes_dir / 'case_cards.jsonl'
    _upsert_jsonl_card(index_path, card)
    return {
        'ok': True,
        'case_id': case_id,
        'bundle_id': bundle_id,
        'case_path': case_path.relative_to(knowledge_dir).as_posix(),
        'case_card_path': index_path.relative_to(knowledge_dir).as_posix(),
        'rule_draft_path': rule_draft_path.relative_to(knowledge_dir).as_posix(),
    }


def _load_cards(knowledge_dir: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for path in sorted(knowledge_dir.glob('bundles/*/indexes/case_cards.jsonl')):
        out.extend(_read_jsonl(path))
    for path in sorted(knowledge_dir.glob('global/*/indexes/case_cards.jsonl')):
        out.extend(_read_jsonl(path))
    return out


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    cards: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return cards
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith('#'):
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get('id'):
            cards.append(data)
    return cards


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise AndroidAnalysisError('case_artifact_missing', f'{path.name} does not exist.')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise AndroidAnalysisError('case_artifact_invalid', f'{path.name} is invalid JSON.') from exc
    if not isinstance(data, dict):
        raise AndroidAnalysisError('case_artifact_invalid', f'{path.name} must be a JSON object.')
    return data


def _read_json_optional(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else default
    except (OSError, json.JSONDecodeError):
        return default
    return data if isinstance(data, dict) else default


def _read_text_optional(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8') if path.is_file() else ''
    except OSError:
        return ''


def _bundle_ids(planner: Dict[str, Any], matched: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for value in planner.get('candidate_bundle_ids') or []:
        _append(out, str(value), 8)
    for event in matched.get('events') or []:
        for value in event.get('source_bundle_ids') or []:
            _append(out, str(value), 8)
    return out


def _derive_title(report: str, issue_type: str) -> str:
    for line in (report or '').splitlines():
        text = line.strip().lstrip('#').strip()
        if text and not text.startswith('Verifier') and len(text) <= 80:
            return text
    return f'Android {issue_type} case'


def _derive_tags(events: List[Dict[str, Any]], planner: Dict[str, Any]) -> List[str]:
    tags: List[str] = []
    for item in planner.get('candidate_keywords') or []:
        if len(str(item)) <= 40:
            _append(tags, str(item).lower(), 12)
    for event in events:
        for tag in event.get('tags') or []:
            _append(tags, str(tag).lower(), 12)
    return tags


def _derive_signature(events: List[Dict[str, Any]], planner: Dict[str, Any]) -> str:
    parts: List[str] = []
    if planner.get('candidate_bundle_ids'):
        parts.extend(str(x) for x in planner.get('candidate_bundle_ids') or [])
    for event in events[:3]:
        parts.append(str(event.get('rule_id') or ''))
        parts.extend(str(x) for x in (event.get('matched_terms') or [])[:3])
    return ' | '.join(x for x in parts if x)[:500]


def _summarize_report(report: str) -> str:
    text = re.sub(r'\s+', ' ', (report or '').strip())
    return text[:700] or '待人工补充案例摘要。'


def _root_cause_summary(report: str, verifier: Dict[str, Any]) -> str:
    if verifier.get('status') == 'needs_more_evidence':
        return 'Verifier 标记证据不足，暂不沉淀明确根因。'
    for line in (report or '').splitlines():
        text = line.strip()
        if any(key in text for key in ('根因', '原因', '结论')):
            return text.lstrip('#').strip()[:500]
    return '待人工确认根因。'


def _evidence_summary(events: List[Dict[str, Any]], evidence: str) -> List[Dict[str, Any]]:
    out = []
    for event in events:
        loc = str(event.get('path') or '')
        line_range = event.get('line_range') or []
        if len(line_range) >= 2:
            loc += f':{line_range[0]}-{line_range[1]}'
        out.append(
            {
                'rule_id': event.get('rule_id'),
                'location': loc,
                'relevance': (event.get('relevance') or {}).get('score', 0),
                'matched_terms': event.get('matched_terms') or [],
            }
        )
    if not out and evidence:
        out.append({'summary': evidence[:500]})
    return out[:8]


def _available_report_artifacts(artifacts_dir: Path) -> List[str]:
    names = [
        'final_report.md',
        'deep_report.md',
        'verified_report.md',
        'first_evidence_pack.md',
        'deep_evidence_pack.md',
        'verifier_result.json',
    ]
    return [name for name in names if (artifacts_dir / name).is_file()]


def _build_rule_candidates(
    draft_id: str,
    bundle_ids: List[str],
    issue_type: str,
    events: List[Dict[str, Any]],
    planner: Dict[str, Any],
) -> List[Dict[str, Any]]:
    candidates = []
    keywords = [str(x) for x in (planner.get('candidate_keywords') or []) if str(x).strip()]
    for idx, event in enumerate(events[:3], start=1):
        terms = []
        for term in list(event.get('matched_terms') or []) + keywords:
            if 2 <= len(str(term)) <= 80 and str(term) not in terms:
                terms.append(str(term))
        candidates.append(
            {
                'id': f'{_safe_id(draft_id)}-rule-{idx}',
                'title': f"Candidate rule from {event.get('rule_title') or event.get('rule_id')}",
                'source_bundle_ids': bundle_ids,
                'issue_type': event.get('issue_type') or issue_type,
                'enabled': False,
                'match': {
                    'keywords': terms[:10],
                    'paths': [str(event.get('path') or '')] if event.get('path') else [],
                },
                'rationale': '自动生成的规则候选，仅作为草稿保存，需人工确认后再加入正式 rules。',
            }
        )
    return candidates


def _case_card_from_confirmed(case: Dict[str, Any], full_case_path: str) -> Dict[str, Any]:
    return {
        'id': case.get('id'),
        'title': case.get('title'),
        'signature': case.get('signature'),
        'issue_type': case.get('issue_type'),
        'source_bundle_ids': case.get('source_bundle_ids') or [],
        'tags': case.get('tags') or [],
        'summary': case.get('summary'),
        'root_cause_summary': case.get('root_cause_summary'),
        'evidence_summary': case.get('evidence_summary') or [],
        'rule_ids': [r.get('id') for r in (case.get('rule_candidates') or []) if r.get('id')],
        'full_case_path': full_case_path,
    }


def _upsert_jsonl_card(path: Path, card: Dict[str, Any]) -> None:
    cards = [c for c in _read_jsonl(path) if c.get('id') != card.get('id')]
    cards.append(card)
    with open(path, 'w', encoding='utf-8') as f:
        for item in cards:
            f.write(json.dumps(item, ensure_ascii=False, separators=(',', ':')) + '\n')


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for i in range(2, 1000):
        candidate = path.with_name(f'{stem}-{i}{suffix}')
        if not candidate.exists():
            return candidate
    return path.with_name(f'{stem}-{int(time.time())}{suffix}')


def _safe_id(value: str) -> str:
    raw = (value or '').strip().lower()
    out = []
    for ch in raw:
        if ch.isalnum() or ch in '._-':
            out.append(ch)
        elif ch in ' /:':
            out.append('-')
    return ''.join(out).strip('-')[:96] or 'case'


def _append(values: List[str], item: str, max_items: int) -> None:
    if item and item not in values and len(values) < max_items:
        values.append(item)


def _score_card(card: Dict[str, Any], planner: Dict[str, Any], matched_rules: Dict[str, Any]) -> tuple[float, List[str]]:
    score = 0.0
    reasons: List[str] = []
    issue_types = set(str(x) for x in (planner.get('issue_types') or []) if x)
    card_issue = str(card.get('issue_type') or '')
    if card_issue and card_issue in issue_types:
        score += 0.4
        reasons.append('issue_type')
    planner_bundles = set(str(x) for x in (planner.get('candidate_bundle_ids') or []) if x)
    card_bundles = set(str(x) for x in (card.get('source_bundle_ids') or []) if x)
    if planner_bundles and card_bundles.intersection(planner_bundles):
        score += 0.25
        reasons.append('bundle')
    event_tags = set()
    for event in matched_rules.get('events') or []:
        event_tags.update(str(x) for x in (event.get('tags') or []) if x)
        if event.get('rule_id') and event.get('rule_id') in (card.get('rule_ids') or []):
            score += 0.2
            reasons.append('rule_id')
            break
    card_tags = set(str(x) for x in (card.get('tags') or []) if x)
    if event_tags and card_tags.intersection(event_tags):
        score += 0.15
        reasons.append('tags')
    keywords = ' '.join(str(x).lower() for x in (planner.get('candidate_keywords') or []))
    signature = ' '.join(str(card.get(k) or '').lower() for k in ('signature', 'title', 'summary'))
    if keywords and signature and any(token and token in signature for token in keywords.split()):
        score += 0.1
        reasons.append('keyword')
    return min(1.0, score), reasons
