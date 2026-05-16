#!/usr/bin/env python3
"""Android 日志规则包管理工具。

该脚本刻意只依赖 Python 标准库，方便用户、Claude/Gemini CLI 或测试进程直接调用。
它生成的 JSON 与 claude_web.android_analysis.rule_loader 当前消费的规则结构保持一致。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[3]
DEFAULT_PATHS_CONFIG = REPO_ROOT / 'claude_web_paths.config.json'
DEFAULT_KNOWLEDGE_DIR = REPO_ROOT / 'android_analysis_knowledge'

SOURCE_SUFFIXES = {
    '.java',
    '.kt',
    '.kts',
    '.c',
    '.cc',
    '.cpp',
    '.cxx',
    '.h',
    '.hh',
    '.hpp',
    '.hxx',
    '.gradle',
    '.cmake',
    '.mk',
    '.xml',
    '.properties',
    '.aidl',
    '.txt',
}
GUIDE_FILENAMES = {'claude.md', 'agents.md'}
IGNORED_DIRS = {
    '.git',
    '.gradle',
    '.idea',
    'build',
    'out',
    'target',
    'node_modules',
    '.cxx',
    '.externalnativebuild',
}
GENERIC_IDENTITY_TERMS = {
    'build',
    'gradle',
    'local',
    'settings',
    'result',
    'success',
    'error',
    'base',
    'main',
    'test',
    'androidmanifest',
}
VALID_SEVERITIES = {'fatal', 'high', 'medium', 'low'}
VALID_ISSUE_TYPES = {
    'android_app_crash',
    'android_system_server_crash',
    'android_anr',
    'android_native_crash',
    'android_permission_denial',
    'android_package_install',
    'android_boot',
    'android_framework_behavior',
    'android_business_spec',
    'android_test_failure',
    'generic_log_error',
    'unknown',
}
VALID_PROJECT_PRESETS = {'app', 'framework', 'native', 'library'}
VALID_PROFILES = {'functional', 'stability', 'xts', 'memory', 'performance'}
PROFILE_RULE_ISSUE_TYPES = {
    'functional': 'android_business_spec',
    'stability': 'android_app_crash',
    'xts': 'android_test_failure',
    'memory': 'generic_log_error',
    'performance': 'android_framework_behavior',
}
APP_CAPABILITY_PATTERNS = {
    'workmanager': [
        r'\bWorkManager\b',
        r'\b(?:CoroutineWorker|ListenableWorker|WorkerFactory|WorkRequest)\b',
        r'\b[A-Z][A-Za-z0-9_]*(?:Worker|Work)\b',
    ],
    'network': [
        r'\b(?:Retrofit|OkHttpClient|HttpURLConnection|WebSocket|ApolloClient|Volley|Ktor)\b',
        r'https?://[A-Za-z0-9._~:/?#\[\]@!$&\'()*+,;=%-]{4,160}',
    ],
    'database': [
        r'\b(?:RoomDatabase|SQLiteOpenHelper|SharedPreferences|DataStore|Realm|ContentProvider)\b',
        r'\b[A-Z][A-Za-z0-9_]*(?:Dao|Database|Entity|Repository)\b',
    ],
    'file': [
        r'\b(?:FileProvider|ContentResolver|MediaStore|DocumentFile|openFileDescriptor)\b',
    ],
    'notification': [
        r'\b(?:NotificationManager|NotificationCompat|NotificationChannel)\b',
        r'\b[A-Z][A-Za-z0-9_]*(?:Notification|Channel)\b',
    ],
    'config': [
        r'\b(?:BuildConfig|RemoteConfig|FeatureFlag|featureFlag|Settings|PreferenceManager)\b',
        r'\b[A-Z][A-Za-z0-9_]*(?:Config|Setting|Preference|Flag)\b',
    ],
    'sync': [
        r'\b(?:SyncAdapter|AbstractThreadedSyncAdapter|SyncResult|SyncRequest|ContentResolver\.requestSync|RemoteOperation|RemoteOperationResult)\b',
        r'\b[A-Z][A-Za-z0-9_]*(?:Sync|Syncer|Synchronizer|SyncAdapter|SyncService|SyncWorker|UploadWorker|DownloadWorker)\b',
    ],
    'account_auth': [
        r'\b(?:AccountManager|AbstractAccountAuthenticator|AccountAuthenticatorActivity|AuthenticatorException|OAuth|OpenID|OIDC|Token|Login|SignIn|WebDAV|WebDav)\b',
        r'\b[A-Z][A-Za-z0-9_]*(?:Authenticator|AccountManager|AccountService|AccountCreator|LoginActivity|TokenProvider|OAuthClient)\b',
    ],
    'background_task': [
        r'\b(?:JobScheduler|JobService|ForegroundService|AlarmManager|PendingIntent|CoroutineWorker|ListenableWorker|WorkRequest)\b',
        r'\b[A-Z][A-Za-z0-9_]*(?:Job|Scheduler|Receiver|Service|Worker)\b',
    ],
    'process_terminal': [
        r'\b(?:ProcessBuilder|Runtime\.getRuntime\(\)\.exec|execve|Os\.exec|TerminalSession|TermuxService|RunCommandService|ExecutionCommand|pty|shell)\b',
        r'\b[A-Z][A-Za-z0-9_]*(?:Terminal|Session|Shell|Process|Command|Execution|Runner)\b',
    ],
    'mail': [
        r'\b(?:ImapStore|SmtpTransport|MailStore|MessageReader|MessageList|Folder|Mailbox|Email|MimeMessage)\b',
        r'\b[A-Z][A-Za-z0-9_]*(?:Mail|Message|Folder|Mailbox|Imap|Smtp|Email)\b',
    ],
}


def default_generated_rule_pack_id(bundle_id: str) -> str:
    """根据 bundle id 生成稳定的默认规则包 id。

    Android 类 bundle 常带有 `android-` 前缀；去掉这个公共前缀后生成
    `<模块名>-generated`，可以保持 `rdm-generated` 这类短名称，同时仍由
    bundle.json 决定它属于哪个项目模块。
    """
    slug = slugify(bundle_id or 'bundle')
    if slug.startswith('android-') and len(slug) > len('android-'):
        slug = slug[len('android-'):]
    return f'{slug}-generated'


class RulePackError(Exception):
    """命令行可读的业务异常。"""


@dataclass
class ScanResult:
    packages: List[str]
    tags: List[str]
    components: List[str]
    business_terms: List[str]
    error_terms: List[str]
    log_messages: List[str]
    exact_logs: List[Dict[str, str]]
    app_signals: List[str]
    app_capability_terms: List[str]
    permissions: List[str]
    gradle_modules: List[str]
    native_signals: List[str]
    native_libraries: List[str]
    native_symbols: List[str]
    native_log_tags: List[str]
    native_trace_sections: List[str]
    source_paths: List[str]
    preferred_paths: List[str]
    claude_md_candidates: List[str]
    scanned_files: int


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
        if result is not None:
            print_output(result, json_mode=getattr(args, 'json', False))
        return 0
    except RulePackError as exc:
        print(str(exc), file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='生成、校验和维护 Android 日志规则包')
    parser.add_argument('--paths-config', default=str(DEFAULT_PATHS_CONFIG), help='claude_web_paths.config.json 路径')
    parser.add_argument('--knowledge-dir', default=str(DEFAULT_KNOWLEDGE_DIR), help='android_analysis_knowledge 路径')
    parser.add_argument('--json', action='store_true', help='以 JSON 输出命令结果')

    sub = parser.add_subparsers(dest='command', required=True)

    gen = sub.add_parser('generate', help='从项目代码生成或刷新规则包')
    add_common_rule_pack_args(gen, require_pack=False)
    gen.add_argument('--project-dir', action='append', default=[], help='额外或替代项目目录；可重复')
    gen.add_argument('--title', default='', help='规则包标题')
    gen.add_argument('--description', default='', help='规则包说明')
    gen.add_argument('--project-preset', default='', help='项目类型预设：app/framework/native/library')
    gen.add_argument('--profile', action='append', default=[], help='问题类型 profile：functional/stability/xts/memory/performance，可重复')
    gen.add_argument('--max-files', type=int, default=1200, help='最多扫描源码文件数量')
    gen.add_argument('--max-bytes-per-file', type=int, default=256_000, help='单文件最多读取字节')
    gen.add_argument('--dry-run', action='store_true', help='只输出规则包，不写入知识目录')
    gen.set_defaults(func=cmd_generate)

    val = sub.add_parser('validate', help='校验规则包 schema 和正则')
    add_common_rule_pack_args(val)
    val.set_defaults(func=cmd_validate)

    ls = sub.add_parser('list', help='列出 bundle 下的规则包和规则摘要')
    ls.add_argument('--bundle-id', required=True)
    ls.set_defaults(func=cmd_list)

    get = sub.add_parser('get', help='查看规则包或单条规则')
    add_common_rule_pack_args(get)
    get.add_argument('--rule-id', default='', help='规则 id；为空则返回整个规则包')
    get.set_defaults(func=cmd_get)

    add = sub.add_parser('add', help='新增规则')
    add_common_rule_pack_args(add)
    add_rule_input_args(add)
    add.set_defaults(func=cmd_add)

    upd = sub.add_parser('update', help='用完整规则 JSON 替换同 id 规则')
    add_common_rule_pack_args(upd)
    add_rule_input_args(upd)
    upd.set_defaults(func=cmd_update)

    delete = sub.add_parser('delete', help='删除规则或规则包')
    add_common_rule_pack_args(delete)
    delete.add_argument('--rule-id', default='', help='规则 id；为空则删除整个规则包')
    delete.add_argument('--yes', action='store_true', help='确认删除整个规则包')
    delete.set_defaults(func=cmd_delete)

    test = sub.add_parser('test', help='用日志文本、目录或 zip 轻量验证规则命中')
    add_common_rule_pack_args(test)
    test.add_argument('--log-path', required=True, help='日志文件、目录或 zip 路径')
    test.add_argument('--require-hit', action='store_true', help='无命中时返回失败')
    test.set_defaults(func=cmd_test)

    ev = sub.add_parser('evaluate', help='批量评测规则生成 case 并输出 scorecard')
    ev.add_argument('--eval-root', default='android_analysis_eval', help='评测集根目录')
    ev.add_argument('--case', action='append', default=[], help='指定 case 目录或 case.json，可重复')
    ev.add_argument('--scorecard', default='', help='scorecard.json 输出路径')
    ev.add_argument('--max-files', type=int, default=1200, help='generate 阶段最大扫描文件数')
    ev.add_argument('--max-bytes-per-file', type=int, default=256_000, help='generate 阶段单文件最大读取字节')
    ev.set_defaults(func=cmd_evaluate)

    return parser


def add_common_rule_pack_args(parser: argparse.ArgumentParser, require_pack: bool = True) -> None:
    parser.add_argument('--bundle-id', required=True, help='claude_web_paths.config.json 中的 bundle id')
    parser.add_argument(
        '--rule-pack-id',
        required=require_pack,
        default='',
        help='规则包 id；generate 默认使用 <bundle-short-name>-generated，例如 android-rdm -> rdm-generated',
    )


def add_rule_input_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--rule-json', help='完整规则 JSON 字符串')
    group.add_argument('--rule-file', help='完整规则 JSON 文件')


def cmd_generate(args: argparse.Namespace) -> Dict[str, Any]:
    config = load_paths_config(Path(args.paths_config))
    bundle = find_bundle(config, args.bundle_id)
    project_dirs = resolve_project_dirs(bundle, args.project_dir)
    if not project_dirs:
        raise RulePackError(f'未找到 bundle={args.bundle_id} 的可扫描项目目录')
    project_preset = normalize_project_preset(getattr(args, 'project_preset', '') or bundle.get('project_preset') or bundle.get('type'))
    profiles = normalize_profiles(getattr(args, 'profile', []) or bundle.get('profiles') or bundle.get('supported_profiles'))

    scan = scan_android_project(
        project_dirs,
        bundle_keywords=bundle_keywords(bundle),
        project_preset=project_preset,
        max_files=args.max_files,
        max_bytes_per_file=args.max_bytes_per_file,
    )
    pack_id = args.rule_pack_id or default_generated_rule_pack_id(args.bundle_id)
    pack = build_generated_rule_pack(
        bundle_id=args.bundle_id,
        pack_id=pack_id,
        title=args.title or f'{bundle.get("title") or args.bundle_id} Generated Log Signals',
        description=args.description or f'Generated from {", ".join(str(p) for p in project_dirs)}',
        project_preset=project_preset,
        profiles=profiles,
        scan=scan,
    )
    validation = validate_pack(pack, config)
    if validation['errors']:
        raise RulePackError('生成的规则包未通过校验：' + '; '.join(validation['errors']))
    if not args.dry_run:
        knowledge_dir = Path(args.knowledge_dir)
        path = write_rule_pack(knowledge_dir, args.bundle_id, pack)
        bundle_manifest = ensure_bundle_manifest(
            knowledge_dir,
            bundle_id=args.bundle_id,
            source_bundle=bundle,
            project_dirs=project_dirs,
            rule_pack_id=pack_id,
            project_preset=project_preset,
            profiles=profiles,
            deep_hints=(pack.get('metadata') or {}).get('deep_hints') or {},
        )
    else:
        path = None
        bundle_manifest = None
    return {
        'ok': True,
        'path': str(path) if path else '',
        'bundle_manifest': str(bundle_manifest) if bundle_manifest else '',
        'rule_pack': pack,
        'scan': {
            'scanned_files': scan.scanned_files,
            'project_preset': project_preset,
            'profiles': profiles,
            'packages': scan.packages[:20],
            'tags': scan.tags[:40],
            'app_signals': scan.app_signals[:60],
            'app_capability_terms': scan.app_capability_terms[:60],
            'permissions': scan.permissions[:40],
            'gradle_modules': scan.gradle_modules[:40],
            'native_signals': scan.native_signals[:60],
            'native_libraries': scan.native_libraries[:40],
            'native_symbols': scan.native_symbols[:60],
            'native_log_tags': scan.native_log_tags[:40],
            'native_trace_sections': scan.native_trace_sections[:40],
            'source_paths': scan.source_paths[:60],
            'preferred_paths': scan.preferred_paths[:60],
            'claude_md_candidates': scan.claude_md_candidates[:20],
            'business_terms': scan.business_terms[:60],
            'exact_logs': scan.exact_logs[:20],
        },
        'validation': validation,
    }


def cmd_validate(args: argparse.Namespace) -> Dict[str, Any]:
    config = load_paths_config(Path(args.paths_config))
    pack = load_rule_pack(Path(args.knowledge_dir), args.bundle_id, args.rule_pack_id)
    result = validate_pack(pack, config)
    result['ok'] = not result['errors']
    if result['errors']:
        raise RulePackError('规则包校验失败：' + '; '.join(result['errors']))
    return result


def cmd_list(args: argparse.Namespace) -> Dict[str, Any]:
    rules_dir = rules_dir_for(Path(args.knowledge_dir), args.bundle_id)
    packs = []
    for path in sorted(rules_dir.glob('*.json')):
        try:
            pack = read_json(path)
        except RulePackError:
            continue
        for item in normalize_rule_packs(pack):
            packs.append(
                {
                    'id': item.get('id'),
                    'title': item.get('title'),
                    'path': str(path),
                    'source_bundle_ids': item.get('source_bundle_ids') or [],
                    'rule_count': len(item.get('rules') or []),
                    'rules': [
                        {
                            'id': r.get('id'),
                            'title': r.get('title'),
                            'issue_type': r.get('issue_type'),
                            'severity': r.get('severity'),
                        }
                        for r in item.get('rules') or []
                    ],
                }
            )
    return {'bundle_id': args.bundle_id, 'packs': packs}


def cmd_get(args: argparse.Namespace) -> Dict[str, Any]:
    pack = load_rule_pack(Path(args.knowledge_dir), args.bundle_id, args.rule_pack_id)
    if not args.rule_id:
        return pack
    rule = find_rule(pack, args.rule_id)
    if not rule:
        raise RulePackError(f'未找到规则：{args.rule_id}')
    return rule


def cmd_add(args: argparse.Namespace) -> Dict[str, Any]:
    config = load_paths_config(Path(args.paths_config))
    pack = load_rule_pack(Path(args.knowledge_dir), args.bundle_id, args.rule_pack_id)
    rule = load_rule_input(args)
    if find_rule(pack, rule.get('id')):
        raise RulePackError(f'规则已存在：{rule.get("id")}')
    pack.setdefault('rules', []).append(rule)
    validation = validate_pack(pack, config)
    if validation['errors']:
        raise RulePackError('新增后校验失败：' + '; '.join(validation['errors']))
    path = write_rule_pack(Path(args.knowledge_dir), args.bundle_id, pack)
    return {'ok': True, 'path': str(path), 'rule': rule}


def cmd_update(args: argparse.Namespace) -> Dict[str, Any]:
    config = load_paths_config(Path(args.paths_config))
    pack = load_rule_pack(Path(args.knowledge_dir), args.bundle_id, args.rule_pack_id)
    rule = load_rule_input(args)
    rules = pack.setdefault('rules', [])
    for idx, old in enumerate(rules):
        if old.get('id') == rule.get('id'):
            rules[idx] = rule
            validation = validate_pack(pack, config)
            if validation['errors']:
                raise RulePackError('更新后校验失败：' + '; '.join(validation['errors']))
            path = write_rule_pack(Path(args.knowledge_dir), args.bundle_id, pack)
            return {'ok': True, 'path': str(path), 'rule': rule}
    raise RulePackError(f'未找到规则：{rule.get("id")}')


def cmd_delete(args: argparse.Namespace) -> Dict[str, Any]:
    pack_path = rule_pack_path(Path(args.knowledge_dir), args.bundle_id, args.rule_pack_id)
    if not args.rule_id:
        if not args.yes:
            raise RulePackError('删除整个规则包需要 --yes')
        if pack_path.exists():
            pack_path.unlink()
        return {'ok': True, 'deleted_pack': str(pack_path)}

    pack = load_rule_pack(Path(args.knowledge_dir), args.bundle_id, args.rule_pack_id)
    before = len(pack.get('rules') or [])
    pack['rules'] = [r for r in (pack.get('rules') or []) if r.get('id') != args.rule_id]
    if len(pack['rules']) == before:
        raise RulePackError(f'未找到规则：{args.rule_id}')
    path = write_rule_pack(Path(args.knowledge_dir), args.bundle_id, pack)
    return {'ok': True, 'path': str(path), 'deleted_rule': args.rule_id}


def cmd_test(args: argparse.Namespace) -> Dict[str, Any]:
    pack = load_rule_pack(Path(args.knowledge_dir), args.bundle_id, args.rule_pack_id)
    log_items = load_log_items(Path(args.log_path))
    hits = match_pack_against_logs(pack, log_items)
    result = {'ok': True, 'hit_count': len(hits), 'hits': hits[:80]}
    if args.require_hit and not hits:
        raise RulePackError('没有规则命中')
    return result


def cmd_evaluate(args: argparse.Namespace) -> Dict[str, Any]:
    eval_root = resolve_cli_path(args.eval_root)
    case_dirs = discover_eval_cases(eval_root, args.case)
    if not case_dirs:
        raise RulePackError(f'评测集未找到 case.json：{eval_root}')

    results = [
        evaluate_case(
            case_dir,
            paths_config=Path(args.paths_config),
            knowledge_dir=Path(args.knowledge_dir),
            max_files=args.max_files,
            max_bytes_per_file=args.max_bytes_per_file,
        )
        for case_dir in case_dirs
    ]
    passed = sum(1 for item in results if item.get('ok'))
    scorecard = {
        'version': 1,
        'ok': passed == len(results),
        'eval_root': str(eval_root),
        'case_count': len(results),
        'passed_count': passed,
        'failed_count': len(results) - passed,
        'cases': results,
    }
    scorecard_path = resolve_scorecard_path(args.scorecard, eval_root)
    scorecard_path.parent.mkdir(parents=True, exist_ok=True)
    with open(scorecard_path, 'w', encoding='utf-8') as f:
        json.dump(scorecard, f, ensure_ascii=False, indent=2)
        f.write('\n')
    return {
        'ok': scorecard['ok'],
        'scorecard': str(scorecard_path),
        'case_count': scorecard['case_count'],
        'passed_count': scorecard['passed_count'],
        'failed_count': scorecard['failed_count'],
        'cases': results,
    }


def evaluate_case(
    case_dir: Path,
    paths_config: Path,
    knowledge_dir: Path,
    max_files: int,
    max_bytes_per_file: int,
) -> Dict[str, Any]:
    case_dir = Path(case_dir).resolve()
    case_data = read_json(case_dir / 'case.json')
    expected = read_json_optional(case_dir / 'expected.json')
    case_id = str(case_data.get('id') or case_dir.name)
    bundle_id = str(case_data.get('bundle_id') or '').strip()
    if not bundle_id:
        return {
            'id': case_id,
            'ok': False,
            'case_dir': str(case_dir),
            'errors': ['case.json 缺少 bundle_id'],
            'warnings': [],
        }
    rule_pack_id = str(
        case_data.get('generated_rule_pack')
        or case_data.get('rule_pack_id')
        or default_generated_rule_pack_id(bundle_id)
    )
    project_dirs = resolve_case_project_dirs(case_dir, case_data)
    log_path = resolve_case_log_path(case_dir, case_data)
    case_paths_config = ensure_case_paths_config(
        paths_config=paths_config,
        knowledge_dir=knowledge_dir,
        case_id=case_id,
        case_data=case_data,
        bundle_id=bundle_id,
        project_dirs=project_dirs,
    )
    errors: List[str] = []
    warnings: List[str] = []
    generated: Dict[str, Any] = {}
    validation: Dict[str, Any] = {}
    tested: Dict[str, Any] = {'ok': True, 'hit_count': 0, 'hits': []}

    try:
        generated = cmd_generate(
            argparse.Namespace(
                paths_config=str(case_paths_config),
                knowledge_dir=str(knowledge_dir),
                bundle_id=bundle_id,
                rule_pack_id=rule_pack_id,
                project_dir=[str(p) for p in project_dirs],
                title=str(case_data.get('title') or ''),
                description=str(case_data.get('description') or ''),
                project_preset=str(case_data.get('project_preset') or ''),
                profile=list(case_data.get('profiles') or []),
                max_files=max_files,
                max_bytes_per_file=max_bytes_per_file,
                dry_run=False,
            )
        )
        validation = cmd_validate(
            argparse.Namespace(
                paths_config=str(case_paths_config),
                knowledge_dir=str(knowledge_dir),
                bundle_id=bundle_id,
                rule_pack_id=rule_pack_id,
            )
        )
        if log_path:
            tested = cmd_test(
                argparse.Namespace(
                    knowledge_dir=str(knowledge_dir),
                    bundle_id=bundle_id,
                    rule_pack_id=rule_pack_id,
                    log_path=str(log_path),
                    require_hit=False,
                )
            )
    except RulePackError as exc:
        errors.append(str(exc))

    pack = generated.get('rule_pack') or {}
    quality = score_rule_pack(pack, tested.get('hits') or [], expected)
    errors.extend(quality.get('errors') or [])
    warnings.extend(quality.get('warnings') or [])
    return {
        'id': case_id,
        'ok': not errors,
        'case_dir': str(case_dir),
        'bundle_id': bundle_id,
        'rule_pack_id': rule_pack_id,
        'project_preset': case_data.get('project_preset') or '',
        'profiles': case_data.get('profiles') or [],
        'paths_config': str(case_paths_config),
        'log_path': str(log_path) if log_path else '',
        'errors': errors,
        'warnings': warnings,
        'metrics': {
            **(quality.get('metrics') or {}),
            'schema_ok': bool(validation.get('ok')) and not validation.get('errors'),
            'validation_warning_count': len(validation.get('warnings') or []),
            'hit_count': int(tested.get('hit_count') or 0),
        },
        'validation': validation,
        'hits': (tested.get('hits') or [])[:40],
    }


def ensure_case_paths_config(
    paths_config: Path,
    knowledge_dir: Path,
    case_id: str,
    case_data: Dict[str, Any],
    bundle_id: str,
    project_dirs: Sequence[Path],
) -> Path:
    """评测 case 可自带 bundle 元数据，减少对用户本地配置的依赖。"""
    try:
        config = load_paths_config(paths_config)
        find_bundle(config, bundle_id)
        return paths_config
    except RulePackError:
        pass

    bundle_meta = case_data.get('bundle') if isinstance(case_data.get('bundle'), dict) else {}
    if not project_dirs:
        return paths_config
    bundle = {
        'id': bundle_id,
        'title': bundle_meta.get('title') or case_data.get('title') or case_data.get('repo') or bundle_id,
        'summary': bundle_meta.get('summary') or case_data.get('summary') or case_data.get('description') or '',
        'keywords': normalize_keyword_list(bundle_meta.get('keywords') or case_data.get('keywords') or []),
        'paths': [str(path) for path in project_dirs],
        'project_preset': case_data.get('project_preset') or bundle_meta.get('project_preset') or '',
        'profiles': case_data.get('profiles') or bundle_meta.get('profiles') or [],
    }
    generated_config = {'version': 2, 'bundles': [bundle]}
    out_dir = Path(knowledge_dir) / '_eval_paths'
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f'{slugify(case_id)}.paths.config.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(generated_config, f, ensure_ascii=False, indent=2)
        f.write('\n')
    return out_path


def normalize_keyword_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return dedupe([x for x in re.split(r'[,;\s]+', value) if x], limit=120)
    if isinstance(value, (list, tuple, set)):
        return dedupe([str(x) for x in value if str(x or '').strip()], limit=120)
    return []


def discover_eval_cases(eval_root: Path, selected: Sequence[str]) -> List[Path]:
    if selected:
        out = []
        for raw in selected:
            path = resolve_cli_path(raw)
            if path.is_file() and path.name == 'case.json':
                path = path.parent
            if not (path / 'case.json').is_file():
                raise RulePackError(f'评测 case 缺少 case.json：{path}')
            out.append(path.resolve())
        return dedupe_paths(out)

    cases_root = eval_root / 'cases'
    if not cases_root.is_dir():
        return []
    return [p.parent.resolve() for p in sorted(cases_root.rglob('case.json'))]


def resolve_case_project_dirs(case_dir: Path, case_data: Dict[str, Any]) -> List[Path]:
    raw_values: List[Any] = []
    if case_data.get('project_dir'):
        raw_values.append(case_data.get('project_dir'))
    raw_values.extend(case_data.get('project_dirs') or [])
    return dedupe_paths([resolve_case_path(case_dir, str(v)) for v in raw_values if str(v or '').strip()])


def resolve_case_log_path(case_dir: Path, case_data: Dict[str, Any]) -> Optional[Path]:
    raw = case_data.get('log_archive') or case_data.get('log_path')
    if not raw:
        return None
    return resolve_case_path(case_dir, str(raw))


def resolve_scorecard_path(raw: str, eval_root: Path) -> Path:
    if raw:
        return resolve_cli_path(raw)
    return eval_root / 'scorecard.json'


def resolve_case_path(case_dir: Path, raw: str) -> Path:
    value = str(raw or '').strip()
    if value.startswith('local://'):
        value = value[len('local://'):]
        return (REPO_ROOT / value).resolve()
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    candidate = (case_dir / path).resolve()
    if candidate.exists():
        return candidate
    return (REPO_ROOT / path).resolve()


def resolve_cli_path(raw: str) -> Path:
    path = Path(str(raw or '')).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (REPO_ROOT / path).resolve()


def score_rule_pack(pack: Dict[str, Any], hits: List[Dict[str, Any]], expected: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    rules = pack.get('rules') or []
    min_rule_count = int(expected.get('min_rule_count') or 0)
    min_hit_count = int(expected.get('min_hit_count') or 0)
    if min_rule_count and len(rules) < min_rule_count:
        errors.append(f'rule_count {len(rules)} < min_rule_count {min_rule_count}')
    if min_hit_count and len(hits) < min_hit_count:
        errors.append(f'hit_count {len(hits)} < min_hit_count {min_hit_count}')

    missing_keywords = [kw for kw in expected.get('expected_keywords') or [] if not pack_contains_term(pack, str(kw))]
    if missing_keywords:
        errors.append('missing expected keywords: ' + ', '.join(missing_keywords))

    hit_rule_ids = {str(h.get('rule_id') or '') for h in hits}
    hit_tags = set()
    for rule in rules:
        if str(rule.get('id') or '') in hit_rule_ids:
            hit_tags.update(str(tag) for tag in (rule.get('tags') or []) if tag)
    missing_tags = [tag for tag in expected.get('must_hit_rule_tags') or [] if str(tag) not in hit_tags]
    if missing_tags:
        errors.append('missing hit rule tags: ' + ', '.join(missing_tags))

    hit_text = json.dumps(hits, ensure_ascii=False)
    missing_hit_terms = [term for term in expected.get('must_hit_terms') or [] if str(term).lower() not in hit_text.lower()]
    if missing_hit_terms:
        errors.append('missing hit terms: ' + ', '.join(missing_hit_terms))
    forbidden_hit_terms = [term for term in expected.get('must_not_hit_terms') or [] if str(term).lower() in hit_text.lower()]
    if forbidden_hit_terms:
        errors.append('forbidden hit terms present: ' + ', '.join(forbidden_hit_terms))

    expected_profiles = expected_profile_list(expected)
    pack_profiles = [str(x) for x in ((pack.get('metadata') or {}).get('profiles') or []) if str(x).strip()]
    missing_profiles = [profile for profile in expected_profiles if profile not in pack_profiles]
    if missing_profiles:
        errors.append('missing expected profiles: ' + ', '.join(missing_profiles))

    generic_ratio = generic_term_ratio(pack)
    max_generic_ratio = expected.get('max_generic_term_ratio')
    if isinstance(max_generic_ratio, (int, float)) and generic_ratio > float(max_generic_ratio):
        warnings.append(f'generic_term_ratio {generic_ratio:.3f} > {float(max_generic_ratio):.3f}')

    return {
        'errors': errors,
        'warnings': warnings,
        'metrics': {
            'rule_count': len(rules),
            'hit_rule_count': len(hit_rule_ids),
            'generic_term_ratio': generic_ratio,
            'profile_coverage': pack_profiles,
            'expected_profile_count': len(expected_profiles),
            'expected_keyword_count': len(expected.get('expected_keywords') or []),
            'missing_expected_keyword_count': len(missing_keywords),
        },
    }


def expected_profile_list(expected: Dict[str, Any]) -> List[str]:
    values: List[Any] = []
    if expected.get('expected_profile'):
        values.append(expected.get('expected_profile'))
    values.extend(expected.get('expected_profiles') or [])
    return normalize_profiles(values)


def pack_contains_term(pack: Dict[str, Any], term: str) -> bool:
    needle = str(term or '').lower()
    if not needle:
        return True
    return needle in json.dumps(pack, ensure_ascii=False).lower()


def generic_term_ratio(pack: Dict[str, Any]) -> float:
    generic = {'error', 'fail', 'failed', 'failure', 'exception', 'crash'}
    total = 0
    generic_count = 0
    for rule in pack.get('rules') or []:
        match = rule.get('match') or {}
        for key in ('keywords', 'packages', 'regex', 'paths', 'kinds'):
            for item in match.get(key) or []:
                total += 1
                if str(item or '').strip().lower() in generic:
                    generic_count += 1
    return round(generic_count / total, 4) if total else 0.0


def load_paths_config(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise RulePackError(f'路径配置不存在：{path}')
    data = read_json(path)
    if not isinstance(data.get('bundles'), list):
        raise RulePackError(f'路径配置缺少 bundles：{path}')
    return data


def find_bundle(config: Dict[str, Any], bundle_id: str) -> Dict[str, Any]:
    for bundle in config.get('bundles') or []:
        if str(bundle.get('id') or '') == bundle_id:
            return bundle
    raise RulePackError(f'claude_web_paths.config.json 中未找到 bundle：{bundle_id}')


def resolve_project_dirs(bundle: Dict[str, Any], override_dirs: Sequence[str]) -> List[Path]:
    raw = override_dirs or bundle.get('paths') or []
    dirs: List[Path] = []
    for item in raw:
        path = Path(str(item)).expanduser()
        if path.is_dir():
            dirs.append(path.resolve())
    return dedupe_paths(dirs)


def bundle_keywords(bundle: Dict[str, Any]) -> List[str]:
    values: List[str] = []
    values.extend(str(x) for x in (bundle.get('keywords') or []) if x)
    # title/summary 常包含自然语言长句，只抽取领域词，避免生成过宽的中文短语规则。
    values.extend(extract_business_terms(' '.join([str(bundle.get('id') or ''), str(bundle.get('title') or ''), str(bundle.get('summary') or '')])))
    return dedupe(values, limit=120)


def normalize_project_preset(value: Any) -> str:
    preset = str(value or '').strip().lower()
    return preset if preset in VALID_PROJECT_PRESETS else ''


def normalize_profiles(value: Any) -> List[str]:
    """把 CLI、case.json 或 bundle.json 中的 profile 配置规范化。"""
    raw_values: List[str] = []
    if isinstance(value, str):
        raw_values.extend(x for x in re.split(r'[,;\s]+', value) if x)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            if isinstance(item, str):
                raw_values.extend(x for x in re.split(r'[,;\s]+', item) if x)
    profiles = [item.strip().lower() for item in raw_values if item.strip()]
    return [item for item in dedupe(profiles, limit=len(VALID_PROFILES)) if item in VALID_PROFILES]


def scan_android_project(
    project_dirs: Sequence[Path],
    bundle_keywords: Sequence[str],
    project_preset: str,
    max_files: int,
    max_bytes_per_file: int,
) -> ScanResult:
    packages: List[str] = []
    tags: List[str] = []
    components: List[str] = []
    business_terms: List[str] = list(bundle_keywords)
    error_terms: List[str] = []
    log_messages: List[str] = []
    exact_logs: List[Dict[str, str]] = []
    app_signals: List[str] = []
    app_capability_terms: List[str] = []
    permissions: List[str] = []
    gradle_modules: List[str] = extract_gradle_modules(project_dirs)
    native_signals: List[str] = []
    native_libraries: List[str] = []
    native_symbols: List[str] = []
    native_log_tags: List[str] = []
    native_trace_sections: List[str] = []
    source_paths: List[str] = []
    preferred_paths: List[str] = []
    claude_md_candidates: List[str] = []
    keyword_exact_logs, log_tag_weights = load_log_keyword_signals(project_dirs)
    exact_logs.extend(keyword_exact_logs)
    tags.extend(item.get('tag', '') for item in keyword_exact_logs)
    log_messages.extend(item.get('message', '') for item in keyword_exact_logs)
    scanned = 0

    for path in iter_source_files(project_dirs):
        if scanned >= max_files:
            break
        text = read_text_prefix(path, max_bytes_per_file)
        if not text:
            continue
        scanned += 1
        rel_path = relative_to_project_dirs(path, project_dirs)
        source_paths.append(rel_path)
        preferred_paths.extend(infer_preferred_paths_from_source(rel_path, path))
        if path.name.lower() in GUIDE_FILENAMES:
            claude_md_candidates.append(rel_path)
        rel_parts = [p for p in path.parts if p and p.lower() not in IGNORED_DIRS]
        business_terms.extend(extract_business_terms(' '.join(rel_parts)))
        packages.extend(extract_packages(text))
        log_patterns = extract_log_patterns(text, path, rel_path)
        exact_logs.extend(log_patterns)
        tags.extend(filter_identity_terms(extract_tags(text, path) + [p.get('tag', '') for p in log_patterns]))
        components.extend(filter_identity_terms(extract_android_components(text)))
        messages = extract_log_messages(text)
        messages.extend(p.get('message', '') for p in log_patterns)
        log_messages.extend(messages)
        business_terms.extend(extract_business_terms(' '.join(messages)))
        error_terms.extend(extract_error_terms(text))
        if project_preset == 'app':
            app = extract_app_signals(text, path)
            app_signals.extend(app['signals'])
            app_capability_terms.extend(app['capabilities'])
            permissions.extend(app['permissions'])
            components.extend(app['components'])
            business_terms.extend(app['business_terms'])
        if project_preset == 'native':
            native = extract_native_signals(text, path)
            native_signals.extend(native['signals'])
            native_libraries.extend(native['libraries'])
            native_symbols.extend(native['symbols'])
            native_log_tags.extend(native['log_tags'])
            native_trace_sections.extend(native['trace_sections'])
            tags.extend(native['log_tags'])
            business_terms.extend(native['business_terms'])
            error_terms.extend(native['error_terms'])

    return ScanResult(
        packages=dedupe(packages, limit=80),
        tags=dedupe(tags, limit=100),
        components=dedupe(components, limit=120),
        business_terms=dedupe(filter_terms(business_terms), limit=160),
        error_terms=dedupe(filter_terms(error_terms), limit=80),
        log_messages=dedupe(log_messages, limit=120),
        exact_logs=dedupe_exact_logs(rank_exact_logs(exact_logs, log_tag_weights), limit=240),
        app_signals=dedupe(filter_app_signal_terms(app_signals), limit=180),
        app_capability_terms=dedupe(filter_app_signal_terms(app_capability_terms), limit=180),
        permissions=dedupe(filter_app_signal_terms(permissions), limit=100),
        gradle_modules=dedupe(filter_app_signal_terms(gradle_modules), limit=80),
        native_signals=dedupe(filter_native_signal_terms(native_signals), limit=200),
        native_libraries=dedupe(filter_native_signal_terms(native_libraries), limit=120),
        native_symbols=dedupe(filter_native_signal_terms(native_symbols), limit=180),
        native_log_tags=dedupe(filter_native_signal_terms(native_log_tags), limit=100),
        native_trace_sections=dedupe(filter_native_signal_terms(native_trace_sections), limit=100),
        source_paths=dedupe(source_paths, limit=400),
        preferred_paths=dedupe(preferred_paths, limit=80),
        claude_md_candidates=dedupe(claude_md_candidates, limit=20),
        scanned_files=scanned,
    )


def iter_source_files(project_dirs: Sequence[Path]) -> Iterable[Path]:
    for root in project_dirs:
        for current, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d.lower() not in IGNORED_DIRS]
            for name in files:
                path = Path(current) / name
                if source_suffix(path) in SOURCE_SUFFIXES or path.name.lower() in GUIDE_FILENAMES:
                    yield path


def load_log_keyword_signals(project_dirs: Sequence[Path]) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    """Load project-maintained log keyword indexes when present.

    Some projects already keep a generated `log_keywords.json` beside the
    module root.  Use it as a ranking hint for tier-1 exact logs so the
    workflow keeps important business TAGs even when the source tree has
    hundreds of log statements.
    """
    candidates: List[Path] = []
    for root in project_dirs:
        root = Path(root)
        candidates.append(root / 'log_keywords.json')
        candidates.append(root.parent / 'log_keywords.json')

    exact_logs: List[Dict[str, str]] = []
    weights: Dict[str, int] = {}
    for path in dedupe_paths(candidates):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        tags = data.get('tags') if isinstance(data, dict) else None
        if not isinstance(tags, dict):
            continue
        for tag, info in tags.items():
            if not isinstance(info, dict):
                continue
            tag_text = str(tag or '').strip()
            if not tag_text:
                continue
            try:
                weights[tag_text] = max(weights.get(tag_text, 0), int(info.get('log_count') or 0))
            except (TypeError, ValueError):
                weights.setdefault(tag_text, 0)
            for sample in info.get('sample_logs') or []:
                if not isinstance(sample, dict):
                    continue
                message = normalize_log_message(sample.get('message') or '')
                if not is_precise_log_message(message):
                    continue
                exact_logs.append(
                    {
                        'tag': str(sample.get('tag') or tag_text).strip(),
                        'message': message,
                        'logger': 'log_keywords',
                        'path': str(sample.get('file') or '').replace('\\', '/'),
                    }
                )
    return exact_logs, weights


def rank_exact_logs(exact_logs: Sequence[Dict[str, str]], tag_weights: Dict[str, int]) -> List[Dict[str, str]]:
    if not tag_weights:
        return list(exact_logs)
    return sorted(
        exact_logs,
        key=lambda item: (
            -int(tag_weights.get(str(item.get('tag') or '').strip(), 0)),
            str(item.get('path') or ''),
            str(item.get('message') or ''),
        ),
    )


def source_suffix(path: Path) -> str:
    lower = path.name.lower()
    if lower.endswith('.gradle.kts'):
        return '.gradle'
    if lower == 'cmakelists.txt':
        return '.cmake'
    if lower in {'android.mk', 'application.mk'}:
        return '.mk'
    if lower.endswith('.map.txt'):
        return '.txt'
    return path.suffix.lower()


def relative_to_project_dirs(path: Path, project_dirs: Sequence[Path]) -> str:
    resolved = path.resolve()
    for root in project_dirs:
        try:
            return resolved.relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
    return path.name


def infer_preferred_paths_from_source(rel_path: str, path: Path) -> List[str]:
    """根据源码路径给 Deep 模式建议可优先读取的相对路径。"""
    rel = str(rel_path or '').replace('\\', '/').strip('/')
    if not rel:
        return []
    parts = [p for p in rel.split('/') if p]
    lower_parts = [p.lower() for p in parts]
    out: List[str] = []
    name = path.name.lower()
    if name in GUIDE_FILENAMES:
        out.append(rel)
    if name in {'androidmanifest.xml', 'build.gradle', 'build.gradle.kts', 'settings.gradle', 'settings.gradle.kts'}:
        out.append(rel)
    if len(parts) >= 3 and lower_parts[1:3] == ['src', 'main']:
        out.append('/'.join(parts[:3]))
    if len(parts) >= 4 and lower_parts[2:4] == ['src', 'main']:
        out.append('/'.join(parts[:4]))
    if 'cpp' in lower_parts:
        idx = lower_parts.index('cpp')
        out.append('/'.join(parts[: idx + 1]))
    if 'java' in lower_parts:
        idx = lower_parts.index('java')
        out.append('/'.join(parts[: idx + 1]))
    if 'kotlin' in lower_parts:
        idx = lower_parts.index('kotlin')
        out.append('/'.join(parts[: idx + 1]))
    if parts:
        first = parts[0]
        if first.lower() in {
            'app',
            'app-common',
            'app-k9mail',
            'app-thunderbird',
            'backend',
            'core',
            'feature',
            'legacy',
            'mail',
            'net',
            'parser',
            'playback',
            'storage',
            'terminal-emulator',
            'terminal-view',
            'termux-shared',
            'sync',
        }:
            out.append(first)
    return out


def read_text_prefix(path: Path, max_bytes: int) -> str:
    try:
        data = path.read_bytes()[:max_bytes]
        return data.decode('utf-8', errors='ignore')
    except OSError:
        return ''


def extract_packages(text: str) -> List[str]:
    out: List[str] = []
    patterns = [
        r'(?m)^\s*package\s+([A-Za-z_][A-Za-z0-9_.]*)\s*;?',
        r'\bnamespace\s*[= ]\s*["\']([^"\']+)["\']',
        r'\bapplicationId\s*[= ]\s*["\']([^"\']+)["\']',
        r'<manifest[^>]+package=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        out.extend(re.findall(pattern, text))
    return out


def extract_tags(text: str, path: Path) -> List[str]:
    """只抽取代码中真实声明或传入日志 API 的 TAG。

    这里刻意不把类名、文件名当作 TAG，避免前置工作流把“代码身份”
    误当成“日志证据”。类名会进入 Deep hints，前置规则只消费真实日志范围信号。
    """
    out: List[str] = []
    tag_defs = extract_tag_definitions(text)
    out.extend(tag_defs.values())
    out.extend(re.findall(r'\b(?:Log|Slog|Rlog|HwLog|HiLog|HLog|XLog|LogUtils|Logger|AppLog|KLog)\s*\.\s*(?:v|d|i|w|e|wtf|debug|info|warn|error)\s*\(\s*["\']([^"\']{2,80})["\']', text))
    out.extend(re.findall(r'\bTimber\s*\.\s*tag\s*\(\s*["\']([^"\']{2,80})["\']\s*\)', text))
    out.extend(re.findall(r'__android_log_print\s*\([^,]+,\s*["\']([^"\']{2,80})["\']', text))
    out.extend(re.findall(r'\bLOG_TAG\b\s+["\']([^"\']{2,80})["\']', text))
    return out


def extract_tag_definitions(text: str) -> Dict[str, str]:
    """抽取常见 Java/Kotlin/C++ TAG 常量，供后续解析日志调用时回填。"""
    defs: Dict[str, str] = {}
    patterns = [
        r'\b(?:private\s+|public\s+|protected\s+)?(?:static\s+)?(?:final\s+)?String\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*["\']([^"\']{2,80})["\']',
        r'\b(?:private\s+)?(?:const\s+)?val\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*["\']([^"\']{2,80})["\']',
        r'#\s*define\s+([A-Za-z_][A-Za-z0-9_]*)\s+["\']([^"\']{2,80})["\']',
    ]
    for pattern in patterns:
        for name, value in re.findall(pattern, text):
            key = str(name or '').strip()
            val = str(value or '').strip()
            if key and val and ('TAG' in key.upper() or key.upper().endswith('LOG')):
                defs[key] = val
    if 'LOG_TAG' in defs and 'TAG' not in defs:
        defs['TAG'] = defs['LOG_TAG']
    return defs


def extract_log_patterns(text: str, path: Path, rel_path: str) -> List[Dict[str, str]]:
    """抽取 1 类精确日志证据：TAG + 日志文本。

    支持 Android Log/Slog、常见私有 Log wrapper、Timber、EventLog 以及 native
    `__android_log_print` / `ALOG*` / `LOG*`。只收录能还原到真实 TAG 或真实日志文本的
    片段，语义词不会在这里生成。
    """
    out: List[Dict[str, str]] = []
    tag_defs = extract_tag_definitions(text)
    fallback_tag = tag_defs.get('TAG') or tag_defs.get('LOG_TAG') or ''
    logger_patterns = [
        (
            'android-log',
            r'\b(?P<logger>Log|Slog|Rlog|HwLog|HiLog|HLog|XLog|LogUtils|Logger|AppLog|KLog)\s*\.\s*(?:v|d|i|w|e|wtf|debug|info|warn|error)\s*\(\s*(?P<tag>[^,\n()]+)\s*,\s*["\'](?P<msg>[^"\']{3,220})["\']',
        ),
        (
            'timber-tag',
            r'\bTimber\s*\.\s*tag\s*\(\s*["\'](?P<tag>[^"\']{2,80})["\']\s*\)\s*\.\s*(?:v|d|i|w|e|wtf|debug|info|warn|error)\s*\(\s*["\'](?P<msg>[^"\']{3,220})["\']',
        ),
        (
            'timber',
            r'\bTimber\s*\.\s*(?:v|d|i|w|e|wtf|debug|info|warn|error)\s*\(\s*["\'](?P<msg>[^"\']{3,220})["\']',
        ),
        (
            'native-android-log',
            r'__android_log_print\s*\([^,]+,\s*(?P<tag>[^,\n()]+)\s*,\s*["\'](?P<msg>[^"\']{3,220})["\']',
        ),
        (
            'native-alog',
            r'\b(?:ALOG|LOG)(?:V|D|I|W|E|F)\s*\(\s*["\'](?P<msg>[^"\']{3,220})["\']',
        ),
        (
            'event-log',
            r'\bEventLog\s*\.\s*writeEvent\s*\(\s*(?P<tag>[^,\n()]+)\s*,\s*["\'](?P<msg>[^"\']{3,220})["\']',
        ),
    ]
    for logger, pattern in logger_patterns:
        for match in re.finditer(pattern, text):
            msg = normalize_log_message(match.groupdict().get('msg') or '')
            if not is_precise_log_message(msg):
                continue
            raw_tag = match.groupdict().get('tag') or fallback_tag
            tag = resolve_log_tag(raw_tag, tag_defs, fallback_tag)
            out.append(
                {
                    'tag': tag,
                    'message': msg,
                    'logger': logger,
                    'path': rel_path,
                }
            )
    return out


def resolve_log_tag(raw_tag: str, tag_defs: Dict[str, str], fallback_tag: str = '') -> str:
    raw = str(raw_tag or '').strip()
    if not raw:
        return fallback_tag
    literal = re.match(r'["\']([^"\']{2,80})["\']', raw)
    if literal:
        return literal.group(1).strip()
    name = re.sub(r'[^A-Za-z0-9_.$]+', '', raw)
    if name in tag_defs:
        return tag_defs[name]
    if name.split('.')[-1] in tag_defs:
        return tag_defs[name.split('.')[-1]]
    return fallback_tag


def normalize_log_message(message: str) -> str:
    text = re.sub(r'\s+', ' ', str(message or '').strip())
    # 去掉纯格式占位符留下的噪声；保留稳定文字片段用于正则拼接。
    text = re.sub(r'%[0-9.+\-]*[sdfoxXeEgGaAcCbBhH]', ' ', text)
    text = re.sub(r'\{[^{}]{0,20}\}', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def is_precise_log_message(message: str) -> bool:
    text = str(message or '').strip()
    lowered = text.lower().strip(" \t\r\n:;,.!()[]{}'\"")
    generic_singletons = {
        'activity',
        'binding',
        'channel',
        'connected',
        'connection',
        'launch',
        'package',
        'publishing',
        'receiver',
        'registering',
        'schedule',
        'service',
        'unbinding',
        'writing to',
    }
    # Java strings containing apostrophes can be truncated by the lightweight
    # regex scanner (for example "Couldn't" -> "Couldn").  Truncated fragments
    # and single broad nouns are too noisy for tier-1 matching.
    if lowered in generic_singletons or lowered in {'couldn', 'whoops, can'}:
        return False
    # Some Android business logs intentionally use very short domain events.
    # They are still precise when paired with a real TAG, for example
    # `RDMQueryService: checkin`.  Keep this whitelist narrow so broad words
    # such as "lock" / "unlock" still stay out of tier-1 matching.
    if lowered in {'checkin', 'check-in', 'check in', 'oobe', 'rpmb'}:
        return True
    if len(text) >= 5 and re.search(r'[_-]', text):
        return True
    if len(text) < 12 and not contains_cjk(text):
        return False
    if len(text.split()) <= 1:
        if lowered in {'error', 'failed', 'success', 'start', 'end'}:
            return False
        if not re.search(r'[:=()/#!-]', text) and len(text) < 18:
            return False
    return any(ch.isalpha() for ch in text) or contains_cjk(text)


def extract_android_components(text: str) -> List[str]:
    names = re.findall(r'<(?:activity|service|receiver|provider)\b[^>]*android:name=["\']([^"\']+)["\']', text)
    return [n.split('.')[-1] for n in names if n]


def extract_app_signals(text: str, path: Path) -> Dict[str, List[str]]:
    signals: List[str] = []
    capabilities: List[str] = []
    permissions: List[str] = []
    components: List[str] = []
    business_terms: List[str] = []

    permissions.extend(re.findall(r'<uses-permission\b[^>]*android:name=["\']([^"\']+)["\']', text))
    permissions.extend(re.findall(r'\bManifest\.permission\.([A-Z0-9_]+)\b', text))
    permissions.extend(re.findall(r'\b(android\.permission\.[A-Z0-9_.]+)\b', text))
    components.extend(extract_android_components(text))
    components.extend(re.findall(r'<intent-filter\b[\s\S]{0,600}?<action\b[^>]*android:name=["\']([^"\']+)["\']', text))
    components.extend(re.findall(r'<meta-data\b[^>]*android:name=["\']([^"\']+)["\']', text))
    components.extend(re.findall(r'<provider\b[^>]*android:authorities=["\']([^"\']+)["\']', text))

    for category, patterns in APP_CAPABILITY_PATTERNS.items():
        for pattern in patterns:
            for raw in re.findall(pattern, text):
                value = raw[0] if isinstance(raw, tuple) else raw
                value = str(value or '').strip()
                if not value:
                    continue
                capabilities.append(value)
                signals.append(value)
                if category in {'workmanager', 'database', 'notification', 'config'}:
                    business_terms.extend(split_camel_identifier(value))

    if source_suffix(path) in {'.gradle', '.kts'}:
        signals.extend(re.findall(r'\b(?:namespace|applicationId)\s*[= ]\s*["\']([^"\']+)["\']', text))
        signals.extend(re.findall(r'\bimplementation\s*\(?\s*["\']([^"\']+)["\']', text))

    signals.extend(components)
    signals.extend(permissions)
    return {
        'signals': signals,
        'capabilities': capabilities,
        'permissions': permissions,
        'components': components,
        'business_terms': business_terms,
    }


def extract_native_signals(text: str, path: Path) -> Dict[str, List[str]]:
    """抽取 native-heavy 项目的 JNI、so、log tag 和 trace 信号。"""
    signals: List[str] = []
    libraries: List[str] = []
    symbols: List[str] = []
    log_tags: List[str] = []
    trace_sections: List[str] = []
    business_terms: List[str] = []
    error_terms: List[str] = []
    suffix = source_suffix(path)

    if suffix in {'.cmake', '.mk', '.gradle'}:
        libraries.extend(extract_native_libraries_from_build(text))
    if suffix in {'.c', '.cc', '.cpp', '.cxx', '.h', '.hh', '.hpp', '.hxx'}:
        native_source = extract_native_source_signals(text)
        libraries.extend(native_source['libraries'])
        symbols.extend(native_source['symbols'])
        log_tags.extend(native_source['log_tags'])
        trace_sections.extend(native_source['trace_sections'])
        error_terms.extend(native_source['error_terms'])
    if suffix == '.txt' and path.name.lower().endswith('.map.txt'):
        symbols.extend(extract_map_symbols(text))

    libraries.extend(re.findall(r'\b(lib[A-Za-z0-9_.-]+\.so)\b', text))
    symbols.extend(re.findall(r'\b(?:JNI_OnLoad|ANativeActivity_onCreate|android_main)\b', text))
    signals.extend(libraries + symbols + log_tags + trace_sections)
    for value in signals:
        business_terms.extend(split_camel_identifier(value.replace('lib', '').replace('.so', '')))
    return {
        'signals': signals,
        'libraries': libraries,
        'symbols': symbols,
        'log_tags': log_tags,
        'trace_sections': trace_sections,
        'business_terms': business_terms,
        'error_terms': error_terms,
    }


def extract_native_libraries_from_build(text: str) -> List[str]:
    out: List[str] = []
    patterns = [
        r'\badd_(?:app_)?library\s*\(\s*([A-Za-z0-9_.-]+)',
        r'\btarget_link_libraries\s*\(\s*([A-Za-z0-9_.-]+)',
        r'\bproject\s*\(\s*["\']?([A-Za-z0-9_.-]+)',
        r'(?m)^\s*LOCAL_MODULE\s*[:+]?=\s*([A-Za-z0-9_.-]+)',
    ]
    for pattern in patterns:
        out.extend(re.findall(pattern, text))
    return dedupe(out, limit=80)


def extract_native_source_signals(text: str) -> Dict[str, List[str]]:
    libraries: List[str] = []
    symbols: List[str] = []
    log_tags: List[str] = []
    trace_sections: List[str] = []
    error_terms: List[str] = []

    log_tags.extend(re.findall(r'\bLOG_TAG\b\s+["\']([^"\']{2,80})["\']', text))
    log_tags.extend(re.findall(r'\bLOG_TAG\b\s*=\s*["\']([^"\']{2,80})["\']', text))
    log_tags.extend(re.findall(r'__android_log_print\s*\([^,]+,\s*["\']([^"\']{2,80})["\']', text))
    trace_sections.extend(re.findall(r'\b(?:ATrace_beginSection|ATrace_name|ATRACE_NAME)\s*\(\s*["\']([^"\']{2,120})["\']', text))
    trace_sections.extend(re.findall(r'\b(?:TraceScope|ScopedTrace|ScopedATrace)\s*\(\s*["\']([^"\']{2,120})["\']', text))

    symbols.extend(re.findall(r'\bJNIEXPORT\s+[A-Za-z0-9_*\s]+\s+JNICALL\s+(Java_[A-Za-z0-9_]+)', text))
    symbols.extend(re.findall(r'\bextern\s+"C"\s+JNIEXPORT\s+[A-Za-z0-9_*\s]+\s+(Java_[A-Za-z0-9_]+)', text))
    symbols.extend(re.findall(r'\{\s*["\']([^"\']{2,80})["\']\s*,\s*["\'][^"\']+["\']\s*,\s*reinterpret_cast<void\*>\(([^)]+)\)', text))
    symbols.extend(re.findall(r'\b(?:JNI_OnLoad|ANativeActivity_onCreate|android_main)\b', text))
    symbols.extend(re.findall(r'\b(?:AChoreographer_[A-Za-z0-9_]+|ANativeWindow_[A-Za-z0-9_]+|AHardwareBuffer_[A-Za-z0-9_]+)\b', text))
    symbols.extend(re.findall(r'\b[A-Za-z_][A-Za-z0-9_]*(?:Render|DrawFrame|DoTick|Tick|Update|Engine|Loop)\b', text))
    symbols.extend(re.findall(r'\b(?:pthread_setname_np|prctl)\s*\([^;]{0,120}["\']([^"\']{2,80})["\']', text))

    for raw in re.findall(r'\b[A-Za-z_][A-Za-z0-9_]*(?:Error|Failure|Failed|Abort|Crash)\b', text):
        error_terms.append(raw)
    if re.search(r'\b(?:abort|SIGSEGV|SIGABRT|signal|tombstone|backtrace)\b', text, flags=re.IGNORECASE):
        error_terms.extend(['SIGSEGV', 'SIGABRT', 'tombstone', 'backtrace'])
    return {
        'libraries': libraries,
        'symbols': flatten_regex_tuple_results(symbols),
        'log_tags': log_tags,
        'trace_sections': trace_sections,
        'error_terms': error_terms,
    }


def flatten_regex_tuple_results(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    for value in values:
        if isinstance(value, tuple):
            out.extend(str(item) for item in value if str(item or '').strip())
        else:
            out.append(str(value))
    return out


def extract_map_symbols(text: str) -> List[str]:
    symbols: List[str] = []
    for line in text.splitlines():
        if len(line) > 240:
            continue
        symbols.extend(re.findall(r'\b[A-Za-z_][A-Za-z0-9_:~]*(?:JNI|Jni|Native|Activity|Render|Engine|Tick|Frame)[A-Za-z0-9_:~]*\b', line))
    return symbols


def extract_gradle_modules(project_dirs: Sequence[Path]) -> List[str]:
    modules: List[str] = []
    for root in project_dirs:
        settings_files = list(root.glob('settings.gradle')) + list(root.glob('settings.gradle.kts'))
        for settings in settings_files:
            text = read_text_prefix(settings, 256_000)
            modules.extend(re.findall(r'include\s*\(?\s*["\'](:[^"\']+)["\']', text))
            modules.extend(re.findall(r'["\'](:[A-Za-z0-9_:.-]+)["\']', text))
        for build in root.rglob('build.gradle*'):
            if any(part.lower() in IGNORED_DIRS for part in build.parts):
                continue
            try:
                rel = build.parent.relative_to(root).as_posix()
            except ValueError:
                rel = build.parent.name
            if rel and rel != '.':
                modules.append(':' + rel.replace('/', ':'))
    return modules


def extract_log_messages(text: str) -> List[str]:
    messages: List[str] = []
    # 只做行级启发式抽取，避免解析 Java/Kotlin 语法导致脚本变重。
    for line in text.splitlines():
        if re.search(r'\b(?:Log|Slog|Timber)\s*\.\s*(?:v|d|i|w|e|wtf)\s*\(', line):
            messages.extend(re.findall(r'["\']([^"\']{3,160})["\']', line))
        if re.search(r'\b(?:printStackTrace|Throwable|Exception|Error)\b', line):
            messages.extend(re.findall(r'["\']([^"\']{3,160})["\']', line))
    return messages


def extract_error_terms(text: str) -> List[str]:
    out: List[str] = []
    for raw in re.findall(r'\b[A-Z][A-Za-z0-9_]*(?:Exception|Error|Failure|Failed)\b', text):
        out.append(raw)
    for raw in re.findall(r'\b(?:ERR|ERROR|FAIL|FAILED|EXCEPTION)_[A-Z0-9_]{2,80}\b', text):
        out.append(raw)
    return out


def extract_business_terms(text: str) -> List[str]:
    terms = split_words(text)
    important = {
        'rdm',
        'lock',
        'unlock',
        'provision',
        'check',
        'checkin',
        'check-in',
        'clear',
        'unbind',
        'bind',
        'payment',
        'remind',
        'notification',
        'policy',
        'devicepolicy',
        'device',
        'owner',
        'escape',
        'activate',
        'activation',
        '锁机',
        '锁定',
        '解锁',
        '激活',
        '注销',
        '解绑',
        '缴费',
        '提醒',
        '防逃逸',
        '设备识别',
    }
    out = [t for t in terms if t.lower() in important or (contains_cjk(t) and len(t) <= 8)]
    for term in important:
        if contains_cjk(term) and term in text:
            out.append(term)
    return filter_terms(out)


def build_generated_rule_pack(
    bundle_id: str,
    pack_id: str,
    title: str,
    description: str,
    project_preset: str,
    profiles: Sequence[str],
    scan: ScanResult,
) -> Dict[str, Any]:
    prefix = slugify(pack_id)
    scope_keywords = tier2_scope_terms(scan, project_preset, limit=160)
    deep_hints = build_deep_hints(project_preset, profiles, scan)
    rules: List[Dict[str, Any]] = []
    rules.extend(build_exact_log_rules(prefix, title, bundle_id, scan.exact_logs))
    if scope_keywords or scan.packages:
        rules.append(
            make_rule(
                f'{prefix}-tier2-project-scope',
                f'{title} tier2 project scope signals',
                'generic_log_error',
                'low',
                bundle_id,
                ['tier2', 'scope', 'tag', 'package', 'component'],
                keywords=scope_keywords,
                packages=scan.packages[:30],
            )
        )
    if project_preset == 'app' and scan.permissions:
        rules.append(
            make_rule(
                f'{prefix}-tier2-permission-scope',
                f'{title} tier2 permission/config scope',
                'android_permission_denial',
                'medium',
                bundle_id,
                ['tier2', 'app', 'permission', 'config'],
                keywords=scan.permissions[:80],
            )
        )
    native_scope = dedupe(scan.native_libraries + scan.native_symbols + scan.native_log_tags + scan.native_trace_sections, limit=120)
    if project_preset == 'native' and native_scope:
        rules.append(
            make_rule(
                f'{prefix}-tier2-native-scope',
                f'{title} tier2 native library/JNI/log/trace scope',
                'android_native_crash',
                'high',
                bundle_id,
                ['tier2', 'native', 'jni', 'symbol', 'library', 'trace'],
                keywords=native_scope,
                regex=native_symbol_regexes(scan.native_libraries + scan.native_symbols)
                + native_trace_regexes(scan.native_log_tags + scan.native_trace_sections),
            )
        )
    if scan.components:
        rules.append(
            make_rule(
                f'{prefix}-tier2-android-components',
                f'{title} tier2 Android component/action scope',
                'android_framework_behavior',
                'medium',
                bundle_id,
                ['tier2', 'component', 'android'],
                keywords=scan.components[:80],
            )
        )
    for rule in build_profile_rules(
        prefix=prefix,
        title=title,
        bundle_id=bundle_id,
        profiles=profiles,
        scope_keywords=scope_keywords,
        scan=scan,
    ):
        rules.append(rule)

    return {
        'version': 1,
        'id': pack_id,
        'title': title,
        'description': description,
        'source_bundle_ids': [bundle_id],
        'rules': rules,
        'metadata': {
            'generator': 'skills/android-log-rule-builder',
            'project_preset': project_preset,
            'profiles': list(profiles),
            'scanned_files': scan.scanned_files,
            'signal_policy': {
                'front_workflow': [
                    '1 类：真实源码日志调用中抽取的 TAG + message 正则',
                    '2 类：真实 TAG / package / component / action / permission / native symbol 范围信号',
                ],
                'deep_only': [
                    '3 类：CLAUDE.md、项目专属 Skill、deep_hints 中的代码入口和扩展线索',
                    '4 类：仅在 1/2/3 类失败后，把真实项目 TAG/包名/组件名与用户语义词组合兜底',
                ],
            },
            'exact_log_count': len(scan.exact_logs),
            'exact_logs': scan.exact_logs[:80],
            'tier2_scope_terms': scope_keywords[:120],
            'packages': scan.packages[:20],
            'tags': scan.tags[:40],
            'app_signals': scan.app_signals[:60],
            'app_capability_terms': scan.app_capability_terms[:60],
            'permissions': scan.permissions[:40],
            'gradle_modules': scan.gradle_modules[:40],
            'native_signals': scan.native_signals[:60],
            'native_libraries': scan.native_libraries[:40],
            'native_symbols': scan.native_symbols[:60],
            'native_log_tags': scan.native_log_tags[:40],
            'native_trace_sections': scan.native_trace_sections[:40],
            'deep_hints': deep_hints,
        },
    }


def build_exact_log_rules(
    prefix: str,
    title: str,
    bundle_id: str,
    exact_logs: Sequence[Dict[str, str]],
) -> List[Dict[str, Any]]:
    """把源码中的真实日志调用拆成 1 类精确规则。

    每条正则同时包含 TAG 和 message，避免单独 TAG 或单独失败词把无关日志拉进来。
    """
    rules: List[Dict[str, Any]] = []
    regexes = exact_log_regexes(exact_logs)
    for index, chunk in enumerate(chunk_list(regexes, 20), start=1):
        rules.append(
            make_rule(
                f'{prefix}-tier1-exact-log-{index}',
                f'{title} tier1 exact TAG/message logs #{index}',
                'android_business_spec',
                'medium',
                bundle_id,
                ['tier1', 'exact-log', 'tag-message'],
                keywords=[],
                regex=chunk,
            )
        )
    return rules


def exact_log_regexes(exact_logs: Sequence[Dict[str, str]]) -> List[str]:
    regexes: List[str] = []
    for item in exact_logs:
        message = str(item.get('message') or '').strip()
        msg_regex = log_message_to_regex(message)
        if not msg_regex:
            continue
        tag = str(item.get('tag') or '').strip()
        if tag:
            tag_regex = term_to_regex(tag)
            regexes.append(r'(?i)(?:' + tag_regex + r'[\s\S]{0,240}' + msg_regex + r'|' + msg_regex + r'[\s\S]{0,240}' + tag_regex + r')')
        else:
            regexes.append(r'(?i)' + msg_regex)
    return dedupe(regexes, limit=240)


def log_message_to_regex(message: str) -> str:
    text = normalize_log_message(message)
    if not is_precise_log_message(text):
        return ''
    if len(text) > 96:
        text = text[:96]
    escaped = re.escape(text)
    escaped = re.sub(r'\\\s+', r'\\s+', escaped)
    return escaped


def term_to_regex(term: str) -> str:
    text = str(term or '').strip()
    escaped = re.escape(text)
    if re.fullmatch(r'[A-Za-z0-9_]+', text):
        return r'(?<![A-Za-z0-9_])' + escaped + r'(?![A-Za-z0-9_])'
    return escaped


def chunk_list(values: Sequence[str], size: int) -> List[List[str]]:
    return [list(values[i : i + size]) for i in range(0, len(values), size) if values[i : i + size]]


def tier2_scope_terms(scan: ScanResult, project_preset: str, limit: int = 160) -> List[str]:
    """生成 2 类前置范围信号。

    只允许来自源码/Manifest/构建文件中的真实身份信号：TAG、包名、组件、action、
    permission、native symbol/trace 等；不混入业务语义词。
    """
    values = (
        filter_code_identity_terms(scan.business_terms)
        + scan.tags
        + scan.components
        + scan.permissions
        + scan.gradle_modules
    )
    if project_preset == 'app':
        values += scan.app_signals + scan.app_capability_terms
    values += (
        scan.packages
        + scan.native_libraries
        + scan.native_symbols
        + scan.native_log_tags
        + scan.native_trace_sections
    )
    return dedupe(filter_app_signal_terms(filter_identity_terms(values)), limit=limit)


def filter_code_identity_terms(values: Iterable[str]) -> List[str]:
    """从配置关键词/路径词里保留看起来像代码身份的项。

    这给人工维护的 bundle keywords 留一个入口：`DownloadService`、`AccountCreator`
    这类真实类/组件名可以进入 2 类 scope；`lock`、`sync`、`error`、中文描述等语义词仍然
    只能进入 Deep hints。
    """
    suffixes = (
        'Activity',
        'Service',
        'Receiver',
        'Provider',
        'Worker',
        'Manager',
        'Controller',
        'Repository',
        'Dao',
        'Database',
        'Store',
        'Client',
        'Settings',
        'Widget',
        'Request',
        'Command',
        'Session',
        'Bridge',
        'Policy',
        'Result',
        'Helper',
        'Factory',
    )
    generic_code_identities = {
        'Activity',
        'Service',
        'Receiver',
        'Provider',
        'Worker',
        'Manager',
        'Controller',
        'Repository',
        'Dao',
        'Database',
        'Store',
        'Client',
        'Settings',
        'Widget',
        'Request',
        'Command',
        'Session',
        'Bridge',
        'Policy',
        'Result',
        'Helper',
        'Factory',
        'Queue',
    }
    out: List[str] = []
    for value in values:
        text = str(value or '').strip()
        if not text or contains_cjk(text) or len(text) > 120:
            continue
        lower = text.lower()
        if lower in GENERIC_IDENTITY_TERMS or lower in {'lock', 'unlock', 'sync', 'network', 'policy', 'error', 'fail', 'failed'}:
            continue
        if text in generic_code_identities:
            continue
        if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+', text):
            out.append(text)
        elif text.endswith(suffixes):
            out.append(text)
        elif re.fullmatch(r'[A-Z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*', text):
            out.append(text)
        elif re.fullmatch(r'[A-Z0-9_]{4,80}', text):
            out.append(text)
        elif re.fullmatch(r'lib[A-Za-z0-9_.-]+\.so', text):
            out.append(text)
    return dedupe(out, limit=120)


def build_deep_hints(project_preset: str, profiles: Sequence[str], scan: ScanResult) -> Dict[str, Any]:
    """生成 Deep 阶段可消费的轻量线索。

    这些字段不会参与首轮规则命中，只用于后续 Deep 分析时缩小代码检索范围、
    提醒 AI 优先使用相关 Skill/项目指南，避免重新在全量代码里盲搜。
    """
    code_terms = dedupe(
        scan.tags
        + scan.components
        + scan.business_terms
        + scan.error_terms
        + scan.app_signals
        + scan.app_capability_terms
        + scan.native_libraries
        + scan.native_symbols
        + scan.native_log_tags
        + scan.native_trace_sections,
        limit=120,
    )
    related_skills = ['android-log-rule-builder']
    if project_preset:
        related_skills.append(f'android-log-rule-builder:{project_preset}')
    related_skills.extend(f'android-log-rule-builder:{profile}' for profile in profiles)
    case_tags = dedupe(
        [project_preset]
        + list(profiles)
        + ['android-log-rule-builder']
        + _capability_case_tags(scan)
        + (['native'] if project_preset == 'native' else []),
        limit=40,
    )
    return {
        'version': 1,
        'search_order': [
            '1 类 exact_logs：优先使用 TAG + message 精确日志',
            '2 类 scope_terms：再扩展到 TAG / package / component / action / permission / native symbol',
            '3 类 code_search_terms：Deep 阶段读取 CLAUDE.md、项目专属 Skill 和源码入口',
            '4 类 fuzzy_semantic_fallback：只有前三层失败后，才把真实项目 scope 与用户语义词组合兜底',
        ],
        'exact_logs': scan.exact_logs[:80],
        'tier2_scope_terms': tier2_scope_terms(scan, project_preset, limit=120),
        'code_search_terms': code_terms,
        'preferred_paths': scan.preferred_paths[:80],
        'related_skills': dedupe(related_skills, limit=20),
        'claude_md_candidates': scan.claude_md_candidates[:20],
        'case_tags': case_tags,
    }


def _capability_case_tags(scan: ScanResult) -> List[str]:
    text = ' '.join(scan.app_capability_terms + scan.app_signals + scan.native_signals).lower()
    tags: List[str] = []
    checks = {
        'sync': ('sync', 'upload', 'download', 'remoteoperation'),
        'account-auth': ('account', 'auth', 'oauth', 'token', 'login', 'webdav', 'imap', 'smtp'),
        'background-task': ('worker', 'job', 'service', 'receiver', 'foreground'),
        'process-terminal': ('termux', 'terminal', 'shell', 'process', 'command', 'execution'),
        'database': ('database', 'room', 'sqlite', 'datastore'),
        'network': ('retrofit', 'okhttp', 'http', 'websocket'),
        'native-symbol': ('jni', 'native', '.so', 'atrace', 'choreographer'),
    }
    for tag, needles in checks.items():
        if any(needle in text for needle in needles):
            tags.append(tag)
    return tags


def build_profile_rules(
    prefix: str,
    title: str,
    bundle_id: str,
    profiles: Sequence[str],
    scope_keywords: Sequence[str],
    scan: ScanResult,
) -> List[Dict[str, Any]]:
    """按问题类型生成面向首轮工作流的规则。

    Profile 规则仍然只使用 1/2 类信号：稳定性/内存/性能/XTS 的通用现象必须
    与项目 scope 同时出现，避免把无关应用 crash、系统慢日志误判为当前项目证据。
    """
    rules: List[Dict[str, Any]] = []
    scope = dedupe(list(scope_keywords) + scan.packages + scan.tags + scan.components, limit=100)
    for profile in profiles:
        if profile == 'functional':
            rules.append(
                make_rule(
                    f'{prefix}-functional-tier2-scope',
                    f'{title} functional tier2 scope signals',
                    PROFILE_RULE_ISSUE_TYPES[profile],
                    'medium',
                    bundle_id,
                    ['profile', 'functional', 'tier2', 'scope'],
                    keywords=scope[:120],
                )
            )
        elif profile == 'stability':
            regex = scoped_near_regexes(
                scope,
                [
                    r'FATAL EXCEPTION',
                    r'AndroidRuntime',
                    r'ANR',
                    r'ApplicationExitInfo',
                    r'Caused by',
                    r'Native crash',
                    r'tombstone',
                    r'SIGSEGV',
                    r'SIGABRT',
                    r'backtrace',
                    r'[A-Za-z0-9_.]*(?:Exception|Error|Failure|Failed)',
                ],
            )
            rules.append(
                make_rule(
                    f'{prefix}-stability-scoped-crash',
                    f'{title} stability/crash signals scoped to project',
                    PROFILE_RULE_ISSUE_TYPES[profile],
                    'high',
                    bundle_id,
                    ['profile', 'stability', 'crash', 'tier2-scoped'],
                    keywords=[],
                    regex=regex,
                )
            )
        elif profile == 'xts':
            regex = scoped_near_regexes(
                scope,
                [r'CTS', r'GTS', r'XTS', r'Tradefed', r'AssertionError', r'test[\s\S]{0,40}fail', r'FAILURES!!!'],
            )
            rules.append(
                make_rule(
                    f'{prefix}-xts-scoped-test',
                    f'{title} XTS/test failure signals scoped to project',
                    PROFILE_RULE_ISSUE_TYPES[profile],
                    'medium',
                    bundle_id,
                    ['profile', 'xts', 'test', 'tier2-scoped'],
                    keywords=[],
                    regex=regex,
                )
            )
        elif profile == 'memory':
            regex = scoped_near_regexes(
                scope,
                [r'OutOfMemoryError', r'\bOOM\b', r'LMKD', r'low memory', r'meminfo', r'smaps', r'hprof', r'\bPSS\b', r'\bRSS\b', r'GC freed'],
            )
            rules.append(
                make_rule(
                    f'{prefix}-memory-scoped',
                    f'{title} memory signals scoped to project',
                    PROFILE_RULE_ISSUE_TYPES[profile],
                    'high',
                    bundle_id,
                    ['profile', 'memory', 'oom', 'tier2-scoped'],
                    keywords=[],
                    regex=regex,
                )
            )
        elif profile == 'performance':
            regex = scoped_near_regexes(
                scope,
                [r'Skipped \d+ frames', r'Choreographer', r'\bslow\b', r'jank', r'binder[\s\S]{0,40}slow', r'trace', r'Perfetto', r'latency'],
            )
            rules.append(
                make_rule(
                    f'{prefix}-performance-scoped',
                    f'{title} performance signals scoped to project',
                    PROFILE_RULE_ISSUE_TYPES[profile],
                    'medium',
                    bundle_id,
                    ['profile', 'performance', 'latency', 'tier2-scoped'],
                    keywords=[],
                    regex=regex,
                )
            )
    return [rule for rule in rules if any((rule.get('match') or {}).get(key) for key in ('keywords', 'packages', 'regex'))]


def app_complex_signal_groups(app_keywords: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """把复杂 App 常见能力拆成更窄的规则组，减少后续 evidence 里只有泛泛 app 信号。"""
    groups = {
        'sync-account': {
            'title': 'sync/account/auth signals',
            'issue_type': 'android_business_spec',
            'severity': 'medium',
            'tags': ['app', 'sync', 'account', 'auth'],
            'keywords': filter_keywords_by_regex(
                app_keywords,
                r'(?i)(sync|upload|download|remoteoperation|account|auth|oauth|token|login|webdav|owncloud|nextcloud|imap|smtp|mail)',
            ),
            'regex': [],
        },
        'background-task': {
            'title': 'background worker/service signals',
            'issue_type': 'android_framework_behavior',
            'severity': 'medium',
            'tags': ['app', 'background', 'worker', 'service'],
            'keywords': filter_keywords_by_regex(
                app_keywords,
                r'(?i)(worker|workmanager|workrequest|job|scheduler|receiver|service|foreground|alarm|pendingintent)',
            ),
            'regex': [],
        },
        'process-terminal': {
            'title': 'process/terminal command signals',
            'issue_type': 'android_framework_behavior',
            'severity': 'medium',
            'tags': ['app', 'process', 'terminal', 'command'],
            'keywords': filter_keywords_by_regex(
                app_keywords,
                r'(?i)(termux|terminal|shell|process|command|execution|runner|exec|pty|session)',
            ),
            'regex': [],
        },
    }
    return {
        group_id: group
        for group_id, group in groups.items()
        if group['keywords']
    }


def filter_keywords_by_regex(values: Sequence[str], pattern: str, limit: int = 80) -> List[str]:
    out = []
    for value in values:
        text = str(value or '').strip()
        if text and re.search(pattern, text):
            out.append(text)
    return dedupe(out, limit=limit)


def scoped_near_regexes(scope_terms: Sequence[str], signal_regexes: Sequence[str], limit: int = 20) -> List[str]:
    """生成“项目范围信号 + 通用现象”共现正则。

    规则引擎里的 match 条件是 OR 关系，所以这里不能把 scope_terms 放到 keywords，
    必须在同一个 regex 内表达共现约束。
    """
    scopes = [
        term_to_regex(term)
        for term in scope_terms
        if 2 <= len(str(term or '').strip()) <= 120 and not contains_cjk(str(term or ''))
    ]
    if not scopes:
        return []
    scope_group = r'(?:' + '|'.join(scopes[:40]) + r')'
    signals = [str(item or '').strip() for item in signal_regexes if str(item or '').strip()]
    regexes: List[str] = []
    for signal in signals[:limit]:
        regexes.append(r'(?i)(?:' + scope_group + r'[\s\S]{0,800}(?:' + signal + r')|(?:' + signal + r')[\s\S]{0,800}' + scope_group + r')')
    return regexes


def make_rule(
    rule_id: str,
    title: str,
    issue_type: str,
    severity: str,
    bundle_id: str,
    tags: Sequence[str],
    keywords: Sequence[str],
    packages: Sequence[str] = (),
    regex: Sequence[str] = (),
) -> Dict[str, Any]:
    match: Dict[str, Any] = {}
    if keywords:
        match['keywords'] = dedupe([str(x) for x in keywords if str(x).strip()], limit=120)
    if packages:
        match['packages'] = dedupe([str(x) for x in packages if str(x).strip()], limit=40)
    if regex:
        match['regex'] = dedupe([str(x) for x in regex if str(x).strip()], limit=20)
    return {
        'id': slugify(rule_id),
        'title': title,
        'issue_type': issue_type,
        'severity': severity,
        'source_bundle_ids': [bundle_id],
        'tags': list(tags),
        'match': match,
    }


def business_regexes(terms: Sequence[str]) -> List[str]:
    ascii_terms = [re.escape(t) for t in terms if re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{2,40}', t)]
    if not ascii_terms:
        return []
    return [r'(?i)\b(' + '|'.join(ascii_terms[:30]) + r')\b']


def failure_regexes(terms: Sequence[str]) -> List[str]:
    ascii_terms = [re.escape(t) for t in terms if re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{2,40}', t)]
    failure = r'(fail(?:ed|ure)?|error|exception|timeout|denied|invalid|crash)'
    if ascii_terms:
        return [r'(?i)\b(' + '|'.join(ascii_terms[:20]) + r').{0,120}\b' + failure]
    return [r'(?i)\b' + failure + r'\b']


def native_symbol_regexes(terms: Sequence[str]) -> List[str]:
    ascii_terms = [
        re.escape(t)
        for t in terms
        if re.fullmatch(r'[A-Za-z0-9_.$:+~-]{3,80}', str(t or ''))
    ]
    if not ascii_terms:
        return []
    return [r'(?i)\b(' + '|'.join(ascii_terms[:30]) + r')\b']


def native_trace_regexes(terms: Sequence[str]) -> List[str]:
    ascii_terms = [
        re.escape(t)
        for t in terms
        if re.fullmatch(r'[A-Za-z0-9_ .$:+~-]{3,100}', str(t or ''))
    ]
    trace = r'(trace|Choreographer|Skipped \d+ frames|jank|frame|render|latency|slow)'
    if ascii_terms:
        return [r'(?i)(' + '|'.join(ascii_terms[:24]) + r').{0,120}' + trace]
    return [r'(?i)' + trace]


def validate_pack(pack: Dict[str, Any], paths_config: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    bundle_ids = {str(b.get('id') or '') for b in (paths_config.get('bundles') or [])}
    pack_id = str(pack.get('id') or '').strip()
    if not pack_id:
        errors.append('规则包缺少 id')
    source_bundle_ids = [str(x) for x in (pack.get('source_bundle_ids') or []) if x]
    if not source_bundle_ids:
        errors.append(f'规则包 {pack_id or "<unknown>"} 缺少 source_bundle_ids')
    for bundle_id in source_bundle_ids:
        if bundle_id not in bundle_ids:
            errors.append(f'规则包引用了未配置 bundle：{bundle_id}')
    rules = pack.get('rules')
    if not isinstance(rules, list) or not rules:
        errors.append(f'规则包 {pack_id or "<unknown>"} 缺少 rules')
        rules = []
    seen_rules = set()
    for idx, rule in enumerate(rules):
        if not isinstance(rule, dict):
            errors.append(f'rules[{idx}] 不是对象')
            continue
        rule_id = str(rule.get('id') or '').strip()
        if not rule_id:
            errors.append(f'rules[{idx}] 缺少 id')
        elif rule_id in seen_rules:
            errors.append(f'重复规则 id：{rule_id}')
        seen_rules.add(rule_id)
        if not rule.get('title'):
            errors.append(f'规则 {rule_id or idx} 缺少 title')
        if rule.get('issue_type') not in VALID_ISSUE_TYPES:
            errors.append(f'规则 {rule_id or idx} issue_type 非法：{rule.get("issue_type")}')
        if str(rule.get('severity') or '').lower() not in VALID_SEVERITIES:
            errors.append(f'规则 {rule_id or idx} severity 非法：{rule.get("severity")}')
        match = rule.get('match')
        if not isinstance(match, dict):
            errors.append(f'规则 {rule_id or idx} 缺少 match 对象')
            continue
        if not any(match.get(key) for key in ('keywords', 'packages', 'regex', 'paths', 'kinds')):
            errors.append(f'规则 {rule_id or idx} match 至少需要一个非空条件')
        for pattern in match.get('regex') or []:
            try:
                re.compile(str(pattern))
            except re.error as exc:
                errors.append(f'规则 {rule_id or idx} regex 无法编译：{pattern} ({exc})')
        rule_bundles = [str(x) for x in (rule.get('source_bundle_ids') or source_bundle_ids) if x]
        for bundle_id in rule_bundles:
            if bundle_id not in bundle_ids:
                errors.append(f'规则 {rule_id or idx} 引用了未配置 bundle：{bundle_id}')
        keywords = match.get('keywords') or []
        if any(str(x).lower() in {'error', 'fail', 'failed', 'exception'} for x in keywords) and len(keywords) < 3:
            warnings.append(f'规则 {rule_id or idx} 过于泛化，建议补充项目 TAG 或业务词')
        fuzzy_terms = {'lock', 'unlock', 'sync', 'network', 'policy', 'error', 'fail', 'failed', 'exception', 'crash'}
        fuzzy_hits = [str(x) for x in keywords if str(x).strip().lower() in fuzzy_terms]
        if fuzzy_hits and 'tier1' not in (rule.get('tags') or []) and 'tier2' not in (rule.get('tags') or []):
            warnings.append(f'规则 {rule_id or idx} 包含疑似语义泛词 {fuzzy_hits}，建议只放入 Deep hints 或与真实 TAG 组合')
    return {'errors': errors, 'warnings': warnings, 'pack_id': pack_id, 'rule_count': len(rules)}


def normalize_rule_packs(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    if isinstance(data.get('rule_packs'), list):
        return [p for p in data['rule_packs'] if isinstance(p, dict)]
    return [data]


def load_rule_pack(knowledge_dir: Path, bundle_id: str, rule_pack_id: str) -> Dict[str, Any]:
    path = rule_pack_path(knowledge_dir, bundle_id, rule_pack_id)
    data = read_json(path)
    packs = normalize_rule_packs(data)
    for pack in packs:
        if str(pack.get('id') or '') == rule_pack_id:
            return pack
    raise RulePackError(f'规则包文件存在但找不到 id={rule_pack_id}：{path}')


def write_rule_pack(knowledge_dir: Path, bundle_id: str, pack: Dict[str, Any]) -> Path:
    path = rule_pack_path(knowledge_dir, bundle_id, str(pack.get('id') or ''))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(pack, f, ensure_ascii=False, indent=2)
        f.write('\n')
    return path


def ensure_bundle_manifest(
    knowledge_dir: Path,
    bundle_id: str,
    source_bundle: Dict[str, Any],
    project_dirs: Sequence[Path],
    rule_pack_id: str,
    project_preset: str = '',
    profiles: Sequence[str] = (),
    deep_hints: Optional[Dict[str, Any]] = None,
) -> Path:
    """登记生成规则包，确保 Android 分析默认会加载它。"""
    bundle_dir = Path(knowledge_dir) / 'bundles' / safe_filename(bundle_id)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = bundle_dir / 'bundle.json'
    if manifest_path.is_file():
        try:
            manifest = read_json(manifest_path)
        except RulePackError:
            manifest = {}
    else:
        manifest = {}
    manifest['id'] = str(manifest.get('id') or bundle_id)
    manifest['title'] = str(manifest.get('title') or source_bundle.get('title') or source_bundle.get('name') or bundle_id)
    if project_dirs and not manifest.get('source_path'):
        manifest['source_path'] = str(project_dirs[0]).replace('\\', '/')
    manifest['source_bundle_id'] = str(manifest.get('source_bundle_id') or bundle_id)
    manifest['description'] = str(
        manifest.get('description')
        or source_bundle.get('summary')
        or source_bundle.get('description')
        or f'Local shared rules and cases for {bundle_id}.'
    )
    rule_packs = [str(x) for x in (manifest.get('rule_packs') or []) if str(x).strip()]
    if rule_pack_id not in rule_packs:
        rule_packs.append(rule_pack_id)
    manifest['rule_packs'] = rule_packs
    if not manifest.get('case_indexes'):
        manifest['case_indexes'] = ['indexes/case_cards.jsonl']
    if project_preset and not manifest.get('project_preset'):
        manifest['project_preset'] = project_preset
    if profiles:
        supported_profiles = [str(x) for x in (manifest.get('supported_profiles') or []) if str(x).strip()]
        manifest['supported_profiles'] = dedupe(supported_profiles + list(profiles), limit=len(VALID_PROFILES))
        profile_overrides = manifest.get('profile_overrides')
        if not isinstance(profile_overrides, dict):
            profile_overrides = {}
        for profile in profiles:
            item = profile_overrides.get(profile)
            if not isinstance(item, dict):
                item = {}
            item_packs = [str(x) for x in (item.get('rule_packs') or []) if str(x).strip()]
            if rule_pack_id not in item_packs:
                item_packs.append(rule_pack_id)
            item['rule_packs'] = item_packs
            item.setdefault('issue_type', PROFILE_RULE_ISSUE_TYPES.get(profile, 'unknown'))
            profile_overrides[profile] = item
        manifest['profile_overrides'] = profile_overrides
    if deep_hints:
        manifest['deep_hints'] = deep_hints
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write('\n')
    return manifest_path


def rule_pack_path(knowledge_dir: Path, bundle_id: str, rule_pack_id: str) -> Path:
    if not rule_pack_id:
        raise RulePackError('缺少 rule_pack_id')
    return rules_dir_for(knowledge_dir, bundle_id) / f'{safe_filename(rule_pack_id)}.json'


def rules_dir_for(knowledge_dir: Path, bundle_id: str) -> Path:
    return Path(knowledge_dir) / 'bundles' / safe_filename(bundle_id) / 'rules'


def find_rule(pack: Dict[str, Any], rule_id: str) -> Optional[Dict[str, Any]]:
    if not rule_id:
        return None
    for rule in pack.get('rules') or []:
        if str(rule.get('id') or '') == rule_id:
            return rule
    return None


def load_rule_input(args: argparse.Namespace) -> Dict[str, Any]:
    if args.rule_json:
        try:
            data = json.loads(args.rule_json)
        except json.JSONDecodeError as exc:
            raise RulePackError(f'--rule-json 不是合法 JSON：{exc}') from exc
    else:
        data = read_json(Path(args.rule_file))
    if not isinstance(data, dict):
        raise RulePackError('规则输入必须是 JSON 对象')
    return data


def load_log_items(path: Path) -> List[Tuple[str, str]]:
    if not path.exists():
        raise RulePackError(f'日志路径不存在：{path}')
    items: List[Tuple[str, str]] = []
    if path.is_dir():
        for file in path.rglob('*'):
            if file.is_file() and file.stat().st_size <= 5_000_000:
                text = read_text_prefix(file, 300_000)
                if text:
                    items.append((str(file), text))
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist()[:500]:
                if info.is_dir() or info.file_size > 5_000_000:
                    continue
                try:
                    data = zf.read(info, pwd=None)[:300_000]
                except (OSError, RuntimeError, zipfile.BadZipFile):
                    continue
                text = data.decode('utf-8', errors='ignore')
                if text:
                    items.append((info.filename, text))
    else:
        text = read_text_prefix(path, 5_000_000)
        if text:
            items.append((str(path), text))
    return items


def match_pack_against_logs(pack: Dict[str, Any], log_items: Sequence[Tuple[str, str]]) -> List[Dict[str, Any]]:
    hits: List[Dict[str, Any]] = []
    for log_path, text in log_items:
        lower_path = log_path.lower()
        for rule in pack.get('rules') or []:
            match = rule.get('match') or {}
            paths = [str(x).lower() for x in (match.get('paths') or []) if x]
            if paths and not any(p in lower_path for p in paths):
                continue
            found_terms: List[str] = []
            for keyword in (match.get('keywords') or []) + (match.get('packages') or []):
                kw = str(keyword or '').strip()
                if kw and keyword_in_text(kw, text):
                    found_terms.append(kw)
            regex_hits: List[str] = []
            for pattern in match.get('regex') or []:
                try:
                    if re.search(str(pattern), text, flags=re.IGNORECASE | re.MULTILINE):
                        regex_hits.append(str(pattern))
                except re.error:
                    continue
            if found_terms or regex_hits:
                hits.append(
                    {
                        'rule_id': rule.get('id'),
                        'rule_title': rule.get('title'),
                        'path': log_path,
                        'matched_terms': dedupe(found_terms, limit=12),
                        'regex_hits': regex_hits[:6],
                    }
                )
    return hits


def keyword_in_text(keyword: str, text: str) -> bool:
    if re.fullmatch(r'[A-Za-z0-9_.$-]+', keyword):
        pattern = r'(?<![A-Za-z0-9_])' + re.escape(keyword) + r'(?![A-Za-z0-9_])'
        return re.search(pattern, text, flags=re.IGNORECASE) is not None
    return keyword.lower() in text.lower()


def read_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise RulePackError(f'文件不存在：{path}')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise RulePackError(f'JSON 解析失败：{path}: {exc}') from exc
    if not isinstance(data, dict):
        raise RulePackError(f'JSON 顶层必须是对象：{path}')
    return data


def read_json_optional(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    return read_json(path)


def print_output(data: Any, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                print(f'{key}: {json.dumps(value, ensure_ascii=False)}')
            else:
                print(f'{key}: {value}')
    else:
        print(data)


def split_words(text: str) -> List[str]:
    raw = re.findall(r'[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_-]{1,80}', text or '')
    out: List[str] = []
    for item in raw:
        out.append(item)
        out.extend(x for x in re.split(r'[-_]+', item) if len(x) >= 2)
    return out


def filter_terms(values: Iterable[str]) -> List[str]:
    stop = {
        'android',
        'java',
        'kotlin',
        'string',
        'public',
        'private',
        'protected',
        'static',
        'final',
        'class',
        'return',
        'true',
        'false',
        'null',
        'and',
        'from',
        'with',
        'get',
        'set',
        'main',
        'next',
        'post',
        'callback',
        'buffer',
        'window',
    }
    out = []
    for value in values:
        text = str(value or '').strip()
        if not text:
            continue
        if contains_cjk(text) and len(text) > 8:
            continue
        if len(text) > 80:
            continue
        if text.lower() in stop:
            continue
        out.append(text)
    return out


def filter_identity_terms(values: Iterable[str]) -> List[str]:
    out = []
    for value in values:
        text = str(value or '').strip()
        if not text:
            continue
        if text.lower() in GENERIC_IDENTITY_TERMS:
            continue
        out.append(text)
    return out


def filter_app_signal_terms(values: Iterable[str]) -> List[str]:
    generic = {
        'activity',
        'service',
        'receiver',
        'provider',
        'worker',
        'database',
        'repository',
        'notification',
        'channel',
        'config',
        'setting',
        'preference',
        'implementation',
    }
    out = []
    for value in values:
        text = str(value or '').strip()
        if not text:
            continue
        if len(text) > 180:
            text = text[:180]
        if text.lower() in generic:
            continue
        out.append(text)
    return out


def filter_native_signal_terms(values: Iterable[str]) -> List[str]:
    generic = {
        'main',
        'android',
        'log',
        'private',
        'public',
        'static',
        'void',
        'int',
        'char',
        'const',
        'return',
        'target',
    }
    out = []
    for value in values:
        text = str(value or '').strip()
        if not text:
            continue
        text = text.strip('",;(){}[]')
        if not text or text.lower() in generic:
            continue
        if len(text) > 180:
            text = text[:180]
        out.append(text)
    return out


def split_camel_identifier(value: str) -> List[str]:
    text = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', str(value or ''))
    return [part for part in re.split(r'[^A-Za-z0-9]+', text) if len(part) >= 3]


def contains_cjk(text: str) -> bool:
    return re.search(r'[\u4e00-\u9fff]', text or '') is not None


def dedupe(values: Iterable[str], limit: int = 10_000) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        text = str(value or '').strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def dedupe_exact_logs(values: Iterable[Dict[str, str]], limit: int = 10_000) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen = set()
    for item in values:
        if not isinstance(item, dict):
            continue
        tag = str(item.get('tag') or '').strip()
        message = str(item.get('message') or '').strip()
        if not tag or not is_precise_log_message(message):
            continue
        key = (tag.lower(), message.lower(), str(item.get('logger') or '').lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                'tag': tag,
                'message': message,
                'logger': str(item.get('logger') or '').strip(),
                'path': str(item.get('path') or '').strip(),
            }
        )
        if len(out) >= limit:
            break
    return out


def dedupe_paths(paths: Iterable[Path]) -> List[Path]:
    out: List[Path] = []
    seen = set()
    for path in paths:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def slugify(value: str) -> str:
    text = re.sub(r'[^A-Za-z0-9_.-]+', '-', str(value or '').strip().lower()).strip('-')
    return text or 'rule'


def safe_filename(value: str) -> str:
    return slugify(value)


if __name__ == '__main__':
    raise SystemExit(main())
