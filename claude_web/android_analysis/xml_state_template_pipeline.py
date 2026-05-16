"""XML/SP state evidence template generation for Android expert packs.

第一版聚焦 Android 项目里最常见、也最适合规则化的状态证据：
SharedPreferences 导出的 XML。它和普通 logcat 证据分开维护，避免把
“状态文件”强行塞进 `android_log` 正则体系里。
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

from .evidence_template_pipeline import derive_subcategory_keyword_hints
from .expert_knowledge import DEFAULT_PROJECT_KNOWLEDGE_RELATIVE_PATH, load_project_knowledge_dir
from .expert_knowledge_builder import (
    XML_STATE_TEMPLATE_FIELDS,
    write_xml_state_templates_csv,
    write_xml_state_templates_xlsx,
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

SP_API_RE = re.compile(
    r'\b(?P<api>getSharedPreferences|SharedPreferences|PreferenceManager|'
    r'getPreference\w*Data|savePreference\w*Data|putPreference\w*Data)\s*\('
)
SP_VALUE_CALL_RE = re.compile(
    r'\b(?P<api>putString|putBoolean|putInt|putLong|putFloat|putStringSet|'
    r'getString|getBoolean|getInt|getLong|getFloat|getStringSet|contains|remove)\s*\('
)
SETTINGS_API_RE = re.compile(r'\bSettings\.(?P<table>Global|Secure|System)\s*\.\s*(?P<api>get\w+|put\w+)\s*\(')
STRING_LITERAL_RE = re.compile(r'"((?:\\.|[^"\\])*)"')
STRING_CONST_RE = re.compile(r'\b(?P<name>[A-Z][A-Z0-9_]{2,})\s*=\s*"(?P<value>[^"]*)"')


def run_xml_state_template_batch_generation_pipeline(
    project_root: Path,
    *,
    subcategory_ids: List[str] | None = None,
    relative_path: str = DEFAULT_PROJECT_KNOWLEDGE_RELATIVE_PATH,
    output_dir: Path | None = None,
    claude_cli_path: str = 'claude',
    per_subcategory_max_candidates: int = 25,
    timeout_seconds: int = 1200,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Generate XML/SP state templates for selected subcategories in one call."""

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
        raise ValueError('No subcategories selected for XML state template generation.')

    started = time.time()
    source_roots = _resolve_source_roots(project_root, module)
    groups: List[Dict[str, Any]] = []
    all_candidates: List[Dict[str, Any]] = []
    for subcategory in selected_subcategories:
        hints = derive_subcategory_keyword_hints(subcategory)
        candidates = scan_source_xml_state_candidates(
            project_root,
            source_roots,
            hints,
            max_candidates=per_subcategory_max_candidates,
        )
        groups.append({'subcategory': subcategory, 'hints': hints, 'candidates': candidates})
        all_candidates.extend(candidates)

    candidate_jsonl = output_root / 'xml_state_candidates.all.prefiltered.jsonl'
    candidate_md = output_root / 'xml_state_candidates.all.prefiltered.md'
    _write_jsonl(
        candidate_jsonl,
        [
            {'subcategory_id': g['subcategory'].get('id'), 'hints': g['hints'], 'candidate': c}
            for g in groups
            for c in g['candidates']
        ],
    )
    candidate_md.write_text(_format_batch_candidates_markdown(groups), encoding='utf-8')

    draft_path = output_root / 'xml_state_templates.all.prefiltered.draft.jsonl'
    normalized_path = output_root / 'xml_state_templates.all.prefiltered.normalized.jsonl'
    csv_path = output_root / 'xml_state_templates.all.prefiltered.normalized.csv'
    xlsx_path = output_root / 'xml_state_templates.all.prefiltered.normalized.xlsx'
    notes_path = output_root / 'xml_state_generation.all.prefiltered.notes.md'
    prompt_path = output_root / 'xml_state_generation.all.prefiltered.prompt.md'
    result_path = output_root / 'xml_state_generation.all.prefiltered.claude_result.json'
    metrics_path = output_root / 'xml_state_generation.all.prefiltered.metrics.json'

    prompt = build_batch_xml_state_generation_prompt(module, groups, draft_path.name, notes_path.name)
    prompt_path.write_text(prompt, encoding='utf-8')

    claude_result: Dict[str, Any] = {}
    if dry_run:
        notes_path.write_text(
            '# Dry run\n\nMode: `xml-state-batch-prefiltered`\n\nClaude CLI was not invoked.\n',
            encoding='utf-8',
        )
        parsed = {'items': [], 'parse_errors': []}
        normalized_path.write_text('', encoding='utf-8')
        validation_errors: List[Dict[str, Any]] = []
    else:
        notes_path.write_text(
            f'# XML state generation in progress\n\nClaude CLI is generating `{draft_path.name}`.\n',
            encoding='utf-8',
        )
        claude_result = _run_claude_generation(
            claude_cli_path,
            prompt,
            output_root,
            timeout_seconds=timeout_seconds,
        )
        result_path.write_text(json.dumps(claude_result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        parsed = normalize_xml_state_template_draft(draft_path, normalized_path)
        validation_errors = validate_generated_xml_state_templates(parsed['items'], project_root, all_candidates)
        if parsed.get('parse_errors'):
            validation_errors.extend(parsed['parse_errors'])

    write_xml_state_templates_csv(parsed['items'], csv_path, overwrite=True)
    write_xml_state_templates_xlsx(parsed['items'], xlsx_path, overwrite=True)
    duration = round(time.time() - started, 3)
    if not dry_run:
        _ensure_generation_notes(
            notes_path,
            draft_path=draft_path,
            normalized_path=normalized_path,
            item_count=len(parsed['items']),
            validation_errors=validation_errors,
            claude_result=claude_result,
            duration=duration,
            candidate_count=len(all_candidates),
        )

    metrics = {
        'mode': 'xml-state-batch-prefiltered',
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
        'mode': 'xml-state-batch-prefiltered',
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


def scan_source_xml_state_candidates(
    project_root: Path,
    source_roots: List[Path],
    keyword_hints: List[str] | None = None,
    *,
    max_candidates: int = 80,
) -> List[Dict[str, Any]]:
    """Scan source roots and return SharedPreferences/Settings state candidates."""

    project_root = Path(project_root).resolve()
    hints = [h.lower() for h in (keyword_hints or []) if str(h).strip()]
    candidates: List[Dict[str, Any]] = []
    global_constants = _scan_string_constants(project_root, source_roots)
    for root in source_roots:
        root = Path(root).resolve()
        for path in _iter_source_files(root, project_root):
            try:
                text = path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            constants = dict(global_constants)
            constants.update(_extract_string_constants(text))
            lines = text.splitlines()
            for idx, line in enumerate(lines):
                if not _looks_like_state_access(line):
                    continue
                statement = _collect_statement(lines, idx)
                rel = path.relative_to(project_root).as_posix()
                literals = [_decode_java_string(m.group(1)) for m in STRING_LITERAL_RE.finditer(statement)]
                symbols = _symbols_in_statement(statement)
                resolved_values = [constants[s] for s in symbols if s in constants]
                api = _detect_api(statement)
                source_type = 'settings_xml' if statement.strip().startswith('Settings.') or 'Settings.' in statement else 'shared_prefs_xml'
                file_hints = _file_hints(statement, literals, resolved_values)
                key_hints = _key_hints(statement, literals, resolved_values)
                score = _score_candidate(rel, statement, literals, resolved_values, hints, api)
                if hints and score <= 0:
                    continue
                candidates.append(
                    {
                        'path': rel,
                        'line': idx + 1,
                        'code_location': f'{rel}:{idx + 1}',
                        'api': api,
                        'source_type': source_type,
                        'file_hints': file_hints,
                        'key_hints': key_hints,
                        'string_literals': literals,
                        'resolved_constants': _related_constants(constants, statement, hints),
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


def build_batch_xml_state_generation_prompt(
    module: Dict[str, Any],
    groups: List[Dict[str, Any]],
    draft_file_name: str,
    notes_file_name: str,
) -> str:
    """Build the Claude prompt for XML/SP state template generation."""

    fields = ','.join(XML_STATE_TEMPLATE_FIELDS)
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
    return f"""???? Android ??????????XML/SP ????????
???
{json.dumps(module, ensure_ascii=False, indent=2)}

?????
{categories}

???????? JSONL?
{candidates}

????????
{fields}

?????
1. ????????? XML/SharedPreferences/Settings ???????????????? logcat ???
2. ?????????? `subcategory_id`?????????????????
3. ???????????????????????????????????????? key?
4. `path_patterns` ??????????????????????????????? `(?i)(shared_prefs|sp).*user_state.*\\.xml$`?
5. `key_regex` ???????????? key????????????????????? key?
6. `value_regex` ?????????????????? `^(0|1|2)$`?`^true$` ??????
7. `value_source` ????? `shared_prefs_value`???????? Settings ?? XML ??? `settings_value`?
8. `meaning` ?????? key ????????????????????
9. `next_steps` ????????????????????????? key?
10. ?????????? Intent extra?UI ?????????????? XML/SP ????????????? `{notes_file_name}` ????????

????????????
- `{draft_file_name}`?JSONL????? JSON ????? Markdown??? CSV header?
- `{notes_file_name}`?Markdown???????????/?????
"""

def normalize_xml_state_template_draft(draft_path: Path, normalized_path: Path) -> Dict[str, Any]:
    """Normalize Claude output into XML state template JSONL."""

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
                    items.append(_normalize_xml_state_item(item))
                else:
                    errors.append({'code': 'invalid_jsonl_item', 'line': idx, 'message': 'JSONL item must be an object.'})
        elif stripped.startswith('['):
            data = json.loads(text)
            if isinstance(data, list):
                items = [_normalize_xml_state_item(item) for item in data if isinstance(item, dict)]
            else:
                errors.append({'code': 'invalid_json', 'path': str(draft_path), 'message': 'JSON array expected.'})
        else:
            reader = csv.DictReader(text.splitlines())
            for row in reader:
                if row and any(str(v or '').strip() for v in row.values()):
                    items.append(_normalize_xml_state_item(row))
    except (csv.Error, json.JSONDecodeError) as exc:
        errors.append({'code': 'parse_error', 'path': str(draft_path), 'message': str(exc)})
    normalized_path.write_text(''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in items), encoding='utf-8')
    return {'items': items, 'parse_errors': errors}


def validate_generated_xml_state_templates(
    items: List[Dict[str, Any]],
    project_root: Path,
    candidates: List[Dict[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    """Validate generated XML/SP templates against regex and source candidates."""

    errors: List[Dict[str, Any]] = []
    candidate_by_loc = {str(c.get('code_location') or ''): c for c in candidates or []}
    for idx, item in enumerate(items, start=1):
        tid = item.get('id') or f'#{idx}'
        path_patterns = item.get('path_patterns') or []
        key_regex = str(item.get('key_regex') or '').strip()
        value_regex = str(item.get('value_regex') or '').strip()
        if not path_patterns:
            errors.append({'code': 'missing_path_patterns', 'template_id': tid, 'message': 'path_patterns is required.'})
        if not key_regex:
            errors.append({'code': 'missing_key_regex', 'template_id': tid, 'message': 'key_regex is required.'})
        for field, patterns in (('path_patterns', path_patterns), ('key_regex', [key_regex] if key_regex else []), ('value_regex', [value_regex] if value_regex else [])):
            for pattern in patterns:
                try:
                    re.compile(str(pattern))
                except re.error as exc:
                    errors.append({'code': 'invalid_regex', 'template_id': tid, 'field': field, 'message': str(exc)})
        loc = str(item.get('code_location') or '').strip()
        if candidates is not None:
            c = candidate_by_loc.get(loc)
            if not c:
                errors.append({'code': 'not_from_candidate', 'template_id': tid, 'code_location': loc, 'message': 'template was not generated from the XML/SP candidate list.'})
            else:
                try:
                    if key_regex and not _candidate_key_regex_matches(key_regex, c):
                        errors.append({'code': 'key_regex_does_not_match_candidate', 'template_id': tid, 'key_regex': key_regex, 'message': 'key_regex does not match the source XML/SP candidate.'})
                except re.error:
                    pass
        elif loc and not _read_code_location_line(project_root, loc):
            errors.append({'code': 'invalid_code_location', 'template_id': tid, 'code_location': loc, 'message': 'code_location does not point to a readable source line.'})
    return errors


def _normalize_xml_state_item(item: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for field in XML_STATE_TEMPLATE_FIELDS:
        value = item.get(field)
        if field in {'path_patterns', 'next_steps'}:
            out[field] = _parse_list_value(value)
        elif field == 'enabled':
            out[field] = _bool_value(value, default=True)
        else:
            out[field] = str(value or '').strip()
    if not out.get('source_type'):
        out['source_type'] = 'shared_prefs_xml'
    if not out.get('value_source'):
        out['value_source'] = 'shared_prefs_value'
    return out


def _run_claude_generation(claude_cli_path: str, prompt: str, cwd: Path, *, timeout_seconds: int) -> Dict[str, Any]:
    prompt_file = cwd / '_claude_xml_state_generation_prompt.md'
    prompt_file.write_text(prompt, encoding='utf-8')
    cli_prompt = (
        f'请先使用 Read 工具读取当前工作目录下的 `{prompt_file.name}`，'
        '然后严格按文件内要求生成输出文件。不要把完整结果写到对话正文里。'
    )
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
        'Read,Write',
    ]
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


def _ensure_generation_notes(
    notes_path: Path,
    *,
    draft_path: Path,
    normalized_path: Path,
    item_count: int,
    validation_errors: List[Dict[str, Any]],
    claude_result: Dict[str, Any],
    duration: float,
    candidate_count: int,
) -> None:
    try:
        content = notes_path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        content = ''
    if content and 'XML state generation in progress' not in content and 'Claude CLI was not invoked' not in content:
        return
    usage = _extract_claude_usage(claude_result)
    lines = [
        '# XML state generation notes',
        '',
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


def _looks_like_state_access(line: str) -> bool:
    text = line.strip()
    if SP_API_RE.search(text) or SETTINGS_API_RE.search(text):
        return True
    if not SP_VALUE_CALL_RE.search(text):
        return False
    lower = text.lower()
    return (
        '.edit().' in lower
        or re.search(r'\b(sp|prefs|preferences|sharedpreferences)\s*\.', lower) is not None
        or 'preference' in lower
    )


def _detect_api(statement: str) -> str:
    m = SETTINGS_API_RE.search(statement)
    if m:
        return f'Settings.{m.group("table")}.{m.group("api")}'
    m = SP_API_RE.search(statement)
    if m:
        return m.group('api')
    m = SP_VALUE_CALL_RE.search(statement)
    return m.group('api') if m else 'unknown'


def _file_hints(statement: str, literals: List[str], resolved_values: List[str]) -> List[str]:
    values = []
    if 'getSharedPreferences' in statement:
        values.extend(literals[:1])
    for value in resolved_values:
        if any(part in value.lower() for part in ('state', 'pref', 'version', 'policy', 'config')):
            values.append(value)
    return _dedupe(values)


def _key_hints(statement: str, literals: List[str], resolved_values: List[str]) -> List[str]:
    values = []
    if any(api in statement for api in ('put', 'get', 'contains', 'remove')):
        values.extend(literals[:2])
    values.extend(resolved_values)
    return _dedupe(values)


def _extract_string_constants(text: str) -> Dict[str, str]:
    return {m.group('name'): m.group('value') for m in STRING_CONST_RE.finditer(text)}


def _scan_string_constants(project_root: Path, source_roots: List[Path]) -> Dict[str, str]:
    """Build a lightweight project-wide string constant index for SP/Settings keys."""

    constants: Dict[str, str] = {}
    project_root = Path(project_root).resolve()
    for root in source_roots:
        root = Path(root).resolve()
        for path in _iter_source_files(root, project_root):
            try:
                text = path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            for name, value in _extract_string_constants(text).items():
                if name not in constants:
                    constants[name] = value
    return constants


def _symbols_in_statement(statement: str) -> List[str]:
    return re.findall(r'\b[A-Z][A-Z0-9_]{2,}\b', statement)


def _related_constants(constants: Dict[str, str], statement: str, hints: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    lower_statement = statement.lower()
    state_words = {'state', 'status', 'lock', 'unlock', 'provision', 'policy', 'notify', 'token', 'imei', 'version', 'user'}
    for name, value in constants.items():
        haystack = f'{name} {value}'.lower()
        if name in statement or name.lower() in lower_statement or any(w in haystack for w in state_words) or any(h in haystack for h in hints):
            out[name] = value
        if len(out) >= 24:
            break
    return out


def _score_candidate(path: str, statement: str, literals: List[str], resolved_values: List[str], hints: List[str], api: str) -> int:
    haystack = f'{path}\n{statement}\n{" ".join(literals)}\n{" ".join(resolved_values)}'.lower()
    score = 2 if 'sharedpreferences' in haystack or 'getsharedpreferences' in haystack else 0
    if api.startswith('Settings.'):
        score += 3
    for hint in hints:
        if hint and hint in haystack:
            score += 12
    for word in ('state', 'status', 'lock', 'unlock', 'provision', 'policy', 'notify', 'token', 'imei', 'version'):
        if word in haystack:
            score += 2
    return score


def _collect_statement(lines: List[str], start_idx: int) -> str:
    parts = [lines[start_idx].strip()]
    balance = lines[start_idx].count('(') - lines[start_idx].count(')')
    for idx in range(start_idx + 1, min(len(lines), start_idx + 10)):
        if balance <= 0 and parts[-1].rstrip().endswith(';'):
            break
        parts.append(lines[idx].strip())
        balance += lines[idx].count('(') - lines[idx].count(')')
        if balance <= 0 and lines[idx].rstrip().endswith(';'):
            break
    return ' '.join(parts)


def _format_batch_candidates_markdown(groups: List[Dict[str, Any]]) -> str:
    lines = ['# Batch XML/SP State Candidates', '']
    for group in groups:
        sub = group['subcategory']
        lines.append(f"## {sub.get('id')} - {sub.get('title') or ''}")
        lines.append('')
        lines.append(f"Keyword hints: {', '.join(group['hints']) if group['hints'] else '(none)'}")
        lines.append('')
        for c in group['candidates']:
            lines.append(f"- `{c['code_location']}` score={c['score']} api=`{c.get('api')}` source_type=`{c.get('source_type')}`")
            lines.append(f"  - file_hints: `{', '.join(c.get('file_hints') or [])}`")
            lines.append(f"  - key_hints: `{', '.join(c.get('key_hints') or [])}`")
            lines.append(f"  - statement: `{c.get('statement')}`")
        if not group['candidates']:
            lines.append('- No candidates.')
        lines.append('')
    return '\n'.join(lines) + '\n'


def _candidate_haystack(candidate: Dict[str, Any]) -> str:
    return '\n'.join(
        [
            str(candidate.get('statement') or ''),
            ' '.join(str(x) for x in candidate.get('file_hints') or []),
            ' '.join(str(x) for x in candidate.get('key_hints') or []),
            ' '.join(str(x) for x in candidate.get('string_literals') or []),
            json.dumps(candidate.get('resolved_constants') or {}, ensure_ascii=False),
        ]
    )


def _candidate_key_regex_matches(key_regex: str, candidate: Dict[str, Any]) -> bool:
    compiled = re.compile(key_regex, re.IGNORECASE)
    literal_prefix = _regex_literal_prefix(key_regex)
    fragments = []
    fragments.extend(str(x) for x in candidate.get('file_hints') or [])
    fragments.extend(str(x) for x in candidate.get('key_hints') or [])
    fragments.extend(str(x) for x in candidate.get('string_literals') or [])
    fragments.extend(str(x) for x in (candidate.get('resolved_constants') or {}).values())
    fragments.append(_candidate_haystack(candidate))
    for fragment in fragments:
        value = str(fragment or '')
        if not value:
            continue
        if compiled.search(value):
            return True
        if literal_prefix and value.lower().startswith(literal_prefix.lower()):
            return True
    return False


def _regex_literal_prefix(pattern: str) -> str:
    """Return a safe literal prefix for dynamic-key regexes such as `^foo_\\d+$`."""

    raw = str(pattern or '').strip()
    if raw.startswith('^'):
        raw = raw[1:]
    chars: List[str] = []
    escaped = False
    for char in raw:
        if escaped:
            if char in '._-':
                chars.append(char)
            else:
                break
            escaped = False
            continue
        if char == '\\':
            escaped = True
            continue
        if char.isalnum() or char in '._-':
            chars.append(char)
            continue
        break
    prefix = ''.join(chars)
    return prefix if len(prefix) >= 3 else ''


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


def _resolve_source_roots(project_root: Path, module: Dict[str, Any]) -> List[Path]:
    roots = []
    for raw in module.get('source_roots') or ['.']:
        raw_path = Path(str(raw))
        path = (raw_path if raw_path.is_absolute() else project_root / raw_path).resolve()
        if path.exists() and _is_within(path, project_root):
            roots.append(path)
    return roots or [project_root]


def _is_source_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES


def _has_skipped_part(path: Path, project_root: Path) -> bool:
    try:
        parts = path.relative_to(project_root).parts
    except ValueError:
        parts = path.parts
    return any(part in DEFAULT_SKIP_DIRS for part in parts)


def _decode_java_string(value: str) -> str:
    return value.replace(r'\"', '"').replace(r'\n', ' ').replace(r'\t', ' ')


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


def _bool_value(value: Any, default: bool = False) -> bool:
    if value is None or value == '':
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


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


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
