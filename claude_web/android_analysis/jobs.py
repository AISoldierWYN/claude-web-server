"""Per-session Android analysis job storage."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


def _now() -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S')


_JOB_LOCKS: dict[str, threading.RLock] = {}
_JOB_LOCKS_GUARD = threading.Lock()


def _job_lock(path: Path) -> threading.RLock:
    """同一 job 目录可能被后台分析线程和 SSE 线程同时读写，按目录串行化 IO。"""
    key = str(path.resolve()).lower()
    with _JOB_LOCKS_GUARD:
        lock = _JOB_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _JOB_LOCKS[key] = lock
        return lock


class AndroidAnalysisJobStore:
    def __init__(self, session_dir: Path):
        self.session_dir = Path(session_dir)
        self.base_dir = self.session_dir / 'android_analysis'
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        return self.base_dir / job_id

    def artifacts_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / 'artifacts'

    def extracted_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / 'extracted'

    def input_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / 'input'

    def create_job(
        self,
        question: str,
        source_files: Optional[Iterable[str]] = None,
        bundle_ids: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        job_id = str(uuid.uuid4())
        job_dir = self.job_dir(job_id)
        for child in ('input', 'extracted', 'artifacts'):
            (job_dir / child).mkdir(parents=True, exist_ok=True)

        job = {
            'id': job_id,
            'status': 'initialized',
            'question': question or '',
            'source_files': list(source_files or []),
            'bundle_ids': list(bundle_ids or []),
            'created_at': _now(),
            'updated_at': _now(),
            'artifacts': {},
            'error': None,
        }
        self._write_job(job)
        self.append_event(job_id, 'job_initialized', {'status': job['status']})
        return job

    def load_job(self, job_id: str) -> Dict[str, Any]:
        path = self.job_dir(job_id) / 'job.json'
        with _job_lock(self.job_dir(job_id)):
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)

    def update_job(self, job_id: str, **updates) -> Dict[str, Any]:
        with _job_lock(self.job_dir(job_id)):
            job = self.load_job(job_id)
            job.update(updates)
            job['updated_at'] = _now()
            self._write_job(job)
            self.append_event(job_id, 'job_updated', {'status': job.get('status')})
            return job

    def append_event(self, job_id: str, event_type: str, data: Optional[Dict[str, Any]] = None) -> None:
        path = self.job_dir(job_id) / 'events.jsonl'
        with _job_lock(self.job_dir(job_id)):
            path.parent.mkdir(parents=True, exist_ok=True)
            event = {
                'type': event_type,
                'timestamp': _now(),
                'data': data or {},
            }
            with open(path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n')

    def read_events(self, job_id: str) -> list[Dict[str, Any]]:
        path = self.job_dir(job_id) / 'events.jsonl'
        with _job_lock(self.job_dir(job_id)):
            if not path.is_file():
                return []
            events = []
            for line in path.read_text(encoding='utf-8').splitlines():
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    events.append(data)
            return events

    def _write_job(self, job: Dict[str, Any]) -> None:
        path = self.job_dir(job['id']) / 'job.json'
        with _job_lock(self.job_dir(job['id'])):
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.json.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(job, f, ensure_ascii=False, indent=2)
            tmp.replace(path)
