"""Bounded text sampling for Android analysis Planner input."""

from __future__ import annotations

import json
import gzip
import re
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Tuple

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

_TEXT_SUFFIXES = {
    '.txt',
    '.log',
    '.out',
    '.trace',
    '.traces',
    '.anr',
    '.csv',
    '.json',
    '.xml',
    '.prop',
}

_STATE_PATH_HINTS = {
    'shared_prefs',
    'settings',
    'state',
    'config',
    'policy',
    'devicepolicy',
    'device_policy',
    'dumpsys',
}

_CRASH_HINTS = {
    'crash',
    'exception',
    'fatal',
    'anr',
    'tombstone',
    'watchdog',
    'backtrace',
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
    priority_paths: Iterable[str] | None = None,
    limits: SampleLimits | None = None,
    phase: str = 'initial',
    debug_trace: Callable[[str, str, Dict[str, Any]], None] | None = None,
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
    priority_path_list = [str(x).replace('\\', '/').strip() for x in (priority_paths or []) if str(x).strip()]
    manifest_files = list(manifest.get('files') or [])
    ordered_manifest, priority_preview = _order_manifest_items(manifest_files, question, sample_keywords, priority_path_list)
    if debug_trace:
        debug_trace(
            'sampling',
            'keyword_plan',
            {
                'phase': phase,
                'question': question,
                'keyword_count': len(sample_keywords),
                'keywords': sample_keywords,
                'priority_paths': priority_path_list,
                'manifest_file_count': len(manifest_files),
                'top_priority_candidates': priority_preview,
                'limits': {
                    'max_files': limits.max_files,
                    'head_lines': limits.head_lines,
                    'tail_lines': limits.tail_lines,
                    'context_lines': limits.context_lines,
                    'max_keyword_matches_per_file': limits.max_keyword_matches_per_file,
                    'max_scan_bytes_per_file': limits.max_scan_bytes_per_file,
                    'max_chars_per_file': limits.max_chars_per_file,
                },
            },
        )
    sampled: List[Dict[str, Any]] = []
    considered_files = 0
    skipped_non_text = 0
    missing_files = 0
    for item in ordered_manifest:
        if len(sampled) >= limits.max_files:
            break
        considered_files += 1
        if not _should_sample(item):
            skipped_non_text += 1
            continue
        rel = str(item.get('path') or '')
        path = (extracted_dir / rel).resolve()
        try:
            path.relative_to(extracted_dir)
        except ValueError as exc:
            raise AndroidAnalysisError('sample_path_escape', 'Manifest path escapes extracted directory.') from exc
        if not path.is_file():
            missing_files += 1
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
    if debug_trace:
        debug_trace(
            'sampling',
            'sampling_result',
            _sampling_debug_summary(
                result,
                phase=phase,
                considered_files=considered_files,
                skipped_non_text=skipped_non_text,
                missing_files=missing_files,
                manifest_file_count=len(manifest_files),
                priority_paths=priority_path_list,
                keyword_count=len(sample_keywords),
            ),
        )
    return result


def _sampling_debug_summary(
    result: Dict[str, Any],
    phase: str,
    considered_files: int,
    skipped_non_text: int,
    missing_files: int,
    manifest_file_count: int,
    priority_paths: List[str],
    keyword_count: int,
) -> Dict[str, Any]:
    files = []
    total_keyword_samples = 0
    keyword_hit_counts: Dict[str, int] = {}
    for file_item in result.get('files') or []:
        per_file_hits: Dict[str, int] = {}
        for sample in file_item.get('samples') or []:
            if sample.get('type') != 'keyword':
                continue
            total_keyword_samples += 1
            keyword = str(sample.get('keyword') or '')
            if keyword:
                per_file_hits[keyword] = per_file_hits.get(keyword, 0) + 1
                keyword_hit_counts[keyword] = keyword_hit_counts.get(keyword, 0) + 1
        files.append(
            {
                'path': file_item.get('path'),
                'kind': file_item.get('kind'),
                'size': file_item.get('size'),
                'skipped': bool(file_item.get('skipped')),
                'skip_reason': file_item.get('skip_reason') or '',
                'sample_count': len(file_item.get('samples') or []),
                'keyword_sample_count': sum(per_file_hits.values()),
                'keyword_hits': per_file_hits,
                'keyword_scan_passes': 1 if not file_item.get('skipped') else 0,
                'keyword_candidates_per_pass': keyword_count if not file_item.get('skipped') else 0,
            }
        )
    return {
        'phase': phase,
        'manifest_file_count': manifest_file_count,
        'considered_files': considered_files,
        'not_considered_due_limit': max(0, manifest_file_count - considered_files),
        'sampled_file_count': result.get('file_count', 0),
        'skipped_non_text_count': skipped_non_text,
        'missing_file_count': missing_files,
        'priority_paths': priority_paths,
        'priority_paths_sampled': _priority_paths_sampled(result.get('files') or [], priority_paths),
        'keyword_count': keyword_count,
        'keyword_search_plan': {
            'scan_mode': 'single sequential pass per sampled text file',
            'total_file_keyword_scan_passes': sum(1 for f in files if not f.get('skipped')),
            'total_keyword_candidates_per_full_pass': keyword_count,
        },
        'total_keyword_samples': total_keyword_samples,
        'keyword_hit_counts': keyword_hit_counts,
        'files': files,
    }


def _order_manifest_items(
    items: List[Dict[str, Any]],
    question: str,
    keywords: List[str],
    priority_paths: List[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    scored: List[Tuple[int, int, Dict[str, Any], List[str]]] = []
    for index, item in enumerate(items):
        score, reasons = _sample_priority(item, question, keywords, priority_paths)
        scored.append((score, index, item, reasons))
    scored.sort(key=lambda x: (-x[0], x[1]))
    preview = [
        {
            'path': row[2].get('path'),
            'kind': row[2].get('kind'),
            'score': row[0],
            'reasons': row[3],
        }
        for row in scored[:40]
        if row[0] > 0
    ]
    return [row[2] for row in scored], preview


def _sample_priority(
    item: Dict[str, Any],
    question: str,
    keywords: List[str],
    priority_paths: List[str],
) -> Tuple[int, List[str]]:
    path = str(item.get('path') or '').replace('\\', '/')
    lower = path.lower()
    kind = str(item.get('kind') or '')
    suffix = Path(lower).suffix
    score = 0
    reasons: List[str] = []

    if _priority_path_match(lower, priority_paths):
        score += 1000
        reasons.append('planner_candidate_path')

    path_terms = _path_priority_terms(question, keywords)
    matched_terms = [term for term in path_terms if term and term in lower]
    if matched_terms:
        score += 120 + min(80, 10 * len(matched_terms))
        reasons.append('path_matches_focus_terms:' + ','.join(matched_terms[:6]))

    if 'shared_prefs' in lower or suffix in {'.xml', '.prop', '.json'}:
        score += 45
        reasons.append('structured_state_file')
    if any(hint in lower for hint in _STATE_PATH_HINTS):
        score += 45
        reasons.append('state_path_hint')
    if kind in {'android_events_log', 'android_logcat', 'android_main_log', 'android_system_log'}:
        score += 30
        reasons.append('android_runtime_log')
    if kind in {'android_crash', 'android_tombstone', 'android_anr_trace'}:
        score += 20
        reasons.append('failure_artifact')

    question_lower = (question or '').lower()
    question_mentions_crash = any(hint in question_lower for hint in _CRASH_HINTS)
    if not question_mentions_crash and ('dropbox' in lower or kind == 'android_crash'):
        score -= 80
        reasons.append('demoted_noisy_crash_bucket')
    if not _should_sample(item):
        score -= 500
        reasons.append('non_text_kind')
    return score, reasons


def _path_priority_terms(question: str, keywords: List[str]) -> List[str]:
    terms = set()
    text = (question or '').lower()
    for keyword in keywords:
        kw = str(keyword or '').lower().strip()
        if len(kw) >= 3 and re.fullmatch(r'[a-z0-9_.$:-]+', kw):
            terms.add(kw)
    for token in re.findall(r'[a-z0-9_.$:-]{3,}|[\u4e00-\u9fff]{2,}', text):
        terms.add(token)
    if any(word in text for word in ('锁', '锁机', '解锁', 'lock', 'unlock')):
        terms.update({'lock', 'unlock', 'device_lock', 'devicelock', 'policy', 'devicepolicy'})
    if any(word in text for word in ('策略', '设备策略', 'device policy', 'policy')):
        terms.update({'policy', 'devicepolicy', 'device_policy', 'settings'})
    return sorted(terms, key=len, reverse=True)

def _priority_path_match(path_lower: str, priority_paths: List[str]) -> bool:
    for candidate in priority_paths:
        c = candidate.lower().replace('\\', '/').strip()
        if not c:
            continue
        if c in path_lower or path_lower.endswith(c) or Path(path_lower).name == Path(c).name:
            return True
    return False


def _priority_paths_sampled(files: List[Dict[str, Any]], priority_paths: List[str]) -> List[Dict[str, Any]]:
    result = []
    for candidate in priority_paths:
        lower = candidate.lower().replace('\\', '/').strip()
        matches = [
            {
                'path': f.get('path'),
                'skipped': bool(f.get('skipped')),
                'skip_reason': f.get('skip_reason') or '',
                'sample_count': len(f.get('samples') or []),
            }
            for f in files
            if lower and _priority_path_match(str(f.get('path') or '').lower(), [lower])
        ]
        result.append({'candidate': candidate, 'matches': matches})
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
    return kind in _TEXT_KINDS or suffix in _TEXT_SUFFIXES


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
    if b'\x00' not in chunk:
        return False
    suffix = path.suffix.lower()
    if suffix in _TEXT_SUFFIXES and _looks_like_utf16_text(chunk):
        return False
    return True


def _open_text(path: Path):
    if path.suffix.lower() == '.gz':
        return gzip.open(path, 'rt', encoding='utf-8', errors='replace')
    encoding = _detect_text_encoding(path)
    return open(path, 'r', encoding=encoding, errors='replace')


def _detect_text_encoding(path: Path) -> str:
    try:
        with open(path, 'rb') as f:
            chunk = f.read(4096)
    except OSError:
        return 'utf-8'
    if chunk.startswith(b'\xff\xfe') or chunk.startswith(b'\xfe\xff'):
        return 'utf-16'
    if _looks_like_utf16_text(chunk):
        even_nulls = chunk[0::2].count(0)
        odd_nulls = chunk[1::2].count(0)
        return 'utf-16-be' if even_nulls > odd_nulls else 'utf-16-le'
    return 'utf-8'


def _looks_like_utf16_text(chunk: bytes) -> bool:
    if not chunk:
        return False
    sample = chunk[:1024]
    even_nulls = sample[0::2].count(0)
    odd_nulls = sample[1::2].count(0)
    pair_count = max(1, len(sample) // 2)
    if max(even_nulls, odd_nulls) / pair_count < 0.25:
        return False
    try:
        encoding = 'utf-16-be' if even_nulls > odd_nulls else 'utf-16-le'
        decoded = sample.decode(encoding, errors='strict')
    except UnicodeError:
        return False
    printable = sum(1 for ch in decoded if ch.isprintable() or ch in '\r\n\t')
    return printable / max(1, len(decoded)) > 0.8


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
