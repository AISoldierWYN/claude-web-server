"""
Claude Web Server V2 - 多用户多会话局域网聊天服务
入口：委托给 claude_web.app_factory.create_app
"""

from claude_web.app_factory import create_app
from claude_web.claude_runner import CLAUDE_CLI_PATH
from claude_web import config

app = create_app()

if __name__ == '__main__':
    print('=' * 50)
    print('  Claude Web Server V2')
    print('=' * 50)
    if config.ENABLE_AUTH:
        print(f'  Token: {config.TOKEN}')
        print(f'  访问地址: http://127.0.0.1:{config.SERVER_PORT}?token={config.TOKEN}')
    else:
        print('  认证: 已禁用（局域网内无需 Token）')
        print(f'  访问地址: http://127.0.0.1:{config.SERVER_PORT}')
    print(f'  监听: {config.SERVER_HOST}:{config.SERVER_PORT}')
    print(f'  数据目录: {config.CACHE_DIR}')
    print(f'  日志目录: {config.LOG_DIR}')
    print(f'  Claude CLI: {CLAUDE_CLI_PATH}')
    print('=' * 50)
    app.run(host=config.SERVER_HOST, port=config.SERVER_PORT, threaded=True)
