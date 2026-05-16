"""Question-only classifier for the Android expert analysis workflow.

Phase 3 deliberately keeps this classifier small and isolated: it sees only the
user's original question plus the cached module/subcategory summaries. It must
not inspect uploaded logs, source code, skills, or case history. Later phases
can use the result to select parameters, evidence templates, and Deep context.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .models import AndroidAnalysisError
from .planner import (
    _build_ai_token_usage,
    _run_claude_cli,
    _trace_ai_stream,
    _trace_ai_token_usage,
)


KNOWN_PROFILES = {'functional', 'stability', 'xts', 'memory', 'performance', 'unknown'}
MODULE_CONFIDENCE_THRESHOLD = 0.5
SUBMODULE_CONFIDENCE_THRESHOLD = 0.6
TOP_CANDIDATE_GAP_THRESHOLD = 0.15


PROFILE_HINTS = {
    'xts': ['xts', 'cts', 'gts', 'vts', 'tradefed', 'test_result', '测试失败', '用例失败'],
    'stability': ['crash', 'fatal exception', '崩溃', '闪退', 'anr', '卡死', 'tombstone', 'native crash'],
    'memory': ['memory', 'meminfo', 'smaps', 'hprof', 'oom', '内存', '泄漏', 'lmk'],
    'performance': ['performance', 'perfetto', 'trace', 'systrace', '卡顿', '耗时', '掉帧', '性能'],
    'functional': ['失败', '异常', '不生效', '没收到', '无法', '未能', '原因', '问题'],
}


MODULE_HINTS = {
    'ams': ['ams', 'activity', 'activitymanager', 'broadcast', 'receiver', 'service', 'adj', '进程', '广播', '四大组件', '拉起', '启动原因', '分身'],
    'pms': ['pms', 'packagemanager', 'package manager', '安装', '卸载', '权限', 'intent', '组件解析', 'home', '默认应用', '禁用'],
    'dpm': ['dpm', 'devicepolicy', 'device policy', 'devicepolicymanager', 'dpms', 'do', 'po', '工作资料', '企业管理', 'xts-dpm'],
    'mdm': ['mdm', 'hdm', 'hihonor', '设备管理接口', '管控接口', '策略未生效'],
    'dlc': ['dlc', 'devicelock', 'device lock', 'kiosk', '锁机', '解锁', '激活流程', '解绑'],
    'rdm': ['rdm', 'realtimedevicemanager', '分期付款', '锁定态', '霸屏', 'locktask', 'check-in', 'push token', '云侧指令'],
}


def run_question_classifier(
    artifacts_dir: Path,
    question: str,
    expert_knowledge_cache: Dict[str, Any] | None = None,
    *,
    cli_path: str = '',
    timeout_seconds: int = 30,
    enable_ai: bool = True,
    ai_runner: Optional[Callable[[str], str]] = None,
    debug_trace: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Classify a user question into module/subcategory candidates.

    The function always writes these artifacts:
    - ``classification_prompt.md``
    - ``classification_raw_output.txt`` when AI was attempted
    - ``classification_result.json``
    - ``classification_metrics.json``
    """

    artifacts_dir = Path(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    catalog = build_classifier_catalog(expert_knowledge_cache)
    prompt = build_classifier_prompt(question, catalog)
    (artifacts_dir / 'classification_prompt.md').write_text(prompt, encoding='utf-8')
    metrics: Dict[str, Any] = {
        'version': 1,
        'mode': 'fallback',
        'prompt_chars': len(prompt),
        'question_chars': len(question or ''),
        'module_count': len(catalog),
        'subcategory_count': sum(len(item.get('subcategories') or []) for item in catalog),
        'started_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'duration_seconds': 0.0,
        'errors': [],
    }
    started = time.perf_counter()
    if debug_trace:
        debug_trace(
            'classifying_question',
            'classification_input',
            {
                'prompt_chars': len(prompt),
                'question': question or '',
                'module_count': metrics['module_count'],
                'subcategory_count': metrics['subcategory_count'],
                'modules': [
                    {
                        'id': item.get('id'),
                        'title': item.get('title'),
                        'subcategory_count': len(item.get('subcategories') or []),
                    }
                    for item in catalog
                ],
            },
        )

    result: Dict[str, Any] | None = None
    raw_output = ''
    errors: List[Dict[str, str]] = []
    if enable_ai and catalog:
        try:
            if ai_runner:
                raw_output = ai_runner(prompt)
                metrics['ai_token_usage'] = _build_ai_token_usage(prompt, raw_output, {})
                _trace_ai_token_usage(debug_trace, 'classifying_question', 'question_classifier', prompt, raw_output)
            else:
                usage_box: Dict[str, Any] = {}

                def usage_callback(usage: Dict[str, Any]) -> None:
                    usage_box.update(
                        _trace_ai_token_usage(
                            debug_trace,
                            'classifying_question',
                            'question_classifier',
                            prompt,
                            usage.get('output_text', ''),
                            usage=usage,
                        )
                    )

                raw_output = _run_claude_cli(
                    prompt,
                    cli_path,
                    timeout_seconds,
                    artifacts_dir,
                    stream_callback=lambda item: _trace_ai_stream(
                        debug_trace,
                        'classifying_question',
                        'question_classifier',
                        item,
                    ),
                    usage_callback=usage_callback,
                )
                metrics['ai_token_usage'] = usage_box
            (artifacts_dir / 'classification_raw_output.txt').write_text(raw_output or '', encoding='utf-8')
            if debug_trace:
                debug_trace(
                    'classifying_question',
                    'classification_raw_output',
                    {'output_chars': len(raw_output or ''), 'output_preview': raw_output or ''},
                )
            result = validate_classification_result(parse_classifier_json(raw_output), catalog, question=question)
            result['classifier_mode'] = 'ai'
        except AndroidAnalysisError as exc:
            errors.append({'code': exc.code, 'message': exc.message})
        except Exception as exc:  # pragma: no cover - defensive around CLI/runtime surprises.
            errors.append({'code': 'classifier_unexpected_error', 'message': str(exc)})
        if errors and debug_trace:
            debug_trace('classifying_question', 'classification_ai_errors', {'errors': errors})

    if result is None:
        result = fallback_question_classifier(question, catalog)
        result['classifier_mode'] = 'fallback'

    result['errors'] = errors
    metrics['mode'] = result.get('classifier_mode') or 'fallback'
    metrics['errors'] = errors
    metrics['duration_seconds'] = round(time.perf_counter() - started, 3)
    _write_json(artifacts_dir / 'classification_result.json', result)
    _write_json(artifacts_dir / 'classification_metrics.json', metrics)
    if debug_trace:
        debug_trace('classifying_question', 'classification_result', result)
    return result


def build_classifier_catalog(expert_knowledge_cache: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    """Build the compact module catalog exposed to the classifier prompt."""

    cache = expert_knowledge_cache or {}
    module_index = cache.get('module_index') if isinstance(cache.get('module_index'), dict) else {}
    catalog: List[Dict[str, Any]] = []
    for module_id in sorted(module_index.keys()):
        loaded = module_index.get(module_id) or {}
        module = loaded.get('module') if isinstance(loaded.get('module'), dict) else {}
        subcategories = loaded.get('subcategories') if isinstance(loaded.get('subcategories'), list) else []
        item = {
            'id': _clean_id(module.get('id') or module_id),
            'title': _clean_text(module.get('title') or module_id, 120),
            'description': _clean_text(module.get('description') or '', 500),
            'profiles': _clean_profile_list(module.get('profiles') or []),
            'default_package_names': _clean_str_list(module.get('default_package_names') or [], max_items=20),
            'package_resolution': module.get('package_resolution') if isinstance(module.get('package_resolution'), dict) else {},
            'aliases': _module_aliases(module, module_id),
            'subcategories': [],
        }
        for sub in subcategories:
            if not isinstance(sub, dict):
                continue
            sub_id = _clean_id(sub.get('id') or '')
            if not sub_id:
                continue
            item['subcategories'].append(
                {
                    'id': sub_id,
                    'title': _clean_text(sub.get('title') or sub_id, 120),
                    'description': _clean_text(sub.get('description') or '', 350),
                    'aliases': _clean_str_list(sub.get('aliases') or [], max_items=20),
                }
            )
        catalog.append(item)
    return catalog


def build_classifier_prompt(question: str, catalog: List[Dict[str, Any]]) -> str:
    payload = {
        'question': question or '',
        'modules': [
            {
                'module_id': item.get('id'),
                'title': item.get('title'),
                'description': item.get('description'),
                'profiles': item.get('profiles') or [],
                'default_package_names': item.get('default_package_names') or [],
                'package_resolution': item.get('package_resolution') or {},
                'aliases': item.get('aliases') or [],
                'subcategories': item.get('subcategories') or [],
            }
            for item in catalog
        ],
    }
    return (
        '# Android 问题模块分类任务\n\n'
        '你是 Android 问题分析工作流的“导诊分类器”。你只能根据用户原始问题描述和下方模块/小类摘要做分类。\n'
        '禁止使用上传日志、源码、skill、历史案例或任何未在输入中出现的信息。输出必须是一个 JSON 对象，不要输出 Markdown。\n\n'
        '分类规则：\n'
        '- 必须返回候选列表，不要只返回单一结论。\n'
        '- module_confidence < 0.5 时，module_id 使用 "unknown"。\n'
        '- submodule_confidence < 0.6 时，submodule_id 使用 "unknown"。\n'
        '- 如果 top1/top2 分差小于 0.15，不要强行收敛到单个小类，submodule_id 使用 "unknown" 并保留候选。\n'
        '- profile 只能是 functional、stability、xts、memory、performance、unknown。\n'
        '- package_candidates 只从用户描述中明显出现的包名抽取；不要猜。\n\n'
        '输出 JSON Schema：\n'
        '{\n'
        '  "module_id": "string",\n'
        '  "module_confidence": 0.0,\n'
        '  "submodule_id": "string",\n'
        '  "submodule_confidence": 0.0,\n'
        '  "profile": "functional|stability|xts|memory|performance|unknown",\n'
        '  "top_candidates": [\n'
        '    {"module_id":"string","submodule_id":"string","profile":"string","score":0.0,"reason":"string"}\n'
        '  ],\n'
        '  "need_submodule": true,\n'
        '  "need_user_clarification": false,\n'
        '  "package_candidates": [],\n'
        '  "time_window_hint": null\n'
        '}\n\n'
        'Input JSON:\n'
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def parse_classifier_json(text: str) -> Dict[str, Any]:
    raw = (text or '').strip()
    if not raw:
        raise AndroidAnalysisError('classifier_empty_output', 'Classifier returned empty output.')
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    fenced = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, flags=re.DOTALL)
    if fenced:
        return _loads_object(fenced.group(1))
    start = raw.find('{')
    if start < 0:
        raise AndroidAnalysisError('classifier_json_missing', 'Classifier output does not contain JSON.')
    for end in range(len(raw), start, -1):
        candidate = raw[start:end].strip()
        if not candidate.endswith('}'):
            continue
        try:
            return _loads_object(candidate)
        except AndroidAnalysisError:
            continue
    raise AndroidAnalysisError('classifier_json_invalid', 'Classifier output is not valid JSON.')


def validate_classification_result(data: Dict[str, Any], catalog: List[Dict[str, Any]], *, question: str = '') -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise AndroidAnalysisError('classifier_schema_invalid', 'Classifier result must be a JSON object.')
    catalog_index = _catalog_index(catalog)
    candidates = [_clean_candidate(item, catalog_index) for item in (data.get('top_candidates') or []) if isinstance(item, dict)]
    candidates = [item for item in candidates if item.get('module_id') != 'unknown' or item.get('reason')]
    main_candidate = _clean_candidate(data, catalog_index)
    if main_candidate.get('module_id') != 'unknown':
        candidates.append(main_candidate)
    if not candidates:
        return _unknown_result(question, errors=[])
    candidates.sort(key=lambda item: item.get('score', 0.0), reverse=True)
    candidates = _dedupe_candidates(candidates)
    candidates.sort(key=lambda item: item.get('score', 0.0), reverse=True)
    candidates = candidates[:5]

    best = candidates[0]
    second = candidates[1] if len(candidates) > 1 else None
    module_id = best['module_id']
    module_confidence = _clean_confidence(data.get('module_confidence'), fallback=best.get('score', 0.0))
    submodule_id = best.get('submodule_id') or 'unknown'
    submodule_confidence = _clean_confidence(data.get('submodule_confidence'), fallback=best.get('score', 0.0) if submodule_id != 'unknown' else 0.0)
    top_gap = best.get('score', 0.0) - (second.get('score', 0.0) if second else 0.0)
    if module_confidence < MODULE_CONFIDENCE_THRESHOLD:
        module_id = 'unknown'
        submodule_id = 'unknown'
        submodule_confidence = 0.0
    elif submodule_confidence < SUBMODULE_CONFIDENCE_THRESHOLD or (second and top_gap < TOP_CANDIDATE_GAP_THRESHOLD):
        submodule_id = 'unknown'
    profile = _clean_profile(data.get('profile') or best.get('profile') or infer_profile(question))
    module_has_subcategories = bool(catalog_index.get(module_id, {}).get('subcategories')) if module_id != 'unknown' else False
    return {
        'schema_version': 1,
        'module_id': module_id,
        'module_confidence': module_confidence,
        'submodule_id': submodule_id,
        'submodule_confidence': submodule_confidence,
        'profile': profile,
        'top_candidates': candidates,
        'need_submodule': bool(module_has_subcategories and submodule_id == 'unknown'),
        'need_user_clarification': bool(module_id == 'unknown' or data.get('need_user_clarification')),
        'package_candidates': _clean_package_candidates(data.get('package_candidates'), question),
        'time_window_hint': _clean_time_window_hint(data.get('time_window_hint')),
    }


def fallback_question_classifier(question: str, catalog: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Local deterministic fallback used when AI is unavailable or times out."""

    question = question or ''
    profile = infer_profile(question)
    scored: List[Dict[str, Any]] = []
    for module in catalog:
        module_terms = _module_terms(module)
        module_raw = _score_terms(question, module_terms)
        best_sub = {'id': 'unknown', 'title': '', 'score_raw': 0.0, 'reason_terms': []}
        for sub in module.get('subcategories') or []:
            terms = _subcategory_terms(sub)
            sub_raw, matched_terms = _score_terms_with_matches(question, terms)
            if sub_raw > best_sub['score_raw']:
                best_sub = {
                    'id': sub.get('id') or 'unknown',
                    'title': sub.get('title') or '',
                    'score_raw': sub_raw,
                    'reason_terms': matched_terms,
                }
        raw = module_raw + best_sub['score_raw'] * 1.4
        if raw <= 0:
            continue
        score = _raw_score_to_confidence(raw)
        reason_terms = best_sub.get('reason_terms') or _matched_terms(question, module_terms)
        reason = '命中描述关键词：' + '、'.join(reason_terms[:6]) if reason_terms else '根据模块摘要弱匹配'
        scored.append(
            {
                'module_id': module.get('id') or 'unknown',
                'module_title': module.get('title') or '',
                'submodule_id': best_sub['id'] if best_sub['score_raw'] > 0 else 'unknown',
                'submodule_title': best_sub['title'],
                'profile': profile,
                'score': score,
                'reason': reason,
            }
        )
    if not scored:
        result = _unknown_result(question, errors=[])
        result['classifier_mode'] = 'fallback'
        return result
    scored.sort(key=lambda item: item['score'], reverse=True)
    scored = scored[:5]
    best = scored[0]
    second = scored[1] if len(scored) > 1 else None
    module_confidence = best['score']
    module_id = best['module_id'] if module_confidence >= MODULE_CONFIDENCE_THRESHOLD else 'unknown'
    submodule_confidence = best['score'] if best.get('submodule_id') != 'unknown' else 0.0
    top_gap = best['score'] - (second['score'] if second else 0.0)
    submodule_id = best.get('submodule_id') or 'unknown'
    if module_id == 'unknown':
        submodule_id = 'unknown'
        submodule_confidence = 0.0
    elif submodule_confidence < SUBMODULE_CONFIDENCE_THRESHOLD or (second and top_gap < TOP_CANDIDATE_GAP_THRESHOLD):
        submodule_id = 'unknown'
    catalog_index = _catalog_index(catalog)
    module_has_subcategories = bool(catalog_index.get(module_id, {}).get('subcategories')) if module_id != 'unknown' else False
    return {
        'schema_version': 1,
        'module_id': module_id,
        'module_confidence': module_confidence,
        'submodule_id': submodule_id,
        'submodule_confidence': submodule_confidence,
        'profile': profile,
        'top_candidates': scored,
        'need_submodule': bool(module_has_subcategories and submodule_id == 'unknown'),
        'need_user_clarification': module_id == 'unknown',
        'package_candidates': _clean_package_candidates(None, question),
        'time_window_hint': _infer_time_window_hint(question),
    }


def infer_profile(question: str) -> str:
    text = (question or '').lower()
    for profile in ('xts', 'stability', 'memory', 'performance', 'functional'):
        if any(term.lower() in text for term in PROFILE_HINTS[profile]):
            return profile
    return 'unknown'


def _catalog_index(catalog: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {str(item.get('id') or ''): item for item in catalog if item.get('id')}


def _clean_candidate(item: Dict[str, Any], catalog_index: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    module_id = _clean_id(item.get('module_id') or item.get('id') or '')
    if module_id not in catalog_index:
        module_id = 'unknown'
    module = catalog_index.get(module_id) or {}
    sub_ids = {str(sub.get('id') or '') for sub in (module.get('subcategories') or [])}
    submodule_id = _clean_id(item.get('submodule_id') or item.get('subcategory_id') or '')
    if not submodule_id or submodule_id not in sub_ids:
        submodule_id = 'unknown'
    return {
        'module_id': module_id,
        'module_title': module.get('title') or '',
        'submodule_id': submodule_id,
        'submodule_title': _subcategory_title(module, submodule_id),
        'profile': _clean_profile(item.get('profile')),
        'score': _clean_confidence(item.get('score'), fallback=0.0),
        'reason': _clean_text(item.get('reason') or '', 260),
    }


def _dedupe_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for item in candidates:
        key = (item.get('module_id'), item.get('submodule_id'), item.get('profile'))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _unknown_result(question: str, errors: List[Dict[str, str]] | None = None) -> Dict[str, Any]:
    return {
        'schema_version': 1,
        'module_id': 'unknown',
        'module_confidence': 0.0,
        'submodule_id': 'unknown',
        'submodule_confidence': 0.0,
        'profile': infer_profile(question),
        'top_candidates': [],
        'need_submodule': False,
        'need_user_clarification': True,
        'package_candidates': _clean_package_candidates(None, question),
        'time_window_hint': _infer_time_window_hint(question),
        'errors': errors or [],
    }


def _module_aliases(module: Dict[str, Any], module_id: str) -> List[str]:
    aliases = _clean_str_list(module.get('aliases') or [], max_items=30)
    hay = ' '.join([module_id, str(module.get('title') or ''), str(module.get('description') or '')]).lower()
    for key, hints in MODULE_HINTS.items():
        if key in hay:
            aliases.extend(hints)
    for package_name in module.get('default_package_names') or []:
        aliases.append(str(package_name))
    return _unique([item for item in aliases if item])


def _module_terms(module: Dict[str, Any]) -> List[str]:
    return _unique(
        [
            module.get('id') or '',
            module.get('title') or '',
            module.get('description') or '',
            *(module.get('aliases') or []),
            *(module.get('default_package_names') or []),
        ]
    )


def _subcategory_terms(subcategory: Dict[str, Any]) -> List[str]:
    return _unique(
        [
            subcategory.get('id') or '',
            subcategory.get('title') or '',
            subcategory.get('description') or '',
            *(subcategory.get('aliases') or []),
        ]
    )


def _score_terms(question: str, terms: Iterable[str]) -> float:
    score, _ = _score_terms_with_matches(question, terms)
    return score


def _score_terms_with_matches(question: str, terms: Iterable[str]) -> tuple[float, List[str]]:
    text = (question or '').lower()
    score = 0.0
    matched: List[str] = []
    for term in terms:
        term = str(term or '').strip().lower()
        if not term or len(term) < 2:
            continue
        parts = [term]
        parts.extend(re.findall(r'[a-z0-9_./+-]{3,}|[\u4e00-\u9fff]{2,}', term))
        for part in _unique(parts):
            if part and part in text:
                score += 2.0 if len(part) >= 4 else 1.0
                matched.append(part)
                break
    return score, _unique(matched)


def _matched_terms(question: str, terms: Iterable[str]) -> List[str]:
    _, matched = _score_terms_with_matches(question, terms)
    return matched


def _raw_score_to_confidence(raw: float) -> float:
    return round(min(0.95, 0.35 + raw / 12.0), 3)


def _clean_confidence(value: Any, *, fallback: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        out = float(fallback)
    return round(min(1.0, max(0.0, out)), 3)


def _clean_profile(value: Any) -> str:
    text = str(value or '').strip().lower()
    return text if text in KNOWN_PROFILES else 'unknown'


def _clean_profile_list(values: Iterable[Any]) -> List[str]:
    out = []
    for item in values or []:
        profile = _clean_profile(item)
        if profile != 'unknown' and profile not in out:
            out.append(profile)
    return out


def _clean_package_candidates(value: Any, question: str) -> List[str]:
    out: List[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                raw = item.get('package_name') or item.get('value') or item.get('name')
            else:
                raw = item
            text = str(raw or '').strip()
            if _looks_like_package_name(text):
                out.append(text)
    for match in re.findall(r'\b[a-zA-Z][\w]*(?:\.[a-zA-Z_][\w]*){2,}\b', question or ''):
        if _looks_like_package_name(match):
            out.append(match)
    return _unique(out)[:10]


def _looks_like_package_name(text: str) -> bool:
    return bool(re.fullmatch(r'[a-zA-Z][\w]*(?:\.[a-zA-Z_][\w]*){2,}', text or ''))


def _clean_time_window_hint(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    return text[:120] if text else None


def _infer_time_window_hint(question: str) -> Any:
    text = question or ''
    for pattern in [r'\d{1,2}:\d{2}(?::\d{2})?', r'\d{4}-\d{1,2}-\d{1,2}', r'\d{1,2}月\d{1,2}日']:
        match = re.search(pattern, text)
        if match:
            return match.group(0)
    return None


def _subcategory_title(module: Dict[str, Any], submodule_id: str) -> str:
    if submodule_id == 'unknown':
        return ''
    for sub in module.get('subcategories') or []:
        if sub.get('id') == submodule_id:
            return sub.get('title') or ''
    return ''


def _clean_id(value: Any) -> str:
    text = str(value or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_.:-]+', text):
        text = re.sub(r'[^A-Za-z0-9_.:-]+', '-', text).strip('-')
    return text[:120]


def _clean_text(value: Any, max_chars: int) -> str:
    text = str(value or '').strip()
    text = re.sub(r'\s+', ' ', text)
    return text[:max_chars]


def _clean_str_list(values: Iterable[Any], *, max_items: int) -> List[str]:
    out = []
    for item in values or []:
        text = _clean_text(item, 180)
        if text:
            out.append(text)
        if len(out) >= max_items:
            break
    return _unique(out)


def _unique(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in values:
        key = str(item or '').strip()
        if not key:
            continue
        low = key.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(key)
    return out


def _loads_object(text: str) -> Dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AndroidAnalysisError('classifier_json_invalid', str(exc)) from exc
    if not isinstance(data, dict):
        raise AndroidAnalysisError('classifier_json_invalid', 'JSON root must be an object.')
    return data


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
