"""Local rule matching for Android issue analysis artifacts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from .models import AndroidAnalysisError
from .rule_loader import load_rule_packs


def run_rule_matching(
    artifacts_dir: Path,
    knowledge_dir: Path,
    question: str = '',
) -> Dict[str, Any]:
    artifacts_dir = Path(artifacts_dir)
    planner = _read_json(artifacts_dir / 'planner_result.json')
    samples = _read_json(artifacts_dir / 'file_samples.json')
    manifest = _read_json(artifacts_dir / 'file_manifest.json')

    packs = load_rule_packs(
        knowledge_dir,
        candidate_rule_packs=planner.get('candidate_rule_packs') or [],
        candidate_bundle_ids=planner.get('candidate_bundle_ids') or [],
    )
    events = match_rule_packs(packs, samples, manifest, planner, question)
    result = {
        'version': 1,
        'rule_pack_count': len(packs),
        'event_count': len(events),
        'events': events,
    }
    _write_json(artifacts_dir / 'matched_rules.json', result)
    return result


def match_rule_packs(
    rule_packs: Iterable[Dict[str, Any]],
    samples: Dict[str, Any],
    manifest: Dict[str, Any],
    planner: Dict[str, Any],
    question: str = '',
) -> List[Dict[str, Any]]:
    # 规则匹配只基于 sampler 产出的有界样本，不读取原始日志全文。
    # 后续若要扩大范围，应走 Deep 模式，而不是在首轮放开成本边界。
    manifest_by_path = {str(f.get('path') or ''): f for f in (manifest.get('files') or [])}
    events: List[Dict[str, Any]] = []
    for file_item in samples.get('files') or []:
        if file_item.get('skipped'):
            continue
        path = str(file_item.get('path') or '')
        if not _path_allowed(path, planner):
            continue
        manifest_item = manifest_by_path.get(path, {})
        file_text = '\n'.join(str(s.get('content') or '') for s in (file_item.get('samples') or []))
        if not file_text:
            continue
        for pack in rule_packs:
            for rule in pack.get('rules') or []:
                if not _file_matches_rule(rule, path, str(file_item.get('kind') or manifest_item.get('kind') or '')):
                    continue
                hit = _match_rule_text(rule, file_text)
                if not hit:
                    continue
                sample, snippet, line_range = _best_sample_hit(file_item.get('samples') or [], hit)
                event = _make_event(pack, rule, file_item, manifest_item, hit, sample, snippet, line_range)
                event['relevance'] = score_event_relevance(event, planner, question)
                events.append(event)
    events.sort(key=lambda e: (float(e.get('relevance', {}).get('score') or 0), _severity_rank(e.get('severity'))), reverse=True)
    return events[:80]


def score_event_relevance(event: Dict[str, Any], planner: Dict[str, Any], question: str = '') -> Dict[str, Any]:
    # 严重级别不能单独决定结论。比如用户问 RDM 锁机时，微信 crash 只能算噪声；
    # 这里通过 bundle/question focus 给相关证据加权，并主动降低无关第三方崩溃分数。
    score = 0.15
    reasons: List[str] = []
    question_text = (question or '').lower()
    candidate_bundles = set(planner.get('candidate_bundle_ids') or [])
    bundles = set(event.get('source_bundle_ids') or [])
    focus_terms = _bundle_focus_terms(candidate_bundles)
    question_has_focus = _contains_any_focus(question_text, focus_terms)
    event_has_focus = _contains_any_focus(_event_search_text(event), focus_terms)

    issue_types = set(planner.get('issue_types') or [])
    if event.get('issue_type') in issue_types:
        score += 0.25
        reasons.append('issue_type matched planner')
    candidate_paths = [str(x).lower() for x in (planner.get('candidate_log_paths') or [])]
    path = str(event.get('path') or '').lower()
    if any(p and (p in path or path.endswith(p)) for p in candidate_paths):
        score += 0.2
        reasons.append('path matched planner candidate')
    if candidate_bundles and bundles.intersection(candidate_bundles):
        score += 0.2
        reasons.append('bundle matched planner candidate')
    terms = [str(x).lower() for x in (event.get('matched_terms') or []) if str(x)]
    if any(t and (t in question_text or _term_matches_focus(t, focus_terms)) for t in terms):
        score += 0.15
        reasons.append('matched term appears in user/focus text')
    if event.get('severity') in {'fatal', 'high'}:
        score += 0.1
        reasons.append('high severity signal')
    if candidate_bundles and question_has_focus and not bundles.intersection(candidate_bundles) and not event_has_focus:
        score -= 0.25
        reasons.append('demoted: no requested bundle/question focus overlap')
    confidence = float(planner.get('confidence') or 0)
    score += min(0.1, confidence * 0.1)
    return {'score': round(max(0.0, min(1.0, score)), 3), 'reasons': reasons or ['local rule match']}


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise AndroidAnalysisError('rule_artifact_missing', f'{path.name} does not exist.')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise AndroidAnalysisError('rule_artifact_invalid', f'{path.name} is invalid JSON.') from exc
    if not isinstance(data, dict):
        raise AndroidAnalysisError('rule_artifact_invalid', f'{path.name} must be a JSON object.')
    return data


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _path_allowed(path: str, planner: Dict[str, Any]) -> bool:
    lower = path.lower()
    for excluded in planner.get('exclude_paths') or []:
        ex = str(excluded).lower()
        if ex and (ex in lower or lower.endswith(ex)):
            return False
    candidates = [str(x).lower() for x in (planner.get('candidate_log_paths') or []) if x]
    if not candidates:
        return True
    return any(c in lower or lower.endswith(c) or Path(lower).name == Path(c).name for c in candidates)


def _file_matches_rule(rule: Dict[str, Any], path: str, kind: str) -> bool:
    match = rule.get('match') if isinstance(rule.get('match'), dict) else {}
    paths = [str(x).lower() for x in (match.get('paths') or []) if x]
    kinds = [str(x) for x in (match.get('kinds') or []) if x]
    if paths and not any(p in path.lower() for p in paths):
        return False
    if kinds and kind not in kinds:
        return False
    return True


def _match_rule_text(rule: Dict[str, Any], text: str) -> Dict[str, Any] | None:
    match = rule.get('match') if isinstance(rule.get('match'), dict) else {}
    terms: List[str] = []
    for keyword in match.get('keywords') or []:
        kw = str(keyword or '').strip()
        if kw and _keyword_in_text(kw, text):
            terms.append(kw)
    for package in match.get('packages') or []:
        pkg = str(package or '').strip()
        if pkg and pkg.lower() in text.lower():
            terms.append(pkg)
    regex_hits: List[str] = []
    for pattern in match.get('regex') or []:
        try:
            found = re.search(str(pattern), text, flags=re.IGNORECASE | re.MULTILINE)
        except re.error:
            continue
        if found:
            regex_hits.append(str(pattern))
            if found.group(0) and found.group(0) not in terms:
                terms.append(found.group(0)[:160])
    if not terms and not regex_hits:
        return None
    return {'matched_terms': _dedupe(terms)[:10], 'regex_hits': regex_hits[:10]}


def _best_sample_hit(
    samples: List[Dict[str, Any]],
    hit: Dict[str, Any],
) -> Tuple[Dict[str, Any], str, List[int]]:
    terms = [str(x).lower() for x in (hit.get('matched_terms') or []) if x]
    for sample in samples:
        content = str(sample.get('content') or '')
        lower = content.lower()
        if any(term and term in lower for term in terms):
            return sample, _trim_snippet(content), [int(sample.get('start_line') or 1), int(sample.get('end_line') or 1)]
    sample = samples[0] if samples else {}
    content = str(sample.get('content') or '')
    return sample, _trim_snippet(content), [int(sample.get('start_line') or 1), int(sample.get('end_line') or 1)]


def _make_event(
    pack: Dict[str, Any],
    rule: Dict[str, Any],
    file_item: Dict[str, Any],
    manifest_item: Dict[str, Any],
    hit: Dict[str, Any],
    sample: Dict[str, Any],
    snippet: str,
    line_range: List[int],
) -> Dict[str, Any]:
    bundle_ids = rule.get('source_bundle_ids') or pack.get('source_bundle_ids') or []
    return {
        'rule_pack_id': pack.get('id'),
        'rule_id': rule.get('id'),
        'rule_title': rule.get('title') or rule.get('id'),
        'issue_type': rule.get('issue_type') or 'generic_log_error',
        'severity': rule.get('severity') or 'medium',
        'source_bundle_ids': [str(x) for x in bundle_ids if x],
        'tags': [str(x) for x in (rule.get('tags') or []) if x],
        'path': file_item.get('path'),
        'kind': file_item.get('kind') or manifest_item.get('kind') or 'unknown',
        'line_range': line_range,
        'sample_type': sample.get('type') or '',
        'matched_terms': hit.get('matched_terms') or [],
        'regex_hits': hit.get('regex_hits') or [],
        'snippet': snippet,
    }


def _trim_snippet(text: str, max_chars: int = 1800) -> str:
    text = (text or '').strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + '\n...'


def _keyword_in_text(keyword: str, text: str) -> bool:
    if re.fullmatch(r'[A-Za-z0-9_.$-]+', keyword):
        pattern = r'(?<![A-Za-z0-9_])' + re.escape(keyword) + r'(?![A-Za-z0-9_])'
        return re.search(pattern, text, flags=re.IGNORECASE) is not None
    return keyword.lower() in text.lower()


def _dedupe(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _severity_rank(value: Any) -> int:
    return {'fatal': 5, 'high': 4, 'medium': 3, 'low': 2}.get(str(value or '').lower(), 1)


def _bundle_focus_terms(bundle_ids: Iterable[str]) -> List[str]:
    terms = set()
    for bundle_id in bundle_ids:
        bid = str(bundle_id or '').strip().lower()
        if not bid:
            continue
        terms.add(bid)
        terms.update(x for x in re.split(r'[-_.\s]+', bid) if x and x not in {'android', 'app', 'com'})
        if bid == 'android-rdm':
            terms.update(
                {
                    'rdm',
                    'realtimedevicemanager',
                    'device lock',
                    'devicelock',
                    'devicepolicy',
                    'device policy',
                    'lock',
                    'unlock',
                    'provision',
                    '锁',
                    '锁定',
                    '锁机',
                    '解锁',
                }
            )
    return sorted(terms, key=len, reverse=True)


def _contains_any_focus(text: str, focus_terms: Iterable[str]) -> bool:
    if not text:
        return False
    return any(term and _keyword_in_text(str(term), text) for term in focus_terms)


def _term_matches_focus(term: str, focus_terms: Iterable[str]) -> bool:
    term = (term or '').lower()
    if not term:
        return False
    for focus in focus_terms:
        f = str(focus or '').lower()
        if not f:
            continue
        if term == f or f in term or term in f:
            return True
    return False


def _event_search_text(event: Dict[str, Any]) -> str:
    parts = [
        event.get('rule_id'),
        event.get('rule_title'),
        event.get('issue_type'),
        event.get('path'),
        event.get('kind'),
        event.get('snippet'),
    ]
    parts.extend(event.get('tags') or [])
    parts.extend(event.get('matched_terms') or [])
    parts.extend(event.get('regex_hits') or [])
    return ' '.join(str(x or '') for x in parts).lower()
