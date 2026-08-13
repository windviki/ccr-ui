"""集成测试：stub CCR RPC + 真实 CcrClient + 真实 HTTP UI 服务全链路。

验证不触碰真实 CCR（3458），全部走内存态 stub。
"""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from ccr_ui.ccr_client import CcrClient
from ccr_ui.config import AuthConfig, make_rpc_callable
from ccr_ui.server import create_server

STATIC_DIR = Path(__file__).resolve().parents[2] / "src" / "ccr_ui" / "static"


def make_config() -> dict:
    return {
        "preferredProvider": "deepseek",
        "Providers": [
            {
                "name": "deepseek",
                "baseurl": "https://api.deepseek.com/anthropic",
                "apikey": "sk-ds-secret-123",
                "type": "anthropic_messages",
                "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
                "credentials": [{"api_key": "sk-ds-secret-123", "name": "default"}],
                "id": "provider-deepseek-aaa",
            },
            {
                "name": "glm",
                "baseurl": "https://open.bigmodel.cn/api/anthropic",
                "apikey": "sk-glm-secret-456",
                "type": "anthropic_messages",
                "models": ["glm-4.5", "glm-5"],
                "credentials": [{"api_key": "sk-glm-secret-456", "name": "default"}],
                "id": "provider-glm-bbb",
            },
        ],
        "profile": {"profiles": [{"agent": "claude-code", "model": "deepseek-v4-flash"}]},
    }


class StubCCRHandler(BaseHTTPRequestHandler):
    """内存态 CCR RPC 桩：实现 getConfig / saveConfig。"""

    config: dict = {}

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        method = body.get("method")
        # 与真实 CCR 一致：RPC 参数字段是 `args` 而非 `params`
        args = body.get("args") or []
        if method == "getConfig":
            payload = {"ok": True, "value": deepcopy(type(self).config)}
        elif method == "saveConfig":
            if not args:
                raise RuntimeError("saveConfig 缺少 args 参数（协议字段应为 args）")
            type(self).config = deepcopy(args[0])
            payload = {"ok": True, "value": deepcopy(type(self).config)}
        else:
            payload = {"ok": False, "error": {"message": f"unknown method {method}"}}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002
        return


class UIServer:
    """真实 UI 服务（注入真 client），提供 http.client 请求辅助。"""

    def __init__(self, client: CcrClient, auth: AuthConfig):
        self.httpd = create_server(
            client=client, auth=auth, static_dir=STATIC_DIR, host="127.0.0.1", port=0
        )
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def request(self, method, path, body=None, token=None):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if body is not None:
            body = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(data) if data else None
        except json.JSONDecodeError:
            return resp.status, data.decode("utf-8", "replace")

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)


@pytest.fixture
def stub_rpc():
    StubCCRHandler.config = deepcopy(make_config())
    httpd = HTTPServer(("127.0.0.1", 0), StubCCRHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    yield f"http://127.0.0.1:{port}/api/ccr/rpc"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=3)


def make_ui(stub_rpc: str, auth: AuthConfig) -> UIServer:
    rpc = make_rpc_callable(token="stub-token", rpc_url=stub_rpc, timeout=10)
    client = CcrClient(rpc=rpc)
    return UIServer(client, auth)


def test_e2e_bootstrap_reflects_stub(stub_rpc):
    ui = make_ui(stub_rpc, AuthConfig(enabled=False))
    try:
        status, payload = ui.request("GET", "/api/bootstrap")
        assert status == 200
        value = payload["value"]
        assert value["preferredProvider"] == "deepseek"
        assert value["currentModel"] == "deepseek-v4-flash"
        assert len(value["providers"]) == 2
        assert value["providers"][0]["has_key"] is True
        assert "secret" not in value["providers"][0]["key_masked"]
    finally:
        ui.close()


def test_e2e_switch_model_persists_to_stub(stub_rpc):
    ui = make_ui(stub_rpc, AuthConfig(enabled=False))
    try:
        status, payload = ui.request("POST", "/api/model/switch", body={"model": "glm-5"})
        assert status == 200
        assert payload["value"] == {"model": "glm-5", "preferredProvider": "glm"}

        # stub 已持久化
        assert StubCCRHandler.config["preferredProvider"] == "glm"
        cc = next(p for p in StubCCRHandler.config["profile"]["profiles"] if p["agent"] == "claude-code")
        assert cc["model"] == "glm-5"

        # 再次 bootstrap 反映新状态
        _, payload = ui.request("GET", "/api/bootstrap")
        assert payload["value"]["currentModel"] == "glm-5"
        assert payload["value"]["preferredProvider"] == "glm"
    finally:
        ui.close()


def test_e2e_add_then_delete_provider(stub_rpc):
    ui = make_ui(stub_rpc, AuthConfig(enabled=False))
    try:
        # 新增
        status, payload = ui.request(
            "POST", "/api/providers",
            body={
                "name": "kimi",
                "baseurl": "https://api.moonshot.cn/anthropic",
                "apikey": "sk-kimi-secret",
                "models": "kimi-k2-instruct, kimi-k2-0711",
            },
        )
        assert status == 200
        assert payload["value"]["name"] == "kimi"
        assert payload["value"]["models"] == ["kimi-k2-instruct", "kimi-k2-0711"]

        _, payload = ui.request("GET", "/api/bootstrap")
        assert len(payload["value"]["providers"]) == 3

        # 删除
        kimi_id = payload["value"]["providers"][-1]["id"]
        status, payload = ui.request("DELETE", f"/api/providers/{kimi_id}")
        assert status == 200

        _, payload = ui.request("GET", "/api/bootstrap")
        assert len(payload["value"]["providers"]) == 2
        assert all(p["name"] != "kimi" for p in payload["value"]["providers"])
    finally:
        ui.close()


def test_e2e_set_default_provider(stub_rpc):
    ui = make_ui(stub_rpc, AuthConfig(enabled=False))
    try:
        status, payload = ui.request("POST", "/api/providers/provider-glm-bbb/default")
        assert status == 200
        assert payload["value"]["preferredProvider"] == "glm"
        assert StubCCRHandler.config["preferredProvider"] == "glm"
    finally:
        ui.close()


def test_e2e_auth_required(stub_rpc):
    ui = make_ui(stub_rpc, AuthConfig(enabled=True, token="ui-secret"))
    try:
        # 无 token → 401
        status, payload = ui.request("GET", "/api/health")
        assert status == 401
        assert payload["ok"] is False

        # 错误 token → 401
        status, _ = ui.request("GET", "/api/health", token="wrong")
        assert status == 401

        # 正确 token → 200
        status, payload = ui.request("GET", "/api/health", token="ui-secret")
        assert status == 200
        assert payload == {"ok": True, "value": {"status": "ok"}}

        # 正确 token 下 bootstrap 正常
        status, payload = ui.request("GET", "/api/bootstrap", token="ui-secret")
        assert status == 200
        assert len(payload["value"]["providers"]) == 2
    finally:
        ui.close()


def test_e2e_static_served_via_ui(stub_rpc):
    ui = make_ui(stub_rpc, AuthConfig(enabled=False))
    try:
        status, _ = ui.request("GET", "/")
        assert status == 200
        status, _ = ui.request("GET", "/app.js")
        assert status == 200
    finally:
        ui.close()


def test_e2e_error_mapping_unknown_provider(stub_rpc):
    ui = make_ui(stub_rpc, AuthConfig(enabled=False))
    try:
        status, payload = ui.request("DELETE", "/api/providers/nope")
        assert status == 404
        assert "不存在" in payload["error"]
    finally:
        ui.close()
