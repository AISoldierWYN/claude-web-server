"""Deep-mode evidence expansion and report generation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .code_scope import collect_candidate_bundle_ids, collect_code_context, resolve_code_scopes
from .models import AndroidAnalysisError
from .planner import _run_claude_cli
from .reporter import _clean_report


_PROMPT_PATH = Path(__file__).resolve().parent / 'prompts' / 'deep_pass.md'


def build_deep_evidence_pack(
    artifacts_dir: Path,
    extracted_dir: Path,
    question: str,
    configured_bundles: Iterable[Dict[str, Any]],
    preferred_paths: Dict[str, List[str]] | None = None,
) -> Dict[str, Any]:
    # Deep 模式才允许扩展读取原始日志片段和白名单代码目录；
    # 代码范围必须由 bundle id 反查配置得到，不能接受前端传入任意本地路径。
    artifacts_dir = Path(artifacts_dir)
    planner = _read_json(artifacts_dir / 'planner_result.json')
    matched = _read_json(artifacts_dir / 'matched_rules.json')
    first_report = _read_text_optional(artifacts_dir / 'final_report.md')
    bundle_ids = collect_candidate_bundle_ids(planner, matched)
    scope_result = resolve_code_scopes(bundle_ids, configured_bundles, preferred_paths=preferred_paths)
    keywords = _keywords_for_deep(question, planner, matched)
    code_files = collect_code_context(scope_result, keywords=keywords)
    log_context = _collect_log_context(Path(extracted_dir), matched.get('events') or [])

    md = _render_deep_markdown(question, planner, matched, first_report, scope_result, log_context, code_files)
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
    }
    with open(artifacts_dir / 'deep_evidence_pack.json', 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return summary


def generate_deep_report(
    artifacts_dir: Path,
    question: str,
    cli_path: str = '',
    timeout_seconds: int = 45,
    enable_ai: bool = True,
    ai_runner: Optional[Callable[[str], str]] = None,
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
        if enable_ai:
            prompt = build_deep_report_prompt(question, deep_pack, deep_summary, planner, matched, first_report)
            report = ai_runner(prompt) if ai_runner else _run_claude_cli(prompt, cli_path, timeout_seconds, artifacts_dir)
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


def _keywords_for_deep(question: str, planner: Dict[str, Any], matched: Dict[str, Any]) -> List[str]:
    values: List[str] = []
    values.append(question or '')
    values.extend(planner.get('candidate_keywords') or [])
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
