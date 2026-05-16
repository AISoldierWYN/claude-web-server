"""Rule pack loading for Android issue analysis."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List


_BUILTIN_RULES_DIR = Path(__file__).resolve().parent / 'rules'


def load_rule_packs(
    knowledge_dir: Path,
    candidate_rule_packs: Iterable[str] | None = None,
    candidate_bundle_ids: Iterable[str] | None = None,
) -> List[Dict[str, Any]]:
    # android-base 永远加载；项目专项规则只有在 Planner 命中 rule_pack 或 bundle id 时加载。
    # 这能让普通 Android crash 有基础覆盖，同时避免每轮分析都加载所有项目知识。
    wanted_packs = {str(x).strip() for x in (candidate_rule_packs or []) if str(x).strip()}
    wanted_bundles = {str(x).strip() for x in (candidate_bundle_ids or []) if str(x).strip()}

    packs: List[Dict[str, Any]] = []
    for path in sorted(_BUILTIN_RULES_DIR.glob('*.json')):
        packs.extend(_load_rule_file(path, source='builtin'))

    knowledge_root = Path(knowledge_dir)
    bundle_rule_allowlist = _load_bundle_rule_allowlist(knowledge_root)
    for path in sorted((knowledge_root / 'global').glob('*/rules/*.json')):
        packs.extend(_load_rule_file(path, source='knowledge'))
    for path in sorted((knowledge_root / 'bundles').glob('*/rules/*.json')):
        bundle_id = _bundle_id_for_rule_path(path)
        loaded = _load_rule_file(path, source='knowledge')
        has_bundle_manifest = bool(bundle_id and bundle_id in bundle_rule_allowlist)
        allowlist = bundle_rule_allowlist.get(bundle_id, set()) if bundle_id else set()
        for pack in loaded:
            pack['declared_bundle_id'] = bundle_id
            pack['declared_by_bundle_config'] = (not has_bundle_manifest) or str(pack.get('id') or '') in allowlist
        packs.extend(loaded)

    out: List[Dict[str, Any]] = []
    seen = set()
    for pack in packs:
        pack_id = str(pack.get('id') or '').strip()
        if not pack_id or pack_id in seen:
            continue
        if _should_include_pack(pack, wanted_packs, wanted_bundles):
            seen.add(pack_id)
            out.append(pack)
    return out


def _load_rule_file(path: Path, source: str) -> List[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return []
    raw_packs = data.get('rule_packs') if isinstance(data, dict) else None
    if isinstance(raw_packs, list):
        packs = raw_packs
    elif isinstance(data, dict):
        packs = [data]
    else:
        return []

    out: List[Dict[str, Any]] = []
    for pack in packs:
        if not isinstance(pack, dict) or not pack.get('id'):
            continue
        normalized = dict(pack)
        normalized['source'] = source
        normalized['source_file'] = path.name
        normalized['rules'] = [r for r in (pack.get('rules') or []) if isinstance(r, dict) and r.get('id')]
        out.append(normalized)
    return out


def _bundle_id_for_rule_path(path: Path) -> str:
    try:
        return path.parent.parent.name
    except Exception:
        return ''


def _load_bundle_rule_allowlist(knowledge_root: Path) -> Dict[str, set[str]]:
    out: Dict[str, set[str]] = {}
    bundles_dir = knowledge_root / 'bundles'
    if not bundles_dir.is_dir():
        return out
    for bundle_json in sorted(bundles_dir.glob('*/bundle.json')):
        try:
            data = json.loads(bundle_json.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        bundle_id = str(data.get('id') or bundle_json.parent.name).strip()
        rule_packs = {str(x).strip() for x in (data.get('rule_packs') or []) if str(x).strip()}
        if bundle_id:
            out[bundle_id] = rule_packs
    return out


def _should_include_pack(pack: Dict[str, Any], wanted_packs: set[str], wanted_bundles: set[str]) -> bool:
    pack_id = str(pack.get('id') or '')
    if pack_id == 'android-base':
        return True
    if wanted_packs:
        return pack_id in wanted_packs
    pack_bundle_ids = {str(x) for x in (pack.get('source_bundle_ids') or []) if x}
    if wanted_bundles and pack_bundle_ids.intersection(wanted_bundles):
        # 若 bundle.json 明确声明了 rule_packs，则只随 bundle 自动加载声明过的包。
        # 未声明的 generated/draft 包仍可通过 candidate_rule_packs 显式请求加载。
        return bool(pack.get('declared_by_bundle_config', True))
    return not wanted_packs and not wanted_bundles and pack.get('source') == 'builtin'
