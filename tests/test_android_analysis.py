import json
import gzip
import tempfile
import unittest
import zipfile
from pathlib import Path

from claude_web.android_analysis.archive import safe_extract_archive
from claude_web.android_analysis.casebook import confirm_case_draft, generate_case_draft, recall_case_cards, write_case_cards
from claude_web.android_analysis.classifier import (
    build_classifier_prompt,
    fallback_question_classifier,
    run_question_classifier,
    validate_classification_result,
)
from claude_web.android_analysis.code_scope import collect_code_context, resolve_code_scopes
from claude_web.android_analysis.deep_analysis import build_deep_evidence_pack, generate_deep_report
from claude_web.android_analysis.evidence import generate_first_evidence_pack
from claude_web.android_analysis.evidence_selector import run_evidence_template_selection
from claude_web.android_analysis.expert_knowledge import (
    build_expert_knowledge_cache,
    load_project_knowledge_dir,
    summarize_expert_knowledge_cache,
)
from claude_web.android_analysis.expert_knowledge_builder import (
    convert_evidence_templates,
    convert_xml_state_templates,
    create_project_knowledge_scaffold,
    read_evidence_templates_csv,
    read_evidence_templates_xlsx,
    read_xml_state_templates_csv,
    read_xml_state_templates_xlsx,
)
from claude_web.android_analysis.evidence_template_pipeline import (
    normalize_evidence_template_draft,
    run_evidence_template_batch_generation_pipeline,
    run_evidence_template_generation_pipeline,
    scan_source_log_candidates,
    validate_generated_templates,
)
from claude_web.android_analysis.jobs import AndroidAnalysisJobStore
from claude_web.android_analysis.models import AndroidAnalysisError, ExtractionLimits, PlannerPromptLimits
from claude_web.android_analysis.parameter_resolver import run_parameter_resolution
from claude_web.android_analysis.planner import _collect_stream_json, _emit_stream_trace, parse_planner_json, run_planner, validate_planner_result
from claude_web.android_analysis.profiler import profile_extracted_tree
from claude_web.android_analysis.reporter import generate_first_report
from claude_web.android_analysis.rule_engine import run_rule_matching, score_event_relevance
from claude_web.android_analysis.rule_loader import load_rule_packs
from claude_web.android_analysis.sampler import sample_files
from claude_web.android_analysis.verifier import run_verifier
from claude_web.android_analysis.xml_state_template_pipeline import (
    normalize_xml_state_template_draft,
    run_xml_state_template_batch_generation_pipeline,
    scan_source_xml_state_candidates,
    validate_generated_xml_state_templates,
)
from claude_web.android_analysis.xml_state_matcher import run_xml_state_matching


class AndroidAnalysisPhaseOneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_zip(self, name_to_content):
        archive = self.root / 'logs.zip'
        with zipfile.ZipFile(archive, 'w') as zf:
            for name, content in name_to_content.items():
                zf.writestr(name, content)
        return archive

    def make_expert_knowledge_project(self, module_id='android-rdm'):
        project = self.root / f'{module_id}-project'
        knowledge = project / '.claude-web' / 'android-analysis'
        (knowledge / 'cases').mkdir(parents=True)
        (knowledge / 'module.json').write_text(
            json.dumps(
                {
                    'id': module_id,
                    'title': 'RealtimeDeviceManager',
                    'description': 'RDM lock, unlock, check-in, EULA and push-token flows.',
                    'source_roots': ['app/src/main/java'],
                    'skill_paths': ['skills/rdm-log-analysis/SKILL.md'],
                    'guide_paths': ['CLAUDE.md', 'AGENTS.md'],
                    'default_package_names': ['com.hihonor.realtimedevicemanager'],
                    'package_resolution': {'required': False, 'reason': 'RDM has a stable package name.'},
                    'profiles': ['functional', 'stability'],
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
        (knowledge / 'subcategories.json').write_text(
            json.dumps(
                [
                    {
                        'id': 'activation_eula',
                        'title': 'Activation EULA',
                        'description': 'Online activation and EULA agreement page flow.',
                        'aliases': ['EULA', 'agreement page'],
                    }
                ],
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
        (knowledge / 'log_types.json').write_text(
            json.dumps(
                [
                    {
                        'id': 'rdm_business_log',
                        'title': 'RDM business log',
                        'path_patterns': ['(?i)(rdm|realtimedevicemanager).*\\.(txt|log)$'],
                        'content_patterns': ['\\bDeviceLockSchedulerImpl\\b'],
                        'priority': 80,
                    }
                ],
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
        (knowledge / 'evidence_templates.jsonl').write_text(
            json.dumps(
                {
                    'id': 'rdm-checkin-eula-missing',
                    'module_id': module_id,
                    'subcategory_id': 'activation_eula',
                    'profile': 'functional',
                    'log_type': 'rdm_business_log',
                    'regex': '\\bDeviceLockSchedulerImpl\\b.*\\bhas eula:false\\b',
                    'parameters': [],
                    'code_location': 'DeviceLockSchedulerImpl#processCheckInResult',
                    'meaning': 'Check-in result does not contain EULA configuration.',
                    'severity': 'critical',
                    'time_anchor': True,
                },
                ensure_ascii=False,
            )
            + '\n',
            encoding='utf-8',
        )
        (knowledge / 'xml_state_templates.jsonl').write_text(
            json.dumps(
                {
                    'id': 'rdm-user-state-lock-state',
                    'module_id': module_id,
                    'subcategory_id': 'activation_eula',
                    'profile': 'functional',
                    'source_type': 'shared_prefs_xml',
                    'path_patterns': ['(?i)shared_prefs.*user_state.*\\.xml$'],
                    'key_regex': '^lock_state$',
                    'value_regex': '^-?\\d+$',
                    'value_source': 'shared_prefs_value',
                    'code_location': 'DeviceLockStateManagerImpl#saveDeviceStateToLocalSp',
                    'meaning': 'Local RDM lock state persisted in user_state.xml.',
                    'severity': 'warning',
                    'time_anchor': True,
                    'next_steps': ['Compare with lock/unlock logs.'],
                },
                ensure_ascii=False,
            )
            + '\n',
            encoding='utf-8',
        )
        (knowledge / 'experience_logs.jsonl').write_text(
            json.dumps(
                {
                    'id': 'rdm-empty-eula',
                    'pattern': '\\bhas eula:false\\b',
                    'meaning': 'RDM did not receive EULA config.',
                    'owner_domain': 'RDM server config',
                },
                ensure_ascii=False,
            )
            + '\n',
            encoding='utf-8',
        )
        (knowledge / 'cases' / 'case_cards.jsonl').write_text(
            json.dumps(
                {
                    'case_id': 'rdm-eula-missing-001',
                    'module_id': module_id,
                    'subcategory_id': 'activation_eula',
                    'profile': 'functional',
                    'summary': 'EULA page missing after activation.',
                    'embedding_text': 'RDM activation EULA missing has eula false',
                    'key_evidence': ['DeviceLockSchedulerImpl has eula:false'],
                    'root_cause': 'Server returned empty EULA config.',
                    'used_template_ids': ['rdm-checkin-eula-missing'],
                },
                ensure_ascii=False,
            )
            + '\n',
            encoding='utf-8',
        )
        (knowledge / 'evidence_templates.csv').write_text(
            'id,module_id,subcategory_id,profile,log_type,regex,meaning\n',
            encoding='utf-8',
        )
        return project, knowledge

    def make_fwk_expert_modules(self):
        project = self.root / 'fwk-project'
        modules_root = project / '.claude-web' / 'android-analysis' / 'modules'
        specs = [
            (
                'android-fwk-ams',
                'Android AMS',
                'ActivityManager process, activity, broadcast, service and adj routing.',
                [
                    {
                        'id': 'broadcast_missing_or_delayed',
                        'title': 'Broadcast missing or delayed',
                        'description': 'Broadcast receiver does not receive the intent or receives it too late.',
                        'aliases': ['广播没收到', '广播延迟', 'receiver', 'BroadcastQueue'],
                    },
                    {
                        'id': 'activity_start_reason',
                        'title': 'Activity start reason',
                        'description': 'Explain why an Activity was launched.',
                        'aliases': ['activity启动原因', 'startActivity'],
                    },
                ],
            ),
            (
                'android-fwk-pms',
                'Android PMS',
                'PackageManager install, uninstall, permission and component resolution.',
                [
                    {
                        'id': 'package_install_failed',
                        'title': 'Package install failed',
                        'description': 'APK install or package scan failed.',
                        'aliases': ['安装失败', 'PackageManager', 'install failed'],
                    }
                ],
            ),
        ]
        for module_id, title, description, subcategories in specs:
            knowledge = modules_root / module_id
            knowledge.mkdir(parents=True, exist_ok=True)
            (knowledge / 'module.json').write_text(
                json.dumps(
                    {
                        'id': module_id,
                        'title': title,
                        'description': description,
                        'profiles': ['functional', 'xts', 'stability'],
                        'source_roots': ['.'],
                    },
                    ensure_ascii=False,
                ),
                encoding='utf-8',
            )
            (knowledge / 'subcategories.json').write_text(
                json.dumps(subcategories, ensure_ascii=False),
                encoding='utf-8',
            )
            (knowledge / 'evidence_templates.jsonl').write_text('', encoding='utf-8')
        return project

    def test_expert_knowledge_loads_project_pack_and_cache_summary(self):
        project, knowledge = self.make_expert_knowledge_project()

        loaded = load_project_knowledge_dir(
            knowledge,
            project_root=project,
            bundle_id='android-rdm',
            bundle_title='RDM',
        )
        cache = build_expert_knowledge_cache(
            [{'id': 'android-rdm', 'title': 'RDM', 'paths': [str(project)]}],
            self.root / 'android_analysis_knowledge',
            '.claude-web/android-analysis',
        )
        summary = summarize_expert_knowledge_cache(cache, include_details=True)

        self.assertEqual(loaded['module']['id'], 'android-rdm')
        self.assertEqual(len(loaded['subcategories']), 1)
        self.assertEqual(len(loaded['evidence_templates']), 1)
        self.assertEqual(len(loaded['xml_state_templates']), 1)
        self.assertEqual(len(loaded['experience_logs']), 1)
        self.assertEqual(len(loaded['case_cards']), 1)
        self.assertEqual(loaded['errors'], [])
        self.assertIn('evidence_templates.csv', loaded['table_sources'])
        self.assertEqual(summary['module_count'], 1)
        self.assertEqual(summary['modules'][0]['evidence_template_count'], 1)
        self.assertEqual(summary['modules'][0]['xml_state_template_count'], 1)
        self.assertEqual(summary['details'][0]['evidence_templates'][0]['id'], 'rdm-checkin-eula-missing')
        self.assertEqual(summary['details'][0]['xml_state_templates'][0]['id'], 'rdm-user-state-lock-state')
        self.assertIn('DeviceLockSchedulerImpl', summary['details'][0]['evidence_templates'][0]['regex'])
        self.assertEqual(summary['details'][0]['experience_logs'][0]['id'], 'rdm-empty-eula')
        self.assertEqual(summary['error_count'], 0)

    def test_expert_knowledge_reports_schema_errors_without_crashing(self):
        project = self.root / 'broken-project'
        knowledge = project / '.claude-web' / 'android-analysis'
        knowledge.mkdir(parents=True)
        (knowledge / 'module.json').write_text(
            json.dumps({'id': 'android-rdm', 'title': 'RDM', 'description': 'Broken test module'}),
            encoding='utf-8',
        )
        (knowledge / 'evidence_templates.jsonl').write_text(
            json.dumps({'id': 'bad-regex', 'regex': '['}) + '\n',
            encoding='utf-8',
        )

        loaded = load_project_knowledge_dir(knowledge, project_root=project)
        cache = build_expert_knowledge_cache(
            [{'id': 'android-rdm', 'title': 'RDM', 'paths': [str(project)]}],
            self.root / 'android_analysis_knowledge',
        )

        self.assertEqual(loaded['module']['id'], 'android-rdm')
        self.assertEqual(loaded['evidence_templates'], [])
        self.assertTrue(any(e['code'] == 'invalid_regex' for e in loaded['errors']))
        self.assertEqual(cache['modules'][0]['id'], 'android-rdm')
        self.assertTrue(any(e['code'] == 'invalid_regex' for e in cache['errors']))

    def test_question_classifier_fallback_uses_only_question_and_module_catalog(self):
        fwk_project = self.make_fwk_expert_modules()
        cache = build_expert_knowledge_cache(
            [{'id': 'android-fwk', 'title': 'FWK', 'paths': [str(fwk_project)]}],
            self.root / 'android_analysis_knowledge',
        )
        artifacts = self.root / 'artifacts'

        result = run_question_classifier(
            artifacts,
            '钉钉广播没收到，后台拉起时间很晚，帮我看下原因',
            cache,
            enable_ai=False,
        )

        self.assertEqual(result['module_id'], 'android-fwk-ams')
        self.assertEqual(result['submodule_id'], 'broadcast_missing_or_delayed')
        self.assertEqual(result['profile'], 'functional')
        self.assertFalse(result['need_user_clarification'])
        self.assertTrue((artifacts / 'classification_prompt.md').is_file())
        self.assertTrue((artifacts / 'classification_result.json').is_file())
        prompt = (artifacts / 'classification_prompt.md').read_text(encoding='utf-8')
        self.assertIn('钉钉广播没收到', prompt)
        self.assertNotIn('file_manifest', prompt)
        self.assertNotIn('matched_rules', prompt)

    def test_question_classifier_validation_keeps_ambiguous_submodule_unknown(self):
        fwk_project = self.make_fwk_expert_modules()
        cache = build_expert_knowledge_cache(
            [{'id': 'android-fwk', 'title': 'FWK', 'paths': [str(fwk_project)]}],
            self.root / 'android_analysis_knowledge',
        )
        catalog_prompt = build_classifier_prompt('activity和广播都有点异常', cache['modules'])
        self.assertIn('activity和广播都有点异常', catalog_prompt)
        catalog = [
            {
                'id': item['id'],
                'title': item['title'],
                'description': item['description'],
                'profiles': item.get('profiles') or [],
                'default_package_names': [],
                'package_resolution': {},
                'aliases': [],
                'subcategories': [
                    {'id': 'broadcast_missing_or_delayed', 'title': 'Broadcast', 'description': '', 'aliases': []},
                    {'id': 'activity_start_reason', 'title': 'Activity', 'description': '', 'aliases': []},
                ],
            }
            for item in cache['modules']
            if item['id'] == 'android-fwk-ams'
        ]

        result = validate_classification_result(
            {
                'module_id': 'android-fwk-ams',
                'module_confidence': 0.72,
                'submodule_id': 'broadcast_missing_or_delayed',
                'submodule_confidence': 0.66,
                'profile': 'functional',
                'top_candidates': [
                    {
                        'module_id': 'android-fwk-ams',
                        'submodule_id': 'broadcast_missing_or_delayed',
                        'profile': 'functional',
                        'score': 0.66,
                        'reason': 'mentions broadcast',
                    },
                    {
                        'module_id': 'android-fwk-ams',
                        'submodule_id': 'activity_start_reason',
                        'profile': 'functional',
                        'score': 0.58,
                        'reason': 'mentions activity',
                    },
                ],
                'package_candidates': ['com.example.demo'],
            },
            catalog,
            question='com.example.demo activity和广播都有点异常',
        )

        self.assertEqual(result['module_id'], 'android-fwk-ams')
        self.assertEqual(result['submodule_id'], 'unknown')
        self.assertTrue(result['need_submodule'])
        self.assertEqual(result['package_candidates'], ['com.example.demo'])

    def test_parameter_resolution_uses_module_default_package_without_clarification(self):
        project, _ = self.make_expert_knowledge_project()
        cache = build_expert_knowledge_cache(
            [{'id': 'android-rdm', 'title': 'RDM', 'paths': [str(project)]}],
            self.root / 'android_analysis_knowledge',
        )
        artifacts = self.root / 'artifacts'
        classification = {
            'module_id': 'android-rdm',
            'submodule_id': 'activation_eula',
            'profile': 'functional',
            'top_candidates': [],
        }

        result = run_parameter_resolution(
            artifacts,
            'RDM 恢厂后在线激活，声明页面后无协议页面',
            cache,
            classification=classification,
        )

        self.assertFalse(result['need_package_resolution'])
        self.assertFalse(result['need_user_clarification'])
        self.assertEqual(result['default_package_names'], ['com.hihonor.realtimedevicemanager'])
        self.assertEqual(result['resolved_parameters']['package_name'], ['com.hihonor.realtimedevicemanager'])
        self.assertEqual(result['package_candidates'][0]['source'], 'module_default_package')
        self.assertTrue((artifacts / 'parameter_resolution.json').is_file())

    def test_parameter_resolution_matches_app_inventory_labels_and_keeps_multiple_candidates(self):
        project = self.root / 'ams-project'
        knowledge = project / '.claude-web' / 'android-analysis'
        knowledge.mkdir(parents=True)
        (knowledge / 'module.json').write_text(
            json.dumps(
                {
                    'id': 'android-fwk-ams',
                    'title': 'Android AMS',
                    'description': 'AMS activity and broadcast routing.',
                    'profiles': ['functional'],
                    'package_resolution': {'required': True, 'reason': 'AMS 问题通常需要目标应用包名。'},
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
        (knowledge / 'subcategories.json').write_text(
            json.dumps(
                [
                    {
                        'id': 'broadcast_missing_or_delayed',
                        'title': 'Broadcast missing or delayed',
                        'description': 'Broadcast receiver delivery issue.',
                    }
                ],
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
        (knowledge / 'app_inventory.json').write_text(
            json.dumps(
                {
                    'apps': [
                        {
                            'packageName': 'com.tencent.mm',
                            'label': '微信',
                            'aliases': ['WeChat'],
                            'uid': 10123,
                        },
                        {
                            'packageName': 'com.tencent.wework',
                            'label': '企业微信',
                            'aliases': ['WeCom'],
                            'uid': 10124,
                        },
                    ]
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
        cache = build_expert_knowledge_cache(
            [{'id': 'android-fwk', 'title': 'FWK', 'paths': [str(project)]}],
            self.root / 'android_analysis_knowledge',
        )
        classification = {
            'module_id': 'android-fwk-ams',
            'submodule_id': 'broadcast_missing_or_delayed',
            'profile': 'functional',
            'top_candidates': [],
        }

        result = run_parameter_resolution(
            self.root / 'artifacts',
            '微信和企业微信的广播都没收到，帮我看下 AMS 分发原因',
            cache,
            classification=classification,
        )

        self.assertTrue(result['need_package_resolution'])
        self.assertFalse(result['need_user_clarification'])
        self.assertEqual(
            result['resolved_parameters']['package_name'],
            ['com.tencent.mm', 'com.tencent.wework'],
        )
        self.assertIn('10123', result['resolved_parameters']['uid'])
        self.assertIn('10124', result['resolved_parameters']['uid'])
        self.assertEqual(len(result['package_candidates']), 2)
        self.assertTrue(all(item['source'] == 'app_inventory_semantic_match' for item in result['package_candidates']))

    def test_global_app_inventory_accepts_pm_list_packages_text(self):
        inventory_dir = self.root / 'android_analysis_knowledge' / 'global' / 'app_inventory'
        inventory_dir.mkdir(parents=True)
        (inventory_dir / 'honor-device.txt').write_text(
            '\n'.join(
                [
                    'package:com.tencent.mm uid:10232',
                    'package:com.android.settings uid:1000',
                    'package:com.newcall uid:10225',
                    'package:android uid:1000',
                ]
            ),
            encoding='utf-8',
        )
        cache = build_expert_knowledge_cache([], self.root / 'android_analysis_knowledge')
        summary = summarize_expert_knowledge_cache(cache)

        result = run_parameter_resolution(
            self.root / 'artifacts-global-inventory',
            'com.tencent.mm 广播没收到，uid:10232',
            cache,
            classification={'module_id': 'android-fwk-ams', 'submodule_id': 'broadcast_missing_or_delayed', 'profile': 'functional'},
        )

        self.assertEqual(summary['global']['app_inventory_count'], 4)
        self.assertEqual(result['inventory_app_count'], 4)
        self.assertEqual(result['resolved_parameters']['package_name'], ['com.tencent.mm'])
        self.assertIn('10232', result['resolved_parameters']['uid'])

    def test_parameter_resolution_extracts_direct_package_component_and_flags_missing_required_package(self):
        project = self.root / 'ams-project'
        knowledge = project / '.claude-web' / 'android-analysis'
        knowledge.mkdir(parents=True)
        (knowledge / 'module.json').write_text(
            json.dumps(
                {
                    'id': 'android-fwk-ams',
                    'title': 'Android AMS',
                    'description': 'AMS activity routing.',
                    'package_resolution': {'required': True, 'reason': 'Need target package.'},
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
        (knowledge / 'subcategories.json').write_text('[]', encoding='utf-8')
        cache = build_expert_knowledge_cache(
            [{'id': 'android-fwk', 'title': 'FWK', 'paths': [str(project)]}],
            self.root / 'android_analysis_knowledge',
        )

        with_component = run_parameter_resolution(
            self.root / 'artifacts-direct',
            'cmp=com.example.demo/.MainActivity 启动失败，callingUid=10260',
            cache,
            classification={'module_id': 'android-fwk-ams', 'submodule_id': 'unknown', 'profile': 'functional'},
        )
        missing = run_parameter_resolution(
            self.root / 'artifacts-missing',
            '某个应用 activity 启动失败，但我没说包名',
            cache,
            classification={'module_id': 'android-fwk-ams', 'submodule_id': 'unknown', 'profile': 'functional'},
        )

        self.assertEqual(with_component['resolved_parameters']['package_name'], ['com.example.demo'])
        self.assertEqual(with_component['resolved_parameters']['component'], ['com.example.demo/.MainActivity'])
        self.assertIn('10260', with_component['resolved_parameters']['uid'])
        self.assertFalse(with_component['need_user_clarification'])
        self.assertTrue(missing['need_user_clarification'])

    def test_evidence_template_selection_uses_specific_subcategory_and_xml_templates(self):
        project, _ = self.make_expert_knowledge_project()
        cache = build_expert_knowledge_cache(
            [{'id': 'android-rdm', 'title': 'RDM', 'paths': [str(project)]}],
            self.root / 'android_analysis_knowledge',
        )
        artifacts = self.root / 'artifacts-evidence-select'
        classification = {
            'module_id': 'android-rdm',
            'module_confidence': 0.9,
            'submodule_id': 'activation_eula',
            'submodule_confidence': 0.86,
            'profile': 'functional',
            'top_candidates': [
                {
                    'module_id': 'android-rdm',
                    'submodule_id': 'activation_eula',
                    'profile': 'functional',
                    'score': 0.86,
                    'reason': 'RDM activation EULA issue',
                }
            ],
        }
        parameter_resolution = {
            'resolved_parameters': {'package_name': ['com.hihonor.realtimedevicemanager']},
            'package_candidates': [],
        }

        result = run_evidence_template_selection(
            artifacts,
            cache,
            classification=classification,
            parameter_resolution=parameter_resolution,
        )

        self.assertTrue((artifacts / 'selected_evidence_templates.json').is_file())
        self.assertEqual(result['module_selections'][0]['submodule_policy'], 'specific')
        self.assertEqual([item['id'] for item in result['templates']], ['rdm-checkin-eula-missing'])
        self.assertEqual([item['id'] for item in result['xml_state_templates']], ['rdm-user-state-lock-state'])
        self.assertEqual(result['templates'][0]['status'], 'ready')
        self.assertTrue(result['templates'][0]['search_enabled'])
        self.assertTrue(any('subcategory:activation_eula' in reason for reason in result['templates'][0]['selection_reasons']))
        self.assertEqual(result['counts']['experience_hint_count'], 1)

    def test_case_recall_uses_selected_templates_and_project_case_cards(self):
        project, _ = self.make_expert_knowledge_project()
        cache = build_expert_knowledge_cache(
            [{'id': 'android-rdm', 'title': 'RDM', 'paths': [str(project)]}],
            self.root / 'android_analysis_knowledge',
        )
        artifacts = self.root / 'artifacts-case-recall'
        classification = {
            'module_id': 'android-rdm',
            'module_confidence': 0.9,
            'submodule_id': 'activation_eula',
            'submodule_confidence': 0.86,
            'profile': 'functional',
            'top_candidates': [
                {
                    'module_id': 'android-rdm',
                    'submodule_id': 'activation_eula',
                    'profile': 'functional',
                    'score': 0.86,
                    'reason': 'RDM activation EULA issue',
                }
            ],
        }
        run_evidence_template_selection(
            artifacts,
            cache,
            classification=classification,
            parameter_resolution={'resolved_parameters': {}},
        )
        planner = {
            'issue_types': ['android_business_spec'],
            'candidate_bundle_ids': ['android-rdm'],
            'candidate_keywords': ['EULA', 'has eula false'],
        }
        matched = {
            'event_count': 1,
            'events': [
                {
                    'rule_id': 'rdm-checkin-eula-missing',
                    'source_bundle_ids': ['android-rdm'],
                    'tags': ['eula'],
                    'matched_terms': ['has eula:false'],
                }
            ],
        }
        traces = []

        cards = recall_case_cards(
            self.root / 'empty-knowledge',
            planner,
            matched,
            artifacts_dir=artifacts,
            expert_knowledge_cache=cache,
            debug_trace=lambda stage, event, data: traces.append((stage, event, data)),
        )

        self.assertEqual(cards['version'], 2)
        self.assertEqual(cards['card_count'], 1)
        self.assertEqual(cards['cards'][0]['id'], 'rdm-eula-missing-001')
        self.assertIn('used_template_id:rdm-checkin-eula-missing', cards['cards'][0]['match_reasons'])
        self.assertIn('submodule', cards['cards'][0]['match_reasons'])
        self.assertIn('rdm-checkin-eula-missing', cards['recall_context']['selected_template_ids'])
        self.assertIn('rdm-user-state-lock-state', cards['recall_context']['selected_template_ids'])
        self.assertTrue(any(event == 'case_recall_result' for _, event, _ in traces))

    def test_evidence_template_selection_expands_placeholders_and_blocks_unresolved(self):
        project, knowledge = self.make_expert_knowledge_project()
        (knowledge / 'evidence_templates.jsonl').write_text(
            '\n'.join(
                [
                    json.dumps(
                        {
                            'id': 'ams-start-package',
                            'module_id': 'android-rdm',
                            'subcategory_id': 'activation_eula',
                            'profile': 'functional',
                            'log_type': 'android_log',
                            'regex': '\\bActivityTaskManager\\b.*$package_name',
                            'parameters': ['package_name'],
                            'meaning': 'Activity log for target package.',
                            'severity': 'info',
                        },
                        ensure_ascii=False,
                    ),
                    '',
                ]
            ),
            encoding='utf-8',
        )
        cache = build_expert_knowledge_cache(
            [{'id': 'android-rdm', 'title': 'RDM', 'paths': [str(project)]}],
            self.root / 'android_analysis_knowledge',
        )
        classification = {
            'module_id': 'android-rdm',
            'module_confidence': 0.9,
            'submodule_id': 'activation_eula',
            'submodule_confidence': 0.8,
            'profile': 'functional',
            'top_candidates': [{'module_id': 'android-rdm', 'submodule_id': 'activation_eula', 'score': 0.8}],
        }

        ready = run_evidence_template_selection(
            self.root / 'artifacts-evidence-ready',
            cache,
            classification=classification,
            parameter_resolution={'resolved_parameters': {'package_name': ['com.example.demo']}},
        )
        blocked = run_evidence_template_selection(
            self.root / 'artifacts-evidence-blocked',
            cache,
            classification=classification,
            parameter_resolution={'resolved_parameters': {}},
        )

        ready_template = ready['templates'][0]
        blocked_template = blocked['templates'][0]
        self.assertEqual(ready_template['expanded_regex'], '\\bActivityTaskManager\\b.*com\\.example\\.demo')
        self.assertEqual(ready_template['status'], 'ready')
        self.assertTrue(ready_template['search_enabled'])
        self.assertEqual(blocked_template['status'], 'needs_parameters')
        self.assertFalse(blocked_template['search_enabled'])
        self.assertEqual(blocked_template['unresolved_parameters'], ['package_name'])

    def test_evidence_template_selection_loads_module_all_when_subcategory_is_ambiguous(self):
        project, knowledge = self.make_expert_knowledge_project()
        (knowledge / 'subcategories.json').write_text(
            json.dumps(
                [
                    {'id': 'activation_eula', 'title': 'Activation EULA'},
                    {'id': 'push_token_failed', 'title': 'Push token failed'},
                ],
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
        (knowledge / 'evidence_templates.jsonl').write_text(
            '\n'.join(
                [
                    json.dumps(
                        {
                            'id': 'rdm-checkin-eula-missing',
                            'module_id': 'android-rdm',
                            'subcategory_id': 'activation_eula',
                            'profile': 'functional',
                            'log_type': 'rdm_business_log',
                            'regex': '\\bDeviceLockSchedulerImpl\\b.*\\bhas eula:false\\b',
                            'meaning': 'Missing EULA.',
                            'severity': 'critical',
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            'id': 'rdm-push-token-empty',
                            'module_id': 'android-rdm',
                            'subcategory_id': 'push_token_failed',
                            'profile': 'functional',
                            'log_type': 'rdm_business_log',
                            'regex': '\\bPushToken\\b.*\\bempty\\b',
                            'meaning': 'Push token is empty.',
                            'severity': 'warning',
                        },
                        ensure_ascii=False,
                    ),
                    '',
                ]
            ),
            encoding='utf-8',
        )
        cache = build_expert_knowledge_cache(
            [{'id': 'android-rdm', 'title': 'RDM', 'paths': [str(project)]}],
            self.root / 'android_analysis_knowledge',
        )

        result = run_evidence_template_selection(
            self.root / 'artifacts-evidence-ambiguous',
            cache,
            classification={
                'module_id': 'android-rdm',
                'module_confidence': 0.74,
                'submodule_id': 'unknown',
                'submodule_confidence': 0.0,
                'profile': 'functional',
                'top_candidates': [
                    {'module_id': 'android-rdm', 'submodule_id': 'activation_eula', 'score': 0.55},
                    {'module_id': 'android-rdm', 'submodule_id': 'push_token_failed', 'score': 0.48},
                ],
            },
            parameter_resolution={'resolved_parameters': {}},
        )

        self.assertEqual(result['module_selections'][0]['submodule_policy'], 'module_all')
        self.assertEqual(
            sorted(item['id'] for item in result['templates']),
            ['rdm-checkin-eula-missing', 'rdm-push-token-empty'],
        )

    def test_expert_knowledge_scaffold_creates_reviewable_project_pack(self):
        project = self.root / 'rdm-real-project'
        project.mkdir()
        (project / 'CLAUDE.md').write_text('# RDM Guide\n', encoding='utf-8')

        result = create_project_knowledge_scaffold(
            project,
            module={
                'id': 'android-rdm',
                'title': 'RealtimeDeviceManager',
                'description': 'RDM lock and activation flows.',
                'default_package_names': ['com.hihonor.realtimedevicemanager'],
                'profiles': ['functional', 'stability'],
            },
            subcategories=[
                {
                    'id': 'activation_eula',
                    'title': 'Activation EULA',
                    'description': 'Check-in and EULA agreement page.',
                    'aliases': ['EULA'],
                }
            ],
            overwrite=False,
        )
        knowledge = project / '.claude-web' / 'android-analysis'
        loaded = load_project_knowledge_dir(knowledge, project_root=project)

        self.assertFalse(result['validation_errors'])
        self.assertTrue((knowledge / 'module.json').is_file())
        self.assertTrue((knowledge / 'subcategories.json').is_file())
        self.assertTrue((knowledge / 'evidence_templates.jsonl').is_file())
        self.assertTrue((knowledge / 'evidence_templates.csv').is_file())
        self.assertTrue((knowledge / 'evidence_templates.xlsx').is_file())
        self.assertTrue((knowledge / 'xml_state_templates.jsonl').is_file())
        self.assertTrue((knowledge / 'xml_state_templates.csv').is_file())
        self.assertTrue((knowledge / 'xml_state_templates.xlsx').is_file())
        self.assertTrue((knowledge / 'generation_prompt.md').is_file())
        self.assertTrue((project / 'skills' / 'android-rdm-analysis' / 'SKILL.md').is_file())
        self.assertEqual(loaded['module']['skill_paths'], ['skills/android-rdm-analysis/SKILL.md'])
        self.assertEqual(loaded['module']['guide_paths'], ['CLAUDE.md'])
        self.assertEqual(loaded['subcategories'][0]['id'], 'activation_eula')

    def test_expert_knowledge_scaffold_warns_about_question_mark_encoding_loss(self):
        project = self.root / 'encoding-loss-project'
        project.mkdir()

        result = create_project_knowledge_scaffold(
            project,
            module={
                'id': 'android-rdm',
                'title': 'RealtimeDeviceManager',
                'description': '???? APK???????',
            },
            subcategories=[
                {
                    'id': 'lock_unlock',
                    'title': '?????',
                    'description': '????????????',
                }
            ],
            overwrite=True,
            include_skill=False,
        )

        codes = [err['code'] for err in result['validation_errors']]
        self.assertIn('possible_encoding_loss', codes)

    def test_evidence_template_csv_and_xlsx_roundtrip(self):
        project = self.root / 'roundtrip-project'
        project.mkdir()
        evidence = [
            {
                'id': 'rdm-checkin-eula-missing',
                'module_id': 'android-rdm',
                'subcategory_id': 'activation_eula',
                'profile': 'functional',
                'log_type': 'rdm_business_log',
                'regex': '\\bDeviceLockSchedulerImpl\\b.*\\bhas eula:false\\b',
                'parameters': ['package_name'],
                'code_location': 'DeviceLockSchedulerImpl#processCheckInResult',
                'meaning': 'Check-in result does not contain EULA configuration.',
                'severity': 'critical',
                'time_anchor': True,
                'next_steps': ['Check server EULA config'],
            }
        ]
        create_project_knowledge_scaffold(
            project,
            module={'id': 'android-rdm', 'title': 'RDM', 'description': 'RDM flows.'},
            subcategories=[{'id': 'activation_eula', 'title': 'Activation EULA'}],
            evidence_templates=evidence,
            overwrite=True,
        )
        knowledge = project / '.claude-web' / 'android-analysis'

        csv_items = read_evidence_templates_csv(knowledge / 'evidence_templates.csv')
        xlsx_items = read_evidence_templates_xlsx(knowledge / 'evidence_templates.xlsx')
        self.assertEqual(csv_items[0]['id'], 'rdm-checkin-eula-missing')
        self.assertEqual(xlsx_items[0]['id'], 'rdm-checkin-eula-missing')

        (knowledge / 'evidence_templates.jsonl').write_text('', encoding='utf-8')
        result = convert_evidence_templates(knowledge, 'csv_to_jsonl')
        loaded = load_project_knowledge_dir(knowledge, project_root=project)

        self.assertFalse(result['validation_errors'])
        self.assertEqual(len(loaded['evidence_templates']), 1)
        self.assertEqual(loaded['evidence_templates'][0]['submodule_id'], 'activation_eula')
        self.assertIn('DeviceLockSchedulerImpl', loaded['evidence_templates'][0]['regex'])

    def test_xml_state_template_csv_and_xlsx_roundtrip(self):
        project = self.root / 'xml-roundtrip-project'
        project.mkdir()
        xml_templates = [
            {
                'id': 'rdm-user-state-lock-state',
                'module_id': 'android-rdm',
                'subcategory_id': 'lock_state_abnormal',
                'profile': 'functional',
                'source_type': 'shared_prefs_xml',
                'path_patterns': ['(?i)shared_prefs.*user_state.*\\.xml$'],
                'key_regex': '^lock_state$',
                'value_regex': '^-?\\d+$',
                'value_source': 'shared_prefs_value',
                'code_location': 'DeviceLockStateManagerImpl#saveDeviceStateToLocalSp',
                'meaning': 'Local lock state in SharedPreferences.',
                'severity': 'warning',
                'time_anchor': True,
                'next_steps': ['Compare with DeviceLockStateManagerImpl logs'],
            }
        ]
        create_project_knowledge_scaffold(
            project,
            module={'id': 'android-rdm', 'title': 'RDM', 'description': 'RDM flows.'},
            subcategories=[{'id': 'lock_state_abnormal', 'title': 'Lock state abnormal'}],
            xml_state_templates=xml_templates,
            overwrite=True,
        )
        knowledge = project / '.claude-web' / 'android-analysis'

        csv_items = read_xml_state_templates_csv(knowledge / 'xml_state_templates.csv')
        xlsx_items = read_xml_state_templates_xlsx(knowledge / 'xml_state_templates.xlsx')
        self.assertEqual(csv_items[0]['id'], 'rdm-user-state-lock-state')
        self.assertEqual(xlsx_items[0]['id'], 'rdm-user-state-lock-state')

        (knowledge / 'xml_state_templates.jsonl').write_text('', encoding='utf-8')
        result = convert_xml_state_templates(knowledge, 'csv_to_jsonl')
        loaded = load_project_knowledge_dir(knowledge, project_root=project)

        self.assertFalse(result['validation_errors'])
        self.assertEqual(len(loaded['xml_state_templates']), 1)
        self.assertEqual(loaded['xml_state_templates'][0]['submodule_id'], 'lock_state_abnormal')
        self.assertEqual(loaded['xml_state_templates'][0]['source_type'], 'shared_prefs_xml')

    def test_evidence_template_pipeline_prefilters_real_log_calls(self):
        project = self.root / 'pipeline-project'
        source_dir = project / 'app' / 'src' / 'main' / 'java' / 'com' / 'example'
        source_dir.mkdir(parents=True)
        (source_dir / 'PhonePropertyUtils.java').write_text(
            '''
package com.example;
import android.util.Log;
class PhonePropertyUtils {
  private static final String LOG_TAG = "PhonePropertyUtils";
  void readImei(Object telephony) {
    if (telephony == null) {
      Log.e(LOG_TAG, "getIMEIInfo-> telephony is null, can not init imei");
    }
    String constantOnly = "imei is null or empty";
  }
}
''',
            encoding='utf-8',
        )

        candidates = scan_source_log_candidates(
            project,
            [project / 'app' / 'src' / 'main'],
            ['imei', 'telephony'],
            max_candidates=10,
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['tag'], 'PhonePropertyUtils')
        self.assertIn('telephony is null', candidates[0]['message'])

    def test_evidence_template_pipeline_normalizes_csv_and_validates_candidate(self):
        project = self.root / 'pipeline-normalize-project'
        source_dir = project / 'app' / 'src' / 'main' / 'java' / 'com' / 'example'
        source_dir.mkdir(parents=True)
        source_file = source_dir / 'PhonePropertyUtils.java'
        source_file.write_text(
            '''
package com.example;
import android.util.Log;
class PhonePropertyUtils {
  private static final String LOG_TAG = "PhonePropertyUtils";
  void readImei(Object telephony) {
    Log.e(LOG_TAG, "getIMEIInfo-> telephony is null, can not init imei");
  }
}
''',
            encoding='utf-8',
        )
        draft = self.root / 'draft.csvish.jsonl'
        normalized = self.root / 'normalized.jsonl'
        draft.write_text(
            'id,module_id,subcategory_id,profile,log_type,regex,parameters,code_location,meaning,severity,time_anchor,next_steps,enabled\n'
            '"imei_telephony_null","android-rdm","device_identification_failed","functional","android_log","PhonePropertyUtils:\\s+getIMEIInfo-> telephony is null, can not init imei","","app/src/main/java/com/example/PhonePropertyUtils.java:7","Telephony empty","critical","problem_start","check telephony",true\n',
            encoding='utf-8',
        )
        parsed = normalize_evidence_template_draft(draft, normalized)
        candidates = scan_source_log_candidates(project, [project / 'app' / 'src' / 'main'], ['imei'], max_candidates=10)
        errors = validate_generated_templates(parsed['items'], project, candidates)

        self.assertEqual(len(parsed['items']), 1)
        self.assertFalse(parsed['parse_errors'])
        self.assertFalse(errors)
        self.assertTrue(normalized.read_text(encoding='utf-8').strip().startswith('{'))

    def test_evidence_template_pipeline_dry_run_writes_prompt_and_candidates(self):
        project = self.root / 'pipeline-dry-run-project'
        source_dir = project / 'app' / 'src' / 'main' / 'java' / 'com' / 'example'
        source_dir.mkdir(parents=True)
        (source_dir / 'PhonePropertyUtils.java').write_text(
            '''
package com.example;
import android.util.Log;
class PhonePropertyUtils {
  private static final String LOG_TAG = "PhonePropertyUtils";
  void readImei(Object telephony) {
    Log.e(LOG_TAG, "getIMEIInfo-> telephony is null, can not init imei");
  }
}
''',
            encoding='utf-8',
        )
        create_project_knowledge_scaffold(
            project,
            module={
                'id': 'android-rdm',
                'title': 'RDM',
                'description': 'RDM flows.',
                'source_roots': ['app/src/main'],
            },
            subcategories=[
                {
                    'id': 'device_identification_failed',
                    'title': 'Device identification failed',
                    'description': 'IMEI and device code failures.',
                }
            ],
            overwrite=True,
            include_skill=False,
        )

        result = run_evidence_template_generation_pipeline(
            project,
            subcategory_id='device_identification_failed',
            mode='prefiltered',
            claude_cli_path='claude-not-called',
            dry_run=True,
        )

        self.assertTrue(result['ok'])
        self.assertEqual(result['candidate_count'], 1)
        self.assertTrue(Path(result['prompt_path']).is_file())
        self.assertTrue(Path(result['candidate_paths']['jsonl']).is_file())
        self.assertGreater(result['metrics']['prompt_chars'], 0)

    def test_evidence_template_batch_pipeline_dry_run_uses_external_output_dir(self):
        project = self.root / 'pipeline-batch-project'
        source_dir = project / 'app' / 'src' / 'main' / 'java' / 'com' / 'example'
        source_dir.mkdir(parents=True)
        (source_dir / 'DeviceFlow.java').write_text(
            '''
package com.example;
import android.util.Log;
class DeviceFlow {
  void readImei() { Log.e("PhonePropertyUtils", "getIMEIInfo-> telephony is null, can not init imei"); }
  void lock() { Log.w("LockFlow", "lock task failed"); }
}
''',
            encoding='utf-8',
        )
        create_project_knowledge_scaffold(
            project,
            module={
                'id': 'android-rdm',
                'title': 'RDM',
                'description': 'RDM flows.',
                'source_roots': ['app/src/main'],
            },
            subcategories=[
                {
                    'id': 'device_identification_failed',
                    'title': 'Device identification failed',
                    'description': 'IMEI and device code failures.',
                },
                {'id': 'lock_failed', 'title': 'Lock failed', 'description': 'Lock task failures.'},
            ],
            overwrite=True,
            include_skill=False,
        )
        output_dir = self.root / 'server-workbench' / 'rdm'

        result = run_evidence_template_batch_generation_pipeline(
            project,
            output_dir=output_dir,
            claude_cli_path='claude-not-called',
            per_subcategory_max_candidates=5,
            dry_run=True,
        )

        self.assertTrue(result['ok'])
        self.assertEqual(result['subcategory_count'], 2)
        self.assertGreaterEqual(result['candidate_count'], 2)
        self.assertTrue(Path(result['candidate_paths']['jsonl']).is_file())
        self.assertTrue(Path(result['prompt_path']).is_file())
        self.assertTrue(str(result['output_dir']).endswith('server-workbench\\rdm') or str(result['output_dir']).endswith('server-workbench/rdm'))
        self.assertFalse((project / '.claude-web' / 'android-analysis' / 'log_candidates.all.prefiltered.jsonl').exists())

    def test_expert_knowledge_cache_loads_nested_module_packs(self):
        project = self.root / 'fwk-project'
        project.mkdir()
        for module_id in ('android-fwk-ams', 'android-fwk-pms'):
            create_project_knowledge_scaffold(
                project,
                relative_path=f'.claude-web/android-analysis/modules/{module_id}',
                module={
                    'id': module_id,
                    'title': module_id,
                    'description': f'{module_id} knowledge pack.',
                    'source_roots': ['.'],
                },
                subcategories=[{'id': 'unknown', 'title': 'Unknown', 'description': 'Generic issue.'}],
                overwrite=True,
                include_skill=False,
            )

        cache = build_expert_knowledge_cache(
            [{'id': 'android-fwk', 'title': 'Android FWK', 'paths': [str(project)]}],
            self.root / 'android_analysis_knowledge',
        )

        self.assertEqual(cache['modules'][0]['bundle_id'], 'android-fwk')
        self.assertEqual(cache['module_index']['android-fwk-ams']['project_root'], str(project.resolve()))
        self.assertEqual(cache['module_index']['android-fwk-pms']['project_root'], str(project.resolve()))
        self.assertEqual(cache['errors'], [])

    def test_evidence_template_pipeline_accepts_single_file_source_root(self):
        project = self.root / 'single-file-source-project'
        project.mkdir()
        (project / 'ActivityManager.java').write_text(
            '''
package android.app;
import android.util.Log;
class ActivityManager {
  void start() { Log.i("ActivityManager", "startActivity called"); }
}
''',
            encoding='utf-8',
        )
        create_project_knowledge_scaffold(
            project,
            module={
                'id': 'android-fwk-ams',
                'title': 'AMS',
                'description': 'Activity manager flows.',
                'source_roots': ['ActivityManager.java'],
            },
            subcategories=[{'id': 'activity_start_reason', 'title': 'Activity start reason', 'description': 'Activity start logs.'}],
            overwrite=True,
            include_skill=False,
        )

        result = run_evidence_template_batch_generation_pipeline(
            project,
            output_dir=self.root / 'server-workbench' / 'ams',
            claude_cli_path='claude-not-called',
            per_subcategory_max_candidates=5,
            dry_run=True,
        )

        self.assertTrue(result['ok'])
        self.assertEqual(result['candidate_count'], 1)

    def test_xml_state_pipeline_prefilters_shared_preferences_candidates(self):
        project = self.root / 'xml-state-pipeline-project'
        source_dir = project / 'app' / 'src' / 'main' / 'java' / 'com' / 'example'
        source_dir.mkdir(parents=True)
        (source_dir / 'DeviceLockStateManagerImpl.java').write_text(
            '''
package com.example;
import android.content.Context;
import android.content.SharedPreferences;
class DeviceLockStateManagerImpl {
  private static final String FILE_NAME = "user_state";
  private static final String TAG_LOCK_STATE = "lock_state";
  void save(Context context, int state) {
    SharedPreferences sp = context.createDeviceProtectedStorageContext()
        .getSharedPreferences(FILE_NAME, Context.MODE_PRIVATE);
    sp.edit().putInt(TAG_LOCK_STATE, state).apply();
  }
}
''',
            encoding='utf-8',
        )

        candidates = scan_source_xml_state_candidates(
            project,
            [project / 'app' / 'src' / 'main'],
            ['lock', 'state'],
            max_candidates=10,
        )

        self.assertTrue(candidates)
        joined = json.dumps(candidates, ensure_ascii=False)
        self.assertIn('user_state', joined)
        self.assertIn('lock_state', joined)

    def test_xml_state_pipeline_normalizes_and_validates_candidate(self):
        project = self.root / 'xml-state-normalize-project'
        source_dir = project / 'app' / 'src' / 'main' / 'java' / 'com' / 'example'
        source_dir.mkdir(parents=True)
        (source_dir / 'DeviceLockStateManagerImpl.java').write_text(
            '''
package com.example;
import android.content.Context;
import android.content.SharedPreferences;
class DeviceLockStateManagerImpl {
  private static final String FILE_NAME = "user_state";
  private static final String TAG_LOCK_STATE = "lock_state";
  void save(Context context, int state) {
    SharedPreferences sp = context.getSharedPreferences(FILE_NAME, Context.MODE_PRIVATE);
    sp.edit().putInt(TAG_LOCK_STATE, state).apply();
  }
}
''',
            encoding='utf-8',
        )
        draft = self.root / 'xml_draft.csvish.jsonl'
        normalized = self.root / 'xml_normalized.jsonl'
        draft.write_text(
            'id,module_id,subcategory_id,profile,source_type,path_patterns,key_regex,value_regex,value_source,code_location,meaning,severity,time_anchor,next_steps,enabled\n'
            '"rdm-user-state-lock-state","android-rdm","lock_state_abnormal","functional","shared_prefs_xml","(?i)shared_prefs.*user_state.*\\.xml$","^lock_state$","^-?\\d+$","shared_prefs_value","app/src/main/java/com/example/DeviceLockStateManagerImpl.java:10","Local lock state","warning","true","compare logs",true\n',
            encoding='utf-8',
        )
        parsed = normalize_xml_state_template_draft(draft, normalized)
        candidates = scan_source_xml_state_candidates(project, [project / 'app' / 'src' / 'main'], ['lock'], max_candidates=10)
        errors = validate_generated_xml_state_templates(parsed['items'], project, candidates)

        self.assertEqual(len(parsed['items']), 1)
        self.assertFalse(parsed['parse_errors'])
        self.assertFalse(errors)
        self.assertTrue(normalized.read_text(encoding='utf-8').strip().startswith('{'))

    def test_xml_state_batch_pipeline_dry_run_uses_external_output_dir(self):
        project = self.root / 'xml-state-batch-project'
        source_dir = project / 'app' / 'src' / 'main' / 'java' / 'com' / 'example'
        source_dir.mkdir(parents=True)
        (source_dir / 'DeviceFlow.java').write_text(
            '''
package com.example;
import android.content.Context;
import android.content.SharedPreferences;
class DeviceFlow {
  private static final String FILE_NAME = "user_state";
  private static final String TAG_LOCK_STATE = "lock_state";
  void lock(Context context, int state) {
    SharedPreferences sp = context.getSharedPreferences(FILE_NAME, Context.MODE_PRIVATE);
    sp.edit().putInt(TAG_LOCK_STATE, state).apply();
  }
}
''',
            encoding='utf-8',
        )
        create_project_knowledge_scaffold(
            project,
            module={
                'id': 'android-rdm',
                'title': 'RDM',
                'description': 'RDM flows.',
                'source_roots': ['app/src/main'],
            },
            subcategories=[{'id': 'lock_state_abnormal', 'title': 'Lock state abnormal', 'description': 'Lock state persisted locally.'}],
            overwrite=True,
            include_skill=False,
        )
        output_dir = self.root / 'server-workbench' / 'rdm-xml'

        result = run_xml_state_template_batch_generation_pipeline(
            project,
            output_dir=output_dir,
            claude_cli_path='claude-not-called',
            per_subcategory_max_candidates=5,
            dry_run=True,
        )

        self.assertTrue(result['ok'])
        self.assertEqual(result['subcategory_count'], 1)
        self.assertGreaterEqual(result['candidate_count'], 1)
        self.assertTrue(Path(result['candidate_paths']['jsonl']).is_file())
        self.assertTrue(Path(result['prompt_path']).is_file())
        self.assertFalse((project / '.claude-web' / 'android-analysis' / 'xml_state_candidates.all.prefiltered.jsonl').exists())

    def test_xml_state_matcher_appends_events_to_evidence_pack(self):
        project, knowledge = self.make_expert_knowledge_project()
        (knowledge / 'xml_state_templates.jsonl').write_text(
            json.dumps(
                {
                    'id': 'rdm-user-state-lock-state',
                    'module_id': 'android-rdm',
                    'subcategory_id': 'activation_eula',
                    'profile': 'functional',
                    'source_type': 'shared_prefs_xml',
                    'path_patterns': ['(?i)shared_prefs.*user_state.*\\.xml$'],
                    'key_regex': '^lock_state$',
                    'value_regex': '^1$',
                    'value_source': 'shared_prefs_value',
                    'code_location': 'DeviceLockStateManagerImpl#saveDeviceStateToLocalSp',
                    'meaning': 'Local lock state is locked.',
                    'severity': 'critical',
                    'time_anchor': True,
                    'next_steps': ['Compare lock task logs'],
                },
                ensure_ascii=False,
            )
            + '\n',
            encoding='utf-8',
        )
        cache = build_expert_knowledge_cache(
            [{'id': 'android-rdm', 'title': 'RDM', 'paths': [str(project)]}],
            self.root / 'android_analysis_knowledge',
        )
        extracted = self.root / 'extracted'
        artifacts = self.root / 'artifacts'
        prefs = extracted / 'data' / 'shared_prefs'
        prefs.mkdir(parents=True)
        (prefs / 'user_state.xml').write_text('<map><int name="lock_state" value="1" /></map>', encoding='utf-8')
        profile_extracted_tree(extracted, artifacts)
        (artifacts / 'planner_result.json').write_text(
            json.dumps({'candidate_bundle_ids': ['android-rdm'], 'issue_types': ['android_business_spec']}),
            encoding='utf-8',
        )
        (artifacts / 'matched_rules.json').write_text(
            json.dumps({'version': 1, 'rule_pack_count': 0, 'event_count': 0, 'events': []}),
            encoding='utf-8',
        )

        result = run_xml_state_matching(extracted, artifacts, cache)
        evidence = generate_first_evidence_pack(artifacts, question='RDM lock failed')
        pack = (artifacts / 'first_evidence_pack.md').read_text(encoding='utf-8')

        self.assertEqual(result['stats']['matched_event_count'], 1)
        self.assertEqual(evidence['event_count'], 1)
        self.assertIn('Local lock state is locked.', pack)
        self.assertIn('Compare lock task logs', pack)

    def test_xml_state_matcher_uses_content_fallback_for_renamed_xml(self):
        project, knowledge = self.make_expert_knowledge_project()
        (knowledge / 'xml_state_templates.jsonl').write_text(
            json.dumps(
                {
                    'id': 'rdm-user-state-lock-state',
                    'module_id': 'android-rdm',
                    'subcategory_id': 'activation_eula',
                    'profile': 'functional',
                    'source_type': 'shared_prefs_xml',
                    'path_patterns': ['(?i)shared_prefs.*user_state.*\\.xml$'],
                    'key_regex': '^lock_state$',
                    'value_regex': '^1$',
                    'value_source': 'shared_prefs_value',
                    'code_location': 'DeviceLockStateManagerImpl#saveDeviceStateToLocalSp',
                    'meaning': 'Local lock state is locked.',
                    'severity': 'critical',
                    'next_steps': [],
                },
                ensure_ascii=False,
            )
            + '\n',
            encoding='utf-8',
        )
        cache = build_expert_knowledge_cache(
            [{'id': 'android-rdm', 'title': 'RDM', 'paths': [str(project)]}],
            self.root / 'android_analysis_knowledge',
        )
        extracted = self.root / 'extracted'
        artifacts = self.root / 'artifacts'
        extracted.mkdir()
        (extracted / 'renamed_state_dump.txt').write_text('<map><int name="lock_state" value="1" /></map>', encoding='utf-8')
        profile_extracted_tree(extracted, artifacts)
        (artifacts / 'planner_result.json').write_text(
            json.dumps({'candidate_bundle_ids': ['android-rdm'], 'issue_types': ['android_business_spec']}),
            encoding='utf-8',
        )
        (artifacts / 'matched_rules.json').write_text(
            json.dumps({'version': 1, 'rule_pack_count': 0, 'event_count': 0, 'events': []}),
            encoding='utf-8',
        )

        result = run_xml_state_matching(extracted, artifacts, cache)

        self.assertEqual(result['stats']['matched_event_count'], 1)
        self.assertEqual(result['stats']['path_fallback_match_count'], 1)
        self.assertEqual(result['events'][0]['path_match_mode'], 'content_fallback')

    def test_job_store_creates_expected_layout_and_events(self):
        store = AndroidAnalysisJobStore(self.root / 'session')
        job = store.create_job('crash after lock', source_files=['logs.zip'], bundle_ids=['android-rdm'])

        self.assertEqual(job['status'], 'initialized')
        self.assertTrue((store.job_dir(job['id']) / 'input').is_dir())
        self.assertTrue((store.job_dir(job['id']) / 'extracted').is_dir())
        self.assertTrue((store.job_dir(job['id']) / 'artifacts').is_dir())

        updated = store.update_job(job['id'], status='profiled', artifacts={'file_tree': 'artifacts/file_tree.json'})
        self.assertEqual(updated['status'], 'profiled')
        events = (store.job_dir(job['id']) / 'events.jsonl').read_text(encoding='utf-8').splitlines()
        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(json.loads(events[0])['type'], 'job_initialized')

    def test_safe_extract_and_profile_writes_manifest_and_tree(self):
        archive = self.make_zip(
            {
                'bugreport/logcat/main.log': '05-07 10:00:00 FATAL EXCEPTION: main\n',
                'anr/traces.txt': '----- pid 123 at 2026-05-07 -----\n',
                'logcat/events.log': '05-07 10:00:01 am_anr pid=123\n',
            }
        )
        extracted = self.root / 'extracted'
        artifacts = self.root / 'artifacts'

        result = safe_extract_archive(archive, extracted)
        profile = profile_extracted_tree(extracted, artifacts)

        self.assertEqual(result['files'], 3)
        self.assertEqual(profile['manifest']['file_count'], 3)
        kinds = {f['path']: f['kind'] for f in profile['manifest']['files']}
        self.assertEqual(kinds['bugreport/logcat/main.log'], 'android_main_log')
        self.assertEqual(kinds['anr/traces.txt'], 'android_anr_trace')
        self.assertEqual(kinds['logcat/events.log'], 'android_events_log')
        self.assertTrue((artifacts / 'file_manifest.json').is_file())
        self.assertTrue((artifacts / 'file_tree.json').is_file())

    def test_safe_extract_recovers_gbk_zip_member_names(self):
        display_name = '\u65e5\u5fd7/main.log'
        archive_name = display_name.encode('gbk').decode('cp437')
        archive = self.make_zip({archive_name: 'AndroidRuntime FATAL EXCEPTION: main\n'})

        extracted = self.root / 'extracted'
        safe_extract_archive(archive, extracted)

        self.assertTrue((extracted / display_name).is_file())
        self.assertFalse((extracted / archive_name).exists())

    def test_sampler_writes_head_tail_and_keyword_context(self):
        extracted = self.root / 'extracted'
        artifacts = self.root / 'artifacts'
        extracted.mkdir()
        lines = [f'05-07 10:00:{i:02d} normal line {i}' for i in range(20)]
        lines.insert(10, '05-07 10:00:10 AndroidRuntime FATAL EXCEPTION: main')
        (extracted / 'main.log').write_text('\n'.join(lines), encoding='utf-8')
        profile_extracted_tree(extracted, artifacts)

        traces = []
        samples = sample_files(
            extracted,
            artifacts,
            question='RDM lock crash',
            debug_trace=lambda stage, event, data: traces.append((stage, event, data)),
        )

        self.assertEqual(samples['file_count'], 1)
        sample_types = [s['type'] for s in samples['files'][0]['samples']]
        self.assertIn('head', sample_types)
        self.assertIn('tail', sample_types)
        keyword_samples = [s for s in samples['files'][0]['samples'] if s['type'] == 'keyword']
        self.assertTrue(keyword_samples)
        self.assertIn('FATAL EXCEPTION', keyword_samples[0]['content'])
        self.assertTrue((artifacts / 'file_samples.json').is_file())
        self.assertIn(('sampling', 'keyword_plan'), [(s, e) for s, e, _ in traces])
        sampling_result = next(data for _, event, data in traces if event == 'sampling_result')
        self.assertGreaterEqual(sampling_result['keyword_hit_counts'].get('FATAL EXCEPTION', 0), 1)

    def test_sampler_skips_binary_files(self):
        extracted = self.root / 'extracted'
        artifacts = self.root / 'artifacts'
        extracted.mkdir()
        (extracted / 'main.log').write_bytes(b'\x00\x01\x02binary')
        profile_extracted_tree(extracted, artifacts)

        samples = sample_files(extracted, artifacts)

        self.assertEqual(samples['files'][0]['skipped'], True)
        self.assertEqual(samples['files'][0]['skip_reason'], 'binary')

    def test_large_noisy_log_is_bounded_and_no_evidence_stays_low_confidence(self):
        extracted = self.root / 'extracted'
        artifacts = self.root / 'artifacts'
        knowledge = self.root / 'knowledge'
        extracted.mkdir()
        noise = '\n'.join(f'05-07 10:{i % 60:02d}:00 noise line {i}' for i in range(6000))
        (extracted / 'main.log').write_text(noise, encoding='utf-8')
        profile_extracted_tree(extracted, artifacts)
        samples = sample_files(extracted, artifacts, question='RDM lock failed')
        sampled_chars = sum(
            len(str(sample.get('content') or ''))
            for file_item in samples.get('files') or []
            for sample in file_item.get('samples') or []
        )
        (artifacts / 'planner_result.json').write_text(
            json.dumps(
                {
                    'issue_types': ['android_business_spec'],
                    'candidate_bundle_ids': ['android-rdm'],
                    'candidate_rule_packs': ['rdm-base'],
                    'candidate_log_paths': ['main.log'],
                    'candidate_keywords': ['lock'],
                    'candidate_entities': {},
                    'exclude_paths': [],
                    'confidence': 0.3,
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
        matched = run_rule_matching(artifacts, knowledge, question='RDM lock failed')
        evidence = generate_first_evidence_pack(artifacts, question='RDM lock failed')
        (artifacts / 'case_cards.json').write_text(json.dumps({'cards': []}), encoding='utf-8')
        report = generate_first_report(artifacts, question='RDM lock failed', enable_ai=False)

        self.assertLess(sampled_chars, 60000)
        self.assertEqual(matched['event_count'], 0)
        self.assertFalse(evidence['has_evidence'])
        self.assertEqual(report['report_mode'], 'fallback')
        self.assertIn('暂无明确证据', (artifacts / 'final_report.md').read_text(encoding='utf-8'))

    def test_sampler_reads_gzip_log_text(self):
        extracted = self.root / 'extracted'
        artifacts = self.root / 'artifacts'
        extracted.mkdir()
        with gzip.open(extracted / 'applogcat-log.I000.gz', 'wt', encoding='utf-8') as f:
            f.write('AndroidRuntime FATAL EXCEPTION: main\n')
        profile_extracted_tree(extracted, artifacts)

        samples = sample_files(extracted, artifacts)

        self.assertFalse(samples['files'][0]['skipped'])
        content = '\n'.join(s['content'] for s in samples['files'][0]['samples'])
        self.assertIn('FATAL EXCEPTION', content)

    def test_sampler_prioritizes_planner_paths_over_noisy_crashes(self):
        extracted = self.root / 'extracted'
        artifacts = self.root / 'artifacts'
        extracted.mkdir()
        noisy_dir = extracted / 'dropbox'
        noisy_dir.mkdir()
        for i in range(12):
            (noisy_dir / f'system_app_crash@{i}.txt').write_text(
                'FATAL EXCEPTION: main\nProcess: com.tencent.mm\nCaused by: noise\n',
                encoding='utf-8',
            )
        prefs = extracted / 'PNM-N49' / 'HnLock' / 'DE' / 'shared_prefs'
        prefs.mkdir(parents=True)
        (prefs / 'DeviceLock_preferences.xml').write_text(
            '<map><boolean name="device_lock_enabled" value="true"/></map>',
            encoding='utf-8',
        )
        profile_extracted_tree(extracted, artifacts)

        samples = sample_files(
            extracted,
            artifacts,
            question='RDM 锁定解锁有没有问题',
            keywords=['DeviceLock'],
            priority_paths=['PNM-N49/HnLock/DE/shared_prefs/DeviceLock_preferences.xml'],
        )

        sampled_paths = [f['path'] for f in samples['files']]
        self.assertIn('PNM-N49/HnLock/DE/shared_prefs/DeviceLock_preferences.xml', sampled_paths)
        self.assertLess(sampled_paths.index('PNM-N49/HnLock/DE/shared_prefs/DeviceLock_preferences.xml'), 3)

    def test_sampler_reads_utf16_xml_state_files(self):
        extracted = self.root / 'extracted'
        artifacts = self.root / 'artifacts'
        extracted.mkdir()
        (extracted / 'device_policy_state.xml').write_text(
            '<policies><owner package="com.hihonor.realtimedevicemanager"/></policies>',
            encoding='utf-16-le',
        )
        profile_extracted_tree(extracted, artifacts)

        samples = sample_files(extracted, artifacts, keywords=['realtimedevicemanager'])

        self.assertFalse(samples['files'][0]['skipped'])
        content = '\n'.join(s['content'] for s in samples['files'][0]['samples'])
        self.assertIn('realtimedevicemanager', content)

    def test_planner_validates_json_and_filters_unsafe_paths(self):
        raw = '''
        Here is JSON:
        ```json
        {
          "issue_types": ["android_app_crash", "bad_type"],
          "candidate_bundle_ids": ["android-rdm"],
          "candidate_rule_packs": ["rdm-base"],
          "candidate_log_paths": ["logcat/main.log", "../evil", "C:/secret"],
          "candidate_keywords": ["FATAL EXCEPTION"],
          "candidate_entities": {"package": "com.example"},
          "exclude_paths": ["/absolute"],
          "confidence": 2,
          "need_user_clarification": false
        }
        ```
        '''
        result = validate_planner_result(parse_planner_json(raw))

        self.assertEqual(result['issue_types'], ['android_app_crash'])
        self.assertEqual(result['candidate_log_paths'], ['logcat/main.log'])
        self.assertEqual(result['confidence'], 1.0)

    def test_planner_fallback_and_ai_runner_write_result(self):
        extracted = self.root / 'extracted'
        artifacts = self.root / 'artifacts'
        extracted.mkdir()
        (extracted / 'main.log').write_text('AndroidRuntime FATAL EXCEPTION: main\nRDM lock failed\n', encoding='utf-8')
        profile_extracted_tree(extracted, artifacts)
        sample_files(extracted, artifacts, question='RDM lock crash')
        bundles = [
            {
                'id': 'android-rdm',
                'title': 'RealtimeDeviceManager',
                'description': 'RDM lock unlock provision',
                'rule_packs': ['rdm-base'],
            }
        ]

        fallback = run_planner(
            artifacts,
            'RDM lock crash',
            bundles=bundles,
            requested_bundle_ids=['android-rdm'],
            enable_ai=False,
        )
        self.assertEqual(fallback['planner_mode'], 'fallback')
        self.assertIn('android_app_crash', fallback['issue_types'])
        self.assertIn('android-rdm', fallback['candidate_bundle_ids'])
        self.assertTrue((artifacts / 'planner_result.json').is_file())

        ai = run_planner(
            artifacts,
            'RDM lock crash',
            bundles=bundles,
            requested_bundle_ids=['android-rdm'],
            enable_ai=True,
            ai_runner=lambda _: json.dumps(
                {
                    'issue_types': ['android_business_spec'],
                    'candidate_bundle_ids': ['android-rdm'],
                    'candidate_rule_packs': ['rdm-base'],
                    'candidate_log_paths': ['main.log'],
                    'candidate_keywords': ['lock'],
                    'candidate_entities': {},
                    'exclude_paths': [],
                    'confidence': 0.8,
                    'need_user_clarification': False,
                }
            ),
        )
        self.assertEqual(ai['planner_mode'], 'ai')
        self.assertEqual(ai['confidence'], 0.8)

    def test_planner_fallback_keeps_requested_bundle_boundary(self):
        extracted = self.root / 'extracted'
        artifacts = self.root / 'artifacts'
        extracted.mkdir()
        (extracted / 'main.log').write_text(
            'AndroidRuntime FATAL EXCEPTION: main\nnextcloud sync failed\n',
            encoding='utf-8',
        )
        profile_extracted_tree(extracted, artifacts)
        sample_files(extracted, artifacts, question='RDM lock crash')
        bundles = [
            {'id': 'android-rdm', 'title': 'RDM', 'description': 'lock unlock', 'rule_packs': ['rdm-base']},
            {'id': 'app-nextcloud', 'title': 'Nextcloud', 'description': 'nextcloud sync', 'rule_packs': ['nextcloud-generated']},
        ]

        result = run_planner(
            artifacts,
            'RDM lock crash',
            bundles=bundles,
            requested_bundle_ids=['android-rdm'],
            enable_ai=False,
        )

        self.assertEqual(result['candidate_bundle_ids'], ['android-rdm'])
        self.assertEqual(result['candidate_rule_packs'], ['rdm-base'])

    def test_planner_fallback_routes_fwk_xts_by_question_before_noisy_watchdog(self):
        extracted = self.root / 'extracted'
        artifacts = self.root / 'artifacts'
        extracted.mkdir()
        (extracted / 'device_logcat_setup.txt').write_text(
            'I Watchdog: noisy heartbeat only\nI PackageManager: query package visibility\n',
            encoding='utf-8',
        )
        profile_extracted_tree(extracted, artifacts)
        sample_files(extracted, artifacts, question='XTS DPM testHideAllApps failed')
        bundles = [
            {
                'id': 'android-fwk',
                'title': 'FWK',
                'description': 'framework DevicePolicy PackageManager DLC ManagedProvisioning',
                'rule_packs': [
                    'fwk-ams-generated',
                    'fwk-pms-generated',
                    'fwk-devicepolicy-generated',
                    'fwk-managedprovisioning-generated',
                    'fwk-devicelock-generated',
                    'fwk-oem-honor-generated',
                ],
            }
        ]

        result = run_planner(
            artifacts,
            'XTS DPM testHideAllApps failed',
            bundles=bundles,
            requested_bundle_ids=['android-fwk'],
            enable_ai=False,
        )

        self.assertIn('android_test_failure', result['issue_types'])
        self.assertNotIn('android_system_server_crash', result['issue_types'])
        self.assertEqual(result['candidate_rule_packs'], ['fwk-devicepolicy-generated', 'fwk-pms-generated'])
        self.assertIn('testHideAllApps', result['candidate_keywords'])

    def test_planner_fallback_routes_fwk_dlc_to_devicelock_rules(self):
        extracted = self.root / 'extracted'
        artifacts = self.root / 'artifacts'
        extracted.mkdir()
        (extracted / 'device_logcat_main.txt').write_text(
            'I ActivityManager: system_server alive\nI DevicePolicyManager: policy updated\n',
            encoding='utf-8',
        )
        profile_extracted_tree(extracted, artifacts)
        sample_files(extracted, artifacts, question='DLC lock activation failed')
        bundles = [
            {
                'id': 'android-fwk',
                'title': 'FWK',
                'description': 'framework DevicePolicy PackageManager DLC ManagedProvisioning',
                'rule_packs': [
                    'fwk-ams-generated',
                    'fwk-pms-generated',
                    'fwk-devicepolicy-generated',
                    'fwk-managedprovisioning-generated',
                    'fwk-devicelock-generated',
                    'fwk-oem-honor-generated',
                ],
            }
        ]

        result = run_planner(
            artifacts,
            'DLC lock activation failed',
            bundles=bundles,
            requested_bundle_ids=['android-fwk'],
            enable_ai=False,
        )

        self.assertIn('android_framework_behavior', result['issue_types'])
        self.assertNotIn('android_system_server_crash', result['issue_types'])
        self.assertEqual(result['candidate_rule_packs'], ['fwk-devicelock-generated', 'fwk-devicepolicy-generated'])
        self.assertIn('DeviceLock', result['candidate_keywords'])

    def test_ai_token_usage_trace_supports_estimate_and_stream_usage(self):
        artifacts = self.root / 'artifacts'
        extracted = self.root / 'extracted'
        artifacts.mkdir()
        extracted.mkdir()
        (extracted / 'main.log').write_text('RDM lock failed\n', encoding='utf-8')
        profile_extracted_tree(extracted, artifacts)
        sample_files(extracted, artifacts, question='RDM lock failed')

        traces = []
        run_planner(
            artifacts,
            'RDM lock failed',
            bundles=[{'id': 'android-rdm', 'title': 'RDM'}],
            requested_bundle_ids=['android-rdm'],
            enable_ai=True,
            ai_runner=lambda _: json.dumps(
                {
                    'issue_types': ['android_business_spec'],
                    'candidate_bundle_ids': ['android-rdm'],
                    'candidate_rule_packs': [],
                    'candidate_log_paths': ['main.log'],
                    'candidate_keywords': ['lock'],
                    'candidate_entities': {},
                    'exclude_paths': [],
                    'confidence': 0.7,
                    'need_user_clarification': False,
                }
            ),
            debug_trace=lambda stage, event, data: traces.append((stage, event, data)),
        )
        usage_events = [data for _, event, data in traces if event == 'ai_token_usage']
        self.assertEqual(len(usage_events), 1)
        self.assertEqual(usage_events[0]['interaction'], 'planner')
        self.assertEqual(usage_events[0]['token_source'], 'estimate')
        self.assertGreater(usage_events[0]['input_tokens'], 0)
        self.assertGreater(usage_events[0]['output_tokens'], 0)

        _, stream_usage = _collect_stream_json(
            '\n'.join(
                [
                    json.dumps({'type': 'message', 'role': 'assistant', 'content': '{"ok":true}'}),
                    json.dumps({'type': 'result', 'usage': {'input_tokens': 12, 'output_tokens': 3, 'cache_read_input_tokens': 4}}),
                ]
            )
        )
        self.assertEqual(stream_usage['input_tokens'], 12)
        self.assertEqual(stream_usage['output_tokens'], 3)
        self.assertEqual(stream_usage['cache_read_input_tokens'], 4)

    def test_planner_prompt_budget_records_component_metrics(self):
        artifacts = self.root / 'artifacts'
        artifacts.mkdir()
        files = [
            {
                'path': f'logs/module_{i}/main_{i}.log',
                'name': f'main_{i}.log',
                'size': 4096,
                'kind': 'android_main_log',
            }
            for i in range(80)
        ]
        manifest = {'version': 1, 'root': '.', 'file_count': len(files), 'total_size': 4096 * len(files), 'files': files}
        tree = {
            'version': 1,
            'root': {
                'name': '.',
                'type': 'directory',
                'children': [
                    {
                        'name': f'module_{i}',
                        'type': 'directory',
                        'children': [
                            {
                                'name': f'main_{i}.log',
                                'type': 'file',
                                'path': f'logs/module_{i}/main_{i}.log',
                                'size': 4096,
                                'kind': 'android_main_log',
                            }
                        ],
                    }
                    for i in range(80)
                ],
            },
        }
        samples = {
            'version': 1,
            'keyword_set': ['FATAL EXCEPTION', 'module', 'failure'],
            'file_count': 80,
            'files': [
                {
                    'path': f'logs/module_{i}/main_{i}.log',
                    'kind': 'android_main_log',
                    'size': 4096,
                    'samples': [
                        {
                            'type': 'keyword',
                            'keyword': 'failure',
                            'start_line': 1,
                            'end_line': 30,
                            'content': ('failure in generic android module\n' * 120),
                        }
                    ],
                }
                for i in range(80)
            ],
        }
        (artifacts / 'file_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        (artifacts / 'file_tree.json').write_text(json.dumps(tree, ensure_ascii=False), encoding='utf-8')
        (artifacts / 'file_samples.json').write_text(json.dumps(samples, ensure_ascii=False), encoding='utf-8')

        prompts = []
        traces = []
        result = run_planner(
            artifacts,
            'generic app failure after launch',
            bundles=[{'id': 'android-app', 'title': 'GenericApp', 'description': 'generic app launch failure'}],
            requested_bundle_ids=['android-app'],
            enable_ai=True,
            prompt_limits=PlannerPromptLimits(
                prompt_budget_chars=15000,
                max_tree_nodes=40,
                max_sample_files=12,
                max_sample_chars=12000,
            ),
            ai_runner=lambda prompt: prompts.append(prompt)
            or json.dumps(
                {
                    'issue_types': ['generic_log_error'],
                    'candidate_bundle_ids': ['android-app'],
                    'candidate_rule_packs': [],
                    'candidate_log_paths': ['logs/module_1/main_1.log'],
                    'candidate_keywords': ['failure'],
                    'candidate_entities': {},
                    'exclude_paths': [],
                    'confidence': 0.6,
                    'need_user_clarification': False,
                }
            ),
            debug_trace=lambda stage, event, data: traces.append((stage, event, data)),
        )

        self.assertEqual(result['planner_mode'], 'ai')
        self.assertLessEqual(len(prompts[0]), 15000)
        metrics = json.loads((artifacts / 'planner_prompt_metrics.json').read_text(encoding='utf-8'))
        self.assertTrue(metrics['clipping']['budget_applied'])
        self.assertIn('file_samples_chars', metrics['component_chars'])
        planner_input = next(data for _, event, data in traces if event == 'planner_input')
        self.assertIn('prompt_component_chars', planner_input)
        self.assertLessEqual(planner_input['prompt_chars'], 15000)

    def test_ai_visible_stream_fragments_can_be_traced(self):
        fragments = []
        _emit_stream_trace(
            {
                'type': 'stream_event',
                'event': {
                    'type': 'content_block_delta',
                    'delta': {'type': 'thinking_delta', 'thinking': 'checking files'},
                },
            },
            fragments.append,
        )
        _emit_stream_trace(
            {
                'type': 'stream_event',
                'event': {
                    'type': 'content_block_delta',
                    'delta': {'type': 'text_delta', 'text': 'done'},
                },
            },
            fragments.append,
        )

        self.assertEqual(fragments[0]['kind'], 'thinking')
        self.assertEqual(fragments[0]['content'], 'checking files')
        self.assertEqual(fragments[1]['kind'], 'text')
        self.assertEqual(fragments[1]['content'], 'done')

    def test_rule_matching_and_evidence_pack_use_bounded_samples(self):
        extracted = self.root / 'extracted'
        artifacts = self.root / 'artifacts'
        knowledge = self.root / 'knowledge'
        extracted.mkdir()
        (extracted / 'main.log').write_text(
            '05-07 10:00:00 normal\n'
            '05-07 10:00:01 E AndroidRuntime: FATAL EXCEPTION: main\n'
            '05-07 10:00:01 E AndroidRuntime: java.lang.IllegalStateException: lock failed\n',
            encoding='utf-8',
        )
        profile_extracted_tree(extracted, artifacts)
        sample_files(extracted, artifacts, question='lock crash', keywords=['FATAL EXCEPTION'])
        (artifacts / 'planner_result.json').write_text(
            json.dumps(
                {
                    'issue_types': ['android_app_crash'],
                    'candidate_bundle_ids': [],
                    'candidate_rule_packs': [],
                    'candidate_log_paths': ['main.log'],
                    'candidate_keywords': ['FATAL EXCEPTION'],
                    'candidate_entities': {},
                    'exclude_paths': [],
                    'confidence': 0.7,
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )

        traces = []
        matched = run_rule_matching(
            artifacts,
            knowledge,
            question='lock crash',
            debug_trace=lambda stage, event, data: traces.append((stage, event, data)),
        )
        evidence = generate_first_evidence_pack(
            artifacts,
            question='lock crash',
            debug_trace=lambda stage, event, data: traces.append((stage, event, data)),
        )

        self.assertGreaterEqual(matched['event_count'], 1)
        self.assertEqual(matched['events'][0]['issue_type'], 'android_app_crash')
        self.assertIn('FATAL EXCEPTION', matched['events'][0]['snippet'])
        self.assertTrue(evidence['has_evidence'])
        self.assertTrue((artifacts / 'matched_rules.json').is_file())
        self.assertTrue((artifacts / 'first_evidence_pack.md').is_file())
        event_names = [event for _, event, _ in traces]
        self.assertIn('rule_pack_selection', event_names)
        self.assertIn('matching_result', event_names)
        self.assertIn('first_evidence_pack_result', event_names)

    def test_rule_matching_centers_snippet_on_visible_hit(self):
        extracted = self.root / 'extracted'
        artifacts = self.root / 'artifacts'
        knowledge = self.root / 'knowledge'
        extracted.mkdir()
        events_log = '\n'.join(
            [f'01-01 11:24:{i:02d}.000 system noise {i}' for i in range(35)]
            + ['01-01 11:24:19.569 wm_create_activity com.hihonor.realtimedevicemanager/.ui.LockActivity']
            + [f'01-01 11:25:{i:02d}.000 more noise {i}' for i in range(35)]
        )
        (extracted / 'eventslogcat-log').write_text(events_log, encoding='utf-8')
        profile_extracted_tree(extracted, artifacts)
        sample_files(extracted, artifacts, question='RDM lock', keywords=['LockActivity'])
        (artifacts / 'planner_result.json').write_text(
            json.dumps(
                {
                    'issue_types': ['android_framework_behavior'],
                    'candidate_bundle_ids': ['android-rdm'],
                    'candidate_rule_packs': ['rdm-generated'],
                    'candidate_log_paths': ['eventslogcat-log'],
                    'candidate_keywords': ['LockActivity'],
                    'candidate_entities': {},
                    'exclude_paths': [],
                    'confidence': 1.0,
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
        bundle_dir = knowledge / 'bundles' / 'android-rdm' / 'rules'
        bundle_dir.mkdir(parents=True)
        (bundle_dir.parent / 'bundle.json').write_text(
            json.dumps({'id': 'android-rdm', 'rule_packs': ['rdm-generated']}, ensure_ascii=False),
            encoding='utf-8',
        )
        (bundle_dir / 'rdm-generated.json').write_text(
            json.dumps(
                {
                    'id': 'rdm-generated',
                    'source_bundle_ids': ['android-rdm'],
                    'rules': [
                        {
                            'id': 'rdm-component',
                            'title': 'RDM component',
                            'issue_type': 'android_framework_behavior',
                            'match': {'keywords': ['LockActivity']},
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )

        matched = run_rule_matching(artifacts, knowledge, question='RDM lock')

        self.assertGreaterEqual(matched['event_count'], 1)
        self.assertIn('LockActivity', matched['events'][0]['snippet'])
        self.assertTrue(matched['events'][0]['hit_visibility']['snippet_contains_hit'])

    def test_rdm_focus_demotes_unrelated_third_party_crash(self):
        planner = {
            'issue_types': ['android_app_crash'],
            'candidate_bundle_ids': ['android-rdm'],
            'candidate_log_paths': ['dropbox/system_app_crash.txt'],
            'confidence': 1.0,
        }
        unrelated = {
            'issue_type': 'android_app_crash',
            'severity': 'high',
            'source_bundle_ids': [],
            'path': 'dropbox/system_app_crash.txt',
            'matched_terms': ['Caused by:', 'Exception'],
            'snippet': 'Process: com.tencent.mm\nTinkerRuntimeException: dlopen failed',
        }
        rdm_signal = {
            'issue_type': 'android_business_spec',
            'severity': 'medium',
            'source_bundle_ids': ['android-rdm'],
            'path': 'android_logs/eventslogcat-log.I000',
            'matched_terms': ['lock'],
            'snippet': 'DeviceLock lock failed while starting LockActivity',
        }

        unrelated_score = score_event_relevance(unrelated, planner, question='看这个是不是锁定失败了')
        rdm_score = score_event_relevance(rdm_signal, planner, question='看这个是不是锁定失败了')

        self.assertLess(unrelated_score['score'], rdm_score['score'])
        self.assertIn('demoted: no requested bundle/question focus overlap', unrelated_score['reasons'])

    def test_broad_framework_signal_is_demoted_for_xts_route(self):
        planner = {
            'issue_types': ['android_test_failure', 'android_framework_behavior'],
            'candidate_bundle_ids': ['android-fwk'],
            'candidate_log_paths': ['device_logcat_test.txt'],
            'candidate_keywords': ['testHideAllApps', 'DevicePolicyManager'],
            'confidence': 0.85,
        }
        broad = {
            'rule_id': 'system-server-watchdog',
            'issue_type': 'android_system_server_crash',
            'severity': 'fatal',
            'source_bundle_ids': [],
            'path': 'device_logcat_test.txt',
            'matched_terms': ['system_server'],
            'snippet': 'system_server normal boot noise',
        }
        specific = {
            'rule_id': 'fwk-devicepolicy-testhideallapps',
            'issue_type': 'android_framework_behavior',
            'severity': 'medium',
            'source_bundle_ids': ['android-fwk'],
            'path': 'device_logcat_test.txt',
            'matched_terms': ['DevicePolicyManager'],
            'snippet': 'DevicePolicyManager testHideAllApps failed for hidden packages',
        }

        broad_score = score_event_relevance(broad, planner, question='XTS-DPM testHideAllApps failed')
        specific_score = score_event_relevance(specific, planner, question='XTS-DPM testHideAllApps failed')

        self.assertLess(broad_score['score'], specific_score['score'])
        self.assertIn('demoted: broad framework/generic signal without planner keyword', broad_score['reasons'])

    def test_android_base_package_install_requires_install_failure_signal(self):
        extracted = self.root / 'extracted'
        artifacts = self.root / 'artifacts'
        knowledge = self.root / 'knowledge'
        extracted.mkdir()
        knowledge.mkdir()
        (extracted / 'device_logcat_test.txt').write_text(
            'I PackageManager: query package visibility for testHideAllApps\n',
            encoding='utf-8',
        )
        profile_extracted_tree(extracted, artifacts)
        sample_files(extracted, artifacts, question='XTS DPM testHideAllApps failed')
        (artifacts / 'planner_result.json').write_text(
            json.dumps(
                {
                    'issue_types': ['android_test_failure', 'android_framework_behavior'],
                    'candidate_bundle_ids': ['android-fwk'],
                    'candidate_rule_packs': [],
                    'candidate_log_paths': ['device_logcat_test.txt'],
                    'candidate_keywords': ['testHideAllApps', 'PackageManager'],
                    'confidence': 0.7,
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )

        matched = run_rule_matching(artifacts, knowledge, question='XTS DPM testHideAllApps failed')

        self.assertNotIn('package-install-failure', [event.get('rule_id') for event in matched.get('events') or []])

    def test_rule_loader_loads_requested_bundle_rule_pack(self):
        knowledge = self.root / 'knowledge'
        bundle_dir = knowledge / 'bundles' / 'android-rdm'
        bundle_dir.mkdir(parents=True)
        (bundle_dir / 'bundle.json').write_text(
            json.dumps({'id': 'android-rdm', 'rule_packs': ['rdm-base']}, ensure_ascii=False),
            encoding='utf-8',
        )
        rules_dir = bundle_dir / 'rules'
        rules_dir.mkdir(parents=True)
        (rules_dir / 'rdm-base.json').write_text(
            json.dumps(
                {
                    'id': 'rdm-base',
                    'title': 'RDM Base',
                    'source_bundle_ids': ['android-rdm'],
                    'rules': [
                        {
                            'id': 'rdm-lock-failure',
                            'title': 'RDM lock failure',
                            'issue_type': 'android_business_spec',
                            'match': {'keywords': ['DeviceLock']},
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
        (rules_dir / 'rdm-generated.json').write_text(
            json.dumps(
                {
                    'id': 'rdm-generated',
                    'title': 'Generated RDM Rules',
                    'source_bundle_ids': ['android-rdm'],
                    'rules': [
                        {
                            'id': 'rdm-generated-noisy',
                            'title': 'Generated noisy rule',
                            'issue_type': 'generic_log_error',
                            'match': {'keywords': ['GeneratedNoise']},
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )

        packs = load_rule_packs(knowledge, candidate_rule_packs=[], candidate_bundle_ids=['android-rdm'])
        ids = [p['id'] for p in packs]

        self.assertIn('android-base', ids)
        self.assertIn('rdm-base', ids)
        self.assertNotIn('rdm-generated', ids)

        (bundle_dir / 'bundle.json').write_text(
            json.dumps({'id': 'android-rdm', 'rule_packs': ['rdm-base', 'rdm-generated']}, ensure_ascii=False),
            encoding='utf-8',
        )
        configured = load_rule_packs(knowledge, candidate_rule_packs=[], candidate_bundle_ids=['android-rdm'])
        configured_ids = [p['id'] for p in configured]
        self.assertIn('rdm-base', configured_ids)
        self.assertIn('rdm-generated', configured_ids)

        explicit = load_rule_packs(knowledge, candidate_rule_packs=['rdm-generated'], candidate_bundle_ids=['android-rdm'])
        explicit_ids = [p['id'] for p in explicit]
        self.assertIn('rdm-generated', explicit_ids)
        self.assertNotIn('rdm-base', explicit_ids)

    def test_case_recall_and_first_report_fallback(self):
        artifacts = self.root / 'artifacts'
        knowledge = self.root / 'knowledge'
        index_dir = knowledge / 'bundles' / 'android-rdm' / 'indexes'
        artifacts.mkdir()
        index_dir.mkdir(parents=True)
        planner = {
            'issue_types': ['android_app_crash'],
            'candidate_bundle_ids': ['android-rdm'],
            'candidate_keywords': ['lock'],
            'confidence': 0.7,
        }
        matched = {
            'event_count': 1,
            'events': [
                {
                    'rule_id': 'androidruntime-fatal-exception',
                    'rule_title': 'AndroidRuntime fatal exception',
                    'issue_type': 'android_app_crash',
                    'severity': 'fatal',
                    'source_bundle_ids': ['android-rdm'],
                    'tags': ['crash'],
                    'path': 'main.log',
                    'line_range': [10, 12],
                    'matched_terms': ['FATAL EXCEPTION'],
                    'relevance': {'score': 0.8},
                    'snippet': 'FATAL EXCEPTION: main',
                }
            ],
        }
        (index_dir / 'case_cards.jsonl').write_text(
            json.dumps(
                {
                    'id': 'case-1',
                    'title': 'Known RDM crash',
                    'issue_type': 'android_app_crash',
                    'source_bundle_ids': ['android-rdm'],
                    'tags': ['crash'],
                    'summary': 'Known lock flow crash.',
                },
                ensure_ascii=False,
            )
            + '\n',
            encoding='utf-8',
        )
        (artifacts / 'planner_result.json').write_text(json.dumps(planner), encoding='utf-8')
        (artifacts / 'matched_rules.json').write_text(json.dumps(matched), encoding='utf-8')
        (artifacts / 'first_evidence_pack.md').write_text('# Evidence\nFATAL EXCEPTION\n', encoding='utf-8')

        traces = []
        cards = recall_case_cards(
            knowledge,
            planner,
            matched,
            debug_trace=lambda stage, event, data: traces.append((stage, event, data)),
        )
        write_case_cards(artifacts, cards)
        report = generate_first_report(
            artifacts,
            question='RDM lock crash',
            enable_ai=False,
            debug_trace=lambda stage, event, data: traces.append((stage, event, data)),
        )

        self.assertEqual(cards['card_count'], 1)
        self.assertEqual(report['report_mode'], 'fallback')
        event_names = [event for _, event, _ in traces]
        self.assertIn('case_recall_result', event_names)
        self.assertIn('first_report_result', event_names)
        self.assertTrue((artifacts / 'case_cards.json').is_file())
        self.assertTrue((artifacts / 'final_report.md').is_file())
        self.assertIn('Android 问题首轮分析报告', (artifacts / 'final_report.md').read_text(encoding='utf-8'))

    def test_verifier_flags_report_evidence_conflict(self):
        artifacts = self.root / 'artifacts'
        artifacts.mkdir()
        planner = {
            'issue_types': ['android_framework_behavior'],
            'candidate_bundle_ids': ['android-rdm'],
            'confidence': 1.0,
        }
        matched = {
            'event_count': 1,
            'events': [
                {
                    'issue_type': 'android_framework_behavior',
                    'severity': 'medium',
                    'source_bundle_ids': ['android-rdm'],
                    'matched_terms': ['LockActivity'],
                    'snippet': 'wm_create_activity com.hihonor.realtimedevicemanager/.ui.LockActivity',
                    'hit_visibility': {
                        'sample_contains_hit': True,
                        'snippet_contains_hit': True,
                        'visible_terms': ['LockActivity'],
                    },
                    'relevance': {'score': 1.0},
                }
            ],
        }
        (artifacts / 'planner_result.json').write_text(json.dumps(planner), encoding='utf-8')
        (artifacts / 'matched_rules.json').write_text(json.dumps(matched), encoding='utf-8')
        (artifacts / 'first_evidence_pack.md').write_text('LockActivity evidence\n', encoding='utf-8')
        (artifacts / 'final_report.md').write_text('未发现 LockActivity 的 Activity 生命周期日志。\n', encoding='utf-8')

        result = run_verifier(artifacts, report_name='final_report.md', enable_ai=False)

        self.assertEqual(result['status'], 'partially_supported')
        self.assertEqual(result['overclaim_risk'], 'medium')
        self.assertTrue(any('LockActivity' in item for item in result['warnings']))

    def test_verifier_allows_positive_signal_with_missing_followup_logs(self):
        artifacts = self.root / 'artifacts'
        artifacts.mkdir()
        planner = {
            'issue_types': ['android_framework_behavior'],
            'candidate_bundle_ids': ['android-rdm'],
            'confidence': 1.0,
        }
        matched = {
            'event_count': 1,
            'events': [
                {
                    'rule_id': 'rdm-component',
                    'issue_type': 'android_framework_behavior',
                    'severity': 'medium',
                    'source_bundle_ids': ['android-rdm'],
                    'matched_terms': ['LockActivity'],
                    'snippet': 'wm_create_activity com.hihonor.realtimedevicemanager/.ui.LockActivity',
                    'hit_visibility': {
                        'sample_contains_hit': True,
                        'snippet_contains_hit': True,
                        'visible_terms': ['LockActivity'],
                    },
                    'relevance': {'score': 1.0},
                }
            ],
        }
        report = (
            'LockActivity 已被记录，说明锁定界面曾启动。\n'
            'LockActivity 启动后未维持前台：有启动记录但无后续日志确认锁定界面持续运行。\n'
        )
        (artifacts / 'planner_result.json').write_text(json.dumps(planner), encoding='utf-8')
        (artifacts / 'matched_rules.json').write_text(json.dumps(matched), encoding='utf-8')
        (artifacts / 'first_evidence_pack.md').write_text('LockActivity evidence\n', encoding='utf-8')
        (artifacts / 'final_report.md').write_text(report, encoding='utf-8')

        result = run_verifier(artifacts, report_name='final_report.md', enable_ai=False)

        self.assertFalse(any('LockActivity' in item for item in result['warnings']))
        self.assertEqual(result['overclaim_risk'], 'low')

    def test_deep_mode_uses_only_configured_bundle_code_scope_and_verifies(self):
        artifacts = self.root / 'artifacts'
        extracted = self.root / 'extracted'
        code_root = self.root / 'rdm-src'
        artifacts.mkdir()
        (extracted / 'logs').mkdir(parents=True)
        code_root.mkdir()
        (code_root / 'LockActivity.java').write_text(
            'package com.hihonor.rdm;\nclass LockActivity { void lockDevice() { DeviceLock.lock(); } }\n',
            encoding='utf-8',
        )
        (extracted / 'logs' / 'main.log').write_text('RDM lock failed at LockActivity\n', encoding='utf-8')
        planner = {
            'issue_types': ['android_business_spec'],
            'candidate_bundle_ids': ['android-rdm', 'missing-bundle'],
            'candidate_keywords': ['LockActivity', 'DeviceLock', 'lock'],
            'candidate_log_paths': ['logs/main.log'],
            'confidence': 0.7,
        }
        matched = {
            'event_count': 1,
            'events': [
                {
                    'rule_id': 'rdm-lock-keywords',
                    'rule_title': 'RDM lock flow',
                    'issue_type': 'android_business_spec',
                    'severity': 'medium',
                    'source_bundle_ids': ['android-rdm'],
                    'tags': ['lock'],
                    'path': 'logs/main.log',
                    'line_range': [1, 1],
                    'matched_terms': ['LockActivity', 'DeviceLock'],
                    'relevance': {'score': 0.8},
                    'snippet': 'RDM lock failed at LockActivity',
                }
            ],
        }
        (artifacts / 'planner_result.json').write_text(json.dumps(planner), encoding='utf-8')
        (artifacts / 'matched_rules.json').write_text(json.dumps(matched), encoding='utf-8')
        (artifacts / 'first_evidence_pack.md').write_text('# Evidence\nLockActivity\n', encoding='utf-8')
        (artifacts / 'final_report.md').write_text('# Android 问题首轮分析报告\n\n结论是 RDM 锁机失败。\n', encoding='utf-8')

        scope = resolve_code_scopes(
            ['android-rdm', 'missing-bundle'],
            [{'id': 'android-rdm', 'title': 'RDM', 'paths': [str(code_root)]}],
        )
        code = collect_code_context(scope, keywords=['LockActivity'])
        deep = build_deep_evidence_pack(
            artifacts,
            extracted,
            'RDM lock failed',
            configured_bundles=[{'id': 'android-rdm', 'title': 'RDM', 'paths': [str(code_root)]}],
        )
        report = generate_deep_report(artifacts, question='RDM lock failed', enable_ai=False)
        verifier = run_verifier(artifacts, report_name='deep_report.md', enable_ai=False)

        self.assertTrue(scope['allowed'])
        self.assertEqual(scope['denied'][0]['bundle_id'], 'missing-bundle')
        self.assertEqual(code[0]['path'], 'LockActivity.java')
        self.assertTrue(deep['has_code_context'])
        self.assertEqual(report['report_mode'], 'fallback')
        self.assertIn(verifier['status'], {'supported', 'partially_supported'})
        self.assertTrue((artifacts / 'deep_evidence_pack.md').is_file())
        self.assertTrue((artifacts / 'deep_report.md').is_file())
        self.assertTrue((artifacts / 'verifier_result.json').is_file())

    def test_deep_mode_consumes_generated_rule_pack_hints(self):
        artifacts = self.root / 'artifacts'
        extracted = self.root / 'extracted'
        code_root = self.root / 'rdm-src'
        artifacts.mkdir()
        (extracted / 'logs').mkdir(parents=True)
        (code_root / 'app' / 'src' / 'main' / 'java' / 'com' / 'example' / 'rdm').mkdir(parents=True)
        (code_root / 'app' / 'src' / 'main' / 'java' / 'com' / 'example' / 'rdm' / 'LockActivity.java').write_text(
            'package com.example.rdm;\nclass LockActivity { void lockDevice() { DeviceLock.lock(); } }\n',
            encoding='utf-8',
        )
        (code_root / 'CLAUDE.md').write_text(
            '# RDM Guide\n\nWhen LockActivity appears, inspect DeviceLock and the lock workflow first.\n',
            encoding='utf-8',
        )
        skill_dir = code_root / 'skills' / 'rdm-log-analysis'
        skill_dir.mkdir(parents=True)
        (skill_dir / 'SKILL.md').write_text(
            '\n'.join(
                [
                    '---',
                    'name: rdm-log-analysis',
                    'description: RDM project Deep log analysis workflow.',
                    '---',
                    '# RDM Project Skill',
                    '',
                    'Start from exact DeviceLock TAG/message evidence before expanding to code search.',
                ]
            ),
            encoding='utf-8',
        )
        (extracted / 'logs' / 'main.log').write_text('RDM lock failed at LockActivity\n', encoding='utf-8')
        planner = {
            'issue_types': ['android_business_spec'],
            'candidate_bundle_ids': ['android-rdm'],
            'candidate_keywords': ['lock'],
            'candidate_log_paths': ['logs/main.log'],
            'confidence': 0.7,
        }
        hints = {
            'version': 1,
            'code_search_terms': ['LockActivity', 'DeviceLock'],
            'tier2_scope_terms': ['com.example.rdm', 'DeviceLock'],
            'search_order': ['selected_project_skill', 'exact_logs', 'tier2_scope_terms', 'code_context'],
            'exact_logs': [{'tag': 'DeviceLock', 'message': 'lock failed', 'path': 'LockActivity.java'}],
            'preferred_paths': ['app/src/main/java', 'CLAUDE.md'],
            'related_skills': ['rdm-log-analysis', 'android-log-rule-builder', 'android-log-rule-builder:app'],
            'claude_md_candidates': ['CLAUDE.md'],
            'case_tags': ['functional', 'sync'],
        }
        matched = {
            'event_count': 1,
            'rule_pack_hints': [
                {
                    'rule_pack_id': 'rdm-generated',
                    'source_bundle_ids': ['android-rdm'],
                    'deep_hints': hints,
                }
            ],
            'events': [
                {
                    'rule_pack_id': 'rdm-generated',
                    'rule_id': 'rdm-lock-keywords',
                    'rule_title': 'RDM lock flow',
                    'issue_type': 'android_business_spec',
                    'severity': 'medium',
                    'source_bundle_ids': ['android-rdm'],
                    'tags': ['lock'],
                    'path': 'logs/main.log',
                    'line_range': [1, 1],
                    'matched_terms': ['LockActivity'],
                    'relevance': {'score': 0.8},
                    'deep_hints': hints,
                    'snippet': 'RDM lock failed at LockActivity',
                }
            ],
        }
        (artifacts / 'planner_result.json').write_text(json.dumps(planner), encoding='utf-8')
        (artifacts / 'matched_rules.json').write_text(json.dumps(matched), encoding='utf-8')
        (artifacts / 'final_report.md').write_text('# First report\nLockActivity needs review.\n', encoding='utf-8')

        deep = build_deep_evidence_pack(
            artifacts,
            extracted,
            'RDM lock failed',
            configured_bundles=[
                {
                    'id': 'android-rdm',
                    'title': 'RDM',
                    'paths': [str(code_root)],
                    'skills': [{'id': 'rdm-log-analysis', 'path': str(skill_dir)}],
                }
            ],
        )
        deep_md = (artifacts / 'deep_evidence_pack.md').read_text(encoding='utf-8')

        self.assertTrue(deep['has_code_context'])
        self.assertEqual(deep['selected_skill_count'], 1)
        self.assertEqual(deep['selected_guidance_count'], 1)
        self.assertIn('RDM Project Skill', deep_md)
        self.assertIn('inspect DeviceLock', deep_md)
        self.assertIn('android-log-rule-builder:app', deep['deep_hints']['related_skills'])
        self.assertIn('selected_project_skill', deep['deep_hints']['search_order'])
        self.assertEqual(deep['deep_hints']['exact_logs'][0]['tag'], 'DeviceLock')
        self.assertIn('app/src/main/java', deep['deep_hints']['preferred_paths_by_bundle']['android-rdm'])
        self.assertIn('Deep Hints / Priority', deep_md)
        self.assertIn('Selected Project Skills', deep_md)
        self.assertIn('Selected Project Guidance', deep_md)
        self.assertIn('CLAUDE.md', deep_md)
        self.assertIn('LockActivity.java', deep_md)

    def test_deep_mode_ranks_discovered_skills_by_question_relevance(self):
        artifacts = self.root / 'artifacts'
        extracted = self.root / 'extracted'
        code_root = self.root / 'fwk-src'
        artifacts.mkdir()
        extracted.mkdir()
        skills_root = code_root / '.claude' / 'skills'
        for skill_id, summary in {
            'cts-issue-analyzer': 'CTS XTS DevicePolicy test failure analyzer for test_result.xml and Tradefed logs.',
            'dlc-issue-analyzer': 'DeviceLock DLC lock activation check-in analyzer.',
            'devicepolicy-framework': 'DevicePolicyManagerService DPMS policy framework guide.',
        }.items():
            skill_dir = skills_root / skill_id
            skill_dir.mkdir(parents=True)
            (skill_dir / 'SKILL.md').write_text(
                f'---\nname: {skill_id}\ndescription: {summary}\n---\n# {skill_id}\n{summary}\n',
                encoding='utf-8',
            )
        (code_root / 'DevicePolicyManagerService.java').write_text('class DevicePolicyManagerService {}\n', encoding='utf-8')
        (artifacts / 'planner_result.json').write_text(
            json.dumps(
                {
                    'issue_types': ['android_test_failure', 'android_framework_behavior'],
                    'candidate_bundle_ids': ['android-fwk'],
                    'candidate_keywords': ['testHideAllApps', 'DevicePolicyManager'],
                    'candidate_log_paths': ['test_result.xml'],
                    'confidence': 0.8,
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
        (artifacts / 'matched_rules.json').write_text(json.dumps({'event_count': 0, 'events': []}), encoding='utf-8')

        deep = build_deep_evidence_pack(
            artifacts,
            extracted,
            'XTS-DPM testHideAllApps 测试失败',
            configured_bundles=[{'id': 'android-fwk', 'title': 'FWK', 'paths': [str(code_root)]}],
        )
        ids = [item['id'] for item in deep['selected_skills']]

        self.assertIn('cts-issue-analyzer', ids)
        self.assertIn('dlc-issue-analyzer', ids)
        self.assertLess(ids.index('cts-issue-analyzer'), ids.index('dlc-issue-analyzer'))

    def test_case_draft_and_confirm_writes_local_knowledge_index(self):
        artifacts = self.root / 'artifacts'
        knowledge = self.root / 'knowledge'
        bundle_dir = knowledge / 'bundles' / 'android-rdm'
        artifacts.mkdir()
        (bundle_dir / 'cases').mkdir(parents=True)
        (bundle_dir / 'indexes').mkdir(parents=True)
        (bundle_dir / 'drafts').mkdir(parents=True)
        planner = {
            'issue_types': ['android_business_spec'],
            'candidate_bundle_ids': ['android-rdm'],
            'candidate_keywords': ['lock'],
        }
        matched = {
            'event_count': 1,
            'events': [
                {
                    'rule_id': 'rdm-lock-keywords',
                    'rule_title': 'RDM lock flow',
                    'issue_type': 'android_business_spec',
                    'severity': 'medium',
                    'source_bundle_ids': ['android-rdm'],
                    'tags': ['lock'],
                    'path': 'main.log',
                    'line_range': [2, 3],
                    'matched_terms': ['lock'],
                    'relevance': {'score': 0.8},
                }
            ],
        }
        (artifacts / 'planner_result.json').write_text(json.dumps(planner), encoding='utf-8')
        (artifacts / 'matched_rules.json').write_text(json.dumps(matched), encoding='utf-8')
        (artifacts / 'final_report.md').write_text('# RDM 锁机失败\n\n结论是锁机流程异常。\n', encoding='utf-8')
        (artifacts / 'first_evidence_pack.md').write_text('# Evidence\nlock\n', encoding='utf-8')
        (artifacts / 'verifier_result.json').write_text(
            json.dumps({'status': 'partially_supported', 'overclaim_risk': 'medium', 'best_evidence_score': 0.8}),
            encoding='utf-8',
        )

        draft = generate_case_draft(artifacts, source_job_id='job-1')
        confirmed = confirm_case_draft(knowledge, artifacts, bundle_id='android-rdm', reviewer_note='ok')

        self.assertEqual(draft['status'], 'draft')
        self.assertTrue(draft['rule_candidates'])
        self.assertTrue((artifacts / 'case_draft.json').is_file())
        self.assertTrue((knowledge / confirmed['case_path']).is_file())
        index_text = (knowledge / confirmed['case_card_path']).read_text(encoding='utf-8')
        self.assertIn(confirmed['case_id'], index_text)

    def test_safe_extract_rejects_path_traversal(self):
        archive = self.make_zip({'../evil.txt': 'nope'})
        with self.assertRaises(AndroidAnalysisError) as ctx:
            safe_extract_archive(archive, self.root / 'extracted')
        self.assertEqual(ctx.exception.code, 'unsafe_path')
        self.assertFalse((self.root / 'evil.txt').exists())

    def test_safe_extract_rejects_limit_violations(self):
        archive = self.make_zip({'main.log': '1234567890'})
        with self.assertRaises(AndroidAnalysisError) as ctx:
            safe_extract_archive(
                archive,
                self.root / 'extracted',
                ExtractionLimits(max_file_size=5, max_total_size=100, max_archive_size=1000),
            )
        self.assertEqual(ctx.exception.code, 'file_too_large')


if __name__ == '__main__':
    unittest.main()
