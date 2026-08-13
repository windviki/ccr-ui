# ccr-ui

A lightweight visual configuration frontend for Claude Code Router (CCR) v3 — a stand-in for CCR's built-in Web UI when it can't be reached.

> 中文说明见 [README.zh_CN.md](README.zh_CN.md)。

It operates on CCR's `config.sqlite` through HTTP RPC only (`getConfig` / `saveConfig`, never touching the file directly),
offering **model switching, provider CRUD, and default-provider switching**.

## Why this project

We self-host a VS Code Server and run Claude Code (and other CLI tools) inside its web page via Claude Code Router (CCR).
However, CCR's built-in Web UI cannot be opened from the browser due to port-forwarding / proxy limitations in that
environment. This project is a lightweight web UI that opens correctly inside the VS Code Server web page, so you can
finish basic configuration (switch models, add / edit / remove providers) without leaving the browser.

## Features

- Switch the model used by the claude-code profile (automatically sets `preferredProvider` to the provider owning that model)
- Provider list: view / edit / remove / set default
- Add a provider (name / baseurl / type / models / apikey)
- Server-side proxy to CCR RPC; the frontend never sees any credentials except the access token
- Dark single-page UI, built for code-server `/proxy/<port>/` access (all relative paths)

## Quick start

```bash
cd ccr-ui
uv sync                       # install dependencies (pytest only)
uv run python main.py         # default 127.0.0.1:24678, auto-reads the CCR web token as the access password
```

The startup log prints the access password and URL.

### Access from code-server

```
https://code.example.com/proxy/24678/?t=<access-token>
```

> `code.example.com` is a placeholder — replace it with your code-server base URL, or set
> `CODE_SERVER_BASE_URL` in `.env` so the startup log prints the correct link.

The password is the `ccr_web_token` in the `url` query of `~/.claude-code-router/service.json`.
The frontend stores it in sessionStorage and strips it from the URL.

### Disable auth (local / trusted network only)

```bash
uv run python main.py --no-auth
```

## Command-line options

| Option | Description | Default |
|---|---|---|
| `--host` | Listen address | `127.0.0.1` |
| `--port` | Listen port | `24678` |
| `--no-auth` | Disable UI password check | off |
| `--token` | Override access password | auto-read service.json |
| `--ccr-base` | CCR RPC base URL | `http://127.0.0.1:3458/api/ccr/rpc` |

## Configuration

Every CLI option has a matching environment variable; put them in `.env` (copy `.env.example` and edit — it is git-ignored).
Precedence: **CLI options > environment / `.env` > defaults**.

| Env var | CLI option | Default |
|---|---|---|
| `CCR_UI_HOST` | `--host` | `127.0.0.1` |
| `CCR_UI_PORT` | `--port` | `24678` |
| `CCR_RPC_URL` | `--ccr-base` | `http://127.0.0.1:3458/api/ccr/rpc` |
| `CCR_SERVICE_FILE` | — | `~/.claude-code-router/service.json` |
| `CCR_UI_TOKEN` | `--token` | auto-read service.json |
| `CODE_SERVER_BASE_URL` | — | none (no proxy link printed) |

The UI password and the CCR RPC auth reuse the same CCR web token (`x-ccr-web-auth` header);
the token is auto-read from the `service.json` pointed to by `CCR_SERVICE_FILE`, or pinned via `CCR_UI_TOKEN`.

## Security notes

- The server listens on `127.0.0.1` by default; it is only reachable externally through the code-server proxy.
- The password reuses the CCR web token: even if the port is exposed, config can't be read or changed without it.
- The token passes through the URL query (browser history / proxy logs); the frontend strips it immediately with
  `history.replaceState`, and sensitive data always sits behind the authenticated `/api/*` routes.
- Changing the provider list triggers an automatic CCR gateway reload (a few seconds); brief request failures during it are normal.

## Development

```bash
uv run pytest                 # all tests (unit + integration; integration uses a stub CCR, never touching real config)
```

Layout:

```
main.py                 # entrypoint
src/ccr_ui/
  config.py             # .env loading / Settings / token reading / RPC call / AuthConfig
  ccr_client.py         # CCR config domain operations (dependency-injected, unit-testable)
  server.py             # HTTP server: static files + /api/* + auth
  static/               # single-page frontend (index.html / app.js / style.css)
tests/
  unit/                 # fake-RPC-driven client; mock client + real HTTP server
  integration/          # full chain against a stub CCR RPC
```

## Documentation

- [docs/使用指南.md](docs/使用指南.md) — end-user guide (Chinese): startup, getting the token `t`, access, UI operations, FAQ
- [docs/CCR-API操作手册.md](docs/CCR-API操作手册.md) — CCR v3 underlying RPC / ports / CLI reference (Chinese)
