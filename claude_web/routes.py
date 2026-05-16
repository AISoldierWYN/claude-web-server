"""HTTP 路由。"""

import json
import logging
import re
import threading
import time
from pathlib import Path

from flask import Response, jsonify, request, send_from_directory, stream_with_context
from . import config
from .auth import optional_token
from .android_analysis.archive import safe_extract_archive
from .android_analysis.casebook import confirm_case_draft, generate_case_draft, recall_case_cards, write_case_cards
from .android_analysis.classifier import run_question_classifier
from .android_analysis.debug_trace import AndroidAnalysisDebugTracer
from .android_analysis.deep_analysis import build_deep_evidence_pack, generate_deep_report
from .android_analysis.evidence import generate_first_evidence_pack
from .android_analysis.evidence_selector import run_evidence_template_selection
from .android_analysis.evidence_template_pipeline import run_evidence_template_generation_pipeline
from .android_analysis.expert_knowledge import build_expert_knowledge_cache, summarize_expert_knowledge_cache
from .android_analysis.expert_knowledge_builder import (
    convert_evidence_templates,
    convert_xml_state_templates,
    create_project_knowledge_scaffold,
)
from .android_analysis.jobs import AndroidAnalysisJobStore
from .android_analysis.knowledge_store import list_bundles as list_android_analysis_bundles
from .android_analysis.models import AndroidAnalysisError, PlannerPromptLimits
from .android_analysis.parameter_resolver import run_parameter_resolution
from .android_analysis.planner import run_planner
from .android_analysis.profiler import profile_extracted_tree
from .android_analysis.reporter import generate_first_report
from .android_analysis.rule_engine import run_rule_matching
from .android_analysis.sampler import sample_files
from .android_analysis.verifier import run_verifier
from .android_analysis.xml_state_template_pipeline import run_xml_state_template_batch_generation_pipeline
from .android_analysis.xml_state_matcher import run_xml_state_matching
from .backup_service import backup_session_before_delete
from . import orchestrator
from .claude_runner import CLAUDE_CLI_PATH, resolve_session_upload_paths, stop_session_process, stream_claude_output
from .dev_projects import (
    DevProjectError,
    clear_dev_session,
    diff_for_project,
    find_project,
    git_status,
    load_dev_session,
    load_projects,
    project_public_info,
    run_project_test,
    save_dev_session,
)
from .feedback_service import save_feedback_package
from .filename_sanitize import ascii_storage_filename, is_ascii_filename, safe_client_filename
from .gemini_runner import stop_gemini_session_process, stream_gemini_output
from .paths import get_client_ip
from .session_manager import (
    USER_GLOBAL_MEMORY_FILENAME,
    SessionManager,
    SUPPORTED_PROVIDERS,
    normalize_provider,
)
from .tavily_search import TavilySearchError, format_tavily_for_prompt, search_tavily
from .user_claude_credentials import (
    delete_credentials,
    load_credentials,
    merge_env_preserve_existing,
    public_status,
    resolve_claude_runtime_for_request,
    sanitize_env,
    save_credentials,
    validate_save_payload,
)

log = logging.getLogger('claude-web')

READONLY_DIRS_KEY = 'READONLY_DIRS'
PATHS_NOTES_KEY = 'CLAUDE_WEB_PATHS_NOTES'
PATHS_BUNDLES_KEY = 'CLAUDE_WEB_PATHS_BUNDLES'
ANDROID_EXPERT_KNOWLEDGE_KEY = 'ANDROID_EXPERT_KNOWLEDGE'


def register_routes(app, sm: SessionManager):
    static_root = config.ROOT / 'static'

    @app.route('/')
    def index():
        resp = send_from_directory(static_root, 'index.html')
        resp.headers['Cache-Control'] = 'no-store, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        return resp

    def _readonly():
        return app.config.get(READONLY_DIRS_KEY, [])

    def _readonly_notes():
        return (app.config.get(PATHS_NOTES_KEY) or '').strip()

    def _readonly_bundles():
        return app.config.get(PATHS_BUNDLES_KEY) or []

    def _bundle_terms(bundle: dict):
        raw = [
            bundle.get('id') or '',
            bundle.get('title') or bundle.get('name') or '',
            bundle.get('summary') or bundle.get('description') or '',
        ]
        raw.extend(str(x) for x in (bundle.get('keywords') or []) if x)
        for skill in bundle.get('skills') or []:
            raw.extend(
                [
                    skill.get('id') or '',
                    skill.get('title') or skill.get('name') or '',
                    skill.get('summary') or skill.get('description') or '',
                ]
            )
            raw.extend(str(x) for x in (skill.get('keywords') or []) if x)
        for res in bundle.get('resources') or []:
            raw.extend(
                [
                    res.get('id') or '',
                    res.get('kind') or '',
                    res.get('summary') or res.get('description') or '',
                ]
            )
            raw.extend(str(x) for x in (res.get('keywords') or []) if x)
        terms = []
        for s in raw:
            s = str(s).strip().lower()
            if not s:
                continue
            if len(s) <= 80:
                terms.append(s)
            for part in re.findall(r'[a-z0-9_./+-]{3,}|[\u4e00-\u9fff]{2,}', s):
                if part not in terms:
                    terms.append(part)
                if re.fullmatch(r'[\u4e00-\u9fff]{2,}', part):
                    for n in (2, 3, 4):
                        for i in range(0, max(0, len(part) - n + 1)):
                            sub = part[i:i + n]
                            if sub not in terms:
                                terms.append(sub)
        return terms

    def _skill_terms(skill: dict):
        raw = [
            skill.get('id') or '',
            skill.get('title') or skill.get('name') or '',
            skill.get('summary') or skill.get('description') or '',
        ]
        raw.extend(str(x) for x in (skill.get('keywords') or []) if x)
        terms = []
        for s in raw:
            s = str(s).strip().lower()
            if not s:
                continue
            if len(s) <= 80:
                terms.append(s)
            for part in re.findall(r'[a-z0-9_./+-]{3,}|[\u4e00-\u9fff]{2,}', s):
                if part not in terms:
                    terms.append(part)
                if re.fullmatch(r'[\u4e00-\u9fff]{2,}', part):
                    for n in (2, 3, 4):
                        for i in range(0, max(0, len(part) - n + 1)):
                            sub = part[i:i + n]
                            if sub not in terms:
                                terms.append(sub)
        return terms

    def _select_skills_for_bundle(bundle: dict, text: str, *, allow_single_fallback: bool = True):
        skills = bundle.get('skills') or []
        selected = []
        for skill in skills:
            reasons = []
            for term in _skill_terms(skill):
                if term and term in text:
                    reasons.append(f'keyword: {term[:40]}')
                    break
            if reasons:
                ss = dict(skill)
                ss['selected'] = True
                ss['match_reason'] = ', '.join(reasons)
                selected.append(ss)
            if len(selected) >= 3:
                break
        if not selected and allow_single_fallback and len(skills) == 1:
            ss = dict(skills[0])
            ss['selected'] = True
            ss['match_reason'] = 'bundle matched; only skill'
            selected.append(ss)
        return selected

    def _recent_history_text(messages: list, limit: int = 4000) -> str:
        chunks = []
        total = 0
        for m in reversed(messages or []):
            if not isinstance(m, dict):
                continue
            c = str(m.get('content') or '').strip()
            if not c:
                continue
            chunks.append(c[:800])
            total += min(len(c), 800)
            if total >= limit:
                break
        return '\n'.join(reversed(chunks))

    def _select_skill_bundles(message: str, prior_messages=None, bundle_ids=None):
        bundles = _readonly_bundles()
        wanted_ids = {str(x) for x in (bundle_ids or []) if x}
        text = f'{message or ""}\n{_recent_history_text(prior_messages or [])}'.lower()
        selected = []
        rendered = []
        for b in bundles:
            bid = str(b.get('id') or '')
            mounted = False
            reason = ''
            skill_matches = _select_skills_for_bundle(b, text, allow_single_fallback=False)
            if b.get('always_mount'):
                mounted = True
                reason = 'always_mount'
            elif wanted_ids and bid in wanted_ids:
                mounted = True
                reason = 'continuation'
            elif skill_matches:
                mounted = True
                reason = 'skill: ' + ', '.join(str(s.get('id') or '?') for s in skill_matches[:3])
            else:
                for term in _bundle_terms(b):
                    if term and term in text:
                        mounted = True
                        reason = f'keyword: {term[:40]}'
                        break
            bb = dict(b)
            bb['mounted'] = mounted
            bb['mount_reason'] = reason
            bb['selected_skills'] = skill_matches or (_select_skills_for_bundle(bb, text) if mounted else [])
            rendered.append(bb)
            if mounted:
                skill_ids = [s.get('id') for s in bb.get('selected_skills') or [] if s.get('id')]
                if skill_ids:
                    log.info('[SkillBundle] mounted=%s reason=%s selected_skills=%s', bid, reason, skill_ids)
                selected.append(bb)
        return rendered, selected

    def _bundle_paths(selected_bundles):
        out = []
        seen = set()
        for b in selected_bundles or []:
            for p in b.get('paths') or []:
                if p and p not in seen:
                    seen.add(p)
                    out.append(p)
        return out

    def _v2_orch_kwargs(rt: dict) -> dict:
        return {
            'child_env_extra': rt.get('child_env_extra'),
            'model_override': rt.get('model_override'),
        }

    def _set_memory_line(existing: str, key: str, value: str) -> str:
        lines = [ln for ln in (existing or '').splitlines() if not ln.strip().startswith(f'- {key}：')]
        while lines and not lines[-1].strip():
            lines.pop()
        lines.extend(['', f'- {key}：{value}'])
        return '\n'.join(lines).rstrip() + '\n'

    def _apply_explicit_user_memory(message: str, session_dir: Path) -> bool:
        text = (message or '').strip()
        if not text or not session_dir:
            return False
        updates = []
        name_match = re.search(r'(?:我叫|我的名字是)\s*([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z·._ -]{0,30})', text)
        if name_match:
            name = re.split(r'[，,。.!！?？；;\s]', name_match.group(1).strip(), maxsplit=1)[0].strip()
            if 1 <= len(name) <= 30:
                updates.append(('用户姓名', name))
        call_match = re.search(r'(?:以后|今后|之后)?(?:都)?(?:请)?(?:记得)?(?:叫我|称呼我|喊我)\s*([^\s，,。.!！?？；;]{1,20})', text)
        if call_match:
            call = call_match.group(1).strip()
            if 1 <= len(call) <= 20:
                updates.append(('偏好称呼', call))
        if not updates:
            return False
        try:
            session_resolved = session_dir.resolve()
            memory_path = (session_resolved / USER_GLOBAL_MEMORY_FILENAME).resolve()
            if memory_path.parent != session_resolved:
                return False
            existing = memory_path.read_text(encoding='utf-8') if memory_path.exists() else ''
            new_text = existing
            for key, value in updates:
                new_text = _set_memory_line(new_text, key, value)
            if new_text != existing:
                memory_path.write_text(new_text, encoding='utf-8')
                log.info('[Memory] 已根据用户显式偏好更新 %s: %s', memory_path, ', '.join(f'{k}={v}' for k, v in updates))
                return True
        except Exception as e:
            log.warning('[Memory] 根据用户显式偏好更新 AGENT.md 失败: %s', e)
        return False

    def _provider_runner(provider: str):
        return stream_gemini_output if provider == 'gemini' else None

    def _provider_label(provider: str) -> str:
        return 'Gemini' if provider == 'gemini' else 'Claude'

    def _dev_enabled():
        return bool(getattr(config, 'FEATURE_MOBILE_REMOTE_DEVELOPMENT', False))

    def _dev_projects():
        return load_projects(config.DEV_PROJECTS_CONFIG_FILE)

    def _dev_disabled_response():
        return jsonify({'error': 'mobile_remote_development disabled', 'code': 'dev_disabled'}), 404

    def _android_analysis_enabled():
        return bool(getattr(config, 'FEATURE_ANDROID_ISSUE_ANALYSIS', False))

    def _android_analysis_disabled_response():
        return jsonify({'error': 'android_issue_analysis disabled', 'code': 'android_analysis_disabled'}), 404

    def _android_expert_workbench_enabled():
        return bool(getattr(config, 'FEATURE_ANDROID_ISSUE_ANALYSIS_EXPERT_WORKBENCH', False))

    def _android_expert_workbench_disabled_response():
        return jsonify({'error': 'android expert workbench disabled', 'code': 'android_expert_workbench_disabled'}), 404

    def _refresh_android_expert_knowledge_cache():
        cache = build_expert_knowledge_cache(
            _readonly_bundles(),
            config.ANDROID_ANALYSIS_KNOWLEDGE_DIR,
            config.ANDROID_ANALYSIS_PROJECT_KNOWLEDGE_RELATIVE_PATH,
            log,
        )
        app.config[ANDROID_EXPERT_KNOWLEDGE_KEY] = cache
        return cache

    def _resolve_expert_project(data: dict):
        bundle_id = str(data.get('bundle_id') or '').strip()
        project_path_raw = str(data.get('project_path') or '').strip()
        bundles = _readonly_bundles()
        bundle = next((b for b in bundles if str(b.get('id') or '') == bundle_id), None)
        if not bundle:
            return None, None, jsonify({'error': 'Configured bundle not found.', 'code': 'bundle_not_found'}), 404
        roots = []
        for raw in bundle.get('paths') or []:
            try:
                root = Path(str(raw)).expanduser().resolve()
            except OSError:
                continue
            if root.is_dir():
                roots.append(root)
        if not roots:
            return bundle, None, jsonify({'error': 'Bundle has no writable configured project path.', 'code': 'bundle_has_no_paths'}), 400
        if project_path_raw:
            try:
                requested = Path(project_path_raw).expanduser().resolve()
            except OSError:
                return bundle, None, jsonify({'error': 'Invalid project path.', 'code': 'invalid_project_path'}), 400
            matched = next((root for root in roots if str(root).lower() == str(requested).lower()), None)
            if not matched:
                return bundle, None, jsonify({'error': 'Project path must be one configured path of the selected bundle.', 'code': 'project_path_not_allowed'}), 403
            return bundle, matched, None, 200
        if len(roots) != 1:
            return bundle, None, jsonify({'error': 'Bundle has multiple paths; project_path is required.', 'code': 'project_path_required'}), 400
        return bundle, roots[0], None, 200

    def _merge_android_artifacts(job: dict, updates: dict) -> dict:
        artifacts = dict(job.get('artifacts') or {})
        artifacts.update(updates)
        return artifacts

    def _android_store_for_request(user_id: str, session_id: str):
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        _, err = _session_for_user(client_ip, user_id, session_id)
        if err:
            return None, None, err
        session_dir = sm.get_session_dir(client_ip, user_id, session_id)
        return AndroidAnalysisJobStore(session_dir), session_dir, None

    def _android_artifact_sizes(artifacts_dir: Path) -> dict:
        out = {}
        if not artifacts_dir.is_dir():
            return out
        for path in sorted(artifacts_dir.iterdir()):
            if path.is_file():
                try:
                    out[path.name] = path.stat().st_size
                except OSError:
                    out[path.name] = 0
        return out

    def _android_kind_counts(files: list[dict]) -> dict:
        counts = {}
        for item in files or []:
            kind = str(item.get('kind') or 'unknown')
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    def _android_planner_prompt_limits() -> PlannerPromptLimits:
        return PlannerPromptLimits(
            prompt_budget_chars=config.ANDROID_ANALYSIS_PLANNER_PROMPT_BUDGET_CHARS,
            max_tree_nodes=config.ANDROID_ANALYSIS_PLANNER_MAX_TREE_NODES,
            max_sample_files=config.ANDROID_ANALYSIS_PLANNER_MAX_SAMPLE_FILES,
            max_sample_chars=config.ANDROID_ANALYSIS_PLANNER_MAX_SAMPLE_CHARS,
        )

    def _android_prompt_char_metrics(artifacts_dir: Path) -> dict:
        def read_len(name: str) -> int:
            path = artifacts_dir / name
            try:
                return len(path.read_text(encoding='utf-8')) if path.is_file() else 0
            except OSError:
                return 0

        return {
            'classification_input_chars_estimate': read_len('classification_prompt.md'),
            'selected_evidence_templates_chars_estimate': read_len('selected_evidence_templates.json'),
            'planner_input_chars_estimate': read_len('file_manifest.json') + read_len('file_tree.json') + read_len('file_samples.json'),
            'first_report_input_chars_estimate': read_len('first_evidence_pack.md') + read_len('matched_rules.json') + read_len('planner_result.json') + read_len('case_cards.json'),
            'deep_report_input_chars_estimate': read_len('deep_evidence_pack.md') + read_len('matched_rules.json') + read_len('planner_result.json') + read_len('final_report.md'),
            'verifier_input_chars_estimate': read_len('deep_evidence_pack.md') + read_len('first_evidence_pack.md') + read_len('matched_rules.json') + read_len('deep_report.md'),
        }

    def _android_ai_token_metrics(artifacts_dir: Path) -> dict:
        # token 统计来源于 debug trace 的 ai_token_usage 事件；Claude CLI 有 usage 时记录真实值，
        # 没有 usage 时记录估算值，并通过 token_source 标明口径。
        path = artifacts_dir / 'android_debug_trace.jsonl'
        interactions = []
        totals = {
            'input_tokens': 0,
            'output_tokens': 0,
            'total_tokens': 0,
            'input_chars': 0,
            'output_chars': 0,
        }
        if not path.is_file():
            return {'interactions': interactions, 'totals': totals}
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except OSError:
            return {'interactions': interactions, 'totals': totals}
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get('event') != 'ai_token_usage' or not isinstance(item.get('data'), dict):
                continue
            data = item['data']
            entry = {
                'stage': item.get('stage'),
                'interaction': data.get('interaction'),
                'token_source': data.get('token_source'),
                'input_tokens': int(data.get('input_tokens') or 0),
                'output_tokens': int(data.get('output_tokens') or 0),
                'total_tokens': int(data.get('total_tokens') or 0),
                'input_chars': int(data.get('input_chars') or 0),
                'output_chars': int(data.get('output_chars') or 0),
            }
            interactions.append(entry)
            for key in totals:
                totals[key] += entry.get(key, 0)
        return {'interactions': interactions, 'totals': totals}

    def _android_planner_metrics(artifacts_dir: Path, ai_token_usage: dict) -> dict:
        prompt_metrics = _read_android_json_artifact(artifacts_dir, 'planner_prompt_metrics.json', {})
        planner_usage = {}
        for item in ai_token_usage.get('interactions') or []:
            if item.get('interaction') == 'planner':
                planner_usage = item
                break
        return {
            'planner_prompt_chars': int(prompt_metrics.get('prompt_chars') or 0),
            'planner_input_tokens': int(planner_usage.get('input_tokens') or 0),
            'planner_output_tokens': int(planner_usage.get('output_tokens') or 0),
            'planner_token_source': planner_usage.get('token_source') or '',
            'planner_component_chars': prompt_metrics.get('component_chars') or {},
            'planner_clipping': prompt_metrics.get('clipping') or {},
        }

    def _write_android_metrics(store: AndroidAnalysisJobStore, job_id: str, timings: list[dict]) -> dict:
        # 指标文件用于后续成本/性能回归分析，不参与模型判断；写入 artifacts 便于前端下载和测试断言。
        ai_token_usage = _android_ai_token_metrics(store.artifacts_dir(job_id))
        metrics = {
            'version': 1,
            'stage_timings': timings,
            'artifact_sizes': _android_artifact_sizes(store.artifacts_dir(job_id)),
            'prompt_chars': _android_prompt_char_metrics(store.artifacts_dir(job_id)),
            'ai_token_usage': ai_token_usage,
            'planner': _android_planner_metrics(store.artifacts_dir(job_id), ai_token_usage),
        }
        metrics_path = store.artifacts_dir(job_id) / 'analysis_metrics.json'
        with open(metrics_path, 'w', encoding='utf-8') as f:
            json.dump(metrics, f, ensure_ascii=False, indent=2)
        store.append_event(
            job_id,
            'analysis_metrics_recorded',
            {
                'stage_count': len(timings),
                'artifact_count': len(metrics['artifact_sizes']),
                'prompt_chars': metrics['prompt_chars'],
                'ai_token_usage': metrics['ai_token_usage']['totals'],
                'planner': metrics['planner'],
            },
        )
        return metrics

    def _timed_android_stage(store: AndroidAnalysisJobStore, job_id: str, timings: list[dict], stage: str, fn):
        # 每个阶段统一记录耗时并推送事件，前端 SSE 面板和 analysis_metrics.json 共用这份数据。
        started = time.perf_counter()
        result = fn()
        duration = round(time.perf_counter() - started, 3)
        item = {'stage': stage, 'duration_seconds': duration}
        timings.append(item)
        store.append_event(job_id, 'stage_timing', item)
        return result

    def _android_progress_markdown(events: list[dict]) -> str:
        lines = ['Android 分析过程']
        ai_stream_counts = {
            'ai_thinking_delta': 0,
            'ai_text_delta': 0,
            'ai_tool_event': 0,
        }
        ai_stream_labels = {
            'ai_thinking_delta': 'AI 可见思考流',
            'ai_text_delta': 'AI 输出流',
            'ai_tool_event': 'AI 工具流',
        }
        for event in events:
            et = event.get('type')
            data = event.get('data') or {}
            if et in ai_stream_counts:
                ai_stream_counts[et] += 1
                continue
            if et == 'job_updated':
                lines.append(f"- 阶段：{data.get('status')}")
            elif et == 'stage_timing':
                lines.append(f"- {data.get('stage')}：{data.get('duration_seconds')}s")
            elif et and et != 'job_initialized':
                lines.append(f"- {et}")
        for et, count in ai_stream_counts.items():
            if count:
                lines.append(f"- {ai_stream_labels[et]}：{count} 个片段（详见 Android 分析过程详情）")
        return '\n'.join(lines)

    def _android_trace_event_sink(store: AndroidAnalysisJobStore, job_id: str):
        def sink(event_type: str, data: dict) -> None:
            slim = dict(data or {})
            content = slim.get('content')
            if isinstance(content, str) and len(content) > 800:
                slim['content'] = content[:800] + f'\n...<truncated {len(content) - 800} chars>'
            store.append_event(job_id, event_type, slim)

        return sink

    def _read_android_json_artifact(artifacts_dir: Path, name: str, default):
        path = artifacts_dir / name
        if not path.is_file():
            return default
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return default
        return data

    def _read_android_debug_trace(artifacts_dir: Path) -> list[dict]:
        path = artifacts_dir / 'android_debug_trace.jsonl'
        if not path.is_file():
            return []
        records = []
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except OSError:
            return []
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
        return records

    def _android_bundle_titles_for_job(store: AndroidAnalysisJobStore, job_id: str, artifacts_dir: Path) -> list[str]:
        bundle_ids = []
        try:
            job = store.load_job(job_id)
            bundle_ids.extend(str(bid) for bid in (job.get('bundle_ids') or []) if bid)
        except Exception:
            job = {}
        planner_result = _read_android_json_artifact(artifacts_dir, 'planner_result.json', {})
        bundle_ids.extend(str(bid) for bid in (planner_result.get('candidate_bundle_ids') or []) if bid)
        seen_ids = []
        for bid in bundle_ids:
            if bid not in seen_ids:
                seen_ids.append(bid)

        bundles = list_android_analysis_bundles(config.ANDROID_ANALYSIS_KNOWLEDGE_DIR)
        title_by_id = {
            str(b.get('id')): str(b.get('title') or b.get('name') or b.get('id')).strip()
            for b in bundles
            if b.get('id')
        }
        titles = []
        for bid in seen_ids:
            title = title_by_id.get(bid) or bid
            if title and title not in titles:
                titles.append(title)
        return titles[:3]

    def _android_stage_titles() -> dict:
        return {
            'queued': '排队',
            'classifying_question': '问题分类',
            'resolving_parameters': '参数解析',
            'selecting_evidence_templates': '证据模板选择',
            'extracting': '解压日志包',
            'profiling': '扫描文件树',
            'sampling': '初始采样日志',
            'planning': 'Planner 路由',
            'sampling_refined': 'Planner 定向采样',
            'matching_rules': '规则匹配',
            'building_evidence': '构建 Evidence Pack',
            'recalling_cases': '案例召回',
            'generating_report': '生成首轮报告',
            'verifying_first_report': '首轮 Verifier 校验',
            'first_pass_decision': '首轮置信度决策',
            'deep_scoping': 'Deep 证据扩展',
            'deep_reporting': '生成 Deep 报告',
            'verifying': 'Deep Verifier 校验',
            'metrics': '性能与 Token 指标',
            'casebook': '案例草稿与入库',
            'job': '任务总览',
        }

    def _android_stage_for_trace(record: dict) -> str:
        stage = str(record.get('stage') or '')
        event = str(record.get('event') or '')
        data = record.get('data') if isinstance(record.get('data'), dict) else {}
        if event in {'pipeline_started', 'pipeline_finished', 'pipeline_failed', 'deep_pipeline_started', 'deep_pipeline_finished'}:
            return 'job'
        if event in {'first_pass_confidence_decision', 'pipeline_auto_deep_handoff'}:
            return 'first_pass_decision'
        if event == 'ai_token_usage':
            return 'metrics'
        if event in {'case_recall_result', 'case_draft_result', 'case_confirmed'}:
            return 'recalling_cases' if event == 'case_recall_result' else 'casebook'
        if stage == 'sampling' and data.get('phase') == 'planner_refined':
            return 'sampling_refined'
        if stage == 'verifying':
            report_name = str(data.get('report_name') or '')
            if report_name == 'final_report.md':
                return 'verifying_first_report'
        return stage or 'job'

    def _android_trace_title(event: str) -> str:
        return {
            'pipeline_started': '任务输入',
            'pipeline_finished': '任务完成',
            'pipeline_failed': '任务失败',
            'classification_input': '问题分类输入',
            'classification_raw_output': '问题分类原始输出',
            'classification_result': '问题分类结果',
            'classification_ai_errors': '问题分类 AI 错误',
            'parameter_resolution_input': '参数解析输入',
            'parameter_resolution_result': '参数解析结果',
            'evidence_template_selection_input': '证据模板选择输入',
            'evidence_template_selection_result': '证据模板选择结果',
            'archive_extracted': '解压结果',
            'profile_result': '文件树扫描结果',
            'keyword_plan': '采样关键词计划',
            'sampling_result': '采样输出',
            'planner_input': 'Planner 输入',
            'planner_raw_output': 'Planner 原始输出',
            'planner_result': 'Planner 结构化结果',
            'planner_ai_errors': 'Planner AI 错误',
            'rule_pack_selection': '规则包选择',
            'matching_result': '规则命中结果',
            'first_evidence_pack_result': '首轮证据包',
            'case_recall_result': '案例召回结果',
            'first_report_input': '首轮报告输入',
            'first_report_prompt': '首轮报告 Prompt',
            'first_report_raw_output': '首轮报告原始输出',
            'first_report_result': '首轮报告结果',
            'verifier_input': 'Verifier 输入',
            'verifier_prompt': 'Verifier Prompt',
            'verifier_raw_output': 'Verifier 原始输出',
            'verifier_result': 'Verifier 结果',
            'first_pass_confidence_decision': '是否自动 Deep 的决策',
            'deep_pipeline_started': 'Deep 触发输入',
            'deep_evidence_result': 'Deep 证据扩展结果',
            'deep_report_input': 'Deep 报告输入',
            'deep_report_prompt': 'Deep 报告 Prompt',
            'deep_report_raw_output': 'Deep 报告原始输出',
            'deep_report_result': 'Deep 报告结果',
            'deep_pipeline_finished': 'Deep 完成',
            'ai_thinking_delta': 'AI 可见思考片段',
            'ai_text_delta': 'AI 输出片段',
            'ai_tool_event': 'AI 工具片段',
            'ai_thinking_stream': 'AI 可见思考流',
            'ai_text_stream': 'AI 输出流',
            'ai_tool_stream': 'AI 工具流',
            'ai_token_usage': 'AI Token 统计',
        }.get(event, event)

    def _android_trace_summary(event: str, data: dict) -> str:
        data = data if isinstance(data, dict) else {}
        if event == 'classification_input':
            return f"模块 {data.get('module_count', 0)} 个，小类 {data.get('subcategory_count', 0)} 个"
        if event == 'classification_result':
            return (
                f"{data.get('module_id') or 'unknown'} / {data.get('submodule_id') or 'unknown'}，"
                f"置信度 {data.get('module_confidence', 0)}"
            )
        if event == 'classification_ai_errors':
            return f"错误 {len(data.get('errors') or [])} 个"
        if event == 'parameter_resolution_input':
            return f"模块 {data.get('module_id') or 'unknown'}，默认包名 {len(data.get('default_package_names') or [])} 个，应用清单 {data.get('inventory_app_count', 0)} 个"
        if event == 'parameter_resolution_result':
            resolved = (data.get('resolved_parameters') or {}).get('package_name') or []
            return f"解析包名 {len(resolved)} 个，候选 {len(data.get('package_candidates') or [])} 个"
        if event == 'evidence_template_selection_input':
            return f"候选模块 {data.get('module_selection_count', 0)} 个，可用模块 {data.get('available_module_count', 0)} 个"
        if event == 'evidence_template_selection_result':
            counts = data.get('counts') or {}
            return f"模板 {counts.get('template_count', 0)} 条，XML 状态模板 {counts.get('xml_state_template_count', 0)} 条，待参数 {counts.get('needs_parameters_count', 0)} 条"
        if event == 'profile_result':
            return f"文件 {data.get('file_count', 0)} 个，总大小 {data.get('total_size', 0)} 字节"
        if event == 'keyword_plan':
            return f"关键词 {data.get('keyword_count', 0)} 个，候选文件 {data.get('manifest_file_count', 0)} 个"
        if event == 'sampling_result':
            return f"采样文件 {data.get('sampled_file_count') or data.get('file_count') or 0} 个，扫描 {data.get('considered_files', 0)} 个"
        if event == 'planner_input':
            return f"Prompt {data.get('prompt_chars', 0)} 字符，样本文件 {data.get('sample_file_count', 0)} 个"
        if event == 'planner_result':
            return f"类型 {', '.join(data.get('issue_types') or ['unknown'])}，置信度 {data.get('confidence', 0)}"
        if event == 'rule_pack_selection':
            return f"加载规则包 {len(data.get('loaded_rule_packs') or [])} 个"
        if event == 'matching_result':
            return f"命中事件 {data.get('event_count', 0)} 个，规则扫描 {data.get('rule_count') or data.get('rules_considered') or 0} 条"
        if event == 'first_evidence_pack_result':
            return f"候选证据 {data.get('event_count', 0)} 个，进入证据包 {data.get('top_event_count', 0)} 个"
        if event == 'case_recall_result':
            return f"召回案例 {data.get('selected_card_count', 0)} 个，候选 {data.get('loaded_card_count', 0)} 个"
        if event in {'first_report_result', 'deep_report_result'}:
            return f"模式 {data.get('report_mode') or 'unknown'}，报告可用 {bool(data.get('has_report'))}"
        if event == 'verifier_result':
            return f"状态 {data.get('status') or 'unknown'}，证据分 {data.get('best_evidence_score', 0)}，风险 {data.get('overclaim_risk') or 'unknown'}"
        if event == 'first_pass_confidence_decision':
            return f"置信度 {data.get('confidence')} / 阈值 {data.get('threshold')}，自动 Deep={bool(data.get('auto_deep'))}"
        if event == 'deep_evidence_result':
            return f"日志片段 {data.get('log_context_count', 0)} 个，代码文件 {data.get('code_file_count', 0)} 个"
        if event == 'ai_token_usage':
            return f"{data.get('interaction') or 'AI'}：输入 {data.get('input_tokens', 0)} / 输出 {data.get('output_tokens', 0)} / 总计 {data.get('total_tokens', 0)} tokens"
        if event in {'ai_thinking_stream', 'ai_text_stream', 'ai_tool_stream'}:
            return f"{data.get('interaction') or 'AI'}：{data.get('chunk_count', 0)} 个流式片段，{data.get('content_chars', 0)} 字符"
        if event in {'ai_thinking_delta', 'ai_text_delta', 'ai_tool_event'}:
            content = str(data.get('content') or '').replace('\n', ' ')
            return content[:160] + ('...' if len(content) > 160 else '')
        output_chars = data.get('output_chars')
        prompt_chars = data.get('prompt_chars')
        if output_chars is not None:
            return f"输出 {output_chars} 字符"
        if prompt_chars is not None:
            return f"Prompt {prompt_chars} 字符"
        return ''

    def _android_process_details(store: AndroidAnalysisJobStore, job_id: str) -> dict:
        artifacts_dir = store.artifacts_dir(job_id)
        events = store.read_events(job_id)
        trace_records = _read_android_debug_trace(artifacts_dir)
        metrics = _read_android_json_artifact(artifacts_dir, 'analysis_metrics.json', {})
        bundle_titles = _android_bundle_titles_for_job(store, job_id, artifacts_dir)
        title_prefix = f"Android {' / '.join(bundle_titles)}" if bundle_titles else 'Android '
        titles = _android_stage_titles()
        order = [
            'queued',
            'classifying_question',
            'resolving_parameters',
            'selecting_evidence_templates',
            'extracting',
            'profiling',
            'sampling',
            'planning',
            'sampling_refined',
            'matching_rules',
            'building_evidence',
            'recalling_cases',
            'generating_report',
            'verifying_first_report',
            'first_pass_decision',
            'deep_scoping',
            'deep_reporting',
            'verifying',
            'metrics',
            'casebook',
            'job',
        ]
        stages: dict[str, dict] = {}

        def ensure(stage_id: str) -> dict:
            if stage_id not in stages:
                stages[stage_id] = {
                    'id': stage_id,
                    'title': titles.get(stage_id, stage_id),
                    'status': 'done',
                    'duration_seconds': None,
                    'started_at': '',
                    'finished_at': '',
                    'items': [],
                }
            return stages[stage_id]

        for event in events:
            data = event.get('data') if isinstance(event.get('data'), dict) else {}
            et = event.get('type')
            if et == 'job_updated':
                ensure(str(data.get('status') or 'job'))['started_at'] = event.get('timestamp') or ''
            elif et == 'stage_timing':
                stage_id = str(data.get('stage') or '')
                stage = ensure(stage_id)
                stage['duration_seconds'] = data.get('duration_seconds')
                stage['finished_at'] = event.get('timestamp') or stage.get('finished_at') or ''

        for record in trace_records:
            event = str(record.get('event') or '')
            data = record.get('data') if isinstance(record.get('data'), dict) else {}
            if event in {'ai_thinking_delta', 'ai_text_delta', 'ai_tool_event'}:
                continue
            stage = ensure(_android_stage_for_trace(record))
            stage['items'].append(
                {
                    'timestamp': record.get('timestamp') or '',
                    'event': event,
                    'title': _android_trace_title(event),
                    'summary': _android_trace_summary(event, data),
                    'data': data,
                }
            )

        for stream_item in _android_ai_stream_items(trace_records):
            stage = ensure(stream_item.pop('stage_id'))
            stage['items'].append(stream_item)

        for item in metrics.get('stage_timings') or []:
            if isinstance(item, dict) and item.get('stage'):
                stage = ensure(str(item.get('stage')))
                stage['duration_seconds'] = item.get('duration_seconds')

        stage_list = [stages[key] for key in order if key in stages]
        stage_list.extend(value for key, value in stages.items() if key not in order)
        for stage in stage_list:
            stage['item_count'] = len(stage.get('items') or [])
        return {
            'version': 1,
            'job_id': job_id,
            'bundle_titles': bundle_titles,
            'process_overview_title': f'{title_prefix}问题分析过程概览',
            'process_detail_title': f'{title_prefix}问题分析过程详情',
            'stages': stage_list,
            'metrics': metrics,
            'trace_record_count': len(trace_records),
            'event_count': len(events),
        }

    def _android_ai_stream_items(trace_records: list[dict]) -> list[dict]:
        event_map = {
            'ai_thinking_delta': ('ai_thinking_stream', 'AI 可见思考流'),
            'ai_text_delta': ('ai_text_stream', 'AI 输出流'),
            'ai_tool_event': ('ai_tool_stream', 'AI 工具流'),
        }
        grouped: dict[tuple[str, str, str], dict] = {}
        order_keys = []
        for record in trace_records:
            event = str(record.get('event') or '')
            if event not in event_map:
                continue
            data = record.get('data') if isinstance(record.get('data'), dict) else {}
            content = str(data.get('content') or '')
            if not content:
                continue
            stage_id = _android_stage_for_trace(record)
            interaction = str(data.get('interaction') or '')
            normalized_event, title = event_map[event]
            key = (stage_id, normalized_event, interaction)
            if key not in grouped:
                grouped[key] = {
                    'stage_id': stage_id,
                    'timestamp': record.get('timestamp') or '',
                    'event': normalized_event,
                    'title': title,
                    'interaction': interaction,
                    'chunks': [],
                }
                order_keys.append(key)
            grouped[key]['chunks'].append(content)

        items = []
        for key in order_keys:
            item = grouped[key]
            chunks = item.pop('chunks')
            content = ''.join(chunks)
            data = {
                'interaction': item.pop('interaction'),
                'chunk_count': len(chunks),
                'content_chars': len(content),
                'content': _android_limit_text(content, 24000),
            }
            event = item['event']
            item['summary'] = _android_trace_summary(event, data)
            item['data'] = data
            items.append(item)
        return items

    def _android_limit_text(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[:limit] + f'\n...<truncated {len(text) - limit} chars>'

    def _android_float(value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _android_first_pass_confidence(verifier: dict, planner: dict, matched: dict) -> dict:
        # 首轮可信度用于成本控制：足够可信时停在人工可继续，证据不足时自动升级 Deep。
        best_score = _android_float(verifier.get('best_evidence_score'), 0.0)
        planner_score = _android_float(planner.get('confidence'), 0.0)
        event_count = int((matched or {}).get('event_count') or 0)
        status = verifier.get('status') or ''
        risk = verifier.get('overclaim_risk') or ''
        confidence = best_score
        reasons = [f'best_evidence_score={best_score:.2f}', f'planner_confidence={planner_score:.2f}']
        if event_count <= 0:
            confidence = min(confidence, 0.25)
            reasons.append('no_rule_evidence')
        if status == 'needs_more_evidence':
            confidence = min(confidence, 0.35)
            reasons.append('verifier_needs_more_evidence')
        elif status == 'partially_supported' and planner_score < 0.45:
            confidence = min(confidence, 0.62)
            reasons.append('low_planner_confidence')
        if risk == 'high':
            confidence = min(confidence, 0.45)
            reasons.append('high_overclaim_risk')
        elif risk == 'medium' and best_score < 0.75:
            confidence = min(confidence, 0.68)
            reasons.append('medium_risk_without_high_evidence')
        threshold = _android_float(getattr(config, 'ANDROID_ANALYSIS_AUTO_DEEP_CONFIDENCE_THRESHOLD', 0.72), 0.72)
        confidence = max(0.0, min(1.0, confidence))
        return {
            'confidence': round(confidence, 3),
            'threshold': round(max(0.0, min(1.0, threshold)), 3),
            'auto_deep': confidence < threshold,
            'status': status,
            'overclaim_risk': risk,
            'event_count': event_count,
            'reasons': reasons,
        }

    def _android_report_metadata(job_id: str, phase: str, confidence: dict | None = None) -> dict:
        meta = {'android_analysis_job_id': job_id, 'android_analysis_phase': phase}
        if confidence:
            meta['android_analysis_confidence'] = confidence.get('confidence')
            meta['android_analysis_auto_deep_threshold'] = confidence.get('threshold')
        return meta

    def _run_android_deep_pipeline(
        store: AndroidAnalysisJobStore,
        job_id: str,
        client_ip: str = '',
        user_id: str = '',
        session_id: str = '',
        preferred_paths: dict | None = None,
        enable_ai: bool | None = None,
        trigger: str = 'manual',
        trigger_message: str = '',
    ) -> dict:
        preferred_paths = preferred_paths if isinstance(preferred_paths, dict) else {}
        if enable_ai is None:
            enable_ai = not app.config.get('TESTING', False)
        try:
            job = store.load_job(job_id)
        except OSError:
            raise AndroidAnalysisError('android_analysis_job_not_found', 'Android analysis job not found.')

        if trigger_message and client_ip and user_id and session_id:
            try:
                sm.add_message(client_ip, user_id, session_id, 'user', trigger_message)
            except Exception as exc:
                log.warning('[AndroidAnalysis] failed to persist deep trigger message: %s', exc)

        timings: list[dict] = []
        tracer = AndroidAnalysisDebugTracer(
            getattr(config, 'ANDROID_ANALYSIS_DEBUG_TRACE', True),
            store.artifacts_dir(job_id),
            log,
            job_id,
            event_sink=_android_trace_event_sink(store, job_id),
        )
        try:
            tracer.trace(
                'deep',
                'deep_pipeline_started',
                {
                    'question': job.get('question') or '',
                    'preferred_paths': preferred_paths,
                    'enable_ai': enable_ai,
                    'trigger': trigger,
                },
            )
            store.append_event(job_id, 'deep_started', {'trigger': trigger})
            store.update_job(job_id, status='deep_scoping', deep_status='running', deep_trigger=trigger)
            deep_summary = _timed_android_stage(
                store,
                job_id,
                timings,
                'deep_scoping',
                lambda: build_deep_evidence_pack(
                    store.artifacts_dir(job_id),
                    store.extracted_dir(job_id),
                    question=job.get('question') or '',
                    configured_bundles=_readonly_bundles(),
                    preferred_paths=preferred_paths,
                    debug_trace=tracer.trace,
                ),
            )
            store.append_event(
                job_id,
                'deep_evidence_created',
                {
                    'has_code_context': bool(deep_summary.get('has_code_context')),
                    'code_file_count': deep_summary.get('code_file_count', 0),
                    'log_context_count': deep_summary.get('log_context_count', 0),
                },
            )
            store.update_job(job_id, status='deep_reporting')
            deep_report = _timed_android_stage(
                store,
                job_id,
                timings,
                'deep_reporting',
                lambda: generate_deep_report(
                    store.artifacts_dir(job_id),
                    question=job.get('question') or '',
                    cli_path=config.CLAUDE_CLI_PATH,
                    timeout_seconds=config.ANDROID_ANALYSIS_PLANNER_TIMEOUT_SECONDS,
                    enable_ai=enable_ai,
                    debug_trace=tracer.trace,
                ),
            )
            store.append_event(
                job_id,
                'deep_report_generated',
                {
                    'report_mode': deep_report.get('report_mode'),
                    'has_report': bool(deep_report.get('has_report')),
                },
            )
            store.update_job(job_id, status='verifying')
            verifier = _timed_android_stage(
                store,
                job_id,
                timings,
                'verifying',
                lambda: run_verifier(
                    store.artifacts_dir(job_id),
                    report_name='deep_report.md',
                    cli_path=config.CLAUDE_CLI_PATH,
                    timeout_seconds=config.ANDROID_ANALYSIS_PLANNER_TIMEOUT_SECONDS,
                    enable_ai=enable_ai,
                    debug_trace=tracer.trace,
                ),
            )
            store.append_event(
                job_id,
                'verifier_completed',
                {
                    'phase': 'deep',
                    'status': verifier.get('status'),
                    'overclaim_risk': verifier.get('overclaim_risk'),
                    'best_evidence_score': verifier.get('best_evidence_score'),
                },
            )
            job = store.load_job(job_id)
            artifacts = _merge_android_artifacts(
                job,
                {
                    'deep_evidence_pack': 'artifacts/deep_evidence_pack.md',
                    'deep_evidence_summary': 'artifacts/deep_evidence_pack.json',
                    'deep_report': 'artifacts/deep_report.md',
                    'deep_report_meta': 'artifacts/deep_report_meta.json',
                    'verifier_result': 'artifacts/verifier_result.json',
                    'verified_report': 'artifacts/verified_report.md',
                },
            )
            if getattr(config, 'ANDROID_ANALYSIS_DEBUG_TRACE', True):
                artifacts['android_debug_trace'] = 'artifacts/android_debug_trace.jsonl'
            _write_android_metrics(store, job_id, timings)
            artifacts['analysis_metrics'] = 'artifacts/analysis_metrics.json'
            status = 'verified' if verifier.get('status') == 'supported' else 'needs_review'
            tracer.trace('deep', 'deep_pipeline_finished', {'status': status, 'timings': timings, 'trigger': trigger})
            job = store.update_job(
                job_id,
                status=status,
                artifacts=artifacts,
                error=None,
                deep_status='completed',
                deep_available=False,
            )
            if client_ip and user_id and session_id:
                try:
                    report_text = (store.artifacts_dir(job_id) / 'verified_report.md').read_text(encoding='utf-8')
                    sm.add_message(
                        client_ip,
                        user_id,
                        session_id,
                        'assistant',
                        report_text,
                        thinking=_android_progress_markdown(store.read_events(job_id)),
                        metadata=_android_report_metadata(job_id, 'deep'),
                    )
                except Exception as exc:
                    log.warning('[AndroidAnalysis] failed to persist deep report message: %s', exc)
            return {'job': job, 'deep': deep_summary, 'verifier': verifier}
        except AndroidAnalysisError as e:
            log.warning('[AndroidAnalysis] deep job=%s failed: %s %s', job_id, e.code, e.message)
            tracer.trace('deep', 'deep_pipeline_failed', {'code': e.code, 'message': e.message, 'trigger': trigger})
            job = store.update_job(job_id, status='error', deep_status='error', error={'code': e.code, 'message': e.message})
            return {'job': job, 'error': {'code': e.code, 'message': e.message}}

    def _run_android_first_pass_pipeline(
        store: AndroidAnalysisJobStore,
        job_id: str,
        source_path: Path,
        question: str,
        bundle_ids: list,
        enable_ai: bool,
        client_ip: str = '',
        user_id: str = '',
        session_id: str = '',
    ) -> dict:
        # 首轮分析是后台 job 的主流水线：先本地解压/采样/规则匹配，再把受控证据包交给 Claude。
        # 任何异常都会落到 job.json，避免 SSE 客户端长时间等待无结果。
        timings: list[dict] = []
        tracer = AndroidAnalysisDebugTracer(
            getattr(config, 'ANDROID_ANALYSIS_DEBUG_TRACE', True),
            store.artifacts_dir(job_id),
            log,
            job_id,
            event_sink=_android_trace_event_sink(store, job_id),
        )
        tracer.trace(
            'job',
            'pipeline_started',
            {
                'source_path': str(source_path),
                'question': question,
                'bundle_ids': bundle_ids,
                'enable_ai': enable_ai,
                'client_ip': client_ip,
                'user_id': user_id,
                'session_id': session_id,
            },
        )
        try:
            # Phase 3: 只根据用户原始描述做模块/小类候选分类。
            # 这里不能读取上传日志、源码、skill 或历史案例；结果先作为可观测产物落盘，
            # 后续 Phase 4/5 再逐步接入参数提取和证据模板选择。
            store.update_job(job_id, status='classifying_question')
            classification = _timed_android_stage(
                store,
                job_id,
                timings,
                'classifying_question',
                lambda: run_question_classifier(
                    store.artifacts_dir(job_id),
                    question=question,
                    expert_knowledge_cache=app.config.get(ANDROID_EXPERT_KNOWLEDGE_KEY),
                    cli_path=config.CLAUDE_CLI_PATH,
                    timeout_seconds=config.ANDROID_ANALYSIS_PLANNER_TIMEOUT_SECONDS,
                    enable_ai=enable_ai,
                    debug_trace=tracer.trace,
                ),
            )
            store.append_event(
                job_id,
                'classification_completed',
                {
                    'classifier_mode': classification.get('classifier_mode'),
                    'module_id': classification.get('module_id'),
                    'module_confidence': classification.get('module_confidence'),
                    'submodule_id': classification.get('submodule_id'),
                    'submodule_confidence': classification.get('submodule_confidence'),
                    'profile': classification.get('profile'),
                    'candidate_count': len(classification.get('top_candidates') or []),
                    'need_user_clarification': bool(classification.get('need_user_clarification')),
                },
            )

            store.update_job(job_id, status='resolving_parameters')
            parameter_resolution = _timed_android_stage(
                store,
                job_id,
                timings,
                'resolving_parameters',
                lambda: run_parameter_resolution(
                    store.artifacts_dir(job_id),
                    question=question,
                    expert_knowledge_cache=app.config.get(ANDROID_EXPERT_KNOWLEDGE_KEY),
                    classification=classification,
                    debug_trace=tracer.trace,
                ),
            )
            store.append_event(
                job_id,
                'parameters_resolved',
                {
                    'module_id': parameter_resolution.get('module_id'),
                    'need_package_resolution': bool(parameter_resolution.get('need_package_resolution')),
                    'package_candidate_count': len(parameter_resolution.get('package_candidates') or []),
                    'resolved_package_names': (parameter_resolution.get('resolved_parameters') or {}).get('package_name') or [],
                    'need_user_clarification': bool(parameter_resolution.get('need_user_clarification')),
                },
            )

            store.update_job(job_id, status='selecting_evidence_templates')
            selected_evidence = _timed_android_stage(
                store,
                job_id,
                timings,
                'selecting_evidence_templates',
                lambda: run_evidence_template_selection(
                    store.artifacts_dir(job_id),
                    app.config.get(ANDROID_EXPERT_KNOWLEDGE_KEY),
                    classification=classification,
                    parameter_resolution=parameter_resolution,
                    debug_trace=tracer.trace,
                ),
            )
            store.append_event(
                job_id,
                'evidence_templates_selected',
                {
                    'module_selection_count': len(selected_evidence.get('module_selections') or []),
                    'template_count': (selected_evidence.get('counts') or {}).get('template_count', 0),
                    'xml_state_template_count': (selected_evidence.get('counts') or {}).get('xml_state_template_count', 0),
                    'ready_count': (selected_evidence.get('counts') or {}).get('ready_count', 0),
                    'needs_parameters_count': (selected_evidence.get('counts') or {}).get('needs_parameters_count', 0),
                },
            )

            store.update_job(job_id, status='extracting')
            extraction = _timed_android_stage(
                store,
                job_id,
                timings,
                'extracting',
                lambda: safe_extract_archive(
                    source_path,
                    store.extracted_dir(job_id),
                    seven_zip_path=config.ANDROID_ANALYSIS_7Z_PATH,
                ),
            )
            store.append_event(job_id, 'archive_extracted', extraction)
            tracer.trace('extracting', 'archive_extracted', extraction)

            store.update_job(job_id, status='profiling')
            profile = _timed_android_stage(
                store,
                job_id,
                timings,
                'profiling',
                lambda: profile_extracted_tree(store.extracted_dir(job_id), store.artifacts_dir(job_id)),
            )
            tracer.trace(
                'profiling',
                'profile_result',
                {
                    'file_count': (profile.get('manifest') or {}).get('file_count', 0),
                    'total_size': (profile.get('manifest') or {}).get('total_size', 0),
                    'kind_counts': _android_kind_counts((profile.get('manifest') or {}).get('files') or []),
                    'tree_preview': profile.get('tree') or {},
                },
            )

            store.update_job(job_id, status='sampling')
            samples = _timed_android_stage(
                store,
                job_id,
                timings,
                'sampling',
                lambda: sample_files(
                    store.extracted_dir(job_id),
                    store.artifacts_dir(job_id),
                    question=question,
                    debug_trace=tracer.trace,
                ),
            )
            store.append_event(job_id, 'files_sampled', {'file_count': samples.get('file_count', 0)})

            store.update_job(job_id, status='planning')
            bundles = list_android_analysis_bundles(config.ANDROID_ANALYSIS_KNOWLEDGE_DIR)
            planner = _timed_android_stage(
                store,
                job_id,
                timings,
                'planning',
                lambda: run_planner(
                    store.artifacts_dir(job_id),
                    question=question,
                    bundles=bundles,
                    requested_bundle_ids=bundle_ids,
                    cli_path=config.CLAUDE_CLI_PATH,
                    timeout_seconds=config.ANDROID_ANALYSIS_PLANNER_TIMEOUT_SECONDS,
                    prompt_limits=_android_planner_prompt_limits(),
                    enable_ai=enable_ai,
                    debug_trace=tracer.trace,
                ),
            )
            store.append_event(
                job_id,
                'planner_completed',
                {
                    'planner_mode': planner.get('planner_mode'),
                    'issue_types': planner.get('issue_types') or [],
                    'candidate_bundle_ids': planner.get('candidate_bundle_ids') or [],
                },
            )

            # Planner 已经判断出候选日志路径和关键词后，重新采样一次，避免首轮自然排序被
            # dropbox/crash 等高噪声目录占满，导致真正相关的 shared_prefs / RDM 日志没有进入证据链。
            store.update_job(job_id, status='sampling_refined')
            refined_samples = _timed_android_stage(
                store,
                job_id,
                timings,
                'sampling_refined',
                lambda: sample_files(
                    store.extracted_dir(job_id),
                    store.artifacts_dir(job_id),
                    question=question,
                    keywords=planner.get('candidate_keywords') or [],
                    priority_paths=planner.get('candidate_log_paths') or [],
                    phase='planner_refined',
                    debug_trace=tracer.trace,
                ),
            )
            store.append_event(
                job_id,
                'files_resampled',
                {
                    'file_count': refined_samples.get('file_count', 0),
                    'priority_path_count': len(planner.get('candidate_log_paths') or []),
                    'keyword_count': len(planner.get('candidate_keywords') or []),
                },
            )

            store.update_job(job_id, status='matching_rules')
            matched = _timed_android_stage(
                store,
                job_id,
                timings,
                'matching_rules',
                lambda: run_rule_matching(
                    store.artifacts_dir(job_id),
                    config.ANDROID_ANALYSIS_KNOWLEDGE_DIR,
                    question=question,
                    debug_trace=tracer.trace,
                ),
            )
            store.append_event(
                job_id,
                'rules_matched',
                {
                    'rule_pack_count': matched.get('rule_pack_count', 0),
                    'event_count': matched.get('event_count', 0),
                },
            )
            store.update_job(job_id, status='matching_xml_state')
            xml_state_matches = _timed_android_stage(
                store,
                job_id,
                timings,
                'matching_xml_state',
                lambda: run_xml_state_matching(
                    store.extracted_dir(job_id),
                    store.artifacts_dir(job_id),
                    app.config.get(ANDROID_EXPERT_KNOWLEDGE_KEY),
                    planner,
                    debug_trace=tracer.trace,
                ),
            )
            if xml_state_matches.get('stats', {}).get('matched_event_count', 0):
                matched = json.loads((store.artifacts_dir(job_id) / 'matched_rules.json').read_text(encoding='utf-8'))
            store.append_event(
                job_id,
                'xml_state_matched',
                {
                    'template_count': (xml_state_matches.get('stats') or {}).get('template_count', 0),
                    'event_count': (xml_state_matches.get('stats') or {}).get('matched_event_count', 0),
                },
            )

            store.update_job(job_id, status='building_evidence')
            evidence = _timed_android_stage(
                store,
                job_id,
                timings,
                'building_evidence',
                lambda: generate_first_evidence_pack(
                    store.artifacts_dir(job_id),
                    question=question,
                    debug_trace=tracer.trace,
                ),
            )
            store.append_event(
                job_id,
                'evidence_pack_created',
                {
                    'event_count': evidence.get('event_count', 0),
                    'has_evidence': bool(evidence.get('has_evidence')),
                },
            )

            store.update_job(job_id, status='recalling_cases')
            case_cards = _timed_android_stage(
                store,
                job_id,
                timings,
                'recalling_cases',
                lambda: recall_case_cards(
                    config.ANDROID_ANALYSIS_KNOWLEDGE_DIR,
                    planner,
                    matched,
                    artifacts_dir=store.artifacts_dir(job_id),
                    expert_knowledge_cache=app.config.get(ANDROID_EXPERT_KNOWLEDGE_KEY),
                    debug_trace=tracer.trace,
                ),
            )
            write_case_cards(store.artifacts_dir(job_id), case_cards)
            store.append_event(job_id, 'case_cards_recalled', {'card_count': case_cards.get('card_count', 0)})

            store.update_job(job_id, status='generating_report')
            report = _timed_android_stage(
                store,
                job_id,
                timings,
                'generating_report',
                lambda: generate_first_report(
                    store.artifacts_dir(job_id),
                    question=question,
                    cli_path=config.CLAUDE_CLI_PATH,
                    timeout_seconds=config.ANDROID_ANALYSIS_PLANNER_TIMEOUT_SECONDS,
                    enable_ai=enable_ai,
                    debug_trace=tracer.trace,
                ),
            )
            store.append_event(
                job_id,
                'first_report_generated',
                {
                    'report_mode': report.get('report_mode'),
                    'has_report': bool(report.get('has_report')),
                },
            )
            store.update_job(job_id, status='verifying')
            verifier = _timed_android_stage(
                store,
                job_id,
                timings,
                'verifying_first_report',
                lambda: run_verifier(
                    store.artifacts_dir(job_id),
                    report_name='final_report.md',
                    cli_path=config.CLAUDE_CLI_PATH,
                    timeout_seconds=config.ANDROID_ANALYSIS_PLANNER_TIMEOUT_SECONDS,
                    enable_ai=False,
                    debug_trace=tracer.trace,
                ),
            )
            store.append_event(
                job_id,
                'verifier_completed',
                {
                    'phase': 'first_pass',
                    'status': verifier.get('status'),
                    'overclaim_risk': verifier.get('overclaim_risk'),
                    'best_evidence_score': verifier.get('best_evidence_score'),
                },
            )
            confidence = _android_first_pass_confidence(verifier, planner, matched)
            store.append_event(job_id, 'first_pass_confidence', confidence)
            tracer.trace('job', 'first_pass_confidence_decision', confidence)
            _write_android_metrics(store, job_id, timings)
            artifacts = {
                'classification_result': 'artifacts/classification_result.json',
                'classification_prompt': 'artifacts/classification_prompt.md',
                'classification_metrics': 'artifacts/classification_metrics.json',
                'classification_raw_output': 'artifacts/classification_raw_output.txt',
                'parameter_resolution': 'artifacts/parameter_resolution.json',
                'parameter_resolution_metrics': 'artifacts/parameter_resolution_metrics.json',
                'selected_evidence_templates': 'artifacts/selected_evidence_templates.json',
                'selected_evidence_templates_metrics': 'artifacts/selected_evidence_templates_metrics.json',
                'file_manifest': 'artifacts/file_manifest.json',
                'file_tree': 'artifacts/file_tree.json',
                'file_samples': 'artifacts/file_samples.json',
                'planner_result': 'artifacts/planner_result.json',
                'matched_rules': 'artifacts/matched_rules.json',
                'first_evidence_pack': 'artifacts/first_evidence_pack.md',
                'first_evidence_summary': 'artifacts/first_evidence_pack.json',
                'case_cards': 'artifacts/case_cards.json',
                'final_report': 'artifacts/final_report.md',
                'first_report_meta': 'artifacts/first_report_meta.json',
                'verifier_result': 'artifacts/verifier_result.json',
                'verified_report': 'artifacts/verified_report.md',
                'analysis_metrics': 'artifacts/analysis_metrics.json',
            }
            if getattr(config, 'ANDROID_ANALYSIS_DEBUG_TRACE', True):
                artifacts['android_debug_trace'] = 'artifacts/android_debug_trace.jsonl'
            final_status = 'needs_review' if verifier.get('status') == 'needs_more_evidence' else 'report_ready'
            if confidence.get('auto_deep'):
                store.append_event(
                    job_id,
                    'auto_deep_triggered',
                    {
                        'confidence': confidence.get('confidence'),
                        'threshold': confidence.get('threshold'),
                        'reasons': confidence.get('reasons') or [],
                    },
                )
                store.update_job(
                    job_id,
                    status='auto_deep_pending',
                    artifacts=artifacts,
                    error=None,
                    first_pass_confidence=confidence.get('confidence'),
                    auto_deep_threshold=confidence.get('threshold'),
                    deep_available=True,
                    deep_status='auto_pending',
                    deep_decision=confidence,
                )
                tracer.trace('job', 'pipeline_auto_deep_handoff', {'job_id': job_id, 'decision': confidence})
                result = _run_android_deep_pipeline(
                    store,
                    job_id,
                    client_ip=client_ip,
                    user_id=user_id,
                    session_id=session_id,
                    preferred_paths={},
                    enable_ai=enable_ai,
                    trigger='auto_confidence',
                )
                return result.get('job') or store.load_job(job_id)
            tracer.trace(
                'job',
                'pipeline_finished',
                {
                    'final_status': final_status,
                    'artifact_keys': sorted(artifacts.keys()),
                    'timings': timings,
                    'deep_decision': confidence,
                },
            )
            job = store.update_job(
                job_id,
                status=final_status,
                artifacts=artifacts,
                error=None,
                first_pass_confidence=confidence.get('confidence'),
                auto_deep_threshold=confidence.get('threshold'),
                deep_available=True,
                deep_status='not_started',
                deep_decision=confidence,
            )
            if client_ip and user_id and session_id:
                try:
                    report_path = store.artifacts_dir(job_id) / 'verified_report.md'
                    if not report_path.is_file():
                        report_path = store.artifacts_dir(job_id) / 'final_report.md'
                    report_text = report_path.read_text(encoding='utf-8')
                    sm.add_message(
                        client_ip,
                        user_id,
                        session_id,
                        'assistant',
                        report_text,
                        thinking=_android_progress_markdown(store.read_events(job_id)),
                        metadata=_android_report_metadata(job_id, 'first_pass', confidence),
                    )
                except Exception as exc:
                    log.warning('[AndroidAnalysis] failed to persist report message: %s', exc)
            return job
        except AndroidAnalysisError as e:
            log.warning('[AndroidAnalysis] job=%s failed: %s %s', job_id, e.code, e.message)
            tracer.trace('job', 'pipeline_failed', {'code': e.code, 'message': e.message})
            artifacts = {'android_debug_trace': 'artifacts/android_debug_trace.jsonl'} if getattr(config, 'ANDROID_ANALYSIS_DEBUG_TRACE', True) else {}
            return store.update_job(job_id, status='error', artifacts=artifacts, error={'code': e.code, 'message': e.message})
        except Exception as e:
            log.exception('[AndroidAnalysis] job=%s unexpected failure', job_id)
            tracer.trace('job', 'pipeline_failed', {'code': 'android_analysis_unexpected_error', 'message': str(e)})
            artifacts = {'android_debug_trace': 'artifacts/android_debug_trace.jsonl'} if getattr(config, 'ANDROID_ANALYSIS_DEBUG_TRACE', True) else {}
            return store.update_job(job_id, status='error', artifacts=artifacts, error={'code': 'android_analysis_unexpected_error', 'message': str(e)})

    def _session_for_user(client_ip: str, user_id: str, session_id: str):
        if not user_id:
            return None, (jsonify({'error': 'user_id required'}), 400)
        if not session_id:
            return None, (jsonify({'error': 'session_id required'}), 400)
        session = sm.get_session(client_ip, user_id, session_id)
        if not session:
            return None, (jsonify({'error': 'Session not found'}), 404)
        return session, None

    def _attached_dev_project(client_ip: str, user_id: str, session_id: str):
        if not _dev_enabled():
            return None, None
        meta = load_dev_session(sm.get_session_dir(client_ip, user_id, session_id))
        if not meta or meta.get('mode') != 'development':
            return None, None
        try:
            project = find_project(_dev_projects(), meta.get('project_id') or '')
        except DevProjectError:
            project = None
        return meta, project

    @app.route('/api/features', methods=['GET'])
    def api_features():
        return jsonify(
            {
                'v2_multi_user_api': bool(config.FEATURE_V2_MULTI_USER_API),
                'v3_linux_deploy': bool(config.FEATURE_V3_LINUX_DEPLOY),
                'tavily_search_configured': bool(config.TAVILY_API_KEY),
                'mobile_remote_development': _dev_enabled(),
                'gemini_support': bool(config.FEATURE_GEMINI_SUPPORT),
                'gemini_configured': bool(config.FEATURE_GEMINI_SUPPORT and (config.GEMINI_CLI_PATH or '').strip()),
                'android_issue_analysis': bool(config.FEATURE_ANDROID_ISSUE_ANALYSIS),
                'android_issue_analysis_expert_workbench': bool(
                    getattr(config, 'FEATURE_ANDROID_ISSUE_ANALYSIS_EXPERT_WORKBENCH', False)
                ),
                'android_analysis_debug_trace': bool(getattr(config, 'ANDROID_ANALYSIS_DEBUG_TRACE', True)),
                'android_analysis_auto_deep_confidence_threshold': _android_float(
                    getattr(config, 'ANDROID_ANALYSIS_AUTO_DEEP_CONFIDENCE_THRESHOLD', 0.72),
                    0.72,
                ),
            }
        )

    @app.route('/api/android-analysis/status', methods=['GET'])
    @optional_token
    def api_android_analysis_status():
        if not _android_analysis_enabled():
            return _android_analysis_disabled_response()
        bundles = list_android_analysis_bundles(config.ANDROID_ANALYSIS_KNOWLEDGE_DIR)
        return jsonify(
            {
                'enabled': True,
                'knowledge_dir': str(config.ANDROID_ANALYSIS_KNOWLEDGE_DIR),
                'debug_trace': bool(getattr(config, 'ANDROID_ANALYSIS_DEBUG_TRACE', True)),
                'auto_deep_confidence_threshold': _android_float(
                    getattr(config, 'ANDROID_ANALYSIS_AUTO_DEEP_CONFIDENCE_THRESHOLD', 0.72),
                    0.72,
                ),
                'bundles': bundles,
                'expert_workbench': bool(getattr(config, 'FEATURE_ANDROID_ISSUE_ANALYSIS_EXPERT_WORKBENCH', False)),
                'expert_knowledge': summarize_expert_knowledge_cache(app.config.get(ANDROID_EXPERT_KNOWLEDGE_KEY)),
            }
        )

    @app.route('/api/android-analysis/expert-knowledge', methods=['GET'])
    @optional_token
    def api_android_analysis_expert_knowledge():
        if not _android_analysis_enabled():
            return _android_analysis_disabled_response()
        include_details = str(request.args.get('details') or '').lower() in {'1', 'true', 'yes', 'on'}
        return jsonify(
            {
                'enabled': bool(getattr(config, 'FEATURE_ANDROID_ISSUE_ANALYSIS_EXPERT_WORKBENCH', False)),
                'knowledge': summarize_expert_knowledge_cache(
                    app.config.get(ANDROID_EXPERT_KNOWLEDGE_KEY),
                    include_details=include_details,
                ),
            }
        )

    @app.route('/api/android-analysis/expert-knowledge/scaffold', methods=['POST'])
    @optional_token
    def api_android_analysis_expert_knowledge_scaffold():
        if not _android_analysis_enabled():
            return _android_analysis_disabled_response()
        if not _android_expert_workbench_enabled():
            return _android_expert_workbench_disabled_response()
        data = request.json or {}
        bundle, project_root, err, status = _resolve_expert_project(data)
        if err:
            return err, status
        module = data.get('module') if isinstance(data.get('module'), dict) else {}
        module = dict(module)
        module.setdefault('id', bundle.get('id'))
        module.setdefault('title', bundle.get('title') or bundle.get('name') or bundle.get('id'))
        module.setdefault('description', bundle.get('summary') or bundle.get('description') or '')
        try:
            result = create_project_knowledge_scaffold(
                project_root,
                module=module,
                subcategories=data.get('subcategories') if isinstance(data.get('subcategories'), list) else [],
                evidence_templates=data.get('evidence_templates') if isinstance(data.get('evidence_templates'), list) else [],
                xml_state_templates=data.get('xml_state_templates') if isinstance(data.get('xml_state_templates'), list) else [],
                relative_path=config.ANDROID_ANALYSIS_PROJECT_KNOWLEDGE_RELATIVE_PATH,
                overwrite=bool(data.get('overwrite')),
                include_skill=data.get('include_skill', True) is not False,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return jsonify({'error': str(exc), 'code': 'expert_scaffold_failed'}), 400
        cache = _refresh_android_expert_knowledge_cache()
        return jsonify(
            {
                'ok': not result.get('validation_errors'),
                'result': result,
                'knowledge': summarize_expert_knowledge_cache(cache, include_details=True),
            }
        )

    @app.route('/api/android-analysis/expert-knowledge/evidence-templates/convert', methods=['POST'])
    @optional_token
    def api_android_analysis_expert_knowledge_convert_evidence_templates():
        if not _android_analysis_enabled():
            return _android_analysis_disabled_response()
        if not _android_expert_workbench_enabled():
            return _android_expert_workbench_disabled_response()
        data = request.json or {}
        bundle, project_root, err, status = _resolve_expert_project(data)
        if err:
            return err, status
        direction = str(data.get('direction') or '').strip()
        knowledge_dir = project_root / config.ANDROID_ANALYSIS_PROJECT_KNOWLEDGE_RELATIVE_PATH
        try:
            result = convert_evidence_templates(
                knowledge_dir,
                direction,
                overwrite=data.get('overwrite', True) is not False,
            )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return jsonify({'error': str(exc), 'code': 'expert_conversion_failed'}), 400
        cache = _refresh_android_expert_knowledge_cache()
        return jsonify(
            {
                'ok': not result.get('validation_errors'),
                'result': result,
                'knowledge': summarize_expert_knowledge_cache(cache, include_details=True),
            }
        )

    @app.route('/api/android-analysis/expert-knowledge/xml-state-templates/convert', methods=['POST'])
    @optional_token
    def api_android_analysis_expert_knowledge_convert_xml_state_templates():
        if not _android_analysis_enabled():
            return _android_analysis_disabled_response()
        if not _android_expert_workbench_enabled():
            return _android_expert_workbench_disabled_response()
        data = request.json or {}
        bundle, project_root, err, status = _resolve_expert_project(data)
        if err:
            return err, status
        direction = str(data.get('direction') or '').strip()
        knowledge_dir = project_root / config.ANDROID_ANALYSIS_PROJECT_KNOWLEDGE_RELATIVE_PATH
        try:
            result = convert_xml_state_templates(
                knowledge_dir,
                direction,
                overwrite=data.get('overwrite', True) is not False,
            )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return jsonify({'error': str(exc), 'code': 'expert_xml_state_conversion_failed'}), 400
        cache = _refresh_android_expert_knowledge_cache()
        return jsonify(
            {
                'ok': not result.get('validation_errors'),
                'result': result,
                'knowledge': summarize_expert_knowledge_cache(cache, include_details=True),
            }
        )

    @app.route('/api/android-analysis/expert-knowledge/evidence-templates/generate', methods=['POST'])
    @optional_token
    def api_android_analysis_expert_knowledge_generate_evidence_templates():
        if not _android_analysis_enabled():
            return _android_analysis_disabled_response()
        if not _android_expert_workbench_enabled():
            return _android_expert_workbench_disabled_response()
        data = request.json or {}
        bundle, project_root, err, status = _resolve_expert_project(data)
        if err:
            return err, status
        subcategory_id = str(data.get('subcategory_id') or '').strip()
        if not subcategory_id:
            return jsonify({'error': 'subcategory_id is required.', 'code': 'missing_subcategory_id'}), 400
        mode = str(data.get('mode') or 'prefiltered').strip() or 'prefiltered'
        keyword_hints = data.get('keyword_hints') if isinstance(data.get('keyword_hints'), list) else None
        try:
            bundle_id = str((bundle or {}).get('id') or 'bundle')
            project_slug = re.sub(r'[^A-Za-z0-9_.-]+', '_', Path(project_root).name or 'project')
            output_dir = (
                config.ANDROID_ANALYSIS_KNOWLEDGE_DIR
                / 'expert_workbench'
                / re.sub(r'[^A-Za-z0-9_.-]+', '_', bundle_id)
                / project_slug
                / subcategory_id
            )
            result = run_evidence_template_generation_pipeline(
                project_root,
                subcategory_id=subcategory_id,
                relative_path=config.ANDROID_ANALYSIS_PROJECT_KNOWLEDGE_RELATIVE_PATH,
                output_dir=output_dir,
                mode=mode,
                claude_cli_path=str(config.CLAUDE_CLI_PATH),
                keyword_hints=[str(x) for x in keyword_hints] if keyword_hints is not None else None,
                max_candidates=int(data.get('max_candidates') or 120),
                timeout_seconds=int(data.get('timeout_seconds') or 900),
                dry_run=bool(data.get('dry_run')),
            )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return jsonify({'error': str(exc), 'code': 'expert_generation_failed'}), 400
        return jsonify({'ok': bool(result.get('ok')), 'result': result})

    @app.route('/api/android-analysis/expert-knowledge/xml-state-templates/generate', methods=['POST'])
    @optional_token
    def api_android_analysis_expert_knowledge_generate_xml_state_templates():
        if not _android_analysis_enabled():
            return _android_analysis_disabled_response()
        if not _android_expert_workbench_enabled():
            return _android_expert_workbench_disabled_response()
        data = request.json or {}
        bundle, project_root, err, status = _resolve_expert_project(data)
        if err:
            return err, status
        subcategory_ids = data.get('subcategory_ids') if isinstance(data.get('subcategory_ids'), list) else None
        subcategory_id = str(data.get('subcategory_id') or '').strip()
        if subcategory_id and not subcategory_ids:
            subcategory_ids = [subcategory_id]
        try:
            bundle_id = str((bundle or {}).get('id') or 'bundle')
            project_slug = re.sub(r'[^A-Za-z0-9_.-]+', '_', Path(project_root).name or 'project')
            output_dir = (
                config.ANDROID_ANALYSIS_KNOWLEDGE_DIR
                / 'expert_workbench'
                / re.sub(r'[^A-Za-z0-9_.-]+', '_', bundle_id)
                / project_slug
                / 'xml_state_templates'
            )
            result = run_xml_state_template_batch_generation_pipeline(
                project_root,
                subcategory_ids=[str(x) for x in subcategory_ids] if subcategory_ids else None,
                relative_path=config.ANDROID_ANALYSIS_PROJECT_KNOWLEDGE_RELATIVE_PATH,
                output_dir=output_dir,
                claude_cli_path=str(config.CLAUDE_CLI_PATH),
                per_subcategory_max_candidates=int(data.get('per_subcategory_max_candidates') or 25),
                timeout_seconds=int(data.get('timeout_seconds') or 1200),
                dry_run=bool(data.get('dry_run')),
            )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return jsonify({'error': str(exc), 'code': 'expert_xml_state_generation_failed'}), 400
        return jsonify({'ok': bool(result.get('ok')), 'result': result})

    @app.route('/api/android-analysis/jobs', methods=['POST'])
    @optional_token
    def api_android_analysis_jobs():
        if not _android_analysis_enabled():
            return _android_analysis_disabled_response()
        data = request.json or {}
        user_id = (data.get('user_id') or '').strip()
        session_id = (data.get('session_id') or '').strip()
        question = (data.get('question') or '').strip()
        source_filename = (data.get('source_filename') or '').strip()
        bundle_ids = data.get('bundle_ids') or []
        if not isinstance(bundle_ids, list):
            bundle_ids = []
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        _, err = _session_for_user(client_ip, user_id, session_id)
        if err:
            return err

        session_dir = sm.get_session_dir(client_ip, user_id, session_id)
        store = AndroidAnalysisJobStore(session_dir)
        job = store.create_job(question=question, source_files=[source_filename] if source_filename else [], bundle_ids=bundle_ids)
        if not source_filename:
            return jsonify({'job': job})

        if '/' in source_filename or '\\' in source_filename or Path(source_filename).name != source_filename:
            job = store.update_job(
                job['id'],
                status='error',
                error={'code': 'unsafe_source_filename', 'message': 'Invalid source filename.'},
            )
            return jsonify({'job': job}), 400
        source_path = (sm.get_upload_dir(client_ip, user_id, session_id) / source_filename).resolve()
        upload_dir = sm.get_upload_dir(client_ip, user_id, session_id).resolve()
        try:
            source_path.relative_to(upload_dir)
        except ValueError:
            job = store.update_job(
                job['id'],
                status='error',
                error={'code': 'unsafe_source_filename', 'message': 'Invalid source filename.'},
            )
            return jsonify({'job': job}), 400
        if not source_path.is_file():
            job = store.update_job(
                job['id'],
                status='error',
                error={'code': 'source_not_found', 'message': 'Uploaded source archive was not found.'},
            )
            return jsonify({'job': job}), 404

        try:
            sm.add_message(
                client_ip,
                user_id,
                session_id,
                'user',
                f'Android 问题分析\n\n文件：{source_filename}\n\n{question}',
                files=[source_filename],
            )
        except Exception as exc:
            log.warning('[AndroidAnalysis] failed to persist user message: %s', exc)

        enable_ai = not app.config.get('TESTING', False)
        if bool(data.get('background')):
            job = store.update_job(job['id'], status='queued')
            thread = threading.Thread(
                target=_run_android_first_pass_pipeline,
                kwargs={
                    'store': AndroidAnalysisJobStore(session_dir),
                    'job_id': job['id'],
                    'source_path': source_path,
                    'question': question,
                    'bundle_ids': bundle_ids,
                    'enable_ai': enable_ai,
                    'client_ip': client_ip,
                    'user_id': user_id,
                    'session_id': session_id,
                },
                daemon=True,
            )
            thread.start()
            return jsonify({'job': job, 'background': True})

        job = _run_android_first_pass_pipeline(
            store,
            job['id'],
            source_path,
            question,
            bundle_ids,
            enable_ai=enable_ai,
            client_ip=client_ip,
            user_id=user_id,
            session_id=session_id,
        )
        status_code = 400 if job.get('status') == 'error' else 200
        return jsonify({'job': job}), status_code

    @app.route('/api/android-analysis/jobs/latest', methods=['GET'])
    @optional_token
    def api_android_analysis_latest_job():
        if not _android_analysis_enabled():
            return _android_analysis_disabled_response()
        user_id = request.args.get('user_id', '').strip()
        session_id = request.args.get('session_id', '').strip()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        _, err = _session_for_user(client_ip, user_id, session_id)
        if err:
            return err
        store = AndroidAnalysisJobStore(sm.get_session_dir(client_ip, user_id, session_id))
        jobs = []
        if store.base_dir.is_dir():
            for child in store.base_dir.iterdir():
                if not child.is_dir():
                    continue
                try:
                    job = store.load_job(child.name)
                except OSError:
                    continue
                if job.get('status') != 'error':
                    jobs.append(job)
        jobs.sort(key=lambda item: item.get('updated_at') or item.get('created_at') or '', reverse=True)
        return jsonify({'job': jobs[0] if jobs else None})

    @app.route('/api/android-analysis/jobs/<job_id>', methods=['GET'])
    @optional_token
    def api_android_analysis_job(job_id):
        if not _android_analysis_enabled():
            return _android_analysis_disabled_response()
        user_id = request.args.get('user_id', '').strip()
        session_id = request.args.get('session_id', '').strip()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        _, err = _session_for_user(client_ip, user_id, session_id)
        if err:
            return err
        store = AndroidAnalysisJobStore(sm.get_session_dir(client_ip, user_id, session_id))
        try:
            return jsonify({'job': store.load_job(job_id)})
        except OSError:
            return jsonify({'error': 'Android analysis job not found', 'code': 'android_analysis_job_not_found'}), 404

    @app.route('/api/android-analysis/jobs/<job_id>/events', methods=['GET'])
    @optional_token
    def api_android_analysis_job_events(job_id):
        if not _android_analysis_enabled():
            return _android_analysis_disabled_response()
        user_id = request.args.get('user_id', '').strip()
        session_id = request.args.get('session_id', '').strip()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        _, err = _session_for_user(client_ip, user_id, session_id)
        if err:
            return err
        store = AndroidAnalysisJobStore(sm.get_session_dir(client_ip, user_id, session_id))
        events_path = store.job_dir(job_id) / 'events.jsonl'
        if not events_path.is_file():
            return jsonify({'error': 'Android analysis job not found', 'code': 'android_analysis_job_not_found'}), 404
        return jsonify({'events': store.read_events(job_id)})

    @app.route('/api/android-analysis/jobs/<job_id>/process-details', methods=['GET'])
    @optional_token
    def api_android_analysis_job_process_details(job_id):
        if not _android_analysis_enabled():
            return _android_analysis_disabled_response()
        user_id = request.args.get('user_id', '').strip()
        session_id = request.args.get('session_id', '').strip()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        _, err = _session_for_user(client_ip, user_id, session_id)
        if err:
            return err
        store = AndroidAnalysisJobStore(sm.get_session_dir(client_ip, user_id, session_id))
        if not (store.job_dir(job_id) / 'job.json').is_file():
            return jsonify({'error': 'Android analysis job not found', 'code': 'android_analysis_job_not_found'}), 404
        return jsonify({'details': _android_process_details(store, job_id)})

    @app.route('/api/android-analysis/jobs/<job_id>/events/stream', methods=['GET'])
    @optional_token
    def api_android_analysis_job_events_stream(job_id):
        if not _android_analysis_enabled():
            return _android_analysis_disabled_response()
        user_id = request.args.get('user_id', '').strip()
        session_id = request.args.get('session_id', '').strip()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        _, err = _session_for_user(client_ip, user_id, session_id)
        if err:
            return err
        store = AndroidAnalysisJobStore(sm.get_session_dir(client_ip, user_id, session_id))
        if not store.job_dir(job_id).is_dir():
            return jsonify({'error': 'Android analysis job not found', 'code': 'android_analysis_job_not_found'}), 404

        terminal = {'report_ready', 'verified', 'needs_review', 'case_draft_ready', 'case_confirmed', 'error'}

        @stream_with_context
        def generate():
            last = 0
            started = time.monotonic()
            while time.monotonic() - started < 3600:
                events = store.read_events(job_id)
                for event in events[last:]:
                    yield 'data: ' + json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n\n'
                last = len(events)
                try:
                    job = store.load_job(job_id)
                except OSError:
                    yield 'event: done\ndata: {"status":"missing"}\n\n'
                    break
                if job.get('status') in terminal:
                    yield 'event: done\ndata: ' + json.dumps({'status': job.get('status')}, ensure_ascii=False) + '\n\n'
                    break
                time.sleep(0.4)

        return Response(generate(), mimetype='text/event-stream', headers={'Cache-Control': 'no-store'})

    @app.route('/api/android-analysis/jobs/<job_id>/artifacts/<artifact_name>', methods=['GET'])
    @optional_token
    def api_android_analysis_job_artifact(job_id, artifact_name):
        if not _android_analysis_enabled():
            return _android_analysis_disabled_response()
        user_id = request.args.get('user_id', '').strip()
        session_id = request.args.get('session_id', '').strip()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        _, err = _session_for_user(client_ip, user_id, session_id)
        if err:
            return err
        if '/' in artifact_name or '\\' in artifact_name or Path(artifact_name).name != artifact_name:
            return jsonify({'error': 'Invalid artifact name', 'code': 'invalid_artifact_name'}), 400
        store = AndroidAnalysisJobStore(sm.get_session_dir(client_ip, user_id, session_id))
        try:
            job = store.load_job(job_id)
        except OSError:
            return jsonify({'error': 'Android analysis job not found', 'code': 'android_analysis_job_not_found'}), 404
        artifacts = job.get('artifacts') or {}
        rel = artifacts.get(artifact_name)
        if not rel:
            for value in artifacts.values():
                if Path(str(value)).name == artifact_name:
                    rel = value
                    break
        if not rel or not str(rel).replace('\\', '/').startswith('artifacts/'):
            return jsonify({'error': 'Artifact not found', 'code': 'android_analysis_artifact_not_found'}), 404
        filename = Path(str(rel)).name
        artifact_path = store.artifacts_dir(job_id) / filename
        if not artifact_path.is_file():
            return jsonify({'error': 'Artifact not found', 'code': 'android_analysis_artifact_not_found'}), 404
        return send_from_directory(
            store.artifacts_dir(job_id),
            filename,
            as_attachment=request.args.get('download') == '1',
        )

    @app.route('/api/android-analysis/jobs/<job_id>/deep', methods=['POST'])
    @optional_token
    def api_android_analysis_job_deep(job_id):
        if not _android_analysis_enabled():
            return _android_analysis_disabled_response()
        data = request.json or {}
        user_id = (data.get('user_id') or '').strip()
        session_id = (data.get('session_id') or '').strip()
        preferred_paths = data.get('preferred_paths') if isinstance(data.get('preferred_paths'), dict) else {}
        store, _, err = _android_store_for_request(user_id, session_id)
        if err:
            return err
        try:
            job = store.load_job(job_id)
        except OSError:
            return jsonify({'error': 'Android analysis job not found', 'code': 'android_analysis_job_not_found'}), 404
        trigger_message = (data.get('trigger_message') or '').strip()
        trigger = (data.get('trigger') or 'manual').strip() or 'manual'
        if job.get('deep_status') == 'running' or str(job.get('status') or '').startswith('deep_'):
            return jsonify({'job': job, 'background': True})
        enable_ai = not app.config.get('TESTING', False)
        if bool(data.get('background')):
            job = store.update_job(job_id, status='deep_queued', deep_status='queued', deep_trigger=trigger)
            thread = threading.Thread(
                target=_run_android_deep_pipeline,
                kwargs={
                    'store': AndroidAnalysisJobStore(store.session_dir),
                    'job_id': job_id,
                    'client_ip': get_client_ip(request, config.TRUST_X_FORWARDED),
                    'user_id': user_id,
                    'session_id': session_id,
                    'preferred_paths': preferred_paths,
                    'enable_ai': enable_ai,
                    'trigger': trigger,
                    'trigger_message': trigger_message,
                },
                daemon=True,
            )
            thread.start()
            return jsonify({'job': job, 'background': True})

        result = _run_android_deep_pipeline(
            store,
            job_id,
            client_ip=get_client_ip(request, config.TRUST_X_FORWARDED),
            user_id=user_id,
            session_id=session_id,
            preferred_paths=preferred_paths,
            enable_ai=enable_ai,
            trigger=trigger,
            trigger_message=trigger_message,
        )
        status_code = 400 if result.get('job', {}).get('status') == 'error' else 200
        return jsonify(result), status_code

    @app.route('/api/android-analysis/jobs/<job_id>/case-draft', methods=['POST'])
    @optional_token
    def api_android_analysis_job_case_draft(job_id):
        if not _android_analysis_enabled():
            return _android_analysis_disabled_response()
        data = request.json or {}
        user_id = (data.get('user_id') or '').strip()
        session_id = (data.get('session_id') or '').strip()
        store, _, err = _android_store_for_request(user_id, session_id)
        if err:
            return err
        try:
            job = store.load_job(job_id)
        except OSError:
            return jsonify({'error': 'Android analysis job not found', 'code': 'android_analysis_job_not_found'}), 404
        try:
            draft = generate_case_draft(store.artifacts_dir(job_id), source_job_id=job_id)
            store.append_event(
                job_id,
                'case_draft_generated',
                {
                    'draft_id': draft.get('id'),
                    'source_bundle_ids': draft.get('source_bundle_ids') or [],
                },
            )
            artifacts = _merge_android_artifacts(
                job,
                {
                    'case_draft': 'artifacts/case_draft.json',
                    'rule_candidates': 'artifacts/rule_candidates.json',
                },
            )
            job = store.update_job(job_id, status='case_draft_ready', artifacts=artifacts, error=None)
            return jsonify({'job': job, 'draft': draft})
        except AndroidAnalysisError as e:
            log.warning('[AndroidAnalysis] case draft job=%s failed: %s %s', job_id, e.code, e.message)
            job = store.update_job(job_id, status='error', error={'code': e.code, 'message': e.message})
            return jsonify({'job': job}), 400

    @app.route('/api/android-analysis/jobs/<job_id>/case-draft/confirm', methods=['POST'])
    @optional_token
    def api_android_analysis_job_case_draft_confirm(job_id):
        if not _android_analysis_enabled():
            return _android_analysis_disabled_response()
        data = request.json or {}
        user_id = (data.get('user_id') or '').strip()
        session_id = (data.get('session_id') or '').strip()
        bundle_id = (data.get('bundle_id') or '').strip()
        reviewer_note = (data.get('reviewer_note') or '').strip()
        store, _, err = _android_store_for_request(user_id, session_id)
        if err:
            return err
        try:
            job = store.load_job(job_id)
        except OSError:
            return jsonify({'error': 'Android analysis job not found', 'code': 'android_analysis_job_not_found'}), 404
        if not bundle_id:
            bundle_id = next(iter(job.get('bundle_ids') or []), '')
        try:
            confirmed = confirm_case_draft(
                config.ANDROID_ANALYSIS_KNOWLEDGE_DIR,
                store.artifacts_dir(job_id),
                bundle_id=bundle_id,
                reviewer_note=reviewer_note,
            )
            store.append_event(
                job_id,
                'case_draft_confirmed',
                {
                    'case_id': confirmed.get('case_id'),
                    'bundle_id': confirmed.get('bundle_id'),
                },
            )
            job = store.update_job(job_id, status='case_confirmed', error=None)
            return jsonify({'job': job, 'confirmed': confirmed})
        except AndroidAnalysisError as e:
            log.warning('[AndroidAnalysis] case confirm job=%s failed: %s %s', job_id, e.code, e.message)
            job = store.update_job(job_id, status='error', error={'code': e.code, 'message': e.message})
            return jsonify({'job': job}), 400

    @app.route('/api/dev/projects', methods=['GET'])
    @optional_token
    def api_dev_projects():
        if not _dev_enabled():
            return _dev_disabled_response()
        try:
            projects = [project_public_info(p) for p in _dev_projects()]
        except DevProjectError as e:
            return jsonify({'error': str(e), 'code': 'dev_projects_config_error'}), 400
        return jsonify({'projects': projects})

    @app.route('/api/dev/sessions/<session_id>/attach-project', methods=['POST'])
    @optional_token
    def api_dev_attach_project(session_id):
        if not _dev_enabled():
            return _dev_disabled_response()
        data = request.json or {}
        user_id = (data.get('user_id') or '').strip()
        project_id = (data.get('project_id') or '').strip()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        _, err = _session_for_user(client_ip, user_id, session_id)
        if err:
            return err
        if not project_id:
            return jsonify({'error': 'project_id required'}), 400
        try:
            project = find_project(_dev_projects(), project_id)
        except DevProjectError as e:
            return jsonify({'error': str(e), 'code': 'dev_projects_config_error'}), 400
        if not project:
            return jsonify({'error': 'Project not found in whitelist'}), 404
        meta = save_dev_session(sm.get_session_dir(client_ip, user_id, session_id), project)
        return jsonify({'ok': True, 'session': meta, 'project': project_public_info(project)})

    @app.route('/api/dev/sessions/<session_id>/detach-project', methods=['POST'])
    @optional_token
    def api_dev_detach_project(session_id):
        if not _dev_enabled():
            return _dev_disabled_response()
        data = request.json or {}
        user_id = (data.get('user_id') or '').strip()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        _, err = _session_for_user(client_ip, user_id, session_id)
        if err:
            return err
        cleared = clear_dev_session(sm.get_session_dir(client_ip, user_id, session_id))
        return jsonify({'ok': True, 'cleared': cleared})

    @app.route('/api/dev/sessions/<session_id>/status', methods=['GET'])
    @optional_token
    def api_dev_status(session_id):
        if not _dev_enabled():
            return _dev_disabled_response()
        user_id = request.args.get('user_id', '').strip()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        _, err = _session_for_user(client_ip, user_id, session_id)
        if err:
            return err
        meta, project = _attached_dev_project(client_ip, user_id, session_id)
        if not meta:
            return jsonify({'mode': 'chat', 'attached': False})
        if not project:
            return jsonify({'mode': 'development', 'attached': False, 'error': 'Project no longer exists in whitelist'})
        return jsonify({'mode': 'development', 'attached': True, 'session': meta, 'project': project_public_info(project)})

    @app.route('/api/dev/sessions/<session_id>/diff', methods=['GET'])
    @optional_token
    def api_dev_diff(session_id):
        if not _dev_enabled():
            return _dev_disabled_response()
        user_id = request.args.get('user_id', '').strip()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        _, err = _session_for_user(client_ip, user_id, session_id)
        if err:
            return err
        meta, project = _attached_dev_project(client_ip, user_id, session_id)
        if not meta or not project:
            return jsonify({'error': 'No development project attached'}), 400
        return jsonify(diff_for_project(Path(project['path'])))

    @app.route('/api/dev/sessions/<session_id>/run-test', methods=['POST'])
    @optional_token
    def api_dev_run_test(session_id):
        if not _dev_enabled():
            return _dev_disabled_response()
        data = request.json or {}
        user_id = (data.get('user_id') or '').strip()
        command = (data.get('command') or '').strip()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        _, err = _session_for_user(client_ip, user_id, session_id)
        if err:
            return err
        meta, project = _attached_dev_project(client_ip, user_id, session_id)
        if not meta or not project:
            return jsonify({'error': 'No development project attached'}), 400
        try:
            result = run_project_test(project, command, config.DEV_TEST_TIMEOUT_SECONDS)
        except DevProjectError as e:
            return jsonify({'error': str(e)}), 400
        return jsonify(result)

    @app.route('/api/dev/sessions/<session_id>/stop', methods=['POST'])
    @optional_token
    def api_dev_stop(session_id):
        if not _dev_enabled():
            return _dev_disabled_response()
        data = request.json or {}
        user_id = (data.get('user_id') or '').strip()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        _, err = _session_for_user(client_ip, user_id, session_id)
        if err:
            return err
        return jsonify({'ok': True, 'stopped': stop_session_process(session_id) or stop_gemini_session_process(session_id)})

    @app.route('/chat/stop', methods=['POST'])
    @optional_token
    def chat_stop():
        data = request.json or {}
        user_id = (data.get('user_id') or '').strip()
        session_id = (data.get('session_id') or '').strip()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        if not user_id or not session_id:
            return jsonify({'error': 'user_id and session_id required'}), 400
        _, err = _session_for_user(client_ip, user_id, session_id)
        if err:
            return err
        stopped = stop_session_process(session_id) or stop_gemini_session_process(session_id)
        return jsonify({'ok': True, 'stopped': stopped})

    @app.route('/api/user/claude-credentials', methods=['GET'])
    @optional_token
    def api_get_claude_credentials():
        user_id = request.args.get('user_id', '').strip()
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        data = load_credentials(sm, client_ip, user_id)
        return jsonify(public_status(data))

    @app.route('/api/user/claude-credentials', methods=['PUT'])
    @optional_token
    def api_put_claude_credentials():
        data = request.json or {}
        user_id = data.get('user_id', '').strip()
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        env_in = data.get('env')
        model_in = data.get('model', '')
        if not isinstance(model_in, str):
            model_in = ''
        env_new, err = sanitize_env(env_in)
        if err:
            return jsonify({'error': err}), 400
        existing = load_credentials(sm, client_ip, user_id)
        merged = merge_env_preserve_existing(existing, env_new or {})
        verr = validate_save_payload(merged, model_in)
        if verr:
            return jsonify({'error': verr}), 400
        save_credentials(sm, client_ip, user_id, merged, model_in)
        return jsonify({'ok': True})

    @app.route('/api/user/claude-credentials', methods=['DELETE'])
    @optional_token
    def api_delete_claude_credentials():
        user_id = request.args.get('user_id', '').strip()
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        delete_credentials(sm, client_ip, user_id)
        return jsonify({'ok': True})

    @app.route('/chat', methods=['POST'])
    @optional_token
    def chat():
        data = request.json or {}
        message = data.get('message', '').strip()
        user_id = data.get('user_id', '').strip()
        session_id = data.get('session_id', '').strip()
        web_search_enabled = bool(data.get('web_search'))
        retry_interrupted = bool(data.get('retry_interrupted'))
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)

        if not message:
            return jsonify({'error': 'Message required'}), 400
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400
        if not session_id:
            return jsonify({'error': 'session_id required'}), 400

        session = sm.get_session(client_ip, user_id, session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        provider = normalize_provider(session.get('provider'))
        if provider == 'gemini' and not config.FEATURE_GEMINI_SUPPORT:
            return jsonify({'error': 'Gemini support disabled', 'code': 'gemini_disabled'}), 400

        dev_meta, dev_project = _attached_dev_project(client_ip, user_id, session_id)
        if dev_meta and not dev_project:
            return jsonify({'error': 'Development project no longer exists in whitelist', 'code': 'dev_project_missing'}), 400
        if provider == 'gemini' and dev_meta:
            return jsonify({'error': 'Gemini 第一版暂不支持开发模式', 'code': 'gemini_dev_not_supported'}), 400

        rt = {}
        if provider == 'claude':
            rt = resolve_claude_runtime_for_request(request, sm, client_ip, user_id)
            if rt.get('error'):
                return jsonify({'error': rt['error'], 'code': 'v2_claude_config_required'}), 400
            if rt.get('use_per_user'):
                log.info('[Chat] V2 使用每用户 API 环境（Host 非本机）')
        if web_search_enabled and not config.TAVILY_API_KEY:
            return jsonify({'error': 'Tavily API key 未配置', 'code': 'tavily_config_required'}), 400

        provider_sid = sm.get_provider_session_id(client_ip, user_id, session_id, provider)
        prior_messages = sm.get_messages(client_ip, user_id, session_id)
        history_for_prompt = prior_messages
        removed_interrupted = None
        if retry_interrupted:
            removed_interrupted = sm.remove_last_assistant_message(
                client_ip, user_id, session_id, interrupted_only=True,
            )
            prior_messages = sm.get_messages(client_ip, user_id, session_id)
            history_for_prompt = prior_messages
            if history_for_prompt:
                last_msg = history_for_prompt[-1]
                if (
                    isinstance(last_msg, dict)
                    and last_msg.get('role') == 'user'
                    and str(last_msg.get('content') or '').strip() == message
                ):
                    history_for_prompt = history_for_prompt[:-1]
            # 重试中断回复时避免直接恢复上游 CLI 里可能已经包含的半截 assistant 内容，
            # 由 Web 侧 messages.json 的干净历史 + 当前用户问题重新驱动一次。
            provider_sid = None
            log.info(
                '[Chat] retry interrupted session=%s removed=%s',
                session_id,
                bool(removed_interrupted),
            )
        if provider == 'claude' and not provider_sid and not retry_interrupted:
            if any(m.get('role') == 'assistant' for m in prior_messages):
                provider_sid = session_id
                sm.update_provider_session_id(client_ip, user_id, session_id, provider, provider_sid)
                log.info(f'[Chat] 补全 claude_session_id 用于 --resume: {provider_sid}')

        log.info(
            '[Chat] user=%s, session=%s, provider=%s, provider_sid=%s, msg_len=%s',
            user_id, session_id, provider, provider_sid, len(message),
        )

        uploaded_files = data.get('files', [])
        if not retry_interrupted:
            sm.add_message(client_ip, user_id, session_id, 'user', message,
                           files=uploaded_files if uploaded_files else None)

        upload_dir = sm.get_upload_dir(client_ip, user_id, session_id)
        session_workspace = sm.get_session_dir(client_ip, user_id, session_id)
        session_workspace.mkdir(parents=True, exist_ok=True)
        sm.sync_user_global_memory_to_session(client_ip, user_id, session_id)
        if provider == 'gemini':
            _apply_explicit_user_memory(message, session_workspace)
        uploaded_files = data.get('files', []) or []
        file_paths = resolve_session_upload_paths(upload_dir, uploaded_files)
        if file_paths:
            log.info(f'[Chat] 附件文件（服务端解析）: {file_paths}')

        skill_bundles, selected_bundles = _select_skill_bundles(message, prior_messages)
        selected_bundle_ids = [str(b.get('id')) for b in selected_bundles if b.get('id')]
        readonly_dirs = _readonly() + _bundle_paths(selected_bundles)
        if selected_bundle_ids:
            log.info('[Chat] 本轮按需挂载技能包: %s', selected_bundle_ids)

        dev_context = None
        dev_cli_cwd = None
        dev_permission_mode = None
        dev_dangerous_skip = None
        if dev_project:
            project_git = git_status(Path(dev_project['path']))
            dev_cli_cwd = dev_project['path']
            dev_permission_mode = config.DEV_PERMISSION_MODE
            dev_dangerous_skip = config.DEV_DANGEROUSLY_SKIP_PERMISSIONS
            dev_context = {
                'project_id': dev_project['id'],
                'project_name': dev_project.get('name') or dev_project['id'],
                'project_path': dev_project['path'],
                'session_cache_dir': str(session_workspace),
                'git': project_git,
                'default_tests': dev_project.get('default_tests') or [],
            }
            log.info('[Chat] 开发模式 session=%s project=%s cwd=%s', session_id, dev_project['id'], dev_project['path'])

        collected_text = []
        collected_thinking = []
        new_claude_sid = [None]
        done_seen = [False]
        cli_ctx = {
            'user_id': user_id,
            'session_id': session_id,
            'log_dir': config.LOG_DIR,
        }

        def _forward_stream(stream_gen):
            for event_str in stream_gen:
                if event_str.startswith('data: '):
                    try:
                        evt = json.loads(event_str[6:].strip())
                        t = evt.get('type')
                        if t == 'text':
                            collected_text.append(evt.get('content', ''))
                        elif t == 'thinking':
                            collected_thinking.append(evt.get('content', ''))
                        elif t == 'done':
                            done_seen[0] = True
                        elif t == 'session':
                            sid = evt.get('session_id')
                            new_claude_sid[0] = sid
                            if sid:
                                sm.update_provider_session_id(client_ip, user_id, session_id, provider, sid)
                                log.info('[Chat] 保存 %s session_id=%s（流内）', provider, sid)
                    except json.JSONDecodeError:
                        pass
                yield event_str

        finish_saved = [False]

        def on_finish():
            if finish_saved[0]:
                return
            finish_saved[0] = True
            full_text = ''.join(collected_text)
            full_thinking = ''.join(collected_thinking) if collected_thinking else None
            if full_text or full_thinking:
                metadata = None
                if not done_seen[0]:
                    metadata = {
                        'interrupted': True,
                        'retry_message': message,
                        'retry_files': uploaded_files if uploaded_files else [],
                        'retry_web_search': bool(web_search_enabled),
                    }
                sm.add_message(
                    client_ip,
                    user_id,
                    session_id,
                    'assistant',
                    full_text,
                    thinking=full_thinking,
                    metadata=metadata,
                )
                log.info(
                    '[Chat] 保存助手回复 session=%s chars=%s thinking_chars=%s interrupted=%s',
                    session_id,
                    len(full_text),
                    len(full_thinking or ''),
                    not done_seen[0],
                )
            if new_claude_sid[0]:
                sm.update_provider_session_id(client_ip, user_id, session_id, provider, new_claude_sid[0])
                log.info('[Chat] 保存 %s session_id=%s（收尾）', provider, new_claude_sid[0])
            sm.sync_session_global_memory_to_user(client_ip, user_id, session_id)

        def generate():
            try:
                web_search_context = ''
                if web_search_enabled:
                    yield 'data: ' + json.dumps({'type': 'info', 'message': '正在使用 Tavily 联网搜索…'}, ensure_ascii=False) + '\n\n'
                    try:
                        tavily_data = search_tavily(
                            api_key=config.TAVILY_API_KEY,
                            query=message,
                            max_results=config.TAVILY_MAX_RESULTS,
                            search_depth=config.TAVILY_SEARCH_DEPTH,
                        )
                        web_search_context = format_tavily_for_prompt(tavily_data, message)
                        yield 'data: ' + json.dumps({'type': 'info', 'message': f'联网搜索完成，正在交给 {_provider_label(provider)} 整理…'}, ensure_ascii=False) + '\n\n'
                        log.info('[Chat] Tavily 搜索完成 user=%s session=%s', user_id, session_id)
                    except TavilySearchError as e:
                        msg = f'联网搜索失败：{e}'
                        yield 'data: ' + json.dumps({'type': 'error', 'message': msg, 'soft': True}, ensure_ascii=False) + '\n\n'
                        web_search_context = (
                            '【联网搜索资料 — Tavily】\n'
                            f'用户请求了联网搜索，但 Tavily 搜索失败：{e}\n'
                            '请明确告知用户联网搜索未成功，不要编造最新信息。\n\n'
                        )

                if dev_project:
                    yield 'data: ' + json.dumps(
                        {
                            'type': 'info',
                            'message': f'开发模式：已连接项目 {dev_project.get("name") or dev_project["id"]}，AI 将在该项目真实目录中工作。',
                        },
                        ensure_ascii=False,
                    ) + '\n\n'

                log.info(
                    '[Chat] 外环编排 max_rounds=%s',
                    config.CLAUDE_WEB_ORCH_MAX_ROUNDS,
                )
                yield from _forward_stream(
                    orchestrator.stream_orchestrated_turns(
                        first_message=message,
                        file_paths=file_paths,
                        session_id=session_id,
                        initial_claude_session_id=provider_sid,
                        max_rounds=config.CLAUDE_WEB_ORCH_MAX_ROUNDS,
                        upload_dir=str(upload_dir),
                        session_workspace_dir=str(session_workspace.resolve()),
                        readonly_dirs=readonly_dirs,
                        readonly_dirs_notes=_readonly_notes(),
                        skill_bundles=skill_bundles,
                        cli_log_context=cli_ctx,
                        conversation_history=history_for_prompt,
                        mounted_bundle_ids=selected_bundle_ids,
                        web_search_context=web_search_context,
                        cli_cwd_dir=dev_cli_cwd,
                        permission_mode_override=dev_permission_mode,
                        dangerously_skip_permissions_override=dev_dangerous_skip,
                        development_context=dev_context,
                        stream_output_func=_provider_runner(provider),
                        **_v2_orch_kwargs(rt),
                    )
                )
            finally:
                on_finish()

        response = Response(
            stream_with_context(generate()),
            content_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
                'Connection': 'keep-alive',
            }
        )
        return response

    @app.route('/chat/orchestration/continue', methods=['POST'])
    @optional_token
    def orchestration_continue():
        """暂停后继续外环编排，或单轮「结束并总结」。需有效的 continuation_token。"""
        data = request.json or {}
        user_id = data.get('user_id', '').strip()
        session_id = data.get('session_id', '').strip()
        token = (data.get('continuation_token') or '').strip()
        action = (data.get('action') or 'continue').strip().lower()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)

        if not user_id or not session_id:
            return jsonify({'error': 'user_id and session_id required'}), 400
        if not token:
            return jsonify({'error': 'continuation_token required'}), 400

        sess = sm.get_session(client_ip, user_id, session_id)
        if not sess:
            return jsonify({'error': 'Session not found'}), 404
        provider = normalize_provider(sess.get('provider'))
        if provider == 'gemini' and not config.FEATURE_GEMINI_SUPPORT:
            return jsonify({'error': 'Gemini support disabled', 'code': 'gemini_disabled'}), 400

        rt = {}
        if provider == 'claude':
            rt = resolve_claude_runtime_for_request(request, sm, client_ip, user_id)
            if rt.get('error'):
                return jsonify({'error': rt['error'], 'code': 'v2_claude_config_required'}), 400
            if rt.get('use_per_user'):
                log.info('[Orchestration/continue] V2 使用每用户 API 环境')

        session_workspace = sm.get_session_dir(client_ip, user_id, session_id)
        state = orchestrator.read_pause_state(session_workspace)
        if not orchestrator.validate_continuation_token(state, token):
            return jsonify({'error': '无效或已过期的 continuation_token'}), 400

        orchestrator.clear_pause_state(session_workspace)

        upload_dir = sm.get_upload_dir(client_ip, user_id, session_id)
        session_workspace.mkdir(parents=True, exist_ok=True)
        sm.sync_user_global_memory_to_session(client_ip, user_id, session_id)

        provider_sid = (state or {}).get('claude_session_id') or sm.get_provider_session_id(client_ip, user_id, session_id, provider) or session_id
        mounted_ids = (state or {}).get('mounted_bundle_ids') or []
        skill_bundles, selected_bundles = _select_skill_bundles('', [], bundle_ids=mounted_ids)
        readonly_dirs = _readonly() + _bundle_paths(selected_bundles)
        try:
            total_offset = int((state or {}).get('total_rounds_all_segments') or 0)
        except (TypeError, ValueError):
            total_offset = 0

        dev_meta, dev_project = _attached_dev_project(client_ip, user_id, session_id)
        if provider == 'gemini' and dev_meta:
            return jsonify({'error': 'Gemini 第一版暂不支持开发模式', 'code': 'gemini_dev_not_supported'}), 400
        dev_context = None
        dev_cli_cwd = None
        dev_permission_mode = None
        dev_dangerous_skip = None
        if dev_project:
            project_git = git_status(Path(dev_project['path']))
            dev_cli_cwd = dev_project['path']
            dev_permission_mode = config.DEV_PERMISSION_MODE
            dev_dangerous_skip = config.DEV_DANGEROUSLY_SKIP_PERMISSIONS
            dev_context = {
                'project_id': dev_project['id'],
                'project_name': dev_project.get('name') or dev_project['id'],
                'project_path': dev_project['path'],
                'session_cache_dir': str(session_workspace),
                'git': project_git,
                'default_tests': dev_project.get('default_tests') or [],
            }

        log.info(
            '[Orchestration/continue] user=%s session=%s action=%s offset=%s',
            user_id, session_id, action, total_offset,
        )

        collected_text = []
        collected_thinking = []
        new_claude_sid = [None]

        cli_ctx = {
            'user_id': user_id,
            'session_id': session_id,
            'log_dir': config.LOG_DIR,
        }

        def _forward_stream(stream_gen):
            for event_str in stream_gen:
                if event_str.startswith('data: '):
                    try:
                        evt = json.loads(event_str[6:].strip())
                        t = evt.get('type')
                        if t == 'text':
                            collected_text.append(evt.get('content', ''))
                        elif t == 'thinking':
                            collected_thinking.append(evt.get('content', ''))
                        elif t == 'session':
                            sid = evt.get('session_id')
                            new_claude_sid[0] = sid
                            if sid:
                                sm.update_provider_session_id(client_ip, user_id, session_id, provider, sid)
                                log.info('[Orchestration/continue] 保存 %s session_id=%s（流内）', provider, sid)
                    except json.JSONDecodeError:
                        pass
                yield event_str

        finish_saved = [False]

        def on_finish():
            if finish_saved[0]:
                return
            finish_saved[0] = True
            full_text = ''.join(collected_text)
            full_thinking = ''.join(collected_thinking) if collected_thinking else None
            if full_text or full_thinking:
                sm.add_message(
                    client_ip, user_id, session_id, 'assistant', full_text, thinking=full_thinking,
                )
                log.info(
                    '[Orchestration/continue] 保存助手回复 session=%s chars=%s thinking_chars=%s',
                    session_id,
                    len(full_text),
                    len(full_thinking or ''),
                )
            if new_claude_sid[0]:
                sm.update_provider_session_id(client_ip, user_id, session_id, provider, new_claude_sid[0])
                log.info('[Orchestration/continue] 保存 %s session_id=%s（收尾）', provider, new_claude_sid[0])
            sm.sync_session_global_memory_to_user(client_ip, user_id, session_id)

        def generate():
            try:
                if action == 'summarize':
                    yield from _forward_stream(
                        orchestrator.stream_summarize_only(
                            message=orchestrator.build_summarize_after_pause_prompt(),
                            session_id=session_id,
                            claude_session_id=provider_sid,
                            upload_dir=str(upload_dir),
                            session_workspace_dir=str(session_workspace.resolve()),
                            readonly_dirs=readonly_dirs,
                            readonly_dirs_notes=_readonly_notes(),
                            skill_bundles=skill_bundles,
                            cli_log_context=cli_ctx,
                            cli_cwd_dir=dev_cli_cwd,
                            permission_mode_override=dev_permission_mode,
                            dangerously_skip_permissions_override=dev_dangerous_skip,
                            development_context=dev_context,
                            stream_output_func=_provider_runner(provider),
                            **_v2_orch_kwargs(rt),
                        )
                    )
                else:
                    yield from _forward_stream(
                        orchestrator.stream_orchestrated_turns(
                            first_message=orchestrator.build_continue_segment_prompt(),
                            file_paths=None,
                            session_id=session_id,
                            initial_claude_session_id=provider_sid,
                            max_rounds=config.CLAUDE_WEB_ORCH_MAX_ROUNDS,
                            upload_dir=str(upload_dir),
                            session_workspace_dir=str(session_workspace.resolve()),
                            readonly_dirs=readonly_dirs,
                            readonly_dirs_notes=_readonly_notes(),
                            skill_bundles=skill_bundles,
                            cli_log_context=cli_ctx,
                            total_rounds_offset=total_offset,
                            mounted_bundle_ids=mounted_ids,
                            cli_cwd_dir=dev_cli_cwd,
                            permission_mode_override=dev_permission_mode,
                            dangerously_skip_permissions_override=dev_dangerous_skip,
                            development_context=dev_context,
                            stream_output_func=_provider_runner(provider),
                            **_v2_orch_kwargs(rt),
                        )
                    )
            finally:
                on_finish()

        response = Response(
            stream_with_context(generate()),
            content_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
                'Connection': 'keep-alive',
            },
        )
        return response

    @app.route('/sessions', methods=['GET'])
    @optional_token
    def get_sessions():
        user_id = request.args.get('user_id', '').strip()
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        sessions = sm.list_sessions(client_ip, user_id)
        return jsonify(sessions)

    @app.route('/sessions', methods=['POST'])
    @optional_token
    def create_session():
        data = request.json or {}
        user_id = data.get('user_id', '').strip()
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400
        raw_provider = (data.get('provider') or 'claude').strip().lower()
        if raw_provider not in SUPPORTED_PROVIDERS:
            return jsonify({'error': 'Unsupported provider', 'code': 'unsupported_provider'}), 400
        provider = normalize_provider(raw_provider)
        if provider == 'gemini' and not config.FEATURE_GEMINI_SUPPORT:
            return jsonify({'error': 'Gemini support disabled', 'code': 'gemini_disabled'}), 400
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        session = sm.create_session(client_ip, user_id, provider=provider)
        return jsonify(session)

    @app.route('/sessions/<session_id>', methods=['DELETE'])
    @optional_token
    def delete_session(session_id):
        user_id = request.args.get('user_id', '').strip()
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        backed = None
        backup_error = None
        try:
            backed = backup_session_before_delete(
                config.BACKUPS_DIR, config.CACHE_DIR, config.LOG_DIR,
                client_ip, user_id, session_id,
            )
        except Exception as exc:
            backup_error = str(exc)
            logging.warning(
                '[Session] Failed to backup before delete: user_id=%s session_id=%s error=%s',
                user_id,
                session_id,
                exc,
            )
        sm.delete_session(client_ip, user_id, session_id)
        return jsonify({'ok': True})

    @app.route('/sessions/<session_id>/messages', methods=['GET'])
    @optional_token
    def get_messages(session_id):
        user_id = request.args.get('user_id', '').strip()
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        messages = sm.get_messages(client_ip, user_id, session_id)
        return jsonify(messages)

    @app.route('/upload', methods=['POST'])
    @optional_token
    def upload_file():
        user_id = request.form.get('user_id', '').strip()
        session_id = request.form.get('session_id', '').strip()
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)

        if not user_id or not session_id:
            return jsonify({'error': 'user_id and session_id required'}), 400

        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400

        file = request.files['file']
        raw = (file.filename or '').strip()
        if not raw:
            raw = ''

        file.seek(0, 2)
        size = file.tell()
        file.seek(0)
        if size > config.UPLOAD_MAX_SIZE:
            return jsonify({'error': f'File too large (max {config.UPLOAD_MAX_SIZE // 1024 // 1024}MB)'}), 400

        upload_dir = sm.get_upload_dir(client_ip, user_id, session_id)
        canonical = safe_client_filename(raw or '')
        disk_name = canonical
        if not is_ascii_filename(canonical):
            disk_name = ascii_storage_filename(canonical)

        target = upload_dir / disk_name
        if target.exists():
            stem = target.stem
            suffix = target.suffix
            counter = 1
            while target.exists():
                target = upload_dir / f'{stem}_{counter}{suffix}'
                counter += 1

        file.save(str(target))

        log.info(f'[Upload] 文件已保存: {target} ({size} bytes), user={user_id}, session={session_id}')

        return jsonify({
            'name': target.name,
            'size': size,
            'path': str(target),
            'display_name': canonical,
        })

    @app.route('/sessions/<session_id>/files', methods=['GET'])
    @optional_token
    def list_session_files(session_id):
        user_id = request.args.get('user_id', '').strip()
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        files = sm.list_uploads(client_ip, user_id, session_id)
        return jsonify(files)

    @app.route('/feedback', methods=['POST'])
    @optional_token
    def feedback():
        client_ip = get_client_ip(request, config.TRUST_X_FORWARDED)
        text = (request.form.get('text') or '').strip()
        contact = (request.form.get('contact') or '').strip()
        user_id = (request.form.get('user_id') or '').strip()
        if not text:
            return jsonify({'error': 'text required'}), 400
        images = request.files.getlist('images')
        dest = save_feedback_package(
            config.FEEDBACK_DIR, client_ip, user_id, text, contact, images,
        )
        log.info(f'[Feedback] 已保存: {dest}')
        try:
            rel = str(dest.relative_to(config.ROOT))
        except ValueError:
            rel = str(dest)
        return jsonify({'ok': True, 'saved_to': rel})

    @app.errorhandler(500)
    def internal_error(e):
        log.error(f'Internal error: {e}')
        return jsonify({'error': 'Internal server error'}), 500

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({'error': 'Not found'}), 404
