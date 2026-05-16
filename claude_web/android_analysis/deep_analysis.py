"""Deep-mode evidence expansion and report generation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from ..skill_bundle_index import as_posix, configured_path, read_skill_metadata
from .code_scope import collect_candidate_bundle_ids, collect_code_context, resolve_code_scopes
from .models import AndroidAnalysisError
from .planner import _run_claude_cli, _trace_ai_stream, _trace_ai_token_usage
from .reporter import _clean_report


_PROMPT_PATH = Path(__file__).resolve().parent / 'prompts' / 'deep_pass.md'
_SKILL_FILENAME = 'SKILL.md'
_GUIDANCE_FILENAMES = ('CLAUDE.md', 'AGENTS.md', 'AGENT.md')
_MAX_SELECTED_SKILLS = 4
_MAX_SELECTED_GUIDANCE = 6
_MAX_SKILL_CHARS = 14000
_MAX_GUIDANCE_CHARS = 10000


def build_deep_evidence_pack(
    artifacts_dir: Path,
    extracted_dir: Path,
    question: str,
    configured_bundles: Iterable[Dict[str, Any]],
    preferred_paths: Dict[str, List[str]] | None = None,
    debug_trace: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    # Deep 模式才允许扩展读取原始日志片段和白名单代码目录；
    # 代码范围必须由 bundle id 反查配置得到，不能接受前端传入任意本地路径。
    artifacts_dir = Path(artifacts_dir)
    configured_bundle_list = list(configured_bundles or [])
    planner = _read_json(artifacts_dir / 'planner_result.json')
    matched = _read_json(artifacts_dir / 'matched_rules.json')
    first_report = _read_text_optional(artifacts_dir / 'final_report.md')
    bundle_ids = collect_candidate_bundle_ids(planner, matched)
    deep_hints = _collect_deep_hints(planner, matched)
    merged_preferred_paths = _merge_preferred_paths(preferred_paths or {}, deep_hints.get('preferred_paths_by_bundle') or {})
    scope_result = resolve_code_scopes(bundle_ids, configured_bundle_list, preferred_paths=merged_preferred_paths)
    project_context = _collect_project_context(bundle_ids, configured_bundle_list, scope_result, deep_hints, question, planner)
    keywords = _keywords_for_deep(question, planner, matched, deep_hints)
    code_files = collect_code_context(scope_result, keywords=keywords)
    log_context = _collect_log_context(Path(extracted_dir), matched.get('events') or [])

    md = _render_deep_markdown(
        question,
        planner,
        matched,
        first_report,
        scope_result,
        log_context,
        code_files,
        deep_hints,
        project_context,
    )
    (artifacts_dir / 'deep_evidence_pack.md').write_text(md, encoding='utf-8')

    summary = {
        'version': 1,
        'candidate_bundle_ids': bundle_ids,
        'code_scope': {
            'allowed': scope_result.get('allowed', False),
            'scopes': [
                {
                    'bundle_id': s.get('bundle_id'),
                    'title': s.get('title'),
                    'root_count': len(s.get('roots') or []),
                    'preferred_path_count': len(s.get('preferred_paths') or []),
                }
                for s in (scope_result.get('scopes') or [])
            ],
            'denied': scope_result.get('denied') or [],
        },
        'log_context_count': len(log_context),
        'code_file_count': len(code_files),
        'has_code_context': bool(code_files),
        'selected_skill_count': len(project_context.get('skills') or []),
        'selected_guidance_count': len(project_context.get('guidance') or []),
        'selected_skills': _public_context_items(project_context.get('skills') or []),
        'selected_guidance': _public_context_items(project_context.get('guidance') or []),
        'deep_hints': {
            'code_search_terms': deep_hints.get('code_search_terms', [])[:80],
            'tier2_scope_terms': deep_hints.get('tier2_scope_terms', [])[:80],
            'search_order': deep_hints.get('search_order', [])[:40],
            'exact_logs': deep_hints.get('exact_logs', [])[:40],
            'related_skills': deep_hints.get('related_skills', [])[:40],
            'case_tags': deep_hints.get('case_tags', [])[:40],
            'claude_md_candidates': deep_hints.get('claude_md_candidates', [])[:40],
            'preferred_paths_by_bundle': {
                bundle_id: paths[:40]
                for bundle_id, paths in (deep_hints.get('preferred_paths_by_bundle') or {}).items()
            },
        },
    }
    with open(artifacts_dir / 'deep_evidence_pack.json', 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    if debug_trace:
        debug_trace(
            'deep_scoping',
            'deep_evidence_result',
            {
                **summary,
                'keywords': keywords,
                'scope_result': scope_result,
                'merged_preferred_paths': merged_preferred_paths,
                'log_context_preview': log_context[:10],
                'code_file_preview': [
                    {
                        'path': item.get('path'),
                        'bundle_id': item.get('bundle_id'),
                        'matched_keywords': item.get('matched_keywords') or [],
                    }
                    for item in code_files[:20]
                ],
                'selected_skills': _public_context_items(project_context.get('skills') or []),
                'selected_guidance': _public_context_items(project_context.get('guidance') or []),
                'deep_evidence_chars': len(md),
                'deep_evidence_preview': md,
            },
        )
    return summary


def generate_deep_report(
    artifacts_dir: Path,
    question: str,
    cli_path: str = '',
    timeout_seconds: int = 45,
    enable_ai: bool = True,
    ai_runner: Optional[Callable[[str], str]] = None,
    debug_trace: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    # Deep 报告仍然只读取 deep_evidence_pack，而不是开放整个 extracted 目录给 CLI。
    # 这样可以扩大证据范围，同时保留可审计的输入边界。
    artifacts_dir = Path(artifacts_dir)
    deep_pack = _read_text(artifacts_dir / 'deep_evidence_pack.md')
    deep_summary = _read_json(artifacts_dir / 'deep_evidence_pack.json')
    matched = _read_json(artifacts_dir / 'matched_rules.json')
    planner = _read_json(artifacts_dir / 'planner_result.json')
    first_report = _read_text_optional(artifacts_dir / 'final_report.md')

    errors: List[Dict[str, str]] = []
    mode = 'fallback'
    try:
        if debug_trace:
            debug_trace(
                'deep_reporting',
                'deep_report_input',
                {
                    'question': question,
                    'enable_ai': enable_ai,
                    'deep_pack_chars': len(deep_pack),
                    'deep_summary': deep_summary,
                    'planner_result': planner,
                    'matched_event_count': matched.get('event_count', 0),
                    'first_report_chars': len(first_report),
                },
            )
        if enable_ai:
            prompt = build_deep_report_prompt(question, deep_pack, deep_summary, planner, matched, first_report)
            if debug_trace:
                debug_trace('deep_reporting', 'deep_report_prompt', {'prompt_chars': len(prompt), 'prompt_preview': prompt})
            if ai_runner:
                report = ai_runner(prompt)
                _trace_ai_token_usage(debug_trace, 'deep_reporting', 'deep_report', prompt, report)
            else:
                report = _run_claude_cli(
                    prompt,
                    cli_path,
                    timeout_seconds,
                    artifacts_dir,
                    stream_callback=lambda item: _trace_ai_stream(debug_trace, 'deep_reporting', 'deep_report', item),
                    usage_callback=lambda usage: _trace_ai_token_usage(
                        debug_trace,
                        'deep_reporting',
                        'deep_report',
                        prompt,
                        usage.get('output_text', ''),
                        usage=usage,
                    ),
                )
            if debug_trace:
                debug_trace('deep_reporting', 'deep_report_raw_output', {'output_chars': len(report or ''), 'output_preview': report or ''})
            report = _clean_report(report)
            if not report:
                raise AndroidAnalysisError('deep_report_empty_output', 'Claude returned empty Deep report.')
            mode = 'ai'
        else:
            report = build_fallback_deep_report(question, deep_summary, matched)
    except AndroidAnalysisError as e:
        errors.append({'code': e.code, 'message': e.message})
        report = build_fallback_deep_report(question, deep_summary, matched)
    except Exception as e:
        errors.append({'code': 'deep_report_unexpected_error', 'message': str(e)})
        report = build_fallback_deep_report(question, deep_summary, matched)

    (artifacts_dir / 'deep_report.md').write_text(report, encoding='utf-8')
    meta = {
        'version': 1,
        'report_mode': mode,
        'errors': errors,
        'has_report': bool(report.strip()),
        'has_code_context': bool(deep_summary.get('has_code_context')),
        'log_context_count': deep_summary.get('log_context_count', 0),
        'code_file_count': deep_summary.get('code_file_count', 0),
    }
    with open(artifacts_dir / 'deep_report_meta.json', 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    if debug_trace:
        debug_trace('deep_reporting', 'deep_report_result', {**meta, 'report_preview': report})
    return meta


def build_deep_report_prompt(
    question: str,
    deep_pack: str,
    deep_summary: Dict[str, Any],
    planner: Dict[str, Any],
    matched_rules: Dict[str, Any],
    first_report: str,
) -> str:
    instruction = _PROMPT_PATH.read_text(encoding='utf-8')
    payload = {
        'question': question or '',
        'planner_result': planner,
        'matched_rule_summary': {
            'event_count': matched_rules.get('event_count', 0),
            'events': (matched_rules.get('events') or [])[:20],
        },
        'deep_summary': deep_summary,
        'first_report': first_report[:12000],
        'deep_evidence_pack_markdown': deep_pack[:50000],
    }
    return instruction + '\n\nInput JSON:\n' + json.dumps(payload, ensure_ascii=False, indent=2)


def build_fallback_deep_report(
    question: str,
    deep_summary: Dict[str, Any],
    matched_rules: Dict[str, Any],
) -> str:
    events = matched_rules.get('events') or []
    lines: List[str] = []
    lines.append('# Android 问题 Deep 分析报告')
    lines.append('')
    lines.append('## 结论')
    if events:
        best = max(float((e.get('relevance') or {}).get('score') or 0) for e in events)
        if best >= 0.75:
            lines.append('Deep 模式已扩展证据，当前最高相关性证据较强，但仍需 Verifier 判断报告是否过度推断。')
        else:
            lines.append('Deep 模式未找到足够强的直接证据，当前更适合保留为“疑似/待确认”结论。')
    else:
        lines.append('Deep 模式没有可用规则证据，建议补充更明确的问题时间窗口、包名或业务日志。')
    lines.append('')
    lines.append('## Deep 范围')
    scope = deep_summary.get('code_scope') or {}
    if scope.get('allowed'):
        scopes = ', '.join(s.get('bundle_id') or '' for s in (scope.get('scopes') or []))
        lines.append(f'- 已按白名单读取代码范围：`{scopes}`。')
        lines.append(f'- 代码上下文文件数：`{deep_summary.get("code_file_count", 0)}`。')
    else:
        lines.append('- 未读取代码：没有命中可在 `claude_web_paths.config.json` 中校验通过的 bundle。')
    lines.append(f'- 扩展日志片段数：`{deep_summary.get("log_context_count", 0)}`。')
    if scope.get('denied'):
        lines.append('- 被拒绝的代码范围：' + '; '.join(f"{d.get('bundle_id')}={d.get('reason')}" for d in scope.get('denied') or []))
    lines.append('')
    lines.append('## 用户问题')
    lines.append((question or '').strip() or '未提供')
    lines.append('')
    lines.append('## 建议下一步')
    lines.append('- 查看 `verifier_result.json` 判断当前结论是否被证据支撑。')
    lines.append('- 若仍为低置信，补充复现时间点、RDM 业务日志 TAG、目标包名或截图。')
    return '\n'.join(lines).rstrip() + '\n'


def _collect_project_context(
    bundle_ids: Iterable[str],
    configured_bundles: Iterable[Dict[str, Any]],
    scope_result: Dict[str, Any],
    deep_hints: Dict[str, Any],
    question: str = '',
    planner: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """按命中的 bundle 白名单读取 Deep 阶段可用的项目 Skill 与指南。

    这里不依赖 Claude CLI 对 `.claude/skills` 的原生发现能力，而是把
    `claude_web_paths.config.json` 命中的项目专属 Skill/CLAUDE.md/AGENTS.md
    截断后写入 Deep Evidence Pack，保证离线报告和后续复现也能看到同一份上下文。
    """
    configured = {str(b.get('id') or ''): b for b in configured_bundles or [] if b.get('id')}
    allowed_scopes = {
        str(s.get('bundle_id') or ''): s
        for s in (scope_result.get('scopes') or [])
        if s.get('bundle_id')
    }
    selected_skills: List[Dict[str, Any]] = []
    selected_guidance: List[Dict[str, Any]] = []
    query_terms = _context_query_terms(question, planner or {}, deep_hints)

    for bundle_id in _dedupe_strings(bundle_ids):
        scope = allowed_scopes.get(bundle_id)
        bundle = configured.get(bundle_id)
        if not scope or not bundle:
            continue
        roots = _scope_roots(scope)
        selected_skills.extend(_collect_bundle_skills(bundle_id, bundle, roots, deep_hints, query_terms))
        selected_guidance.extend(_collect_bundle_guidance(bundle_id, bundle, roots, deep_hints, query_terms))

    selected_skills = _dedupe_context_items(selected_skills)
    selected_guidance = _dedupe_context_items(selected_guidance)
    selected_skills.sort(key=lambda item: item.get('_score', 0), reverse=True)
    selected_guidance.sort(key=lambda item: item.get('_score', 0), reverse=True)
    for item in selected_skills + selected_guidance:
        item.pop('_score', None)
    return {
        'skills': selected_skills[:_MAX_SELECTED_SKILLS],
        'guidance': selected_guidance[:_MAX_SELECTED_GUIDANCE],
    }


def _collect_bundle_skills(
    bundle_id: str,
    bundle: Dict[str, Any],
    roots: List[Path],
    deep_hints: Dict[str, Any],
    query_terms: List[str] | None = None,
) -> List[Dict[str, Any]]:
    terms = query_terms or []
    related = [x.lower() for x in _context_hint_terms(deep_hints.get('related_skills') or [], ' '.join(terms[:60]).lower())]
    skill_files: List[tuple[Path, str]] = []

    for item in bundle.get('skills') or []:
        raw_path = configured_path(item)
        if not raw_path:
            continue
        try:
            configured = Path(raw_path).expanduser().resolve()
        except OSError:
            continue
        for skill_file in _candidate_skill_files(configured):
            if _is_under_any(skill_file, roots) or _is_explicit_configured_skill(item):
                skill_files.append((skill_file, 'configured'))

    # 兼容项目根目录下的 skills 以及 Claude 生态常见的 .claude/skills。
    for root in roots:
        for skill_root in (root / 'skills', root / '.claude' / 'skills'):
            for skill_file in _candidate_skill_files(skill_root):
                skill_files.append((skill_file, 'discovered'))

    out: List[Dict[str, Any]] = []
    for skill_file, source in skill_files:
        text, truncated = _read_limited_text(skill_file, _MAX_SKILL_CHARS)
        if not text:
            continue
        metadata = read_skill_metadata(skill_file)
        haystack = ' '.join(
            [
                metadata.get('id', ''),
                metadata.get('title', ''),
                metadata.get('summary', ''),
                skill_file.as_posix(),
                text[:6000],
            ]
        ).lower()
        score = 20 if any(token and token in haystack for token in related) else 5
        score += _relevance_score(haystack, terms, max_score=35)
        score += _skill_identity_bonus(metadata, skill_file, terms)
        if source == 'configured':
            score += 10
        out.append(
            {
                'kind': 'skill',
                'bundle_id': bundle_id,
                'id': metadata.get('id') or skill_file.parent.name,
                'title': metadata.get('title') or skill_file.parent.name,
                'summary': metadata.get('summary') or '',
                'path': as_posix(skill_file),
                'source': source,
                'reason': 'matched bundle Deep context; load project-specific triage steps before broad grep',
                'content_chars': len(text),
                'truncated': truncated,
                'content': text,
                '_score': score,
            }
        )
    return out


def _collect_bundle_guidance(
    bundle_id: str,
    bundle: Dict[str, Any],
    roots: List[Path],
    deep_hints: Dict[str, Any],
    query_terms: List[str] | None = None,
) -> List[Dict[str, Any]]:
    candidates: List[tuple[Path, str, int]] = []

    for raw in bundle.get('claude_md_paths') or []:
        try:
            path = Path(str(raw)).expanduser().resolve()
        except OSError:
            continue
        if _is_under_any(path, roots):
            candidates.append((path, 'configured', 30))

    for root in roots:
        for filename in _GUIDANCE_FILENAMES:
            candidate = root / filename
            if candidate.is_file():
                candidates.append((candidate, 'root', 25))

    for raw in deep_hints.get('claude_md_candidates') or []:
        for candidate in _resolve_guidance_candidate(str(raw), roots):
            candidates.append((candidate, 'deep_hint', 35))

    out: List[Dict[str, Any]] = []
    for path, source, score in candidates:
        text, truncated = _read_limited_text(path, _MAX_GUIDANCE_CHARS)
        if not text:
            continue
        haystack = f'{path.as_posix()} {text[:5000]}'.lower()
        score += _relevance_score(haystack, query_terms or [], max_score=20)
        out.append(
            {
                'kind': 'guidance',
                'bundle_id': bundle_id,
                'id': path.name,
                'title': path.name,
                'summary': '',
                'path': as_posix(path),
                'source': source,
                'reason': 'matched bundle project guidance; use before expanding code/log search',
                'content_chars': len(text),
                'truncated': truncated,
                'content': text,
                '_score': score,
            }
        )
    return out


def _context_query_terms(question: str, planner: Dict[str, Any], deep_hints: Dict[str, Any]) -> List[str]:
    primary_values: List[str] = [question or '']
    primary_values.extend(planner.get('issue_types') or [])
    primary_values.extend(planner.get('candidate_keywords') or [])
    primary_values.extend(planner.get('candidate_rule_packs') or [])
    primary_values.extend(_flatten_candidate_entities(planner.get('candidate_entities')))
    text = ' '.join(str(v or '') for v in primary_values).lower()
    values: List[str] = list(primary_values)
    values.extend(_context_hint_terms(deep_hints.get('related_skills') or [], text))
    values.extend(_context_hint_terms(deep_hints.get('case_tags') or [], text))
    for item in deep_hints.get('exact_logs') or []:
        if isinstance(item, dict):
            values.extend(item.values())
    extras: List[str] = []
    if any(token in text for token in ('xts', 'cts', 'tradefed', 'testhideallapps', 'test_result', '测试')):
        extras.extend(['cts', 'xts', 'test', 'devicepolicy', 'device policy'])
    if any(token in text for token in ('dlc', 'devicelock', 'device lock', 'checkin', 'check-in', 'lock', 'unlock', '锁机', '解锁', '激活')):
        extras.extend(['dlc', 'devicelock', 'device lock', 'checkin', 'check-in'])
    if any(token in text for token in ('mdm', 'nfc', 'bluetooth', '蓝牙', '策略', '禁用')):
        extras.extend(['mdm', 'honor', 'devicepolicy', 'device policy'])
    if any(token in text for token in ('pms', 'package', 'packages', 'hideallapps', 'launcher')):
        extras.extend(['pms', 'package', 'package manager', 'launcher'])
    if any(token in text for token in ('ams', 'activitymanager', 'activity manager', 'anr')):
        extras.extend(['ams', 'activitymanager', 'activity manager'])

    terms: List[str] = []
    _append_unique(terms, extras, limit=160)
    for raw in values:
        s = str(raw or '').strip().lower()
        if len(s) >= 3:
            _append_unique(terms, [s], limit=160)
        for token in _term_tokens(s):
            _append_unique(terms, [token], limit=160)
    return terms[:160]


_GENERIC_CONTEXT_HINT_TERMS = {
    'android-log-rule-builder',
    'framework',
    'functional',
    'stability',
    'xts',
    'memory',
    'performance',
}


def _context_hint_terms(values: Iterable[Any], primary_text: str) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in values:
        value = str(raw or '').strip()
        lower = value.lower()
        if not lower:
            continue
        if lower.startswith('android-log-rule-builder'):
            continue
        if lower in _GENERIC_CONTEXT_HINT_TERMS and lower not in primary_text:
            continue
        if lower in seen:
            continue
        seen.add(lower)
        out.append(value)
    return out


def _flatten_candidate_entities(value: Any) -> List[str]:
    out: List[str] = []
    if isinstance(value, dict):
        for item in value.values():
            out.extend(_flatten_candidate_entities(item))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            out.extend(_flatten_candidate_entities(item))
    elif value is not None:
        out.append(str(value))
    return out


def _term_tokens(text: str) -> List[str]:
    import re

    out = re.findall(r'[a-z0-9][a-z0-9_.:-]{2,}|[\u4e00-\u9fff]{2,}', text.lower())
    split_more: List[str] = []
    for token in out:
        split_more.append(token)
        split_more.extend(x for x in re.split(r'[-_.:/]+', token) if len(x) >= 3)
    return _dedupe_strings(split_more)


def _relevance_score(haystack: str, terms: List[str], max_score: int) -> int:
    if not haystack or not terms:
        return 0
    score = 0
    seen = set()
    for term in terms[:80]:
        t = str(term or '').strip().lower()
        if len(t) < 3 or t in seen:
            continue
        seen.add(t)
        if t in haystack:
            score += 6 if any(ch in t for ch in '-_.: ') or len(t) >= 8 else 3
        if score >= max_score:
            return max_score
    return min(score, max_score)


def _skill_identity_bonus(metadata: Dict[str, str], skill_file: Path, terms: List[str]) -> int:
    identity = ' '.join(
        [
            metadata.get('id', ''),
            metadata.get('title', ''),
            metadata.get('summary', ''),
            skill_file.parent.name,
        ]
    ).lower()
    term_text = ' '.join(terms[:50]).lower()
    bonus = 0
    if any(t in term_text for t in ('dlc', 'devicelock', 'device lock', 'checkin', 'check-in')) and any(
        marker in identity for marker in ('dlc', 'devicelock', 'device lock')
    ):
        bonus += 24
    if any(t in term_text for t in ('xts', 'cts', 'tradefed', 'testhideallapps')) and any(
        marker in identity for marker in ('cts', 'xts', 'tradefed')
    ):
        bonus += 24
    if any(t in term_text for t in ('devicepolicy', 'device policy', 'dpms')) and any(
        marker in identity for marker in ('devicepolicy', 'device policy', 'dpms')
    ):
        bonus += 16
    if any(t in term_text for t in ('mdm', 'nfc', 'bluetooth')) and any(marker in identity for marker in ('mdm', 'honor')):
        bonus += 16
    if any(t in term_text for t in ('pms', 'package manager', 'packagemanager')) and any(
        marker in identity for marker in ('pms', 'package manager', 'packagemanager')
    ):
        bonus += 16
    return bonus


def _candidate_skill_files(path: Path) -> Iterable[Path]:
    if path.is_file() and path.name.lower() == _SKILL_FILENAME.lower():
        yield path
        return
    if not path.is_dir():
        return
    direct = path / _SKILL_FILENAME
    if direct.is_file():
        yield direct
    try:
        children = sorted(path.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return
    for child in children:
        candidate = child / _SKILL_FILENAME
        if child.is_dir() and candidate.is_file():
            yield candidate


def _resolve_guidance_candidate(raw: str, roots: List[Path]) -> List[Path]:
    value = (raw or '').strip().replace('\\', '/')
    if not value:
        return []
    out: List[Path] = []
    try:
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            resolved = candidate.resolve()
            if resolved.is_file() and _is_under_any(resolved, roots):
                out.append(resolved)
            return out
    except OSError:
        return []
    if value.startswith('/') or '..' in value.split('/') or ':' in value.split('/')[0]:
        return []
    for root in roots:
        try:
            resolved = (root / value).resolve()
        except OSError:
            continue
        if resolved.is_file() and _is_under_any(resolved, roots):
            out.append(resolved)
    return out


def _scope_roots(scope: Dict[str, Any]) -> List[Path]:
    roots: List[Path] = []
    for raw in scope.get('roots') or []:
        try:
            root = Path(str(raw)).resolve()
        except OSError:
            continue
        if root.is_dir():
            roots.append(root)
    return roots


def _is_explicit_configured_skill(item: Any) -> bool:
    return isinstance(item, (str, dict))


def _is_under_any(path: Path, roots: List[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _read_limited_text(path: Path, max_chars: int) -> tuple[str, bool]:
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return '', False
    if len(text) <= max_chars:
        return text.strip(), False
    return text[:max_chars].rstrip() + '\n...[truncated]\n', True


def _dedupe_context_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for item in items:
        path = str(item.get('path') or '').lower()
        if not path or path in seen:
            continue
        seen.add(path)
        out.append(item)
    return out


def _public_context_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    public: List[Dict[str, Any]] = []
    for item in items:
        public.append({k: v for k, v in item.items() if k not in {'content'}})
    return public


def _collect_log_context(extracted_dir: Path, events: List[Dict[str, Any]], max_events: int = 12) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    root = extracted_dir.resolve()
    for event in events[:max_events]:
        rel = str(event.get('path') or '').replace('\\', '/')
        if not rel or rel.startswith('/') or '..' in rel.split('/') or ':' in rel.split('/')[0]:
            continue
        try:
            path = (root / rel).resolve()
            path.relative_to(root)
        except (OSError, ValueError):
            continue
        if not path.is_file():
            continue
        snippet = _read_line_window(path, event.get('line_range') or [])
        if not snippet:
            snippet = str(event.get('snippet') or '')[:3000]
        out.append(
            {
                'rule_id': event.get('rule_id'),
                'path': rel,
                'line_range': event.get('line_range') or [],
                'relevance': event.get('relevance') or {},
                'snippet': snippet,
            }
        )
    return out


def _read_line_window(path: Path, line_range: List[Any], padding: int = 20, max_chars: int = 8000) -> str:
    if len(line_range) < 2:
        return ''
    try:
        start = max(1, int(line_range[0]) - padding)
        end = max(start, int(line_range[1]) + padding)
    except (TypeError, ValueError):
        return ''
    lines: List[str] = []
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for idx, line in enumerate(f, start=1):
                if idx < start:
                    continue
                if idx > end:
                    break
                lines.append(line.rstrip('\n'))
    except OSError:
        return ''
    text = '\n'.join(lines).strip()
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + '\n...[log truncated]\n'
    return text


def _collect_deep_hints(planner: Dict[str, Any], matched: Dict[str, Any]) -> Dict[str, Any]:
    """合并 planner、规则包和命中事件给 Deep 阶段提供的检索线索。"""
    out: Dict[str, Any] = {
        'code_search_terms': [],
        'tier2_scope_terms': [],
        'search_order': [],
        'exact_logs': [],
        'preferred_paths_by_bundle': {},
        'related_skills': [],
        'claude_md_candidates': [],
        'case_tags': [],
    }
    candidate_bundles = [str(x) for x in (planner.get('candidate_bundle_ids') or []) if str(x).strip()]

    def add_hints(hints: Dict[str, Any], bundle_ids: Iterable[str]) -> None:
        if not isinstance(hints, dict):
            return
        _append_unique(out['code_search_terms'], hints.get('code_search_terms') or [], limit=160)
        _append_unique(out['tier2_scope_terms'], hints.get('tier2_scope_terms') or [], limit=160)
        _append_unique(out['search_order'], hints.get('search_order') or [], limit=80)
        _append_unique_dicts(out['exact_logs'], hints.get('exact_logs') or [], limit=120)
        _append_unique(out['related_skills'], hints.get('related_skills') or [], limit=80)
        _append_unique(out['claude_md_candidates'], hints.get('claude_md_candidates') or [], limit=80)
        _append_unique(out['case_tags'], hints.get('case_tags') or [], limit=80)
        preferred = [str(x).replace('\\', '/') for x in (hints.get('preferred_paths') or []) if str(x).strip()]
        if not preferred:
            return
        targets = [str(x) for x in bundle_ids if str(x).strip()] or candidate_bundles
        for bundle_id in targets:
            bucket = out['preferred_paths_by_bundle'].setdefault(bundle_id, [])
            _append_unique(bucket, preferred, limit=120)

    for item in matched.get('rule_pack_hints') or []:
        add_hints(item.get('deep_hints') if isinstance(item, dict) else {}, item.get('source_bundle_ids') or [])
    for event in matched.get('events') or []:
        add_hints(event.get('deep_hints') if isinstance(event, dict) else {}, event.get('source_bundle_ids') or [])

    return out


def _merge_preferred_paths(
    base: Dict[str, List[str]],
    hints: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    merged: Dict[str, List[str]] = {}
    for source in (base or {}, hints or {}):
        for bundle_id, paths in source.items():
            bucket = merged.setdefault(str(bundle_id), [])
            _append_unique(bucket, paths or [], limit=160)
    return merged


def _append_unique(target: List[str], values: Iterable[Any], limit: int = 120) -> None:
    seen = {str(x).lower() for x in target}
    for value in values:
        text = str(value or '').strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        target.append(text)
        if len(target) >= limit:
            return


def _append_unique_dicts(target: List[Dict[str, Any]], values: Iterable[Any], limit: int = 120) -> None:
    seen = {json.dumps(x, ensure_ascii=False, sort_keys=True).lower() for x in target if isinstance(x, dict)}
    for value in values:
        if not isinstance(value, dict):
            continue
        item = {
            str(k): str(v)
            for k, v in value.items()
            if str(k).strip() and str(v).strip()
        }
        if not item:
            continue
        key = json.dumps(item, ensure_ascii=False, sort_keys=True).lower()
        if key in seen:
            continue
        seen.add(key)
        target.append(item)
        if len(target) >= limit:
            return


def _dedupe_strings(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        text = str(value or '').strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _keywords_for_deep(
    question: str,
    planner: Dict[str, Any],
    matched: Dict[str, Any],
    deep_hints: Dict[str, Any] | None = None,
) -> List[str]:
    values: List[str] = []
    values.append(question or '')
    values.extend(planner.get('candidate_keywords') or [])
    if deep_hints:
        values.extend(deep_hints.get('code_search_terms') or [])
        values.extend(deep_hints.get('tier2_scope_terms') or [])
        values.extend(deep_hints.get('case_tags') or [])
    for event in matched.get('events') or []:
        values.append(event.get('rule_title') or '')
        values.extend(event.get('matched_terms') or [])
        values.extend(event.get('tags') or [])
    out: List[str] = []
    for raw in values:
        for token in str(raw or '').replace('.', ' ').replace('-', ' ').replace('_', ' ').split():
            token = token.strip().lower()
            if len(token) >= 3 and token not in out:
                out.append(token)
    return out[:80]


def _render_deep_markdown(
    question: str,
    planner: Dict[str, Any],
    matched: Dict[str, Any],
    first_report: str,
    scope_result: Dict[str, Any],
    log_context: List[Dict[str, Any]],
    code_files: List[Dict[str, Any]],
    deep_hints: Dict[str, Any] | None = None,
    project_context: Dict[str, Any] | None = None,
) -> str:
    lines: List[str] = []
    lines.append('# Android Deep Evidence Pack')
    lines.append('')
    lines.append('## User Question')
    lines.append((question or '').strip() or '(empty)')
    lines.append('')
    lines.append('## Planner / Rule Route')
    lines.append(f"- Issue types: {', '.join(planner.get('issue_types') or ['unknown'])}")
    lines.append(f"- Candidate bundles: {', '.join(planner.get('candidate_bundle_ids') or []) or '(none)'}")
    lines.append(f"- Matched events: {matched.get('event_count', 0)}")
    lines.append('')
    lines.append('## Deep Hints / Priority')
    hints = deep_hints or {}
    related_skills = hints.get('related_skills') or []
    code_terms = hints.get('code_search_terms') or []
    tier2_terms = hints.get('tier2_scope_terms') or []
    search_order = hints.get('search_order') or []
    exact_logs = hints.get('exact_logs') or []
    case_tags = hints.get('case_tags') or []
    claude_md = hints.get('claude_md_candidates') or []
    preferred = hints.get('preferred_paths_by_bundle') or {}
    lines.append('- Priority: use related skills and project guidance first, then rule hints, then broader code/log context.')
    lines.append(f"- Search order: {', '.join(search_order[:20]) or '(none)'}")
    lines.append(f"- Related skills: {', '.join(related_skills[:20]) or '(none)'}")
    lines.append(f"- Code search terms: {', '.join(code_terms[:40]) or '(none)'}")
    lines.append(f"- Tier2 scope terms: {', '.join(tier2_terms[:40]) or '(none)'}")
    lines.append(f"- Case tags: {', '.join(case_tags[:30]) or '(none)'}")
    lines.append(f"- CLAUDE.md candidates: {', '.join(claude_md[:20]) or '(none)'}")
    if exact_logs:
        lines.append('- Exact log hints:')
        for item in exact_logs[:20]:
            tag = str(item.get('tag') or '').strip()
            message = str(item.get('message') or '').strip()
            path = str(item.get('path') or '').strip()
            logger = str(item.get('logger') or '').strip()
            lines.append(f"  - `{tag}` / `{message}` ({logger or 'log'}, {path or 'unknown path'})")
    if preferred:
        for bundle_id, paths in preferred.items():
            lines.append(f"- Preferred paths for `{bundle_id}`: {', '.join(paths[:30])}")
    else:
        lines.append('- Preferred paths: (none)')
    lines.append('')
    lines.append('## Selected Project Skills')
    project = project_context or {}
    skills = project.get('skills') or []
    if not skills:
        lines.append('- No project-specific Skill was loaded for the matched bundle.')
    for idx, item in enumerate(skills, start=1):
        lines.append(f"### Skill {idx}: [{item.get('bundle_id')}] {item.get('title') or item.get('id')}")
        lines.append(f"- Path: `{item.get('path')}`")
        lines.append(f"- Reason: {item.get('reason')}")
        lines.append(f"- Source: {item.get('source')}, truncated: {bool(item.get('truncated'))}")
        lines.append('```markdown')
        lines.append(str(item.get('content') or '').strip())
        lines.append('```')
        lines.append('')
    lines.append('## Selected Project Guidance')
    guidance = project.get('guidance') or []
    if not guidance:
        lines.append('- No CLAUDE.md/AGENTS.md guidance was loaded for the matched bundle.')
    for idx, item in enumerate(guidance, start=1):
        lines.append(f"### Guidance {idx}: [{item.get('bundle_id')}] {item.get('title') or item.get('id')}")
        lines.append(f"- Path: `{item.get('path')}`")
        lines.append(f"- Reason: {item.get('reason')}")
        lines.append(f"- Source: {item.get('source')}, truncated: {bool(item.get('truncated'))}")
        lines.append('```markdown')
        lines.append(str(item.get('content') or '').strip())
        lines.append('```')
        lines.append('')
    lines.append('## Prior First Report')
    lines.append(first_report[:6000].strip() or '(missing)')
    lines.append('')
    lines.append('## Code Scope')
    if scope_result.get('allowed'):
        for scope in scope_result.get('scopes') or []:
            lines.append(f"- Allowed bundle `{scope.get('bundle_id')}` with {len(scope.get('roots') or [])} root(s).")
    else:
        lines.append('- No configured code scope was allowed.')
    for denied in scope_result.get('denied') or []:
        lines.append(f"- Denied `{denied.get('bundle_id')}`: {denied.get('reason')}")
    lines.append('')
    lines.append('## Expanded Log Evidence')
    if not log_context:
        lines.append('- No expanded log context available.')
    for idx, item in enumerate(log_context, start=1):
        lines.append(f"### Log {idx}: {item.get('path')}")
        lines.append(f"- Rule: {item.get('rule_id')}")
        lines.append(f"- Relevance: {(item.get('relevance') or {}).get('score', 0)}")
        lines.append('```text')
        lines.append(str(item.get('snippet') or '').strip())
        lines.append('```')
        lines.append('')
    lines.append('## Code Context')
    if not code_files:
        lines.append('- No code context was loaded.')
    for idx, item in enumerate(code_files, start=1):
        lines.append(f"### Code {idx}: [{item.get('bundle_id')}] {item.get('path')}")
        lines.append(f"- Size: {item.get('size', 0)} bytes")
        lines.append('```text')
        lines.append(str(item.get('snippet') or '').strip())
        lines.append('```')
        lines.append('')
    lines.append('## Boundary')
    lines.append('Deep mode may read only code scopes validated by bundle id and configured paths. Do not infer from files outside this pack.')
    return '\n'.join(lines).rstrip() + '\n'


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise AndroidAnalysisError('deep_artifact_missing', f'{path.name} does not exist.')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise AndroidAnalysisError('deep_artifact_invalid', f'{path.name} is invalid JSON.') from exc
    if not isinstance(data, dict):
        raise AndroidAnalysisError('deep_artifact_invalid', f'{path.name} must be a JSON object.')
    return data


def _read_text(path: Path) -> str:
    if not path.is_file():
        raise AndroidAnalysisError('deep_artifact_missing', f'{path.name} does not exist.')
    return path.read_text(encoding='utf-8')


def _read_text_optional(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8') if path.is_file() else ''
    except OSError:
        return ''
