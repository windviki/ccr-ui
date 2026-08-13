"""配置：`.env` 加载、环境变量聚合、CCR 令牌读取、RPC 调用、UI 鉴权。

配置优先级：命令行参数 > 环境变量 / `.env` > 默认值。
所有路径与地址均可用环境变量覆盖，避免把个人部署信息写死进代码。

环境变量一览（完整说明见 `.env.example`）：
- ``CCR_UI_HOST`` / ``CCR_UI_PORT``   UI 监听地址 / 端口
- ``CCR_RPC_URL``                     CCR 管理 RPC 端点
- ``CCR_SERVICE_FILE``                CCR service.json 位置（含 web token，``~`` 自动展开）
- ``CCR_UI_TOKEN``                    固定 UI 访问口令（可选，覆盖自动读取）
- ``CODE_SERVER_BASE_URL``            code-server 基址（可选，用于打印访问链接）
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ccr_ui.ccr_client import CcrError

# ---- 默认值 ----

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 24678
DEFAULT_RPC_URL = "http://127.0.0.1:3458/api/ccr/rpc"
DEFAULT_SERVICE_FILE = Path("~/.claude-code-router/service.json").expanduser()

#: 兼容旧导入（main.py 曾引用该常量）
CCR_DEFAULT_RPC_URL = DEFAULT_RPC_URL


def load_dotenv(path: str | Path = ".env", override: bool = False) -> bool:
    """极简 `.env` 加载器：解析 ``KEY=VALUE`` 行（支持 ``#`` 注释、空行、可选引号）。

    默认不覆盖已存在的进程环境变量（与 python-dotenv 语义一致）。
    返回是否成功读取文件。
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
    return True


def _env(name: str, default: str | None = None) -> str | None:
    """读取环境变量（`.env` 已由入口 `load_dotenv()` 注入），空值视为未设置。"""
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _parse_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """运行配置，从环境变量 / `.env` 聚合（命令行参数可在入口处覆盖）。"""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    rpc_url: str = DEFAULT_RPC_URL
    service_file: Path = DEFAULT_SERVICE_FILE
    token: str | None = None
    code_server_base_url: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=_env("CCR_UI_HOST") or DEFAULT_HOST,
            port=_parse_int(_env("CCR_UI_PORT"), DEFAULT_PORT),
            rpc_url=_env("CCR_RPC_URL") or DEFAULT_RPC_URL,
            service_file=Path(_env("CCR_SERVICE_FILE") or DEFAULT_SERVICE_FILE).expanduser(),
            token=_env("CCR_UI_TOKEN"),
            code_server_base_url=_env("CODE_SERVER_BASE_URL"),
        )


@dataclass(frozen=True)
class AuthConfig:
    """UI 访问口令配置。"""

    enabled: bool
    token: str | None = None


def load_service_token(path: Path | str | None = None) -> str:
    """从 service.json 的 url 参数读取 ``ccr_web_token``。

    ``path`` 缺省时取 ``CCR_SERVICE_FILE`` 环境变量或默认位置（支持 ``~`` 展开）。
    """
    if path is None:
        path = Path(_env("CCR_SERVICE_FILE") or DEFAULT_SERVICE_FILE).expanduser()
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取 {path}: {exc}") from exc
    token = urllib.parse.parse_qs(urllib.parse.urlparse(data.get("url", "")).query).get(
        "ccr_web_token", [""]
    )[0]
    if not token:
        raise RuntimeError(f"在 {path} 的 url 参数中未找到 ccr_web_token")
    return token


def make_rpc_callable(
    token: str | None,
    rpc_url: str = DEFAULT_RPC_URL,
    timeout: float = 30.0,
) -> Callable[[str, list], Any]:
    """返回 ``rpc(method, params)`` 调用函数。

    通过 urllib 调用 CCR RPC，显式禁用代理（ProxyHandler({})），
    避免本机 HTTP_PROXY 拦截对 127.0.0.1 的请求。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def rpc(method: str, params: list) -> Any:
        # CCR v3 的 RPC 请求参数字段为 `args`（getConfig/saveConfig 均如此），
        # 之前误用 `params` 导致 saveConfig 收不到配置参数、访问 proxy 崩溃。
        body = json.dumps({"method": method, "args": params}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["x-ccr-web-auth"] = token
        req = urllib.request.Request(
            rpc_url,
            data=body,
            method="POST",
            headers=headers,
        )
        try:
            with opener.open(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            message = f"CCR RPC HTTP {exc.code}"
            try:
                err = json.loads(exc.read().decode("utf-8"))
                message = err.get("error", {}).get("message") or message
            except Exception:
                pass
            raise CcrError(message) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CcrError(f"无法连接 CCR ({rpc_url}): {exc}") from exc
        if not payload.get("ok"):
            raise CcrError(payload.get("error", {}).get("message") or "CCR RPC 返回失败")
        return payload["value"]

    return rpc
