import json
import gzip
import tempfile
import unittest
import zipfile
from pathlib import Path

from claude_web.android_analysis.archive import safe_extract_archive
from claude_web.android_analysis.casebook import confirm_case_draft, generate_case_draft, recall_case_cards, write_case_cards
from claude_web.android_analysis.code_scope import collect_code_context, resolve_code_scopes
from claude_web.android_analysis.deep_analysis import build_deep_evidence_pack, generate_deep_report
from claude_web.android_analysis.evidence import generate_first_evidence_pack
from claude_web.android_analysis.jobs import AndroidAnalysisJobStore
from claude_web.android_analysis.models import AndroidAnalysisError, ExtractionLimits
from claude_web.android_analysis.planner import parse_planner_json, run_planner, validate_planner_result
from claude_web.android_analysis.profiler import profile_extracted_tree
from claude_web.android_analysis.reporter import generate_first_report
from claude_web.android_analysis.rule_engine import run_rule_matching, score_event_relevance
from claude_web.android_analysis.rule_loader import load_rule_packs
from claude_web.android_analysis.sampler import sample_files
from claude_web.android_analysis.verifier import run_verifier


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

    def test_sampler_writes_head_tail_and_keyword_context(self):
        extracted = self.root / 'extracted'
        artifacts = self.root / 'artifacts'
        extracted.mkdir()
        lines = [f'05-07 10:00:{i:02d} normal line {i}' for i in range(20)]
        lines.insert(10, '05-07 10:00:10 AndroidRuntime FATAL EXCEPTION: main')
        (extracted / 'main.log').write_text('\n'.join(lines), encoding='utf-8')
        profile_extracted_tree(extracted, artifacts)

        samples = sample_files(extracted, artifacts, question='RDM lock crash')

        self.assertEqual(samples['file_count'], 1)
        sample_types = [s['type'] for s in samples['files'][0]['samples']]
        self.assertIn('head', sample_types)
        self.assertIn('tail', sample_types)
        keyword_samples = [s for s in samples['files'][0]['samples'] if s['type'] == 'keyword']
        self.assertTrue(keyword_samples)
        self.assertIn('FATAL EXCEPTION', keyword_samples[0]['content'])
        self.assertTrue((artifacts / 'file_samples.json').is_file())

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

        matched = run_rule_matching(artifacts, knowledge, question='lock crash')
        evidence = generate_first_evidence_pack(artifacts, question='lock crash')

        self.assertGreaterEqual(matched['event_count'], 1)
        self.assertEqual(matched['events'][0]['issue_type'], 'android_app_crash')
        self.assertIn('FATAL EXCEPTION', matched['events'][0]['snippet'])
        self.assertTrue(evidence['has_evidence'])
        self.assertTrue((artifacts / 'matched_rules.json').is_file())
        self.assertTrue((artifacts / 'first_evidence_pack.md').is_file())

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

    def test_rule_loader_loads_requested_bundle_rule_pack(self):
        knowledge = self.root / 'knowledge'
        rules_dir = knowledge / 'bundles' / 'android-rdm' / 'rules'
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

        packs = load_rule_packs(knowledge, candidate_rule_packs=['rdm-base'], candidate_bundle_ids=['android-rdm'])
        ids = [p['id'] for p in packs]

        self.assertIn('android-base', ids)
        self.assertIn('rdm-base', ids)

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

        cards = recall_case_cards(knowledge, planner, matched)
        write_case_cards(artifacts, cards)
        report = generate_first_report(artifacts, question='RDM lock crash', enable_ai=False)

        self.assertEqual(cards['card_count'], 1)
        self.assertEqual(report['report_mode'], 'fallback')
        self.assertTrue((artifacts / 'case_cards.json').is_file())
        self.assertTrue((artifacts / 'final_report.md').is_file())
        self.assertIn('Android 问题首轮分析报告', (artifacts / 'final_report.md').read_text(encoding='utf-8'))

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
