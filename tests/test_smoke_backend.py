import io
import json
import logging
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from claude_web import config, settings_loader
from claude_web.app_factory import create_app
from claude_web.session_manager import (
    DEFAULT_SESSION_TITLE,
    USER_GLOBAL_MEMORY_FILENAME,
    SessionManager,
    derive_session_title_from_message,
)


class BackendSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)

        config.CACHE_DIR = root / 'cache'
        config.LOG_DIR = root / 'logs'
        config.BACKUPS_DIR = root / 'backups'
        config.FEEDBACK_DIR = root / 'feedback'
        config.PATHS_CONFIG_FILE = root / 'missing-paths.json'
        config.ANDROID_ANALYSIS_KNOWLEDGE_DIR = root / 'android_analysis_knowledge'
        config.ANDROID_ANALYSIS_7Z_PATH = ''
        config.ANDROID_ANALYSIS_DEBUG_TRACE = True
        config.ANDROID_ANALYSIS_AUTO_DEEP_CONFIDENCE_THRESHOLD = 0.72
        config.TOKEN = ''
        config.ENABLE_AUTH = False
        config.TRUST_X_FORWARDED = False
        config.TAVILY_API_KEY = ''
        config.FEATURE_V2_MULTI_USER_API = False
        config.FEATURE_MOBILE_REMOTE_DEVELOPMENT = False
        config.FEATURE_GEMINI_SUPPORT = False
        config.FEATURE_ANDROID_ISSUE_ANALYSIS = False
        config.FEATURE_ANDROID_ISSUE_ANALYSIS_EXPERT_WORKBENCH = False
        config.ANDROID_ANALYSIS_PROJECT_KNOWLEDGE_RELATIVE_PATH = '.claude-web/android-analysis'
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

    def make_fake_claude_cli_success_then_nonzero(self):
        fake_dir = Path(self.tmp.name) / 'fake-claude'
        fake_dir.mkdir()
        script = fake_dir / 'fake_claude.py'
        script.write_text(
            '\n'.join(
                [
                    'import json, sys',
                    'sys.stdin.read()',
                    'sid = "fake-claude-session"',
                    'print(json.dumps({"type": "system", "session_id": sid}), flush=True)',
                    'print(json.dumps({"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text"}}}), flush=True)',
                    'print(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "OK"}}}), flush=True)',
                    'print(json.dumps({"type": "stream_event", "event": {"type": "content_block_stop"}}), flush=True)',
                    'print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "result": "OK"}), flush=True)',
                    'sys.exit(1)',
                ]
            ),
            encoding='utf-8',
        )
        if os.name == 'nt':
            launcher = fake_dir / 'fake_claude.cmd'
            launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding='utf-8')
        else:
            launcher = fake_dir / 'fake_claude'
            launcher.write_text(f'#!{sys.executable}\nimport runpy\nrunpy.run_path({str(script)!r}, run_name="__main__")\n', encoding='utf-8')
            launcher.chmod(0o755)
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
        self.assertIn('android_issue_analysis', data)
        self.assertIn('android_issue_analysis_expert_workbench', data)
        self.assertIn('android_analysis_debug_trace', data)
        self.assertFalse(data['tavily_search_configured'])
        self.assertFalse(data['mobile_remote_development'])
        self.assertFalse(data['gemini_support'])
        self.assertFalse(data['gemini_configured'])
        self.assertFalse(data['android_issue_analysis'])
        self.assertFalse(data['android_issue_analysis_expert_workbench'])

    def test_claude_success_result_suppresses_late_nonzero_exit(self):
        from claude_web import claude_runner

        old_cli_path = config.CLAUDE_CLI_PATH
        config.CLAUDE_CLI_PATH = str(self.make_fake_claude_cli_success_then_nonzero())
        workspace = Path(self.tmp.name) / 'session-workspace'
        workspace.mkdir()
        try:
            events = ''.join(
                claude_runner.stream_claude_output(
                    'hello',
                    session_id='fake-session',
                    session_workspace_dir=workspace,
                    readonly_dirs=[],
                )
            )
        finally:
            config.CLAUDE_CLI_PATH = old_cli_path

        self.assertIn('"type": "done"', events)
        self.assertIn('"content": "OK"', events)
        self.assertNotIn('"type": "error"', events)

    def test_orchestrator_rebuilds_expired_claude_session_without_user_visible_error(self):
        from claude_web import orchestrator

        calls = []
        workspace = Path(self.tmp.name) / 'expired-session-workspace'
        workspace.mkdir()

        def fake_runner(message, claude_session_id=None, file_paths=None, **kwargs):
            calls.append(
                {
                    'message': message,
                    'claude_session_id': claude_session_id,
                    'file_paths': file_paths,
                    'history': kwargs.get('conversation_history'),
                }
            )
            if claude_session_id == 'expired-session':
                yield 'data: ' + json.dumps(
                    {
                        'type': 'error',
                        'message': '上游返回失败（无 result 详情）',
                        'soft': True,
                        'recoverable_session_rebuild': True,
                    },
                    ensure_ascii=False,
                ) + '\n\n'
                yield 'data: ' + json.dumps(
                    {
                        'type': 'done',
                        'ok': False,
                        'recoverable_session_rebuild': True,
                    },
                    ensure_ascii=False,
                ) + '\n\n'
                return
            yield 'data: {"type":"session","session_id":"new-session"}\n\n'
            yield 'data: {"type":"text","content":"rebuilt ok"}\n\n'
            yield 'data: {"type":"done","ok":true}\n\n'

        events = ''.join(
            orchestrator.stream_orchestrated_turns(
                first_message='继续分析 RDM 锁机',
                file_paths=['uploads/log.txt'],
                session_id='web-session',
                initial_claude_session_id='expired-session',
                max_rounds=3,
                upload_dir=str(workspace / 'uploads'),
                session_workspace_dir=str(workspace),
                readonly_dirs=[],
                cli_log_context={},
                conversation_history=[{'role': 'assistant', 'content': '之前讨论过 RDM 锁机流程'}],
                stream_output_func=fake_runner,
            )
        )

        self.assertEqual([c['claude_session_id'] for c in calls], ['expired-session', None])
        self.assertEqual(calls[1]['message'], '继续分析 RDM 锁机')
        self.assertEqual(calls[1]['file_paths'], ['uploads/log.txt'])
        self.assertEqual(calls[1]['history'][0]['content'], '之前讨论过 RDM 锁机流程')
        self.assertNotIn('上游返回失败', events)
        self.assertNotIn('"type": "error"', events)
        self.assertIn('new-session', events)
        self.assertIn('rebuilt ok', events)

    def test_dev_api_is_hidden_when_feature_disabled(self):
        resp = self.client.get('/api/dev/projects')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()['code'], 'dev_disabled')

    def test_android_analysis_api_is_hidden_when_feature_disabled(self):
        resp = self.client.get('/api/android-analysis/status')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()['code'], 'android_analysis_disabled')

    def test_android_expert_knowledge_cache_is_loaded_at_startup_and_listed(self):
        config.FEATURE_ANDROID_ISSUE_ANALYSIS = True
        config.FEATURE_ANDROID_ISSUE_ANALYSIS_EXPERT_WORKBENCH = True
        project = Path(self.tmp.name) / 'rdm-project'
        knowledge = project / '.claude-web' / 'android-analysis'
        (knowledge / 'cases').mkdir(parents=True)
        (knowledge / 'module.json').write_text(
            json.dumps(
                {
                    'id': 'android-rdm',
                    'title': 'RealtimeDeviceManager',
                    'description': 'RDM lock and activation flows.',
                    'default_package_names': ['com.hihonor.realtimedevicemanager'],
                    'profiles': ['functional'],
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
        (knowledge / 'subcategories.json').write_text(
            json.dumps([{'id': 'activation_eula', 'title': 'Activation EULA', 'description': 'EULA flow'}]),
            encoding='utf-8',
        )
        (knowledge / 'evidence_templates.jsonl').write_text(
            json.dumps(
                {
                    'id': 'rdm-checkin-eula-missing',
                    'module_id': 'android-rdm',
                    'subcategory_id': 'activation_eula',
                    'profile': 'functional',
                    'log_type': 'logcat',
                    'regex': '\\bDeviceLockSchedulerImpl\\b.*\\bhas eula:false\\b',
                    'meaning': 'Check-in result does not contain EULA configuration.',
                    'severity': 'critical',
                },
                ensure_ascii=False,
            )
            + '\n',
            encoding='utf-8',
        )
        config.PATHS_CONFIG_FILE.write_text(
            json.dumps(
                {'bundles': [{'id': 'android-rdm', 'title': 'RDM', 'paths': [str(project)]}]},
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )

        app = create_app()
        app.config['TESTING'] = True
        client = app.test_client()

        status = client.get('/api/android-analysis/status')
        self.assertEqual(status.status_code, 200, status.get_data(as_text=True))
        status_data = status.get_json()
        self.assertTrue(status_data['expert_workbench'])
        self.assertEqual(status_data['expert_knowledge']['module_count'], 1)
        self.assertEqual(status_data['expert_knowledge']['modules'][0]['id'], 'android-rdm')

        detail = client.get('/api/android-analysis/expert-knowledge?details=1')
        self.assertEqual(detail.status_code, 200, detail.get_data(as_text=True))
        payload = detail.get_json()
        self.assertTrue(payload['enabled'])
        self.assertEqual(payload['knowledge']['module_count'], 1)
        self.assertEqual(payload['knowledge']['details'][0]['evidence_templates'][0]['id'], 'rdm-checkin-eula-missing')
        self.assertIn('regex', payload['knowledge']['details'][0]['evidence_templates'][0])
        self.assertIn('experience_logs', payload['knowledge']['details'][0])

    def test_android_expert_knowledge_scaffold_api_writes_project_pack_and_refreshes_cache(self):
        config.FEATURE_ANDROID_ISSUE_ANALYSIS = True
        config.FEATURE_ANDROID_ISSUE_ANALYSIS_EXPERT_WORKBENCH = True
        project = Path(self.tmp.name) / 'empty-rdm-project'
        project.mkdir()
        source_dir = project / 'app' / 'src' / 'main' / 'java' / 'com' / 'example'
        source_dir.mkdir(parents=True)
        (source_dir / 'DeviceLockSchedulerImpl.java').write_text(
            '\n'.join(
                [
                    'package com.example;',
                    'import android.util.Log;',
                    'class DeviceLockSchedulerImpl {',
                    '  void processCheckInResult() {',
                    '    Log.d("DeviceLockSchedulerImpl", "has eula:false");',
                    '  }',
                    '}',
                ]
            )
            + '\n',
            encoding='utf-8',
        )
        config.PATHS_CONFIG_FILE.write_text(
            json.dumps(
                {'bundles': [{'id': 'android-rdm', 'title': 'RDM', 'paths': [str(project)]}]},
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
        app = create_app()
        app.config['TESTING'] = True
        client = app.test_client()

        scaffold = client.post(
            '/api/android-analysis/expert-knowledge/scaffold',
            json={
                'bundle_id': 'android-rdm',
                'module': {
                    'description': 'RDM lock and activation flows.',
                    'default_package_names': ['com.hihonor.realtimedevicemanager'],
                    'source_roots': ['app/src/main'],
                },
                'subcategories': [
                    {'id': 'activation_eula', 'title': 'Activation EULA', 'description': 'EULA flow'}
                ],
                'evidence_templates': [
                    {
                        'id': 'rdm-checkin-eula-missing',
                        'subcategory_id': 'activation_eula',
                        'profile': 'functional',
                        'log_type': 'logcat',
                        'regex': '\\bDeviceLockSchedulerImpl\\b.*\\bhas eula:false\\b',
                        'meaning': 'Check-in result does not contain EULA configuration.',
                    }
                ],
                'overwrite': True,
            },
        )
        self.assertEqual(scaffold.status_code, 200, scaffold.get_data(as_text=True))
        body = scaffold.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['knowledge']['module_count'], 1)
        self.assertTrue((project / '.claude-web' / 'android-analysis' / 'module.json').is_file())
        self.assertTrue((project / 'skills' / 'android-rdm-analysis' / 'SKILL.md').is_file())

        convert = client.post(
            '/api/android-analysis/expert-knowledge/evidence-templates/convert',
            json={'bundle_id': 'android-rdm', 'direction': 'jsonl_to_xlsx', 'overwrite': True},
        )
        self.assertEqual(convert.status_code, 200, convert.get_data(as_text=True))
        self.assertTrue((project / '.claude-web' / 'android-analysis' / 'evidence_templates.xlsx').is_file())
        generated = client.post(
            '/api/android-analysis/expert-knowledge/evidence-templates/generate',
            json={
                'bundle_id': 'android-rdm',
                'subcategory_id': 'activation_eula',
                'mode': 'prefiltered',
                'keyword_hints': ['eula'],
                'max_candidates': 10,
                'dry_run': True,
            },
        )
        self.assertEqual(generated.status_code, 200, generated.get_data(as_text=True))
        generated_body = generated.get_json()
        self.assertTrue(generated_body['ok'])
        self.assertEqual(generated_body['result']['candidate_count'], 1)
        self.assertTrue(Path(generated_body['result']['candidate_paths']['jsonl']).is_file())
        self.assertIn('expert_workbench', generated_body['result']['output_dir'])
        detail = client.get('/api/android-analysis/expert-knowledge?details=1')
        self.assertEqual(detail.get_json()['knowledge']['details'][0]['subcategories'][0]['id'], 'activation_eula')

    def test_android_analysis_phase_one_job_profiles_uploaded_archive(self):
        config.FEATURE_ANDROID_ISSUE_ANALYSIS = True
        bundle_dir = config.ANDROID_ANALYSIS_KNOWLEDGE_DIR / 'bundles' / 'android-rdm'
        bundle_dir.mkdir(parents=True)
        (bundle_dir / 'bundle.json').write_text(
            json.dumps({'id': 'android-rdm', 'title': 'RDM', 'source_path': 'D:/AndroidCode/RealtimeDeviceManager'}),
            encoding='utf-8',
        )
        code_dir = Path(self.tmp.name) / 'rdm-code'
        code_dir.mkdir()
        (code_dir / 'LockActivity.java').write_text('class LockActivity { void lock() {} }\n', encoding='utf-8')
        self.app.config['CLAUDE_WEB_PATHS_BUNDLES'] = [
            {'id': 'android-rdm', 'title': 'RDM', 'paths': [str(code_dir)]}
        ]
        session = self.create_session()
        upload_dir = config.CACHE_DIR / '127_0_0_1' / self.user_id / session['id'] / 'uploads'
        archive = upload_dir / 'logs.zip'
        with zipfile.ZipFile(archive, 'w') as zf:
            zf.writestr('logcat/main.log', '05-07 10:00:00 FATAL EXCEPTION: main\nRDM LockActivity lock failed\n')

        status = self.client.get('/api/android-analysis/status')
        self.assertEqual(status.status_code, 200, status.get_data(as_text=True))
        self.assertEqual(status.get_json()['bundles'][0]['id'], 'android-rdm')
        self.assertTrue(status.get_json()['debug_trace'])
        self.assertNotIn('source_path', status.get_json()['bundles'][0])

        created = self.client.post(
            '/api/android-analysis/jobs',
            json={
                'user_id': self.user_id,
                'session_id': session['id'],
                'question': 'lock flow crash',
                'source_filename': 'logs.zip',
                'bundle_ids': ['android-rdm'],
            },
        )
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        job = created.get_json()['job']
        self.assertEqual(job['status'], 'report_ready')
        self.assertGreaterEqual(job['first_pass_confidence'], config.ANDROID_ANALYSIS_AUTO_DEEP_CONFIDENCE_THRESHOLD)
        self.assertTrue(job['deep_available'])
        artifacts = config.CACHE_DIR / '127_0_0_1' / self.user_id / session['id'] / 'android_analysis' / job['id'] / 'artifacts'
        self.assertTrue((artifacts / 'file_manifest.json').is_file())
        self.assertTrue((artifacts / 'file_tree.json').is_file())
        self.assertTrue((artifacts / 'file_samples.json').is_file())
        self.assertTrue((artifacts / 'planner_result.json').is_file())
        self.assertTrue((artifacts / 'matched_rules.json').is_file())
        self.assertTrue((artifacts / 'first_evidence_pack.md').is_file())
        self.assertTrue((artifacts / 'case_cards.json').is_file())
        self.assertTrue((artifacts / 'final_report.md').is_file())
        self.assertTrue((artifacts / 'verifier_result.json').is_file())
        self.assertTrue((artifacts / 'verified_report.md').is_file())
        self.assertTrue((artifacts / 'analysis_metrics.json').is_file())
        self.assertTrue((artifacts / 'android_debug_trace.jsonl').is_file())
        trace_text = (artifacts / 'android_debug_trace.jsonl').read_text(encoding='utf-8')
        self.assertIn('sampling_result', trace_text)
        self.assertIn('matching_result', trace_text)
        self.assertIn('case_recall_result', trace_text)
        metrics = json.loads((artifacts / 'analysis_metrics.json').read_text(encoding='utf-8'))
        self.assertGreaterEqual(len(metrics['stage_timings']), 1)
        self.assertIn('first_report_input_chars_estimate', metrics['prompt_chars'])
        self.assertIn('ai_token_usage', metrics)
        self.assertIn('totals', metrics['ai_token_usage'])
        self.assertIn('planner', metrics)
        self.assertIn('planner_component_chars', metrics['planner'])

        loaded_job = self.client.get(
            f'/api/android-analysis/jobs/{job["id"]}?user_id={self.user_id}&session_id={session["id"]}'
        )
        self.assertEqual(loaded_job.status_code, 200)
        self.assertEqual(loaded_job.get_json()['job']['status'], 'report_ready')

        latest_job = self.client.get(
            f'/api/android-analysis/jobs/latest?user_id={self.user_id}&session_id={session["id"]}'
        )
        self.assertEqual(latest_job.status_code, 200)
        self.assertEqual(latest_job.get_json()['job']['id'], job['id'])

        messages = self.client.get(f'/sessions/{session["id"]}/messages?user_id={self.user_id}')
        self.assertEqual(messages.status_code, 200)
        assistant = [m for m in messages.get_json() if m['role'] == 'assistant'][-1]
        self.assertEqual(assistant['metadata']['android_analysis_job_id'], job['id'])

        events = self.client.get(
            f'/api/android-analysis/jobs/{job["id"]}/events?user_id={self.user_id}&session_id={session["id"]}'
        )
        self.assertEqual(events.status_code, 200)
        event_types = [e['type'] for e in events.get_json()['events']]
        self.assertIn('first_report_generated', event_types)
        self.assertIn('verifier_completed', event_types)
        self.assertIn('first_pass_confidence', event_types)
        self.assertIn('analysis_metrics_recorded', event_types)

        details = self.client.get(
            f'/api/android-analysis/jobs/{job["id"]}/process-details?user_id={self.user_id}&session_id={session["id"]}'
        )
        self.assertEqual(details.status_code, 200, details.get_data(as_text=True))
        detail_data = details.get_json()['details']
        self.assertEqual(detail_data['process_overview_title'], 'Android RDM问题分析过程概览')
        self.assertEqual(detail_data['process_detail_title'], 'Android RDM问题分析过程详情')
        stages = detail_data['stages']
        stage_ids = [s['id'] for s in stages]
        self.assertIn('profiling', stage_ids)
        self.assertIn('sampling', stage_ids)
        self.assertTrue(any(s.get('items') for s in stages))

        report = self.client.get(
            f'/api/android-analysis/jobs/{job["id"]}/artifacts/final_report.md?user_id={self.user_id}&session_id={session["id"]}'
        )
        self.assertEqual(report.status_code, 200)
        report_text = report.get_data(as_text=True)
        report.close()
        self.assertIn('Android 问题首轮分析报告', report_text)

        deep = self.client.post(
            f'/api/android-analysis/jobs/{job["id"]}/deep',
            json={'user_id': self.user_id, 'session_id': session['id']},
        )
        self.assertEqual(deep.status_code, 200, deep.get_data(as_text=True))
        self.assertTrue(deep.get_json()['deep']['has_code_context'])
        deep_job = deep.get_json()['job']
        self.assertIn('deep_report', deep_job['artifacts'])
        self.assertIn('verifier_result', deep_job['artifacts'])
        self.assertIn('analysis_metrics', deep_job['artifacts'])

        draft = self.client.post(
            f'/api/android-analysis/jobs/{job["id"]}/case-draft',
            json={'user_id': self.user_id, 'session_id': session['id']},
        )
        self.assertEqual(draft.status_code, 200, draft.get_data(as_text=True))
        self.assertEqual(draft.get_json()['draft']['status'], 'draft')

        confirmed = self.client.post(
            f'/api/android-analysis/jobs/{job["id"]}/case-draft/confirm',
            json={'user_id': self.user_id, 'session_id': session['id'], 'bundle_id': 'android-rdm'},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.get_data(as_text=True))
        case_path = config.ANDROID_ANALYSIS_KNOWLEDGE_DIR / confirmed.get_json()['confirmed']['case_path']
        self.assertTrue(case_path.is_file())

    def test_android_analysis_background_job_streams_events(self):
        config.FEATURE_ANDROID_ISSUE_ANALYSIS = True
        bundle_dir = config.ANDROID_ANALYSIS_KNOWLEDGE_DIR / 'bundles' / 'android-rdm'
        bundle_dir.mkdir(parents=True)
        (bundle_dir / 'bundle.json').write_text(
            json.dumps({'id': 'android-rdm', 'title': 'RDM'}),
            encoding='utf-8',
        )
        session = self.create_session()
        upload_dir = config.CACHE_DIR / '127_0_0_1' / self.user_id / session['id'] / 'uploads'
        archive = upload_dir / 'logs.zip'
        with zipfile.ZipFile(archive, 'w') as zf:
            zf.writestr('logcat/main.log', 'RDM LockActivity lock failed\n')

        created = self.client.post(
            '/api/android-analysis/jobs',
            json={
                'user_id': self.user_id,
                'session_id': session['id'],
                'question': 'RDM lock failed',
                'source_filename': 'logs.zip',
                'bundle_ids': ['android-rdm'],
                'background': True,
            },
        )
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        job = created.get_json()['job']
        self.assertEqual(job['status'], 'queued')
        sessions = self.client.get(f'/sessions?user_id={self.user_id}').get_json()
        self.assertIn('RDM lock failed', sessions[0]['title'])

        stream = self.client.get(
            f'/api/android-analysis/jobs/{job["id"]}/events/stream?user_id={self.user_id}&session_id={session["id"]}',
            buffered=True,
        )
        self.assertEqual(stream.status_code, 200, stream.get_data(as_text=True))
        body = stream.get_data(as_text=True)
        self.assertIn('planner_completed', body)
        self.assertIn('verifier_completed', body)
        self.assertIn('analysis_metrics_recorded', body)

        loaded = self.client.get(
            f'/api/android-analysis/jobs/{job["id"]}?user_id={self.user_id}&session_id={session["id"]}'
        )
        self.assertIn(loaded.get_json()['job']['status'], {'report_ready', 'needs_review'})

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

    def test_session_title_is_derived_from_first_user_message(self):
        session = self.create_session()
        session_id = session['id']
        manager = SessionManager(config.CACHE_DIR)

        manager.add_message(
            '127.0.0.1',
            self.user_id,
            session_id,
            'user',
            '帮我看一下这个rdm锁机问题，可能是没有锁定或者解锁失败？',
        )

        list_resp = self.client.get(f'/sessions?user_id={self.user_id}')
        self.assertEqual(list_resp.status_code, 200)
        title = list_resp.get_json()[0]['title']
        self.assertNotEqual(title, DEFAULT_SESSION_TITLE)
        self.assertIn('rdm锁机问题', title.lower())
        self.assertNotIn('帮我', title)
        self.assertNotIn('看一下', title)

    def test_session_title_derivation_skips_android_file_metadata(self):
        title = derive_session_title_from_message(
            'Android 问题分析\n\n文件：PNM-N49.zip\n\n帮我看一下这个RDM锁机问题，可能是没有锁定或者解锁失败？'
        )
        self.assertIn('RDM锁机问题', title)

    def test_session_title_can_skip_initial_greeting(self):
        session = self.create_session()
        manager = SessionManager(config.CACHE_DIR)
        manager.add_message('127.0.0.1', self.user_id, session['id'], 'user', '你好')
        manager.add_message('127.0.0.1', self.user_id, session['id'], 'user', '帮我分析一下RDM锁机失败')

        title = self.client.get(f'/sessions?user_id={self.user_id}').get_json()[0]['title']
        self.assertIn('RDM锁机失败', title)

    def test_delete_session_backup_ignores_volatile_claude_debug_dir(self):
        session = self.create_session()
        session_id = session['id']
        session_dir = config.CACHE_DIR / '127_0_0_1' / self.user_id / session_id
        debug_dir = session_dir / '.claude_web_home' / '.claude' / 'debug'
        debug_dir.mkdir(parents=True)
        (debug_dir / 'latest').write_text('volatile debug pointer', encoding='utf-8')

        resp = self.client.delete(f'/sessions/{session_id}?user_id={self.user_id}')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertEqual(data, {'ok': True})
        backup_dirs = sorted(config.BACKUPS_DIR.glob('*/*'))
        self.assertEqual(len(backup_dirs), 1)
        backup_dir = backup_dirs[0]
        self.assertTrue((backup_dir / 'session_snapshot' / 'messages.json').is_file())
        self.assertFalse((backup_dir / 'session_snapshot' / '.claude_web_home' / '.claude' / 'debug' / 'latest').exists())

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

        session_agent_path = config.CACHE_DIR / '127_0_0_1' / self.user_id / session['id'] / USER_GLOBAL_MEMORY_FILENAME
        user_agent_path = config.CACHE_DIR / '127_0_0_1' / self.user_id / USER_GLOBAL_MEMORY_FILENAME
        session_agent = session_agent_path.read_text(encoding='utf-8')
        user_agent = user_agent_path.read_text(encoding='utf-8')
        self.assertIn('- 用户姓名：王亚宁', session_agent)
        self.assertIn('- 偏好称呼：宁哥', session_agent)
        self.assertIn('- 用户姓名：王亚宁', user_agent)
        self.assertIn('- 偏好称呼：宁哥', user_agent)

    def test_user_global_agent_memory_is_shared_between_sessions(self):
        manager = SessionManager(config.CACHE_DIR)
        first = self.create_session()
        first_agent_path = config.CACHE_DIR / '127_0_0_1' / self.user_id / first['id'] / USER_GLOBAL_MEMORY_FILENAME
        first_agent_path.write_text(
            first_agent_path.read_text(encoding='utf-8') + '\n- 偏好称呼：宁哥\n',
            encoding='utf-8',
        )
        manager.sync_session_global_memory_to_user('127.0.0.1', self.user_id, first['id'])

        second = self.create_session()
        second_agent_path = config.CACHE_DIR / '127_0_0_1' / self.user_id / second['id'] / USER_GLOBAL_MEMORY_FILENAME
        self.assertIn('偏好称呼：宁哥', second_agent_path.read_text(encoding='utf-8'))

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
