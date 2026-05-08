"""Bounded text sampling for Android analysis Planner input."""

from __future__ import annotations

import json
import gzip
import re
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .models import AndroidAnalysisError, SampleLimits


_DEFAULT_KEYWORDS = [
    'FATAL EXCEPTION',
    'AndroidRuntime',
    'ANR',
    'am_anr',
    'tombstone',
    'backtrace',
    'signal',
    'crash',
    'Exception',
    'Caused by',
    'Watchdog',
    'permission denial',
    'SecurityException',
    'INSTALL_FAILED',
    'force finishing',
    'system_server',
]

_TEXT_KINDS = {
    'android_anr_trace',
    'android_bugreport',
    'android_crash',
    'android_dumpsys',
    'android_events_log',
    'android_logcat',
    'android_main_log',
    'android_radio_log',
    'android_system_log',
    'android_tombstone',
    'text',
}


def build_sample_keywords(question: str = '', extra_keywords: Iterable[str] | None = None) -> List[str]:
    keywords: List[str] = []
    for item in list(extra_keywords or []) + _DEFAULT_KEYWORDS:
        _append_keyword(keywords, item)
    text = question or ''
    for token in re.findall(r'[A-Za-z0-9_.$:-]{3,}|[\u4e00-\u9fff]{2,}', text):
        _append_keyword(keywords, token)
    return keywords[:80]


def sample_files(
    extracted_dir: Path,
    artifacts_dir: Path,
    question: str = '',
    keywords: Iterable[str] | None = None,
    limits: SampleLimits | None = None,
) -> Dict[str, Any]:
    # Planner 只需要“看哪里”的路由信息，不能把完整 bugreport/logcat 全量塞给模型。
    # 因此每个候选文件只取 head/tail/关键词附近片段，并在文件数和字符数上做硬限制。
    limits = limits or SampleLimits()
    extracted_dir = Path(extracted_dir).resolve()
    artifacts_dir = Path(artifacts_dir).resolve()
    manifest_path = artifacts_dir / 'file_manifest.json'
    if not manifest_path.is_file():
        raise AndroidAnalysisError('manifest_missing', 'file_manifest.json does not exist.')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if not isinstance(manifest, dict):
        raise AndroidAnalysisError('manifest_invalid', 'file_manifest.json is invalid.')

    sample_keywords = build_sample_keywords(question, keywords)
    sampled: List[Dict[str, Any]] = []
    for item in manifest.get('files') or []:
        if len(sampled) >= limits.max_files:
            break
        if not _should_sample(item):
            continue
        rel = str(item.get('path') or '')
        path = (extracted_dir / rel).resolve()
        try:
            path.relative_to(extracted_dir)
        except ValueError as exc:
            raise AndroidAnalysisError('sample_path_escape', 'Manifest path escapes extracted directory.') from exc
        if not path.is_file():
            continue
        sampled.append(_sample_one_file(path, item, sample_keywords, limits))

    result = {
        'version': 1,
        'keyword_set': sample_keywords,
        'file_count': len(sampled),
        'files': sampled,
    }
    with open(artifacts_dir / 'file_samples.json', 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def _sample_one_file(path: Path, manifest_item: Dict[str, Any], keywords: List[str], limits: SampleLimits) -> Dict[str, Any]:
    rel = str(manifest_item.get('path') or path.name)
    if _looks_binary(path):
        return {
            'path': rel,
            'kind': manifest_item.get('kind') or 'unknown',
            'size': manifest_item.get('size') or path.stat().st_size,
            'skipped': True,
            'skip_reason': 'binary',
            'samples': [],
        }

    # 三类样本互补：头部看元信息，尾部看最近异常，关键词窗口看用户问题和通用 Android 故障信号。
    samples: List[Dict[str, Any]] = []
    samples.extend(_head_samples(path, limits))
    samples.extend(_tail_samples(path, limits))
    samples.extend(_keyword_samples(path, keywords, limits))
    samples = _dedupe_samples(samples)
    _trim_samples(samples, limits.max_chars_per_file)
    return {
        'path': rel,
        'kind': manifest_item.get('kind') or 'unknown',
        'size': manifest_item.get('size') or path.stat().st_size,
        'skipped': False,
        'samples': samples,
    }


def _head_samples(path: Path, limits: SampleLimits) -> List[Dict[str, Any]]:
    lines = []
    with _open_text(path) as f:
        for idx, line in enumerate(f, start=1):
            if idx > limits.head_lines:
                break
            lines.append((idx, line.rstrip('\n')))
    if not lines:
        return []
    return [_make_sample('head', lines)]


def _tail_samples(path: Path, limits: SampleLimits) -> List[Dict[str, Any]]:
    lines = deque(maxlen=limits.tail_lines)
    bytes_seen = 0
    with _open_text(path) as f:
        for idx, line in enumerate(f, start=1):
            bytes_seen += len(line.encode('utf-8', errors='ignore'))
            if bytes_seen > limits.max_scan_bytes_per_file:
                break
            lines.append((idx, line.rstrip('\n')))
    if not lines:
        return []
    return [_make_sample('tail', list(lines))]


def _keyword_samples(path: Path, keywords: List[str], limits: SampleLimits) -> List[Dict[str, Any]]:
    if not keywords:
        return []
    lowered = [(kw, kw.lower()) for kw in keywords if kw]
    samples = []
    before = deque(maxlen=limits.context_lines)
    after_remaining = 0
    current_keyword = ''
    current_lines: List[tuple[int, str]] = []
    bytes_seen = 0
    with _open_text(path) as f:
        for idx, line in enumerate(f, start=1):
            line = line.rstrip('\n')
            bytes_seen += len(line.encode('utf-8', errors='ignore'))
            if bytes_seen > limits.max_scan_bytes_per_file:
                break
            lower = line.lower()
            if after_remaining:
                current_lines.append((idx, line))
                after_remaining -= 1
                if after_remaining == 0:
                    samples.append(_make_sample('keyword', current_lines, keyword=current_keyword))
                    current_lines = []
                    current_keyword = ''
                    if len(samples) >= limits.max_keyword_matches_per_file:
                        break
                before.append((idx, line))
                continue
            hit = next((kw for kw, low in lowered if low in lower), '')
            if hit:
                current_keyword = hit
                current_lines = list(before) + [(idx, line)]
                after_remaining = limits.context_lines
                if after_remaining == 0:
                    samples.append(_make_sample('keyword', current_lines, keyword=current_keyword))
                    current_lines = []
                    current_keyword = ''
                    if len(samples) >= limits.max_keyword_matches_per_file:
                        break
            before.append((idx, line))
    if current_lines and len(samples) < limits.max_keyword_matches_per_file:
        samples.append(_make_sample('keyword', current_lines, keyword=current_keyword))
    return samples


def _make_sample(sample_type: str, lines: List[tuple[int, str]], keyword: str = '') -> Dict[str, Any]:
    start_line = lines[0][0]
    end_line = lines[-1][0]
    sample = {
        'type': sample_type,
        'start_line': start_line,
        'end_line': end_line,
        'content': '\n'.join(text for _, text in lines),
    }
    if keyword:
        sample['keyword'] = keyword
    return sample


def _should_sample(item: Dict[str, Any]) -> bool:
    kind = str(item.get('kind') or '')
    suffix = Path(str(item.get('path') or '')).suffix.lower()
    return kind in _TEXT_KINDS or suffix in {'.txt', '.log', '.trace', '.traces', '.anr'}


def _looks_binary(path: Path) -> bool:
    if path.suffix.lower() == '.gz':
        try:
            with gzip.open(path, 'rt', encoding='utf-8', errors='replace') as f:
                f.read(512)
            return False
        except OSError:
            return True
    with open(path, 'rb') as f:
        chunk = f.read(4096)
    return b'\x00' in chunk


def _open_text(path: Path):
    if path.suffix.lower() == '.gz':
        return gzip.open(path, 'rt', encoding='utf-8', errors='replace')
    return open(path, 'r', encoding='utf-8', errors='replace')


def _dedupe_samples(samples: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for sample in samples:
        key = (sample.get('type'), sample.get('start_line'), sample.get('end_line'), sample.get('keyword', ''))
        if key in seen:
            continue
        seen.add(key)
        out.append(sample)
    return out


def _trim_samples(samples: List[Dict[str, Any]], max_chars: int) -> None:
    remaining = max_chars
    for sample in samples:
        content = sample.get('content') or ''
        if remaining <= 0:
            sample['content'] = ''
            sample['truncated'] = True
            continue
        if len(content) > remaining:
            sample['content'] = content[:remaining]
            sample['truncated'] = True
            remaining = 0
        else:
            remaining -= len(content)


def _append_keyword(keywords: List[str], item: str) -> None:
    kw = str(item or '').strip()
    if not kw:
        return
    existing = {x.lower() for x in keywords}
    if kw.lower() not in existing:
        keywords.append(kw)
