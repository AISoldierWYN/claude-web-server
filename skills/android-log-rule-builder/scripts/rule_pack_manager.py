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
    '.gradle',
    '.xml',
    '.properties',
    '.aidl',
}
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

    return parser


def add_common_rule_pack_args(parser: argparse.ArgumentParser, require_pack: bool = True) -> None:
    parser.add_argument('--bundle-id', required=True, help='claude_web_paths.config.json 中的 bundle id')
    parser.add_argument(
        '--rule-pack-id',
        required=require_pack,
        default='',
        help='规则包 id；generate 默认使用 <bundle-id>-generated',
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

    scan = scan_android_project(
        project_dirs,
        bundle_keywords=bundle_keywords(bundle),
        max_files=args.max_files,
        max_bytes_per_file=args.max_bytes_per_file,
    )
    pack_id = args.rule_pack_id or f'{args.bundle_id}-generated'
    pack = build_generated_rule_pack(
        bundle_id=args.bundle_id,
        pack_id=pack_id,
        title=args.title or f'{bundle.get("title") or args.bundle_id} Generated Log Signals',
        description=args.description or f'Generated from {", ".join(str(p) for p in project_dirs)}',
        scan=scan,
    )
    validation = validate_pack(pack, config)
    if validation['errors']:
        raise RulePackError('生成的规则包未通过校验：' + '; '.join(validation['errors']))
    if not args.dry_run:
        path = write_rule_pack(Path(args.knowledge_dir), args.bundle_id, pack)
    else:
        path = None
    return {
        'ok': True,
        'path': str(path) if path else '',
        'rule_pack': pack,
        'scan': {
            'scanned_files': scan.scanned_files,
            'packages': scan.packages[:20],
            'tags': scan.tags[:40],
            'business_terms': scan.business_terms[:60],
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


def scan_android_project(
    project_dirs: Sequence[Path],
    bundle_keywords: Sequence[str],
    max_files: int,
    max_bytes_per_file: int,
) -> ScanResult:
    packages: List[str] = []
    tags: List[str] = []
    components: List[str] = []
    business_terms: List[str] = list(bundle_keywords)
    error_terms: List[str] = []
    log_messages: List[str] = []
    scanned = 0

    for path in iter_source_files(project_dirs):
        if scanned >= max_files:
            break
        text = read_text_prefix(path, max_bytes_per_file)
        if not text:
            continue
        scanned += 1
        rel_parts = [p for p in path.parts if p and p.lower() not in IGNORED_DIRS]
        business_terms.extend(extract_business_terms(' '.join(rel_parts)))
        packages.extend(extract_packages(text))
        tags.extend(filter_identity_terms(extract_tags(text, path)))
        components.extend(filter_identity_terms(extract_android_components(text)))
        messages = extract_log_messages(text)
        log_messages.extend(messages)
        business_terms.extend(extract_business_terms(' '.join(messages)))
        error_terms.extend(extract_error_terms(text))

    return ScanResult(
        packages=dedupe(packages, limit=80),
        tags=dedupe(tags, limit=100),
        components=dedupe(components, limit=120),
        business_terms=dedupe(filter_terms(business_terms), limit=160),
        error_terms=dedupe(filter_terms(error_terms), limit=80),
        log_messages=dedupe(log_messages, limit=120),
        scanned_files=scanned,
    )


def iter_source_files(project_dirs: Sequence[Path]) -> Iterable[Path]:
    for root in project_dirs:
        for current, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d.lower() not in IGNORED_DIRS]
            for name in files:
                path = Path(current) / name
                if source_suffix(path) in SOURCE_SUFFIXES:
                    yield path


def source_suffix(path: Path) -> str:
    lower = path.name.lower()
    if lower.endswith('.gradle.kts'):
        return '.gradle'
    return path.suffix.lower()


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
    out: List[str] = []
    out.extend(re.findall(r'\bTAG\b\s*[:=]\s*["\']([^"\']{2,80})["\']', text))
    out.extend(re.findall(r'\b(?:Log|Slog|Timber)\s*\.\s*(?:v|d|i|w|e|wtf)\s*\(\s*["\']([^"\']{2,80})["\']', text))
    out.extend(re.findall(r'\bclass\s+([A-Z][A-Za-z0-9_]{2,80})\b', text))
    if path.suffix.lower() in {'.java', '.kt'} and path.stem and re.match(r'[A-Za-z][A-Za-z0-9_]{2,80}$', path.stem):
        out.append(path.stem)
    return out


def extract_android_components(text: str) -> List[str]:
    names = re.findall(r'<(?:activity|service|receiver|provider)\b[^>]*android:name=["\']([^"\']+)["\']', text)
    return [n.split('.')[-1] for n in names if n]


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
    scan: ScanResult,
) -> Dict[str, Any]:
    prefix = slugify(pack_id)
    identity_keywords = dedupe(scan.packages + scan.tags + scan.components, limit=80)
    business_keywords = dedupe(scan.business_terms, limit=100)
    rules: List[Dict[str, Any]] = []
    if identity_keywords:
        rules.append(
            make_rule(
                f'{prefix}-identity-signals',
                f'{title} identity/log tag signals',
                'generic_log_error',
                'low',
                bundle_id,
                ['identity', 'tag', 'package'],
                keywords=identity_keywords,
                packages=scan.packages[:30],
            )
        )
    if business_keywords:
        rules.append(
            make_rule(
                f'{prefix}-business-flow',
                f'{title} business flow keywords',
                'android_business_spec',
                'medium',
                bundle_id,
                ['business', 'flow'],
                keywords=business_keywords,
                regex=business_regexes(business_keywords),
            )
        )
    if business_keywords:
        # 失败类规则只使用“业务词附近出现失败词”的正则，避免 RuntimeException/failed 这类泛词单独把无关 crash 归到项目规则。
        rules.append(
            make_rule(
                f'{prefix}-failure-signals',
                f'{title} failure/error signals',
                'android_business_spec',
                'high',
                bundle_id,
                ['failure', 'error'],
                keywords=[],
                regex=failure_regexes(business_keywords[:20]),
            )
        )
    if scan.components:
        rules.append(
            make_rule(
                f'{prefix}-android-components',
                f'{title} Android component signals',
                'android_framework_behavior',
                'medium',
                bundle_id,
                ['component', 'android'],
                keywords=scan.components[:80],
            )
        )

    return {
        'version': 1,
        'id': pack_id,
        'title': title,
        'description': description,
        'source_bundle_ids': [bundle_id],
        'rules': rules,
        'metadata': {
            'generator': 'skills/android-log-rule-builder',
            'scanned_files': scan.scanned_files,
            'packages': scan.packages[:20],
            'tags': scan.tags[:40],
        },
    }


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
        text = read_text_prefix(path, 500_000)
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
