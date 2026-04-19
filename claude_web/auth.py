"""可选 Token 鉴权。"""

from functools import wraps

from flask import jsonify, request

from . import config


def optional_token(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not config.ENABLE_AUTH:
            return f(*args, **kwargs)
        auth = request.headers.get('Authorization', '')
        if auth != f'Bearer {config.TOKEN}':
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated
