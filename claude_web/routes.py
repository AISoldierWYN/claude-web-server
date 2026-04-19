"""HTTP 路由。"""

import json
import logging
import threading
from pathlib import Path

from flask import Response, jsonify, request, send_from_directory, stream_with_context
from . import config
from .auth import optional_token
from .backup_service import backup_session_before_delete
from . import orchestrator
from .claude_runner import CLAUDE_CLI_PATH, resolve_session_upload_paths, stream_claude_output
from .feedback_service import save_feedback_package
from .filename_sanitize import ascii_storage_filename, is_ascii_filename, safe_client_filename
from .paths import get_client_ip
from .session_manager import SessionManager

log = logging.getLogger('claude-web')

READONLY_DIRS_KEY = 'READONLY_DIRS'
PATHS_NOTES_KEY = 'CLAUDE_WEB_PATHS_NOTES'
PATHS_BUNDLES_KEY = 'CLAUDE_WEB_PATHS_BUNDLES'


def register_routes(app, sm: SessionManager):
    static_root = config.ROOT / 'static'

    @app.route('/')
    def index():
        resp = send_from_directory(static_root, 'index.html')
        resp.headers['Cache-Control'] = 'no-store, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        return resp

    def _readonly():
        return app.config.get(READONLY_DIRS_KEY, [])

    def _readonly_notes():
        return (app.config.get(PATHS_NOTES_KEY) or '').strip()

    def _readonly_bundles():
        return app.config.get(PATHS_BUNDLES_KEY) or []

    @app.route('/chat', methods=['POST'])
    @optional_token
    def chat():
        data = request.json or {}
        message = data.get('message', '').strip()
        user_id = data.get('user_id', '').strip()
        session_id = data.get('session_id', '').strip()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)

        if not message:
            return jsonify({'error': 'Message required'}), 400
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400
        if not session_id:
            return jsonify({'error': 'session_id required'}), 400

        session = sm.get_session(client_ip, user_id, session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        claude_sid = session.get('claude_session_id')
        if not claude_sid:
            prior = sm.get_messages(client_ip, user_id, session_id)
            if any(m.get('role') == 'assistant' for m in prior):
                claude_sid = session_id
                sm.update_session(client_ip, user_id, session_id, claude_session_id=claude_sid)
                log.info(f'[Chat] 补全 claude_session_id 用于 --resume: {claude_sid}')

        log.info(f'[Chat] user={user_id}, session={session_id}, claude_sid={claude_sid}, msg_len={len(message)}')

        if session.get('title') == '新对话':
            title = message[:20] + ('...' if len(message) > 20 else '')
            sm.update_session(client_ip, user_id, session_id, title=title)

        uploaded_files = data.get('files', [])
        sm.add_message(client_ip, user_id, session_id, 'user', message,
                       files=uploaded_files if uploaded_files else None)

        upload_dir = sm.get_upload_dir(client_ip, user_id, session_id)
        session_workspace = sm.get_session_dir(client_ip, user_id, session_id)
        session_workspace.mkdir(parents=True, exist_ok=True)
        uploaded_files = data.get('files', []) or []
        file_paths = resolve_session_upload_paths(upload_dir, uploaded_files)
        if file_paths:
            log.info(f'[Chat] 附件文件（服务端解析）: {file_paths}')

        collected_text = []
        collected_thinking = []
        new_claude_sid = [None]
        cli_ctx = {
            'user_id': user_id,
            'session_id': session_id,
            'log_dir': config.LOG_DIR,
        }

        def _forward_stream(stream_gen):
            for event_str in stream_gen:
                yield event_str
                if not event_str.startswith('data: '):
                    continue
                try:
                    evt = json.loads(event_str[6:].strip())
                    t = evt.get('type')
                    if t == 'text':
                        collected_text.append(evt.get('content', ''))
                    elif t == 'thinking':
                        collected_thinking.append(evt.get('content', ''))
                    elif t == 'session':
                        sid = evt.get('session_id')
                        new_claude_sid[0] = sid
                        if sid:
                            sm.update_session(client_ip, user_id, session_id, claude_session_id=sid)
                            log.info(f'[Chat] 保存 claude_session_id={sid}（流内）')
                except json.JSONDecodeError:
                    pass

        def generate():
            log.info(
                '[Chat] 外环编排 max_rounds=%s',
                config.CLAUDE_WEB_ORCH_MAX_ROUNDS,
            )
            yield from _forward_stream(
                orchestrator.stream_orchestrated_turns(
                    first_message=message,
                    file_paths=file_paths,
                    session_id=session_id,
                    initial_claude_session_id=claude_sid,
                    max_rounds=config.CLAUDE_WEB_ORCH_MAX_ROUNDS,
                    upload_dir=str(upload_dir),
                    session_workspace_dir=str(session_workspace.resolve()),
                    readonly_dirs=_readonly(),
                    readonly_dirs_notes=_readonly_notes(),
                    skill_bundles=_readonly_bundles(),
                    cli_log_context=cli_ctx,
                )
            )

        def on_finish():
            full_text = ''.join(collected_text)
            full_thinking = ''.join(collected_thinking) if collected_thinking else None
            if full_text:
                sm.add_message(client_ip, user_id, session_id, 'assistant', full_text, thinking=full_thinking)
            if new_claude_sid[0]:
                sm.update_session(client_ip, user_id, session_id, claude_session_id=new_claude_sid[0])
                log.info(f'[Chat] 保存 claude_session_id={new_claude_sid[0]}（收尾）')

        response = Response(
            stream_with_context(generate()),
            content_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
                'Connection': 'keep-alive',
            }
        )
        response.call_on_close(lambda: threading.Thread(target=on_finish, daemon=True).start())
        return response

    @app.route('/chat/orchestration/continue', methods=['POST'])
    @optional_token
    def orchestration_continue():
        """暂停后继续外环编排，或单轮「结束并总结」。需有效的 continuation_token。"""
        data = request.json or {}
        user_id = data.get('user_id', '').strip()
        session_id = data.get('session_id', '').strip()
        token = (data.get('continuation_token') or '').strip()
        action = (data.get('action') or 'continue').strip().lower()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)

        if not user_id or not session_id:
            return jsonify({'error': 'user_id and session_id required'}), 400
        if not token:
            return jsonify({'error': 'continuation_token required'}), 400

        sess = sm.get_session(client_ip, user_id, session_id)
        if not sess:
            return jsonify({'error': 'Session not found'}), 404

        session_workspace = sm.get_session_dir(client_ip, user_id, session_id)
        state = orchestrator.read_pause_state(session_workspace)
        if not orchestrator.validate_continuation_token(state, token):
            return jsonify({'error': '无效或已过期的 continuation_token'}), 400

        orchestrator.clear_pause_state(session_workspace)

        upload_dir = sm.get_upload_dir(client_ip, user_id, session_id)
        session_workspace.mkdir(parents=True, exist_ok=True)

        claude_sid = (state or {}).get('claude_session_id') or session_id
        try:
            total_offset = int((state or {}).get('total_rounds_all_segments') or 0)
        except (TypeError, ValueError):
            total_offset = 0

        log.info(
            '[Orchestration/continue] user=%s session=%s action=%s offset=%s',
            user_id, session_id, action, total_offset,
        )

        collected_text = []
        collected_thinking = []
        new_claude_sid = [None]

        cli_ctx = {
            'user_id': user_id,
            'session_id': session_id,
            'log_dir': config.LOG_DIR,
        }

        def _forward_stream(stream_gen):
            for event_str in stream_gen:
                yield event_str
                if not event_str.startswith('data: '):
                    continue
                try:
                    evt = json.loads(event_str[6:].strip())
                    t = evt.get('type')
                    if t == 'text':
                        collected_text.append(evt.get('content', ''))
                    elif t == 'thinking':
                        collected_thinking.append(evt.get('content', ''))
                    elif t == 'session':
                        sid = evt.get('session_id')
                        new_claude_sid[0] = sid
                        if sid:
                            sm.update_session(client_ip, user_id, session_id, claude_session_id=sid)
                            log.info(f'[Orchestration/continue] 保存 claude_session_id={sid}（流内）')
                except json.JSONDecodeError:
                    pass

        def generate():
            if action == 'summarize':
                yield from _forward_stream(
                    orchestrator.stream_summarize_only(
                        message=orchestrator.build_summarize_after_pause_prompt(),
                        session_id=session_id,
                        claude_session_id=claude_sid,
                        upload_dir=str(upload_dir),
                        session_workspace_dir=str(session_workspace.resolve()),
                        readonly_dirs=_readonly(),
                        readonly_dirs_notes=_readonly_notes(),
                        skill_bundles=_readonly_bundles(),
                        cli_log_context=cli_ctx,
                    )
                )
            else:
                yield from _forward_stream(
                    orchestrator.stream_orchestrated_turns(
                        first_message=orchestrator.build_continue_segment_prompt(),
                        file_paths=None,
                        session_id=session_id,
                        initial_claude_session_id=claude_sid,
                        max_rounds=config.CLAUDE_WEB_ORCH_MAX_ROUNDS,
                        upload_dir=str(upload_dir),
                        session_workspace_dir=str(session_workspace.resolve()),
                        readonly_dirs=_readonly(),
                        readonly_dirs_notes=_readonly_notes(),
                        skill_bundles=_readonly_bundles(),
                        cli_log_context=cli_ctx,
                        total_rounds_offset=total_offset,
                    )
                )

        def on_finish():
            full_text = ''.join(collected_text)
            full_thinking = ''.join(collected_thinking) if collected_thinking else None
            if full_text:
                sm.add_message(
                    client_ip, user_id, session_id, 'assistant', full_text, thinking=full_thinking,
                )
            if new_claude_sid[0]:
                sm.update_session(client_ip, user_id, session_id, claude_session_id=new_claude_sid[0])
                log.info(f'[Orchestration/continue] 保存 claude_session_id={new_claude_sid[0]}（收尾）')

        response = Response(
            stream_with_context(generate()),
            content_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
                'Connection': 'keep-alive',
            },
        )
        response.call_on_close(lambda: threading.Thread(target=on_finish, daemon=True).start())
        return response

    @app.route('/sessions', methods=['GET'])
    @optional_token
    def get_sessions():
        user_id = request.args.get('user_id', '').strip()
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        sessions = sm.list_sessions(client_ip, user_id)
        return jsonify(sessions)

    @app.route('/sessions', methods=['POST'])
    @optional_token
    def create_session():
        data = request.json or {}
        user_id = data.get('user_id', '').strip()
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        session = sm.create_session(client_ip, user_id)
        return jsonify(session)

    @app.route('/sessions/<session_id>', methods=['DELETE'])
    @optional_token
    def delete_session(session_id):
        user_id = request.args.get('user_id', '').strip()
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        backed = backup_session_before_delete(
            config.BACKUPS_DIR, config.CACHE_DIR, config.LOG_DIR,
            client_ip, user_id, session_id,
        )
        sm.delete_session(client_ip, user_id, session_id)
        out = {'ok': True}
        if backed is not None:
            try:
                out['backed_up_to'] = str(backed.relative_to(config.ROOT))
            except ValueError:
                out['backed_up_to'] = str(backed)
        return jsonify(out)

    @app.route('/sessions/<session_id>/messages', methods=['GET'])
    @optional_token
    def get_messages(session_id):
        user_id = request.args.get('user_id', '').strip()
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        messages = sm.get_messages(client_ip, user_id, session_id)
        return jsonify(messages)

    @app.route('/upload', methods=['POST'])
    @optional_token
    def upload_file():
        user_id = request.form.get('user_id', '').strip()
        session_id = request.form.get('session_id', '').strip()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)

        if not user_id or not session_id:
            return jsonify({'error': 'user_id and session_id required'}), 400

        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400

        file = request.files['file']
        raw = (file.filename or '').strip()
        if not raw:
            raw = ''

        file.seek(0, 2)
        size = file.tell()
        file.seek(0)
        if size > config.UPLOAD_MAX_SIZE:
            return jsonify({'error': f'File too large (max {config.UPLOAD_MAX_SIZE // 1024 // 1024}MB)'}), 400

        upload_dir = sm.get_upload_dir(client_ip, user_id, session_id)
        canonical = safe_client_filename(raw or '')
        disk_name = canonical
        if not is_ascii_filename(canonical):
            disk_name = ascii_storage_filename(canonical)

        target = upload_dir / disk_name
        if target.exists():
            stem = target.stem
            suffix = target.suffix
            counter = 1
            while target.exists():
                target = upload_dir / f'{stem}_{counter}{suffix}'
                counter += 1

        file.save(str(target))

        log.info(f'[Upload] 文件已保存: {target} ({size} bytes), user={user_id}, session={session_id}')

        return jsonify({
            'name': target.name,
            'size': size,
            'path': str(target),
            'display_name': canonical,
        })

    @app.route('/sessions/<session_id>/files', methods=['GET'])
    @optional_token
    def list_session_files(session_id):
        user_id = request.args.get('user_id', '').strip()
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        files = sm.list_uploads(client_ip, user_id, session_id)
        return jsonify(files)

    @app.route('/feedback', methods=['POST'])
    @optional_token
    def feedback():
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        text = (request.form.get('text') or '').strip()
        contact = (request.form.get('contact') or '').strip()
        user_id = (request.form.get('user_id') or '').strip()
        if not text:
            return jsonify({'error': 'text required'}), 400
        images = request.files.getlist('images')
        dest = save_feedback_package(
            config.FEEDBACK_DIR, client_ip, user_id, text, contact, images,
        )
        log.info(f'[Feedback] 已保存: {dest}')
        try:
            rel = str(dest.relative_to(config.ROOT))
        except ValueError:
            rel = str(dest)
        return jsonify({'ok': True, 'saved_to': rel})

    @app.errorhandler(500)
    def internal_error(e):
        log.error(f'Internal error: {e}')
        return jsonify({'error': 'Internal server error'}), 500

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({'error': 'Not found'}), 404
