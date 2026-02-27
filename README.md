# Codex Provider — Agent Zero Plugin

Use your **ChatGPT Pro, Plus, or Team subscription** to power [Agent Zero](https://github.com/agent0ai/agent-zero) with OpenAI's latest Codex models — no API key required.

## What This Does

This plugin runs a local proxy inside the Agent Zero container that bridges LiteLLM's OpenAI-compatible calls to ChatGPT's backend Responses API. It authenticates via OAuth (the same device-code flow used by the official [Codex CLI](https://github.com/openai/codex)).

```
Agent Zero ──► LiteLLM ──► Local Proxy (:8400) ──► chatgpt.com/backend-api/codex/responses
                           (this plugin)
```

## Supported Models

These models are verified working with ChatGPT subscription accounts:

| Model | Description |
|-------|-------------|
| `gpt-5.3-codex` | Latest Codex model (recommended) |
| `gpt-5.2-codex` | Previous-gen Codex |
| `gpt-5.1-codex` | Earlier Codex |
| `gpt-5.1-codex-mini` | Lightweight Codex (good for utility/browser) |
| `gpt-5.2` | Base GPT-5.2 |
| `gpt-5.1` | Base GPT-5.1 |

Models **not** supported via this method: `codex-mini-latest`, `o3`, `o4-mini`, `gpt-4.x`, `gpt-4o`.

## Installation

1. Copy this entire `codex-provider/` directory into your Agent Zero `usr/plugins/` folder:

   ```bash
   # If running via Docker (recommended):
   docker cp codex-provider/ agent-zero:/a0/usr/plugins/codex-provider/
   docker compose restart agent-zero

   # If running directly:
   cp -r codex-provider/ /path/to/agent-zero/usr/plugins/
   ```

2. Open Agent Zero's web UI, go to **Settings > Plugins** and verify "Codex Provider" appears.

## Setup

### Option A: Device Code Flow (Recommended)

1. Open the **Codex Provider** plugin dashboard in Agent Zero
2. Click **"Sign in with OpenAI"**
3. A code appears (e.g. `ABCD-1234`) — enter it at the OpenAI verification page that opens
4. Once approved, the plugin automatically:
   - Stores your OAuth tokens
   - Starts the local proxy
   - Configures Agent Zero to use the proxy

### Option B: Import from Codex CLI

If you already use the [Codex CLI](https://github.com/openai/codex):

1. Run `codex auth login` on your machine
2. Copy the contents of `~/.codex/auth.json`
3. Paste them into the "Import Tokens" section of the plugin dashboard

### Option C: Manual Token Import

Paste an access token and/or refresh token directly via the dashboard.

## How It Works

### OAuth Authentication

The plugin uses OpenAI's device code authorization flow — the same flow the official Codex CLI uses. Your browser opens a verification page where you sign in with your ChatGPT account. The plugin receives OAuth tokens and stores them securely in the plugin config.

Tokens refresh automatically. You can also manually refresh or revoke tokens from the dashboard.

### Proxy Architecture

The proxy listens on `127.0.0.1:8400` inside the container (not exposed externally) and handles two endpoint types:

- **`/v1/responses`** — Near-passthrough for LiteLLM's Responses API calls (primary path for Codex models)
- **`/v1/chat/completions`** — Transforms standard chat completion format to Responses API format

Both routes forward to `chatgpt.com/backend-api/codex/responses` with the required authentication headers.

### Auto-Start

When `auto_configure` is enabled (set automatically after successful authentication), the proxy starts on the first agent message loop via the `_10_codex_proxy.py` extension.

## Configuration

The plugin settings page exposes:

| Setting | Default | Description |
|---------|---------|-------------|
| `proxy_port` | `8400` | Local proxy port |
| `chat_model` | `gpt-5.3-codex` | Default chat model |
| `auto_configure` | `false` | Auto-start proxy on agent init |

Advanced users can edit `config.json` directly in the plugin's data directory.

## File Structure

```
codex-provider/
├── plugin.yaml                          # Plugin manifest
├── default_config.yaml                  # Default settings
├── api/
│   ├── oauth.py                         # OAuth device code + PKCE handlers
│   ├── configure.py                     # Apply settings to Agent Zero
│   └── status.py                        # Proxy health & status
├── helpers/
│   ├── oauth_manager.py                 # OAuth flow logic (device code, PKCE, refresh)
│   └── proxy_server.py                  # aiohttp proxy server
├── extensions/
│   └── python/message_loop_start/
│       └── _10_codex_proxy.py           # Auto-start extension
└── webui/
    ├── main.html                        # Plugin dashboard UI
    ├── config.html                      # Settings panel
    └── codex-store.js                   # Alpine.js frontend state
```

## Requirements

- **Agent Zero** development branch (plugin system required)
- **ChatGPT Pro, Plus, or Team subscription** (free accounts won't work)
- **Python 3.12+** with `aiohttp` (included in Agent Zero's Docker image)

## Disclaimer

This plugin uses ChatGPT's backend API (`chatgpt.com/backend-api`) and the Codex CLI's public OAuth client ID for authentication. These are the same endpoints and credentials used by [OpenAI's official Codex CLI](https://github.com/openai/codex).

**Important:**
- This is an unofficial plugin, not endorsed by OpenAI
- The backend API is undocumented and may change without notice
- Use of this plugin is subject to OpenAI's [Terms of Service](https://openai.com/policies/terms-of-use)
- Your ChatGPT subscription usage through this plugin counts against your account's rate limits
- No tokens, credentials, or user data are sent anywhere except to OpenAI's official servers

## License

MIT
