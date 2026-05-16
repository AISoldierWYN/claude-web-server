"""Local rule matching for Android issue analysis artifacts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Tuple

from .models import AndroidAnalysisError
from .rule_loader import load_rule_packs


DEEP_HINT_FIELDS = (
    'code_search_terms',
    'tier2_scope_terms',
    'search_order',
    'preferred_paths',
    'related_skills',
    'claude_md_candidates',
    'case_tags',
)
DEEP_HINT_OBJECT_FIELDS = (
    'exact_logs',
)


def run_rule_matching(
    artifacts_dir: Path,
    knowledge_dir: Path,
    question: str = '',
    debug_trace: Callable[[str, str, Dict[str, Any]], None] | None = None,
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
    debug_stats = _new_match_debug_stats()
    if debug_trace:
        debug_trace(
            'matching_rules',
            'rule_pack_selection',
            {
                'candidate_rule_packs': planner.get('candidate_rule_packs') or [],
                'candidate_bundle_ids': planner.get('candidate_bundle_ids') or [],
                'loaded_rule_packs': [
                    {
                        'id': pack.get('id'),
                        'source': pack.get('source'),
                        'source_file': pack.get('source_file'),
                        'source_bundle_ids': pack.get('source_bundle_ids') or [],
                        'declared_bundle_id': pack.get('declared_bundle_id') or '',
                        'rule_count': len(pack.get('rules') or []),
                        'deep_hint_counts': _deep_hint_counts(_pack_deep_hints(pack)),
                    }
                    for pack in packs
                ],
            },
        )
    events = match_rule_packs(packs, samples, manifest, planner, question, debug_stats=debug_stats)
    rule_pack_hints = collect_rule_pack_hints(packs)
    result = {
        'version': 1,
        'rule_pack_count': len(packs),
        'event_count': len(events),
        'rule_pack_hints': rule_pack_hints,
        'events': events,
    }
    _write_json(artifacts_dir / 'matched_rules.json', result)
    if debug_trace:
        debug_trace(
            'matching_rules',
            'matching_result',
            {
                **debug_stats,
                'event_count': len(events),
                'top_events': [
                    {
                        'rule_pack_id': event.get('rule_pack_id'),
                        'rule_id': event.get('rule_id'),
                        'issue_type': event.get('issue_type'),
                        'severity': event.get('severity'),
                        'path': event.get('path'),
                        'line_range': event.get('line_range') or [],
                        'matched_terms': event.get('matched_terms') or [],
                        'regex_hits': event.get('regex_hits') or [],
                        'deep_hints': _deep_hint_counts(event.get('deep_hints') or {}),
                        'hit_visibility': event.get('hit_visibility') or {},
                        'relevance': event.get('relevance') or {},
                        'snippet_preview': str(event.get('snippet') or '')[:800],
                    }
                    for event in events[:20]
                ],
            },
        )
    return result


def match_rule_packs(
    rule_packs: Iterable[Dict[str, Any]],
    samples: Dict[str, Any],
    manifest: Dict[str, Any],
    planner: Dict[str, Any],
    question: str = '',
    debug_stats: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    # 规则匹配只基于 sampler 产出的有界样本，不读取原始日志全文。
    # 后续若要扩大范围，应走 Deep 模式，而不是在首轮放开成本边界。
    manifest_by_path = {str(f.get('path') or ''): f for f in (manifest.get('files') or [])}
    events: List[Dict[str, Any]] = []
    for file_item in samples.get('files') or []:
        if debug_stats is not None:
            debug_stats['sample_files_seen'] += 1
        if file_item.get('skipped'):
            if debug_stats is not None:
                debug_stats['sample_files_skipped'] += 1
            continue
        path = str(file_item.get('path') or '')
        if not _path_allowed(path, planner):
            if debug_stats is not None:
                debug_stats['sample_files_excluded_by_planner'] += 1
            continue
        if debug_stats is not None:
            debug_stats['sample_files_allowed'] += 1
        manifest_item = manifest_by_path.get(path, {})
        file_text = '\n'.join(str(s.get('content') or '') for s in (file_item.get('samples') or []))
        if not file_text:
            if debug_stats is not None:
                debug_stats['sample_files_without_text'] += 1
            continue
        for pack in rule_packs:
            if debug_stats is not None:
                debug_stats['rule_packs_iterated'] += 1
            for rule in pack.get('rules') or []:
                if debug_stats is not None:
                    debug_stats['rules_considered'] += 1
                if not _file_matches_rule(rule, path, str(file_item.get('kind') or manifest_item.get('kind') or '')):
                    if debug_stats is not None:
                        debug_stats['rules_rejected_by_file_filter'] += 1
                    continue
                hit = _match_rule_text(rule, file_text, debug_stats=debug_stats)
                if not hit:
                    if debug_stats is not None:
                        debug_stats['rules_without_text_hit'] += 1
                    continue
                sample, snippet, line_range, hit_visibility = _best_sample_hit(file_item.get('samples') or [], hit)
                event = _make_event(pack, rule, file_item, manifest_item, hit, sample, snippet, line_range, hit_visibility)
                event['relevance'] = score_event_relevance(event, planner, question)
                events.append(event)
                if debug_stats is not None:
                    debug_stats['rules_with_hit'] += 1
                    if not hit_visibility.get('snippet_contains_hit'):
                        debug_stats['rules_with_hidden_hit'] += 1
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
    if _event_matches_candidate_keyword(event, planner):
        score += 0.12
        reasons.append('matched planner candidate keyword')
    visibility = event.get('hit_visibility') if isinstance(event.get('hit_visibility'), dict) else {}
    if visibility and not visibility.get('snippet_contains_hit'):
        score -= 0.4
        reasons.append('demoted: matched term is not visible in snippet')
    if _only_generic_focus_hits(event):
        score -= 0.18
        reasons.append('demoted: only generic lock/device wording matched')
    if _broad_framework_or_generic_hit(event, planner, question_text):
        score -= 0.22
        reasons.append('demoted: broad framework/generic signal without planner keyword')
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


def _new_match_debug_stats() -> Dict[str, Any]:
    return {
        'sample_files_seen': 0,
        'sample_files_skipped': 0,
        'sample_files_excluded_by_planner': 0,
        'sample_files_allowed': 0,
        'sample_files_without_text': 0,
        'rule_packs_iterated': 0,
        'rules_considered': 0,
        'rules_rejected_by_file_filter': 0,
        'rules_without_text_hit': 0,
        'rules_with_hit': 0,
        'keyword_checks': 0,
        'package_checks': 0,
        'regex_checks': 0,
        'keyword_hits': {},
        'package_hits': {},
        'regex_hits': {},
        'rules_with_hidden_hit': 0,
    }


def _match_rule_text(
    rule: Dict[str, Any],
    text: str,
    debug_stats: Dict[str, Any] | None = None,
) -> Dict[str, Any] | None:
    match = rule.get('match') if isinstance(rule.get('match'), dict) else {}
    terms: List[str] = []
    for keyword in match.get('keywords') or []:
        kw = str(keyword or '').strip()
        if debug_stats is not None:
            debug_stats['keyword_checks'] += 1
        if kw and _keyword_in_text(kw, text):
            terms.append(kw)
            if debug_stats is not None:
                hits = debug_stats.setdefault('keyword_hits', {})
                hits[kw] = hits.get(kw, 0) + 1
    for package in match.get('packages') or []:
        pkg = str(package or '').strip()
        if debug_stats is not None:
            debug_stats['package_checks'] += 1
        if pkg and pkg.lower() in text.lower():
            terms.append(pkg)
            if debug_stats is not None:
                hits = debug_stats.setdefault('package_hits', {})
                hits[pkg] = hits.get(pkg, 0) + 1
    regex_hits: List[str] = []
    for pattern in match.get('regex') or []:
        if debug_stats is not None:
            debug_stats['regex_checks'] += 1
        try:
            found = re.search(str(pattern), text, flags=re.IGNORECASE | re.MULTILINE)
        except re.error:
            continue
        if found:
            regex_hits.append(str(pattern))
            if debug_stats is not None:
                hits = debug_stats.setdefault('regex_hits', {})
                key = str(pattern)
                hits[key] = hits.get(key, 0) + 1
            if found.group(0) and found.group(0) not in terms:
                terms.append(found.group(0)[:160])
    if not terms and not regex_hits:
        return None
    return {'matched_terms': _dedupe(terms)[:10], 'regex_hits': regex_hits[:10]}


def _best_sample_hit(
    samples: List[Dict[str, Any]],
    hit: Dict[str, Any],
) -> Tuple[Dict[str, Any], str, List[int], Dict[str, Any]]:
    terms = [str(x) for x in (hit.get('matched_terms') or []) if str(x)]
    for sample in samples:
        content = str(sample.get('content') or '')
        visible_terms = _visible_hit_terms(content, terms)
        if visible_terms:
            snippet = _trim_snippet_around_terms(content, visible_terms)
            return (
                sample,
                snippet,
                [int(sample.get('start_line') or 1), int(sample.get('end_line') or 1)],
                {
                    'sample_contains_hit': True,
                    'snippet_contains_hit': bool(_visible_hit_terms(snippet, visible_terms)),
                    'visible_terms': visible_terms,
                },
            )
    sample = samples[0] if samples else {}
    content = str(sample.get('content') or '')
    snippet = _trim_snippet(content)
    return (
        sample,
        snippet,
        [int(sample.get('start_line') or 1), int(sample.get('end_line') or 1)],
        {
            'sample_contains_hit': False,
            'snippet_contains_hit': bool(_visible_hit_terms(snippet, terms)),
            'visible_terms': _visible_hit_terms(snippet, terms),
        },
    )


def _make_event(
    pack: Dict[str, Any],
    rule: Dict[str, Any],
    file_item: Dict[str, Any],
    manifest_item: Dict[str, Any],
    hit: Dict[str, Any],
    sample: Dict[str, Any],
    snippet: str,
    line_range: List[int],
    hit_visibility: Dict[str, Any],
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
        'deep_hints': merge_deep_hints(_pack_deep_hints(pack), _rule_deep_hints(rule)),
        'hit_visibility': hit_visibility,
        'snippet': snippet,
    }


def collect_rule_pack_hints(packs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """返回每个规则包声明的 Deep 线索，供后续 evidence pack 合并消费。"""
    out: List[Dict[str, Any]] = []
    for pack in packs:
        hints = _pack_deep_hints(pack)
        if not any(hints.get(field) for field in DEEP_HINT_FIELDS + DEEP_HINT_OBJECT_FIELDS):
            continue
        out.append(
            {
                'rule_pack_id': pack.get('id'),
                'source_bundle_ids': pack.get('source_bundle_ids') or [],
                'deep_hints': hints,
            }
        )
    return out


def merge_deep_hints(*items: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {'version': 1}
    for field in DEEP_HINT_FIELDS:
        merged[field] = []
    for field in DEEP_HINT_OBJECT_FIELDS:
        merged[field] = []
    for item in items:
        hints = _normalize_deep_hints(item)
        for field in DEEP_HINT_FIELDS:
            merged[field].extend(hints.get(field) or [])
        for field in DEEP_HINT_OBJECT_FIELDS:
            merged[field].extend(hints.get(field) or [])
    for field in DEEP_HINT_FIELDS:
        merged[field] = _dedupe([str(x) for x in merged.get(field) or [] if str(x).strip()])[:120]
    for field in DEEP_HINT_OBJECT_FIELDS:
        merged[field] = _dedupe_dicts(merged.get(field) or [])[:120]
    return merged


def _pack_deep_hints(pack: Dict[str, Any]) -> Dict[str, Any]:
    metadata = pack.get('metadata') if isinstance(pack.get('metadata'), dict) else {}
    raw = metadata.get('deep_hints') if isinstance(metadata.get('deep_hints'), dict) else pack.get('deep_hints')
    return _normalize_deep_hints(raw if isinstance(raw, dict) else {})


def _rule_deep_hints(rule: Dict[str, Any]) -> Dict[str, Any]:
    raw = rule.get('deep_hints') if isinstance(rule.get('deep_hints'), dict) else {}
    return _normalize_deep_hints(raw)


def _normalize_deep_hints(hints: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {'version': 1}
    for field in DEEP_HINT_FIELDS:
        value = hints.get(field)
        if isinstance(value, str):
            items = [value]
        elif isinstance(value, (list, tuple, set)):
            items = [str(x) for x in value if str(x).strip()]
        else:
            items = []
        out[field] = _dedupe(items)[:120]
    for field in DEEP_HINT_OBJECT_FIELDS:
        out[field] = _dedupe_dicts(_normalize_hint_dicts(hints.get(field)))[:120]
    return out


def _deep_hint_counts(hints: Dict[str, Any]) -> Dict[str, int]:
    normalized = _normalize_deep_hints(hints)
    return {field: len(normalized.get(field) or []) for field in DEEP_HINT_FIELDS + DEEP_HINT_OBJECT_FIELDS}


def _normalize_hint_dicts(value: Any) -> List[Dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: List[Dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        normalized = {
            str(k): str(v)
            for k, v in item.items()
            if str(k).strip() and str(v).strip()
        }
        if normalized:
            out.append(normalized)
    return out


def _dedupe_dicts(values: Iterable[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        normalized = {
            str(k): str(v)
            for k, v in value.items()
            if str(k).strip() and str(v).strip()
        }
        if not normalized:
            continue
        key = json.dumps(normalized, ensure_ascii=False, sort_keys=True).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
    return out


def _trim_snippet(text: str, max_chars: int = 1800) -> str:
    text = (text or '').strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + '\n...'


def _trim_snippet_around_terms(text: str, terms: List[str], max_chars: int = 1800) -> str:
    text = (text or '').strip()
    if len(text) <= max_chars:
        return text
    lower = text.lower()
    hit_pos = -1
    for term in sorted((t for t in terms if t), key=len, reverse=True):
        hit_pos = lower.find(term.lower())
        if hit_pos >= 0:
            break
    if hit_pos < 0:
        return _trim_snippet(text, max_chars=max_chars)
    half = max_chars // 2
    start = max(0, hit_pos - half)
    end = min(len(text), start + max_chars)
    start = max(0, end - max_chars)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = '...\n' + snippet
    if end < len(text):
        snippet = snippet.rstrip() + '\n...'
    return snippet


def _visible_hit_terms(text: str, terms: List[str]) -> List[str]:
    lower = (text or '').lower()
    out: List[str] = []
    for term in terms:
        t = str(term or '').strip()
        if t and t.lower() in lower:
            out.append(t)
    return _dedupe(out)


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
        if bid == 'android-fwk':
            terms.update(
                {
                    'fwk',
                    'framework',
                    'frameworks',
                    'system_server',
                    'devicepolicy',
                    'device policy',
                    'devicepolicymanager',
                    'pms',
                    'packagemanager',
                    'package manager',
                    'ams',
                    'activitymanager',
                    'activity manager',
                    'managedprovisioning',
                    'provisioning',
                    'devicelock',
                    'device lock',
                    'dlc',
                    'honor',
                    'mdm',
                    'xts',
                    'cts',
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


def _only_generic_focus_hits(event: Dict[str, Any]) -> bool:
    terms = [str(x).lower() for x in (event.get('matched_terms') or []) if str(x)]
    if not terms:
        return False
    generic = {'lock', 'unlock', 'clear', 'check', 'policy', 'device', 'owner', 'bind', 'signal'}
    strong_markers = {
        'rdm',
        'realtimedevicemanager',
        'devicelock',
        'hnlock',
        'lockactivity',
        'com.hihonor.realtimedevicemanager',
    }
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
    normalized = {re.sub(r'[^a-z0-9]+', '', term) for term in terms}
    return bool(normalized) and all(term in generic for term in normalized)


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


def _event_matches_candidate_keyword(event: Dict[str, Any], planner: Dict[str, Any]) -> bool:
    text = _event_search_text(event)
    for keyword in planner.get('candidate_keywords') or []:
        kw = str(keyword or '').strip().lower()
        if len(kw) < 4 or kw in _GENERIC_SIGNAL_TERMS:
            continue
        if kw in text:
            return True
    return False


def _broad_framework_or_generic_hit(event: Dict[str, Any], planner: Dict[str, Any], question_text: str) -> bool:
    if _event_matches_candidate_keyword(event, planner):
        return False
    rule_id = str(event.get('rule_id') or '').lower()
    broad_rule = any(
        marker in rule_id
        for marker in (
            'stability-scoped-crash',
            'functional-tier2-scope',
            'memory-scoped',
            'system-server-watchdog',
        )
    )
    terms = [str(x).strip().lower() for x in (event.get('matched_terms') or []) if str(x).strip()]
    normalized = {re.sub(r'[^a-z0-9_]+', '', term) for term in terms if len(term) <= 80}
    only_generic_terms = bool(normalized) and all(term in _GENERIC_SIGNAL_TERMS for term in normalized)
    issue_types = set(planner.get('issue_types') or [])
    non_crash_route = issue_types and not issue_types.intersection(
        {'android_app_crash', 'android_system_server_crash', 'android_anr', 'android_native_crash'}
    )
    return (broad_rule or only_generic_terms) and (non_crash_route or not question_text)


_GENERIC_SIGNAL_TERMS = {
    'system',
    'system_server',
    'package',
    'pkg',
    'error',
    'failed',
    'failure',
    'exception',
    'null',
    'root',
    'policy',
    'device',
    'lock',
    'unlock',
    'permission',
    'permissions',
    'meminfo',
}
