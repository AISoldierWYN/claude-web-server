"""V2：按用户（cache 目录）在服务端保存 Claude API 环境变量与 model，供局域网访问时使用。"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import config
from .host_scope import effective_browser_hostname, is_loopback_hostname
from .session_manager import SessionManager

log = logging.getLogger('claude-web')

CREDENTIALS_FILENAME = 'claude_api_credentials.json'
_VERSION = 1
_MAX_ENV_KEYS = 128
_MAX_VALUE_LEN = 16384
_KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def sanitize_env(raw: Any) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """校验并规范化 env 字典；值一律转为 str。"""
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return None, 'env 须为 JSON 对象'
    out: Dict[str, str] = {}
    for k, v in raw.items():
        ks = str(k).strip()
        if not _KEY_RE.match(ks):
            return None, f'非法环境变量名: {k!r}'
        if len(out) >= _MAX_ENV_KEYS:
            return None, f'环境变量数量超过上限 ({_MAX_ENV_KEYS})'
        if v is None:
            continue
        vs = str(v)
        if len(vs) > _MAX_VALUE_LEN:
            return None, f'环境变量 {ks} 值过长'
        out[ks] = vs
    return out, None


def merge_env_preserve_existing(existing: Optional[dict], env_new: Dict[str, str]) -> Dict[str, str]:
    """
    若新 env 未提供有效 token，则沿用旧文件中的 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY，
    便于前端「留空表示不修改密钥」。
    """
    if has_auth_token(env_new):
        return dict(env_new)
    merged = dict(env_new)
    if existing and isinstance(existing.get('env'), dict):
        old = existing['env']
        for k in ('ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY'):
            ov = old.get(k)
            if ov and str(ov).strip():
                merged[k] = str(ov).strip()
                break
    return merged


def credentials_path(sm: SessionManager, client_ip: str, user_id: str) -> Path:
    return sm.get_user_dir(client_ip, user_id) / CREDENTIALS_FILENAME


def has_auth_token(env: Dict[str, str]) -> bool:
    t = (env.get('ANTHROPIC_AUTH_TOKEN') or env.get('ANTHROPIC_API_KEY') or '').strip()
    return bool(t)


def validate_save_payload(env: Dict[str, str], _model: str) -> Optional[str]:
    if not has_auth_token(env):
        return '请至少设置 ANTHROPIC_AUTH_TOKEN（或 ANTHROPIC_API_KEY）'
    return None


def load_credentials(sm: SessionManager, client_ip: str, user_id: str) -> Optional[dict]:
    p = credentials_path(sm, client_ip, user_id)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as e:
        log.warning('[V2] 读取凭证失败 %s: %s', p, e)
        return None
    if not isinstance(data, dict):
        return None
    return data


def save_credentials(sm: SessionManager, client_ip: str, user_id: str, env: Dict[str, str], model: str) -> None:
    ud = sm.get_user_dir(client_ip, user_id)
    ud.mkdir(parents=True, exist_ok=True)
    p = ud / CREDENTIALS_FILENAME
    payload = {'version': _VERSION, 'env': env, 'model': (model or '').strip()}
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    log.info('[V2] 已保存用户 API 凭证（路径存在，内容不记录）: %s', p)


def delete_credentials(sm: SessionManager, client_ip: str, user_id: str) -> bool:
    p = credentials_path(sm, client_ip, user_id)
    try:
        if p.is_file():
            p.unlink()
            log.info('[V2] 已删除用户 API 凭证: %s', p)
            return True
    except OSError as e:
        log.warning('[V2] 删除凭证失败: %s', e)
    return False


def _env_preview_for_ui(env: Dict[str, str]) -> Dict[str, str]:
    """供前端编辑：去掉疑似密钥的键，避免在 GET 中泄露。"""
    out: Dict[str, str] = {}
    for k, v in env.items():
        ku = k.upper()
        if any(x in ku for x in ('TOKEN', 'SECRET', 'PASSWORD', 'API_KEY')):
            continue
        if ku == 'ANTHROPIC_API_KEY':
            continue
        out[k] = v
    return out


def public_status(data: Optional[dict]) -> dict:
    """GET 接口安全摘要（不含密钥明文）。"""
    if not data or not isinstance(data, dict):
        return {
            'configured': False,
            'has_token': False,
            'model': '',
            'base_url': '',
            'env_preview': {},
        }
    env = data.get('env') if isinstance(data.get('env'), dict) else {}
    env = env or {}
    return {
        'configured': True,
        'has_token': has_auth_token(env),
        'model': (data.get('model') or '').strip() if isinstance(data.get('model'), str) else '',
        'base_url': (env.get('ANTHROPIC_BASE_URL') or '').strip(),
        'env_key_count': len([k for k, v in env.items() if str(v).strip()]),
        'env_preview': _env_preview_for_ui(env),
    }


def resolve_claude_runtime_for_request(
    request: Request,
    sm: SessionManager,
    client_ip: str,
    user_id: str,
) -> dict:
    """
    返回供 claude_runner 使用的字段：
    - child_env_extra: 合并进子进程 env；None 表示不合并（沿用进程环境 + config.ini 行为）
    - model_override: 传入 stream_claude_output；None 表示不按用户覆盖 model
    - error: 非空时应对 /chat 返回 400
    - use_per_user: 是否处于「局域网 + 用户凭证」模式（用于日志，不含敏感信息）
    """
    if not config.FEATURE_V2_MULTI_USER_API:
        return {
            'child_env_extra': None,
            'model_override': None,
            'error': None,
            'use_per_user': False,
        }

    host = effective_browser_hostname(request, config.TRUST_X_FORWARDED)
    if is_loopback_hostname(host):
        return {
            'child_env_extra': None,
            'model_override': None,
            'error': None,
            'use_per_user': False,
        }

    data = load_credentials(sm, client_ip, user_id)
    env: Dict[str, str] = {}
    if data and isinstance(data.get('env'), dict):
        se, err = sanitize_env(data.get('env'))
        if err:
            return {'child_env_extra': None, 'model_override': None, 'error': err, 'use_per_user': True}
        env = se or {}
    if not has_auth_token(env):
        return {
            'child_env_extra': None,
            'model_override': None,
            'error': (
                '当前通过局域网地址访问且已开启 V2：请先在侧栏「API 配置」中保存 '
                'ANTHROPIC_AUTH_TOKEN 与（如需）ANTHROPIC_BASE_URL、model 等。'
            ),
            'use_per_user': True,
        }

    model_raw = ''
    if data and isinstance(data.get('model'), str):
        model_raw = data.get('model', '').strip()

    return {
        'child_env_extra': env,
        'model_override': model_raw if model_raw else None,
        'error': None,
        'use_per_user': True,
    }
