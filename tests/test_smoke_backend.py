import io
import json
import logging
import tempfile
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

    def test_features_endpoint_reports_core_flags(self):
        resp = self.client.get('/api/features')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn('v2_multi_user_api', data)
        self.assertIn('v3_linux_deploy', data)
        self.assertIn('tavily_search_configured', data)
        self.assertIn('mobile_remote_development', data)
        self.assertFalse(data['tavily_search_configured'])
        self.assertFalse(data['mobile_remote_development'])

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
