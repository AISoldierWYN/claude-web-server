"""Tavily 联网搜索：服务端调用后把结果注入 Claude prompt。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List


class TavilySearchError(RuntimeError):
    pass


def search_tavily(
    *,
    api_key: str,
    query: str,
    max_results: int = 5,
    search_depth: str = 'basic',
    timeout: int = 20,
) -> Dict[str, Any]:
    key = (api_key or '').strip()
    q = (query or '').strip()
    if not key:
        raise TavilySearchError('Tavily API key 未配置')
    if not q:
        raise TavilySearchError('搜索 query 为空')

    payload = {
        'query': q,
        'search_depth': (search_depth or 'basic').strip() or 'basic',
        'max_results': max(1, min(int(max_results or 5), 10)),
        'include_answer': True,
        'include_raw_content': False,
    }
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        'https://api.tavily.com/search',
        data=body,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {key}',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='replace')[:800]
        raise TavilySearchError(f'Tavily HTTP {e.code}: {detail}') from e
    except urllib.error.URLError as e:
        raise TavilySearchError(f'Tavily 网络错误: {e.reason}') from e
    except OSError as e:
        raise TavilySearchError(f'Tavily 请求失败: {e}') from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise TavilySearchError('Tavily 返回了非 JSON 内容') from e
    if not isinstance(data, dict):
        raise TavilySearchError('Tavily 返回格式异常')
    return data


def format_tavily_for_prompt(data: Dict[str, Any], query: str) -> str:
    answer = (data.get('answer') or '').strip()
    results: List[Dict[str, Any]] = data.get('results') if isinstance(data.get('results'), list) else []
    lines = [
        '【联网搜索资料 — Tavily】',
        f'搜索问题：{query}',
        '请基于这些资料回答用户，并在涉及事实、新闻、数据时尽量附上来源链接；不要编造搜索结果之外的最新信息。',
    ]
    if answer:
        lines.extend(['', 'Tavily 摘要：', answer])
    if results:
        lines.extend(['', '搜索结果：'])
        for i, item in enumerate(results, 1):
            title = str(item.get('title') or '').strip() or f'结果 {i}'
            url = str(item.get('url') or '').strip()
            content = str(item.get('content') or '').strip()
            score = item.get('score')
            score_txt = f'；score={score}' if score is not None else ''
            lines.append(f'{i}. {title}{score_txt}')
            if url:
                lines.append(f'   URL: {url}')
            if content:
                lines.append(f'   摘要: {content[:1200]}')
    else:
        lines.append('搜索结果为空。')
    return '\n'.join(lines) + '\n\n'
