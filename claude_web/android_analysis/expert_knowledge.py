"""Project-local Android expert workbench knowledge loading.

Phase 1 only scans, validates, and caches knowledge packs. It does not feed the
new data into the existing Android analysis pipeline yet.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


DEFAULT_PROJECT_KNOWLEDGE_RELATIVE_PATH = '.claude-web/android-analysis'


def build_expert_knowledge_cache(
    configured_bundles: Iterable[Dict[str, Any]] | None,
    global_knowledge_dir: Path,
    project_relative_path: str = DEFAULT_PROJECT_KNOWLEDGE_RELATIVE_PATH,
    log=None,
) -> Dict[str, Any]:
    """Scan configured project roots and return a read-only cache dictionary."""

    rel = _normalize_relative_path(project_relative_path)
    cache: Dict[str, Any] = {
        'version': 1,
        'project_knowledge_relative_path': rel,
        'modules': [],
        'module_index': {},
        'global': _load_global_knowledge(global_knowledge_dir),
        'errors': [],
    }
    seen_modules: set[str] = set()
    for bundle in configured_bundles or []:
        if not isinstance(bundle, dict):
            continue
        bundle_id = str(bundle.get('id') or '').strip()
        bundle_title = str(bundle.get('title') or bundle.get('name') or bundle_id).strip()
        for root in _bundle_project_roots(bundle):
            for knowledge_dir in _iter_project_knowledge_dirs(root, rel):
                loaded = load_project_knowledge_dir(
                    knowledge_dir,
                    project_root=root,
                    bundle_id=bundle_id,
                    bundle_title=bundle_title,
                )
                module_id = str((loaded.get('module') or {}).get('id') or '').strip()
                if not module_id:
                    cache['errors'].extend(loaded.get('errors') or [])
                    continue
                if module_id in seen_modules:
                    cache['errors'].append(
                        {
                            'code': 'duplicate_module_id',
                            'module_id': module_id,
                            'path': str(knowledge_dir),
                            'message': f'Duplicate Android expert module id: {module_id}',
                        }
                    )
                    continue
                seen_modules.add(module_id)
                cache['modules'].append(_module_public_summary(loaded))
                cache['module_index'][module_id] = loaded
                cache['errors'].extend(loaded.get('errors') or [])
    if log:
        log.info(
            '[AndroidExpertKnowledge] loaded modules=%s, errors=%s, rel=%s',
            len(cache['modules']),
            len(cache['errors']),
            rel,
        )
    return cache


def load_project_knowledge_dir(
    knowledge_dir: Path,
    *,
    project_root: Path | None = None,
    bundle_id: str = '',
    bundle_title: str = '',
) -> Dict[str, Any]:
    """Load one ``.claude-web/android-analysis`` directory."""

    knowledge_dir = Path(knowledge_dir).resolve()
    project_root = Path(project_root).resolve() if project_root else knowledge_dir.parent.parent.resolve()
    errors: List[Dict[str, Any]] = []
    module = _read_json_object(knowledge_dir / 'module.json', required=True, errors=errors)
    module = _validate_module(module, errors, knowledge_dir)
    module_id = str(module.get('id') or '').strip()
    subcategories = _validate_subcategories(
        _read_json_array(knowledge_dir / 'subcategories.json', errors=errors),
        module_id,
        errors,
        knowledge_dir,
    )
    log_types = _validate_log_types(
        _read_json_array(knowledge_dir / 'log_types.json', errors=errors),
        errors,
        knowledge_dir,
        source='project',
    )
    evidence_templates = _validate_evidence_templates(
        _read_jsonl(knowledge_dir / 'evidence_templates.jsonl', errors=errors),
        module_id,
        {item.get('id') for item in subcategories if isinstance(item, dict)},
        errors,
        knowledge_dir,
    )
    xml_state_templates = _validate_xml_state_templates(
        _read_jsonl(knowledge_dir / 'xml_state_templates.jsonl', errors=errors),
        module_id,
        {item.get('id') for item in subcategories if isinstance(item, dict)},
        errors,
        knowledge_dir,
    )
    experience_logs = _validate_experience_logs(
        _read_jsonl(knowledge_dir / 'experience_logs.jsonl', errors=errors),
        errors,
        knowledge_dir,
        source='project',
    )
    app_inventory = _read_project_app_inventory(knowledge_dir, errors=errors)
    cases = _validate_case_cards(
        _read_jsonl(knowledge_dir / 'cases' / 'case_cards.jsonl', errors=errors),
        module_id,
        errors,
        knowledge_dir,
    )
    table_sources = _existing_table_sources(knowledge_dir)
    return {
        'module': module,
        'bundle_id': bundle_id,
        'bundle_title': bundle_title,
        'project_root': str(project_root),
        'knowledge_dir': str(knowledge_dir),
        'subcategories': subcategories,
        'evidence_templates': evidence_templates,
        'xml_state_templates': xml_state_templates,
        'experience_logs': experience_logs,
        'app_inventory': app_inventory,
        'log_types': log_types,
        'case_cards': cases,
        'table_sources': table_sources,
        'errors': errors,
    }


def summarize_expert_knowledge_cache(cache: Dict[str, Any] | None, *, include_details: bool = False) -> Dict[str, Any]:
    """Return a JSON-safe API summary."""

    cache = cache or {}
    modules = list(cache.get('modules') or [])
    out: Dict[str, Any] = {
        'version': cache.get('version') or 1,
        'project_knowledge_relative_path': cache.get('project_knowledge_relative_path') or DEFAULT_PROJECT_KNOWLEDGE_RELATIVE_PATH,
        'module_count': len(modules),
        'modules': modules,
        'global': _global_public_summary(cache.get('global') or {}),
        'error_count': len(cache.get('errors') or []),
        'errors': list(cache.get('errors') or [])[:50],
    }
    if include_details:
        details = []
        for module_id in sorted((cache.get('module_index') or {}).keys()):
            item = dict((cache.get('module_index') or {}).get(module_id) or {})
            details.append(_module_detail_summary(item))
        out['details'] = details
    return out


def _bundle_project_roots(bundle: Dict[str, Any]) -> List[Path]:
    roots: List[Path] = []
    for raw in bundle.get('paths') or []:
        try:
            path = Path(str(raw)).expanduser().resolve()
        except OSError:
            continue
        if path.is_dir() and path not in roots:
            roots.append(path)
    return roots


def _iter_project_knowledge_dirs(root: Path, rel: str) -> List[Path]:
    """Return the root pack and optional nested module packs under one project.

    Large Android checkouts often contain several logical modules under the same
    repository root. Keeping them under ``.claude-web/android-analysis/modules``
    lets one configured path expose many independently routable knowledge packs.
    """

    base = (Path(root) / rel).resolve()
    out: List[Path] = []
    if (base / 'module.json').is_file():
        out.append(base)
    modules_dir = base / 'modules'
    if modules_dir.is_dir():
        for child in sorted(modules_dir.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir() and (child / 'module.json').is_file():
                resolved = child.resolve()
                if resolved not in out:
                    out.append(resolved)
    return out


def _normalize_relative_path(value: str) -> str:
    value = str(value or DEFAULT_PROJECT_KNOWLEDGE_RELATIVE_PATH).replace('\\', '/').strip().strip('/')
    if not value or value.startswith('../') or '/..' in value or ':' in value:
        return DEFAULT_PROJECT_KNOWLEDGE_RELATIVE_PATH
    return value


def _read_json_object(path: Path, *, required: bool, errors: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not path.is_file():
        if required:
            errors.append(_err('missing_file', path, 'Required JSON file is missing.'))
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(_err('invalid_json', path, str(exc)))
        return {}
    if not isinstance(data, dict):
        errors.append(_err('invalid_schema', path, 'JSON root must be an object.'))
        return {}
    return data


def _read_json_array(path: Path, *, errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(_err('invalid_json', path, str(exc)))
        return []
    if isinstance(data, dict):
        data = data.get('items') or data.get('log_types') or data.get('subcategories') or []
    if not isinstance(data, list):
        errors.append(_err('invalid_schema', path, 'JSON root must be an array or object with an items array.'))
        return []
    return [item for item in data if isinstance(item, dict)]


def _read_jsonl(path: Path, *, errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    out: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError as exc:
        errors.append(_err('read_error', path, str(exc)))
        return out
    for idx, line in enumerate(lines, start=1):
        text = line.strip()
        if not text or text.startswith('#'):
            continue
        try:
            item = json.loads(text)
        except json.JSONDecodeError as exc:
            errors.append(_err('invalid_jsonl', path, f'line {idx}: {exc}'))
            continue
        if not isinstance(item, dict):
            errors.append(_err('invalid_schema', path, f'line {idx}: JSONL item must be an object.'))
            continue
        out.append(item)
    return out


def _read_project_app_inventory(knowledge_dir: Path, *, errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Load app inventory files from one project knowledge directory.

    用户从设备上导出的应用清单通常是 ``pm list packages -U`` 的原始文本。
    这里同时支持 JSON 和这种文本格式，避免每次都手动转换成 JSON。
    """

    paths: List[Path] = []
    for name in ('app_inventory.json', 'app_inventory.txt'):
        path = knowledge_dir / name
        if path.is_file():
            paths.append(path)
    inventory_dir = knowledge_dir / 'app_inventory'
    if inventory_dir.is_dir():
        paths.extend(sorted(inventory_dir.glob('*.json')))
        paths.extend(sorted(inventory_dir.glob('*.txt')))
    out: List[Dict[str, Any]] = []
    for path in paths:
        out.extend(_read_app_inventory(path, errors=errors))
    return out


def _read_app_inventory(path: Path, *, errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    if path.suffix.lower() in ('.txt', '.list', '.lst'):
        return _read_app_inventory_text(path, errors=errors)
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(_err('invalid_app_inventory', path, str(exc)))
        return []
    if isinstance(raw, dict):
        items = raw.get('apps') or raw.get('packages') or raw.get('items') or []
    elif isinstance(raw, list):
        items = raw
    else:
        errors.append(_err('invalid_app_inventory', path, 'app_inventory.json must be an array or object with apps/packages/items.'))
        return []
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        package_name = str(
            item.get('package_name')
            or item.get('packageName')
            or item.get('package')
            or item.get('id')
            or ''
        ).strip()
        if not package_name:
            continue
        out.append(
            {
                'package_name': package_name,
                'label': str(item.get('label') or item.get('name') or item.get('app_label') or '').strip(),
                'aliases': _list_str(item.get('aliases') or item.get('alias') or item.get('keywords')),
                'uid': item.get('uid'),
                'version_name': item.get('versionName') or item.get('version_name'),
                'version_code': item.get('versionCode') or item.get('version_code'),
                'launcher_activity': item.get('launcherActivity') or item.get('launcher_activity') or item.get('component'),
                'source': str(item.get('source') or path.name),
            }
        )
    return out


def _read_app_inventory_text(path: Path, *, errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Parse ``package:<name> uid:<uid>`` lines exported from Android devices."""

    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError as exc:
        errors.append(_err('read_error', path, str(exc)))
        return []
    out: List[Dict[str, Any]] = []
    pattern = re.compile(r'\bpackage:([A-Za-z][\w]*(?:\.[A-Za-z_][\w]*)*)\s+uid:(u\d+a\d+|\d{1,7})\b', re.IGNORECASE)
    for idx, line in enumerate(lines, start=1):
        text = line.strip()
        if not text or text.startswith('#'):
            continue
        match = pattern.search(text)
        if not match:
            errors.append(_err('invalid_app_inventory_line', path, f'line {idx}: expected "package:<name> uid:<uid>".'))
            continue
        package_name, uid = match.groups()
        out.append(
            {
                'package_name': package_name,
                'label': '',
                'aliases': [],
                'uid': uid,
                'version_name': None,
                'version_code': None,
                'launcher_activity': None,
                'source': path.name,
            }
        )
    return out


def _validate_module(data: Dict[str, Any], errors: List[Dict[str, Any]], base: Path) -> Dict[str, Any]:
    module = dict(data or {})
    for key in ('id', 'title', 'description'):
        if not str(module.get(key) or '').strip():
            errors.append(_err('invalid_module', base / 'module.json', f'Missing required field: {key}'))
    module['id'] = _clean_id(module.get('id'))
    module['title'] = str(module.get('title') or module.get('id') or '').strip()
    module['description'] = str(module.get('description') or '').strip()
    module['source_roots'] = _list_str(module.get('source_roots') or ['.'])
    module['skill_paths'] = _list_str(module.get('skill_paths'))
    module['guide_paths'] = _list_str(module.get('guide_paths'))
    module['default_package_names'] = _list_str(module.get('default_package_names') or module.get('package_names'))
    module['profiles'] = _list_str(module.get('profiles'))
    package_resolution = module.get('package_resolution')
    module['package_resolution'] = package_resolution if isinstance(package_resolution, dict) else {}
    return module


def _validate_subcategories(items: List[Dict[str, Any]], module_id: str, errors: List[Dict[str, Any]], base: Path) -> List[Dict[str, Any]]:
    out = []
    seen = set()
    for item in items:
        sid = _clean_id(item.get('id'))
        if not sid:
            errors.append(_err('invalid_subcategory', base / 'subcategories.json', 'Subcategory id is required.'))
            continue
        if sid in seen:
            errors.append(_err('duplicate_subcategory', base / 'subcategories.json', f'Duplicate subcategory id: {sid}'))
            continue
        seen.add(sid)
        out.append(
            {
                'id': sid,
                'module_id': str(item.get('module_id') or module_id),
                'title': str(item.get('title') or sid).strip(),
                'description': str(item.get('description') or '').strip(),
                'aliases': _list_str(item.get('aliases')),
            }
        )
    return out


def _validate_evidence_templates(
    items: List[Dict[str, Any]],
    module_id: str,
    subcategory_ids: set[str],
    errors: List[Dict[str, Any]],
    base: Path,
) -> List[Dict[str, Any]]:
    out = []
    seen = set()
    for item in items:
        tid = _clean_id(item.get('id'))
        if not tid:
            errors.append(_err('invalid_evidence_template', base / 'evidence_templates.jsonl', 'Template id is required.'))
            continue
        if tid in seen:
            errors.append(_err('duplicate_evidence_template', base / 'evidence_templates.jsonl', f'Duplicate template id: {tid}'))
            continue
        seen.add(tid)
        regex = str(item.get('regex') or '').strip()
        if not regex:
            errors.append(_err('invalid_evidence_template', base / 'evidence_templates.jsonl', f'{tid}: regex is required.'))
            continue
        try:
            re.compile(regex)
        except re.error as exc:
            errors.append(_err('invalid_regex', base / 'evidence_templates.jsonl', f'{tid}: {exc}'))
            continue
        sub_id = _clean_id(item.get('submodule_id') or item.get('subcategory_id'))
        if sub_id and subcategory_ids and sub_id not in subcategory_ids:
            errors.append(_err('unknown_subcategory', base / 'evidence_templates.jsonl', f'{tid}: unknown subcategory {sub_id}'))
        out.append(
            {
                'id': tid,
                'module_id': str(item.get('module_id') or module_id),
                'submodule_id': sub_id,
                'profile': str(item.get('profile') or 'functional').strip(),
                'log_type': str(item.get('log_type') or '').strip(),
                'regex': regex,
                'parameters': _list_str(item.get('parameters')),
                'code_location': str(item.get('code_location') or '').strip(),
                'meaning': str(item.get('meaning') or '').strip(),
                'severity': str(item.get('severity') or 'info').strip(),
                'time_anchor': bool(item.get('time_anchor')),
                'next_steps': _list_str(item.get('next_steps')),
                'enabled': item.get('enabled', True) is not False,
            }
        )
    return out


def _validate_xml_state_templates(
    items: List[Dict[str, Any]],
    module_id: str,
    subcategory_ids: set[str],
    errors: List[Dict[str, Any]],
    base: Path,
) -> List[Dict[str, Any]]:
    """Validate project-local XML/SP state evidence templates."""

    out = []
    seen = set()
    for item in items:
        tid = _clean_id(item.get('id'))
        if not tid:
            errors.append(_err('invalid_xml_state_template', base / 'xml_state_templates.jsonl', 'Template id is required.'))
            continue
        if tid in seen:
            errors.append(_err('duplicate_xml_state_template', base / 'xml_state_templates.jsonl', f'Duplicate template id: {tid}'))
            continue
        seen.add(tid)
        sub_id = _clean_id(item.get('submodule_id') or item.get('subcategory_id'))
        if sub_id and subcategory_ids and sub_id not in subcategory_ids:
            errors.append(_err('unknown_subcategory', base / 'xml_state_templates.jsonl', f'{tid}: unknown subcategory {sub_id}'))
        path_patterns = _list_str(item.get('path_patterns'))
        key_regex = str(item.get('key_regex') or '').strip()
        value_regex = str(item.get('value_regex') or '').strip()
        if not path_patterns:
            errors.append(_err('invalid_xml_state_template', base / 'xml_state_templates.jsonl', f'{tid}: path_patterns is required.'))
            continue
        if not key_regex:
            errors.append(_err('invalid_xml_state_template', base / 'xml_state_templates.jsonl', f'{tid}: key_regex is required.'))
            continue
        for field, patterns in (('path_patterns', path_patterns), ('key_regex', [key_regex]), ('value_regex', [value_regex] if value_regex else [])):
            for pattern in patterns:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    errors.append(_err('invalid_regex', base / 'xml_state_templates.jsonl', f'{tid}.{field}: {exc}'))
        out.append(
            {
                'id': tid,
                'module_id': str(item.get('module_id') or module_id),
                'submodule_id': sub_id,
                'profile': str(item.get('profile') or 'functional').strip(),
                'source_type': str(item.get('source_type') or 'shared_prefs_xml').strip(),
                'path_patterns': path_patterns,
                'key_regex': key_regex,
                'value_regex': value_regex,
                'value_source': str(item.get('value_source') or 'shared_prefs_value').strip(),
                'code_location': str(item.get('code_location') or '').strip(),
                'meaning': str(item.get('meaning') or '').strip(),
                'severity': str(item.get('severity') or 'info').strip(),
                'time_anchor': bool(item.get('time_anchor')),
                'next_steps': _list_str(item.get('next_steps')),
                'enabled': item.get('enabled', True) is not False,
            }
        )
    return out


def _validate_log_types(
    items: List[Dict[str, Any]],
    errors: List[Dict[str, Any]],
    base: Path,
    *,
    source: str,
) -> List[Dict[str, Any]]:
    out = []
    seen = set()
    for item in items:
        lid = _clean_id(item.get('id'))
        if not lid:
            errors.append(_err('invalid_log_type', base / 'log_types.json', 'Log type id is required.'))
            continue
        if lid in seen:
            errors.append(_err('duplicate_log_type', base / 'log_types.json', f'Duplicate log type id: {lid}'))
            continue
        seen.add(lid)
        path_patterns, content_patterns = _list_str(item.get('path_patterns')), _list_str(item.get('content_patterns'))
        for pattern in path_patterns + content_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append(_err('invalid_regex', base / 'log_types.json', f'{lid}: {exc}'))
        out.append(
            {
                'id': lid,
                'title': str(item.get('title') or lid).strip(),
                'path_patterns': path_patterns,
                'content_patterns': content_patterns,
                'priority': _int_value(item.get('priority'), 50),
                'notes': str(item.get('notes') or '').strip(),
                'source': source,
            }
        )
    return out


def _validate_experience_logs(
    items: List[Dict[str, Any]],
    errors: List[Dict[str, Any]],
    base: Path,
    *,
    source: str,
) -> List[Dict[str, Any]]:
    out = []
    for item in items:
        eid = _clean_id(item.get('id'))
        pattern = str(item.get('pattern') or '').strip()
        if not eid or not pattern:
            errors.append(_err('invalid_experience_log', base / 'experience_logs.jsonl', 'Experience item requires id and pattern.'))
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            errors.append(_err('invalid_regex', base / 'experience_logs.jsonl', f'{eid}: {exc}'))
            continue
        out.append(
            {
                'id': eid,
                'pattern': pattern,
                'meaning': str(item.get('meaning') or '').strip(),
                'typical_impact': str(item.get('typical_impact') or '').strip(),
                'owner_domain': str(item.get('owner_domain') or '').strip(),
                'next_steps': _list_str(item.get('next_steps')),
                'source': source,
            }
        )
    return out


def _validate_case_cards(items: List[Dict[str, Any]], module_id: str, errors: List[Dict[str, Any]], base: Path) -> List[Dict[str, Any]]:
    out = []
    for item in items:
        cid = _clean_id(item.get('case_id') or item.get('id'))
        if not cid:
            errors.append(_err('invalid_case_card', base / 'cases' / 'case_cards.jsonl', 'Case card id is required.'))
            continue
        out.append(
            {
                'case_id': cid,
                'module_id': str(item.get('module_id') or module_id),
                'submodule_id': _clean_id(item.get('submodule_id') or item.get('subcategory_id')),
                'profile': str(item.get('profile') or '').strip(),
                'summary': str(item.get('summary') or '').strip(),
                'embedding_text': str(item.get('embedding_text') or item.get('summary') or '').strip(),
                'key_evidence': _list_str(item.get('key_evidence')),
                'root_cause': str(item.get('root_cause') or '').strip(),
                'used_template_ids': _list_str(item.get('used_template_ids')),
                'handoff_domains': _list_str(item.get('handoff_domains')),
            }
        )
    return out


def _load_global_knowledge(global_knowledge_dir: Path) -> Dict[str, Any]:
    root = Path(global_knowledge_dir)
    errors: List[Dict[str, Any]] = []
    profiles = []
    profiles_dir = root / 'global' / 'profiles'
    if profiles_dir.is_dir():
        for path in sorted(profiles_dir.glob('*.json')):
            data = _read_json_object(path, required=False, errors=errors)
            if data:
                profiles.append({'id': _clean_id(data.get('id') or path.stem), 'title': str(data.get('title') or path.stem)})
    log_types = []
    log_types_dir = root / 'global' / 'log_types'
    if log_types_dir.is_dir():
        for path in sorted(log_types_dir.glob('*.json')):
            log_types.extend(_validate_log_types(_read_json_array(path, errors=errors), errors, path.parent, source='global'))
    experience = []
    exp_dir = root / 'global' / 'oem_experience'
    if exp_dir.is_dir():
        for path in sorted(exp_dir.glob('*.jsonl')):
            experience.extend(_validate_experience_logs(_read_jsonl(path, errors=errors), errors, path.parent, source='global'))
    app_inventory = []
    app_inventory_dir = root / 'global' / 'app_inventory'
    if app_inventory_dir.is_dir():
        for path in sorted(app_inventory_dir.glob('*.json')):
            app_inventory.extend(_read_app_inventory(path, errors=errors))
        for path in sorted(app_inventory_dir.glob('*.txt')):
            app_inventory.extend(_read_app_inventory(path, errors=errors))
    app_inventory_path = root / 'global' / 'app_inventory.json'
    if app_inventory_path.is_file():
        app_inventory.extend(_read_app_inventory(app_inventory_path, errors=errors))
    app_inventory_txt_path = root / 'global' / 'app_inventory.txt'
    if app_inventory_txt_path.is_file():
        app_inventory.extend(_read_app_inventory(app_inventory_txt_path, errors=errors))
    return {
        'knowledge_dir': str(root),
        'profiles': profiles,
        'log_types': log_types,
        'oem_experience': experience,
        'app_inventory': app_inventory,
        'errors': errors,
    }


def _global_public_summary(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'profile_count': len(data.get('profiles') or []),
        'profiles': data.get('profiles') or [],
        'log_type_count': len(data.get('log_types') or []),
        'log_types': [
            {'id': item.get('id'), 'title': item.get('title'), 'source': item.get('source')}
            for item in data.get('log_types') or []
        ],
        'oem_experience_count': len(data.get('oem_experience') or []),
        'app_inventory_count': len(data.get('app_inventory') or []),
        'error_count': len(data.get('errors') or []),
    }


def _module_public_summary(item: Dict[str, Any]) -> Dict[str, Any]:
    module = item.get('module') or {}
    return {
        'id': module.get('id'),
        'title': module.get('title'),
        'description': module.get('description'),
        'bundle_id': item.get('bundle_id'),
        'bundle_title': item.get('bundle_title'),
        'profiles': module.get('profiles') or [],
        'default_package_names': module.get('default_package_names') or [],
        'package_resolution': module.get('package_resolution') or {},
        'subcategory_count': len(item.get('subcategories') or []),
        'evidence_template_count': len(item.get('evidence_templates') or []),
        'xml_state_template_count': len(item.get('xml_state_templates') or []),
        'experience_log_count': len(item.get('experience_logs') or []),
        'app_inventory_count': len(item.get('app_inventory') or []),
        'log_type_count': len(item.get('log_types') or []),
        'case_count': len(item.get('case_cards') or []),
        'table_sources': item.get('table_sources') or [],
        'error_count': len(item.get('errors') or []),
    }


def _module_detail_summary(item: Dict[str, Any]) -> Dict[str, Any]:
    summary = _module_public_summary(item)
    summary.update(
        {
            'subcategories': item.get('subcategories') or [],
            'evidence_templates': [
                {
                    'id': t.get('id'),
                    'submodule_id': t.get('submodule_id'),
                    'profile': t.get('profile'),
                    'log_type': t.get('log_type'),
                    'regex': t.get('regex'),
                    'code_location': t.get('code_location'),
                    'meaning': t.get('meaning'),
                    'severity': t.get('severity'),
                    'time_anchor': t.get('time_anchor'),
                    'enabled': t.get('enabled'),
                    'parameters': t.get('parameters') or [],
                }
                for t in item.get('evidence_templates') or []
            ],
            'xml_state_templates': [
                {
                    'id': t.get('id'),
                    'submodule_id': t.get('submodule_id'),
                    'profile': t.get('profile'),
                    'source_type': t.get('source_type'),
                    'path_patterns': t.get('path_patterns') or [],
                    'key_regex': t.get('key_regex'),
                    'value_regex': t.get('value_regex'),
                    'value_source': t.get('value_source'),
                    'code_location': t.get('code_location'),
                    'meaning': t.get('meaning'),
                    'severity': t.get('severity'),
                    'time_anchor': t.get('time_anchor'),
                    'enabled': t.get('enabled'),
                    'next_steps': t.get('next_steps') or [],
                }
                for t in item.get('xml_state_templates') or []
            ],
            'experience_logs': item.get('experience_logs') or [],
            'app_inventory': item.get('app_inventory') or [],
            'log_types': item.get('log_types') or [],
            'case_cards': item.get('case_cards') or [],
            'errors': item.get('errors') or [],
        }
    )
    return summary


def _existing_table_sources(base: Path) -> List[str]:
    return [
        name
        for name in (
            'evidence_templates.csv',
            'evidence_templates.xlsx',
            'xml_state_templates.csv',
            'xml_state_templates.xlsx',
        )
        if (base / name).is_file()
    ]


def _clean_id(value: Any) -> str:
    text = str(value or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_.:-]+', text):
        text = re.sub(r'[^A-Za-z0-9_.:-]+', '-', text).strip('-')
    return text


def _list_str(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in re.split(r'[;,]\s*|\n+', value) if p.strip()]
        return parts
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _err(code: str, path: Path, message: str) -> Dict[str, Any]:
    return {'code': code, 'path': str(path), 'message': message}
