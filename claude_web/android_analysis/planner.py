"""Lightweight AI Planner for Android issue analysis."""

from __future__ import annotations

import json
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .models import AndroidAnalysisError, PlannerPromptLimits


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
    prompt_limits: PlannerPromptLimits | None = None,
    enable_ai: bool = True,
    ai_runner: Optional[Callable[[str], str]] = None,
    debug_trace: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    # Planner 是轻量路由器，不给最终根因；它只决定后续看哪些日志、规则包和 bundle。
    # AI 失败时必须回退本地启发式结果，保证 Android 分析基础流程离线可用。
    artifacts_dir = Path(artifacts_dir)
    manifest = _read_artifact(artifacts_dir, 'file_manifest.json')
    tree = _read_artifact(artifacts_dir, 'file_tree.json')
    samples = _read_artifact(artifacts_dir, 'file_samples.json')
    bundle_summaries = _bundle_summaries(bundles or [])
    prompt, prompt_metrics = build_planner_prompt_with_metadata(
        question,
        manifest,
        tree,
        samples,
        bundle_summaries,
        requested_bundle_ids or [],
        prompt_limits=prompt_limits,
    )
    _write_planner_prompt_metrics(artifacts_dir, prompt_metrics)
    if debug_trace:
        debug_trace(
            'planning',
            'planner_input',
            {
                'prompt_chars': len(prompt),
                'prompt_budget_chars': prompt_metrics.get('budget_chars'),
                'prompt_component_chars': prompt_metrics.get('component_chars') or {},
                'prompt_clipping': prompt_metrics.get('clipping') or {},
                'prompt_preview_chars': min(len(prompt), 4000),
                'prompt_preview_truncated': len(prompt) > 4000,
                'question': question,
                'requested_bundle_ids': list(requested_bundle_ids or []),
                'bundle_count': len(bundle_summaries),
                'bundles': bundle_summaries,
                'manifest_summary': _manifest_summary(manifest),
                'file_tree_root_count': len((tree.get('root') or {}).get('children') or []),
                'sample_file_count': samples.get('file_count', 0),
                'sample_keyword_count': len(samples.get('keyword_set') or []),
                'sample_keywords': samples.get('keyword_set') or [],
            },
        )

    errors: List[Dict[str, str]] = []
    if enable_ai:
        try:
            if ai_runner:
                text = ai_runner(prompt)
                _trace_ai_token_usage(debug_trace, 'planning', 'planner', prompt, text)
            else:
                text = _run_claude_cli(
                    prompt,
                    cli_path,
                    timeout_seconds,
                    artifacts_dir,
                    stream_callback=lambda item: _trace_ai_stream(debug_trace, 'planning', 'planner', item),
                    usage_callback=lambda usage: _trace_ai_token_usage(
                        debug_trace,
                        'planning',
                        'planner',
                        prompt,
                        usage.get('output_text', ''),
                        usage=usage,
                    ),
                )
            if debug_trace:
                debug_trace(
                    'planning',
                    'planner_raw_output',
                    {
                        'output_chars': len(text or ''),
                        'output_preview': text or '',
                    },
                )
            result = validate_planner_result(parse_planner_json(text))
            result['planner_mode'] = 'ai'
            result['errors'] = []
            _write_planner_result(artifacts_dir, result)
            if debug_trace:
                debug_trace('planning', 'planner_result', result)
            return result
        except AndroidAnalysisError as e:
            errors.append({'code': e.code, 'message': e.message})
        except Exception as e:
            errors.append({'code': 'planner_unexpected_error', 'message': str(e)})
        if debug_trace:
            debug_trace('planning', 'planner_ai_errors', {'errors': errors})

    result = fallback_planner(question, manifest, samples, bundle_summaries, requested_bundle_ids)
    result['planner_mode'] = 'fallback'
    result['errors'] = errors
    _write_planner_result(artifacts_dir, result)
    if debug_trace:
        debug_trace('planning', 'planner_result', result)
    return result


def build_planner_prompt(
    question: str,
    manifest: Dict[str, Any],
    tree: Dict[str, Any],
    samples: Dict[str, Any],
    bundles: List[Dict[str, Any]],
    requested_bundle_ids: Iterable[str],
    prompt_limits: PlannerPromptLimits | None = None,
) -> str:
    prompt, _ = build_planner_prompt_with_metadata(
        question,
        manifest,
        tree,
        samples,
        bundles,
        requested_bundle_ids,
        prompt_limits=prompt_limits,
    )
    return prompt


def build_planner_prompt_with_metadata(
    question: str,
    manifest: Dict[str, Any],
    tree: Dict[str, Any],
    samples: Dict[str, Any],
    bundles: List[Dict[str, Any]],
    requested_bundle_ids: Iterable[str],
    prompt_limits: PlannerPromptLimits | None = None,
) -> tuple[str, Dict[str, Any]]:
    limits = prompt_limits or PlannerPromptLimits()
    instruction = _PROMPT_PATH.read_text(encoding='utf-8')
    requested_ids = list(requested_bundle_ids or [])
    manifest_summary = _manifest_summary(manifest, max_files=min(300, max(60, limits.max_tree_nodes)))
    compact_tree = _tree_summary(tree, manifest, question, limits.max_tree_nodes)
    sample_summary = _samples_summary(
        samples,
        question=question,
        max_files=limits.max_sample_files,
        max_total_chars=limits.max_sample_chars,
    )
    payload = {
        'question': question or '',
        'requested_bundle_ids': requested_ids,
        'bundles': bundles,
        'manifest_summary': manifest_summary,
        'file_tree': compact_tree,
        'file_samples': sample_summary,
    }
    prompt = _render_planner_prompt(instruction, payload)
    clipping = {
        'budget_applied': False,
        'original_prompt_chars': len(prompt),
        'final_prompt_chars': len(prompt),
        'budget_chars': limits.prompt_budget_chars,
        'tree_total_nodes': compact_tree.get('total_nodes', 0),
        'tree_included_nodes': compact_tree.get('included_nodes', 0),
        'sample_total_files': sample_summary.get('source_file_count', sample_summary.get('file_count', 0)),
        'sample_included_files': len(sample_summary.get('files') or []),
        'sample_total_content_chars': sample_summary.get('source_content_chars', 0),
        'sample_included_content_chars': sample_summary.get('included_content_chars', 0),
    }
    if len(prompt) > limits.prompt_budget_chars:
        clipping['budget_applied'] = True
        payload, prompt, shrink_info = _shrink_planner_payload_to_budget(
            instruction,
            payload,
            question,
            limits,
        )
        clipping.update(shrink_info)
        clipping['final_prompt_chars'] = len(prompt)
    metrics = {
        'version': 1,
        'prompt_chars': len(prompt),
        'budget_chars': limits.prompt_budget_chars,
        'component_chars': _planner_component_chars(instruction, payload),
        'clipping': clipping,
    }
    return prompt, metrics


def _render_planner_prompt(instruction: str, payload: Dict[str, Any]) -> str:
    return instruction + '\n\nInput JSON:\n' + json.dumps(payload, ensure_ascii=False, indent=2)


def _planner_component_chars(instruction: str, payload: Dict[str, Any]) -> Dict[str, int]:
    def size(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False, indent=2))

    return {
        'instruction_chars': len(instruction or ''),
        'requested_bundle_chars': size(payload.get('requested_bundle_ids') or []),
        'bundle_summary_chars': size(payload.get('bundles') or []),
        'manifest_summary_chars': size(payload.get('manifest_summary') or {}),
        'file_tree_chars': size(payload.get('file_tree') or {}),
        'file_samples_chars': size(payload.get('file_samples') or {}),
    }


def _shrink_planner_payload_to_budget(
    instruction: str,
    payload: Dict[str, Any],
    question: str,
    limits: PlannerPromptLimits,
) -> tuple[Dict[str, Any], str, Dict[str, Any]]:
    samples_source = payload.get('file_samples') if isinstance(payload.get('file_samples'), dict) else {}
    tree_source = payload.get('file_tree') if isinstance(payload.get('file_tree'), dict) else {}
    manifest_source = payload.get('manifest_summary') if isinstance(payload.get('manifest_summary'), dict) else {}
    attempts = [
        {
            'sample_files': max(8, limits.max_sample_files // 2),
            'sample_chars': max(12000, limits.max_sample_chars // 2),
            'tree_nodes': max(120, limits.max_tree_nodes // 2),
            'manifest_files': 120,
        },
        {
            'sample_files': max(4, limits.max_sample_files // 3),
            'sample_chars': max(7000, limits.max_sample_chars // 4),
            'tree_nodes': 80,
            'manifest_files': 80,
        },
        {
            'sample_files': 3,
            'sample_chars': 4000,
            'tree_nodes': 40,
            'manifest_files': 40,
        },
        {
            'sample_files': 1,
            'sample_chars': 1200,
            'tree_nodes': 12,
            'manifest_files': 12,
        },
        {
            'sample_files': 0,
            'sample_chars': 0,
            'tree_nodes': 0,
            'manifest_files': 8,
        },
    ]
    best_payload = dict(payload)
    best_prompt = _render_planner_prompt(instruction, best_payload)
    applied = {}
    for attempt in attempts:
        candidate = dict(payload)
        candidate['manifest_summary'] = _trim_manifest_summary(manifest_source, attempt['manifest_files'])
        candidate['file_tree'] = _trim_tree_summary(tree_source, attempt['tree_nodes'])
        candidate['file_samples'] = _trim_samples_summary(samples_source, question, attempt['sample_files'], attempt['sample_chars'])
        candidate_prompt = _render_planner_prompt(instruction, candidate)
        best_payload = candidate
        best_prompt = candidate_prompt
        applied = attempt
        if len(candidate_prompt) <= limits.prompt_budget_chars:
            break
    info = {
        'budget_applied_attempt': applied,
        'post_clip_component_chars': _planner_component_chars(instruction, best_payload),
    }
    return best_payload, best_prompt, info


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
        'candidate_rule_packs': _clean_str_list(data.get('candidate_rule_packs'), max_items=10),
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
    question_text = (question or '').lower()
    combined = question_text
    question_issue_types = _fallback_issue_types_from_question(question_text)
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
    if ('watchdog' in combined or 'system_server' in combined) and (
        _has_strong_system_server_signal(combined) or not question_issue_types
    ):
        issue_types.add('android_system_server_crash')
        keywords.update(['Watchdog', 'system_server'])
    requested_ids = [str(x).strip() for x in (requested_bundle_ids or []) if str(x).strip()]
    if 'rdm' in question_text or 'lock' in question_text or 'unlock' in question_text or 'provision' in question_text:
        keywords.update(['rdm', 'lock', 'unlock', 'provision'])
    elif 'android-rdm' in requested_ids and (
        'rdm' in combined or 'lock' in combined or 'unlock' in combined or 'provision' in combined
    ):
        keywords.update(['rdm', 'lock', 'unlock', 'provision'])
    issue_types.update(question_issue_types)
    keywords.update(_fallback_keywords_from_question(question_text))

    bundle_ids = _candidate_bundle_ids(combined, bundles, requested_bundle_ids)
    rule_packs = _fallback_rule_packs(bundle_ids, bundles, question_text, combined)

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


def _fallback_issue_types_from_question(question_text: str) -> set[str]:
    out: set[str] = set()
    if _text_has_any(question_text, ('xts', 'cts', 'tradefed', 'testhideallapps', '测试失败', '用例失败')):
        out.add('android_test_failure')
    if _text_has_any(question_text, ('dlc', 'devicelock', 'device lock', '锁机', '解锁', '激活', 'lock activation')):
        out.add('android_framework_behavior')
    if _text_has_any(question_text, ('dpm', 'devicepolicy', 'device policy', 'managedprovisioning', 'provision')):
        out.add('android_framework_behavior')
    if _text_has_any(question_text, ('hide all apps', 'testhideallapps', 'package visibility', '包可见', '隐藏应用')):
        out.add('android_package_install')
    return out


def _fallback_keywords_from_question(question_text: str) -> set[str]:
    keywords: set[str] = set()
    if _text_has_any(question_text, ('xts', 'cts', 'tradefed', 'testhideallapps', '测试失败', '用例失败')):
        keywords.update(['xts', 'cts', 'Tradefed', 'testHideAllApps', 'DevicePolicy', 'PackageManager'])
    if _text_has_any(question_text, ('dlc', 'devicelock', 'device lock', '锁机', '解锁', '激活', 'lock activation')):
        keywords.update(['DLC', 'DeviceLock', 'DevicePolicy', 'lock', 'unlock', 'activation'])
    if _text_has_any(question_text, ('dpm', 'devicepolicy', 'device policy')):
        keywords.update(['DevicePolicy', 'DevicePolicyManager', 'DevicePolicyManagerService'])
    if _text_has_any(question_text, ('managedprovisioning', 'provision', 'work profile', 'byod')):
        keywords.update(['ManagedProvisioning', 'provision', 'DevicePolicy'])
    if _text_has_any(question_text, ('package', 'pms', 'testhideallapps', 'hide all apps', '包管理', '包可见')):
        keywords.update(['PackageManager', 'PackageManagerService', 'packages'])
    return keywords


def _fallback_rule_packs(
    bundle_ids: Iterable[str],
    bundles: List[Dict[str, Any]],
    question_text: str,
    combined: str,
) -> List[str]:
    by_bundle = {str(b.get('id') or ''): [str(p) for p in (b.get('rule_packs') or []) if str(p)] for b in bundles}
    out: List[str] = []
    # 规则包路由优先听用户问题本身，避免日志样本里的 ActivityManager/Watchdog 等噪声
    # 把 fallback 带到过宽的 AMS / system_server 分支。
    route_text = question_text or combined
    for bundle_id in bundle_ids:
        packs = by_bundle.get(str(bundle_id) or '', [])
        if bundle_id == 'android-fwk':
            preferred = _fallback_fwk_rule_packs(route_text, packs)
            if preferred:
                for pack in preferred:
                    _append(out, pack, 10)
                continue
        for pack in packs:
            _append(out, pack, 10)
    return out


def _fallback_fwk_rule_packs(route_text: str, available_packs: List[str]) -> List[str]:
    preferred: List[str] = []

    def add(pack_id: str) -> None:
        if pack_id in available_packs:
            _append(preferred, pack_id, 10)

    if _text_has_any(route_text, ('xts', 'cts', 'tradefed', 'testhideallapps', 'hide all apps', 'package visibility')):
        add('fwk-devicepolicy-generated')
        add('fwk-pms-generated')
    if _text_has_any(route_text, ('dlc', 'devicelock', 'device lock', '锁机', '解锁', 'lock activation')):
        add('fwk-devicelock-generated')
        add('fwk-devicepolicy-generated')
    if _text_has_any(route_text, ('managedprovisioning', 'provision', 'work profile', 'byod', '开机向导')):
        add('fwk-managedprovisioning-generated')
        add('fwk-devicepolicy-generated')
    if _text_has_any(route_text, ('dpm', 'devicepolicy', 'device policy', 'policy')):
        add('fwk-devicepolicy-generated')
    if _text_has_any(route_text, ('package', 'pms', 'packagemanager', '包管理', '安装')):
        add('fwk-pms-generated')
    if _text_has_any(route_text, ('ams', 'activitymanager', 'activity manager', 'anr', 'activity')):
        add('fwk-ams-generated')
    if _text_has_any(route_text, ('honor', 'oem', 'mdm', 'nfc', 'bluetooth')):
        add('fwk-oem-honor-generated')
    return preferred


def _has_strong_system_server_signal(text: str) -> bool:
    return bool(
        re.search(r'watchdog killing system process|system_server.*(?:crash|fatal|died)|fatal.*system_server', text, re.I)
    )


def _text_has_any(text: str, needles: Iterable[str]) -> bool:
    return any(str(needle or '').lower() in text for needle in needles if str(needle or '').strip())


def _run_claude_cli(
    prompt: str,
    cli_path: str,
    timeout_seconds: int,
    cwd: Path,
    usage_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    stream_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> str:
    exe = (cli_path or 'claude').strip() or 'claude'
    cmd = [exe, '--output-format', 'stream-json', '--include-partial-messages', '--verbose', '--print']
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
                    if stream_callback:
                        try:
                            parsed = json.loads(line.strip())
                        except json.JSONDecodeError:
                            continue
                        _emit_stream_trace(parsed, stream_callback)
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
    output_text, stream_usage = _collect_stream_json(''.join(stdout_lines))
    if usage_callback:
        usage_callback(_build_ai_token_usage(prompt, output_text, stream_usage))
    return output_text


def _emit_stream_trace(data: Dict[str, Any], stream_callback: Callable[[Dict[str, Any]], None]) -> None:
    """Forward visible CLI stream fragments for Android analysis observability."""
    if not isinstance(data, dict):
        return
    msg_type = data.get('type')
    if msg_type == 'stream_event':
        event = data.get('event') or {}
        event_type = event.get('type')
        if event_type == 'content_block_delta':
            delta = event.get('delta') or {}
            delta_type = (delta.get('type') or '').strip()
            if delta_type == 'thinking_delta':
                content = delta.get('thinking') or ''
                if content:
                    stream_callback({'kind': 'thinking', 'content': content})
            elif delta_type == 'text_delta':
                content = delta.get('text') or ''
                if content:
                    stream_callback({'kind': 'text', 'content': content})
            elif delta_type in {'input_json_delta', 'input_json'}:
                partial = delta.get('partial_json') or delta.get('partial') or delta.get('input_json_delta') or ''
                if isinstance(partial, dict):
                    partial = json.dumps(partial, ensure_ascii=False)
                if partial:
                    stream_callback({'kind': 'tool', 'content': partial})
        elif event_type == 'content_block_start':
            block = event.get('content_block') or {}
            block_type = (block.get('type') or '').strip()
            if block_type in {'tool_use', 'tool_use_block', 'server_tool_use'}:
                stream_callback(
                    {
                        'kind': 'tool',
                        'content': json.dumps(
                            {
                                'name': block.get('name') or block.get('tool_name') or '',
                                'input': block.get('input') or {},
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
    elif msg_type == 'assistant':
        message = data.get('message') or {}
        for block in message.get('content') or []:
            if not isinstance(block, dict):
                continue
            block_type = block.get('type')
            if block_type in {'thinking', 'redacted_thinking'} and block.get('thinking'):
                stream_callback({'kind': 'thinking', 'content': block.get('thinking') or ''})
            elif block_type == 'text' and block.get('text'):
                stream_callback({'kind': 'text', 'content': block.get('text') or ''})


def _collect_stream_json_text(stdout: str) -> str:
    return _collect_stream_json(stdout)[0]


def _collect_stream_json(stdout: str) -> tuple[str, Dict[str, Any]]:
    chunks = []
    stream_usage: Dict[str, Any] = {}
    for line in (stdout or '').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            chunks.append(line)
            continue
        _merge_stream_usage(stream_usage, data)
        if data.get('type') == 'message' and data.get('role') == 'assistant':
            content = data.get('content')
            if isinstance(content, str):
                chunks.append(content)
        elif isinstance(data.get('result'), str):
            chunks.append(data['result'])
    return ''.join(chunks).strip() or stdout, stream_usage


def _trace_ai_token_usage(
    debug_trace: Optional[Callable[[str, str, Dict[str, Any]], None]],
    stage: str,
    interaction: str,
    prompt: str,
    output: str,
    usage: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    data = usage or _build_ai_token_usage(prompt, output, {})
    data = {k: v for k, v in data.items() if k != 'output_text'}
    data['interaction'] = interaction
    if debug_trace:
        debug_trace(stage, 'ai_token_usage', data)
    return data


def _trace_ai_stream(
    debug_trace: Optional[Callable[[str, str, Dict[str, Any]], None]],
    stage: str,
    interaction: str,
    item: Dict[str, Any],
) -> None:
    if not debug_trace:
        return
    kind = item.get('kind')
    content = str(item.get('content') or '')
    if not content:
        return
    if kind == 'thinking':
        event = 'ai_thinking_delta'
    elif kind == 'tool':
        event = 'ai_tool_event'
    else:
        event = 'ai_text_delta'
    debug_trace(stage, event, {'interaction': interaction, 'content': content})


def _build_ai_token_usage(prompt: str, output: str, stream_usage: Dict[str, Any] | None = None) -> Dict[str, Any]:
    input_estimate = _estimate_token_count(prompt)
    output_estimate = _estimate_token_count(output)
    usage = dict(stream_usage or {})
    has_stream_tokens = any(k.endswith('_tokens') or k == 'total_tokens' for k in usage)
    input_tokens = _first_number(usage, ['input_tokens', 'prompt_tokens']) if has_stream_tokens else None
    output_tokens = _first_number(usage, ['output_tokens', 'completion_tokens']) if has_stream_tokens else None
    total_tokens = _first_number(usage, ['total_tokens']) if has_stream_tokens else None
    if input_tokens is None:
        input_tokens = input_estimate
    if output_tokens is None:
        output_tokens = output_estimate
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens
    data = {
        'token_source': 'stream_usage' if has_stream_tokens else 'estimate',
        'input_tokens': int(input_tokens),
        'output_tokens': int(output_tokens),
        'total_tokens': int(total_tokens),
        'input_tokens_estimate': int(input_estimate),
        'output_tokens_estimate': int(output_estimate),
        'input_chars': len(prompt or ''),
        'output_chars': len(output or ''),
        'output_text': output or '',
    }
    for key in sorted(usage):
        if key not in data and (key.endswith('_tokens') or key == 'total_tokens'):
            data[key] = usage[key]
    return data


def _merge_stream_usage(out: Dict[str, Any], data: Any) -> None:
    # Claude CLI 不同版本的 stream-json usage 位置略有差异：可能在 usage、
    # stats、result message 或模型子对象中。递归提取 token 字段，取最大值作为本轮累计值。
    if isinstance(data, dict):
        for key, value in data.items():
            if _is_token_key(key) and isinstance(value, (int, float)):
                prev = out.get(key)
                out[key] = value if not isinstance(prev, (int, float)) else max(prev, value)
            elif isinstance(value, (dict, list)):
                _merge_stream_usage(out, value)
    elif isinstance(data, list):
        for item in data:
            _merge_stream_usage(out, item)


def _is_token_key(key: str) -> bool:
    return str(key).endswith('_tokens') or str(key) in {'total_tokens', 'prompt_tokens', 'completion_tokens'}


def _first_number(data: Dict[str, Any], keys: List[str]) -> Optional[float]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, (int, float)):
            return value
    return None


def _estimate_token_count(text: str) -> int:
    if not text:
        return 0
    ascii_chars = 0
    non_ascii_chars = 0
    for ch in text:
        if ord(ch) < 128:
            ascii_chars += 1
        else:
            non_ascii_chars += 1
    # 估算只用于 CLI 未返回 usage 的版本：英文大约 4 chars/token，
    # 中文/符号更接近 1.6 chars/token，取偏保守值便于成本预估。
    return max(1, int((ascii_chars / 4.0) + (non_ascii_chars / 1.6) + 0.5))


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


def _write_planner_prompt_metrics(artifacts_dir: Path, metrics: Dict[str, Any]) -> None:
    with open(artifacts_dir / 'planner_prompt_metrics.json', 'w', encoding='utf-8') as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)


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
                'keywords': _clean_str_list(b.get('keywords'), max_items=30),
                'rule_packs': [str(x) for x in (b.get('rule_packs') or []) if x],
            }
        )
    return out


def _manifest_summary(manifest: Dict[str, Any], max_files: int = 300) -> Dict[str, Any]:
    files = []
    source_files = manifest.get('files') or []
    for f in source_files[:max_files]:
        files.append(
            {
                'path': f.get('path'),
                'size': f.get('size'),
                'kind': f.get('kind'),
            }
        )
    kind_counts: Dict[str, int] = {}
    for f in source_files:
        kind = str(f.get('kind') or 'unknown')
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
    return {
        'file_count': manifest.get('file_count', len(source_files)),
        'total_size': manifest.get('total_size', 0),
        'included_file_count': len(files),
        'omitted_file_count': max(0, len(source_files) - len(files)),
        'kind_counts': kind_counts,
        'files': files,
    }


def _samples_summary(
    samples: Dict[str, Any],
    question: str = '',
    max_files: int = 24,
    max_total_chars: int = 50000,
) -> Dict[str, Any]:
    out_files = []
    source_files = samples.get('files') or []
    ranked = _rank_sample_files(source_files, question, samples.get('keyword_set') or [])
    total_source_chars = _sample_content_chars(source_files)
    used_chars = 0
    for f in ranked[:max_files]:
        out_samples = []
        for sample in (f.get('samples') or [])[:4]:
            if used_chars >= max_total_chars:
                break
            content = str(sample.get('content') or '')
            remaining = max(0, max_total_chars - used_chars)
            clipped_content = content[: min(1000, remaining)]
            used_chars += len(clipped_content)
            out_samples.append(
                {
                    'type': sample.get('type'),
                    'keyword': sample.get('keyword', ''),
                    'start_line': sample.get('start_line'),
                    'end_line': sample.get('end_line'),
                    'content': clipped_content,
                    'content_chars': len(clipped_content),
                    'truncated': len(clipped_content) < len(content),
                }
            )
        if not out_samples and used_chars >= max_total_chars:
            break
        out_files.append(
            {
                'path': f.get('path'),
                'kind': f.get('kind'),
                'size': f.get('size'),
                'skipped': f.get('skipped', False),
                'priority_score': f.get('_planner_priority_score', 0),
                'samples': out_samples,
            }
        )
        if used_chars >= max_total_chars:
            break
    return {
        'keyword_set': (samples.get('keyword_set') or [])[:80],
        'file_count': samples.get('file_count', len(source_files)),
        'source_file_count': len(source_files),
        'included_file_count': len(out_files),
        'omitted_file_count': max(0, len(source_files) - len(out_files)),
        'source_content_chars': total_source_chars,
        'included_content_chars': used_chars,
        'files': out_files,
    }


def _tree_summary(tree: Dict[str, Any], manifest: Dict[str, Any], question: str, max_nodes: int) -> Dict[str, Any]:
    flat_nodes = _flatten_tree_nodes(tree.get('root') or {})
    important_paths = _important_manifest_paths(manifest.get('files') or [], question, max_nodes=max(20, max_nodes // 3))
    return {
        'version': tree.get('version', 1),
        'compacted': True,
        'total_nodes': len(flat_nodes),
        'included_nodes': min(len(flat_nodes), max_nodes),
        'omitted_nodes': max(0, len(flat_nodes) - max_nodes),
        'top_nodes': flat_nodes[:max_nodes],
        'important_paths': important_paths,
    }


def _trim_manifest_summary(summary: Dict[str, Any], max_files: int) -> Dict[str, Any]:
    out = dict(summary or {})
    files = list(out.get('files') or [])
    out['files'] = files[:max_files]
    out['included_file_count'] = len(out['files'])
    total = int(out.get('file_count') or len(files))
    out['omitted_file_count'] = max(0, total - len(out['files']))
    return out


def _trim_tree_summary(summary: Dict[str, Any], max_nodes: int) -> Dict[str, Any]:
    out = dict(summary or {})
    nodes = list(out.get('top_nodes') or [])
    out['top_nodes'] = nodes[:max_nodes]
    out['included_nodes'] = len(out['top_nodes'])
    total = int(out.get('total_nodes') or len(nodes))
    out['omitted_nodes'] = max(0, total - len(out['top_nodes']))
    return out


def _trim_samples_summary(summary: Dict[str, Any], question: str, max_files: int, max_total_chars: int) -> Dict[str, Any]:
    return _samples_summary(summary, question=question, max_files=max_files, max_total_chars=max_total_chars)


def _flatten_tree_nodes(root: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    def walk(node: Dict[str, Any], depth: int) -> None:
        if not isinstance(node, dict):
            return
        item = {
            'depth': depth,
            'name': node.get('name'),
            'type': node.get('type'),
        }
        if node.get('path'):
            item['path'] = node.get('path')
        if node.get('kind'):
            item['kind'] = node.get('kind')
        if node.get('size') is not None:
            item['size'] = node.get('size')
        out.append(item)
        for child in node.get('children') or []:
            walk(child, depth + 1)

    walk(root, 0)
    return out


def _important_manifest_paths(files: List[Dict[str, Any]], question: str, max_nodes: int) -> List[Dict[str, Any]]:
    scored = []
    terms = _focus_terms(question, [])
    for index, item in enumerate(files):
        score = _path_score(str(item.get('path') or ''), str(item.get('kind') or ''), terms)
        if score <= 0:
            continue
        scored.append((score, index, item))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [
        {
            'path': item.get('path'),
            'kind': item.get('kind'),
            'size': item.get('size'),
            'score': score,
        }
        for score, _, item in scored[:max_nodes]
    ]


def _rank_sample_files(files: List[Dict[str, Any]], question: str, keywords: Iterable[str]) -> List[Dict[str, Any]]:
    terms = _focus_terms(question, keywords)
    ranked = []
    for index, item in enumerate(files):
        score = _path_score(str(item.get('path') or ''), str(item.get('kind') or ''), terms)
        score += min(80, 12 * _keyword_sample_count(item))
        if item.get('skipped'):
            score -= 120
        clone = dict(item)
        clone['_planner_priority_score'] = score
        ranked.append((score, index, clone))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in ranked]


def _sample_content_chars(files: List[Dict[str, Any]]) -> int:
    total = 0
    for f in files or []:
        for sample in f.get('samples') or []:
            total += len(str(sample.get('content') or ''))
    return total


def _keyword_sample_count(item: Dict[str, Any]) -> int:
    return sum(1 for sample in item.get('samples') or [] if sample.get('type') == 'keyword')


def _focus_terms(question: str, keywords: Iterable[str]) -> List[str]:
    terms = set()
    for text in [question or '', ' '.join(str(x or '') for x in keywords)]:
        for token in re.findall(r'[A-Za-z0-9_.$:-]{3,}|[\u4e00-\u9fff]{2,}', text):
            terms.add(token.lower())
    return sorted(terms, key=len, reverse=True)[:80]


def _path_score(path: str, kind: str, terms: List[str]) -> int:
    lower = path.lower().replace('\\', '/')
    score = 0
    matched = [term for term in terms if term and term in lower]
    if matched:
        score += 120 + min(80, 10 * len(matched))
    if kind in {'android_main_log', 'android_system_log', 'android_events_log', 'android_logcat'}:
        score += 45
    if kind in {'android_crash', 'android_tombstone', 'android_anr_trace'}:
        score += 40
    if kind in {'android_bugreport', 'android_dumpsys'}:
        score += 25
    if any(part in lower for part in ('dropbox', 'crash', 'anr', 'tombstone', 'logcat', 'events', 'system', 'main')):
        score += 20
    if any(part in lower for part in ('shared_prefs', 'settings', 'policy', 'state', 'config')) or Path(lower).suffix in {'.xml', '.prop', '.json'}:
        score += 20
    return score


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
    if out:
        return out
    for b in bundles:
        terms = _bundle_match_terms(b)
        if any(term and term in combined for term in terms):
            _append(out, str(b.get('id')), 10)
    return out


def _bundle_match_terms(bundle: Dict[str, Any]) -> List[str]:
    source = ' '.join(str(bundle.get(k) or '') for k in ('id', 'title', 'description'))
    source += ' ' + ' '.join(str(x or '') for x in (bundle.get('keywords') or []))
    terms = set()
    for token in re.findall(r'[A-Za-z0-9]{2,}|[\u4e00-\u9fff]{2,}', source):
        terms.add(token.lower())
    title = str(bundle.get('title') or '')
    acronym = ''.join(ch for ch in title if 'A' <= ch <= 'Z')
    if len(acronym) >= 2:
        terms.add(acronym.lower())
    return sorted(terms, key=len, reverse=True)[:50]

def _append(values: List[str], item: str, max_items: int) -> None:
    if item and item not in values and len(values) < max_items:
        values.append(item)
