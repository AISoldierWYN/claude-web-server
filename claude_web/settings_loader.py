"""从项目根目录 config.ini 加载配置；环境变量可覆盖（与 config 模块配合）。"""

from __future__ import annotations

import configparser
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple


def _truthy(s: str) -> bool:
    return s.strip().lower() in ('1', 'true', 'yes', 'on')


def _get_env_str(key: str) -> Optional[str]:
    v = os.environ.get(key)
    if v is None:
        return None
    v = v.strip()
    return v if v else None


def load_configparser(ini_path: Path) -> configparser.ConfigParser:
    p = configparser.ConfigParser(interpolation=None)
    if ini_path.is_file():
        p.read(ini_path, encoding='utf-8')
    return p


def get_str(
    parser: configparser.ConfigParser,
    section: str,
    key: str,
    default: str = '',
    env_key: Optional[str] = None,
) -> str:
    if env_key:
        ev = _get_env_str(env_key)
        if ev is not None:
            return ev
    if parser.has_option(section, key):
        try:
            return (parser.get(section, key) or '').strip()
        except (configparser.NoSectionError, configparser.NoOptionError):
            pass
    return default


def get_bool(
    parser: configparser.ConfigParser,
    section: str,
    key: str,
    default: bool,
    env_key: Optional[str] = None,
) -> bool:
    if env_key:
        ev = os.environ.get(env_key)
        if ev is not None and str(ev).strip() != '':
            return _truthy(str(ev))
    if parser.has_option(section, key):
        try:
            return parser.getboolean(section, key)
        except (ValueError, configparser.NoSectionError, configparser.NoOptionError):
            pass
    return default


def get_int(
    parser: configparser.ConfigParser,
    section: str,
    key: str,
    default: int,
    env_key: Optional[str] = None,
    minimum: Optional[int] = None,
) -> int:
    if env_key:
        ev = _get_env_str(env_key)
        if ev is not None:
            try:
                v = int(ev)
                if minimum is not None:
                    v = max(minimum, v)
                return v
            except ValueError:
                pass
    if parser.has_option(section, key):
        try:
            v = parser.getint(section, key)
            if minimum is not None:
                v = max(minimum, v)
            return v
        except (ValueError, configparser.NoSectionError, configparser.NoOptionError):
            pass
    return default if minimum is None else max(minimum, default)


def resolve_optional_dir(root: Path, raw: str, default_name: str) -> Path:
    raw = (raw or '').strip()
    if not raw:
        return root / default_name
    p = Path(raw)
    if p.is_absolute():
        return p
    return (root / p).resolve()


def parse_readonly_dirs_line(raw: str) -> List[str]:
    raw = (raw or '').strip()
    if not raw:
        return []
    if '\n' in raw:
        parts = [x.strip() for x in raw.splitlines() if x.strip()]
    elif ';' in raw:
        parts = [x.strip() for x in raw.split(';') if x.strip()]
    else:
        parts = [x.strip() for x in raw.split(',') if x.strip()]
    return parts


def find_claude_cli_explicit(cli_path: str) -> Optional[str]:
    """若配置了绝对/相对路径且存在则返回。"""
    p = (cli_path or '').strip()
    if not p:
        return None
    cand = Path(p).expanduser()
    try:
        if cand.is_file():
            return str(cand.resolve())
    except OSError:
        pass
    w = shutil.which(p)
    return w


def find_claude_cli_auto() -> str:
    if sys.platform == 'win32':
        npm_prefix = os.environ.get('APPDATA', '')
        if npm_prefix:
            for ext in ('.cmd',):
                candidate = os.path.join(npm_prefix, 'npm', 'claude' + ext)
                if os.path.isfile(candidate):
                    return candidate
    path = shutil.which('claude')
    if path:
        return path
    return 'claude'


def split_extra_cli_args(raw: str) -> List[str]:
    raw = (raw or '').strip()
    if not raw:
        return []
    try:
        return shlex.split(raw, posix=(sys.platform != 'win32'))
    except ValueError:
        return raw.split()

