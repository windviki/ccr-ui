"""配置：CCR 令牌读取、RPC 调用、UI 鉴权配置。"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ccr_ui.ccr_client import CcrError

#: CCR 运行时数据目录下的 service.json（含 web token）
SERVICE_FILE = Path("/home/user/.claude-code-router/service.json")
#: CCR Web RPC 端点
CCR_DEFAULT_RPC_URL = "http://127.0.0.1:3458/api/ccr/rpc"


@dataclass(frozen=True)
class AuthConfig:
    """UI 访问口令配置。"""

    enabled: bool
    token: str | None = None


def load_service_token(path: Path = SERVICE_FILE) -> str:
    """从 service.json 的 url 参数读取 `ccr_web_token`。"""
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
    token: str,
    rpc_url: str = CCR_DEFAULT_RPC_URL,
    timeout: float = 30.0,
) -> Callable[[str, list], Any]:
    """返回 `rpc(method, params)` 调用函数。

    通过 urllib 调用 CCR RPC，显式禁用代理（ProxyHandler({})），
    避免本机 HTTP_PROXY 拦截对 127.0.0.1 的请求。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def rpc(method: str, params: list) -> Any:
        body = json.dumps({"method": method, "params": params}).encode("utf-8")
        req = urllib.request.Request(
            rpc_url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "x-ccr-web-auth": token},
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
