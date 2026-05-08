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
    for path in sorted((knowledge_root / 'global').glob('*/rules/*.json')):
        packs.extend(_load_rule_file(path, source='knowledge'))
    for path in sorted((knowledge_root / 'bundles').glob('*/rules/*.json')):
        packs.extend(_load_rule_file(path, source='knowledge'))

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


def _should_include_pack(pack: Dict[str, Any], wanted_packs: set[str], wanted_bundles: set[str]) -> bool:
    pack_id = str(pack.get('id') or '')
    if pack_id == 'android-base':
        return True
    if wanted_packs and pack_id in wanted_packs:
        return True
    pack_bundle_ids = {str(x) for x in (pack.get('source_bundle_ids') or []) if x}
    if wanted_bundles and pack_bundle_ids.intersection(wanted_bundles):
        return True
    return not wanted_packs and not wanted_bundles and pack.get('source') == 'builtin'
