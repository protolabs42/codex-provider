"""Configure API handler — apply Codex provider as A0's active model."""

import sys
from pathlib import Path
from helpers.api import ApiHandler, Request, Response

_plugin_root = Path(__file__).parent.parent
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))

from helpers.proxy_server import ensure_running


class ConfigureHandler(ApiHandler):

    async def process(self, input: dict, request: Request) -> dict | Response:
        action = input.get("action", "apply")

        if action == "apply":
            return await self._apply_to_settings(input)
        elif action == "get_models":
            return await self._get_available_models()
        else:
            return {"ok": False, "error": f"Unknown action: {action}"}

    async def _apply_to_settings(self, input: dict) -> dict:
        """Write Codex provider into A0's settings so it uses the proxy."""
        from helpers import plugins, settings as a0_settings, dotenv

        config = plugins.get_plugin_config("codex-provider") or {}
        if not config.get("api_key") and not config.get("oauth_access_token"):
            return {"ok": False, "error": "No credentials configured. Set api_key or oauth tokens first."}

        # Start proxy
        proxy = await ensure_running(config)

        chat_model = input.get("chat_model", config.get("chat_model", "gpt-5.3-codex"))
        util_model = input.get("util_model", config.get("util_model", "gpt-5.1-codex-mini"))
        browser_model = input.get("browser_model", config.get("browser_model", "gpt-5.1-codex-mini"))

        dummy_key = "sk-codex-proxy-local"

        # Use A0's settings API instead of hardcoded file paths
        a0_settings.set_settings_delta({
            "chat_model_provider": "other",
            "chat_model_name": chat_model,
            "chat_model_api_base": proxy.base_url,
            "chat_model_api_key": dummy_key,
            "util_model_provider": "other",
            "util_model_name": util_model,
            "util_model_api_base": proxy.base_url,
            "util_model_api_key": dummy_key,
            "browser_model_provider": "other",
            "browser_model_name": browser_model,
            "browser_model_api_base": proxy.base_url,
            "browser_model_api_key": dummy_key,
        })

        # Set the centralized API key so A0's banner check doesn't complain
        dotenv.save_dotenv_value("API_KEY_OTHER", dummy_key)

        return {
            "ok": True,
            "message": "Agent Zero configured to use Codex via proxy",
            "proxy_url": proxy.base_url,
            "chat_model": chat_model,
            "util_model": util_model,
            "browser_model": browser_model,
        }

    async def _get_available_models(self) -> dict:
        """Fetch OpenAI models from OpenRouter (public, no auth needed)."""
        import aiohttp

        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(
                    "https://openrouter.ai/api/v1/models",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return self._fallback_models()

                    data = await resp.json()
                    models = []
                    for m in data.get("data", []):
                        mid = m.get("id", "")
                        # Only OpenAI models, strip "openai/" prefix
                        if not mid.startswith("openai/"):
                            continue
                        short_id = mid.replace("openai/", "")
                        models.append({
                            "id": short_id,
                            "name": m.get("name", short_id),
                            "context_length": m.get("context_length", 0),
                        })

                    # Sort: codex first, then o-series, then gpt, then rest
                    def sort_key(m):
                        mid = m["id"]
                        if "codex" in mid: return (0, mid)
                        if mid.startswith("o"): return (1, mid)
                        if mid.startswith("gpt"): return (2, mid)
                        return (3, mid)
                    models.sort(key=sort_key)
                    return {"ok": True, "models": models}

        except Exception:
            return self._fallback_models()

    def _fallback_models(self) -> dict:
        """Hardcoded fallback if OpenRouter is unreachable."""
        return {
            "ok": True,
            "models": [
                {"id": "gpt-5.3-codex", "name": "GPT-5.3 Codex"},
                {"id": "gpt-5.2-codex", "name": "GPT-5.2 Codex"},
                {"id": "gpt-5.1-codex", "name": "GPT-5.1 Codex"},
                {"id": "gpt-5.1-codex-mini", "name": "GPT-5.1 Codex Mini"},
                {"id": "gpt-5.2", "name": "GPT-5.2"},
                {"id": "gpt-5.1", "name": "GPT-5.1"},
            ],
        }
