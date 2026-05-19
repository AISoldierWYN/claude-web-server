"""环境变量、config.ini 与路径常量。优先级：命令行/环境变量 > config.ini > 默认值。"""

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import settings_loader as _sl
from .skill_bundle_index import (
    as_posix,
    configured_path,
    discover_claude_md,
    discover_skills,
    normalize_resource_items,
    resolve_existing_path,
)

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


def _float(sec: str, key: str, default: float, env: Optional[str] = None, minimum: Optional[float] = None, maximum: Optional[float] = None) -> float:
    raw = _get_env_str(env) if env else None
    if raw is None:
        raw = _str(sec, key, str(default), env=None)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float(default)
    if minimum is not None:
        value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value


_ENV_KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _env_section(*sections: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for section in sections:
        if not _parser.has_section(section):
            continue
        for raw_key, raw_value in _parser.items(section):
            key = str(raw_key or '').strip().upper()
            value = str(raw_value or '').strip()
            if not key or not value or not _ENV_KEY_RE.match(key):
                continue
            out[key] = value
    return out


def _list(sec: str, key: str, default: Optional[List[str]] = None, env: Optional[str] = None) -> List[str]:
    raw = _get_env_str(env) if env else None
    if raw is None:
        raw = _str(sec, key, '', env=None)
    values: List[str] = []
    for item in re.split(r'[,;\r\n]+', raw or ''):
        item = item.strip()
        if item and item not in values:
            values.append(item)
    if values:
        return values
    return list(default or [])


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
CLAUDE_MODEL_OPTIONS = _list(
    'claude',
    'model_options',
    ['glm-5', 'tc-code-latest', 'hunyuan-2.0-thinking'],
    env='CLAUDE_WEB_MODEL_OPTIONS',
)
if CLAUDE_MODEL and CLAUDE_MODEL not in CLAUDE_MODEL_OPTIONS:
    CLAUDE_MODEL_OPTIONS.insert(0, CLAUDE_MODEL)
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
CLAUDE_CHILD_ENV = _env_section('claude_env', 'claude.env')

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
ANDROID_ANALYSIS_KNOWLEDGE_DIR = _sl.resolve_optional_dir(
    ROOT,
    _str('android_analysis', 'knowledge_dir', '', env='CLAUDE_WEB_ANDROID_ANALYSIS_KNOWLEDGE_DIR'),
    'android_analysis_knowledge',
)
ANDROID_ANALYSIS_7Z_PATH = _str(
    'android_analysis',
    'seven_zip_path',
    '',
    env='CLAUDE_WEB_ANDROID_ANALYSIS_7Z_PATH',
)
ANDROID_ANALYSIS_PLANNER_TIMEOUT_SECONDS = _int(
    'android_analysis',
    'planner_timeout_seconds',
    45,
    env='CLAUDE_WEB_ANDROID_ANALYSIS_PLANNER_TIMEOUT_SECONDS',
    minimum=1,
)
ANDROID_ANALYSIS_PLANNER_PROMPT_BUDGET_CHARS = _int(
    'android_analysis',
    'planner_prompt_budget_chars',
    80000,
    env='CLAUDE_WEB_ANDROID_ANALYSIS_PLANNER_PROMPT_BUDGET_CHARS',
    minimum=8000,
)
ANDROID_ANALYSIS_PLANNER_MAX_TREE_NODES = _int(
    'android_analysis',
    'planner_max_tree_nodes',
    300,
    env='CLAUDE_WEB_ANDROID_ANALYSIS_PLANNER_MAX_TREE_NODES',
    minimum=20,
)
ANDROID_ANALYSIS_PLANNER_MAX_SAMPLE_FILES = _int(
    'android_analysis',
    'planner_max_sample_files',
    24,
    env='CLAUDE_WEB_ANDROID_ANALYSIS_PLANNER_MAX_SAMPLE_FILES',
    minimum=1,
)
ANDROID_ANALYSIS_PLANNER_MAX_SAMPLE_CHARS = _int(
    'android_analysis',
    'planner_max_sample_chars',
    50000,
    env='CLAUDE_WEB_ANDROID_ANALYSIS_PLANNER_MAX_SAMPLE_CHARS',
    minimum=1000,
)
ANDROID_ANALYSIS_DEBUG_TRACE = _bool(
    'android_analysis',
    'debug_trace',
    True,
    env='CLAUDE_WEB_ANDROID_ANALYSIS_DEBUG_TRACE',
)
ANDROID_ANALYSIS_AUTO_DEEP_CONFIDENCE_THRESHOLD = _float(
    'android_analysis',
    'auto_deep_confidence_threshold',
    0.72,
    env='CLAUDE_WEB_ANDROID_ANALYSIS_AUTO_DEEP_CONFIDENCE_THRESHOLD',
    minimum=0.0,
    maximum=1.0,
)
ANDROID_ANALYSIS_PROJECT_KNOWLEDGE_RELATIVE_PATH = _str(
    'android_analysis',
    'project_knowledge_relative_path',
    '.claude-web/android-analysis',
    env='CLAUDE_WEB_ANDROID_ANALYSIS_PROJECT_KNOWLEDGE_RELATIVE_PATH',
)

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
FEATURE_ANDROID_ISSUE_ANALYSIS = _bool(
    'features',
    'android_issue_analysis',
    False,
    env='CLAUDE_WEB_ANDROID_ISSUE_ANALYSIS',
)
FEATURE_ANDROID_ISSUE_ANALYSIS_EXPERT_WORKBENCH = _bool(
    'features',
    'android_issue_analysis_expert_workbench',
    False,
    env='CLAUDE_WEB_ANDROID_ISSUE_ANALYSIS_EXPERT_WORKBENCH',
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
    pp = resolve_existing_path(p, log, require_dir=True)
    return str(pp) if pp else None


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
            resources = normalize_resource_items(b.get('resources') or [], log)
            praw = b.get('paths') or b.get('readonly_dirs') or []
            if not isinstance(praw, list):
                log.warning('[Config] %s 包 %s 的 paths 须为数组', path.name, bid)
                praw = []
            resolved_posix: List[str] = []
            search_roots: List[Path] = []
            legacy_resources: List[Dict[str, Any]] = []
            for item in praw:
                p = configured_path(item)
                if not p:
                    continue
                r = resolve_existing_path(p, log, require_dir=True)
                if not r:
                    continue
                rp = as_posix(r)
                resolved_posix.append(rp)
                search_roots.append(r)
                legacy_resources.append(
                    {
                        'id': str(item.get('id') or '').strip() if isinstance(item, dict) else '',
                        'kind': str(item.get('kind') or 'generic').strip() if isinstance(item, dict) else 'generic',
                        'summary': str(item.get('summary') or item.get('description') or '').strip()
                        if isinstance(item, dict)
                        else '',
                        'keywords': item.get('keywords') if isinstance(item, dict) and isinstance(item.get('keywords'), list) else [],
                        'path': rp,
                        'is_dir': True,
                    }
                )
            for res in resources:
                res_path = res.get('path') or ''
                if res.get('is_dir') and res_path:
                    resolved_posix.append(res_path)
                    search_roots.append(Path(res_path))
            seen_paths = set()
            resolved_posix = [
                p for p in resolved_posix
                if not (p.lower() in seen_paths or seen_paths.add(p.lower()))
            ]
            all_resources = legacy_resources + resources
            skills = discover_skills(b.get('skills') or [], search_roots, log, bid)
            claude_md_paths = discover_claude_md(search_roots)
            rule_packs = [
                str(x).strip()
                for x in (b.get('rule_packs') or [])
                if str(x).strip()
            ] if isinstance(b.get('rule_packs'), list) else []
            bundles_out.append(
                {
                    'id': bid,
                    'title': title,
                    'summary': summary,
                    'keywords': b.get('keywords') if isinstance(b.get('keywords'), list) else [],
                    'always_mount': bool(b.get('always_mount')),
                    'paths': resolved_posix,
                    'resources': all_resources,
                    'skills': skills,
                    'claude_md_paths': claude_md_paths,
                    'rule_packs': rule_packs,
                }
            )

    if path_acc:
        log.info('[Config] %s: %s 个全局只读路径', path.name, len(path_acc))
    if bundles_out:
        skill_count = sum(len(b.get('skills') or []) for b in bundles_out)
        resource_count = sum(len(b.get('resources') or []) for b in bundles_out)
        claude_md_count = sum(len(b.get('claude_md_paths') or []) for b in bundles_out)
        log.info(
            '[Config] %s: %s 个技能包，%s 个 skill，%s 个 resource，%s 个 CLAUDE.md（均按需挂载/注入）',
            path.name,
            len(bundles_out),
            skill_count,
            resource_count,
            claude_md_count,
        )
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
    if CLAUDE_MODEL_OPTIONS:
        log.info('[Config] Claude 可选模型: %s', CLAUDE_MODEL_OPTIONS)
    if CLAUDE_CHILD_ENV:
        log.info('[Config] Claude 子进程 env 覆盖: %s 个键（不打印值）: %s', len(CLAUDE_CHILD_ENV), sorted(CLAUDE_CHILD_ENV.keys()))
    if CLAUDE_EXTRA_CLI_ARGS:
        log.info('[Config] Claude 附加参数: %s', CLAUDE_EXTRA_CLI_ARGS)
    log.info('[Config] 会话 fork Claude HOME（共享父配置但隔离全局记忆）: %s', CLAUDE_WEB_FORK_CLAUDE_HOME)
    log.info('[Config] Tavily 联网搜索: %s', bool(TAVILY_API_KEY))
    log.info('[Config] V2 每用户 API（局域网）: %s', FEATURE_V2_MULTI_USER_API)
    log.info('[Config] 移动端远程开发控制: %s', FEATURE_MOBILE_REMOTE_DEVELOPMENT)
    log.info('[Config] Gemini CLI 支持: %s', FEATURE_GEMINI_SUPPORT)
    log.info('[Config] Android 问题分析: %s', FEATURE_ANDROID_ISSUE_ANALYSIS)
    if FEATURE_ANDROID_ISSUE_ANALYSIS:
        log.info('[Config] Android 问题分析知识目录: %s', ANDROID_ANALYSIS_KNOWLEDGE_DIR)
        log.info('[Config] Android 专家工作台: %s', FEATURE_ANDROID_ISSUE_ANALYSIS_EXPERT_WORKBENCH)
        log.info('[Config] Android 项目知识包相对路径: %s', ANDROID_ANALYSIS_PROJECT_KNOWLEDGE_RELATIVE_PATH)
        if ANDROID_ANALYSIS_7Z_PATH:
            log.info('[Config] Android RAR 7-Zip 路径: %s', ANDROID_ANALYSIS_7Z_PATH)
        log.info('[Config] Android Planner timeout: %ss', ANDROID_ANALYSIS_PLANNER_TIMEOUT_SECONDS)
        log.info('[Config] Android Planner prompt budget: %s chars', ANDROID_ANALYSIS_PLANNER_PROMPT_BUDGET_CHARS)
        log.info('[Config] Android Planner tree/sample caps: tree_nodes=%s, sample_files=%s, sample_chars=%s', ANDROID_ANALYSIS_PLANNER_MAX_TREE_NODES, ANDROID_ANALYSIS_PLANNER_MAX_SAMPLE_FILES, ANDROID_ANALYSIS_PLANNER_MAX_SAMPLE_CHARS)
        log.info('[Config] Android debug trace: %s', ANDROID_ANALYSIS_DEBUG_TRACE)
        log.info('[Config] Android auto Deep confidence threshold: %.2f', ANDROID_ANALYSIS_AUTO_DEEP_CONFIDENCE_THRESHOLD)
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
