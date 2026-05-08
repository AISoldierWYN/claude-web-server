import json
import logging
import tempfile
import unittest
from pathlib import Path

from claude_web import config
from claude_web.claude_runner import _skill_bundles_instruction


class SkillBundleLoadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_paths_config = config.PATHS_CONFIG_FILE

        self.skill_root = self.root / 'skills'
        self.skill_dir = self.skill_root / 'rdm-triage'
        self.skill_dir.mkdir(parents=True)
        (self.skill_dir / 'SKILL.md').write_text(
            '\n'.join(
                [
                    '---',
                    'name: rdm-triage',
                    'description: RDM 锁机日志优先排查流程。',
                    '---',
                    '# RDM Triage',
                    '',
                    '步骤1：先识别 RDM 业务日志。',
                    '步骤2：再读取必要代码证据。',
                ]
            ),
            encoding='utf-8',
        )

        self.code_dir = self.root / 'RealtimeDeviceManager'
        self.code_dir.mkdir()
        (self.code_dir / 'CLAUDE.md').write_text(
            '# RDM 项目规则\n优先使用业务规则，不要先全仓 grep。\n',
            encoding='utf-8',
        )

        self.paths_config = self.root / 'paths.json'
        self.paths_config.write_text(
            json.dumps(
                {
                    'version': 3,
                    'notes': 'test notes',
                    'bundles': [
                        {
                            'id': 'android-rdm',
                            'title': 'RealtimeDeviceManager',
                            'summary': 'RDM 锁机、解锁、设备管理问题。',
                            'keywords': ['rdm', '锁机'],
                            'paths': [str(self.skill_root)],
                            'resources': [
                                {
                                    'id': 'rdm-code',
                                    'kind': 'code',
                                    'path': str(self.code_dir),
                                    'summary': 'RDM 代码目录。',
                                }
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
        config.PATHS_CONFIG_FILE = self.paths_config

    def tearDown(self):
        config.PATHS_CONFIG_FILE = self.old_paths_config
        self.tmp.cleanup()

    def _load_bundle(self):
        _, _, bundles = config.load_paths_config_file(logging.getLogger('test-skill-bundles'))
        self.assertEqual(len(bundles), 1)
        return bundles[0]

    def test_config_discovers_skills_resources_and_claude_md(self):
        bundle = self._load_bundle()

        self.assertEqual(bundle['id'], 'android-rdm')
        self.assertEqual(len(bundle['skills']), 1)
        self.assertEqual(bundle['skills'][0]['id'], 'rdm-triage')
        self.assertIn('RDM 锁机日志', bundle['skills'][0]['summary'])
        self.assertIn((self.skill_dir / 'SKILL.md').resolve().as_posix(), bundle['skills'][0]['path'])
        self.assertEqual(bundle['resources'][1]['id'], 'rdm-code')
        self.assertIn(self.code_dir.resolve().as_posix(), bundle['paths'])
        self.assertEqual(bundle['claude_md_paths'], [(self.code_dir / 'CLAUDE.md').resolve().as_posix()])

    def test_prompt_injects_selected_skill_before_resource_paths(self):
        bundle = self._load_bundle()
        selected = dict(bundle['skills'][0])
        selected['match_reason'] = 'keyword: rdm'
        bundle['mounted'] = True
        bundle['mount_reason'] = 'keyword: rdm'
        bundle['selected_skills'] = [selected]

        prompt = _skill_bundles_instruction([bundle])

        self.assertIn('处理顺序必须遵守', prompt)
        self.assertIn('RDM Triage', prompt)
        self.assertIn('步骤1：先识别 RDM 业务日志', prompt)
        self.assertIn('RDM 项目规则', prompt)
        self.assertLess(prompt.index('本轮优先 Skill'), prompt.index('按需深入路径'))

    def test_unmounted_bundle_keeps_paths_and_skill_content_hidden(self):
        bundle = self._load_bundle()
        bundle['mounted'] = False
        bundle['selected_skills'] = []

        prompt = _skill_bundles_instruction([bundle])

        self.assertIn('仅摘要，本轮未挂载路径', prompt)
        self.assertNotIn('步骤1：先识别 RDM 业务日志', prompt)
        self.assertNotIn(self.code_dir.resolve().as_posix(), prompt)

    def test_claude_md_is_injected_only_for_mounted_bundles(self):
        mounted = self._load_bundle()
        mounted['mounted'] = True
        mounted['mount_reason'] = 'keyword: rdm'
        mounted['selected_skills'] = []

        other_dir = self.root / 'OtherProject'
        other_dir.mkdir()
        (other_dir / 'CLAUDE.md').write_text(
            '# Other Project Rules\nSECRET_UNMOUNTED_CLAUDE_MD_SHOULD_NOT_LOAD\n',
            encoding='utf-8',
        )
        unmounted = {
            'id': 'other-project',
            'title': 'Other Project',
            'summary': '另一个未命中的技能包。',
            'keywords': ['other'],
            'mounted': False,
            'paths': [other_dir.resolve().as_posix()],
            'resources': [
                {
                    'id': 'other-code',
                    'kind': 'code',
                    'path': other_dir.resolve().as_posix(),
                    'summary': '未命中代码目录。',
                }
            ],
            'skills': [],
            'selected_skills': [],
            'claude_md_paths': [(other_dir / 'CLAUDE.md').resolve().as_posix()],
        }

        prompt = _skill_bundles_instruction([mounted, unmounted])

        self.assertIn('RDM 项目规则', prompt)
        self.assertIn('other-project', prompt)
        self.assertNotIn('SECRET_UNMOUNTED_CLAUDE_MD_SHOULD_NOT_LOAD', prompt)
        self.assertNotIn(other_dir.resolve().as_posix(), prompt)


if __name__ == '__main__':
    unittest.main()
