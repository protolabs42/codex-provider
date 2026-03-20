"""Configure API handler — apply Codex provider as A0's active model."""

import sys
from pathlib import Path
from helpers.api import ApiHandler, Request, Response

_plugin_root = Path(__file__).parent.parent
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))

from codex_helpers.proxy_server import ensure_running


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
        """Write Codex provider into _model_config plugin so A0 uses the proxy."""
        from helpers import plugins, dotenv

        config = plugins.get_plugin_config("codex_provider") or {}
        if not config.get("api_key") and not config.get("oauth_access_token"):
            return {"ok": False, "error": "No credentials configured. Set api_key or oauth tokens first."}

        # Start proxy
        proxy = await ensure_running(config)

        chat_model = input.get("chat_model", config.get("chat_model", "gpt-5.2-codex"))
        util_model = input.get("util_model", config.get("util_model", "gpt-5.1-codex-mini"))
        browser_model = input.get("browser_model", config.get("browser_model", "gpt-5.1-codex-mini"))

        # Write to _model_config plugin (the current model system on dev branch)
        mc_config = plugins.get_plugin_config("_model_config") or {}
        mc_config.setdefault("chat_model", {})
        mc_config["chat_model"]["provider"] = "other"
        mc_config["chat_model"]["name"] = chat_model
        mc_config["chat_model"]["api_base"] = proxy.base_url

        mc_config.setdefault("utility_model", {})
        mc_config["utility_model"]["provider"] = "other"
        mc_config["utility_model"]["name"] = util_model
        mc_config["utility_model"]["api_base"] = proxy.base_url

        plugins.save_plugin_config("_model_config", "", "", mc_config)

        # Set the centralized API key so LiteLLM and banner check work
        dotenv.save_dotenv_value("API_KEY_OTHER", "sk-codex-proxy-local")

        # Save selected models back to codex_provider config
        config["chat_model"] = chat_model
        config["util_model"] = util_model
        config["browser_model"] = browser_model
        plugins.save_plugin_config("codex_provider", "", "", config)

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
