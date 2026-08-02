# ccr-ui

Claude Code Router (CCR) v3 的轻量可视化配置前端，替代无法访问的 CCR 自带 Web UI。

通过 HTTP API 操作 CCR 的 `config.sqlite`（仅经 `getConfig` / `saveConfig` RPC，不直接改文件），
提供：**模型选择切换、Provider 增删改、默认 Provider 切换**。

## 功能

- 切换 claude-code profile 使用的模型（自动把 `preferredProvider` 设为该模型所在 Provider）
- Provider 列表：查看 / 编辑 / 删除 / 设为默认
- 新增 Provider（name / baseurl / type / models / apikey）
- 服务端代理 CCR RPC，前端不接触 token 明文之外的任何凭据
- 深色单页 UI，适配 code-server `/proxy/<port>/` 代理访问（全部相对路径）

## 快速开始

```bash
cd /home/user/new-ccr-ui
uv sync                       # 安装依赖（仅 pytest）
uv run python main.py         # 默认 127.0.0.1:24678，自动读取 CCR web token 作为访问口令
```

启动后日志会打印访问口令与 URL。

### code-server 内访问

```
https://code.example.com/proxy/24678/?t=<访问口令>
```

口令即 `~/.claude-code-router/service.json` 中 `url` 参数里的 `ccr_web_token`。
前端会将口令存入 sessionStorage 并从 URL 中清除。

### 关闭鉴权（仅限本机内网 / 信任环境）

```bash
uv run python main.py --no-auth
```

## 命令行参数

| 参数 | 说明 | 默认 |
|---|---|---|
| `--host` | 监听地址 | `127.0.0.1` |
| `--port` | 监听端口 | `24678` |
| `--no-auth` | 关闭 UI 访问口令校验 | 关 |
| `--token` | 覆盖访问口令 | 自动读 service.json |
| `--ccr-base` | CCR RPC 地址 | `http://127.0.0.1:3458/api/ccr/rpc` |

## 安全说明

- 服务默认监听 `127.0.0.1`，经 code-server 代理方可从外部访问；code-server 之外端口未暴露。
- 访问口令复用 CCR web token：即使端口暴露，无口令也无法读取/修改配置。
- 令牌经 URL query 传递有泄露面（浏览器历史/代理日志），前端会立即用 `history.replaceState` 清除，
  敏感数据始终在需鉴权的 `/api/*` 之后。
- 修改 Provider 列表会触发 CCR gateway 自动重载（数秒），期间请求可能短暂失败，属正常现象。

## 开发

```bash
uv run pytest                 # 全部测试（单元 + 集成，集成用 stub CCR，不触碰真实配置）
```

目录结构：

```
main.py                 # 入口
src/ccr_ui/
  config.py             # token 读取 / RPC 客户端 / AuthConfig
  ccr_client.py         # CCR 配置领域操作（依赖注入，可单测）
  server.py             # HTTP 服务：静态文件 + /api/* + 鉴权
  static/               # 前端单页（index.html / app.js / style.css）
tests/
  unit/                 # fake RPC 驱动 client；mock client + 真实 HTTP server
  integration/          # stub CCR RPC 全链路
```

## 文档

- [docs/使用指南.md](docs/使用指南.md) —— 面向最终用户的使用指南：启动、获取口令 `t`、访问方式、UI 操作、FAQ
- [docs/CCR-API操作手册.md](docs/CCR-API操作手册.md) —— CCR v3 底层 RPC / 端口 / 命令行操作参考
