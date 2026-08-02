"""server 单元测试：mock CcrClient + 真实 ThreadingHTTPServer + http.client 请求。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest import mock

import pytest

from ccr_ui.ccr_client import CcrClient, CcrError, NotFoundError
from ccr_ui.config import AuthConfig
from ccr_ui.server import create_server


def make_config() -> dict:
    return {
        "preferredProvider": "deepseek",
        "Providers": [
            {
                "name": "deepseek",
                "baseurl": "https://api.deepseek.com/anthropic",
                "apikey": "sk-deepseek-abcdef123456",
                "type": "anthropic_messages",
                "models": ["deepseek-v4-flash"],
                "credentials": [{"api_key": "sk-deepseek-abcdef123456", "name": "default"}],
                "id": "provider-deepseek-aaa",
            },
        ],
        "profile": {"profiles": [{"agent": "claude-code", "model": "deepseek-v4-flash"}]},
    }


def make_client(config: dict | None = None) -> mock.Mock:
    client = mock.Mock(spec=CcrClient)
    client.get_config.return_value = config or make_config()
    client.switch_model.return_value = {"model": "x", "preferredProvider": "p"}
    client.add_provider.return_value = {"name": "x", "id": "provider-x-0000"}
    client.update_provider.return_value = {"name": "x", "id": "provider-x-0000"}
    client.delete_provider.return_value = {"deleted_id": "abc", "preferredProvider": "", "currentModel": ""}
    client.set_default_provider.return_value = {"preferredProvider": "x"}
    return client


class ServerHarness:
    """在随机端口启动真实 HTTP 服务，提供请求辅助方法。"""

    def __init__(self, client, auth: AuthConfig, static_dir: Path):
        self.httpd = create_server(
            client=client, auth=auth, static_dir=static_dir, host="127.0.0.1", port=0
        )
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def request(self, method, path, body=None, token=None):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
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
            payload = json.loads(data) if data else None
        except json.JSONDecodeError:
            payload = data.decode("utf-8", "replace")
        return resp.status, payload

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text("<html><head><title>CCR UI</title></head></html>", encoding="utf-8")
    (tmp_path / "app.js").write_text("console.log('x');", encoding="utf-8")
    (tmp_path / "style.css").write_text("body {}", encoding="utf-8")
    return tmp_path


@pytest.fixture
def harness(static_dir: Path):
    h = ServerHarness(make_client(), AuthConfig(enabled=True, token="secret"), static_dir)
    yield h
    h.close()


@pytest.fixture
def no_auth_harness(static_dir: Path):
    h = ServerHarness(make_client(), AuthConfig(enabled=False), static_dir)
    yield h
    h.close()


# ---- 静态文件 ----

def test_index_html_served(harness):
    status, payload = harness.request("GET", "/", token="secret")
    assert status == 200
    assert isinstance(payload, str)
    assert "CCR UI" in payload


def test_static_assets_content_type(harness):
    status, _ = harness.request("GET", "/app.js", token="secret")
    assert status == 200
    status, _ = harness.request("GET", "/style.css", token="secret")
    assert status == 200


def test_path_traversal_rejected(harness):
    status, _ = harness.request("GET", "/../etc/passwd", token="secret")
    assert status in (400, 404)


# ---- 鉴权 ----

def test_health_without_token_401(harness):
    status, payload = harness.request("GET", "/api/health")
    assert status == 401
    assert payload["ok"] is False


def test_health_with_correct_token(harness):
    status, payload = harness.request("GET", "/api/health", token="secret")
    assert status == 200
    assert payload == {"ok": True, "value": {"status": "ok"}}


def test_wrong_token_401(harness):
    status, _ = harness.request("GET", "/api/health", token="wrong")
    assert status == 401


def test_no_auth_mode_allows_access(no_auth_harness):
    status, payload = no_auth_harness.request("GET", "/api/health")
    assert status == 200


# ---- API 端点 ----

def test_bootstrap_structure(harness):
    status, payload = harness.request("GET", "/api/bootstrap", token="secret")
    assert status == 200
    value = payload["value"]
    assert value["preferredProvider"] == "deepseek"
    assert value["currentModel"] == "deepseek-v4-flash"
    assert value["models"] == ["deepseek-v4-flash"]
    assert value["providers"][0]["name"] == "deepseek"
    assert value["providers"][0]["has_key"] is True
    assert "sk-deepseek" not in value["providers"][0]["key_masked"]
    assert harness.httpd.RequestHandlerClass.client.get_config.called


def test_bootstrap_masks_api_key(harness):
    status, payload = harness.request("GET", "/api/bootstrap", token="secret")
    assert status == 200
    masked = payload["value"]["providers"][0]["key_masked"]
    assert "sk-deepseek-abcdef123456" not in masked
    assert masked.startswith("sk-de")


def test_switch_model(harness):
    status, payload = harness.request("POST", "/api/model/switch", body={"model": "x"}, token="secret")
    assert status == 200
    assert payload["value"] == {"model": "x", "preferredProvider": "p"}
    harness.httpd.RequestHandlerClass.client.switch_model.assert_called_once_with("x")


def test_switch_model_missing_field_400(harness):
    status, payload = harness.request("POST", "/api/model/switch", body={}, token="secret")
    assert status == 400
    assert "model" in payload["error"]


def test_switch_model_invalid_json_400(harness):
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", harness.port, timeout=5)
    conn.request("POST", "/api/model/switch", body="{not-json", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer secret",
    })
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    assert resp.status == 400
    assert "JSON" in data["error"]


def test_add_provider(harness):
    status, payload = harness.request(
        "POST", "/api/providers",
        body={"name": "x", "baseurl": "https://x.example/anthropic", "apikey": "sk-x"},
        token="secret",
    )
    assert status == 200
    harness.httpd.RequestHandlerClass.client.add_provider.assert_called_once()


def test_add_provider_value_error_400(harness):
    harness.httpd.RequestHandlerClass.client.add_provider.side_effect = ValueError("重名")
    status, payload = harness.request("POST", "/api/providers", body={"name": "x"}, token="secret")
    assert status == 400
    assert "重名" in payload["error"]


def test_update_provider(harness):
    status, payload = harness.request("PUT", "/api/providers/abc", body={"name": "y"}, token="secret")
    assert status == 200
    harness.httpd.RequestHandlerClass.client.update_provider.assert_called_once_with("abc", {"name": "y"})


def test_update_provider_notfound_404(harness):
    harness.httpd.RequestHandlerClass.client.update_provider.side_effect = NotFoundError("不存在")
    status, payload = harness.request("PUT", "/api/providers/abc", body={"name": "y"}, token="secret")
    assert status == 404
    assert "不存在" in payload["error"]


def test_delete_provider(harness):
    status, payload = harness.request("DELETE", "/api/providers/abc", token="secret")
    assert status == 200
    harness.httpd.RequestHandlerClass.client.delete_provider.assert_called_once_with("abc")


def test_set_default_provider(harness):
    status, payload = harness.request("POST", "/api/providers/abc/default", token="secret")
    assert status == 200
    harness.httpd.RequestHandlerClass.client.set_default_provider.assert_called_once_with("abc")


def test_unknown_api_404(harness):
    status, _ = harness.request("GET", "/api/nope", token="secret")
    assert status == 404


def test_ccr_error_500(harness):
    harness.httpd.RequestHandlerClass.client.get_config.side_effect = CcrError("CCR 连接失败")
    status, payload = harness.request("GET", "/api/bootstrap", token="secret")
    assert status == 500
    assert "CCR 连接失败" in payload["error"]
