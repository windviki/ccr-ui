#!/usr/bin/env python3
"""CCR UI 服务入口。

用法:
    uv run python main.py                          # 默认 127.0.0.1:24678，带访问口令
    uv run python main.py --no-auth                # 关闭访问口令（仅限本机使用）
    uv run python main.py --port 3000 --token xxx  # 自定义端口 / 口令
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from ccr_ui.ccr_client import CcrClient
from ccr_ui.config import (
    CCR_DEFAULT_RPC_URL,
    AuthConfig,
    load_service_token,
    make_rpc_callable,
)
from ccr_ui.server import create_server


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CCR 可视化配置前端服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=24678, help="监听端口（默认 24678）")
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="关闭 UI 访问口令校验（默认开启，口令复用 CCR web token）",
    )
    parser.add_argument(
        "--token", default=None, help="覆盖访问口令（默认自动读取 service.json 的 ccr_web_token）"
    )
    parser.add_argument(
        "--ccr-base",
        default=CCR_DEFAULT_RPC_URL,
        help=f"CCR RPC 地址（默认 {CCR_DEFAULT_RPC_URL}）",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    sys.stdout.reconfigure(line_buffering=True)  # 启动信息即时输出到日志
    args = parse_args(argv)
    token = args.token or load_service_token()
    auth = AuthConfig(enabled=not args.no_auth, token=token)
    rpc = make_rpc_callable(token=token, rpc_url=args.ccr_base)
    client = CcrClient(rpc=rpc)
    static_dir = Path(os.path.dirname(os.path.abspath(__file__))) / "src" / "ccr_ui" / "static"
    server = create_server(
        client=client, auth=auth, static_dir=static_dir, host=args.host, port=args.port
    )

    print(f"[ccr-ui] 监听 http://{args.host}:{args.port}/")
    print(f"[ccr-ui] code-server 访问: https://code.example.com/proxy/{args.port}/")
    if auth.enabled:
        print(f"[ccr-ui] 访问口令: {token}")
        print(f"[ccr-ui] 带口令访问: https://code.example.com/proxy/{args.port}/?t={token}")
    else:
        print("[ccr-ui] 鉴权已关闭 (--no-auth)，仅限本机内网使用")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[ccr-ui] 已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
