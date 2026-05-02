import io
import json
import logging
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from claude_web import config, settings_loader
from claude_web.app_factory import create_app


class BackendSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)

        config.CACHE_DIR = root / 'cache'
        config.LOG_DIR = root / 'logs'
        config.BACKUPS_DIR = root / 'backups'
        config.FEEDBACK_DIR = root / 'feedback'
        config.PATHS_CONFIG_FILE = root / 'missing-paths.json'
        config.TOKEN = ''
        config.ENABLE_AUTH = False
        config.TRUST_X_FORWARDED = False
        config.TAVILY_API_KEY = ''
        config.FEATURE_V2_MULTI_USER_API = False
        config.FEATURE_MOBILE_REMOTE_DEVELOPMENT = False
        config.FEATURE_GEMINI_SUPPORT = False
        config.GEMINI_CLI_PATH = 'gemini'
        config.GEMINI_MODEL = ''
        config.GEMINI_APPROVAL_MODE = 'plan'
        config.GEMINI_SANDBOX = False
        config.GEMINI_SKIP_TRUST = True
        config.GEMINI_PROXY = ''
        config.GEMINI_REQUEST_TIMEOUT_SECONDS = 10
        config.DEV_PROJECTS_CONFIG_FILE = root / 'claude_web_projects.config.json'
        config.DEV_TEST_TIMEOUT_SECONDS = 10
        config.DEV_PERMISSION_MODE = 'acceptEdits'
        config.DEV_DANGEROUSLY_SKIP_PERMISSIONS = False
        config.UPLOAD_MAX_SIZE = 100 * 1024 * 1024

        self.app = create_app()
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        self.user_id = 'smoke-user'

    def tearDown(self):
        logging.shutdown()
        self.tmp.cleanup()

    def create_session(self):
        resp = self.client.post('/sessions', json={'user_id': self.user_id})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertIn('id', data)
        return data

    def make_fake_gemini_cli(self):
        fake_dir = Path(self.tmp.name) / 'fake-gemini'
        fake_dir.mkdir()
        script = fake_dir / 'fake_gemini.py'
        script.write_text(
            '\n'.join(
                [
                    'import json, os, pathlib, sys',
                    'args = sys.argv[1:]',
                    'pathlib.Path(os.environ["FAKE_GEMINI_ARGS_FILE"]).write_text(json.dumps(args), encoding="utf-8")',
                    'pathlib.Path(os.environ["FAKE_GEMINI_PROMPT_FILE"]).write_text(sys.stdin.read(), encoding="utf-8")',
                    'sid = "fake-gemini-session"',
                    'if "--resume" in args:',
                    '    sid = args[args.index("--resume") + 1]',
                    'print(json.dumps({"type": "init", "session_id": sid, "model": "fake"}), flush=True)',
                    'print(json.dumps({"type": "tool_use", "tool_name": "read_file", "tool_id": "t1", "parameters": {"path": "uploads/a.txt"}}), flush=True)',
                    'print(json.dumps({"type": "tool_result", "tool_id": "t1", "status": "success"}), flush=True)',
                    'print(json.dumps({"type": "message", "role": "assistant", "content": "Gemini ", "delta": True}), flush=True)',
                    'print(json.dumps({"type": "message", "role": "assistant", "content": "OK", "delta": True}), flush=True)',
                    'print(json.dumps({"type": "result", "status": "success", "stats": {}}), flush=True)',
                ]
            ),
            encoding='utf-8',
        )
        if os.name == 'nt':
            launcher = fake_dir / 'fake_gemini.cmd'
            launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding='utf-8')
        else:
            launcher = fake_dir / 'fake_gemini'
            launcher.write_text(f'#!{sys.executable}\nimport runpy\nrunpy.run_path({str(script)!r}, run_name="__main__")\n', encoding='utf-8')
            launcher.chmod(0o755)
        os.environ['FAKE_GEMINI_ARGS_FILE'] = str(fake_dir / 'args.json')
        os.environ['FAKE_GEMINI_PROMPT_FILE'] = str(fake_dir / 'prompt.txt')
        return launcher

    def test_features_endpoint_reports_core_flags(self):
        resp = self.client.get('/api/features')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn('v2_multi_user_api', data)
        self.assertIn('v3_linux_deploy', data)
        self.assertIn('tavily_search_configured', data)
        self.assertIn('mobile_remote_development', data)
        self.assertIn('gemini_support', data)
        self.assertIn('gemini_configured', data)
        self.assertFalse(data['tavily_search_configured'])
        self.assertFalse(data['mobile_remote_development'])
        self.assertFalse(data['gemini_support'])
        self.assertFalse(data['gemini_configured'])

    def test_dev_api_is_hidden_when_feature_disabled(self):
        resp = self.client.get('/api/dev/projects')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()['code'], 'dev_disabled')

    def test_dev_project_whitelist_attach_diff_and_test_when_enabled(self):
        config.FEATURE_MOBILE_REMOTE_DEVELOPMENT = True
        project_dir = Path(self.tmp.name) / 'project'
        project_dir.mkdir()
        (project_dir / 'app.py').write_text('print("hello")\n', encoding='utf-8')
        config.DEV_PROJECTS_CONFIG_FILE.write_text(
            json.dumps(
                {
                    'projects': [
                        {
                            'id': 'demo',
                            'name': 'Demo Project',
                            'path': str(project_dir),
                            'default_tests': ['python -c "print(123)"'],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )

        session = self.create_session()
        projects = self.client.get('/api/dev/projects')
        self.assertEqual(projects.status_code, 200, projects.get_data(as_text=True))
        self.assertEqual(projects.get_json()['projects'][0]['id'], 'demo')

        attached = self.client.post(
            f'/api/dev/sessions/{session["id"]}/attach-project',
            json={'user_id': self.user_id, 'project_id': 'demo'},
        )
        self.assertEqual(attached.status_code, 200, attached.get_data(as_text=True))
        self.assertTrue(attached.get_json()['ok'])

        status = self.client.get(f'/api/dev/sessions/{session["id"]}/status?user_id={self.user_id}')
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.get_json()['attached'])

        diff = self.client.get(f'/api/dev/sessions/{session["id"]}/diff?user_id={self.user_id}')
        self.assertEqual(diff.status_code, 200)
        self.assertIn('diff', diff.get_json())

        result = self.client.post(
            f'/api/dev/sessions/{session["id"]}/run-test',
            json={'user_id': self.user_id, 'command': 'python -c "print(123)"'},
        )
        self.assertEqual(result.status_code, 200, result.get_data(as_text=True))
        self.assertTrue(result.get_json()['ok'])
        self.assertIn('123', result.get_json()['stdout'])

    def test_session_crud_is_available_and_user_scoped(self):
        missing_user = self.client.get('/sessions')
        self.assertEqual(missing_user.status_code, 400)

        session = self.create_session()
        session_id = session['id']
        self.assertEqual(session['provider'], 'claude')
        self.assertEqual(session['provider_session_ids'], {})

        list_resp = self.client.get(f'/sessions?user_id={self.user_id}')
        self.assertEqual(list_resp.status_code, 200)
        self.assertEqual([s['id'] for s in list_resp.get_json()], [session_id])

        other_resp = self.client.get('/sessions?user_id=another-user')
        self.assertEqual(other_resp.status_code, 200)
        self.assertEqual(other_resp.get_json(), [])

        msg_resp = self.client.get(f'/sessions/{session_id}/messages?user_id={self.user_id}')
        self.assertEqual(msg_resp.status_code, 200)
        self.assertEqual(msg_resp.get_json(), [])

        del_resp = self.client.delete(f'/sessions/{session_id}?user_id={self.user_id}')
        self.assertEqual(del_resp.status_code, 200)
        self.assertTrue(del_resp.get_json()['ok'])

        list_after = self.client.get(f'/sessions?user_id={self.user_id}')
        self.assertEqual(list_after.get_json(), [])

    def test_legacy_session_records_default_to_claude_provider(self):
        user_dir = config.CACHE_DIR / '127_0_0_1' / self.user_id
        user_dir.mkdir(parents=True)
        session_id = 'legacy-session'
        (user_dir / 'sessions.json').write_text(
            json.dumps(
                [
                    {
                        'id': session_id,
                        'claude_session_id': 'claude-legacy-id',
                        'title': 'Legacy',
                        'created_at': '2026-01-01 00:00:00',
                        'updated_at': '2026-01-01 00:00:00',
                    }
                ],
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )

        resp = self.client.get(f'/sessions?user_id={self.user_id}')
        self.assertEqual(resp.status_code, 200)
        session = resp.get_json()[0]
        self.assertEqual(session['provider'], 'claude')
        self.assertEqual(session['provider_session_ids']['claude'], 'claude-legacy-id')

    def test_gemini_session_creation_requires_feature_flag(self):
        unsupported = self.client.post('/sessions', json={'user_id': self.user_id, 'provider': 'unknown'})
        self.assertEqual(unsupported.status_code, 400)
        self.assertEqual(unsupported.get_json()['code'], 'unsupported_provider')

        resp = self.client.post('/sessions', json={'user_id': self.user_id, 'provider': 'gemini'})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()['code'], 'gemini_disabled')

    def test_gemini_session_metadata_when_feature_enabled(self):
        config.FEATURE_GEMINI_SUPPORT = True
        config.GEMINI_CLI_PATH = str(self.make_fake_gemini_cli())
        resp = self.client.post('/sessions', json={'user_id': self.user_id, 'provider': 'gemini'})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        session = resp.get_json()
        self.assertEqual(session['provider'], 'gemini')
        self.assertEqual(session['provider_session_ids'], {})

        chat = self.client.post(
            '/chat',
            json={'user_id': self.user_id, 'session_id': session['id'], 'message': 'hello'},
        )
        self.assertEqual(chat.status_code, 200, chat.get_data(as_text=True))
        body = chat.get_data(as_text=True)
        self.assertIn('"type": "session"', body)
        self.assertIn('"provider": "gemini"', body)
        self.assertIn('"type": "tool_start"', body)
        self.assertIn('"content": "Gemini "', body)
        self.assertIn('"content": "OK"', body)

        updated = self.client.get(f'/sessions?user_id={self.user_id}').get_json()[0]
        self.assertEqual(updated['provider_session_ids']['gemini'], 'fake-gemini-session')

        second = self.client.post(
            '/chat',
            json={'user_id': self.user_id, 'session_id': session['id'], 'message': 'again'},
        )
        self.assertEqual(second.status_code, 200, second.get_data(as_text=True))
        args_file = Path(os.environ['FAKE_GEMINI_ARGS_FILE'])
        args = json.loads(args_file.read_text(encoding='utf-8'))
        self.assertIn('--resume', args)
        self.assertEqual(args[args.index('--resume') + 1], 'fake-gemini-session')
        self.assertNotIn('latest', args)

        # call_on_close 保存助手消息在测试客户端中异步触发，留一小段时间避免慢机器抖动。
        second.close()
        time.sleep(0.05)

    def test_explicit_user_memory_is_saved_without_model_tool_call(self):
        config.FEATURE_GEMINI_SUPPORT = True
        config.GEMINI_CLI_PATH = str(self.make_fake_gemini_cli())
        session = self.client.post('/sessions', json={'user_id': self.user_id, 'provider': 'gemini'}).get_json()

        chat = self.client.post(
            '/chat',
            json={'user_id': self.user_id, 'session_id': session['id'], 'message': '我叫王亚宁，以后记得叫我宁哥。'},
        )
        self.assertEqual(chat.status_code, 200, chat.get_data(as_text=True))
        chat.get_data(as_text=True)

        memory_path = config.CACHE_DIR / '127_0_0_1' / self.user_id / session['id'] / 'memory.md'
        memory = memory_path.read_text(encoding='utf-8')
        self.assertIn('- 用户姓名：王亚宁', memory)
        self.assertIn('- 偏好称呼：宁哥', memory)

    def test_explicit_user_memory_fallback_does_not_apply_to_claude(self):
        session = self.create_session()
        memory_path = config.CACHE_DIR / '127_0_0_1' / self.user_id / session['id'] / 'memory.md'
        before = memory_path.read_text(encoding='utf-8')
        from claude_web import orchestrator

        old_stream = orchestrator.stream_orchestrated_turns

        def fake_stream(**kwargs):
            yield 'data: {"type":"text","content":"ok"}\n\n'
            yield 'data: {"type":"done","ok":true}\n\n'
            yield 'data: {"type":"orchestration_complete","ok":true}\n\n'

        try:
            orchestrator.stream_orchestrated_turns = fake_stream
            chat = self.client.post(
                '/chat',
                json={'user_id': self.user_id, 'session_id': session['id'], 'message': '我叫王亚宁，以后记得叫我宁哥。'},
            )
            self.assertEqual(chat.status_code, 200, chat.get_data(as_text=True))
            chat.get_data(as_text=True)
        finally:
            orchestrator.stream_orchestrated_turns = old_stream

        self.assertEqual(memory_path.read_text(encoding='utf-8'), before)

    def test_upload_accepts_small_files_and_rejects_over_limit(self):
        session = self.create_session()
        session_id = session['id']

        small = {
            'user_id': self.user_id,
            'session_id': session_id,
            'file': (io.BytesIO(b'hello'), 'hello.txt'),
        }
        resp = self.client.post('/upload', data=small, content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()['size'], 5)

        files_resp = self.client.get(f'/sessions/{session_id}/files?user_id={self.user_id}')
        self.assertEqual(files_resp.status_code, 200)
        self.assertEqual(files_resp.get_json()[0]['name'], 'hello.txt')

        config.UPLOAD_MAX_SIZE = 5
        too_large = {
            'user_id': self.user_id,
            'session_id': session_id,
            'file': (io.BytesIO(b'123456'), 'large.txt'),
        }
        rejected = self.client.post('/upload', data=too_large, content_type='multipart/form-data')
        self.assertEqual(rejected.status_code, 400)
        self.assertIn('File too large', rejected.get_json()['error'])

    def test_chat_rejects_web_search_when_tavily_is_not_configured(self):
        session = self.create_session()
        resp = self.client.post(
            '/chat',
            json={
                'user_id': self.user_id,
                'session_id': session['id'],
                'message': 'search current news',
                'web_search': True,
            },
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()['code'], 'tavily_config_required')

    def test_settings_loader_accepts_utf8_bom_config(self):
        ini = Path(self.tmp.name) / 'bom-config.ini'
        ini.write_bytes('\ufeff[upload]\nmax_size_mb = 7\n'.encode('utf-8'))
        parser = settings_loader.load_configparser(ini)
        self.assertEqual(settings_loader.get_int(parser, 'upload', 'max_size_mb', 1), 7)


if __name__ == '__main__':
    unittest.main(verbosity=2)
