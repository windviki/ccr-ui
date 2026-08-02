"""HTTP 服务：静态前端 + `/api/*` 路由 + Bearer 鉴权。

路由约定：
- 静态文件（`/`、`/index.html`、`/app.js`、`/style.css`）免鉴权
- `/api/*` 默认需要 `Authorization: Bearer <token>`
- 统一响应：成功 `200 {"ok":true,"value":...}`；失败 `{"ok":false,"error":"..."}`
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ccr_ui.ccr_client import CcrClient, CcrError, NotFoundError
from ccr_ui.config import AuthConfig

logger = logging.getLogger(__name__)

_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}

#: 允许直接访问的静态文件白名单
_STATIC_ALLOWED = {"index.html", "app.js", "style.css"}

_MAX_BODY = 10 * 1024 * 1024  # 10MB


class CcrUIHandler(BaseHTTPRequestHandler):
    """由 `create_server` 注入 `client` / `auth` / `static_dir` 类属性后使用。"""

    client: CcrClient
    auth: AuthConfig
    static_dir: Path
    server_version = "ccr-ui/0.1"

    # ---------------- HTTP 方法入口 ----------------

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        # 同源场景用不到 CORS，防御性返回 200
        self.send_response(200)
        self.end_headers()

    # ---------------- 路由 ----------------

    def _dispatch(self) -> None:
        method, path, _query = self._route()
        if method == "GET" and path in ("/", "/index.html", "/app.js", "/style.css"):
            rel = path.lstrip("/") or "index.html"
            self._serve_static(rel)
            return
        if path.startswith("/api/"):
            self._dispatch_api(method, path)
            return
        self._send_json(404, {"ok": False, "error": "Not Found"})

    def _route(self) -> tuple[str, str, dict[str, list[str]]]:
        parsed = urllib.parse.urlparse(self.path)
        return self.command, parsed.path, urllib.parse.parse_qs(parsed.query)

    # ---------------- API ----------------

    def _dispatch_api(self, method: str, path: str) -> None:
        if not self._require_auth():
            return
        try:
            if path == "/api/health" and method == "GET":
                return self._send_json(200, {"ok": True, "value": {"status": "ok"}})

            if path == "/api/bootstrap" and method == "GET":
                return self._send_json(200, {"ok": True, "value": self._bootstrap()})

            if path == "/api/model/switch" and method == "POST":
                body = self._read_json_body()
                model = str(body.get("model") or "").strip()
                if not model:
                    raise ValueError("字段 'model' 不能为空")
                return self._send_json(200, {"ok": True, "value": self.client.switch_model(model)})

            if path == "/api/providers" and method == "POST":
                data = self._read_json_body()
                return self._send_json(200, {"ok": True, "value": self.client.add_provider(data)})

            m = re.fullmatch(r"/api/providers/([^/]+)", path)
            if m:
                pid = urllib.parse.unquote(m.group(1))
                if method == "PUT":
                    data = self._read_json_body()
                    return self._send_json(200, {"ok": True, "value": self.client.update_provider(pid, data)})
                if method == "DELETE":
                    return self._send_json(200, {"ok": True, "value": self.client.delete_provider(pid)})

            m = re.fullmatch(r"/api/providers/([^/]+)/default", path)
            if m and method == "POST":
                pid = urllib.parse.unquote(m.group(1))
                return self._send_json(200, {"ok": True, "value": self.client.set_default_provider(pid)})

            self._send_json(404, {"ok": False, "error": "Not Found"})
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
        except NotFoundError as exc:
            self._send_json(404, {"ok": False, "error": str(exc)})
        except CcrError as exc:
            self._send_json(500, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            logger.exception("处理 %s %s 出错", method, path)
            self._send_json(500, {"ok": False, "error": f"内部错误: {exc}"})

    def _bootstrap(self) -> dict:
        cfg = self.client.get_config()
        providers: list[dict[str, Any]] = []
        models: set[str] = set()
        for p in cfg.get("Providers", []):
            providers.append(
                {
                    "id": p.get("id", ""),
                    "name": p.get("name", ""),
                    "baseurl": p.get("baseurl", ""),
                    "type": p.get("type", ""),
                    "models": p.get("models", []),
                    "has_key": bool(p.get("apikey")),
                    "key_masked": self._mask_key(p.get("apikey", "")),
                }
            )
            models.update(p.get("models", []))
        return {
            "providers": providers,
            "preferredProvider": cfg.get("preferredProvider", ""),
            "currentModel": self._cc_model(cfg),
            "models": sorted(models),
            "authEnabled": self.auth.enabled,
        }

    @staticmethod
    def _cc_model(cfg: dict) -> str:
        for p in cfg.get("profile", {}).get("profiles", []):
            if p.get("agent") == "claude-code":
                return p.get("model") or ""
        return ""

    @staticmethod
    def _mask_key(apikey: str) -> str:
        if not apikey:
            return ""
        if len(apikey) <= 8:
            return "********"
        return apikey[:6] + "****" + apikey[-4:]

    # ---------------- 鉴权 ----------------

    def _require_auth(self) -> bool:
        if not self.auth.enabled:
            return True
        header = self.headers.get("Authorization", "")
        token = header[len("Bearer "):] if header.startswith("Bearer ") else ""
        if token and self.auth.token and hmac.compare_digest(token, self.auth.token):
            return True
        self._send_json(401, {"ok": False, "error": "未授权：请提供正确的访问口令 (?t=<token> 或 Authorization 头)"})
        return False

    # ---------------- 静态文件 ----------------

    def _serve_static(self, relpath: str) -> None:
        if relpath not in _STATIC_ALLOWED:
            self._send_json(404, {"ok": False, "error": "Not Found"})
            return
        base = self.static_dir.resolve()
        target = (base / relpath).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            self._send_json(404, {"ok": False, "error": "Not Found"})
            return
        data = target.read_bytes()
        content_type = _MIME_TYPES.get(target.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---------------- 工具 ----------------

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > _MAX_BODY:
            raise ValueError("请求体为空或过大")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("请求体不是合法的 JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002
        # 静默访问日志，仅错误通过 logger.exception 输出
        return


def create_server(
    client: CcrClient,
    auth: AuthConfig,
    static_dir: Path,
    host: str = "127.0.0.1",
    port: int = 24678,
) -> ThreadingHTTPServer:
    """注入依赖并创建线程化 HTTP 服务。"""
    CcrUIHandler.client = client
    CcrUIHandler.auth = auth
    CcrUIHandler.static_dir = static_dir
    return ThreadingHTTPServer((host, port), CcrUIHandler)
