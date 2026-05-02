import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

from claude_web import config
from claude_web import settings_loader
from claude_web.gemini_runner import stream_gemini_output


def parse_sse_events(chunks):
    events = []
    for chunk in chunks:
        if not chunk.startswith('data: '):
            continue
        events.append(json.loads(chunk[6:].strip()))
    return events


class GeminiRunnerOfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._logger = logging.getLogger('claude-web')
        self._old_handlers = list(self._logger.handlers)
        self._old_propagate = self._logger.propagate
        self._logger.handlers = [logging.NullHandler()]
        self._logger.propagate = False
        self.session_dir = self.root / 'session'
        self.session_dir.mkdir()
        self.readonly_dir = self.root / 'readonly'
        self.readonly_dir.mkdir()
        self.upload_dir = self.session_dir / 'uploads'
        self.upload_dir.mkdir()
        self.attachment = self.upload_dir / 'note.txt'
        self.attachment.write_text('attached text body', encoding='utf-8')

        self._old = {
            'GEMINI_CLI_PATH': config.GEMINI_CLI_PATH,
            'GEMINI_MODEL': config.GEMINI_MODEL,
            'GEMINI_APPROVAL_MODE': config.GEMINI_APPROVAL_MODE,
            'GEMINI_SANDBOX': config.GEMINI_SANDBOX,
            'GEMINI_SKIP_TRUST': config.GEMINI_SKIP_TRUST,
            'GEMINI_PROXY': config.GEMINI_PROXY,
            'GEMINI_REQUEST_TIMEOUT_SECONDS': config.GEMINI_REQUEST_TIMEOUT_SECONDS,
        }
        config.GEMINI_MODEL = ''
        config.GEMINI_APPROVAL_MODE = 'plan'
        config.GEMINI_SANDBOX = False
        config.GEMINI_SKIP_TRUST = True
        config.GEMINI_PROXY = ''
        config.GEMINI_REQUEST_TIMEOUT_SECONDS = 5

    def tearDown(self):
        for k, v in self._old.items():
            setattr(config, k, v)
        self._logger.handlers = self._old_handlers
        self._logger.propagate = self._old_propagate
        self.tmp.cleanup()

    def make_fake_cli(self, body_lines):
        fake_dir = self.root / ('fake-' + str(len(list(self.root.glob('fake-*')))))
        fake_dir.mkdir()
        script = fake_dir / 'fake_gemini.py'
        script.write_text(
            '\n'.join(
                [
                    'import json, os, pathlib, sys',
                    'args = sys.argv[1:]',
                    'pathlib.Path(os.environ["FAKE_GEMINI_ARGS_FILE"]).write_text(json.dumps(args), encoding="utf-8")',
                    'pathlib.Path(os.environ["FAKE_GEMINI_PROMPT_FILE"]).write_text(sys.stdin.read(), encoding="utf-8")',
                    'pathlib.Path(os.environ["FAKE_GEMINI_ENV_FILE"]).write_text(json.dumps({',
                    '    "HTTP_PROXY": os.environ.get("HTTP_PROXY", ""),',
                    '    "HTTPS_PROXY": os.environ.get("HTTPS_PROXY", ""),',
                    '    "EXTRA_FLAG": os.environ.get("EXTRA_FLAG", ""),',
                    '}), encoding="utf-8")',
                    *body_lines,
                ]
            ),
            encoding='utf-8',
        )
        if os.name == 'nt':
            launcher = fake_dir / 'fake_gemini.cmd'
            launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding='utf-8')
        else:
            launcher = fake_dir / 'fake_gemini'
            launcher.write_text(
                f'#!{sys.executable}\nimport runpy\nrunpy.run_path({str(script)!r}, run_name="__main__")\n',
                encoding='utf-8',
            )
            launcher.chmod(0o755)
        os.environ['FAKE_GEMINI_ARGS_FILE'] = str(fake_dir / 'args.json')
        os.environ['FAKE_GEMINI_PROMPT_FILE'] = str(fake_dir / 'prompt.txt')
        os.environ['FAKE_GEMINI_ENV_FILE'] = str(fake_dir / 'env.json')
        return launcher

    def run_stream(self, **overrides):
        kwargs = dict(
            message='hello 中文',
            session_id='web-session',
            claude_session_id=None,
            upload_dir=str(self.upload_dir),
            file_paths=[str(self.attachment)],
            session_workspace_dir=str(self.session_dir),
            readonly_dirs=[str(self.readonly_dir)],
            readonly_dirs_notes='readonly note',
            skill_bundles=[
                {
                    'id': 'demo',
                    'title': 'Demo',
                    'summary': 'Demo bundle',
                    'mounted': True,
                    'paths': [str(self.readonly_dir)],
                }
            ],
            conversation_history=[{'role': 'user', 'content': 'old question'}],
            web_search_context='【联网搜索资料】\nsource text',
            child_env_extra={'EXTRA_FLAG': 'from-test'},
            model_override='gemini-test-model',
        )
        kwargs.update(overrides)
        return list(stream_gemini_output(**kwargs))

    def test_success_stream_maps_jsonl_and_builds_command(self):
        config.GEMINI_PROXY = 'http://127.0.0.1:1080'
        config.GEMINI_CLI_PATH = str(
            self.make_fake_cli(
                [
                    'print(json.dumps({"type": "init", "session_id": "sid-1", "model": "fake"}), flush=True)',
                    'print(json.dumps({"type": "tool_use", "tool_name": "read_file", "tool_id": "t1", "parameters": {"path": "uploads/note.txt"}}), flush=True)',
                    'print(json.dumps({"type": "tool_result", "tool_id": "t1", "status": "success"}), flush=True)',
                    'print(json.dumps({"type": "message", "role": "assistant", "content": "hello ", "delta": True}), flush=True)',
                    'print(json.dumps({"type": "message", "role": "assistant", "content": [{"text": "world"}]}), flush=True)',
                    'print(json.dumps({"type": "result", "status": "success", "stats": {"tokens": 1}}), flush=True)',
                ]
            )
        )

        events = parse_sse_events(self.run_stream())
        self.assertEqual(events[0]['type'], 'session')
        self.assertEqual(events[0]['session_id'], 'sid-1')
        self.assertEqual(events[0]['provider'], 'gemini')
        self.assertIn({'type': 'tool_start', 'name': 'read_file', 'id': 't1'}, events)
        self.assertTrue(any(e.get('type') == 'tool_input_delta' and 'uploads/note.txt' in e.get('partial', '') for e in events))
        self.assertTrue(any(e.get('type') == 'tool_stop' and e.get('status') == 'success' for e in events))
        self.assertEqual(''.join(e.get('content', '') for e in events if e.get('type') == 'text'), 'hello world')
        self.assertTrue(any(e.get('type') == 'done' and e.get('ok') is True for e in events))

        args = json.loads(Path(os.environ['FAKE_GEMINI_ARGS_FILE']).read_text(encoding='utf-8'))
        self.assertIn('--output-format', args)
        self.assertIn('stream-json', args)
        self.assertIn('--approval-mode', args)
        self.assertIn('plan', args)
        self.assertIn('--skip-trust', args)
        self.assertIn('--model', args)
        self.assertIn('gemini-test-model', args)
        self.assertIn('--include-directories', args)
        self.assertIn(str(self.session_dir.resolve()), args)
        self.assertNotIn('--resume', args)
        self.assertNotIn('latest', args)

        prompt = Path(os.environ['FAKE_GEMINI_PROMPT_FILE']).read_text(encoding='utf-8')
        self.assertIn('hello 中文', prompt)
        self.assertIn('attached text body', prompt)
        self.assertIn('old question', prompt)
        self.assertIn('source text', prompt)
        self.assertIn('Gemini CLI 模式', prompt)

        env = json.loads(Path(os.environ['FAKE_GEMINI_ENV_FILE']).read_text(encoding='utf-8'))
        self.assertEqual(env['HTTP_PROXY'], 'http://127.0.0.1:1080')
        self.assertEqual(env['HTTPS_PROXY'], 'http://127.0.0.1:1080')
        self.assertEqual(env['EXTRA_FLAG'], 'from-test')

    def test_resume_uses_explicit_session_id_only(self):
        config.GEMINI_CLI_PATH = str(
            self.make_fake_cli(
                [
                    'sid = args[args.index("--resume") + 1] if "--resume" in args else "missing"',
                    'print(json.dumps({"type": "init", "session_id": sid}), flush=True)',
                    'print(json.dumps({"type": "message", "role": "assistant", "content": "resumed"}), flush=True)',
                    'print(json.dumps({"type": "result", "status": "success"}), flush=True)',
                ]
            )
        )
        events = parse_sse_events(self.run_stream(claude_session_id='sid-existing', file_paths=None))
        self.assertEqual(events[0]['session_id'], 'sid-existing')
        args = json.loads(Path(os.environ['FAKE_GEMINI_ARGS_FILE']).read_text(encoding='utf-8'))
        self.assertIn('--resume', args)
        self.assertEqual(args[args.index('--resume') + 1], 'sid-existing')
        self.assertNotIn('latest', args)

    def test_memory_write_tool_is_mirrored_to_session_memory_only(self):
        memory_abs = str((self.session_dir / 'memory.md').resolve()).replace('\\', '/')
        memory_abs_content = '# Memory\n- name: Ning Ge'
        config.GEMINI_CLI_PATH = str(
            self.make_fake_cli(
                [
                    'print(json.dumps({"type": "init", "session_id": "sid-memory"}), flush=True)',
                    'print(json.dumps({"type": "tool_use", "tool_name": "write_file", "tool_id": "w1", "parameters": {"file_path": "memory.md", "content": "# Memory\\n- name: Ning"}}), flush=True)',
                    f'print(json.dumps({{"type": "tool_use", "tool_name": "write_file", "tool_id": "wabs", "parameters": {{"file_path": {memory_abs!r}, "content": {memory_abs_content!r}}}}}), flush=True)',
                    f'print(json.dumps({{"type": "tool_use", "tool_name": "replace", "tool_id": "r1", "parameters": {{"file_path": {memory_abs!r}, "old_string": "- name: Ning Ge", "new_string": "- name: 宁哥"}}}}), flush=True)',
                    'print(json.dumps({"type": "tool_use", "tool_name": "write_file", "tool_id": "w2", "parameters": {"file_path": "../memory.md", "content": "bad"}}), flush=True)',
                    'print(json.dumps({"type": "result", "status": "success"}), flush=True)',
                ]
            )
        )

        events = parse_sse_events(self.run_stream(file_paths=None))
        self.assertTrue(any(e.get('type') == 'done' and e.get('ok') is True for e in events))
        self.assertEqual((self.session_dir / 'memory.md').read_text(encoding='utf-8'), '# Memory\n- name: 宁哥')
        self.assertFalse((self.root / 'memory.md').exists())

    def test_memory_tool_params_can_use_arguments_key(self):
        memory_abs = str((self.session_dir / 'memory.md').resolve())
        config.GEMINI_CLI_PATH = str(
            self.make_fake_cli(
                [
                    'print(json.dumps({"type": "init", "session_id": "sid-memory-args"}), flush=True)',
                    f'print(json.dumps({{"type": "tool_use", "name": "write_file", "id": "wargs", "arguments": {{"file_path": {memory_abs!r}, "content": "args content"}}}}), flush=True)',
                    'print(json.dumps({"type": "result", "status": "success"}), flush=True)',
                ]
            )
        )

        events = parse_sse_events(self.run_stream(file_paths=None))
        self.assertTrue(any(e.get('type') == 'done' and e.get('ok') is True for e in events))
        self.assertEqual((self.session_dir / 'memory.md').read_text(encoding='utf-8'), 'args content')

    def test_chinese_user_text_filters_gemini_process_narration(self):
        config.GEMINI_CLI_PATH = str(
            self.make_fake_cli(
                [
                    'print(json.dumps({"type": "init", "session_id": "sid-clean"}), flush=True)',
                    'print(json.dumps({"type": "message", "role": "assistant", "content": "Providing Weather Information I am synthesizing the data. "}), flush=True)',
                    'print(json.dumps({"type": "message", "role": "assistant", "content": "[Thought: true]宁哥，今天西安多云。"}), flush=True)',
                    'print(json.dumps({"type": "result", "status": "success"}), flush=True)',
                ]
            )
        )

        events = parse_sse_events(self.run_stream(message='查一下今天西安天气', file_paths=None))
        text = ''.join(e.get('content', '') for e in events if e.get('type') == 'text')
        self.assertEqual(text, '宁哥，今天西安多云。')

    def test_latest_resume_is_rejected_without_starting_cli(self):
        config.GEMINI_CLI_PATH = str(self.root / 'does-not-matter')
        events = parse_sse_events(self.run_stream(claude_session_id='latest', file_paths=None))
        self.assertEqual(events[0]['type'], 'error')
        self.assertTrue(events[0]['fatal'])
        self.assertIn('latest', events[0]['message'])
        self.assertEqual(events[-1], {'type': 'done', 'ok': False})

    def test_result_error_is_fatal_and_friendly(self):
        config.GEMINI_CLI_PATH = str(
            self.make_fake_cli(
                [
                    'print(json.dumps({"type": "init", "session_id": "sid-error"}), flush=True)',
                    'print(json.dumps({"type": "result", "status": "error", "message": "429 quota exceeded"}), flush=True)',
                ]
            )
        )
        events = parse_sse_events(self.run_stream(file_paths=None))
        err = next(e for e in events if e.get('type') == 'error')
        self.assertTrue(err['soft'])
        self.assertTrue(err['fatal'])
        self.assertIn('额度', err['message'])
        self.assertTrue(any(e.get('type') == 'done' and e.get('ok') is False for e in events))

    def test_nonzero_exit_capacity_stderr_is_condensed_to_friendly_error(self):
        config.GEMINI_CLI_PATH = str(
            self.make_fake_cli(
                [
                    'print("MODEL_CAPACITY_EXHAUSTED: No capacity available for model", file=sys.stderr, flush=True)',
                    'sys.exit(1)',
                ]
            )
        )
        events = parse_sse_events(self.run_stream(file_paths=None))
        err = next(e for e in events if e.get('type') == 'error')
        self.assertTrue(err['fatal'])
        self.assertIn('容量', err['message'])
        self.assertTrue(any(e.get('type') == 'done' and e.get('ok') is False for e in events))

    def test_error_event_maps_auth_failure(self):
        config.GEMINI_CLI_PATH = str(
            self.make_fake_cli(
                [
                    'print(json.dumps({"type": "error", "message": "OAuth login required"}), flush=True)',
                ]
            )
        )
        events = parse_sse_events(self.run_stream(file_paths=None))
        err = next(e for e in events if e.get('type') == 'error')
        self.assertTrue(err['fatal'])
        self.assertIn('认证', err['message'])
        self.assertEqual(events[-1], {'type': 'done', 'ok': False})

    def test_development_mode_is_rejected_before_cli_starts(self):
        config.GEMINI_CLI_PATH = str(self.root / 'does-not-matter')
        events = parse_sse_events(
            self.run_stream(
                file_paths=None,
                development_context={'project_id': 'demo'},
            )
        )
        self.assertEqual(events[0]['type'], 'error')
        self.assertTrue(events[0]['fatal'])
        self.assertIn('开发模式', events[0]['message'])
        self.assertEqual(events[-1], {'type': 'done', 'ok': False})

    def test_gemini_auto_path_prefers_cmd_when_available(self):
        found = settings_loader.find_gemini_cli_auto()
        npm_gemini_cmd = Path(os.environ.get('APPDATA', '')).joinpath('npm', 'gemini.cmd')
        if os.name == 'nt' and npm_gemini_cmd.is_file():
            self.assertTrue(found.lower().endswith('gemini.cmd'), found)
        else:
            self.assertTrue(found)


if __name__ == '__main__':
    unittest.main(verbosity=2)
