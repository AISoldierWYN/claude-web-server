"""Package and parameter resolution for Android expert analysis.

Phase 4 sits after question classification and before evidence-template
selection. It does not inspect uploaded logs or source code in the first
version. Its job is to resolve stable parameters, especially ``$package_name``,
from module metadata, user text, and optional app inventory JSON.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


PACKAGE_RE = re.compile(r'\b[a-zA-Z][\w]*(?:\.[a-zA-Z_][\w]*){1,}\b')
COMPONENT_RE = re.compile(r'\b([a-zA-Z][\w]*(?:\.[a-zA-Z_][\w]*){1,})/([.$a-zA-Z_][\w.$]*)\b')
UID_RE = re.compile(r'\b(?:uid|callingUid|userId|UID)\s*[=:]?\s*(u\d+a\d+|\d{3,7})\b', re.IGNORECASE)


def run_parameter_resolution(
    artifacts_dir: Path,
    question: str,
    expert_knowledge_cache: Dict[str, Any] | None,
    *,
    classification: Dict[str, Any] | None = None,
    app_inventory: Optional[Iterable[Dict[str, Any]]] = None,
    debug_trace=None,
) -> Dict[str, Any]:
    """Resolve packages/components/uids and write Phase 4 artifacts."""

    artifacts_dir = Path(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    if classification is None:
        classification = _read_json(artifacts_dir / 'classification_result.json')
    classification = classification or {}
    cache = expert_knowledge_cache or {}
    module_index = cache.get('module_index') if isinstance(cache.get('module_index'), dict) else {}
    inventory = normalize_app_inventory(app_inventory if app_inventory is not None else load_app_inventory_from_cache(cache))
    module = _selected_module(classification, module_index)
    module_id = str((module.get('module') or {}).get('id') or classification.get('module_id') or 'unknown')
    module_title = str((module.get('module') or {}).get('title') or module_id)
    module_meta = module.get('module') if isinstance(module.get('module'), dict) else {}
    package_resolution = module_meta.get('package_resolution') if isinstance(module_meta.get('package_resolution'), dict) else {}
    required = bool(package_resolution.get('required'))
    default_packages = _unique([str(x).strip() for x in (module_meta.get('default_package_names') or []) if str(x).strip()])

    result: Dict[str, Any] = {
        'schema_version': 1,
        'module_id': module_id,
        'module_title': module_title,
        'submodule_id': classification.get('submodule_id') or 'unknown',
        'profile': classification.get('profile') or 'unknown',
        'need_package_resolution': required,
        'package_resolution_required': required,
        'package_resolution_reason': str(package_resolution.get('reason') or ''),
        'default_package_names': default_packages,
        'package_candidates': [],
        'parameter_candidates': {
            'uid': [],
            'component': [],
        },
        'resolved_parameters': {
            'package_name': [],
            'uid': [],
            'component': [],
        },
        'need_user_clarification': False,
        'inventory_app_count': len(inventory),
    }
    if debug_trace:
        debug_trace(
            'resolving_parameters',
            'parameter_resolution_input',
            {
                'question': question or '',
                'classification': {
                    'module_id': classification.get('module_id'),
                    'submodule_id': classification.get('submodule_id'),
                    'profile': classification.get('profile'),
                    'candidate_count': len(classification.get('top_candidates') or []),
                },
                'module_id': module_id,
                'need_package_resolution': required,
                'default_package_names': default_packages,
                'inventory_app_count': len(inventory),
            },
        )

    candidates: List[Dict[str, Any]] = []
    for package_name in default_packages:
        _add_package_candidate(
            candidates,
            package_name=package_name,
            confidence=1.0,
            source='module_default_package',
            reason='模块配置了固定默认包名',
        )
    for package_name in _classification_package_candidates(classification):
        _add_package_candidate(
            candidates,
            package_name=package_name,
            confidence=0.95,
            source='classification_package_candidate',
            reason='分类阶段从用户描述中抽取到包名',
        )
    for package_name in _extract_package_names(question or ''):
        _add_package_candidate(
            candidates,
            package_name=package_name,
            confidence=0.92,
            source='question_package_regex',
            reason='用户描述中出现完整包名',
        )

    component_candidates = _extract_components(question or '')
    for component in component_candidates:
        package_name = component.split('/')[0]
        _add_package_candidate(
            candidates,
            package_name=package_name,
            confidence=0.93,
            source='question_component_regex',
            reason=f'用户描述中出现组件 {component}',
        )
    result['parameter_candidates']['component'] = [
        {'value': item, 'confidence': 0.93, 'source': 'question_component_regex'}
        for item in component_candidates
    ]
    result['parameter_candidates']['uid'] = [
        {'value': item, 'confidence': 0.9, 'source': 'question_uid_regex'}
        for item in _extract_uids(question or '')
    ]

    _match_inventory(question or '', inventory, candidates)
    _fill_inventory_metadata(candidates, inventory)
    candidates.sort(key=lambda item: (-float(item.get('confidence') or 0), item.get('package_name') or ''))
    result['package_candidates'] = candidates[:20]

    resolved_packages = [
        item['package_name']
        for item in result['package_candidates']
        if item.get('package_name') and float(item.get('confidence') or 0) >= 0.75
    ]
    result['resolved_parameters']['package_name'] = _unique(resolved_packages)
    result['resolved_parameters']['component'] = [item['value'] for item in result['parameter_candidates']['component']]
    direct_uids = [item['value'] for item in result['parameter_candidates']['uid']]
    inventory_uids = [
        str(item.get('uid'))
        for item in result['package_candidates']
        if item.get('package_name') in result['resolved_parameters']['package_name'] and item.get('uid') not in (None, '')
    ]
    result['resolved_parameters']['uid'] = _unique(direct_uids + inventory_uids)
    result['need_user_clarification'] = bool(required and not result['resolved_parameters']['package_name'])
    result['duration_seconds'] = round(time.perf_counter() - started, 3)

    _write_json(artifacts_dir / 'parameter_resolution.json', result)
    _write_json(
        artifacts_dir / 'parameter_resolution_metrics.json',
        {
            'version': 1,
            'duration_seconds': result['duration_seconds'],
            'question_chars': len(question or ''),
            'inventory_app_count': len(inventory),
            'candidate_count': len(result['package_candidates']),
            'resolved_package_count': len(result['resolved_parameters']['package_name']),
            'need_user_clarification': result['need_user_clarification'],
        },
    )
    if debug_trace:
        debug_trace('resolving_parameters', 'parameter_resolution_result', result)
    return result


def normalize_app_inventory(items: Iterable[Dict[str, Any]] | Dict[str, Any] | None) -> List[Dict[str, Any]]:
    """Normalize flexible app inventory JSON into a stable internal schema."""

    if items is None:
        return []
    if isinstance(items, dict):
        items = items.get('apps') or items.get('packages') or items.get('items') or []
    out: List[Dict[str, Any]] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        package_name = str(
            item.get('package_name')
            or item.get('packageName')
            or item.get('package')
            or item.get('id')
            or ''
        ).strip()
        if not _looks_like_package_name(package_name):
            continue
        label = str(item.get('label') or item.get('name') or item.get('app_label') or '').strip()
        aliases = _list_str(item.get('aliases') or item.get('alias') or item.get('keywords'))
        launcher = str(item.get('launcherActivity') or item.get('launcher_activity') or item.get('component') or '').strip()
        out.append(
            {
                'package_name': package_name,
                'label': label,
                'aliases': _unique([label, *aliases, package_name]),
                'uid': item.get('uid'),
                'version_name': item.get('versionName') or item.get('version_name'),
                'version_code': item.get('versionCode') or item.get('version_code'),
                'launcher_activity': launcher,
                'source': str(item.get('source') or 'app_inventory'),
            }
        )
    return out


def load_app_inventory_from_cache(cache: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    cache = cache or {}
    out: List[Dict[str, Any]] = []
    global_data = cache.get('global') if isinstance(cache.get('global'), dict) else {}
    out.extend(normalize_app_inventory(global_data.get('app_inventory') or []))
    module_index = cache.get('module_index') if isinstance(cache.get('module_index'), dict) else {}
    for module in module_index.values():
        if isinstance(module, dict):
            out.extend(normalize_app_inventory(module.get('app_inventory') or []))
    return _dedupe_inventory(out)


def _selected_module(classification: Dict[str, Any], module_index: Dict[str, Any]) -> Dict[str, Any]:
    module_id = str(classification.get('module_id') or '')
    if module_id in module_index:
        return module_index[module_id] or {}
    for candidate in classification.get('top_candidates') or []:
        if not isinstance(candidate, dict):
            continue
        mid = str(candidate.get('module_id') or '')
        if mid in module_index:
            return module_index[mid] or {}
    return {}


def _classification_package_candidates(classification: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for item in classification.get('package_candidates') or []:
        if isinstance(item, dict):
            raw = item.get('package_name') or item.get('value') or item.get('name')
        else:
            raw = item
        text = str(raw or '').strip()
        if _looks_like_package_name(text):
            out.append(text)
    return _unique(out)


def _match_inventory(question: str, inventory: List[Dict[str, Any]], candidates: List[Dict[str, Any]]) -> None:
    lower_question = (question or '').lower()
    for app in inventory:
        package_name = app['package_name']
        package_lower = package_name.lower()
        if package_lower in lower_question:
            _add_package_candidate(
                candidates,
                package_name=package_name,
                confidence=0.98,
                source='app_inventory_package_match',
                reason='用户描述中出现应用包名',
                label=app.get('label') or '',
                uid=app.get('uid'),
            )
            continue
        best_alias = ''
        best_score = 0.0
        for alias in app.get('aliases') or []:
            alias = str(alias or '').strip()
            if not alias or alias.lower() == package_lower:
                continue
            score = _alias_match_score(question, alias)
            if score > best_score:
                best_score = score
                best_alias = alias
        if best_score > 0:
            _add_package_candidate(
                candidates,
                package_name=package_name,
                confidence=best_score,
                source='app_inventory_semantic_match',
                reason=f'用户描述与应用清单名称/别名匹配：{best_alias}',
                label=app.get('label') or '',
                uid=app.get('uid'),
            )


def _alias_match_score(question: str, alias: str) -> float:
    q = (question or '').lower()
    alias_lower = alias.lower()
    if alias_lower and alias_lower in q:
        return 0.9 if len(alias_lower) >= 2 else 0.0
    tokens = re.findall(r'[a-z0-9_+-]{3,}|[\u4e00-\u9fff]{2,}', alias_lower)
    if not tokens:
        return 0.0
    hits = [token for token in tokens if token in q]
    if not hits:
        return 0.0
    return min(0.82, 0.45 + 0.18 * len(hits))


def _fill_inventory_metadata(candidates: List[Dict[str, Any]], inventory: List[Dict[str, Any]]) -> None:
    by_package = {item['package_name']: item for item in inventory}
    for candidate in candidates:
        app = by_package.get(candidate.get('package_name') or '')
        if not app:
            continue
        candidate.setdefault('label', app.get('label') or '')
        if app.get('uid') not in (None, ''):
            candidate.setdefault('uid', app.get('uid'))
        if app.get('launcher_activity'):
            candidate.setdefault('launcher_activity', app.get('launcher_activity'))
        if app.get('version_name'):
            candidate.setdefault('version_name', app.get('version_name'))


def _add_package_candidate(
    candidates: List[Dict[str, Any]],
    *,
    package_name: str,
    confidence: float,
    source: str,
    reason: str,
    label: str = '',
    uid: Any = None,
) -> None:
    if not _looks_like_package_name(package_name):
        return
    confidence = round(min(1.0, max(0.0, float(confidence))), 3)
    existing = next((item for item in candidates if item.get('package_name') == package_name), None)
    if existing:
        if confidence > float(existing.get('confidence') or 0):
            existing['confidence'] = confidence
            existing['source'] = source
            existing['reason'] = reason
        existing.setdefault('sources', [])
        if source not in existing['sources']:
            existing['sources'].append(source)
        if label and not existing.get('label'):
            existing['label'] = label
        if uid not in (None, '') and existing.get('uid') in (None, ''):
            existing['uid'] = uid
        return
    item = {
        'package_name': package_name,
        'label': label,
        'confidence': confidence,
        'source': source,
        'sources': [source],
        'reason': reason,
    }
    if uid not in (None, ''):
        item['uid'] = uid
    candidates.append(item)


def _extract_package_names(text: str) -> List[str]:
    return _unique([m.group(0) for m in PACKAGE_RE.finditer(text or '') if _looks_like_package_name(m.group(0))])


def _extract_components(text: str) -> List[str]:
    out: List[str] = []
    for match in COMPONENT_RE.finditer(text or ''):
        package_name, cls = match.groups()
        out.append(f'{package_name}/{cls}')
    return _unique(out)


def _extract_uids(text: str) -> List[str]:
    return _unique([m.group(1) for m in UID_RE.finditer(text or '')])


def _dedupe_inventory(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    seen = set()
    for item in items:
        package_name = item.get('package_name')
        if not package_name or package_name in seen:
            continue
        seen.add(package_name)
        out.append(item)
    return out


def _looks_like_package_name(text: str) -> bool:
    text = text or ''
    if text in {'android', 'androidhnext'}:
        return True
    return bool(re.fullmatch(r'[a-zA-Z][\w]*(?:\.[a-zA-Z_][\w]*)+', text))


def _list_str(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in re.split(r'[;,]\s*|\n+', value) if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _unique(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        text = str(value or '').strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
