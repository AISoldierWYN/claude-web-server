"""Structured debug tracing for Android analysis jobs."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict


TraceFn = Callable[[str, str, Dict[str, Any]], None]


class AndroidAnalysisDebugTracer:
    """Write bounded, structured debug records to artifacts and server logs."""

    def __init__(
        self,
        enabled: bool,
        artifacts_dir: Path,
        logger,
        job_id: str = '',
        event_sink: Callable[[str, Dict[str, Any]], None] | None = None,
    ):
        self.enabled = bool(enabled)
        self.artifacts_dir = Path(artifacts_dir)
        self.logger = logger
        self.job_id = job_id
        self.event_sink = event_sink
        self.path = self.artifacts_dir / 'android_debug_trace.jsonl'
        if self.enabled:
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def trace(self, stage: str, event: str, data: Dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        safe_data = _safe_json(data or {})
        record = {
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'job_id': self.job_id,
            'stage': stage,
            'event': event,
            'data': safe_data,
        }
        line = json.dumps(record, ensure_ascii=False, separators=(',', ':'))
        try:
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except OSError as exc:
            if self.logger:
                self.logger.warning('[AndroidAnalysisDebug] failed to write trace: %s', exc)
        if self.logger:
            if event in {'ai_thinking_delta', 'ai_text_delta', 'ai_tool_event'}:
                self.logger.debug('[AndroidAnalysisDebugStream] %s', line[:1200])
            else:
                self.logger.info('[AndroidAnalysisDebug] %s', line[:12000])
        if self.event_sink and event in {'ai_thinking_delta', 'ai_text_delta', 'ai_tool_event'}:
            try:
                self.event_sink(
                    event,
                    {
                        'stage': stage,
                        'event': event,
                        'content': _short_text(str(safe_data.get('content') or ''), 800),
                        'interaction': safe_data.get('interaction') or '',
                    },
                )
            except Exception as exc:
                if self.logger:
                    self.logger.warning('[AndroidAnalysisDebug] failed to sink trace event: %s', exc)


def null_trace(stage: str, event: str, data: Dict[str, Any] | None = None) -> None:
    return None


def _safe_json(value: Any, depth: int = 0) -> Any:
    """Keep trace useful without letting huge log snippets flood console/files."""
    if isinstance(value, (str, bytes)):
        text = value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value
        return _short_text(text, 4000)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth > 6:
        return _short_text(repr(value), 300)
    if isinstance(value, dict):
        out = {}
        for idx, (key, item) in enumerate(value.items()):
            if idx >= 120:
                out['__truncated_keys__'] = len(value) - idx
                break
            out[str(key)] = _safe_json(item, depth + 1)
        return out
    if isinstance(value, list):
        limit = 120
        out = [_safe_json(item, depth + 1) for item in value[:limit]]
        if len(value) > limit:
            out.append({'__truncated_items__': len(value) - limit})
        return out
    if isinstance(value, tuple):
        return [_safe_json(item, depth + 1) for item in value[:120]]
    return _short_text(repr(value), 1000)


def _short_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f'\n...<truncated {len(text) - limit} chars>'
