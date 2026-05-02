"""HTTP 路由。"""

import json
import logging
import re
import threading
from pathlib import Path

from flask import Response, jsonify, request, send_from_directory, stream_with_context
from . import config
from .auth import optional_token
from .backup_service import backup_session_before_delete
from . import orchestrator
from .claude_runner import CLAUDE_CLI_PATH, resolve_session_upload_paths, stop_session_process, stream_claude_output
from .dev_projects import (
    DevProjectError,
    clear_dev_session,
    diff_for_project,
    find_project,
    git_status,
    load_dev_session,
    load_projects,
    project_public_info,
    run_project_test,
    save_dev_session,
)
from .feedback_service import save_feedback_package
from .filename_sanitize import ascii_storage_filename, is_ascii_filename, safe_client_filename
from .paths import get_client_ip
from .session_manager import SessionManager
from .tavily_search import TavilySearchError, format_tavily_for_prompt, search_tavily
from .user_claude_credentials import (
    delete_credentials,
    load_credentials,
    merge_env_preserve_existing,
    public_status,
    resolve_claude_runtime_for_request,
    sanitize_env,
    save_credentials,
    validate_save_payload,
)

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

    def _bundle_terms(bundle: dict):
        raw = [
            bundle.get('id') or '',
            bundle.get('title') or bundle.get('name') or '',
            bundle.get('summary') or bundle.get('description') or '',
        ]
        raw.extend(str(x) for x in (bundle.get('keywords') or []) if x)
        terms = []
        for s in raw:
            s = str(s).strip().lower()
            if not s:
                continue
            if len(s) <= 80:
                terms.append(s)
            for part in re.findall(r'[a-z0-9_./+-]{3,}|[\u4e00-\u9fff]{2,}', s):
                if part not in terms:
                    terms.append(part)
                if re.fullmatch(r'[\u4e00-\u9fff]{2,}', part):
                    for n in (2, 3, 4):
                        for i in range(0, max(0, len(part) - n + 1)):
                            sub = part[i:i + n]
                            if sub not in terms:
                                terms.append(sub)
        return terms

    def _recent_history_text(messages: list, limit: int = 4000) -> str:
        chunks = []
        total = 0
        for m in reversed(messages or []):
            if not isinstance(m, dict):
                continue
            c = str(m.get('content') or '').strip()
            if not c:
                continue
            chunks.append(c[:800])
            total += min(len(c), 800)
            if total >= limit:
                break
        return '\n'.join(reversed(chunks))

    def _select_skill_bundles(message: str, prior_messages=None, bundle_ids=None):
        bundles = _readonly_bundles()
        wanted_ids = {str(x) for x in (bundle_ids or []) if x}
        text = f'{message or ""}\n{_recent_history_text(prior_messages or [])}'.lower()
        selected = []
        rendered = []
        for b in bundles:
            bid = str(b.get('id') or '')
            mounted = False
            reason = ''
            if b.get('always_mount'):
                mounted = True
                reason = 'always_mount'
            elif wanted_ids and bid in wanted_ids:
                mounted = True
                reason = 'continuation'
            else:
                for term in _bundle_terms(b):
                    if term and term in text:
                        mounted = True
                        reason = f'keyword: {term[:40]}'
                        break
            bb = dict(b)
            bb['mounted'] = mounted
            bb['mount_reason'] = reason
            rendered.append(bb)
            if mounted:
                selected.append(bb)
        return rendered, selected

    def _bundle_paths(selected_bundles):
        out = []
        seen = set()
        for b in selected_bundles or []:
            for p in b.get('paths') or []:
                if p and p not in seen:
                    seen.add(p)
                    out.append(p)
        return out

    def _v2_orch_kwargs(rt: dict) -> dict:
        return {
            'child_env_extra': rt.get('child_env_extra'),
            'model_override': rt.get('model_override'),
        }

    def _dev_enabled():
        return bool(getattr(config, 'FEATURE_MOBILE_REMOTE_DEVELOPMENT', False))

    def _dev_projects():
        return load_projects(config.DEV_PROJECTS_CONFIG_FILE)

    def _dev_disabled_response():
        return jsonify({'error': 'mobile_remote_development disabled', 'code': 'dev_disabled'}), 404

    def _session_for_user(client_ip: str, user_id: str, session_id: str):
        if not user_id:
            return None, (jsonify({'error': 'user_id required'}), 400)
        if not session_id:
            return None, (jsonify({'error': 'session_id required'}), 400)
        session = sm.get_session(client_ip, user_id, session_id)
        if not session:
            return None, (jsonify({'error': 'Session not found'}), 404)
        return session, None

    def _attached_dev_project(client_ip: str, user_id: str, session_id: str):
        if not _dev_enabled():
            return None, None
        meta = load_dev_session(sm.get_session_dir(client_ip, user_id, session_id))
        if not meta or meta.get('mode') != 'development':
            return None, None
        try:
            project = find_project(_dev_projects(), meta.get('project_id') or '')
        except DevProjectError:
            project = None
        return meta, project

    @app.route('/api/features', methods=['GET'])
    def api_features():
        return jsonify(
            {
                'v2_multi_user_api': bool(config.FEATURE_V2_MULTI_USER_API),
                'v3_linux_deploy': bool(config.FEATURE_V3_LINUX_DEPLOY),
                'tavily_search_configured': bool(config.TAVILY_API_KEY),
                'mobile_remote_development': _dev_enabled(),
            }
        )

    @app.route('/api/dev/projects', methods=['GET'])
    @optional_token
    def api_dev_projects():
        if not _dev_enabled():
            return _dev_disabled_response()
        try:
            projects = [project_public_info(p) for p in _dev_projects()]
        except DevProjectError as e:
            return jsonify({'error': str(e), 'code': 'dev_projects_config_error'}), 400
        return jsonify({'projects': projects})

    @app.route('/api/dev/sessions/<session_id>/attach-project', methods=['POST'])
    @optional_token
    def api_dev_attach_project(session_id):
        if not _dev_enabled():
            return _dev_disabled_response()
        data = request.json or {}
        user_id = (data.get('user_id') or '').strip()
        project_id = (data.get('project_id') or '').strip()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        _, err = _session_for_user(client_ip, user_id, session_id)
        if err:
            return err
        if not project_id:
            return jsonify({'error': 'project_id required'}), 400
        try:
            project = find_project(_dev_projects(), project_id)
        except DevProjectError as e:
            return jsonify({'error': str(e), 'code': 'dev_projects_config_error'}), 400
        if not project:
            return jsonify({'error': 'Project not found in whitelist'}), 404
        meta = save_dev_session(sm.get_session_dir(client_ip, user_id, session_id), project)
        return jsonify({'ok': True, 'session': meta, 'project': project_public_info(project)})

    @app.route('/api/dev/sessions/<session_id>/detach-project', methods=['POST'])
    @optional_token
    def api_dev_detach_project(session_id):
        if not _dev_enabled():
            return _dev_disabled_response()
        data = request.json or {}
        user_id = (data.get('user_id') or '').strip()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        _, err = _session_for_user(client_ip, user_id, session_id)
        if err:
            return err
        cleared = clear_dev_session(sm.get_session_dir(client_ip, user_id, session_id))
        return jsonify({'ok': True, 'cleared': cleared})

    @app.route('/api/dev/sessions/<session_id>/status', methods=['GET'])
    @optional_token
    def api_dev_status(session_id):
        if not _dev_enabled():
            return _dev_disabled_response()
        user_id = request.args.get('user_id', '').strip()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        _, err = _session_for_user(client_ip, user_id, session_id)
        if err:
            return err
        meta, project = _attached_dev_project(client_ip, user_id, session_id)
        if not meta:
            return jsonify({'mode': 'chat', 'attached': False})
        if not project:
            return jsonify({'mode': 'development', 'attached': False, 'error': 'Project no longer exists in whitelist'})
        return jsonify({'mode': 'development', 'attached': True, 'session': meta, 'project': project_public_info(project)})

    @app.route('/api/dev/sessions/<session_id>/diff', methods=['GET'])
    @optional_token
    def api_dev_diff(session_id):
        if not _dev_enabled():
            return _dev_disabled_response()
        user_id = request.args.get('user_id', '').strip()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        _, err = _session_for_user(client_ip, user_id, session_id)
        if err:
            return err
        meta, project = _attached_dev_project(client_ip, user_id, session_id)
        if not meta or not project:
            return jsonify({'error': 'No development project attached'}), 400
        return jsonify(diff_for_project(Path(project['path'])))

    @app.route('/api/dev/sessions/<session_id>/run-test', methods=['POST'])
    @optional_token
    def api_dev_run_test(session_id):
        if not _dev_enabled():
            return _dev_disabled_response()
        data = request.json or {}
        user_id = (data.get('user_id') or '').strip()
        command = (data.get('command') or '').strip()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        _, err = _session_for_user(client_ip, user_id, session_id)
        if err:
            return err
        meta, project = _attached_dev_project(client_ip, user_id, session_id)
        if not meta or not project:
            return jsonify({'error': 'No development project attached'}), 400
        try:
            result = run_project_test(project, command, config.DEV_TEST_TIMEOUT_SECONDS)
        except DevProjectError as e:
            return jsonify({'error': str(e)}), 400
        return jsonify(result)

    @app.route('/api/dev/sessions/<session_id>/stop', methods=['POST'])
    @optional_token
    def api_dev_stop(session_id):
        if not _dev_enabled():
            return _dev_disabled_response()
        data = request.json or {}
        user_id = (data.get('user_id') or '').strip()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        _, err = _session_for_user(client_ip, user_id, session_id)
        if err:
            return err
        return jsonify({'ok': True, 'stopped': stop_session_process(session_id)})

    @app.route('/api/user/claude-credentials', methods=['GET'])
    @optional_token
    def api_get_claude_credentials():
        user_id = request.args.get('user_id', '').strip()
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        data = load_credentials(sm, client_ip, user_id)
        return jsonify(public_status(data))

    @app.route('/api/user/claude-credentials', methods=['PUT'])
    @optional_token
    def api_put_claude_credentials():
        data = request.json or {}
        user_id = data.get('user_id', '').strip()
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        env_in = data.get('env')
        model_in = data.get('model', '')
        if not isinstance(model_in, str):
            model_in = ''
        env_new, err = sanitize_env(env_in)
        if err:
            return jsonify({'error': err}), 400
        existing = load_credentials(sm, client_ip, user_id)
        merged = merge_env_preserve_existing(existing, env_new or {})
        verr = validate_save_payload(merged, model_in)
        if verr:
            return jsonify({'error': verr}), 400
        save_credentials(sm, client_ip, user_id, merged, model_in)
        return jsonify({'ok': True})

    @app.route('/api/user/claude-credentials', methods=['DELETE'])
    @optional_token
    def api_delete_claude_credentials():
        user_id = request.args.get('user_id', '').strip()
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        delete_credentials(sm, client_ip, user_id)
        return jsonify({'ok': True})

    @app.route('/chat', methods=['POST'])
    @optional_token
    def chat():
        data = request.json or {}
        message = data.get('message', '').strip()
        user_id = data.get('user_id', '').strip()
        session_id = data.get('session_id', '').strip()
        web_search_enabled = bool(data.get('web_search'))
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

        dev_meta, dev_project = _attached_dev_project(client_ip, user_id, session_id)
        if dev_meta and not dev_project:
            return jsonify({'error': 'Development project no longer exists in whitelist', 'code': 'dev_project_missing'}), 400

        rt = resolve_claude_runtime_for_request(request, sm, client_ip, user_id)
        if rt.get('error'):
            return jsonify({'error': rt['error'], 'code': 'v2_claude_config_required'}), 400
        if rt.get('use_per_user'):
            log.info('[Chat] V2 使用每用户 API 环境（Host 非本机）')
        if web_search_enabled and not config.TAVILY_API_KEY:
            return jsonify({'error': 'Tavily API key 未配置', 'code': 'tavily_config_required'}), 400

        claude_sid = session.get('claude_session_id')
        prior_messages = sm.get_messages(client_ip, user_id, session_id)
        if not claude_sid:
            if any(m.get('role') == 'assistant' for m in prior_messages):
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

        skill_bundles, selected_bundles = _select_skill_bundles(message, prior_messages)
        selected_bundle_ids = [str(b.get('id')) for b in selected_bundles if b.get('id')]
        readonly_dirs = _readonly() + _bundle_paths(selected_bundles)
        if selected_bundle_ids:
            log.info('[Chat] 本轮按需挂载技能包: %s', selected_bundle_ids)

        dev_context = None
        dev_cli_cwd = None
        dev_permission_mode = None
        dev_dangerous_skip = None
        if dev_project:
            project_git = git_status(Path(dev_project['path']))
            dev_cli_cwd = dev_project['path']
            dev_permission_mode = config.DEV_PERMISSION_MODE
            dev_dangerous_skip = config.DEV_DANGEROUSLY_SKIP_PERMISSIONS
            dev_context = {
                'project_id': dev_project['id'],
                'project_name': dev_project.get('name') or dev_project['id'],
                'project_path': dev_project['path'],
                'session_cache_dir': str(session_workspace),
                'git': project_git,
                'default_tests': dev_project.get('default_tests') or [],
            }
            log.info('[Chat] 开发模式 session=%s project=%s cwd=%s', session_id, dev_project['id'], dev_project['path'])

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
            web_search_context = ''
            if web_search_enabled:
                yield 'data: ' + json.dumps({'type': 'info', 'message': '正在使用 Tavily 联网搜索…'}, ensure_ascii=False) + '\n\n'
                try:
                    tavily_data = search_tavily(
                        api_key=config.TAVILY_API_KEY,
                        query=message,
                        max_results=config.TAVILY_MAX_RESULTS,
                        search_depth=config.TAVILY_SEARCH_DEPTH,
                    )
                    web_search_context = format_tavily_for_prompt(tavily_data, message)
                    yield 'data: ' + json.dumps({'type': 'info', 'message': '联网搜索完成，正在交给 Claude 整理…'}, ensure_ascii=False) + '\n\n'
                    log.info('[Chat] Tavily 搜索完成 user=%s session=%s', user_id, session_id)
                except TavilySearchError as e:
                    msg = f'联网搜索失败：{e}'
                    yield 'data: ' + json.dumps({'type': 'error', 'message': msg, 'soft': True}, ensure_ascii=False) + '\n\n'
                    web_search_context = (
                        '【联网搜索资料 — Tavily】\n'
                        f'用户请求了联网搜索，但 Tavily 搜索失败：{e}\n'
                        '请明确告知用户联网搜索未成功，不要编造最新信息。\n\n'
                    )

            if dev_project:
                yield 'data: ' + json.dumps(
                    {
                        'type': 'info',
                        'message': f'开发模式：已连接项目 {dev_project.get("name") or dev_project["id"]}，AI 将在该项目真实目录中工作。',
                    },
                    ensure_ascii=False,
                ) + '\n\n'

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
                    readonly_dirs=readonly_dirs,
                    readonly_dirs_notes=_readonly_notes(),
                    skill_bundles=skill_bundles,
                    cli_log_context=cli_ctx,
                    conversation_history=prior_messages,
                    mounted_bundle_ids=selected_bundle_ids,
                    web_search_context=web_search_context,
                    cli_cwd_dir=dev_cli_cwd,
                    permission_mode_override=dev_permission_mode,
                    dangerously_skip_permissions_override=dev_dangerous_skip,
                    development_context=dev_context,
                    **_v2_orch_kwargs(rt),
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

        rt = resolve_claude_runtime_for_request(request, sm, client_ip, user_id)
        if rt.get('error'):
            return jsonify({'error': rt['error'], 'code': 'v2_claude_config_required'}), 400
        if rt.get('use_per_user'):
            log.info('[Orchestration/continue] V2 使用每用户 API 环境')

        session_workspace = sm.get_session_dir(client_ip, user_id, session_id)
        state = orchestrator.read_pause_state(session_workspace)
        if not orchestrator.validate_continuation_token(state, token):
            return jsonify({'error': '无效或已过期的 continuation_token'}), 400

        orchestrator.clear_pause_state(session_workspace)

        upload_dir = sm.get_upload_dir(client_ip, user_id, session_id)
        session_workspace.mkdir(parents=True, exist_ok=True)

        claude_sid = (state or {}).get('claude_session_id') or session_id
        mounted_ids = (state or {}).get('mounted_bundle_ids') or []
        skill_bundles, selected_bundles = _select_skill_bundles('', [], bundle_ids=mounted_ids)
        readonly_dirs = _readonly() + _bundle_paths(selected_bundles)
        try:
            total_offset = int((state or {}).get('total_rounds_all_segments') or 0)
        except (TypeError, ValueError):
            total_offset = 0

        dev_meta, dev_project = _attached_dev_project(client_ip, user_id, session_id)
        dev_context = None
        dev_cli_cwd = None
        dev_permission_mode = None
        dev_dangerous_skip = None
        if dev_project:
            project_git = git_status(Path(dev_project['path']))
            dev_cli_cwd = dev_project['path']
            dev_permission_mode = config.DEV_PERMISSION_MODE
            dev_dangerous_skip = config.DEV_DANGEROUSLY_SKIP_PERMISSIONS
            dev_context = {
                'project_id': dev_project['id'],
                'project_name': dev_project.get('name') or dev_project['id'],
                'project_path': dev_project['path'],
                'session_cache_dir': str(session_workspace),
                'git': project_git,
                'default_tests': dev_project.get('default_tests') or [],
            }

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
                        readonly_dirs=readonly_dirs,
                        readonly_dirs_notes=_readonly_notes(),
                        skill_bundles=skill_bundles,
                        cli_log_context=cli_ctx,
                        cli_cwd_dir=dev_cli_cwd,
                        permission_mode_override=dev_permission_mode,
                        dangerously_skip_permissions_override=dev_dangerous_skip,
                        development_context=dev_context,
                        **_v2_orch_kwargs(rt),
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
                        readonly_dirs=readonly_dirs,
                        readonly_dirs_notes=_readonly_notes(),
                        skill_bundles=skill_bundles,
                        cli_log_context=cli_ctx,
                        total_rounds_offset=total_offset,
                        mounted_bundle_ids=mounted_ids,
                        cli_cwd_dir=dev_cli_cwd,
                        permission_mode_override=dev_permission_mode,
                        dangerously_skip_permissions_override=dev_dangerous_skip,
                        development_context=dev_context,
                        **_v2_orch_kwargs(rt),
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
