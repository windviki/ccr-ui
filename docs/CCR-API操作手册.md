# CCR (Claude Code Router) 命令行/HTTP API 操作手册

> 适用版本：`@musistudio/claude-code-router@3.0.6`（CCR v3）
> 适用场景：无法使用 CCR Web UI 图形化配置时，通过 `curl` / HTTP API 完成配置管理。

---

## 1. 架构与端口

CCR v3 运行后有三个本地 HTTP 服务：

| 端口 | 角色 | 说明 |
|------|------|------|
| **3456** | 代理端口 | Claude Code / Codex 等客户端连接的网关入口（`ANTHROPIC_BASE_URL` 指向这里） |
| **3457** | AI Gateway 管理端口 | 底层 `@the-next-ai/ai-gateway` 提供 `/manager/config` 等底层 API |
| **3458** | CCR 管理/Web UI 端口 | 提供 `/api/ccr/rpc` RPC 接口（**本文主用的配置入口**） |

**配置存储（重要）**：CCR v3 的配置**持久化在 SQLite**：

- `/home/user/.claude-code-router/config.sqlite` → 表 `app_config`，存主配置（Providers、profile、Router 等）
- `/home/user/.claude-code-router/app-data/api-keys.sqlite` → 表 `api_keys`，存 CCR 网关自签密钥

磁盘上的 `config.json`（v2 遗留格式）**不是 v3 运行时的配置源**，修改它不生效、也不会被运行时读取（只要 `config.sqlite` 存在）。改配置请走 RPC API 或 sqlite，**不要手动改 `config.json`**。

---

## 2. 获取认证 Token

### 2.1 CCR Web Token（操作 3458 RPC 用）

存在 `/home/user/.claude-code-router/service.json` 的 `url` 参数中：

```bash
WEB_TOKEN=$(python3 -c "import json;print(json.load(open('/home/user/.claude-code-router/service.json'))['url'].split('ccr_web_token=')[1])")
```

请求时放入请求头 `x-ccr-web-auth`：

```bash
curl -s -X POST http://127.0.0.1:3458/api/ccr/rpc \
  -H "Content-Type: application/json" \
  -H "x-ccr-web-auth: $WEB_TOKEN" \
  -d '{"method":"getConfig","params":[]}'
```

### 2.2 Gateway Token（操作 3457 底层 API 用）

存在 `/home/user/.claude-code-router/gateway.config.json` 的 `auth.staticApiKeys.keys[0]`：

```bash
GW_TOKEN=$(python3 -c "import json;print(json.load(open('/home/user/.claude-code-router/gateway.config.json'))['auth']['staticApiKeys']['keys'][0])")
```

请求时放入请求头 `x-ccr-core-auth`：

```bash
curl -s -H "x-ccr-core-auth: $GW_TOKEN" http://127.0.0.1:3457/manager/providers/health
```

---

## 3. 核心 RPC 接口（改模型、增 Provider）

RPC 端点：`POST http://127.0.0.1:3458/api/ccr/rpc`
请求体格式：`{"method": "<方法名>", "params": [<参数>]}` 或 `{"method": "<方法名>", "args": [<参数>]}`

已验证可用的方法：

| 方法 | 用途 |
|------|------|
| `getConfig` | 读取当前完整配置 |
| `saveConfig` | 保存完整配置（需传**完整**配置对象，第二个参数可传 `{"applyProfile": false}` 跳过 profile 应用） |

> `saveConfig` 流程：校验 Provider → 写入 `config.sqlite` → 重新生成 `gateway.config.json` → 应用 profile（写入 `~/.claude/settings.json`）。**修改 Provider 列表会触发 gateway 重载（数秒，自动恢复）**。

---

## 4. 修改模型（v4-pro → v4-flash）

以把 Claude Code profile 的模型从 `deepseek-v4-pro` 改为 `deepseek-v4-flash` 为例。

### 4.1 一键脚本（推荐）

```bash
python3 << 'PYEOF'
import json, urllib.request

WEB_TOKEN = "<REDACTED>"  # 建议改用 2.1 节的自动读取
NEW_MODEL  = "deepseek-v4-flash"

def rpc(method, args=None):
    req = urllib.request.Request(
        "http://127.0.0.1:3458/api/ccr/rpc",
        data=json.dumps({"method": method, "params": args or []}).encode(),
        headers={"Content-Type": "application/json", "x-ccr-web-auth": WEB_TOKEN})
    resp = json.loads(urllib.request.urlopen(req).read())
    if not resp.get("ok"):
        raise SystemExit(f"RPC 失败: {resp}")
    return resp["value"]

# 1. 读取当前配置
cfg = rpc("getConfig")

# 2. 修改 claude-code profile 的 model
for p in cfg["profile"]["profiles"]:
    if p.get("agent") == "claude-code":
        print(f"原模型: {p['model']}")
        p["model"] = NEW_MODEL
        print(f"新模型: {p['model']}")

# 3. 保存（回传完整配置）
result = rpc("saveConfig", [cfg])
print("保存成功 ✓")

# 4. 验证
for p in result["profile"]["profiles"]:
    if p.get("agent") == "claude-code":
        print(f"验证 model = {p['model']}")
PYEOF
```

### 4.2 验证生效

三个层面都应变为 `deepseek-v4-flash`：

```bash
# ① 运行中服务（RPC 读取）
curl -s -X POST http://127.0.0.1:3458/api/ccr/rpc \
  -H "Content-Type: application/json" -H "x-ccr-web-auth: $WEB_TOKEN" \
  -d '{"method":"getConfig","params":[]}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print([p['model'] for p in d['value']['profile']['profiles'] if p.get('agent')=='claude-code'])"

# ② 持久化 sqlite
python3 -c "
import sqlite3, json
db = sqlite3.connect('/home/user/.claude-code-router/config.sqlite')
row = db.execute(\"SELECT value_json FROM app_config WHERE key='default'\").fetchone()
cfg = json.loads(row[0])
print([p['model'] for p in cfg['profile']['profiles'] if p.get('agent')=='claude-code'])"

# ③ Claude Code 环境变量
grep -E "ANTHROPIC_MODEL|CCR_CLAUDE_CODE_MODEL" ~/.claude/settings.json
```

### 4.3 生效时机

- **新启动的 Claude Code 会话**会读取 `~/.claude/settings.json` 中的 `ANTHROPIC_MODEL=deepseek-v4-flash` → 立即生效。
- **已经运行中的会话**不会变（环境变量在进程启动时已固化），需要退出重开才会用新模型。

---

## 5. 新增 Provider

### 5.1 Provider 对象结构

从 `getConfig` 返回的 `Providers` 数组中，每个元素格式如下（参考现有的 deepseek provider）：

```json
{
  "apikey": "sk-xxxxxxxxxxxxxxxx",
  "baseurl": "https://api.deepseek.com/anthropic",
  "credentials": [
    { "api_key": "sk-xxxxxxxxxxxxxxxx", "name": "default" }
  ],
  "id": "provider-deepseek-ece048ef3a",
  "models": ["deepseek-chat", "deepseek-reasoner", "deepseek-v4-flash", "deepseek-v4-pro"],
  "name": "deepseek",
  "type": "anthropic_messages"
}
```

字段说明：

| 字段 | 说明 |
|------|------|
| `name` | Provider 唯一名称（小写），路由时引用 |
| `baseurl` | 上游 API base URL（**注意路径要完整**，Anthropic 协议以 `/anthropic` 结尾，OpenAI 协议以 `/v1` 结尾） |
| `type` | 协议类型：`anthropic_messages`（Anthropic 协议）/ `openai_chat_completions`（OpenAI 协议） |
| `models` | 该 Provider 暴露的模型 ID 数组 |
| `apikey` / `credentials[].api_key` | 上游 API 密钥 |
| `id` | 系统生成的唯一 ID，格式 `provider-<name前48字符>-<sha256(name+baseUrl)前10位hex>`。新增时可留任意唯一值，保存后以 CCR 实际生成的为准 |

### 5.2 新增脚本

```bash
python3 << 'PYEOF'
import json, urllib.request

WEB_TOKEN = "<REDACTED>"  # 建议改用 2.1 节的自动读取

def rpc(method, args=None):
    req = urllib.request.Request(
        "http://127.0.0.1:3458/api/ccr/rpc",
        data=json.dumps({"method": method, "params": args or []}).encode(),
        headers={"Content-Type": "application/json", "x-ccr-web-auth": WEB_TOKEN})
    resp = json.loads(urllib.request.urlopen(req).read())
    if not resp.get("ok"):
        raise SystemExit(f"RPC 失败: {resp}")
    return resp["value"]

cfg = rpc("getConfig")

# 新增的 Provider（示例：Moonshot Kimi）
new_provider = {
    "apikey": "sk-你的-kimi-api-key",
    "baseurl": "https://api.moonshot.cn/anthropic",      # Anthropic 协议
    "credentials": [{"api_key": "sk-你的-kimi-api-key", "name": "default"}],
    "id": "provider-kimi-a1b2c3d4e5",                     # 唯一 id，可占位，CCR 保存后会生成规范 id
    "models": ["kimi-k2-0711-preview", "kimi-k2-instruct"],
    "name": "kimi",
    "type": "anthropic_messages"
}

# 检查是否已存在同名 provider，避免重复
names = [p["name"] for p in cfg["Providers"]]
if new_provider["name"] in names:
    raise SystemExit(f"Provider '{new_provider['name']}' 已存在，无需重复添加")

cfg["Providers"].append(new_provider)
result = rpc("saveConfig", [cfg])

# 验证
saved_names = [p["name"] for p in result["Providers"]]
print("当前 Providers:", saved_names)
print("新增成功 ✓" if "kimi" in saved_names else "新增失败 ✗")
PYEOF
```

> ⚠️ 新增/删除 Provider 会改变 gateway 配置 → **gateway 会自动重载**（数秒内自动恢复）。请勿在重载瞬间发送请求。

### 5.3 把新 Provider 设为默认

新增 Provider 后，若要默认使用它的某个模型，需要同时改两处（都在同一份配置里）：

1. `preferredProvider` → 设为新 Provider 的 `name`
2. `profile.profiles[]` 中 claude-code 的 `model` → 设为新 Provider 里的模型 ID（不带 Provider 前缀，如 `kimi-k2-instruct`）

```python
# 接 5.2 脚本，在 saveConfig 之前：
cfg["preferredProvider"] = "kimi"
for p in cfg["profile"]["profiles"]:
    if p.get("agent") == "claude-code":
        p["model"] = "kimi-k2-instruct"
```

---

## 6. Gateway 底层 API（备用，直接操作 gateway 配置）

底层 AI Gateway（端口 3457）提供直接管理接口，认证用 `x-ccr-core-auth`。**适用于排查 gateway 层问题，日常改模型/加 Provider 请优先用第 3~5 节的 RPC 方式。**

| 方法 | 端点 | 用途 |
|------|------|------|
| GET | `/manager/config` | 读取当前 gateway 配置（密钥字段会被 `[REDACTED]`） |
| PUT | `/manager/config` | 整体替换 gateway 配置（高风险，需完整有效配置） |
| POST | `/manager/config/validate` | 校验配置（**只校验不保存，安全**） |
| GET | `/manager/providers/health` | 查看各 Provider 健康状态 |
| POST | `/manager/providers/health/check` | 触发即时健康检查 |

示例：

```bash
# 查看 provider 健康状态
curl -s -H "x-ccr-core-auth: $GW_TOKEN" http://127.0.0.1:3457/manager/providers/health

# 校验一段新配置是否合法（不保存）
curl -s -X POST -H "x-ccr-core-auth: $GW_TOKEN" -H "Content-Type: application/json" \
  -d '{"providers": []}' http://127.0.0.1:3457/manager/config/validate
```

---

## 7. 注意事项与安全

1. **配置源是 `config.sqlite`**，`config.json` 是 v2 遗留文件，不要手动改它。
2. **`saveConfig` 必须传完整配置**（由 `getConfig` 读出后修改再回传），传不完整的对象会报错或丢字段。
3. 修改 **Provider 列表**会触发 gateway 自动重载；只改 **profile 的 model** 不会重载 gateway。
4. 改模型对**已运行会话不生效**，需新开会话。
5. 敏感信息：RPC 请求体里包含上游 API Key，仅在本机 `127.0.0.1:3458` 传输，注意 shell 历史记录（`history`）会留存明文 Key，可改用文件方式传参。
6. 操作前建议备份：`cp ~/.claude-code-router/config.sqlite ~/.claude-code-router/config.sqlite.bak`。
7. 遇到异常不要直接重启服务（会中断正在运行的会话）；先通过 `getConfig` 确认配置完整，再用 `saveConfig` 修正。

---

## 附：常用命令速查

```bash
# Token 读取
WEB_TOKEN=$(python3 -c "import json;print(json.load(open('/home/user/.claude-code-router/service.json'))['url'].split('ccr_web_token=')[1])")
GW_TOKEN=$(python3 -c "import json;print(json.load(open('/home/user/.claude-code-router/gateway.config.json'))['auth']['staticApiKeys']['keys'][0])")

# 读配置
curl -s -X POST http://127.0.0.1:3458/api/ccr/rpc \
  -H "Content-Type: application/json" -H "x-ccr-web-auth: $WEB_TOKEN" \
  -d '{"method":"getConfig","params":[]}'

# 服务健康检查
curl -s http://127.0.0.1:3456/            # 代理端口
curl -s http://127.0.0.1:3457/health      # gateway 端口

# Provider 健康
curl -s -H "x-ccr-core-auth: $GW_TOKEN" http://127.0.0.1:3457/manager/providers/health
```
