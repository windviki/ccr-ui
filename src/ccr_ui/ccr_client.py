"""CCR 配置领域操作客户端。

CCR v3 的 RPC 只有 `getConfig` / `saveConfig` 两个持久化接口（整读整写），
本模块封装"读完整配置 → 规范化 → 改字段 → 整写保存"的领域操作，
并通过依赖注入 `rpc` 调用方，便于单元测试 mock。
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Callable


class CcrError(Exception):
    """CCR 业务错误（HTTP 层映射 400/500）。"""


class NotFoundError(CcrError):
    """目标不存在（HTTP 层映射 404）。"""


def _slugify(name: str) -> str:
    """把 provider 名规范化为 id 中的 slug 段（小写字母数字，非字母数字转 `-`，截断 48 字符）。"""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return slug[:48] or "provider"


class CcrClient:
    """通过注入的 rpc 调用方读取并修改 CCR 配置。"""

    def __init__(self, *, rpc: Callable[[str, list], Any]):
        self._rpc = rpc

    # ---------------- RPC 层 ----------------

    def get_config(self) -> dict:
        """读取 CCR 完整配置。"""
        return self._rpc("getConfig", [])

    def save_config(self, cfg: dict, options: dict | None = None) -> dict:
        """规范化并整写保存配置；`options`（如 {"applyProfile": false}）可选透传。"""
        cfg = self._normalize_config(cfg)
        if options is None:
            return self._rpc("saveConfig", [cfg])
        return self._rpc("saveConfig", [cfg, options])

    # ---------------- 领域操作 ----------------

    def switch_model(self, model: str) -> dict:
        """切换 claude-code profile 的模型，并把 preferredProvider 设为该模型所在 Provider。"""
        cfg = self._normalize_config(self.get_config())
        provider = self._find_provider_for_model(cfg, model)
        if provider is None:
            raise NotFoundError(f"模型 '{model}' 不在任何 Provider 的模型列表中")
        cfg["preferredProvider"] = provider["name"]
        self._set_cc_model(cfg, model)
        self.save_config(cfg)
        return {"model": model, "preferredProvider": provider["name"]}

    def add_provider(self, data: dict) -> dict:
        """新增 Provider；name/baseurl/apikey 必填，models 兼容数组或逗号字符串。"""
        name = self._require(data, "name")
        baseurl = self._require(data, "baseurl")
        apikey = self._require(data, "apikey")
        cfg = self._normalize_config(self.get_config())
        for p in cfg["Providers"]:
            if p.get("name", "").lower() == name.lower():
                raise ValueError(f"Provider '{name}' 已存在")
        provider = self._build_provider(data, name=name, baseurl=baseurl, apikey=apikey)
        cfg["Providers"].append(provider)
        result = self.save_config(cfg)
        return self._find_provider_by_name(result, name) or provider

    def update_provider(self, provider_id: str, data: dict) -> dict:
        """按 id/name 更新 Provider；apikey 传空保留原值；改 name 时同步 preferredProvider。"""
        cfg = self._normalize_config(self.get_config())
        _idx, provider = self._locate_provider(cfg, provider_id)
        old_name = provider.get("name", "")

        if data.get("name"):
            new_name = str(data["name"]).strip()
            if new_name.lower() != old_name.lower():
                for p in cfg["Providers"]:
                    if p is not provider and p.get("name", "").lower() == new_name.lower():
                        raise ValueError(f"Provider '{new_name}' 已存在")
                provider["name"] = new_name
                if cfg.get("preferredProvider") == old_name:
                    cfg["preferredProvider"] = new_name
        if data.get("baseurl"):
            provider["baseurl"] = str(data["baseurl"]).strip()
        if data.get("type"):
            provider["type"] = str(data["type"]).strip()
        if "models" in data:
            provider["models"] = self._normalize_models(data["models"])
        if data.get("apikey"):
            provider["apikey"] = str(data["apikey"]).strip()
            provider["credentials"] = self._derive_credentials(provider["apikey"], provider.get("credentials"))

        # 一致性：若本 Provider 是 preferred，且当前模型不在其 models，切到其首个模型
        if cfg.get("preferredProvider") == provider["name"]:
            cc_model = self._cc_model(cfg)
            if cc_model and provider["models"] and cc_model not in provider["models"]:
                self._set_cc_model(cfg, provider["models"][0])

        result = self.save_config(cfg)
        return self._find_provider_by_name(result, provider["name"]) or provider

    def delete_provider(self, provider_id: str) -> dict:
        """删除 Provider；若删除的是 preferredProvider 则重设为剩余首个（空则置空串）；
        若当前模型属于被删 Provider 则重设为新默认 Provider 的首个模型。"""
        cfg = self._normalize_config(self.get_config())
        _idx, provider = self._locate_provider(cfg, provider_id)
        removed_name = provider.get("name", "")
        removed_models = provider.get("models", [])
        cfg["Providers"] = [p for p in cfg["Providers"] if p.get("id") != provider_id and p.get("name") != provider_id]

        if cfg.get("preferredProvider") == removed_name:
            cfg["preferredProvider"] = cfg["Providers"][0]["name"] if cfg["Providers"] else ""

        cc_model = self._cc_model(cfg)
        if cc_model and removed_models and cc_model in removed_models:
            new_default = self._find_provider_by_name(cfg, cfg.get("preferredProvider", ""))
            if new_default and new_default.get("models"):
                self._set_cc_model(cfg, new_default["models"][0])
            else:
                self._set_cc_model(cfg, "")

        result = self.save_config(cfg)
        return {
            "deleted_id": provider_id,
            "preferredProvider": result.get("preferredProvider", ""),
            "currentModel": self._cc_model(result),
        }

    def set_default_provider(self, provider_id: str) -> dict:
        """把指定 Provider 设为默认（preferredProvider）。"""
        cfg = self._normalize_config(self.get_config())
        _idx, provider = self._locate_provider(cfg, provider_id)
        cfg["preferredProvider"] = provider["name"]
        result = self.save_config(cfg)
        return {"preferredProvider": result.get("preferredProvider", "")}

    # ---------------- 内部工具 ----------------

    @staticmethod
    def _require(data: dict, field: str) -> str:
        value = data.get(field)
        if value is None or not str(value).strip():
            raise ValueError(f"字段 '{field}' 不能为空")
        return str(value).strip()

    @staticmethod
    def _normalize_models(models: Any) -> list[str]:
        """把 models 规范成去重保序的字符串列表（兼容 list 或逗号分隔字符串）。"""
        if isinstance(models, str):
            items = [m.strip() for m in models.split(",") if m.strip()]
        else:
            items = [str(m).strip() for m in (models or []) if str(m).strip()]
        seen: set[str] = set()
        out: list[str] = []
        for m in items:
            if m not in seen:
                seen.add(m)
                out.append(m)
        return out

    @staticmethod
    def _derive_credentials(apikey: str, existing: list | None = None) -> list[dict]:
        if existing and isinstance(existing, list) and existing:
            existing[0]["api_key"] = apikey
            return existing
        return [{"api_key": apikey, "name": "default"}]

    @classmethod
    def _build_provider(cls, data: dict, *, name: str, baseurl: str, apikey: str) -> dict:
        ptype = str(data.get("type") or "").strip()
        if not ptype:
            ptype = "openai_chat_completions" if baseurl.rstrip("/").endswith("/v1") else "anthropic_messages"
        return {
            "name": name,
            "baseurl": baseurl,
            "apikey": apikey,
            "type": ptype,
            "models": cls._normalize_models(data.get("models")),
            "credentials": cls._derive_credentials(apikey),
            "id": cls._make_id(name),
        }

    @staticmethod
    def _make_id(name: str) -> str:
        return f"provider-{_slugify(name)}-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _normalize_config(cfg: dict) -> dict:
        """保证整写前的结构完整，避免保存时丢字段或写坏配置。"""
        cfg = dict(cfg)
        if not isinstance(cfg.get("Providers"), list):
            cfg["Providers"] = []
        if not isinstance(cfg.get("preferredProvider"), str):
            cfg["preferredProvider"] = ""
        profile = cfg.get("profile")
        if not isinstance(profile, dict):
            profile = {}
            cfg["profile"] = profile
        profiles = profile.get("profiles")
        if not isinstance(profiles, list):
            profiles = []
            profile["profiles"] = profiles
        if not any(p.get("agent") == "claude-code" for p in profiles):
            profiles.append({"agent": "claude-code", "model": ""})
        return cfg

    def _find_provider_for_model(self, cfg: dict, model: str) -> dict | None:
        for p in cfg["Providers"]:
            if model in p.get("models", []):
                return p
        return None

    def _locate_provider(self, cfg: dict, provider_id: str) -> tuple[int, dict]:
        for i, p in enumerate(cfg["Providers"]):
            if p.get("id") == provider_id or p.get("name") == provider_id:
                return i, p
        raise NotFoundError(f"Provider '{provider_id}' 不存在")

    def _find_provider_by_name(self, cfg: dict, name: str) -> dict | None:
        for p in cfg.get("Providers", []):
            if p.get("name") == name:
                return p
        return None

    @staticmethod
    def _cc_model(cfg: dict) -> str:
        for p in cfg.get("profile", {}).get("profiles", []):
            if p.get("agent") == "claude-code":
                return p.get("model") or ""
        return ""

    def _set_cc_model(self, cfg: dict, model: str) -> None:
        for p in cfg.get("profile", {}).get("profiles", []):
            if p.get("agent") == "claude-code":
                p["model"] = model
                return
        cfg.setdefault("profile", {}).setdefault("profiles", []).append(
            {"agent": "claude-code", "model": model}
        )
