"""客户端 IP 解析与用于路径的规范化。"""

import ipaddress
import re

from flask import Request


def sanitize_ip_for_path(addr: str) -> str:
    """IPv4 将点换为下划线；IPv6 压缩后将冒号换为下划线。"""
    if not addr:
        return 'unknown'
    s = addr.strip()
    try:
        ip = ipaddress.ip_address(s.split('%')[0])
    except ValueError:
        safe = re.sub(r'[^\w.\-]', '_', s)[:200]
        return safe or 'unknown'
    if ip.version == 4:
        return str(ip).replace('.', '_')
    return ip.compressed.replace(':', '_')


def get_client_ip(request: Request, trust_x_forwarded: bool) -> str:
    """优先 X-Forwarded-For 首跳（需显式信任代理）。"""
    if trust_x_forwarded:
        xff = request.headers.get('X-Forwarded-For') or ''
        if xff:
            first = xff.split(',')[0].strip()
            if first:
                return first
    return request.remote_addr or 'unknown'
