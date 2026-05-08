"""Local shared Android analysis knowledge store helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


def list_bundles(knowledge_dir: Path) -> List[Dict[str, Any]]:
    bundles_dir = Path(knowledge_dir) / 'bundles'
    if not bundles_dir.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for bundle_json in sorted(bundles_dir.glob('*/bundle.json')):
        try:
            data = json.loads(bundle_json.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get('id'):
            public = dict(data)
            public.pop('source_path', None)
            out.append(public)
    return out

