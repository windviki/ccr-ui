"""ccr_client 单元测试：用 fake RPC 驱动，断言对 AppConfig 字段的改动与保存参数。"""

from copy import deepcopy

import pytest

from ccr_ui.ccr_client import CcrClient, CcrError, NotFoundError


class FakeRPC:
    """内存态 getConfig / saveConfig，记录调用并回放改动后的配置。"""

    def __init__(self, config: dict):
        self.config = deepcopy(config)
        self.calls: list[tuple[str, list]] = []
        self.saved: list[dict] = []

    def __call__(self, method: str, params: list):
        self.calls.append((method, params))
        if method == "getConfig":
            return deepcopy(self.config)
        if method == "saveConfig":
            self.config = deepcopy(params[0])
            self.saved.append(deepcopy(params[0]))
            return deepcopy(self.config)
        raise CcrError(f"unknown rpc method {method}")


def make_config() -> dict:
    return {
        "preferredProvider": "deepseek",
        "Providers": [
            {
                "name": "deepseek",
                "baseurl": "https://api.deepseek.com/anthropic",
                "apikey": "sk-ds",
                "type": "anthropic_messages",
                "models": ["deepseek-chat", "deepseek-v4-flash", "deepseek-v4-pro"],
                "credentials": [{"api_key": "sk-ds", "name": "default"}],
                "id": "provider-deepseek-aaa",
            },
            {
                "name": "glm",
                "baseurl": "https://open.bigmodel.cn/api/anthropic",
                "apikey": "sk-glm",
                "type": "anthropic_messages",
                "models": ["glm-4.5", "glm-5"],
                "credentials": [{"api_key": "sk-glm", "name": "default"}],
                "id": "provider-glm-bbb",
            },
        ],
        "profile": {
            "profiles": [
                {"agent": "claude-code", "model": "deepseek-v4-flash", "id": "default-claude-code", "name": "Claude Code"},
            ]
        },
        "APIKEYS": [],
        "Router": {},
    }


@pytest.fixture
def client():
    return CcrClient(rpc=FakeRPC(make_config()))


# ---- 1. get_config ----

def test_get_config_returns_full_config(client):
    cfg = client.get_config()
    assert cfg["preferredProvider"] == "deepseek"
    assert len(cfg["Providers"]) == 2


# ---- 2~5. switch_model ----

def test_switch_model_sets_model_and_preferred(client):
    result = client.switch_model("glm-5")
    assert result == {"model": "glm-5", "preferredProvider": "glm"}
    saved = client._rpc.saved[-1]
    assert saved["preferredProvider"] == "glm"
    cc = next(p for p in saved["profile"]["profiles"] if p["agent"] == "claude-code")
    assert cc["model"] == "glm-5"


def test_switch_model_creates_missing_cc_profile(client):
    client._rpc.config["profile"]["profiles"] = []  # 模拟缺少 claude-code profile
    client.switch_model("glm-5")
    saved = client._rpc.saved[-1]
    cc = [p for p in saved["profile"]["profiles"] if p.get("agent") == "claude-code"]
    assert len(cc) == 1
    assert cc[0]["model"] == "glm-5"


def test_switch_model_unknown_model_raises_notfound(client):
    with pytest.raises(NotFoundError):
        client.switch_model("no-such-model")


def test_switch_model_shared_model_takes_first_provider():
    cfg = make_config()
    cfg["Providers"].append(
        {
            "name": "deepseek2",
            "baseurl": "https://x.example/anthropic",
            "apikey": "sk-x",
            "type": "anthropic_messages",
            "models": ["shared-model"],
            "credentials": [{"api_key": "sk-x", "name": "default"}],
            "id": "provider-deepseek2-ccc",
        }
    )
    cfg["Providers"][0]["models"].append("shared-model")  # deepseek 在前，应优先
    c = CcrClient(rpc=FakeRPC(cfg))
    result = c.switch_model("shared-model")
    assert result["preferredProvider"] == "deepseek"


# ---- 6~9. add_provider ----

def test_add_provider_appends_with_id_and_credentials(client):
    result = client.add_provider(
        {"name": "kimi", "baseurl": "https://api.moonshot.cn/anthropic", "apikey": "sk-kimi", "models": ["kimi-1", "kimi-2"]}
    )
    assert result["name"] == "kimi"
    assert result["id"].startswith("provider-kimi-")
    assert result["credentials"] == [{"api_key": "sk-kimi", "name": "default"}]
    assert result["type"] == "anthropic_messages"
    assert len(client._rpc.config["Providers"]) == 3


def test_add_provider_normalizes_comma_string_models(client):
    result = client.add_provider(
        {"name": "x", "baseurl": "https://x.example/v1", "apikey": "sk-x", "models": " a , b, a ,"}
    )
    assert result["models"] == ["a", "b"]
    assert result["type"] == "openai_chat_completions"


def test_add_provider_duplicate_name_raises(client):
    with pytest.raises(ValueError):
        client.add_provider({"name": "DeepSeek", "baseurl": "https://x.example/anthropic", "apikey": "sk-x"})


def test_add_provider_missing_required_raises(client):
    with pytest.raises(ValueError):
        client.add_provider({"name": "x", "baseurl": "https://x.example/anthropic"})
    with pytest.raises(ValueError):
        client.add_provider({"name": "x", "apikey": "sk-x"})


# ---- 10~12. update_provider ----

def test_update_provider_keeps_apikey_when_omitted(client):
    result = client.update_provider("provider-deepseek-aaa", {"baseurl": "https://new.example/anthropic", "models": ["m1"]})
    assert result["baseurl"] == "https://new.example/anthropic"
    assert result["models"] == ["m1"]
    assert result["apikey"] == "sk-ds"
    assert result["credentials"][0]["api_key"] == "sk-ds"


def test_update_provider_rename_syncs_preferred(client):
    result = client.update_provider("provider-deepseek-aaa", {"name": "ds-new"})
    assert result["name"] == "ds-new"
    assert client._rpc.config["preferredProvider"] == "ds-new"


def test_update_provider_unknown_raises(client):
    with pytest.raises(NotFoundError):
        client.update_provider("nope", {"name": "x"})


def test_update_provider_apikey_overwrites_credentials(client):
    result = client.update_provider("provider-deepseek-aaa", {"apikey": "sk-new"})
    assert result["apikey"] == "sk-new"
    assert result["credentials"][0]["api_key"] == "sk-new"


def test_update_provider_preferred_with_orphan_model_resets_to_first(client):
    # deepseek 是 preferred，把它的模型改成不含当前 cc model 的列表 → cc model 应重置为首个
    result = client.update_provider("provider-deepseek-aaa", {"models": ["brand-new"]})
    assert client._rpc.config["preferredProvider"] == "deepseek"
    cc = next(p for p in client._rpc.config["profile"]["profiles"] if p["agent"] == "claude-code")
    assert cc["model"] == "brand-new"


# ---- 13~16. delete_provider ----

def test_delete_provider_resets_preferred_to_first_remaining(client):
    result = client.delete_provider("provider-deepseek-aaa")
    assert result["deleted_id"] == "provider-deepseek-aaa"
    assert result["preferredProvider"] == "glm"
    names = [p["name"] for p in client._rpc.config["Providers"]]
    assert names == ["glm"]


def test_delete_provider_wipes_preferred_when_empty():
    cfg = make_config()
    cfg["Providers"] = [cfg["Providers"][0]]  # 只剩 deepseek
    c = CcrClient(rpc=FakeRPC(cfg))
    result = c.delete_provider("provider-deepseek-aaa")
    assert result["preferredProvider"] == ""
    assert result["currentModel"] == ""


def test_delete_provider_resets_cc_model_to_new_default(client):
    # preferred=deepseek 被删，cc model=deepseek-v4-flash 属于被删 provider
    result = client.delete_provider("provider-deepseek-aaa")
    assert result["currentModel"] == "glm-4.5"


def test_delete_provider_non_preferred_keeps_preferred():
    cfg = make_config()
    cfg["preferredProvider"] = "glm"
    cfg["profile"]["profiles"][0]["model"] = "deepseek-v4-flash"  # cc model 属于 deepseek，但 deepseek 非 preferred
    c = CcrClient(rpc=FakeRPC(cfg))
    result = c.delete_provider("provider-deepseek-aaa")
    assert result["preferredProvider"] == "glm"
    assert result["currentModel"] == "glm-4.5"


def test_delete_provider_unknown_raises(client):
    with pytest.raises(NotFoundError):
        client.delete_provider("nope")


# ---- 17~18. set_default_provider ----

def test_set_default_provider(client):
    result = client.set_default_provider("provider-glm-bbb")
    assert result["preferredProvider"] == "glm"


def test_set_default_provider_unknown_raises(client):
    with pytest.raises(NotFoundError):
        client.set_default_provider("nope")


# ---- 19. save_config options ----

def test_save_config_passes_options():
    c = CcrClient(rpc=FakeRPC(make_config()))
    c.save_config(make_config(), options={"applyProfile": False})
    method, params = c._rpc.calls[-1]
    assert method == "saveConfig"
    assert params[1] == {"applyProfile": False}


def test_save_config_without_options_single_arg():
    c = CcrClient(rpc=FakeRPC(make_config()))
    c.save_config(make_config())
    method, params = c._rpc.calls[-1]
    assert method == "saveConfig"
    assert len(params) == 1


# ---- 20. _normalize_config ----

def test_normalize_config_fills_missing_fields():
    cfg = CcrClient._normalize_config({})
    assert cfg["Providers"] == []
    assert cfg["preferredProvider"] == ""
    assert cfg["profile"]["profiles"][0]["agent"] == "claude-code"


def test_normalize_config_keeps_existing_structure():
    cfg = make_config()
    out = CcrClient._normalize_config(cfg)
    assert out["preferredProvider"] == "deepseek"
    assert len(out["Providers"]) == 2
    assert len(out["profile"]["profiles"]) == 1  # 已有 claude-code，不重复添加


def test_normalize_config_is_shallow_copy_no_mutation():
    cfg = make_config()
    CcrClient._normalize_config(cfg)
    assert cfg["profile"]["profiles"][0]["agent"] == "claude-code"
