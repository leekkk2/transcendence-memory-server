"""CLI for transcendence-memory-server."""
import argparse
import os
import sys


def cmd_start(args):
    """启动服务。"""
    os.environ.setdefault('WORKSPACE', os.getcwd())
    # 确保运行时目录存在
    from pathlib import Path
    ws = Path(os.environ['WORKSPACE'])
    for d in ['tasks/active', 'tasks/archived', 'tasks/rag/containers', 'memory', 'memory_archive']:
        (ws / d).mkdir(parents=True, exist_ok=True)

    import uvicorn
    # 需要把 scripts/ 加到 sys.path 以便导入 task_rag_server
    scripts_dir = Path(__file__).resolve().parent.parent.parent / 'scripts'
    if scripts_dir.exists() and str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    uvicorn.run(
        'task_rag_server:app',
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


def cmd_health(args):
    """检查服务健康状态。"""
    import httpx
    url = f'http://{args.host}:{args.port}/health'
    try:
        resp = httpx.get(url, timeout=10)
        import json
        print(json.dumps(resp.json(), indent=2))
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)


def cmd_export_token(args):
    """导出连接令牌。"""
    import httpx
    url = f'http://{args.host}:{args.port}/export-connection-token'
    params = {'container': args.container}
    headers = {'X-API-KEY': os.environ.get('RAG_API_KEY', '')}
    try:
        resp = httpx.get(url, params=params, headers=headers, timeout=10)
        import json
        data = resp.json()
        if args.token_only:
            print(data.get('token', ''))
        else:
            print(json.dumps(data, indent=2))
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(prog='tm-server', description='Transcendence Memory Server')
    sub = parser.add_subparsers(dest='command')

    # start
    p_start = sub.add_parser('start', help='Start the server')
    p_start.add_argument('--host', default='0.0.0.0')
    p_start.add_argument('--port', type=int, default=8711)
    p_start.add_argument('--reload', action='store_true')

    # health
    p_health = sub.add_parser('health', help='Check server health')
    p_health.add_argument('--host', default='127.0.0.1')
    p_health.add_argument('--port', type=int, default=8711)

    # export-token
    p_token = sub.add_parser('export-token', help='Export connection token')
    p_token.add_argument('--container', default='imac')
    p_token.add_argument('--token-only', action='store_true')
    p_token.add_argument('--host', default='127.0.0.1')
    p_token.add_argument('--port', type=int, default=8711)

    args = parser.parse_args()
    if args.command == 'start':
        cmd_start(args)
    elif args.command == 'health':
        cmd_health(args)
    elif args.command == 'export-token':
        cmd_export_token(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
