"""Flask 应用工厂。"""

import logging
import sys
from pathlib import Path

from flask import Flask

from . import config
from .claude_runner import CLAUDE_CLI_PATH
from .routes import PATHS_BUNDLES_KEY, PATHS_NOTES_KEY, READONLY_DIRS_KEY, register_routes
from .session_manager import SessionManager


def setup_logging():
    config.LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(config.LOG_DIR / 'server.log', encoding='utf-8'),
        ],
    )


def create_app():
    setup_logging()
    log = logging.getLogger('claude-web')

    config.log_config_summary(log)
    readonly, paths_notes, paths_bundles = config.merge_readonly_dirs(log)
    if readonly:
        log.info(f'[Config] 合并后只读目录（共 {len(readonly)} 个）: {readonly}')
    if paths_notes:
        log.info('[Config] claude_web_paths.config.json 含 notes，将注入沙箱提示')
    if paths_bundles:
        log.info('[Config] 技能包 bundles: %s 个（摘要注入提示词，路径已合并至 --add-dir）', len(paths_bundles))
    if config.CLAUDE_WEB_PERMISSION_MODE:
        log.info(f'[Config] CLAUDE_WEB_PERMISSION_MODE={config.CLAUDE_WEB_PERMISSION_MODE}')
    if config.CLAUDE_WEB_DANGEROUSLY_SKIP_PERMISSIONS:
        log.warning(
            '[Config] CLAUDE_WEB_DANGEROUSLY_SKIP_PERMISSIONS=1：已启用 --dangerously-skip-permissions，'
            '所有工具操作将不再询问，请仅在可信环境使用'
        )
    if config.TRUST_X_FORWARDED:
        log.info('[Config] CLAUDE_WEB_TRUST_X_FORWARDED=1：将使用 X-Forwarded-For 首跳作为客户端 IP')

    config.CACHE_DIR.mkdir(exist_ok=True)
    config.BACKUPS_DIR.mkdir(exist_ok=True)
    config.FEEDBACK_DIR.mkdir(exist_ok=True)

    static_dir = str(config.ROOT / 'static')
    app = Flask(__name__, static_folder=static_dir, static_url_path='/static')

    sm = SessionManager(config.CACHE_DIR)
    app.config[READONLY_DIRS_KEY] = readonly
    app.config[PATHS_NOTES_KEY] = paths_notes
    app.config[PATHS_BUNDLES_KEY] = paths_bundles

    register_routes(app, sm)

    return app
