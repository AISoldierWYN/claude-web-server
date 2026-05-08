import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / 'skills' / 'android-log-rule-builder' / 'scripts' / 'rule_pack_manager.py'


class AndroidLogRuleBuilderSkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / 'RealtimeDeviceManager'
        self.project.mkdir()
        (self.project / 'app' / 'src' / 'main').mkdir(parents=True)
        (self.project / 'app' / 'src' / 'main' / 'AndroidManifest.xml').write_text(
            '<manifest package="com.example.rdm"><application>'
            '<service android:name=".LockService" />'
            '<receiver android:name=".PolicyReceiver" />'
            '</application></manifest>',
            encoding='utf-8',
        )
        (self.project / 'app' / 'src' / 'main' / 'LockManager.java').write_text(
            'package com.example.rdm;\n'
            'import android.util.Log;\n'
            'class LockManager {\n'
            '  private static final String TAG = "RDM-Lock";\n'
            '  static final String ERR_LOCK_FAILED = "ERR_LOCK_FAILED";\n'
            '  void lockDevice() { Log.e(TAG, "RDM lock failed DeviceLock policy denied"); }\n'
            '}\n',
            encoding='utf-8',
        )
        self.paths_config = self.root / 'claude_web_paths.config.json'
        self.paths_config.write_text(
            json.dumps(
                {
                    'version': 2,
                    'bundles': [
                        {
                            'id': 'android-rdm',
                            'title': 'Android RDM',
                            'summary': '锁机 解锁 provision device policy',
                            'keywords': ['rdm', '锁机', 'lock', 'unlock', 'provision', 'device policy'],
                            'paths': [str(self.project)],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
        self.knowledge = self.root / 'android_analysis_knowledge'

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        cmd = [
            sys.executable,
            str(SCRIPT),
            '--json',
            '--paths-config',
            str(self.paths_config),
            '--knowledge-dir',
            str(self.knowledge),
            *args,
        ]
        proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True, timeout=30)
        if proc.returncode != 0:
            self.fail(f'command failed: {cmd}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}')
        return json.loads(proc.stdout)

    def test_help_lists_supported_commands(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), '--help'],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('generate', proc.stdout)
        self.assertIn('validate', proc.stdout)
        self.assertIn('delete', proc.stdout)

    def test_generate_validate_crud_and_test_rule_pack(self):
        generated = self.run_cli(
            'generate',
            '--bundle-id',
            'android-rdm',
            '--rule-pack-id',
            'rdm-generated',
        )
        pack = generated['rule_pack']
        self.assertEqual(pack['id'], 'rdm-generated')
        self.assertGreaterEqual(len(pack['rules']), 3)
        self.assertTrue((self.knowledge / 'bundles' / 'android-rdm' / 'rules' / 'rdm-generated.json').is_file())

        validated = self.run_cli('validate', '--bundle-id', 'android-rdm', '--rule-pack-id', 'rdm-generated')
        self.assertTrue(validated['ok'])
        self.assertEqual(validated['errors'], [])

        listed = self.run_cli('list', '--bundle-id', 'android-rdm')
        self.assertEqual(listed['packs'][0]['id'], 'rdm-generated')

        got_pack = self.run_cli('get', '--bundle-id', 'android-rdm', '--rule-pack-id', 'rdm-generated')
        first_rule_id = got_pack['rules'][0]['id']
        got_rule = self.run_cli(
            'get',
            '--bundle-id',
            'android-rdm',
            '--rule-pack-id',
            'rdm-generated',
            '--rule-id',
            first_rule_id,
        )
        self.assertEqual(got_rule['id'], first_rule_id)

        custom_rule = {
            'id': 'rdm-custom-state',
            'title': 'RDM custom state',
            'issue_type': 'android_business_spec',
            'severity': 'medium',
            'source_bundle_ids': ['android-rdm'],
            'tags': ['custom'],
            'match': {'keywords': ['CustomRdmState']},
        }
        added = self.run_cli(
            'add',
            '--bundle-id',
            'android-rdm',
            '--rule-pack-id',
            'rdm-generated',
            '--rule-json',
            json.dumps(custom_rule, ensure_ascii=False),
        )
        self.assertEqual(added['rule']['id'], 'rdm-custom-state')

        custom_rule['severity'] = 'high'
        updated = self.run_cli(
            'update',
            '--bundle-id',
            'android-rdm',
            '--rule-pack-id',
            'rdm-generated',
            '--rule-json',
            json.dumps(custom_rule, ensure_ascii=False),
        )
        self.assertEqual(updated['rule']['severity'], 'high')

        log_file = self.root / 'rdm.log'
        log_file.write_text('05-08 RDM lock failed DeviceLock policy denied CustomRdmState\n', encoding='utf-8')
        tested = self.run_cli(
            'test',
            '--bundle-id',
            'android-rdm',
            '--rule-pack-id',
            'rdm-generated',
            '--log-path',
            str(log_file),
            '--require-hit',
        )
        self.assertGreater(tested['hit_count'], 0)

        deleted = self.run_cli(
            'delete',
            '--bundle-id',
            'android-rdm',
            '--rule-pack-id',
            'rdm-generated',
            '--rule-id',
            'rdm-custom-state',
        )
        self.assertEqual(deleted['deleted_rule'], 'rdm-custom-state')


if __name__ == '__main__':
    unittest.main()
