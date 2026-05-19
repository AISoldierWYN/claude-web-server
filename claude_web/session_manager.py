"""多用户多会话 CRUD 与持久化；用户根目录为 cache/<规范化IP>/<user_id>/。"""

import json
import logging
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from .paths import sanitize_ip_for_path

log = logging.getLogger('claude-web')

SESSION_MEMORY_FILENAME = 'memory.md'
USER_GLOBAL_MEMORY_FILENAME = 'AGENT.md'
DEFAULT_SESSION_TITLE = '新对话'
AUTO_TITLE_MAX_CHARS = 24
DEFAULT_PROVIDER = 'claude'
SUPPORTED_PROVIDERS = {'claude', 'gemini'}


def normalize_provider(provider: str) -> str:
    p = (provider or DEFAULT_PROVIDER).strip().lower()
    return p if p in SUPPORTED_PROVIDERS else DEFAULT_PROVIDER


def normalize_session_record(session: dict) -> dict:
    if not isinstance(session, dict):
        return session
    provider = normalize_provider(session.get('provider') or DEFAULT_PROVIDER)
    session['provider'] = provider
    ids = session.get('provider_session_ids')
    if not isinstance(ids, dict):
        ids = {}
    claude_sid = session.get('claude_session_id')
    if claude_sid and not ids.get('claude'):
        ids['claude'] = claude_sid
    session['provider_session_ids'] = ids
    if 'model' not in session or session.get('model') is None:
        session['model'] = ''
    session['model_handoff_pending'] = bool(session.get('model_handoff_pending'))
    session['starred'] = bool(session.get('starred'))
    session['starred_at'] = str(session.get('starred_at') or '')
    return session


def _is_default_session_title(title: str) -> bool:
    return not str(title or '').strip() or str(title or '').strip() == DEFAULT_SESSION_TITLE


def derive_session_title_from_message(content: str, max_chars: int = AUTO_TITLE_MAX_CHARS) -> str:
    """从首条用户消息里提取一个稳定的会话标题，避免额外调用模型产生费用和延迟。"""
    text = str(content or '').replace('\r\n', '\n').replace('\r', '\n')
    if not text.strip():
        return DEFAULT_SESSION_TITLE

    lines = []
    in_fence = False
    saw_code = False
    for raw in text.split('\n'):
        line = raw.strip()
        if line.startswith('```') or line.startswith('~~~'):
            in_fence = not in_fence
            saw_code = True
            continue
        if in_fence:
            continue
        if not line:
            continue
        if line in {'Android 问题分析', 'Android分析'}:
            continue
        if line.startswith(('文件：', '文件:', '附件：', '附件:')):
            continue
        if re.fullmatch(r'[\s\-\|\+:=]{3,}', line):
            continue

        line = re.sub(r'!\[[^\]]*\]\([^)]+\)', '', line)
        line = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', line)
        line = re.sub(r'<[^>]+>', '', line)
        line = re.sub(r'^[#>\-\*\+\d\.\)\s]+', '', line)
        line = re.sub(r'[`*_~]+', '', line).strip()
        if line:
            lines.append(line)

    candidate = ' '.join(lines).strip()
    if not candidate and saw_code:
        candidate = '代码片段讨论'
    if not candidate:
        return DEFAULT_SESSION_TITLE

    candidate = re.sub(r'\s+', ' ', candidate)
    # 去掉常见开场白，让标题更像“问题摘要”，而不是“帮我/请问”。
    candidate = re.sub(
        r'^(你好|您好|hi|hello)[，,。！!\s]*(请问|请|帮我|帮忙|麻烦|能不能|可以)?[，,：:\s]*',
        '',
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(
        r'^(请帮我|帮我看一下|帮我查一下|帮忙看一下|帮忙查一下|看一下|查一下|请问|请|帮我|帮忙|给我|麻烦|能不能|能否|可以)[，,：:\s]*',
        '',
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(r'^(这个|一下|下)\s*', '', candidate)
    candidate = candidate.strip(' \t\r\n,，。！？!?;；:：')
    if not candidate:
        return DEFAULT_SESSION_TITLE

    # 先取第一句，避免把整段 Markdown 或日志说明塞进侧栏。
    sentence_parts = [p.strip() for p in re.split(r'[。！？!?\n]+', candidate) if p.strip()]
    if sentence_parts:
        candidate = sentence_parts[0]
    candidate = candidate.strip(' \t\r\n,，。！？!?;；:：')

    if len(candidate) > max_chars:
        candidate = candidate[:max_chars].rstrip(' \t\r\n,，。！？!?;；:：') + '...'
    return candidate or DEFAULT_SESSION_TITLE


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


def ensure_user_global_memory_file(user_dir: Path) -> Path:
    """确保同一 IP + user_id 下共享的用户级记忆文件存在。"""
    user_dir.mkdir(parents=True, exist_ok=True)
    p = user_dir / USER_GLOBAL_MEMORY_FILENAME
    if not p.exists():
        p.write_text(
            '# 用户全局记忆（同一用户共享）\n\n'
            '本文件由 Claude Web 服务为同一 IP + user_id 创建，位于用户根目录。'
            '它会在每个会话开始前同步到当前会话工作目录下的 AGENT.md，'
            '用于保存跨对话共享的用户偏好、称呼、长期项目习惯等。\n\n'
            '使用约定：\n'
            '- 需要跨所有对话记住的信息写入本文件。\n'
            '- 只属于当前对话的信息写入 memory.md。\n'
            '- 不要写入 Claude/Codex 全局 HOME 下的记忆或配置文件。\n',
            encoding='utf-8',
        )
    return p


def ensure_session_global_memory_file(session_dir: Path) -> Path:
    """确保当前会话目录内有一份用户级 AGENT.md 副本供 CLI 读写。"""
    session_dir.mkdir(parents=True, exist_ok=True)
    user_dir = session_dir.parent
    global_path = ensure_user_global_memory_file(user_dir)
    local_path = session_dir / USER_GLOBAL_MEMORY_FILENAME
    if not local_path.exists():
        try:
            shutil.copy2(global_path, local_path)
        except OSError:
            local_path.write_text(global_path.read_text(encoding='utf-8', errors='replace'), encoding='utf-8')
    return local_path


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

    def _apply_auto_title_locked(
        self,
        sessions_file: Path,
        sessions: list,
        session_id: str,
        content: str,
        *,
        touch_updated_at: bool,
    ) -> bool:
        title = derive_session_title_from_message(content)
        if title == DEFAULT_SESSION_TITLE:
            return False
        for s in sessions:
            if isinstance(s, dict) and s.get('id') == session_id and _is_default_session_title(s.get('title')):
                s['title'] = title
                if touch_updated_at:
                    s['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
                self._write_json(sessions_file, sessions)
                log.info('[Session] 自动生成会话标题: session_id=%s, title=%s', session_id, title)
                return True
        return False

    def _first_user_message_content(self, user_dir: Path, session_id: str) -> str:
        messages = self._read_json(user_dir / session_id / 'messages.json', []) or []
        fallback = ''
        for msg in messages:
            if isinstance(msg, dict) and msg.get('role') == 'user':
                content = str(msg.get('content') or '')
                if not fallback:
                    fallback = content
                if derive_session_title_from_message(content) != DEFAULT_SESSION_TITLE:
                    return content
        return fallback

    def list_sessions(self, client_ip: str, user_id: str) -> list:
        user_dir = self._get_user_dir(client_ip, user_id)
        sessions_file = user_dir / 'sessions.json'
        lock = self._get_user_lock(client_ip, user_id)
        with lock:
            raw_sessions = self._read_json(sessions_file, []) or []
            sessions = [normalize_session_record(s) for s in raw_sessions if isinstance(s, dict)]
            changed = len(sessions) != len(raw_sessions)
            for session in sessions:
                if _is_default_session_title(session.get('title')):
                    first_message = self._first_user_message_content(user_dir, session.get('id', ''))
                    if first_message:
                        title = derive_session_title_from_message(first_message)
                        if title != DEFAULT_SESSION_TITLE:
                            session['title'] = title
                            changed = True
            if changed:
                self._write_json(sessions_file, sessions)
        if sessions:
            sessions.sort(key=lambda s: s.get('updated_at', ''), reverse=True)
        return sessions if sessions else []

    def create_session(self, client_ip: str, user_id: str, provider: str = DEFAULT_PROVIDER, model: str = '') -> dict:
        user_dir = self._get_user_dir(client_ip, user_id)
        sessions_file = user_dir / 'sessions.json'
        lock = self._get_user_lock(client_ip, user_id)
        provider = normalize_provider(provider)

        with lock:
            sessions = self._read_json(sessions_file, []) or []
            now = time.strftime('%Y-%m-%d %H:%M:%S')
            session = {
                'id': str(uuid.uuid4()),
                'provider': provider,
                'model': str(model or '').strip() if provider == DEFAULT_PROVIDER else '',
                'model_handoff_pending': False,
                'starred': False,
                'starred_at': '',
                'claude_session_id': None,
                'provider_session_ids': {},
                'title': DEFAULT_SESSION_TITLE,
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
            ensure_user_global_memory_file(user_dir)
            ensure_session_global_memory_file(msg_dir)

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
                    normalize_session_record(s)
                    s['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
                    break
            self._write_json(sessions_file, sessions)

    def update_provider_session_id(
        self, client_ip: str, user_id: str, session_id: str, provider: str, provider_session_id: str
    ):
        provider = normalize_provider(provider)
        if not provider_session_id:
            return
        if provider == DEFAULT_PROVIDER and provider_session_id == session_id:
            log.warning('[Session] ignore web session id as Claude resume id: session_id=%s', session_id)
            return
        user_dir = self._get_user_dir(client_ip, user_id)
        sessions_file = user_dir / 'sessions.json'
        lock = self._get_user_lock(client_ip, user_id)

        with lock:
            sessions = self._read_json(sessions_file, []) or []
            for s in sessions:
                if s['id'] == session_id:
                    normalize_session_record(s)
                    s['provider_session_ids'][provider] = provider_session_id
                    if provider == DEFAULT_PROVIDER:
                        s['claude_session_id'] = provider_session_id
                        s['model_handoff_pending'] = False
                    s['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
                    break
            self._write_json(sessions_file, sessions)

    def clear_provider_session_id(
        self,
        client_ip: str,
        user_id: str,
        session_id: str,
        provider: str,
        *,
        model_handoff_pending: bool = False,
    ):
        provider = normalize_provider(provider)
        user_dir = self._get_user_dir(client_ip, user_id)
        sessions_file = user_dir / 'sessions.json'
        lock = self._get_user_lock(client_ip, user_id)

        with lock:
            sessions = self._read_json(sessions_file, []) or []
            for s in sessions:
                if s['id'] == session_id:
                    normalize_session_record(s)
                    s['provider_session_ids'].pop(provider, None)
                    if provider == DEFAULT_PROVIDER:
                        s['claude_session_id'] = None
                        s['model_handoff_pending'] = bool(model_handoff_pending)
                    s['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
                    break
            self._write_json(sessions_file, sessions)

    def get_provider_session_id(self, client_ip: str, user_id: str, session_id: str, provider: str):
        session = self.get_session(client_ip, user_id, session_id)
        if not session:
            return None
        provider = normalize_provider(provider)
        ids = session.get('provider_session_ids') or {}
        if provider == DEFAULT_PROVIDER:
            sid = ids.get(provider) or session.get('claude_session_id')
            if sid == session_id:
                return None
            return sid
        return ids.get(provider)

    def set_session_starred(self, client_ip: str, user_id: str, session_id: str, starred: bool):
        user_dir = self._get_user_dir(client_ip, user_id)
        sessions_file = user_dir / 'sessions.json'
        lock = self._get_user_lock(client_ip, user_id)
        updated = None

        with lock:
            sessions = self._read_json(sessions_file, []) or []
            for s in sessions:
                if s['id'] == session_id:
                    normalize_session_record(s)
                    s['starred'] = bool(starred)
                    s['starred_at'] = time.strftime('%Y-%m-%d %H:%M:%S') if starred else ''
                    updated = s
                    break
            self._write_json(sessions_file, sessions)
        return updated

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
                    thinking: str = None, files: list = None, metadata: dict = None):
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
            if isinstance(metadata, dict) and metadata:
                msg['metadata'] = metadata
            messages.append(msg)
            self._write_json(msg_file, messages)
            if role == 'user':
                user_dir = self._get_user_dir(client_ip, user_id)
                sessions_file = user_dir / 'sessions.json'
                sessions = self._read_json(sessions_file, []) or []
                self._apply_auto_title_locked(
                    sessions_file,
                    sessions,
                    session_id,
                    content,
                    touch_updated_at=True,
                )

    def remove_last_assistant_message(
        self,
        client_ip: str,
        user_id: str,
        session_id: str,
        *,
        interrupted_only: bool = True,
    ) -> dict | None:
        """删除最后一条助手消息；默认只删除被标记为 interrupted 的未完成回复。"""
        msg_file = self._get_user_dir(client_ip, user_id) / session_id / 'messages.json'
        lock = self._get_user_lock(client_ip, user_id)
        with lock:
            messages = self._read_json(msg_file, []) or []
            for idx in range(len(messages) - 1, -1, -1):
                msg = messages[idx]
                if not isinstance(msg, dict) or msg.get('role') not in {'assistant', 'ai'}:
                    continue
                meta = msg.get('metadata') if isinstance(msg.get('metadata'), dict) else {}
                if interrupted_only and not meta.get('interrupted'):
                    return None
                removed = messages.pop(idx)
                self._write_json(msg_file, messages)
                return removed
        return None

    def get_session_dir(self, client_ip: str, user_id: str, session_id: str) -> Path:
        return self._get_user_dir(client_ip, user_id) / session_id

    def sync_user_global_memory_to_session(self, client_ip: str, user_id: str, session_id: str) -> Path:
        """把用户级 AGENT.md 同步到当前会话目录，供本轮 CLI 默认加载。"""
        user_dir = self._get_user_dir(client_ip, user_id)
        session_dir = user_dir / session_id
        lock = self._get_user_lock(client_ip, user_id)
        with lock:
            global_path = ensure_user_global_memory_file(user_dir)
            session_dir.mkdir(parents=True, exist_ok=True)
            local_path = session_dir / USER_GLOBAL_MEMORY_FILENAME
            try:
                if local_path.exists():
                    if local_path.stat().st_mtime > global_path.stat().st_mtime:
                        # 若上次异常退出导致会话副本更新但未回写，先保留较新的内容。
                        shutil.copy2(local_path, global_path)
                    else:
                        shutil.copy2(global_path, local_path)
                else:
                    shutil.copy2(global_path, local_path)
            except OSError as exc:
                log.warning(
                    '[Memory] sync user AGENT.md to session failed: user=%s session=%s error=%s',
                    user_id,
                    session_id,
                    exc,
                )
            return local_path

    def sync_session_global_memory_to_user(self, client_ip: str, user_id: str, session_id: str) -> Path:
        """把当前会话内被 CLI 修改过的 AGENT.md 回写到用户根目录。"""
        user_dir = self._get_user_dir(client_ip, user_id)
        session_dir = user_dir / session_id
        lock = self._get_user_lock(client_ip, user_id)
        with lock:
            global_path = ensure_user_global_memory_file(user_dir)
            local_path = session_dir / USER_GLOBAL_MEMORY_FILENAME
            if local_path.exists():
                try:
                    shutil.copy2(local_path, global_path)
                except OSError as exc:
                    log.warning(
                        '[Memory] sync session AGENT.md to user failed: user=%s session=%s error=%s',
                        user_id,
                        session_id,
                        exc,
                    )
            return global_path

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
