"""上传文件名：在防穿越前提下尽量保留用户原始 basename（含 Unicode）与扩展名。"""

import re
import uuid
from pathlib import Path

_WIN_RESERVED = frozenset({
    'CON', 'PRN', 'AUX', 'NUL',
    *(f'COM{i}' for i in range(1, 10)),
    *(f'LPT{i}' for i in range(1, 10)),
})

_ILLEGAL_WIN = re.compile(r'[<>:"|?*\\/\x00-\x1f]')


def safe_client_filename(original: str, max_len: int = 200) -> str:
    """
    仅取 basename，拒绝路径段中的「..」，替换非法字符，
    保留 Unicode；非法或空时返回 upload_<uuid>.bin
    """
    if not original or not isinstance(original, str):
        return f'upload_{uuid.uuid4().hex[:12]}.bin'

    name = original.replace('\\', '/').split('/')[-1].strip()
    if not name or '..' in name:
        return f'upload_{uuid.uuid4().hex[:12]}.bin'

    name = Path(name).name
    if not name or name in ('.', '..'):
        return f'upload_{uuid.uuid4().hex[:12]}.bin'

    name = _ILLEGAL_WIN.sub('_', name)
    name = name.rstrip(' .')
    if not name:
        return f'upload_{uuid.uuid4().hex[:12]}.bin'

    stem = Path(name).stem
    suffix = Path(name).suffix

    if not stem.strip('._-'):
        ext = suffix if suffix else '.bin'
        if not ext.startswith('.'):
            ext = '.' + ext
        return f'upload_{uuid.uuid4().hex[:8]}{ext}'

    if stem.upper() in _WIN_RESERVED:
        stem = '_' + stem

    base = stem + suffix
    if len(base) > max_len:
        if len(suffix) <= max_len:
            base = stem[: max(1, max_len - len(suffix))] + suffix
        else:
            base = base[:max_len]

    return base if base else f'upload_{uuid.uuid4().hex[:12]}.bin'


def is_ascii_filename(name: str) -> bool:
    """是否可仅用 ASCII 安全传递（供上游工具/API 路径，避免 invalid params）。"""
    if not name:
        return True
    try:
        name.encode('ascii')
        return True
    except UnicodeEncodeError:
        return False


def ascii_storage_filename(from_sanitized_basename: str) -> str:
    """在保留扩展名的前提下生成磁盘上的纯 ASCII 文件名。"""
    ext = Path(from_sanitized_basename).suffix or '.bin'
    return f'f_{uuid.uuid4().hex[:16]}{ext}'
