"""环境变量、config.ini 与路径常量。优先级：命令行/环境变量 > config.ini > 默认值。"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import settings_loader as _sl

ROOT = Path(__file__).resolve().parent.parent
CONFIG_INI_PATH = ROOT / 'config.ini'

_parser = _sl.load_configparser(CONFIG_INI_PATH)


def _get_env_str(key: str) -> Optional[str]:
    v = os.environ.get(key)
    if v is None:
        return None
    v = v.strip()
    return v if v else None


def _str(sec: str, key: str, default: str = '', env: Optional[str] = None) -> str:
    return _sl.get_str(_parser, sec, key, default, env_key=env)


def _bool(sec: str, key: str, default: bool, env: Optional[str] = None) -> bool:
    return _sl.get_bool(_parser, sec, key, default, env_key=env)


def _int(sec: str, key: str, default: int, env: Optional[str] = None, minimum: Optional[int] = None) -> int:
    return _sl.get_int(_parser, sec, key, default, env_key=env, minimum=minimum)


# ---------- 服务 ----------
SERVER_HOST = _str('server', 'host', '0.0.0.0', env='CLAUDE_WEB_HOST')
SERVER_PORT = _int('server', 'port', 8080, env='CLAUDE_WEB_PORT', minimum=1)

# ---------- 认证（优先级：命令行参数 > 环境变量 CLAUDE_WEB_TOKEN > config.ini [auth] token）----------
_ini_token = _str('auth', 'token', '', env=None)
if len(sys.argv) > 1:
    TOKEN = sys.argv[1].strip()
else:
    _ev = _get_env_str('CLAUDE_WEB_TOKEN')
    TOKEN = _ev if _ev is not None else _ini_token
ENABLE_AUTH = bool(TOKEN)

# ---------- 代理 ----------
TRUST_X_FORWARDED = _bool('proxy', 'trust_x_forwarded', False, env='CLAUDE_WEB_TRUST_X_FORWARDED')

# ---------- Claude CLI ----------
CLAUDE_CLI_PATH_RAW = _str('claude', 'cli_path', '', env='CLAUDE_WEB_CLI_PATH')
_explicit = _sl.find_claude_cli_explicit(CLAUDE_CLI_PATH_RAW)
CLAUDE_CLI_PATH = _explicit if _explicit else _sl.find_claude_cli_auto()

CLAUDE_MODEL = _str('claude', 'model', '', env='CLAUDE_WEB_MODEL')
CLAUDE_WEB_PERMISSION_MODE = _str(
    'claude', 'permission_mode', 'bypassPermissions', env='CLAUDE_WEB_PERMISSION_MODE'
)
CLAUDE_WEB_DANGEROUSLY_SKIP_PERMISSIONS = _bool(
    'claude', 'dangerously_skip_permissions', False, env='CLAUDE_WEB_DANGEROUSLY_SKIP_PERMISSIONS'
)
CLAUDE_WEB_ISOLATE_HOME = _bool('claude', 'isolate_home', False, env='CLAUDE_WEB_ISOLATE_HOME')
CLAUDE_WEB_FORK_CLAUDE_HOME = _bool('claude', 'fork_claude_home', True, env='CLAUDE_WEB_FORK_CLAUDE_HOME')
CLAUDE_WEB_ORCH_MAX_ROUNDS = _int('claude', 'orch_max_rounds', 20, env='CLAUDE_WEB_ORCH_MAX_ROUNDS', minimum=1)
CLAUDE_EXTRA_CLI_ARGS = _sl.split_extra_cli_args(
    _str('claude', 'extra_args', '', env='CLAUDE_WEB_EXTRA_CLI_ARGS')
)

# ---------- Gemini CLI（可选，默认关闭） ----------
GEMINI_CLI_PATH_RAW = _str('gemini', 'cli_path', '', env='CLAUDE_WEB_GEMINI_CLI_PATH')
_gemini_explicit = _sl.find_cli_explicit(GEMINI_CLI_PATH_RAW)
GEMINI_CLI_PATH = _gemini_explicit if _gemini_explicit else _sl.find_gemini_cli_auto()
GEMINI_MODEL = _str('gemini', 'model', '', env='CLAUDE_WEB_GEMINI_MODEL')
GEMINI_APPROVAL_MODE = _str('gemini', 'approval_mode', 'plan', env='CLAUDE_WEB_GEMINI_APPROVAL_MODE')
GEMINI_SANDBOX = _bool('gemini', 'sandbox', False, env='CLAUDE_WEB_GEMINI_SANDBOX')
GEMINI_SKIP_TRUST = _bool('gemini', 'skip_trust', True, env='CLAUDE_WEB_GEMINI_SKIP_TRUST')
GEMINI_PROXY = _str('gemini', 'proxy', '', env='CLAUDE_WEB_GEMINI_PROXY')
GEMINI_REQUEST_TIMEOUT_SECONDS = _int(
    'gemini',
    'request_timeout_seconds',
    300,
    env='CLAUDE_WEB_GEMINI_REQUEST_TIMEOUT_SECONDS',
    minimum=1,
)

# ---------- 路径（目录） ----------
_paths_json_rel = _str('paths', 'paths_config_file', 'claude_web_paths.config.json', env='CLAUDE_WEB_PATHS_CONFIG_FILE')
PATHS_CONFIG_FILE = (
    Path(_paths_json_rel).resolve()
    if Path(_paths_json_rel).is_absolute()
    else (ROOT / _paths_json_rel).resolve()
)

CACHE_DIR = _sl.resolve_optional_dir(ROOT, _str('paths', 'cache_dir', '', env='CLAUDE_WEB_CACHE_DIR'), 'cache')
LOG_DIR = _sl.resolve_optional_dir(ROOT, _str('paths', 'log_dir', '', env='CLAUDE_WEB_LOG_DIR'), 'logs')
BACKUPS_DIR = _sl.resolve_optional_dir(ROOT, _str('paths', 'backups_dir', '', env='CLAUDE_WEB_BACKUPS_DIR'), 'backups')
FEEDBACK_DIR = _sl.resolve_optional_dir(ROOT, _str('paths', 'feedback_dir', '', env='CLAUDE_WEB_FEEDBACK_DIR'), 'feedback')

# ---------- 上传 ----------
UPLOAD_MAX_SIZE = _int('upload', 'max_size_mb', 100, env='CLAUDE_WEB_UPLOAD_MAX_MB', minimum=1) * 1024 * 1024

# ---------- Tavily 联网搜索 ----------
TAVILY_API_KEY = _str('tavily', 'api_key', '', env='TAVILY_API_KEY')
if not TAVILY_API_KEY:
    # 兼容常见拼写误差：Tavily 容易被写成 Tabil(y)。
    TAVILY_API_KEY = _get_env_str('TABILY_API_KEY') or _str('tavily', 'tabily_api_key', '')
TAVILY_MAX_RESULTS = _int('tavily', 'max_results', 5, env='TAVILY_MAX_RESULTS', minimum=1)
TAVILY_SEARCH_DEPTH = _str('tavily', 'search_depth', 'basic', env='TAVILY_SEARCH_DEPTH')

# ---------- V2：局域网每用户 API（Host 非本机时读用户保存的 env + model）----------
FEATURE_V2_MULTI_USER_API = _bool(
    'features', 'v2_multi_user_api', False, env='CLAUDE_WEB_V2_MULTI_USER_API'
)
# V3：可选标记（Linux 服务器部署说明等）；**不**作为「仅在 Linux 生效」的运行时硬开关，兼容仍靠 sys.platform
FEATURE_V3_LINUX_DEPLOY = _bool(
    'features', 'v3_linux_deploy', False, env='CLAUDE_WEB_V3_LINUX_DEPLOY'
)
FEATURE_MOBILE_REMOTE_DEVELOPMENT = _bool(
    'features',
    'mobile_remote_development',
    False,
    env='CLAUDE_WEB_MOBILE_REMOTE_DEVELOPMENT',
)
FEATURE_GEMINI_SUPPORT = _bool(
    'features',
    'gemini_support',
    False,
    env='CLAUDE_WEB_GEMINI_SUPPORT',
)

# ---------- 开发模式（手机端远程控制 PC 项目） ----------
_dev_projects_rel = _str(
    'development',
    'projects_config_file',
    'claude_web_projects.config.json',
    env='CLAUDE_WEB_PROJECTS_CONFIG_FILE',
)
DEV_PROJECTS_CONFIG_FILE = (
    Path(_dev_projects_rel).resolve()
    if Path(_dev_projects_rel).is_absolute()
    else (ROOT / _dev_projects_rel).resolve()
)
DEV_PERMISSION_MODE = _str(
    'development',
    'permission_mode',
    'acceptEdits',
    env='CLAUDE_WEB_DEV_PERMISSION_MODE',
)
DEV_DANGEROUSLY_SKIP_PERMISSIONS = _bool(
    'development',
    'dangerously_skip_permissions',
    False,
    env='CLAUDE_WEB_DEV_DANGEROUSLY_SKIP_PERMISSIONS',
)
DEV_TEST_TIMEOUT_SECONDS = _int(
    'development',
    'test_timeout_seconds',
    300,
    env='CLAUDE_WEB_DEV_TEST_TIMEOUT_SECONDS',
    minimum=1,
)


def parse_readonly_dirs(log):
    """
    只读目录：环境变量 CLAUDE_WEB_READONLY_DIRS 优先；否则使用 config.ini [readonly] dirs。
    """
    raw = _get_env_str('CLAUDE_WEB_READONLY_DIRS')
    if raw is None:
        raw = _str('readonly', 'dirs', '')
    if not raw.strip():
        return []
    parts = _sl.parse_readonly_dirs_line(raw)
    out = []
    for p in parts:
        try:
            pp = Path(p).expanduser().resolve()
            if pp.is_dir():
                out.append(str(pp))
            else:
                log.warning(f'[Config] 只读目录忽略（非目录）: {p}')
        except Exception as e:
            log.warning(f'[Config] 只读目录无效 {p}: {e}')
    return out


def _resolve_dir_entry(p: str, log) -> Optional[str]:
    try:
        pp = Path(p).expanduser().resolve()
        if pp.is_dir():
            return str(pp)
        log.warning(f'[Config] 忽略（非目录）: {p}')
    except Exception as e:
        log.warning(f'[Config] 路径无效 {p}: {e}')
    return None


def load_paths_config_file(log) -> Tuple[List[str], str, List[Dict[str, Any]]]:
    """
    读取 PATHS_CONFIG_FILE（JSON）：readonly_dirs、bundles。
    """
    path = PATHS_CONFIG_FILE
    if not path.is_file():
        return [], '', []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as e:
        log.warning('[Config] %s 解析失败: %s', path.name, e)
        return [], '', []
    if not isinstance(data, dict):
        log.warning('[Config] %s 根节点须为 JSON 对象', path.name)
        return [], '', []
    notes = ''
    n = data.get('notes')
    if isinstance(n, str):
        notes = n.strip()

    path_acc: List[str] = []

    raw = data.get('readonly_dirs')
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        log.warning('[Config] %s 中 readonly_dirs 须为数组', path.name)
        raw = []
    for item in raw:
        p = ''
        if isinstance(item, str):
            p = item.strip()
        elif isinstance(item, dict):
            p = (item.get('path') or '').strip()
        if not p:
            continue
        r = _resolve_dir_entry(p, log)
        if r:
            path_acc.append(r)

    bundles_out: List[Dict[str, Any]] = []
    bundles_raw = data.get('bundles')
    if isinstance(bundles_raw, list):
        for i, b in enumerate(bundles_raw):
            if not isinstance(b, dict):
                log.warning('[Config] %s bundles[%s] 跳过（非对象）', path.name, i)
                continue
            bid = (b.get('id') or '').strip() or f'bundle-{i + 1}'
            title = (b.get('title') or b.get('name') or bid).strip()
            summary = (b.get('summary') or b.get('description') or '').strip()
            praw = b.get('paths') or b.get('readonly_dirs') or []
            if not isinstance(praw, list):
                log.warning('[Config] %s 包 %s 的 paths 须为数组', path.name, bid)
                praw = []
            resolved_posix: List[str] = []
            for item in praw:
                p = ''
                if isinstance(item, str):
                    p = item.strip()
                elif isinstance(item, dict):
                    p = (item.get('path') or '').strip()
                if not p:
                    continue
                r = _resolve_dir_entry(p, log)
                if r:
                    try:
                        resolved_posix.append(Path(r).resolve().as_posix())
                    except OSError:
                        resolved_posix.append(r)
            bundles_out.append(
                {
                    'id': bid,
                    'title': title,
                    'summary': summary,
                    'keywords': b.get('keywords') if isinstance(b.get('keywords'), list) else [],
                    'always_mount': bool(b.get('always_mount')),
                    'paths': resolved_posix,
                }
            )

    if path_acc:
        log.info('[Config] %s: %s 个全局只读路径', path.name, len(path_acc))
    if bundles_out:
        log.info('[Config] %s: %s 个技能包（路径按需挂载，不默认加入 --add-dir）', path.name, len(bundles_out))
    return path_acc, notes, bundles_out


def merge_readonly_dirs(log) -> Tuple[List[str], str, List[Dict[str, Any]]]:
    env_dirs = parse_readonly_dirs(log)
    json_dirs, notes, bundles = load_paths_config_file(log)
    seen = set()
    merged: List[str] = []
    for p in env_dirs + json_dirs:
        try:
            key = str(Path(p).resolve()).lower()
        except OSError:
            key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(p)
    return merged, notes, bundles


def log_config_summary(log: logging.Logger) -> None:
    """启动时打印关键配置（不含 token 明文）。"""
    log.info('[Config] 配置文件: %s', CONFIG_INI_PATH)
    log.info('[Config] 运行平台: %s（Windows/Linux/macOS 由运行时自动适配）', sys.platform)
    log.info('[Config] 监听 %s:%s', SERVER_HOST, SERVER_PORT)
    log.info('[Config] Claude CLI: %s', CLAUDE_CLI_PATH)
    if CLAUDE_MODEL:
        log.info('[Config] Claude --model: %s', CLAUDE_MODEL)
    if CLAUDE_EXTRA_CLI_ARGS:
        log.info('[Config] Claude 附加参数: %s', CLAUDE_EXTRA_CLI_ARGS)
    log.info('[Config] 会话 fork Claude HOME（共享父配置但隔离全局记忆）: %s', CLAUDE_WEB_FORK_CLAUDE_HOME)
    log.info('[Config] Tavily 联网搜索: %s', bool(TAVILY_API_KEY))
    log.info('[Config] V2 每用户 API（局域网）: %s', FEATURE_V2_MULTI_USER_API)
    log.info('[Config] 移动端远程开发控制: %s', FEATURE_MOBILE_REMOTE_DEVELOPMENT)
    log.info('[Config] Gemini CLI 支持: %s', FEATURE_GEMINI_SUPPORT)
    if FEATURE_GEMINI_SUPPORT:
        log.info('[Config] Gemini CLI: %s', GEMINI_CLI_PATH)
        log.info('[Config] Gemini approval_mode: %s', GEMINI_APPROVAL_MODE)
        if GEMINI_PROXY:
            log.info('[Config] Gemini proxy: %s', GEMINI_PROXY)
    if FEATURE_MOBILE_REMOTE_DEVELOPMENT:
        log.info('[Config] 开发项目白名单: %s', DEV_PROJECTS_CONFIG_FILE)
        log.info('[Config] 开发模式 CLI permission_mode: %s', DEV_PERMISSION_MODE)
    if FEATURE_V3_LINUX_DEPLOY:
        log.info('[Config] V3 Linux 部署标记: 已开启（文档/运维提示；与 sys.platform 自动兼容并存）')
