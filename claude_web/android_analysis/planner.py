"""Lightweight AI Planner for Android issue analysis."""

from __future__ import annotations

import json
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .models import AndroidAnalysisError


ALLOWED_ISSUE_TYPES = {
    'android_app_crash',
    'android_system_server_crash',
    'android_anr',
    'android_native_crash',
    'android_permission_denial',
    'android_package_install',
    'android_boot',
    'android_framework_behavior',
    'android_business_spec',
    'android_test_failure',
    'generic_log_error',
    'unknown',
}

_PROMPT_PATH = Path(__file__).resolve().parent / 'prompts' / 'planner.md'


def run_planner(
    artifacts_dir: Path,
    question: str,
    bundles: Iterable[Dict[str, Any]] | None = None,
    requested_bundle_ids: Iterable[str] | None = None,
    cli_path: str = '',
    timeout_seconds: int = 45,
    enable_ai: bool = True,
    ai_runner: Optional[Callable[[str], str]] = None,
) -> Dict[str, Any]:
    # Planner 是轻量路由器，不给最终根因；它只决定后续看哪些日志、规则包和 bundle。
    # AI 失败时必须回退本地启发式结果，保证 Android 分析基础流程离线可用。
    artifacts_dir = Path(artifacts_dir)
    manifest = _read_artifact(artifacts_dir, 'file_manifest.json')
    tree = _read_artifact(artifacts_dir, 'file_tree.json')
    samples = _read_artifact(artifacts_dir, 'file_samples.json')
    bundle_summaries = _bundle_summaries(bundles or [])
    prompt = build_planner_prompt(question, manifest, tree, samples, bundle_summaries, requested_bundle_ids or [])

    errors: List[Dict[str, str]] = []
    if enable_ai:
        try:
            text = ai_runner(prompt) if ai_runner else _run_claude_cli(prompt, cli_path, timeout_seconds, artifacts_dir)
            result = validate_planner_result(parse_planner_json(text))
            result['planner_mode'] = 'ai'
            result['errors'] = []
            _write_planner_result(artifacts_dir, result)
            return result
        except AndroidAnalysisError as e:
            errors.append({'code': e.code, 'message': e.message})
        except Exception as e:
            errors.append({'code': 'planner_unexpected_error', 'message': str(e)})

    result = fallback_planner(question, manifest, samples, bundle_summaries, requested_bundle_ids)
    result['planner_mode'] = 'fallback'
    result['errors'] = errors
    _write_planner_result(artifacts_dir, result)
    return result


def build_planner_prompt(
    question: str,
    manifest: Dict[str, Any],
    tree: Dict[str, Any],
    samples: Dict[str, Any],
    bundles: List[Dict[str, Any]],
    requested_bundle_ids: Iterable[str],
) -> str:
    instruction = _PROMPT_PATH.read_text(encoding='utf-8')
    payload = {
        'question': question or '',
        'requested_bundle_ids': list(requested_bundle_ids or []),
        'bundles': bundles,
        'manifest_summary': _manifest_summary(manifest),
        'file_tree': tree,
        'file_samples': _samples_summary(samples),
    }
    return instruction + '\n\nInput JSON:\n' + json.dumps(payload, ensure_ascii=False, indent=2)


def parse_planner_json(text: str) -> Dict[str, Any]:
    raw = (text or '').strip()
    if not raw:
        raise AndroidAnalysisError('planner_empty_output', 'Planner returned empty output.')
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # CLI 偶尔会包一层 Markdown fence 或夹带说明文字，解析时尽量提取 JSON，
    # 但后续仍会走 schema 清洗，避免模型输出污染规则加载。
    fenced = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, flags=re.DOTALL)
    if fenced:
        return _loads_object(fenced.group(1))

    start = raw.find('{')
    if start < 0:
        raise AndroidAnalysisError('planner_json_missing', 'Planner output does not contain JSON.')
    for end in range(len(raw), start, -1):
        candidate = raw[start:end].strip()
        if not candidate.endswith('}'):
            continue
        try:
            return _loads_object(candidate)
        except AndroidAnalysisError:
            continue
    raise AndroidAnalysisError('planner_json_invalid', 'Planner output is not valid JSON.')


def validate_planner_result(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise AndroidAnalysisError('planner_schema_invalid', 'Planner result must be a JSON object.')
    result = {
        'schema_version': 1,
        'issue_types': _clean_issue_types(data.get('issue_types')),
        'candidate_bundle_ids': _clean_str_list(data.get('candidate_bundle_ids'), max_items=10),
        'candidate_rule_packs': _clean_str_list(data.get('candidate_rule_packs'), max_items=5),
        'candidate_log_paths': _clean_relative_paths(data.get('candidate_log_paths'), max_items=20),
        'candidate_keywords': _clean_str_list(data.get('candidate_keywords'), max_items=40),
        'candidate_entities': data.get('candidate_entities') if isinstance(data.get('candidate_entities'), dict) else {},
        'exclude_paths': _clean_relative_paths(data.get('exclude_paths'), max_items=40),
        'confidence': _clean_confidence(data.get('confidence')),
        'need_user_clarification': bool(data.get('need_user_clarification')),
    }
    if not result['issue_types']:
        result['issue_types'] = ['unknown']
    return result


def fallback_planner(
    question: str,
    manifest: Dict[str, Any],
    samples: Dict[str, Any],
    bundles: List[Dict[str, Any]],
    requested_bundle_ids: Iterable[str],
) -> Dict[str, Any]:
    files = manifest.get('files') or []
    issue_types = set()
    paths: List[str] = []
    keywords = set()
    combined = (question or '').lower()
    for f in files:
        path = str(f.get('path') or '')
        kind = str(f.get('kind') or '')
        lower = path.lower()
        if kind in {'android_anr_trace', 'android_events_log'} or 'anr' in lower:
            issue_types.add('android_anr')
            _append(paths, path, 20)
        if kind == 'android_tombstone' or 'tombstone' in lower:
            issue_types.add('android_native_crash')
            _append(paths, path, 20)
        if kind in {'android_main_log', 'android_system_log', 'android_logcat', 'android_crash'}:
            _append(paths, path, 20)
        if 'bugreport' in lower or kind == 'android_bugreport':
            _append(paths, path, 20)

    for f in samples.get('files') or []:
        for sample in f.get('samples') or []:
            combined += '\n' + str(sample.get('content') or '').lower()
    if 'fatal exception' in combined or 'androidruntime' in combined:
        issue_types.add('android_app_crash')
        keywords.update(['FATAL EXCEPTION', 'AndroidRuntime', 'Exception'])
    if 'permission denial' in combined or 'securityexception' in combined:
        issue_types.add('android_permission_denial')
        keywords.update(['permission denial', 'SecurityException'])
    if 'install_failed' in combined:
        issue_types.add('android_package_install')
        keywords.add('INSTALL_FAILED')
    if 'watchdog' in combined or 'system_server' in combined:
        issue_types.add('android_system_server_crash')
        keywords.update(['Watchdog', 'system_server'])
    if 'rdm' in combined or 'lock' in combined or 'unlock' in combined or 'provision' in combined:
        keywords.update(['rdm', 'lock', 'unlock', 'provision'])

    bundle_ids = _candidate_bundle_ids(combined, bundles, requested_bundle_ids)
    rule_packs = []
    for b in bundles:
        if b.get('id') in bundle_ids:
            for pack in b.get('rule_packs') or []:
                _append(rule_packs, str(pack), 5)

    return validate_planner_result(
        {
            'issue_types': sorted(issue_types) or ['unknown'],
            'candidate_bundle_ids': bundle_ids,
            'candidate_rule_packs': rule_packs,
            'candidate_log_paths': paths,
            'candidate_keywords': sorted(keywords),
            'candidate_entities': {},
            'exclude_paths': [],
            'confidence': 0.35 if issue_types else 0.15,
            'need_user_clarification': not bool(issue_types),
        }
    )


def _run_claude_cli(prompt: str, cli_path: str, timeout_seconds: int, cwd: Path) -> str:
    exe = (cli_path or 'claude').strip() or 'claude'
    cmd = [exe, '--output-format', 'stream-json', '--verbose', '--print']
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise AndroidAnalysisError('planner_cli_not_found', f'Claude CLI not found: {exe}') from exc
    stdout_lines: List[str] = []
    stderr_lines: List[str] = []

    def stdin_writer() -> None:
        try:
            if proc.stdin:
                proc.stdin.write(prompt)
                proc.stdin.close()
        except BrokenPipeError:
            pass

    def stdout_reader() -> None:
        try:
            if proc.stdout:
                for line in proc.stdout:
                    stdout_lines.append(line)
        finally:
            try:
                if proc.stdout:
                    proc.stdout.close()
            except OSError:
                pass

    def stderr_reader() -> None:
        try:
            if proc.stderr:
                for line in proc.stderr:
                    stderr_lines.append(line)
        finally:
            try:
                if proc.stderr:
                    proc.stderr.close()
            except OSError:
                pass

    threads = [
        threading.Thread(target=stdin_writer, daemon=True),
        threading.Thread(target=stdout_reader, daemon=True),
        threading.Thread(target=stderr_reader, daemon=True),
    ]
    for t in threads:
        t.start()
    try:
        proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        raise AndroidAnalysisError('planner_timeout', 'Claude Planner timed out.') from exc
    for t in threads:
        t.join(timeout=1)
    if proc.returncode != 0:
        message = (''.join(stderr_lines) or ''.join(stdout_lines) or '').strip()[:1000]
        raise AndroidAnalysisError('planner_cli_failed', message or f'Claude CLI exited with {proc.returncode}.')
    return _collect_stream_json_text(''.join(stdout_lines))


def _collect_stream_json_text(stdout: str) -> str:
    chunks = []
    for line in (stdout or '').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            chunks.append(line)
            continue
        if data.get('type') == 'message' and data.get('role') == 'assistant':
            content = data.get('content')
            if isinstance(content, str):
                chunks.append(content)
        elif isinstance(data.get('result'), str):
            chunks.append(data['result'])
    return ''.join(chunks).strip() or stdout


def _read_artifact(artifacts_dir: Path, name: str) -> Dict[str, Any]:
    path = artifacts_dir / name
    if not path.is_file():
        raise AndroidAnalysisError('planner_artifact_missing', f'{name} does not exist.')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise AndroidAnalysisError('planner_artifact_invalid', f'{name} is invalid JSON.') from exc
    if not isinstance(data, dict):
        raise AndroidAnalysisError('planner_artifact_invalid', f'{name} must be a JSON object.')
    return data


def _write_planner_result(artifacts_dir: Path, result: Dict[str, Any]) -> None:
    with open(artifacts_dir / 'planner_result.json', 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


def _bundle_summaries(bundles: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for b in bundles:
        if not isinstance(b, dict) or not b.get('id'):
            continue
        out.append(
            {
                'id': str(b.get('id') or ''),
                'title': str(b.get('title') or ''),
                'description': str(b.get('description') or b.get('summary') or ''),
                'rule_packs': [str(x) for x in (b.get('rule_packs') or []) if x],
            }
        )
    return out


def _manifest_summary(manifest: Dict[str, Any]) -> Dict[str, Any]:
    files = []
    for f in (manifest.get('files') or [])[:300]:
        files.append(
            {
                'path': f.get('path'),
                'size': f.get('size'),
                'kind': f.get('kind'),
            }
        )
    return {
        'file_count': manifest.get('file_count', len(files)),
        'total_size': manifest.get('total_size', 0),
        'files': files,
    }


def _samples_summary(samples: Dict[str, Any]) -> Dict[str, Any]:
    out_files = []
    for f in (samples.get('files') or [])[:40]:
        out_samples = []
        for sample in (f.get('samples') or [])[:4]:
            content = str(sample.get('content') or '')
            out_samples.append(
                {
                    'type': sample.get('type'),
                    'keyword': sample.get('keyword', ''),
                    'start_line': sample.get('start_line'),
                    'end_line': sample.get('end_line'),
                    'content': content[:1000],
                }
            )
        out_files.append(
            {
                'path': f.get('path'),
                'kind': f.get('kind'),
                'size': f.get('size'),
                'skipped': f.get('skipped', False),
                'samples': out_samples,
            }
        )
    return {
        'keyword_set': (samples.get('keyword_set') or [])[:80],
        'file_count': samples.get('file_count', len(out_files)),
        'files': out_files,
    }


def _loads_object(text: str) -> Dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AndroidAnalysisError('planner_json_invalid', 'Planner output is not valid JSON.') from exc
    if not isinstance(data, dict):
        raise AndroidAnalysisError('planner_json_invalid', 'Planner JSON must be an object.')
    return data


def _clean_issue_types(value: Any) -> List[str]:
    cleaned = []
    for item in value if isinstance(value, list) else []:
        s = str(item or '').strip()
        if s in ALLOWED_ISSUE_TYPES and s not in cleaned:
            cleaned.append(s)
    return cleaned[:8]


def _clean_str_list(value: Any, max_items: int) -> List[str]:
    cleaned = []
    for item in value if isinstance(value, list) else []:
        s = str(item or '').strip()
        if s and s not in cleaned:
            cleaned.append(s[:200])
        if len(cleaned) >= max_items:
            break
    return cleaned


def _clean_relative_paths(value: Any, max_items: int) -> List[str]:
    cleaned = []
    for item in value if isinstance(value, list) else []:
        s = str(item or '').strip().replace('\\', '/')
        if not s or s.startswith('/') or '..' in s.split('/') or ':' in s.split('/')[0]:
            continue
        if s not in cleaned:
            cleaned.append(s[:500])
        if len(cleaned) >= max_items:
            break
    return cleaned


def _clean_confidence(value: Any) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, n))


def _candidate_bundle_ids(combined: str, bundles: List[Dict[str, Any]], requested_bundle_ids: Iterable[str]) -> List[str]:
    out = []
    known = {str(b.get('id')) for b in bundles}
    for bid in requested_bundle_ids:
        if str(bid) in known:
            _append(out, str(bid), 10)
    for b in bundles:
        text = ' '.join(str(b.get(k) or '') for k in ('id', 'title', 'description')).lower()
        if any(token and token in combined for token in text.split()):
            _append(out, str(b.get('id')), 10)
        if str(b.get('id')) == 'android-rdm' and any(x in combined for x in ('rdm', 'lock', 'unlock', 'provision', '锁机')):
            _append(out, 'android-rdm', 10)
    return out


def _append(values: List[str], item: str, max_items: int) -> None:
    if item and item not in values and len(values) < max_items:
        values.append(item)
