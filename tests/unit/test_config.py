"""config 模块单元测试：.env 加载、Settings 聚合、token 读取、RPC 鉴权头。"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from ccr_ui import config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "CCR_UI_HOST",
        "CCR_UI_PORT",
        "CCR_RPC_URL",
        "CCR_SERVICE_FILE",
        "CCR_UI_TOKEN",
        "CODE_SERVER_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


# ---- load_dotenv ----

def test_load_dotenv_parses_kv_comments_and_quotes(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text(
        "# comment\n"
        "A=1\n"
        "\n"
        "B='quoted value'\n"
        'C="dq"\n'
        "export D=4\n",
        encoding="utf-8",
    )
    assert config.load_dotenv(p) is True
    assert os.environ["A"] == "1"
    assert os.environ["B"] == "quoted value"
    assert os.environ["C"] == "dq"
    assert os.environ["D"] == "4"


def test_load_dotenv_does_not_override_existing(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("A", "existing")
    p = tmp_path / ".env"
    p.write_text("A=new\n", encoding="utf-8")
    config.load_dotenv(p)
    assert os.environ["A"] == "existing"


def test_load_dotenv_missing_file_returns_false():
    assert config.load_dotenv("/no/such/file.env") is False


# ---- Settings.from_env ----

def test_settings_defaults():
    s = config.Settings.from_env()
    assert s.host == "127.0.0.1"
    assert s.port == 24678
    assert s.rpc_url == config.DEFAULT_RPC_URL
    assert s.service_file == config.DEFAULT_SERVICE_FILE
    assert s.token is None
    assert s.code_server_base_url is None


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("CCR_UI_HOST", "0.0.0.0")
    monkeypatch.setenv("CCR_UI_PORT", "3000")
    monkeypatch.setenv("CCR_RPC_URL", "http://localhost:9/rpc")
    monkeypatch.setenv("CCR_SERVICE_FILE", "~/ccr/service.json")
    monkeypatch.setenv("CCR_UI_TOKEN", "abc")
    monkeypatch.setenv("CODE_SERVER_BASE_URL", "https://code.example.com")
    s = config.Settings.from_env()
    assert s.host == "0.0.0.0"
    assert s.port == 3000
    assert s.rpc_url == "http://localhost:9/rpc"
    assert s.service_file == Path("~/ccr/service.json").expanduser()
    assert s.token == "abc"
    assert s.code_server_base_url == "https://code.example.com"


def test_settings_invalid_port_falls_back(monkeypatch):
    monkeypatch.setenv("CCR_UI_PORT", "not-a-number")
    assert config.Settings.from_env().port == 24678


# ---- load_service_token ----

def _write_service_json(path: Path, token: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'{{"url": "http://127.0.0.1:3458/?ccr_web_token={token}"}}',
        encoding="utf-8",
    )
    return path


def test_load_service_token_reads_query_param(tmp_path: Path):
    p = _write_service_json(tmp_path / "service.json", "tok-123")
    assert config.load_service_token(p) == "tok-123"


def test_load_service_token_missing_file_raises(tmp_path: Path):
    with pytest.raises(RuntimeError):
        config.load_service_token(tmp_path / "missing.json")


def test_load_service_token_no_token_raises(tmp_path: Path):
    p = tmp_path / "service.json"
    p.write_text('{"url": "http://127.0.0.1:3458/"}', encoding="utf-8")
    with pytest.raises(RuntimeError):
        config.load_service_token(p)


def test_load_service_token_default_uses_env(monkeypatch, tmp_path: Path):
    p = _write_service_json(tmp_path / "svc.json", "env-tok")
    monkeypatch.setenv("CCR_SERVICE_FILE", str(p))
    assert config.load_service_token() == "env-tok"


# ---- make_rpc_callable 鉴权头 ----

def test_make_rpc_callable_auth_header_and_args():
    captured: dict = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            captured["auth"] = self.headers.get("x-ccr-web-auth")
            n = int(self.headers.get("Content-Length") or 0)
            captured["body"] = json.loads(self.rfile.read(n).decode("utf-8"))
            data = json.dumps({"ok": True, "value": {"x": 1}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args) -> None:  # noqa: A002
            return

    httpd = HTTPServer(("127.0.0.1", 0), H)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/rpc"
    try:
        config.make_rpc_callable(token=None, rpc_url=url)("getConfig", [])
        assert captured["auth"] is None
        assert captured["body"]["args"] == []  # CCR v3 参数字段是 args

        config.make_rpc_callable(token="tok", rpc_url=url)("getConfig", [])
        assert captured["auth"] == "tok"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
