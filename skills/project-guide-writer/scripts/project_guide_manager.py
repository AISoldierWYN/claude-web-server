#!/usr/bin/env python3
"""项目指南与项目专属日志分析 Skill 生成器。

脚本只依赖标准库，并复用 android-log-rule-builder 的轻量扫描能力。
生成内容的目标不是替代人工设计，而是把项目内真实存在的 package、
component、TAG、日志文本和代码入口沉淀成可检视的初稿。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[3]
RULE_BUILDER_SCRIPT_DIR = REPO_ROOT / 'skills' / 'android-log-rule-builder' / 'scripts'
sys.path.insert(0, str(RULE_BUILDER_SCRIPT_DIR))

from rule_pack_manager import (  # noqa: E402
    ScanResult,
    dedupe,
    default_generated_rule_pack_id,
    normalize_profiles,
    scan_android_project,
    slugify,
    tier2_scope_terms,
)


VALID_PROJECT_PRESETS = {'app', 'framework', 'native', 'library'}


class ProjectGuideError(Exception):
    """命令行可读的业务异常。"""


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
        if result is not None:
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                for key, value in result.items():
                    print(f'{key}: {value}')
        return 0
    except ProjectGuideError as exc:
        print(str(exc), file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='生成 CLAUDE.md / AGENTS.md / 项目专属日志分析 Skill')
    parser.add_argument('--json', action='store_true', help='以 JSON 输出结果')
    sub = parser.add_subparsers(dest='command', required=True)

    gen = sub.add_parser('generate', help='扫描项目并生成指南文件')
    gen.add_argument('--project-root', required=True, help='目标项目根目录')
    gen.add_argument('--project-id', default='', help='项目短 id，例如 rdm / nowinandroid')
    gen.add_argument('--title', default='', help='项目展示标题')
    gen.add_argument('--bundle-id', default='', help='claude_web_paths.config.json 中的 bundle id')
    gen.add_argument('--project-preset', default='app', help='app/framework/native/library')
    gen.add_argument('--profile', action='append', default=[], help='functional/stability/xts/memory/performance，可重复')
    gen.add_argument('--keyword', action='append', default=[], help='项目已知关键词，仅进入 Deep hints，不直接生成前置模糊规则')
    gen.add_argument('--max-files', type=int, default=1200, help='最大扫描源码文件数')
    gen.add_argument('--max-bytes-per-file', type=int, default=256_000, help='单文件最多读取字节')
    gen.add_argument('--skip-claude', action='store_true', help='不写 CLAUDE.md')
    gen.add_argument('--skip-agents', action='store_true', help='不写 AGENTS.md')
    gen.add_argument('--skip-log-skill', action='store_true', help='不写项目专属日志分析 Skill')
    gen.add_argument('--dry-run', action='store_true', help='只输出预览，不写入文件')
    gen.set_defaults(func=cmd_generate)
    return parser


def cmd_generate(args: argparse.Namespace) -> Dict[str, Any]:
    root = Path(args.project_root).resolve()
    if not root.is_dir():
        raise ProjectGuideError(f'项目目录不存在：{root}')
    project_id = slugify(args.project_id or root.name)
    title = args.title or root.name
    bundle_id = args.bundle_id or project_id
    project_preset = normalize_project_preset(args.project_preset)
    profiles = normalize_profiles(args.profile) or ['functional', 'stability']

    scan = scan_android_project(
        [root],
        bundle_keywords=args.keyword or [title, project_id],
        project_preset=project_preset,
        max_files=args.max_files,
        max_bytes_per_file=args.max_bytes_per_file,
    )
    scope_terms = tier2_scope_terms(scan, project_preset, limit=140)
    rule_pack_id = default_generated_rule_pack_id(bundle_id)
    log_skill_rel = f'skills/{project_id}-log-analysis/SKILL.md'
    outputs: Dict[str, str] = {}

    context = {
        'root': root,
        'project_id': project_id,
        'title': title,
        'bundle_id': bundle_id,
        'rule_pack_id': rule_pack_id,
        'project_preset': project_preset,
        'profiles': profiles,
        'scan': scan,
        'scope_terms': scope_terms,
        'log_skill_rel': log_skill_rel,
    }
    if not args.skip_claude:
        outputs['CLAUDE.md'] = render_claude_md(context)
    if not args.skip_agents:
        outputs['AGENTS.md'] = render_agents_md(context)
    if not args.skip_log_skill:
        outputs[log_skill_rel] = render_project_log_skill(context)

    written: List[str] = []
    if not args.dry_run:
        for rel, content in outputs.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
            written.append(str(path))

    return {
        'ok': True,
        'project_root': str(root),
        'project_id': project_id,
        'title': title,
        'bundle_id': bundle_id,
        'rule_pack_id': rule_pack_id,
        'project_preset': project_preset,
        'profiles': profiles,
        'scanned_files': scan.scanned_files,
        'exact_log_count': len(scan.exact_logs),
        'scope_term_count': len(scope_terms),
        'outputs': list(outputs.keys()),
        'written': written,
    }


def render_claude_md(ctx: Dict[str, Any]) -> str:
    scan: ScanResult = ctx['scan']
    title = ctx['title']
    log_skill_rel = ctx['log_skill_rel']
    lines = [
        f'# {title} Claude Guide',
        '',
        '## 项目定位',
        f'- 项目根目录：`{ctx["root"]}`',
        f'- claude_web bundle：`{ctx["bundle_id"]}`，生成规则包：`{ctx["rule_pack_id"]}`',
        f'- 项目类型：`{ctx["project_preset"]}`；已配置问题类型：`{", ".join(ctx["profiles"])}`',
        '',
        '## 优先阅读',
        bullet_list(scan.preferred_paths[:16], fallback='暂无稳定优先路径，先看根目录 README、Manifest、Gradle 与主模块源码。'),
        '',
        '## 关键模块与范围信号',
        '- 包名：' + inline_list(scan.packages[:12]),
        '- 组件 / action / authorities：' + inline_list(scan.components[:24]),
        '- 2 类范围信号：' + inline_list(ctx['scope_terms'][:36]),
        '',
        '## Android / 代码入口与精确日志入口',
        '### 代码入口',
        bullet_list(scan.preferred_paths[:20], fallback='暂无自动识别入口，先从 Manifest、Gradle module 和包名对应源码目录读取。'),
        '',
        '### 精确日志入口',
        render_exact_logs(scan.exact_logs[:40]),
        '',
        '## Deep 分析约束',
        '- 先使用项目专属日志分析 Skill：`' + log_skill_rel + '`。',
        '- 再使用规则包中的 1 类 exact logs：真实 `TAG + message`。',
        '- 1 类没有命中时，扩展到 2 类范围信号：真实 TAG、package、component、action、permission、native symbol。',
        '- 仍不够时，读取本文件、项目专属 Skill、规则包 `deep_hints` 和白名单源码路径。',
        '- 最后才允许把真实项目 TAG/package/component 与用户描述语义词组合兜底；不要直接 grep 模糊词。',
        '',
        '## 常用命令',
        render_command_hints(ctx),
        '',
        '## 修改注意事项',
        '- 不要提交本地密钥、token、账号、构建产物或用户私有日志。',
        '- 如果新增日志排查经验，优先补充项目专属 Skill 和规则包，而不是只写在聊天记录里。',
        '- 前置工作流规则只放 1/2 类信号；业务语义、类名和代码阅读建议放在 Deep 指南里。',
        '',
        '## 需要后续补充',
        '- 按真实问题继续补充功能到代码入口、精确日志入口、案例与置信度说明。',
    ]
    return '\n'.join(lines).rstrip() + '\n'


def render_agents_md(ctx: Dict[str, Any]) -> str:
    scan: ScanResult = ctx['scan']
    title = ctx['title']
    lines = [
        f'# {title} Agent Guide',
        '',
        '## 项目概览',
        f'- bundle：`{ctx["bundle_id"]}`',
        f'- project preset：`{ctx["project_preset"]}`',
        f'- rule pack：`{ctx["rule_pack_id"]}`',
        '',
        '## 目录结构',
        bullet_list(scan.source_paths[:24], fallback='暂无足够目录样本。'),
        '',
        '## 构建与测试',
        render_command_hints(ctx),
        '',
        '## 问题排查',
        '- 优先遵守 `CLAUDE.md` 的 1/2/3/4 类日志搜索顺序。',
        f'- 项目专属日志分析 Skill：`{ctx["log_skill_rel"]}`。',
        '- 不要从用户语义词直接开始全局 grep；必须先定位真实 TAG、包名、组件或代码入口。',
        '',
        '## 安全与边界',
        '- 只修改当前项目相关文件；不要清理用户日志、缓存或未确认的本地改动。',
        '- 不写入密钥、账号、token、设备私有标识。',
    ]
    return '\n'.join(lines).rstrip() + '\n'


def render_project_log_skill(ctx: Dict[str, Any]) -> str:
    scan: ScanResult = ctx['scan']
    skill_name = f'{ctx["project_id"]}-log-analysis'
    title = ctx['title']
    lines = [
        '---',
        f'name: {skill_name}',
        f'description: {title} 项目专属 Android 日志分析 Skill。用于 Deep 分析本项目日志、源码和规则包时，按 1 类精确日志、2 类范围信号、3 类代码/CLAUDE 指南、4 类受限语义兜底逐层扩大搜索，避免直接使用模糊关键词全局 grep。',
        '---',
        '',
        f'# {title} Log Analysis',
        '',
        '## 使用边界',
        '- 只在用户问题命中本项目或本项目 bundle 时使用。',
        '- 这是 Deep 分析 Skill，不替代前置工作流规则包。',
        '- 禁止第一步直接 grep 用户语义词；必须按下面顺序逐层扩大。',
        '',
        '## 1 类：精确日志入口',
        render_exact_logs(scan.exact_logs[:80]),
        '',
        '## 2 类：范围扩展信号',
        '- TAG / package / component / action / permission / native symbol：',
        bullet_list(ctx['scope_terms'][:80], fallback='暂无足够范围信号，先读 CLAUDE.md 和 Manifest/Gradle/源码入口。'),
        '',
        '## 3 类：代码与资料入口',
        bullet_list(scan.preferred_paths[:30], fallback='暂无自动识别入口，先读 CLAUDE.md、README、Manifest、Gradle 和主包源码。'),
        '',
        '## 搜索流程',
        '1. 根据用户描述判断功能区域，先从 1 类 `TAG + message` 精确日志搜索。',
        '2. 如果 1 类没有命中，扩展到同功能的 2 类 TAG、包名、组件、action、permission、native symbol。',
        '3. 如果仍不清楚，读取 `CLAUDE.md`、本 Skill、规则包 `deep_hints` 和对应源码入口，找到真实日志调用后回到第 1 层。',
        '4. 只有前三层失败时，才允许把真实项目 TAG/package/component 与用户语义词组合搜索，并记录为什么放宽范围。',
        '5. 每次放宽都要记录：搜了哪些词、命中了哪些文件、结论置信度是否提升。',
        '',
        '## 不要这样做',
        '- 不要直接搜索 `lock`、`unlock`、`sync`、`fail`、`error`、`policy` 等泛词，除非它们与真实 TAG/组件共同限定。',
        '- 不要把代码类名当作日志 TAG，除非源码确实以该类名作为 TAG 输出。',
        '- 不要把无关应用、系统进程或第三方 crash 直接归因到本项目。',
    ]
    return '\n'.join(lines).rstrip() + '\n'


def render_exact_logs(exact_logs: Sequence[Dict[str, str]]) -> str:
    if not exact_logs:
        return '- 暂无可确认的精确日志模式；先读取代码入口或规则包，再从真实日志调用中补充。'
    lines: List[str] = []
    for item in exact_logs:
        tag = item.get('tag') or '<no-tag>'
        message = item.get('message') or ''
        path = item.get('path') or ''
        lines.append(f'- `{tag}` / `{message}`' + (f'（{path}）' if path else ''))
    return '\n'.join(lines)


def render_command_hints(ctx: Dict[str, Any]) -> str:
    root: Path = ctx['root']
    has_gradlew = (root / 'gradlew').is_file() or (root / 'gradlew.bat').is_file()
    if has_gradlew:
        return '- Windows：`gradlew.bat tasks`、`gradlew.bat test`\n- Linux/macOS：`./gradlew tasks`、`./gradlew test`'
    return '- 暂未识别稳定构建命令；先查看 README、Gradle/Maven/脚本文件。'


def bullet_list(values: Iterable[str], fallback: str = '暂无') -> str:
    items = [str(v).strip() for v in values if str(v).strip()]
    if not items:
        return f'- {fallback}'
    return '\n'.join(f'- `{item}`' for item in items)


def inline_list(values: Iterable[str], fallback: str = '暂无') -> str:
    items = [str(v).strip() for v in values if str(v).strip()]
    if not items:
        return fallback
    return ', '.join(f'`{item}`' for item in items)


def normalize_project_preset(value: str) -> str:
    text = str(value or '').strip().lower()
    return text if text in VALID_PROJECT_PRESETS else 'app'


if __name__ == '__main__':
    raise SystemExit(main())
