"""根据请求 Host 判断是否为「本机访问」（127 / localhost / ::1），用于 V2 每用户 API 策略。"""

from __future__ import annotations

import ipaddress

from flask import Request


def host_header_hostname(host_value: str) -> str:
    """
    从 Host / X-Forwarded-Host 取值中解析纯主机名（去掉端口）。
    支持 IPv6 方括号形式，如 [::1]:8080。
    """
    host_value = (host_value or '').strip()
    if not host_value:
        return ''
    if host_value.startswith('['):
        end = host_value.find(']')
        if end > 1:
            return host_value[1:end]
        return host_value
    # IPv4 host:port 或域名:port
    if ':' in host_value and not host_value.startswith('['):
        # 仅第一个冒号前为 hostname（IPv6 无括号时此处不处理，依赖上游使用括号形式）
        return host_value.split(':', 1)[0]
    return host_value


def effective_browser_hostname(request: Request, trust_x_forwarded: bool) -> str:
    """
    用于与浏览器地址栏一致的「站点主机名」判断。
    若信任代理且存在 X-Forwarded-Host，优先取其首段（与 XFF 行为一致）。
    """
    if trust_x_forwarded:
        xf = (request.headers.get('X-Forwarded-Host') or '').strip()
        if xf:
            first = xf.split(',')[0].strip()
            return host_header_hostname(first)
    return host_header_hostname(request.host or '')


def is_loopback_hostname(hostname: str) -> bool:
    h = (hostname or '').strip().lower()
    if h in ('localhost', '127.0.0.1', '::1'):
        return True
    try:
        ip = ipaddress.ip_address(h.split('%')[0])
        return bool(getattr(ip, 'is_loopback', False))
    except ValueError:
        return False
