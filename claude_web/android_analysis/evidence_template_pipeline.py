"""Evidence template generation pipeline for project-local Android knowledge packs.

This module intentionally keeps the expensive LLM step behind a narrow, auditable
candidate list. The default path is:

1. Scan source files for real logging calls.
2. Filter/rank candidates for a specific issue subcategory.
3. Ask Claude CLI to transform candidates into evidence templates.
4. Normalize and validate the draft before it can be merged into the formal pack.
"""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

from .expert_knowledge import DEFAULT_PROJECT_KNOWLEDGE_RELATIVE_PATH, load_project_knowledge_dir
from .expert_knowledge_builder import (
    EVIDENCE_TEMPLATE_FIELDS,
    write_evidence_templates_csv,
    write_evidence_templates_xlsx,
)


SOURCE_SUFFIXES = {'.java', '.kt'}
DEFAULT_SKIP_DIRS = {
    '.git',
    '.gradle',
    '.idea',
    '.claude',
    '.code-index',
    '.codebuddy',
    '.comagic',
    'build',
    'generated',
    'assets',
    'test_log',
    'refs',
    'skills',
}
LOG_CALL_RE = re.compile(
    r'\b(?P<logger>Log|Slog|HiLog|Hilog|Logger|RLog|MLog|Timber|LogUtils|RdmLog)\s*'
    r'\.\s*(?P<level>v|d|i|w|e|wtf|debug|info|warn|error)\s*\('
)
STRING_LITERAL_RE = re.compile(r'"((?:\\.|[^"\\])*)"')
TAG_ASSIGN_RE = re.compile(r'\b(?P<name>[A-Z_]*TAG)\s*=\s*"(?P<value>[^"]+)"')

SUBCATEGORY_KEYWORD_HINTS = {
    'device_identification_failed': [
        'imei',
        'device code',
        'devicecode',
        'telephony',
        'rpmb',
        'serial',
        'certificate',
        'chain verify',
        'device id',
        'android_id',
    ],
    'activation_failed': ['activation', 'activate', 'provision', 'checkin', 'enroll'],
    'activation_flow_display_abnormal': ['activation', 'privacy', 'agreement', 'statement', 'activity', 'page'],
    'lock_failed': ['lock', 'locktask', 'lock task', 'locked', 'screen'],
    'lock_state_abnormal': ['lockstate', 'lock state', 'global state', 'currentlock', 'lock type'],
    'call_management_during_lock_abnormal': ['call', 'phone', 'telecom', 'emergency', 'dial'],
    'unlock_failed': ['unlock', 'unlocking', 'clear lock', 'stop lock'],
    'unlock_notification_abnormal': ['unlock', 'notification', 'notify'],
    'unbind_unregister_failed': ['unbind', 'unregister', 'deactivate', 'logout', 'remove'],
    'unbind_notification_abnormal': ['unbind', 'unregister', 'notification', 'notify'],
    'payment_reminder_notification_missing': ['payment', 'remind', 'reminder', 'notification', 'overdue'],
    'push_command_missing_or_delayed': ['push', 'command', 'message', 'cloud', 'receive'],
    'push_token_register_failed': ['push', 'token', 'register'],
    'success_state_report_failed': ['report', 'state', 'success', 'upload', 'sync'],
    'window_period_identification_activation_failed': ['window', 'period', 'grace', 'activation', 'identify', 'imei'],
    'imei_abnormal_cloud_request_failed': ['imei', 'cloud', 'request', 'query', 'api'],
    'cloud_custom_policy_local_management': ['custom', 'policy', 'cloud', 'strategy'],
    'global_lock_state_management': ['global', 'lock state', 'lockstate', 'lock type'],
    'multi_user_state_sync': ['user', 'multi-user', 'foreground user', 'current user'],
}


def run_evidence_template_generation_pipeline(
    project_root: Path,
    *,
    subcategory_id: str,
    relative_path: str = DEFAULT_PROJECT_KNOWLEDGE_RELATIVE_PATH,
    output_dir: Path | None = None,
    mode: str = 'prefiltered',
    claude_cli_path: str = 'claude',
    keyword_hints: List[str] | None = None,
    max_candidates: int = 120,
    timeout_seconds: int = 900,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Generate a draft evidence template pack for one subcategory.

    `prefiltered` keeps Claude on a short candidate list. `full_read` preserves
    the old expensive behavior for quality/cost comparison.
    """

    project_root = Path(project_root).resolve()
    knowledge_dir = (project_root / relative_path).resolve()
    output_root = Path(output_dir).resolve() if output_dir else knowledge_dir
    output_root.mkdir(parents=True, exist_ok=True)
    knowledge = load_project_knowledge_dir(knowledge_dir, project_root=project_root)
    if knowledge.get('errors'):
        raise ValueError(f'Knowledge pack has validation errors: {knowledge["errors"]}')
    module = knowledge.get('module') or {}
    subcategory = _find_subcategory(knowledge.get('subcategories') or [], subcategory_id)
    if not subcategory:
        raise ValueError(f'Unknown subcategory_id: {subcategory_id}')
    if mode not in {'prefiltered', 'full_read'}:
        raise ValueError(f'Unsupported evidence generation mode: {mode}')

    source_roots = _resolve_source_roots(project_root, module)
    mode_prefix = f'evidence_templates.{subcategory_id}.{mode}'
    draft_path = output_root / f'{mode_prefix}.draft.jsonl'
    normalized_path = output_root / f'{mode_prefix}.normalized.jsonl'
    csv_path = output_root / f'{mode_prefix}.normalized.csv'
    xlsx_path = output_root / f'{mode_prefix}.normalized.xlsx'
    notes_path = output_root / f'evidence_generation.{subcategory_id}.{mode}.notes.md'
    prompt_path = output_root / f'evidence_generation.{subcategory_id}.{mode}.prompt.md'
    result_path = output_root / f'evidence_generation.{subcategory_id}.{mode}.claude_result.json'
    metrics_path = output_root / f'evidence_generation.{subcategory_id}.{mode}.metrics.json'

    started = time.time()
    candidates: List[Dict[str, Any]] = []
    candidate_paths: Dict[str, str] = {}
    if mode == 'prefiltered':
        hints = keyword_hints or derive_subcategory_keyword_hints(subcategory)
        candidates = scan_source_log_candidates(project_root, source_roots, hints, max_candidates=max_candidates)
        candidate_jsonl = output_root / f'log_candidates.{subcategory_id}.jsonl'
        candidate_md = output_root / f'log_candidates.{subcategory_id}.md'
        _write_jsonl(candidate_jsonl, candidates)
        candidate_md.write_text(_format_candidates_markdown(candidates, subcategory, hints), encoding='utf-8')
        candidate_paths = {'jsonl': str(candidate_jsonl), 'markdown': str(candidate_md)}
        prompt = build_prefiltered_generation_prompt(module, subcategory, candidates, draft_path.name, notes_path.name)
    else:
        prompt = build_full_read_generation_prompt(module, subcategory, draft_path.name, notes_path.name)

    prompt_path.write_text(prompt, encoding='utf-8')
    claude_result: Dict[str, Any] = {}
    if dry_run:
        notes_path.write_text(
            f'# Dry run\n\nMode: `{mode}`\n\nClaude CLI was not invoked. Prompt and candidates were generated only.\n',
            encoding='utf-8',
        )
        parsed = {'items': [], 'parse_errors': []}
        normalized_path.write_text('', encoding='utf-8')
        validation_errors: List[Dict[str, Any]] = []
    else:
        notes_path.write_text(
            f'# Evidence generation in progress\n\nMode: `{mode}`\n\nClaude CLI is generating `{draft_path.name}`.\n',
            encoding='utf-8',
        )
        claude_result = _run_claude_generation(
            claude_cli_path,
            prompt,
            output_root,
            source_roots=source_roots if mode == 'full_read' else [],
            mode=mode,
            timeout_seconds=timeout_seconds,
        )
        result_path.write_text(json.dumps(claude_result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        parsed = normalize_evidence_template_draft(draft_path, normalized_path)
        validation_errors = validate_generated_templates(
            parsed['items'],
            project_root,
            candidates if mode == 'prefiltered' else None,
        )
        if parsed.get('parse_errors'):
            validation_errors.extend(parsed['parse_errors'])
    write_evidence_templates_csv(parsed['items'], csv_path, overwrite=True)
    write_evidence_templates_xlsx(parsed['items'], xlsx_path, overwrite=True)

    duration = round(time.time() - started, 3)
    if not dry_run:
        _ensure_generation_notes(
            notes_path,
            mode=mode,
            subcategory_id=subcategory_id,
            draft_path=draft_path,
            normalized_path=normalized_path,
            item_count=len(parsed['items']),
            validation_errors=validation_errors,
            claude_result=claude_result,
            duration=duration,
            candidate_count=len(candidates),
        )
    metrics = {
        'mode': mode,
        'subcategory_id': subcategory_id,
        'candidate_count': len(candidates),
        'prompt_chars': len(prompt),
        'draft_item_count': len(parsed['items']),
        'validation_error_count': len(validation_errors),
        'duration_seconds': duration,
        'claude_usage': _extract_claude_usage(claude_result),
        'dry_run': dry_run,
    }
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return {
        'ok': not validation_errors,
        'mode': mode,
        'subcategory_id': subcategory_id,
        'knowledge_dir': str(knowledge_dir),
        'output_dir': str(output_root),
        'source_roots': [str(p) for p in source_roots],
        'candidate_count': len(candidates),
        'candidate_paths': candidate_paths,
        'prompt_path': str(prompt_path),
        'draft_path': str(draft_path),
        'normalized_path': str(normalized_path),
        'csv_path': str(csv_path),
        'xlsx_path': str(xlsx_path),
        'notes_path': str(notes_path),
        'metrics_path': str(metrics_path),
        'validation_errors': validation_errors,
        'metrics': metrics,
    }


def run_evidence_template_batch_generation_pipeline(
    project_root: Path,
    *,
    subcategory_ids: List[str] | None = None,
    relative_path: str = DEFAULT_PROJECT_KNOWLEDGE_RELATIVE_PATH,
    output_dir: Path | None = None,
    claude_cli_path: str = 'claude',
    per_subcategory_max_candidates: int = 30,
    timeout_seconds: int = 1800,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Generate evidence templates for many subcategories in one Claude call.

    This is the preferred path when bootstrapping a new module: local code scan
    pays the per-subcategory cost, while Claude sees one compact grouped prompt.
    """

    project_root = Path(project_root).resolve()
    knowledge_dir = (project_root / relative_path).resolve()
    output_root = Path(output_dir).resolve() if output_dir else knowledge_dir
    output_root.mkdir(parents=True, exist_ok=True)
    knowledge = load_project_knowledge_dir(knowledge_dir, project_root=project_root)
    if knowledge.get('errors'):
        raise ValueError(f'Knowledge pack has validation errors: {knowledge["errors"]}')

    module = knowledge.get('module') or {}
    subcategories = knowledge.get('subcategories') or []
    wanted = set(subcategory_ids or [])
    selected_subcategories = [s for s in subcategories if not wanted or str(s.get('id') or '') in wanted]
    if not selected_subcategories:
        raise ValueError('No subcategories selected for batch evidence generation.')

    source_roots = _resolve_source_roots(project_root, module)
    started = time.time()
    groups: List[Dict[str, Any]] = []
    all_candidates: List[Dict[str, Any]] = []
    for subcategory in selected_subcategories:
        subcategory_id = str(subcategory.get('id') or '').strip()
        hints = derive_subcategory_keyword_hints(subcategory)
        candidates = scan_source_log_candidates(
            project_root,
            source_roots,
            hints,
            max_candidates=per_subcategory_max_candidates,
        )
        groups.append({'subcategory': subcategory, 'hints': hints, 'candidates': candidates})
        all_candidates.extend(candidates)

    candidate_jsonl = output_root / 'log_candidates.all.prefiltered.jsonl'
    candidate_md = output_root / 'log_candidates.all.prefiltered.md'
    _write_jsonl(
        candidate_jsonl,
        [
            {'subcategory_id': g['subcategory'].get('id'), 'hints': g['hints'], 'candidate': c}
            for g in groups
            for c in g['candidates']
        ],
    )
    candidate_md.write_text(_format_batch_candidates_markdown(groups), encoding='utf-8')

    draft_path = output_root / 'evidence_templates.all.prefiltered.draft.jsonl'
    normalized_path = output_root / 'evidence_templates.all.prefiltered.normalized.jsonl'
    csv_path = output_root / 'evidence_templates.all.prefiltered.normalized.csv'
    xlsx_path = output_root / 'evidence_templates.all.prefiltered.normalized.xlsx'
    notes_path = output_root / 'evidence_generation.all.prefiltered.notes.md'
    prompt_path = output_root / 'evidence_generation.all.prefiltered.prompt.md'
    result_path = output_root / 'evidence_generation.all.prefiltered.claude_result.json'
    metrics_path = output_root / 'evidence_generation.all.prefiltered.metrics.json'

    prompt = build_batch_prefiltered_generation_prompt(module, groups, draft_path.name, notes_path.name)
    prompt_path.write_text(prompt, encoding='utf-8')

    claude_result: Dict[str, Any] = {}
    if dry_run:
        notes_path.write_text(
            '# Dry run\n\nMode: `batch-prefiltered`\n\nClaude CLI was not invoked. Prompt and candidates were generated only.\n',
            encoding='utf-8',
        )
        parsed = {'items': [], 'parse_errors': []}
        normalized_path.write_text('', encoding='utf-8')
        validation_errors: List[Dict[str, Any]] = []
    else:
        notes_path.write_text(
            f'# Evidence generation in progress\n\nMode: `batch-prefiltered`\n\nClaude CLI is generating `{draft_path.name}`.\n',
            encoding='utf-8',
        )
        claude_result = _run_claude_generation(
            claude_cli_path,
            prompt,
            output_root,
            source_roots=[],
            mode='prefiltered',
            timeout_seconds=timeout_seconds,
        )
        result_path.write_text(json.dumps(claude_result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        parsed = normalize_evidence_template_draft(draft_path, normalized_path)
        validation_errors = validate_generated_templates(parsed['items'], project_root, all_candidates)
        if parsed.get('parse_errors'):
            validation_errors.extend(parsed['parse_errors'])

    write_evidence_templates_csv(parsed['items'], csv_path, overwrite=True)
    write_evidence_templates_xlsx(parsed['items'], xlsx_path, overwrite=True)
    duration = round(time.time() - started, 3)
    if not dry_run:
        _ensure_generation_notes(
            notes_path,
            mode='batch-prefiltered',
            subcategory_id='all',
            draft_path=draft_path,
            normalized_path=normalized_path,
            item_count=len(parsed['items']),
            validation_errors=validation_errors,
            claude_result=claude_result,
            duration=duration,
            candidate_count=len(all_candidates),
        )

    metrics = {
        'mode': 'batch-prefiltered',
        'subcategory_count': len(groups),
        'subcategories': [
            {
                'id': g['subcategory'].get('id'),
                'title': g['subcategory'].get('title'),
                'candidate_count': len(g['candidates']),
                'hints': g['hints'],
            }
            for g in groups
        ],
        'candidate_count': len(all_candidates),
        'prompt_chars': len(prompt),
        'draft_item_count': len(parsed['items']),
        'validation_error_count': len(validation_errors),
        'duration_seconds': duration,
        'claude_usage': _extract_claude_usage(claude_result),
        'dry_run': dry_run,
    }
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return {
        'ok': not validation_errors,
        'mode': 'batch-prefiltered',
        'knowledge_dir': str(knowledge_dir),
        'output_dir': str(output_root),
        'source_roots': [str(p) for p in source_roots],
        'subcategory_count': len(groups),
        'candidate_count': len(all_candidates),
        'candidate_paths': {'jsonl': str(candidate_jsonl), 'markdown': str(candidate_md)},
        'prompt_path': str(prompt_path),
        'draft_path': str(draft_path),
        'normalized_path': str(normalized_path),
        'csv_path': str(csv_path),
        'xlsx_path': str(xlsx_path),
        'notes_path': str(notes_path),
        'metrics_path': str(metrics_path),
        'validation_errors': validation_errors,
        'metrics': metrics,
    }


def scan_source_log_candidates(
    project_root: Path,
    source_roots: List[Path],
    keyword_hints: List[str] | None = None,
    *,
    max_candidates: int = 120,
) -> List[Dict[str, Any]]:
    """Scan source roots and return ranked real logging call candidates."""

    project_root = Path(project_root).resolve()
    hints = [h.lower() for h in (keyword_hints or []) if str(h).strip()]
    candidates: List[Dict[str, Any]] = []
    for root in source_roots:
        root = Path(root).resolve()
        for path in _iter_source_files(root, project_root):
            try:
                text = path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            tag_values = _extract_tag_values(text)
            lines = text.splitlines()
            for idx, line in enumerate(lines):
                if not LOG_CALL_RE.search(line):
                    continue
                statement = _collect_statement(lines, idx)
                match = LOG_CALL_RE.search(statement)
                if not match:
                    continue
                args = statement[match.end() :]
                tag_expr, message = _extract_log_args(args)
                tag = tag_values.get(tag_expr, tag_expr.strip('" '))
                rel = path.relative_to(project_root).as_posix()
                score = _score_candidate(rel, statement, message, hints, match.group('level'))
                if hints and score <= 0:
                    continue
                candidates.append(
                    {
                        'path': rel,
                        'line': idx + 1,
                        'logger': match.group('logger'),
                        'level': match.group('level'),
                        'tag_expr': tag_expr,
                        'tag': tag,
                        'message': message,
                        'statement': _squash(statement),
                        'score': score,
                    }
                )
    candidates.sort(key=lambda item: (-int(item.get('score') or 0), item.get('path') or '', int(item.get('line') or 0)))
    return candidates[: max(1, int(max_candidates or 1))]


def _iter_source_files(root: Path, project_root: Path) -> List[Path]:
    """Return Java/Kotlin files from either a directory root or a single file."""

    root = Path(root).resolve()
    project_root = Path(project_root).resolve()
    if root.is_file():
        return [root] if _is_source_file(root) and not _has_skipped_part(root, project_root) else []
    if not root.is_dir():
        return []
    return [
        path
        for path in sorted(root.rglob('*'))
        if _is_source_file(path) and not _has_skipped_part(path, project_root)
    ]


def derive_subcategory_keyword_hints(subcategory: Dict[str, Any]) -> List[str]:
    sid = str(subcategory.get('id') or '').strip()
    out: List[str] = list(SUBCATEGORY_KEYWORD_HINTS.get(sid, []))
    for part in re.split(r'[_\W]+', sid):
        if len(part) >= 3:
            out.append(part)
    for key in ('title', 'description'):
        text = str(subcategory.get(key) or '')
        for token in re.findall(r'[A-Za-z][A-Za-z0-9_]{2,}', text):
            out.append(token)
    for alias in subcategory.get('aliases') or []:
        for token in re.findall(r'[A-Za-z][A-Za-z0-9_]{2,}', str(alias)):
            out.append(token)
    return _dedupe(out)


def build_prefiltered_generation_prompt(
    module: Dict[str, Any],
    subcategory: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    draft_file_name: str,
    notes_file_name: str,
) -> str:
    candidate_lines = '\n'.join(json.dumps(c, ensure_ascii=False) for c in candidates)
    if not candidate_lines:
        candidate_lines = '(no candidates)'
    fields = ','.join(EVIDENCE_TEMPLATE_FIELDS)
    return f"""请生成 Android 单行日志证据模板
模块：{module.get('title') or module.get('id')} ({module.get('id')})
小类：{subcategory.get('title')} ({subcategory.get('id')})
小类说明：{subcategory.get('description') or ''}

下面是本地预筛出来的源码日志候选，请只把适合前置工作流的候选转成证据模板，复杂推理写进 notes 或 skill。
要求：
1. 只能使用候选中真实存在的 Log/Slog/Logger 调用。
2. `regex` 必须绑定 TAG 和稳定 message 片段。
3. 不要把泛化的 message、UI 文案或纯语义词当成证据。
4. 需要多行上下文才能判断的候选写到 `{notes_file_name}`，不要生成模板。
5. `meaning` 用中文说明命中后代表什么状态或异常。

候选日志 JSONL：
{candidate_lines}

请写入两个文件：
- `{draft_file_name}`：仅写 JSONL，不要 CSV header，每行一个 JSON object。
- `{notes_file_name}`：记录放弃原因、疑点和后续人工审阅建议。

JSONL 字段顺序：
{fields}

固定字段：
- module_id = `{module.get('id')}`
- subcategory_id = `{subcategory.get('id')}`
- profile = `functional`
- log_type = `android_log`
- enabled = true

`parameters` 没有占位符时填 []，有占位符时例如 ["type", "result"]。
"""

def build_full_read_generation_prompt(
    module: Dict[str, Any],
    subcategory: Dict[str, Any],
    draft_file_name: str,
    notes_file_name: str,
) -> str:
    fields = ','.join(EVIDENCE_TEMPLATE_FIELDS)
    return f"""请生成 Android 单行日志证据模板
这是 full_read 模式：请完整阅读相关源码后，抽取可单行确认含义的日志证据；复杂流程经验写入 notes 或模块 skill。
模块：{module.get('title') or module.get('id')} ({module.get('id')})
模块说明：{module.get('description') or ''}
小类：{subcategory.get('title')} ({subcategory.get('id')})
小类说明：{subcategory.get('description') or ''}

要求：
1. 只提取源码中真实存在的 Log/Slog/Logger 调用。
2. `regex` 必须绑定 TAG 和稳定 message 片段。
3. 不要把泛化 message、UI 文案或纯语义词当成证据。
4. 需要多行上下文才能判断的内容只写 notes。
5. 最多生成 80 条。

请写入两个文件：
- `{draft_file_name}`：仅写 JSONL，不要 CSV header，每行一个 JSON object。
- `{notes_file_name}`：记录阅读范围、舍弃候选和人工审阅建议。

JSONL 字段顺序：
{fields}

固定字段：
- module_id = `{module.get('id')}`
- subcategory_id = `{subcategory.get('id')}`
- profile = `functional`
- log_type = `android_log`
- enabled = true

`parameters` 没有占位符时填 []，有占位符时例如 ["type", "result"]。
"""

def build_batch_prefiltered_generation_prompt(
    module: Dict[str, Any],
    groups: List[Dict[str, Any]],
    draft_file_name: str,
    notes_file_name: str,
) -> str:
    fields = ','.join(EVIDENCE_TEMPLATE_FIELDS)
    category_lines = []
    candidate_lines = []
    for group in groups:
        sub = group['subcategory']
        sub_id = str(sub.get('id') or '')
        category_lines.append(
            json.dumps(
                {
                    'id': sub_id,
                    'title': sub.get('title'),
                    'description': sub.get('description'),
                    'candidate_count': len(group['candidates']),
                    'hints': group['hints'],
                },
                ensure_ascii=False,
            )
        )
        for candidate in group['candidates']:
            candidate_lines.append(json.dumps({'subcategory_id': sub_id, 'candidate': candidate}, ensure_ascii=False))
    candidates = '\n'.join(candidate_lines) or '(no candidates)'
    categories = '\n'.join(category_lines)
    return f"""请批量生成 Android 单行日志证据模板
模块：
{json.dumps(module, ensure_ascii=False, indent=2)}

小类：
{categories}

候选日志：
{candidates}

输出字段：
{fields}

要求：
1. 每条模板必须来自候选日志中真实存在的源码位置。
2. `subcategory_id` 必须使用候选项自带的 `subcategory_id`。
3. `code_location` 使用候选中的 `candidate.path:candidate.line`。
4. `regex` 尽量组合 `candidate.tag + ": " + candidate.message` 中的稳定片段。
5. 每个小类优先生成 2-6 条高价值模板，宁缺毋滥。
6. `meaning` 写清命中含义，`next_steps` 写后续应查什么。
7. 不要输出 Markdown 包裹，`{draft_file_name}` 只写 JSONL，每行一个 JSON 对象。
8. `{notes_file_name}` 用 Markdown 记录舍弃原因、风险和人工审阅建议。
9. 默认填入 `module_id={module.get('id')}`、`profile=functional`、`log_type=android_log`、`enabled=true`。

必须创建这些文件：
- `{draft_file_name}`
- `{notes_file_name}`
"""

def normalize_evidence_template_draft(draft_path: Path, normalized_path: Path) -> Dict[str, Any]:
    """Normalize Claude output into valid JSONL, accepting accidental CSV output."""

    draft_path = Path(draft_path)
    normalized_path = Path(normalized_path)
    items: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    if not draft_path.is_file():
        normalized_path.write_text('', encoding='utf-8')
        return {'items': [], 'parse_errors': [{'code': 'missing_draft', 'path': str(draft_path), 'message': 'Draft file was not created.'}]}
    text = draft_path.read_text(encoding='utf-8-sig', errors='replace')
    stripped = text.lstrip()
    if not stripped:
        normalized_path.write_text('', encoding='utf-8')
        return {'items': [], 'parse_errors': []}
    try:
        if stripped.startswith('{'):
            for idx, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                item = json.loads(line)
                if isinstance(item, dict):
                    items.append(_normalize_template_item(item))
                else:
                    errors.append({'code': 'invalid_jsonl_item', 'line': idx, 'message': 'JSONL item must be an object.'})
        elif stripped.startswith('['):
            data = json.loads(text)
            if isinstance(data, list):
                items = [_normalize_template_item(item) for item in data if isinstance(item, dict)]
            else:
                errors.append({'code': 'invalid_json', 'path': str(draft_path), 'message': 'JSON array expected.'})
        else:
            reader = csv.DictReader(text.splitlines())
            for row in reader:
                if row and any(str(v or '').strip() for v in row.values()):
                    items.append(_normalize_template_item(row))
    except (csv.Error, json.JSONDecodeError) as exc:
        errors.append({'code': 'parse_error', 'path': str(draft_path), 'message': str(exc)})
    normalized_path.write_text(''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in items), encoding='utf-8')
    return {'items': items, 'parse_errors': errors}


def validate_generated_templates(
    items: List[Dict[str, Any]],
    project_root: Path,
    candidates: List[Dict[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    errors: List[Dict[str, Any]] = []
    candidate_by_loc = {}
    for c in candidates or []:
        candidate_by_loc[f"{c.get('path')}:{c.get('line')}"] = c
    for idx, item in enumerate(items, start=1):
        tid = item.get('id') or f'#{idx}'
        regex = str(item.get('regex') or '').strip()
        if not regex:
            errors.append({'code': 'missing_regex', 'template_id': tid, 'message': 'regex is required.'})
            continue
        try:
            compiled = re.compile(regex)
        except re.error as exc:
            errors.append({'code': 'invalid_regex', 'template_id': tid, 'message': str(exc)})
            continue
        loc = str(item.get('code_location') or '').strip()
        source_line = _read_code_location_line(project_root, loc)
        if not source_line:
            errors.append({'code': 'invalid_code_location', 'template_id': tid, 'code_location': loc, 'message': 'code_location does not point to a readable source line.'})
        elif not LOG_CALL_RE.search(source_line):
            errors.append({'code': 'not_real_log_call', 'template_id': tid, 'code_location': loc, 'message': 'code_location line is not a supported logging call.'})
        if candidates is not None:
            c = candidate_by_loc.get(loc)
            if not c:
                errors.append({'code': 'not_from_candidate', 'template_id': tid, 'code_location': loc, 'message': 'template was not generated from the prefiltered candidate list.'})
            elif not _regex_matches_candidate(compiled, c):
                errors.append({'code': 'regex_does_not_match_candidate', 'template_id': tid, 'regex': regex, 'message': 'regex does not match the source candidate log message.'})
    return errors


def _regex_matches_candidate(compiled: re.Pattern, candidate: Dict[str, Any]) -> bool:
    """Match a generated regex against conservative runtime samples.

    The scanner intentionally avoids executing code, so dynamic log statements
    such as ``"start " + packageName`` first appear as static fragments. For
    validation we rebuild a small sample with ``VALUE`` placeholders between
    literal fragments, which lets strict regexes with capture groups pass while
    still requiring a real source logging candidate.
    """

    statement = str(candidate.get('statement') or '')
    message = str(candidate.get('message') or '')
    messages = [message, _render_runtime_message(statement)]
    tags = _candidate_tag_alternatives(candidate)
    samples: List[str] = [statement]
    for msg in messages:
        if msg:
            samples.append(msg)
            for tag in tags:
                samples.append(f'{tag}: {msg}')
    return any(sample and compiled.search(sample) for sample in samples)


def _render_runtime_message(statement: str) -> str:
    literals = [_decode_java_string(m.group(1)) for m in STRING_LITERAL_RE.finditer(statement or '')]
    if not literals:
        return ''
    parts: List[str] = []
    for idx, literal in enumerate(literals):
        if idx:
            parts.append('VALUE')
        parts.append(literal)
    return _squash(''.join(parts))


def _candidate_tag_alternatives(candidate: Dict[str, Any]) -> List[str]:
    tag = str(candidate.get('tag') or '').strip()
    path = str(candidate.get('path') or '')
    out = [tag] if tag else []
    if tag in {'TAG', 'TAG_MU', 'TAG_CONFIGURATION', 'LOG_TAG'} or tag.startswith('TAG_'):
        stem = Path(path).stem
        if stem:
            out.append(stem)
        lower = path.lower().replace('\\', '/')
        if '/com/android/server/am/' in lower:
            out.extend(['ActivityManager', 'ActivityTaskManager'])
        if '/com/android/server/pm/' in lower:
            out.extend(['PackageManager', 'PackageInstallerSession'])
        if 'devicepolicy' in lower:
            out.extend(['DevicePolicyManagerService', 'DevicePolicyManager', 'DevicePolicy'])
    return _dedupe(out)


def _run_claude_generation(
    claude_cli_path: str,
    prompt: str,
    cwd: Path,
    *,
    source_roots: List[Path],
    mode: str,
    timeout_seconds: int,
) -> Dict[str, Any]:
    prompt_file = cwd / f'_claude_evidence_generation_prompt.{mode}.md'
    prompt_file.write_text(prompt, encoding='utf-8')
    cli_prompt = (
        f'请先使用 Read 工具读取当前工作目录下的 `{prompt_file.name}`，'
        '然后严格按文件内要求生成输出文件。不要把完整结果写到对话正文里。'
    )
    tools = 'Read,Grep,Glob,Write' if mode == 'full_read' else 'Read,Write'
    cmd = [
        str(claude_cli_path or 'claude'),
        '-p',
        cli_prompt,
        '--input-format',
        'text',
        '--output-format',
        'json',
        '--permission-mode',
        'bypassPermissions',
        '--disable-slash-commands',
        '--tools',
        tools,
    ]
    if source_roots:
        cmd.append('--add-dir')
        cmd.extend(str(p) for p in source_roots)
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        encoding='utf-8',
        errors='replace',
        capture_output=True,
        timeout=timeout_seconds,
    )
    result: Dict[str, Any] = {
        'returncode': proc.returncode,
        'stdout': proc.stdout,
        'stderr': proc.stderr,
        'prompt_file': str(prompt_file),
    }
    try:
        parsed = json.loads(proc.stdout)
        if isinstance(parsed, dict):
            result['json'] = parsed
    except json.JSONDecodeError:
        pass
    if proc.returncode != 0:
        raise RuntimeError(f'Claude CLI failed with exit code {proc.returncode}: {proc.stderr[:1000]}')
    return result


def _normalize_template_item(item: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for field in EVIDENCE_TEMPLATE_FIELDS:
        value = item.get(field)
        if field in {'parameters', 'next_steps'}:
            out[field] = _parse_list_value(value)
        elif field == 'enabled':
            out[field] = _bool_value(value, default=True)
        else:
            out[field] = str(value or '').strip()
    if str(out.get('log_type') or '').lower() in {'v', 'd', 'i', 'w', 'e', 'wtf', 'debug', 'info', 'warn', 'error'}:
        out['log_type'] = 'android_log'
    return out


def _parse_list_value(value: Any) -> List[str]:
    if value is None or value == '':
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, dict):
        return [str(k).strip() for k in value if str(k).strip()]
    raw = str(value).strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(v).strip() for v in parsed if str(v).strip()]
        if isinstance(parsed, dict):
            return [str(k).strip() for k in parsed if str(k).strip()]
    except json.JSONDecodeError:
        pass
    return [p.strip() for p in re.split(r'[;,]\s*|\n+', raw) if p.strip()]


def _resolve_source_roots(project_root: Path, module: Dict[str, Any]) -> List[Path]:
    roots = []
    for raw in module.get('source_roots') or ['.']:
        raw_path = Path(str(raw))
        path = (raw_path if raw_path.is_absolute() else project_root / raw_path).resolve()
        if path.exists() and _is_within(path, project_root):
            roots.append(path)
    return roots or [project_root]


def _find_subcategory(items: List[Dict[str, Any]], subcategory_id: str) -> Dict[str, Any] | None:
    for item in items:
        if str(item.get('id') or '') == subcategory_id:
            return item
    return None


def _is_source_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES


def _has_skipped_part(path: Path, project_root: Path) -> bool:
    try:
        parts = path.relative_to(project_root).parts
    except ValueError:
        parts = path.parts
    return any(part in DEFAULT_SKIP_DIRS for part in parts)


def _extract_tag_values(text: str) -> Dict[str, str]:
    values = {}
    for match in TAG_ASSIGN_RE.finditer(text):
        values[match.group('name')] = match.group('value')
    return values


def _collect_statement(lines: List[str], start_idx: int) -> str:
    parts = [lines[start_idx].strip()]
    balance = lines[start_idx].count('(') - lines[start_idx].count(')')
    for idx in range(start_idx + 1, min(len(lines), start_idx + 8)):
        if balance <= 0 and parts[-1].rstrip().endswith(';'):
            break
        parts.append(lines[idx].strip())
        balance += lines[idx].count('(') - lines[idx].count(')')
        if balance <= 0 and lines[idx].rstrip().endswith(';'):
            break
    return ' '.join(parts)


def _extract_log_args(args: str) -> tuple[str, str]:
    # The first argument is usually TAG, and string literals after that form the
    # stable message fragments. This intentionally avoids evaluating code.
    first_comma = args.find(',')
    tag_expr = args[:first_comma].strip() if first_comma >= 0 else ''
    literals = [_decode_java_string(m.group(1)) for m in STRING_LITERAL_RE.finditer(args)]
    message = ' '.join(l for l in literals if l).strip()
    return tag_expr, message


def _decode_java_string(value: str) -> str:
    return value.replace(r'\"', '"').replace(r'\n', ' ').replace(r'\t', ' ')


def _score_candidate(path: str, statement: str, message: str, hints: List[str], level: str) -> int:
    haystack = f'{path}\n{statement}\n{message}'.lower()
    score = 0
    for hint in hints:
        if hint and hint in haystack:
            score += 20 if hint in message.lower() else 8
    if str(level).lower() in {'e', 'error', 'w', 'warn', 'wtf'}:
        score += 3
    return score


def _format_candidates_markdown(candidates: List[Dict[str, Any]], subcategory: Dict[str, Any], hints: List[str]) -> str:
    lines = [
        f"# Log candidates for {subcategory.get('id')}",
        '',
        f"Keyword hints: {', '.join(hints) if hints else '(none)'}",
        '',
    ]
    for c in candidates:
        lines.append(f"- `{c['path']}:{c['line']}` score={c['score']} tag=`{c.get('tag')}` level={c.get('level')}")
        lines.append(f"  - message: `{c.get('message')}`")
        lines.append(f"  - statement: `{c.get('statement')}`")
    return '\n'.join(lines) + '\n'


def _format_batch_candidates_markdown(groups: List[Dict[str, Any]]) -> str:
    lines = ['# Batch Log Candidates', '']
    for group in groups:
        sub = group['subcategory']
        lines.append(f"## {sub.get('id')} - {sub.get('title') or ''}")
        lines.append('')
        lines.append(f"Keyword hints: {', '.join(group['hints']) if group['hints'] else '(none)'}")
        lines.append('')
        for c in group['candidates']:
            lines.append(f"- `{c['path']}:{c['line']}` score={c['score']} tag=`{c.get('tag')}` level={c.get('level')}")
            lines.append(f"  - message: `{c.get('message')}`")
            lines.append(f"  - statement: `{c.get('statement')}`")
        if not group['candidates']:
            lines.append('- No candidates.')
        lines.append('')
    return '\n'.join(lines) + '\n'


def _read_code_location_line(project_root: Path, code_location: str) -> str:
    if not code_location or ':' not in code_location:
        return ''
    path_part, line_part = code_location.rsplit(':', 1)
    try:
        line_no = int(re.sub(r'\D.*$', '', line_part))
    except ValueError:
        return ''
    path = (Path(project_root) / path_part).resolve()
    if not _is_within(path, Path(project_root).resolve()) or not path.is_file():
        return ''
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return ''
    if line_no < 1 or line_no > len(lines):
        return ''
    return lines[line_no - 1]


def _extract_claude_usage(result: Dict[str, Any]) -> Dict[str, Any]:
    data = result.get('json') if isinstance(result.get('json'), dict) else {}
    return {
        'returncode': result.get('returncode'),
        'terminal_reason': data.get('terminal_reason') or data.get('stop_reason'),
        'input_tokens': (data.get('usage') or {}).get('input_tokens', 0),
        'output_tokens': (data.get('usage') or {}).get('output_tokens', 0),
        'total_cost_usd': data.get('total_cost_usd', 0),
        'model_usage': data.get('modelUsage') or {},
    }


def _ensure_generation_notes(
    notes_path: Path,
    *,
    mode: str,
    subcategory_id: str,
    draft_path: Path,
    normalized_path: Path,
    item_count: int,
    validation_errors: List[Dict[str, Any]],
    claude_result: Dict[str, Any],
    duration: float,
    candidate_count: int,
) -> None:
    """Write a concise summary when Claude did not produce the expected notes."""

    try:
        content = notes_path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        content = ''
    if content and 'Evidence generation in progress' not in content and 'Claude CLI was not invoked' not in content:
        return

    usage = _extract_claude_usage(claude_result)
    lines = [
        '# Evidence generation notes',
        '',
        f'- Mode: `{mode}`',
        f'- Subcategory: `{subcategory_id}`',
        f'- Candidate count: {candidate_count}',
        f'- Normalized template count: {item_count}',
        f'- Validation error count: {len(validation_errors)}',
        f'- Duration: {duration}s',
        f'- Claude return code: {usage.get("returncode")}',
        f'- Claude input tokens: {usage.get("input_tokens")}',
        f'- Claude output tokens: {usage.get("output_tokens")}',
        f'- Claude cost USD: {usage.get("total_cost_usd")}',
        '',
        '## Outputs',
        f'- Draft: `{draft_path.name}`',
        f'- Normalized: `{normalized_path.name}`',
    ]
    if validation_errors:
        lines.extend(['', '## Validation Errors'])
        for error in validation_errors[:20]:
            lines.append(f'- `{error.get("code")}` {error.get("template_id", "")}: {error.get("message", "")}')
    else:
        lines.extend(['', 'No validation errors.'])
    notes_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def _write_jsonl(path: Path, items: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in items), encoding='utf-8')


def _squash(value: str) -> str:
    return re.sub(r'\s+', ' ', value).strip()


def _dedupe(items: List[str]) -> List[str]:
    out = []
    seen = set()
    for item in items:
        value = str(item).strip()
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _bool_value(value: Any, default: bool = False) -> bool:
    if value is None or value == '':
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
