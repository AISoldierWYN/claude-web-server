"""Safe archive extraction for Android analysis jobs."""

from __future__ import annotations

import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable

from .models import AndroidAnalysisError, ExtractionLimits


_SUPPORTED_SUFFIXES = {'.zip', '.tar', '.tgz', '.gz', '.bz2', '.xz', '.rar'}


def safe_extract_archive(
    archive_path: Path,
    dest_dir: Path,
    limits: ExtractionLimits | None = None,
    seven_zip_path: str = '',
) -> Dict[str, int]:
    # Android 日志包通常来自手机或测试机，必须先做大小、数量、路径边界校验，
    # 再写入 job 专属 extracted 目录，避免 Zip Slip 或超大压缩包影响主服务。
    limits = limits or ExtractionLimits()
    archive_path = Path(archive_path).resolve()
    dest_dir = Path(dest_dir).resolve()
    if not archive_path.is_file():
        raise AndroidAnalysisError('archive_not_found', 'Archive file does not exist.')
    if archive_path.stat().st_size > limits.max_archive_size:
        raise AndroidAnalysisError('archive_too_large', 'Archive exceeds the configured size limit.')

    suffixes = [s.lower() for s in archive_path.suffixes]
    archive_type = _detect_archive_type(archive_path)
    if archive_type == 'zip':
        return _extract_zip(archive_path, dest_dir, limits)
    if archive_type == 'rar':
        return _extract_rar_with_7z(archive_path, dest_dir, limits, seven_zip_path=seven_zip_path)
    if archive_type == 'tar' or (any(s in suffixes for s in _SUPPORTED_SUFFIXES) and tarfile.is_tarfile(archive_path)):
        return _extract_tar(archive_path, dest_dir, limits)
    raise AndroidAnalysisError('unsupported_archive', 'Only zip, tar, and rar archives are supported for now.')


def _detect_archive_type(archive_path: Path) -> str:
    """Prefer file signatures over suffixes because user log archives are often mislabeled."""
    try:
        header = archive_path.read_bytes()[:8]
    except OSError:
        return ''
    if header.startswith((b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08')):
        return 'zip'
    if header.startswith(b'Rar!\x1a\x07\x00') or header.startswith(b'Rar!\x1a\x07\x01\x00'):
        return 'rar'
    try:
        if tarfile.is_tarfile(archive_path):
            return 'tar'
    except (OSError, tarfile.TarError):
        return ''
    return ''


def _extract_zip(archive_path: Path, dest_dir: Path, limits: ExtractionLimits) -> Dict[str, int]:
    with zipfile.ZipFile(archive_path) as zf:
        members = zf.infolist()
        _validate_count(members, limits)
        total_size = 0
        files = 0
        dest_dir.mkdir(parents=True, exist_ok=True)
        for member in members:
            member_name = _zip_member_name(member)
            if member.is_dir():
                _safe_target(dest_dir, member_name, limits).mkdir(parents=True, exist_ok=True)
                continue
            if _zip_is_symlink(member):
                raise AndroidAnalysisError('archive_symlink', 'Symbolic links are not allowed in archives.')
            total_size += int(member.file_size)
            _validate_sizes(member.file_size, total_size, limits)
            target = _safe_target(dest_dir, member_name, limits)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member, 'r') as src, open(target, 'wb') as dst:
                shutil.copyfileobj(src, dst)
            files += 1
    return {'files': files, 'total_size': total_size}


def _zip_member_name(member: zipfile.ZipInfo) -> str:
    """Recover common Windows/Android ZIP names that were encoded as GBK/CP936."""
    name = member.filename
    if member.flag_bits & 0x800 and _zip_name_mojibake_score(name) <= 0:
        return name
    try:
        raw = name.encode('cp437')
    except UnicodeEncodeError:
        return name
    candidates = [name]
    for encoding in ('gbk', 'cp936', 'utf-8'):
        try:
            decoded = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if decoded not in candidates:
            candidates.append(decoded)
    return min(candidates, key=_zip_name_mojibake_score)


def _zip_name_mojibake_score(name: str) -> int:
    weird = sum(1 for ch in name if ch in '┐└┘┌┬┴┼═║╧╦╩╔╗╝╚╠╣�')
    controls = sum(1 for ch in name if ord(ch) < 32)
    cjk = len(re.findall(r'[\u4e00-\u9fff]', name))
    return weird * 8 + controls * 12 - cjk


def _extract_tar(archive_path: Path, dest_dir: Path, limits: ExtractionLimits) -> Dict[str, int]:
    with tarfile.open(archive_path) as tf:
        members = tf.getmembers()
        _validate_count(members, limits)
        total_size = 0
        files = 0
        dest_dir.mkdir(parents=True, exist_ok=True)
        for member in members:
            if member.isdir():
                _safe_target(dest_dir, member.name, limits).mkdir(parents=True, exist_ok=True)
                continue
            if member.issym() or member.islnk():
                raise AndroidAnalysisError('archive_symlink', 'Links are not allowed in archives.')
            if not member.isfile():
                continue
            total_size += int(member.size)
            _validate_sizes(member.size, total_size, limits)
            target = _safe_target(dest_dir, member.name, limits)
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                continue
            with src, open(target, 'wb') as dst:
                shutil.copyfileobj(src, dst)
            files += 1
    return {'files': files, 'total_size': total_size}


def _extract_rar_with_7z(
    archive_path: Path,
    dest_dir: Path,
    limits: ExtractionLimits,
    seven_zip_path: str = '',
) -> Dict[str, int]:
    exe = _find_7z(seven_zip_path)
    if not exe:
        raise AndroidAnalysisError('rar_tool_missing', 'RAR extraction requires 7-Zip, but 7z.exe was not found.')
    # 7-Zip 没有 Python 标准库等价实现；这里先列表校验，再解到 staging，
    # 最后逐文件搬运到目标目录，保证路径穿越和符号链接不会绕过我们的检查。
    entries = _list_7z_entries(exe, archive_path)
    _validate_7z_entries(dest_dir, entries, limits)

    staging = (dest_dir.parent / f'.{dest_dir.name}_7z_staging').resolve()
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [exe, 'x', '-y', f'-o{str(staging)}', str(archive_path)],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=120,
            shell=False,
        )
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or '').strip()[:1000]
            raise AndroidAnalysisError('rar_extract_failed', message or '7-Zip failed to extract the RAR archive.')
        return _move_validated_staging(staging, dest_dir, limits)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _find_7z(explicit_path: str = '') -> str:
    explicit = str(explicit_path or '').strip().strip('"')
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p)
    found = shutil.which('7z') or shutil.which('7za')
    if found:
        return found
    for raw in (
        r'C:\Program Files\7-Zip\7z.exe',
        r'C:\Program Files (x86)\7-Zip\7z.exe',
        r'D:\Program Files\7-Zip\7z.exe',
        r'D:\Program Files (x86)\7-Zip\7z.exe',
    ):
        p = Path(raw)
        if p.is_file():
            return str(p)
    return ''


def _list_7z_entries(exe: str, archive_path: Path) -> list[dict]:
    proc = subprocess.run(
        [exe, 'l', '-slt', str(archive_path)],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=60,
        shell=False,
    )
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or '').strip()[:1000]
        raise AndroidAnalysisError('rar_list_failed', message or '7-Zip failed to list the RAR archive.')
    entries = []
    current: dict = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        if ' = ' not in line:
            continue
        key, value = line.split(' = ', 1)
        if key == 'Path' and current:
            entries.append(current)
            current = {}
        current[key] = value
    if current:
        entries.append(current)
    return [e for e in entries if 'Folder' in e and e.get('Path')]


def _validate_7z_entries(dest_dir: Path, entries: list[dict], limits: ExtractionLimits) -> None:
    _validate_count(entries, limits)
    total_size = 0
    for entry in entries:
        path = entry.get('Path') or ''
        _safe_target(dest_dir, path, limits)
        if (entry.get('Symbolic Link') or '').strip() or (entry.get('Hard Link') or '').strip():
            raise AndroidAnalysisError('archive_symlink', 'Links are not allowed in archives.')
        if entry.get('Folder') == '+':
            continue
        try:
            size = int(entry.get('Size') or 0)
        except ValueError:
            size = 0
        total_size += size
        _validate_sizes(size, total_size, limits)


def _move_validated_staging(staging: Path, dest_dir: Path, limits: ExtractionLimits) -> Dict[str, int]:
    total_size = 0
    files = 0
    for path in sorted(staging.rglob('*')):
        rel = path.relative_to(staging).as_posix()
        target = _safe_target(dest_dir, rel, limits)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not path.is_file():
            continue
        size = path.stat().st_size
        total_size += size
        files += 1
        if files > limits.max_files:
            raise AndroidAnalysisError('too_many_files', 'Archive contains too many files.')
        _validate_sizes(size, total_size, limits)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))
    return {'files': files, 'total_size': total_size}


def _validate_count(members: Iterable[object], limits: ExtractionLimits) -> None:
    if len(list(members)) > limits.max_files:
        raise AndroidAnalysisError('too_many_files', 'Archive contains too many entries.')


def _validate_sizes(file_size: int, total_size: int, limits: ExtractionLimits) -> None:
    if file_size > limits.max_file_size:
        raise AndroidAnalysisError('file_too_large', 'Archive member exceeds the single-file size limit.')
    if total_size > limits.max_total_size:
        raise AndroidAnalysisError('expanded_too_large', 'Archive exceeds the expanded size limit.')


def _safe_target(dest_dir: Path, raw_name: str, limits: ExtractionLimits) -> Path:
    name = str(raw_name).replace('\\', '/').strip()
    pure = PurePosixPath(name)
    if not name or pure.is_absolute() or ':' in pure.parts[0] or any(part == '..' for part in pure.parts):
        raise AndroidAnalysisError('unsafe_path', 'Archive contains an unsafe path.')
    parts = [part for part in pure.parts if part not in ('', '.')]
    if len(parts) > limits.max_depth:
        raise AndroidAnalysisError('path_too_deep', 'Archive path exceeds the depth limit.')
    target = (dest_dir / Path(*parts)).resolve()
    try:
        target.relative_to(dest_dir)
    except ValueError as exc:
        raise AndroidAnalysisError('unsafe_path', 'Archive path escapes the destination directory.') from exc
    return target


def _zip_is_symlink(member: zipfile.ZipInfo) -> bool:
    return ((member.external_attr >> 16) & 0o170000) == 0o120000
