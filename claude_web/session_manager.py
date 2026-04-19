"""多用户多会话 CRUD 与持久化；用户根目录为 cache/<规范化IP>/<user_id>/。"""

import json
import logging
import shutil
import threading
import time
import uuid
from pathlib import Path

from .paths import sanitize_ip_for_path

log = logging.getLogger('claude-web')

SESSION_MEMORY_FILENAME = 'memory.md'


def ensure_session_memory_file(session_dir: Path) -> Path:
    session_dir.mkdir(parents=True, exist_ok=True)
    p = session_dir / SESSION_MEMORY_FILENAME
    if not p.exists():
        p.write_text(
            '# 记忆（本对话）\n\n'
            '本文件由 Claude Web 服务为本对话创建。请使用 Read / Edit / Write 工具读写**本文件**以保存或回忆用户偏好；'
            '不要使用 Claude 内置「记忆」功能写入全局配置（易因权限失败）。\n\n',
            encoding='utf-8',
        )
    return p


class SessionManager:
    """管理多用户多会话的 CRUD 和持久化"""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(exist_ok=True)
        self._locks = {}
        self._locks_lock = threading.Lock()

    def _get_user_dir(self, client_ip: str, user_id: str) -> Path:
        sip = sanitize_ip_for_path(client_ip)
        return self.cache_dir / sip / user_id

    def get_user_dir(self, client_ip: str, user_id: str) -> Path:
        """用户根目录 cache/<规范化IP>/<user_id>/（供凭证等扩展使用）。"""
        return self._get_user_dir(client_ip, user_id)

    def _lock_key(self, client_ip: str, user_id: str) -> str:
        return f'{sanitize_ip_for_path(client_ip)}|{user_id}'

    def _get_user_lock(self, client_ip: str, user_id: str) -> threading.Lock:
        key = self._lock_key(client_ip, user_id)
        with self._locks_lock:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    def _read_json(self, path: Path, default=None):
        if not path.exists():
            return default if default is not None else None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return default if default is not None else None

    def _write_json(self, path: Path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def list_sessions(self, client_ip: str, user_id: str) -> list:
        user_dir = self._get_user_dir(client_ip, user_id)
        sessions_file = user_dir / 'sessions.json'
        sessions = self._read_json(sessions_file, [])
        if sessions:
            sessions.sort(key=lambda s: s.get('updated_at', ''), reverse=True)
        return sessions if sessions else []

    def create_session(self, client_ip: str, user_id: str) -> dict:
        user_dir = self._get_user_dir(client_ip, user_id)
        sessions_file = user_dir / 'sessions.json'
        lock = self._get_user_lock(client_ip, user_id)

        with lock:
            sessions = self._read_json(sessions_file, []) or []
            now = time.strftime('%Y-%m-%d %H:%M:%S')
            session = {
                'id': str(uuid.uuid4()),
                'claude_session_id': None,
                'title': '新对话',
                'created_at': now,
                'updated_at': now,
            }
            sessions.append(session)
            self._write_json(sessions_file, sessions)
            msg_dir = user_dir / session['id']
            msg_dir.mkdir(parents=True, exist_ok=True)
            self._write_json(msg_dir / 'messages.json', [])
            (msg_dir / 'uploads').mkdir(exist_ok=True)
            ensure_session_memory_file(msg_dir)

        log.info(f"[Session] 创建会话: user={user_id}, session_id={session['id']}")
        return session

    def get_session(self, client_ip: str, user_id: str, session_id: str):
        sessions = self.list_sessions(client_ip, user_id)
        for s in sessions:
            if s['id'] == session_id:
                return s
        return None

    def update_session(self, client_ip: str, user_id: str, session_id: str, **kwargs):
        user_dir = self._get_user_dir(client_ip, user_id)
        sessions_file = user_dir / 'sessions.json'
        lock = self._get_user_lock(client_ip, user_id)

        with lock:
            sessions = self._read_json(sessions_file, []) or []
            for s in sessions:
                if s['id'] == session_id:
                    for k, v in kwargs.items():
                        s[k] = v
                    s['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
                    break
            self._write_json(sessions_file, sessions)

    def delete_session(self, client_ip: str, user_id: str, session_id: str) -> bool:
        user_dir = self._get_user_dir(client_ip, user_id)
        sessions_file = user_dir / 'sessions.json'
        lock = self._get_user_lock(client_ip, user_id)

        with lock:
            sessions = self._read_json(sessions_file, []) or []
            sessions = [s for s in sessions if s['id'] != session_id]
            self._write_json(sessions_file, sessions)

        session_dir = user_dir / session_id
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)

        log.info(f"[Session] 删除会话: user={user_id}, session_id={session_id}")
        return True

    def get_messages(self, client_ip: str, user_id: str, session_id: str) -> list:
        msg_file = self._get_user_dir(client_ip, user_id) / session_id / 'messages.json'
        result = self._read_json(msg_file, [])
        return result if result else []

    def add_message(self, client_ip: str, user_id: str, session_id: str, role: str, content: str,
                    thinking: str = None, files: list = None):
        msg_file = self._get_user_dir(client_ip, user_id) / session_id / 'messages.json'
        lock = self._get_user_lock(client_ip, user_id)

        with lock:
            messages = self._read_json(msg_file, []) or []
            msg = {
                'role': role,
                'content': content,
                'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            }
            if thinking:
                msg['thinking'] = thinking
            if files:
                msg['files'] = files
            messages.append(msg)
            self._write_json(msg_file, messages)

    def get_session_dir(self, client_ip: str, user_id: str, session_id: str) -> Path:
        return self._get_user_dir(client_ip, user_id) / session_id

    def get_upload_dir(self, client_ip: str, user_id: str, session_id: str) -> Path:
        upload_dir = self._get_user_dir(client_ip, user_id) / session_id / 'uploads'
        upload_dir.mkdir(parents=True, exist_ok=True)
        return upload_dir

    def list_uploads(self, client_ip: str, user_id: str, session_id: str) -> list:
        upload_dir = self.get_upload_dir(client_ip, user_id, session_id)
        files = []
        for f in upload_dir.iterdir():
            if f.is_file():
                files.append({
                    'name': f.name,
                    'size': f.stat().st_size,
                })
        return files
