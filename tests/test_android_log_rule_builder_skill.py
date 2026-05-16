import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / 'skills' / 'android-log-rule-builder' / 'scripts' / 'rule_pack_manager.py'
BOOTSTRAP_SCRIPT = REPO_ROOT / 'skills' / 'android-log-rule-builder' / 'scripts' / 'bootstrap_eval_repos.py'


class AndroidLogRuleBuilderSkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / 'RealtimeDeviceManager'
        self.project.mkdir()
        (self.project / 'app' / 'src' / 'main').mkdir(parents=True)
        (self.project / 'app' / 'src' / 'main' / 'AndroidManifest.xml').write_text(
            '<manifest package="com.example.rdm">'
            '<uses-permission android:name="android.permission.POST_NOTIFICATIONS" />'
            '<application>'
            '<activity android:name=".HomeActivity" />'
            '<service android:name=".LockService" />'
            '<receiver android:name=".PolicyReceiver" />'
            '<provider android:name=".RdmFileProvider" android:authorities="com.example.rdm.files" />'
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
        (self.project / 'app' / 'src' / 'main' / 'SyncWorker.kt').write_text(
            'package com.example.rdm\n'
            'import androidx.work.CoroutineWorker\n'
            'import retrofit2.Retrofit\n'
            'import androidx.room.RoomDatabase\n'
            'import android.app.NotificationManager\n'
            'class RdmSyncWorker : CoroutineWorker() {\n'
            '  fun sync() {\n'
            '    val retrofit = Retrofit.Builder()\n'
            '    val db: RoomDatabase? = null\n'
            '    val nm: NotificationManager? = null\n'
            '  }\n'
            '}\n',
            encoding='utf-8',
        )
        (self.project / 'settings.gradle').write_text("include ':app'\n", encoding='utf-8')
        (self.project / 'app' / 'build.gradle').write_text(
            'android { namespace "com.example.rdm"; defaultConfig { applicationId "com.example.rdm" } }\n',
            encoding='utf-8',
        )
        self.native_project = self.root / 'NativeSample'
        (self.native_project / 'app' / 'src' / 'main' / 'cpp').mkdir(parents=True)
        (self.native_project / 'app' / 'src' / 'main' / 'AndroidManifest.xml').write_text(
            '<manifest package="com.example.nativeapp">'
            '<application><activity android:name=".NativeActivity" /></application></manifest>',
            encoding='utf-8',
        )
        (self.native_project / 'app' / 'src' / 'main' / 'cpp' / 'CMakeLists.txt').write_text(
            'cmake_minimum_required(VERSION 3.22.1)\n'
            'project(NativeSample LANGUAGES C CXX)\n'
            'add_library(native-sample SHARED native-sample.cpp)\n'
            'target_link_libraries(native-sample android log)\n',
            encoding='utf-8',
        )
        (self.native_project / 'app' / 'src' / 'main' / 'cpp' / 'native-sample.cpp').write_text(
            '#include <jni.h>\n'
            '#include <android/log.h>\n'
            '#include <android/trace.h>\n'
            '#define LOG_TAG "NativeSample"\n'
            'void drawFrame() { ATrace_beginSection("NativeRenderFrame"); __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, "native render failed"); }\n'
            'extern "C" JNIEXPORT void JNICALL Java_com_example_nativeapp_NativeBridge_nativeCrash(JNIEnv*, jobject) { drawFrame(); }\n'
            'extern "C" JNIEXPORT jint JNI_OnLoad(JavaVM*, void*) { return JNI_VERSION_1_6; }\n',
            encoding='utf-8',
        )
        (self.native_project / 'settings.gradle').write_text("include ':app'\n", encoding='utf-8')
        (self.native_project / 'app' / 'build.gradle').write_text(
            'android { namespace "com.example.nativeapp"; defaultConfig { applicationId "com.example.nativeapp" } }\n',
            encoding='utf-8',
        )
        self.complex_project = self.root / 'ComplexApp'
        (self.complex_project / 'app' / 'src' / 'main' / 'java' / 'com' / 'example' / 'complex').mkdir(parents=True)
        (self.complex_project / 'app' / 'src' / 'main' / 'AndroidManifest.xml').write_text(
            '<manifest package="com.example.complex">'
            '<uses-permission android:name="android.permission.FOREGROUND_SERVICE" />'
            '<application>'
            '<service android:name=".sync.FileSyncService" android:foregroundServiceType="dataSync" />'
            '<service android:name=".auth.AccountAuthenticatorService" />'
            '<service android:name=".terminal.TermuxService" />'
            '<receiver android:name=".sync.UploadBroadcastReceiver" />'
            '</application></manifest>',
            encoding='utf-8',
        )
        (self.complex_project / 'app' / 'src' / 'main' / 'java' / 'com' / 'example' / 'complex' / 'ComplexSignals.kt').write_text(
            'package com.example.complex\n'
            'import android.accounts.AccountManager\n'
            'import android.accounts.AuthenticatorException\n'
            'import androidx.work.CoroutineWorker\n'
            'class FileSyncService\n'
            'class AccountAuthenticatorService\n'
            'class UploadBroadcastReceiver\n'
            'class RemoteOperationResult\n'
            'class OAuthTokenProvider\n'
            'class ComplexSyncWorker : CoroutineWorker()\n'
            'class RunCommandService\n'
            'class TermuxService\n'
            'class ExecutionCommand\n'
            'class TerminalSession\n'
            'fun runShell() { val shell = "sh"; val process = ProcessBuilder(shell).start() }\n'
            'fun auth(accountManager: AccountManager) { throw AuthenticatorException("token expired") }\n',
            encoding='utf-8',
        )
        (self.complex_project / 'settings.gradle').write_text("include ':app'\n", encoding='utf-8')
        (self.complex_project / 'app' / 'build.gradle').write_text(
            'android { namespace "com.example.complex"; defaultConfig { applicationId "com.example.complex" } }\n',
            encoding='utf-8',
        )
        (self.complex_project / 'CLAUDE.md').write_text(
            '# Complex App Guide\n\nPrioritize FileSyncService and AccountAuthenticatorService when debugging sync failures.\n',
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
                        },
                        {
                            'id': 'app-native-sample',
                            'title': 'Native Sample',
                            'summary': 'JNI native sample with render trace and native crash signals',
                            'keywords': ['NativeSample', 'native-sample', 'NativeRenderFrame', 'NativeBridge'],
                            'paths': [str(self.native_project)],
                        },
                        {
                            'id': 'app-complex-sample',
                            'title': 'Complex App Sample',
                            'summary': 'Complex sync account background terminal command app',
                            'keywords': ['FileSyncService', 'AccountAuthenticatorService', 'RunCommandService', 'TerminalSession'],
                            'paths': [str(self.complex_project)],
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

    def run_cli(self, *args, paths_config=None):
        cmd = [
            sys.executable,
            str(SCRIPT),
            '--json',
            '--paths-config',
            str(paths_config or self.paths_config),
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
        self.assertIn('evaluate', proc.stdout)

    def test_bootstrap_eval_repos_dry_run(self):
        repos_file = self.root / 'repos.json'
        repos_file.write_text(
            json.dumps(
                {
                    'version': 1,
                    'repositories': [
                        {
                            'repo': 'android/nowinandroid',
                            'url': 'https://github.com/android/nowinandroid',
                            'local_dir': str(self.root / 'github_apps' / 'android__nowinandroid'),
                            'sparse_paths': ['app', 'core'],
                        }
                    ],
                }
            ),
            encoding='utf-8',
        )
        proc = subprocess.run(
            [
                sys.executable,
                str(BOOTSTRAP_SCRIPT),
                '--repos-file',
                str(repos_file),
                '--repo',
                'android/nowinandroid',
                '--dry-run',
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('--sparse', proc.stdout)
        self.assertIn('android__nowinandroid', proc.stdout)

    def test_generate_validate_crud_and_test_rule_pack(self):
        generated = self.run_cli(
            'generate',
            '--bundle-id',
            'android-rdm',
            '--project-preset',
            'app',
            '--profile',
            'functional',
            '--profile',
            'stability',
            '--profile',
            'performance',
        )
        pack = generated['rule_pack']
        self.assertEqual(pack['id'], 'rdm-generated')
        self.assertEqual(pack['metadata']['profiles'], ['functional', 'stability', 'performance'])
        self.assertGreaterEqual(len(pack['rules']), 3)
        rule_ids = {rule['id'] for rule in pack['rules']}
        self.assertIn('rdm-generated-tier1-exact-log-1', rule_ids)
        self.assertIn('rdm-generated-tier2-project-scope', rule_ids)
        self.assertIn('rdm-generated-tier2-permission-scope', rule_ids)
        self.assertIn('rdm-generated-functional-tier2-scope', rule_ids)
        self.assertIn('rdm-generated-stability-scoped-crash', rule_ids)
        self.assertIn('rdm-generated-performance-scoped', rule_ids)
        self.assertGreater(pack['metadata']['exact_log_count'], 0)
        self.assertIn('tier2_scope_terms', pack['metadata'])
        serialized_pack = json.dumps(pack, ensure_ascii=False)
        self.assertIn('RdmSyncWorker', serialized_pack)
        self.assertIn('Retrofit', serialized_pack)
        self.assertIn('RoomDatabase', serialized_pack)
        self.assertIn('FATAL EXCEPTION', serialized_pack)
        self.assertIn('Choreographer', serialized_pack)
        self.assertIn('android.permission.POST_NOTIFICATIONS', serialized_pack)
        self.assertTrue((self.knowledge / 'bundles' / 'android-rdm' / 'rules' / 'rdm-generated.json').is_file())
        bundle_manifest = json.loads(
            (self.knowledge / 'bundles' / 'android-rdm' / 'bundle.json').read_text(encoding='utf-8')
        )
        self.assertIn('rdm-generated', bundle_manifest['rule_packs'])
        self.assertEqual(bundle_manifest['supported_profiles'], ['functional', 'stability', 'performance'])
        self.assertEqual(bundle_manifest['profile_overrides']['functional']['rule_packs'], ['rdm-generated'])
        self.assertEqual(bundle_manifest['profile_overrides']['stability']['issue_type'], 'android_app_crash')

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

    def test_generate_native_preset_rule_pack(self):
        generated = self.run_cli(
            'generate',
            '--bundle-id',
            'app-native-sample',
            '--rule-pack-id',
            'native-sample-generated',
            '--project-preset',
            'native',
            '--profile',
            'stability',
            '--profile',
            'performance',
        )
        pack = generated['rule_pack']
        self.assertEqual(pack['metadata']['project_preset'], 'native')
        self.assertEqual(pack['metadata']['profiles'], ['stability', 'performance'])
        rule_ids = {rule['id'] for rule in pack['rules']}
        self.assertIn('native-sample-generated-tier1-exact-log-1', rule_ids)
        self.assertIn('native-sample-generated-tier2-native-scope', rule_ids)
        self.assertIn('native-sample-generated-stability-scoped-crash', rule_ids)
        self.assertIn('native-sample-generated-performance-scoped', rule_ids)
        serialized_pack = json.dumps(pack, ensure_ascii=False)
        self.assertIn('native-sample', serialized_pack)
        self.assertIn('Java_com_example_nativeapp_NativeBridge_nativeCrash', serialized_pack)
        self.assertIn('NativeRenderFrame', serialized_pack)
        self.assertIn('JNI_OnLoad', serialized_pack)

        log_file = self.root / 'native.log'
        log_file.write_text(
            '05-10 F DEBUG: tombstone signal 11 backtrace #00 pc libnative-sample.so '
            'Java_com_example_nativeapp_NativeBridge_nativeCrash NativeRenderFrame\n',
            encoding='utf-8',
        )
        tested = self.run_cli(
            'test',
            '--bundle-id',
            'app-native-sample',
            '--rule-pack-id',
            'native-sample-generated',
            '--log-path',
            str(log_file),
            '--require-hit',
        )
        hit_ids = {hit['rule_id'] for hit in tested['hits']}
        self.assertIn('native-sample-generated-tier2-native-scope', hit_ids)
        self.assertIn('native-sample-generated-stability-scoped-crash', hit_ids)

    def test_generate_complex_app_signal_groups(self):
        generated = self.run_cli(
            'generate',
            '--bundle-id',
            'app-complex-sample',
            '--rule-pack-id',
            'complex-sample-generated',
            '--project-preset',
            'app',
            '--profile',
            'functional',
            '--profile',
            'performance',
        )
        pack = generated['rule_pack']
        rule_ids = {rule['id'] for rule in pack['rules']}
        self.assertIn('complex-sample-generated-tier2-project-scope', rule_ids)
        self.assertIn('complex-sample-generated-tier2-android-components', rule_ids)
        self.assertIn('complex-sample-generated-functional-tier2-scope', rule_ids)
        serialized_pack = json.dumps(pack, ensure_ascii=False)
        self.assertIn('FileSyncService', serialized_pack)
        self.assertIn('AccountAuthenticatorService', serialized_pack)
        self.assertIn('RemoteOperationResult', serialized_pack)
        self.assertIn('RunCommandService', serialized_pack)
        self.assertIn('TerminalSession', serialized_pack)
        hints = pack['metadata']['deep_hints']
        self.assertIn('FileSyncService', hints['code_search_terms'])
        self.assertIn('app/src/main/java', hints['preferred_paths'])
        self.assertIn('CLAUDE.md', hints['claude_md_candidates'])
        self.assertIn('android-log-rule-builder:app', hints['related_skills'])
        self.assertIn('sync', hints['case_tags'])
        bundle_manifest = json.loads(
            (self.knowledge / 'bundles' / 'app-complex-sample' / 'bundle.json').read_text(encoding='utf-8')
        )
        self.assertIn('deep_hints', bundle_manifest)

        log_file = self.root / 'complex.log'
        log_file.write_text(
            '05-10 ComplexApp FileSyncService AccountAuthenticatorService RemoteOperationResult token expired '
            'RunCommandService ExecutionCommand TerminalSession shell process failed\n',
            encoding='utf-8',
        )
        tested = self.run_cli(
            'test',
            '--bundle-id',
            'app-complex-sample',
            '--rule-pack-id',
            'complex-sample-generated',
            '--log-path',
            str(log_file),
            '--require-hit',
        )
        hit_ids = {hit['rule_id'] for hit in tested['hits']}
        self.assertIn('complex-sample-generated-tier2-project-scope', hit_ids)
        self.assertIn('complex-sample-generated-functional-tier2-scope', hit_ids)

    def test_evaluate_case_writes_scorecard(self):
        eval_root = self.root / 'android_analysis_eval'
        case_dir = eval_root / 'cases' / 'rdm-functional-lock'
        case_dir.mkdir(parents=True)
        (case_dir / 'rdm.log').write_text(
            '05-08 RDM lock failed DeviceLock policy denied CustomRdmState Retrofit RdmSyncWorker\n',
            encoding='utf-8',
        )
        (case_dir / 'case.json').write_text(
            json.dumps(
                {
                    'id': 'rdm-functional-lock',
                    'bundle_id': 'android-rdm',
                    'rule_pack_id': 'rdm-generated',
                    'project_preset': 'app',
                    'profiles': ['functional'],
                    'project_dirs': [str(self.project)],
                    'log_path': 'rdm.log',
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
        (case_dir / 'expected.json').write_text(
            json.dumps(
                {
                    'min_rule_count': 3,
                    'min_hit_count': 1,
                    'expected_keywords': ['RDM-Lock', 'com.example.rdm', 'RdmSyncWorker', 'Retrofit'],
                    'must_hit_rule_tags': ['tier2', 'profile'],
                    'must_hit_terms': ['RDM', 'Retrofit'],
                    'max_generic_term_ratio': 0.2,
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
        scorecard = self.root / 'scorecard.json'
        result = self.run_cli(
            'evaluate',
            '--eval-root',
            str(eval_root),
            '--scorecard',
            str(scorecard),
        )
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['case_count'], 1)
        self.assertEqual(result['passed_count'], 1)
        self.assertTrue(scorecard.is_file())

        saved = json.loads(scorecard.read_text(encoding='utf-8'))
        self.assertTrue(saved['ok'])
        self.assertEqual(saved['cases'][0]['id'], 'rdm-functional-lock')
        self.assertGreaterEqual(saved['cases'][0]['metrics']['hit_count'], 1)

    def test_evaluate_can_synthesize_paths_config_from_case_bundle(self):
        empty_paths_config = self.root / 'empty_paths.config.json'
        empty_paths_config.write_text(json.dumps({'version': 2, 'bundles': []}), encoding='utf-8')
        eval_root = self.root / 'android_analysis_eval'
        case_dir = eval_root / 'cases' / 'rdm-profile-synth'
        case_dir.mkdir(parents=True)
        (case_dir / 'rdm.log').write_text(
            '05-08 FATAL EXCEPTION AndroidRuntime com.example.rdm RDM lock failed\n',
            encoding='utf-8',
        )
        (case_dir / 'case.json').write_text(
            json.dumps(
                {
                    'id': 'rdm-profile-synth',
                    'bundle_id': 'android-rdm',
                    'rule_pack_id': 'rdm-generated',
                    'project_preset': 'app',
                    'profiles': ['functional', 'stability'],
                    'project_dirs': [str(self.project)],
                    'bundle': {
                        'title': 'Android RDM',
                        'summary': '锁机 解锁 provision device policy',
                        'keywords': ['rdm', 'lock', 'unlock', 'device policy'],
                    },
                    'log_path': 'rdm.log',
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
        (case_dir / 'expected.json').write_text(
            json.dumps(
                {
                    'min_rule_count': 4,
                    'min_hit_count': 1,
                    'expected_profile': 'stability',
                    'expected_keywords': ['FATAL EXCEPTION', 'com.example.rdm'],
                    'must_hit_rule_tags': ['profile'],
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )

        result = self.run_cli(
            'evaluate',
            '--eval-root',
            str(eval_root),
            paths_config=empty_paths_config,
        )
        self.assertTrue(result['ok'], result)
        case_result = result['cases'][0]
        self.assertIn('_eval_paths', case_result['paths_config'])
        generated_paths = Path(case_result['paths_config'])
        self.assertTrue(generated_paths.is_file())
        generated_config = json.loads(generated_paths.read_text(encoding='utf-8'))
        self.assertEqual(generated_config['bundles'][0]['id'], 'android-rdm')
        self.assertIn('stability', case_result['metrics']['profile_coverage'])


if __name__ == '__main__':
    unittest.main()
