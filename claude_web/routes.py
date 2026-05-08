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
from .android_analysis.deep_analysis import build_deep_evidence_pack, generate_deep_report
from .android_analysis.evidence import generate_first_evidence_pack
from .android_analysis.jobs import AndroidAnalysisJobStore
from .android_analysis.knowledge_store import list_bundles as list_android_analysis_bundles
from .android_analysis.models import AndroidAnalysisError
from .android_analysis.planner import run_planner
from .android_analysis.profiler import profile_extracted_tree
from .android_analysis.reporter import generate_first_report
from .android_analysis.rule_engine import run_rule_matching
from .android_analysis.sampler import sample_files
from .android_analysis.verifier import run_verifier
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
from .session_manager import SESSION_MEMORY_FILENAME, SessionManager, SUPPORTED_PROVIDERS, normalize_provider
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

    def _select_skills_for_bundle(bundle: dict, text: str):
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
        if not selected and len(skills) == 1:
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
            if b.get('always_mount'):
                mounted = True
                reason = 'always_mount'
            elif wanted_ids and bid in wanted_ids:
                mounted = True
                reason = 'continuation'
            else:
                for term in _bundle_terms(b):
                    if term and term in text:
                        mounted = True
                        reason = f'keyword: {term[:40]}'
                        break
            bb = dict(b)
            bb['mounted'] = mounted
            bb['mount_reason'] = reason
            bb['selected_skills'] = _select_skills_for_bundle(bb, text) if mounted else []
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
            memory_path = (session_resolved / SESSION_MEMORY_FILENAME).resolve()
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
            log.warning('[Memory] 根据用户显式偏好更新 memory.md 失败: %s', e)
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

    def _android_prompt_char_metrics(artifacts_dir: Path) -> dict:
        def read_len(name: str) -> int:
            path = artifacts_dir / name
            try:
                return len(path.read_text(encoding='utf-8')) if path.is_file() else 0
            except OSError:
                return 0

        return {
            'planner_input_chars_estimate': read_len('file_manifest.json') + read_len('file_tree.json') + read_len('file_samples.json'),
            'first_report_input_chars_estimate': read_len('first_evidence_pack.md') + read_len('matched_rules.json') + read_len('planner_result.json') + read_len('case_cards.json'),
            'deep_report_input_chars_estimate': read_len('deep_evidence_pack.md') + read_len('matched_rules.json') + read_len('planner_result.json') + read_len('final_report.md'),
            'verifier_input_chars_estimate': read_len('deep_evidence_pack.md') + read_len('first_evidence_pack.md') + read_len('matched_rules.json') + read_len('deep_report.md'),
        }

    def _write_android_metrics(store: AndroidAnalysisJobStore, job_id: str, timings: list[dict]) -> dict:
        # 指标文件用于后续成本/性能回归分析，不参与模型判断；写入 artifacts 便于前端下载和测试断言。
        metrics = {
            'version': 1,
            'stage_timings': timings,
            'artifact_sizes': _android_artifact_sizes(store.artifacts_dir(job_id)),
            'prompt_chars': _android_prompt_char_metrics(store.artifacts_dir(job_id)),
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
        for event in events:
            et = event.get('type')
            data = event.get('data') or {}
            if et == 'job_updated':
                lines.append(f"- 阶段：{data.get('status')}")
            elif et == 'stage_timing':
                lines.append(f"- {data.get('stage')}：{data.get('duration_seconds')}s")
            elif et and et != 'job_initialized':
                lines.append(f"- {et}")
        return '\n'.join(lines)

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
        try:
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

            store.update_job(job_id, status='profiling')
            _timed_android_stage(
                store,
                job_id,
                timings,
                'profiling',
                lambda: profile_extracted_tree(store.extracted_dir(job_id), store.artifacts_dir(job_id)),
            )

            store.update_job(job_id, status='sampling')
            samples = _timed_android_stage(
                store,
                job_id,
                timings,
                'sampling',
                lambda: sample_files(store.extracted_dir(job_id), store.artifacts_dir(job_id), question=question),
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
                    enable_ai=enable_ai,
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

            store.update_job(job_id, status='building_evidence')
            evidence = _timed_android_stage(
                store,
                job_id,
                timings,
                'building_evidence',
                lambda: generate_first_evidence_pack(store.artifacts_dir(job_id), question=question),
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
                lambda: recall_case_cards(config.ANDROID_ANALYSIS_KNOWLEDGE_DIR, planner, matched),
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
            _write_android_metrics(store, job_id, timings)
            artifacts = {
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
            final_status = 'needs_review' if verifier.get('status') == 'needs_more_evidence' else 'report_ready'
            job = store.update_job(job_id, status=final_status, artifacts=artifacts, error=None)
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
                    )
                except Exception as exc:
                    log.warning('[AndroidAnalysis] failed to persist report message: %s', exc)
            return job
        except AndroidAnalysisError as e:
            log.warning('[AndroidAnalysis] job=%s failed: %s %s', job_id, e.code, e.message)
            return store.update_job(job_id, status='error', error={'code': e.code, 'message': e.message})
        except Exception as e:
            log.exception('[AndroidAnalysis] job=%s unexpected failure', job_id)
            return store.update_job(job_id, status='error', error={'code': 'android_analysis_unexpected_error', 'message': str(e)})

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
                'bundles': bundles,
            }
        )

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
        try:
            timings: list[dict] = []
            store.update_job(job_id, status='deep_scoping')
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
                    enable_ai=not app.config.get('TESTING', False),
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
                    enable_ai=not app.config.get('TESTING', False),
                ),
            )
            store.append_event(
                job_id,
                'verifier_completed',
                {
                    'status': verifier.get('status'),
                    'overclaim_risk': verifier.get('overclaim_risk'),
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
            _write_android_metrics(store, job_id, timings)
            artifacts['analysis_metrics'] = 'artifacts/analysis_metrics.json'
            status = 'verified' if verifier.get('status') == 'supported' else 'needs_review'
            job = store.update_job(job_id, status=status, artifacts=artifacts, error=None)
            try:
                report_text = (store.artifacts_dir(job_id) / 'verified_report.md').read_text(encoding='utf-8')
                sm.add_message(
                    get_client_ip(request, config.TRUST_X_FORWARDED),
                    user_id,
                    session_id,
                    'assistant',
                    report_text,
                    thinking=_android_progress_markdown(store.read_events(job_id)),
                )
            except Exception as exc:
                log.warning('[AndroidAnalysis] failed to persist deep report message: %s', exc)
            return jsonify({'job': job, 'deep': deep_summary, 'verifier': verifier})
        except AndroidAnalysisError as e:
            log.warning('[AndroidAnalysis] deep job=%s failed: %s %s', job_id, e.code, e.message)
            job = store.update_job(job_id, status='error', error={'code': e.code, 'message': e.message})
            return jsonify({'job': job}), 400

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
        if provider == 'claude' and not provider_sid:
            if any(m.get('role') == 'assistant' for m in prior_messages):
                provider_sid = session_id
                sm.update_provider_session_id(client_ip, user_id, session_id, provider, provider_sid)
                log.info(f'[Chat] 补全 claude_session_id 用于 --resume: {provider_sid}')

        log.info(
            '[Chat] user=%s, session=%s, provider=%s, provider_sid=%s, msg_len=%s',
            user_id, session_id, provider, provider_sid, len(message),
        )

        if session.get('title') == '新对话':
            title = message[:20] + ('...' if len(message) > 20 else '')
            sm.update_session(client_ip, user_id, session_id, title=title)

        uploaded_files = data.get('files', [])
        sm.add_message(client_ip, user_id, session_id, 'user', message,
                       files=uploaded_files if uploaded_files else None)

        upload_dir = sm.get_upload_dir(client_ip, user_id, session_id)
        session_workspace = sm.get_session_dir(client_ip, user_id, session_id)
        session_workspace.mkdir(parents=True, exist_ok=True)
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
        cli_ctx = {
            'user_id': user_id,
            'session_id': session_id,
            'log_dir': config.LOG_DIR,
        }

        def _forward_stream(stream_gen):
            for event_str in stream_gen:
                yield event_str
                if not event_str.startswith('data: '):
                    continue
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
                            log.info('[Chat] 保存 %s session_id=%s（流内）', provider, sid)
                except json.JSONDecodeError:
                    pass

        def generate():
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
                    conversation_history=prior_messages,
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

        def on_finish():
            full_text = ''.join(collected_text)
            full_thinking = ''.join(collected_thinking) if collected_thinking else None
            if full_text:
                sm.add_message(client_ip, user_id, session_id, 'assistant', full_text, thinking=full_thinking)
            if new_claude_sid[0]:
                sm.update_provider_session_id(client_ip, user_id, session_id, provider, new_claude_sid[0])
                log.info('[Chat] 保存 %s session_id=%s（收尾）', provider, new_claude_sid[0])

        response = Response(
            stream_with_context(generate()),
            content_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
                'Connection': 'keep-alive',
            }
        )
        response.call_on_close(lambda: threading.Thread(target=on_finish, daemon=True).start())
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
                yield event_str
                if not event_str.startswith('data: '):
                    continue
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

        def generate():
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

        def on_finish():
            full_text = ''.join(collected_text)
            full_thinking = ''.join(collected_thinking) if collected_thinking else None
            if full_text:
                sm.add_message(
                    client_ip, user_id, session_id, 'assistant', full_text, thinking=full_thinking,
                )
            if new_claude_sid[0]:
                sm.update_provider_session_id(client_ip, user_id, session_id, provider, new_claude_sid[0])
                log.info('[Orchestration/continue] 保存 %s session_id=%s（收尾）', provider, new_claude_sid[0])

        response = Response(
            stream_with_context(generate()),
            content_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
                'Connection': 'keep-alive',
            },
        )
        response.call_on_close(lambda: threading.Thread(target=on_finish, daemon=True).start())
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
        out = {'ok': True}
        if backed is not None:
            try:
                out['backed_up_to'] = str(backed.relative_to(config.ROOT))
            except ValueError:
                out['backed_up_to'] = str(backed)
        if backup_error:
            out['backup_warning'] = 'delete_backup_failed'
        return jsonify(out)

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
