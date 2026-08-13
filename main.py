#!/usr/bin/env python3
"""CCR UI 服务入口。

用法:
    uv run python main.py                          # 默认 127.0.0.1:24678，带访问口令
    uv run python main.py --no-auth                # 关闭访问口令（仅限本机使用）
    uv run python main.py --port 3000 --token xxx  # 自定义端口 / 口令

配置方式（优先级：命令行参数 > 环境变量 / `.env` > 默认值），
完整环境变量说明见 `.env.example`。
"""

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from ccr_ui.ccr_client import CcrClient
from ccr_ui.config import AuthConfig, Settings, load_dotenv, load_service_token, make_rpc_callable
from ccr_ui.server import create_server

load_dotenv()  # 载入 .env（存在才载入，不覆盖已设置的进程环境变量）


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CCR 可视化配置前端服务")
    parser.add_argument(
        "--host", default=None, help="监听地址（默认取 CCR_UI_HOST，其次 127.0.0.1）"
    )
    parser.add_argument(
        "--port", type=int, default=None, help="监听端口（默认取 CCR_UI_PORT，其次 24678）"
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="关闭 UI 访问口令校验（默认开启，口令复用 CCR web token）",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="覆盖访问口令（默认取 CCR_UI_TOKEN，其次自动读 service.json 的 ccr_web_token）",
    )
    parser.add_argument(
        "--ccr-base", default=None, help="CCR RPC 地址（默认取 CCR_RPC_URL）"
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    sys.stdout.reconfigure(line_buffering=True)  # 启动信息即时输出到日志
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    settings = Settings.from_env()
    args = parse_args(argv)

    host = args.host or settings.host
    port = args.port if args.port is not None else settings.port
    rpc_url = args.ccr_base or settings.rpc_url
    token = args.token or settings.token or load_service_token(settings.service_file)

    auth = AuthConfig(enabled=not args.no_auth, token=token)
    rpc = make_rpc_callable(token=token, rpc_url=rpc_url)
    client = CcrClient(rpc=rpc)
    static_dir = Path(os.path.dirname(os.path.abspath(__file__))) / "src" / "ccr_ui" / "static"
    server = create_server(
        client=client, auth=auth, static_dir=static_dir, host=host, port=port
    )

    code_server = settings.code_server_base_url.rstrip("/") if settings.code_server_base_url else None

    print(f"[ccr-ui] 监听 http://{host}:{port}/")
    if code_server:
        print(f"[ccr-ui] code-server 访问: {code_server}/proxy/{port}/")
    if auth.enabled:
        print(f"[ccr-ui] 访问口令: {token}")
        if code_server:
            print(f"[ccr-ui] 带口令访问: {code_server}/proxy/{port}/?t={token}")
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
