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
        """Return models available through the ChatGPT subscription backend.

        Only models that work via chatgpt.com/backend-api/codex/responses
        are listed. API-only models (gpt-5.4-pro, o3, etc.) are excluded
        because they require a separate OpenAI API key.
        """
        from helpers import plugins

        config = plugins.get_plugin_config("codex_provider") or {}

        # Try fetching from the running proxy first (it has the curated list)
        try:
            from codex_helpers.proxy_server import get_proxy
            proxy = get_proxy()
            if proxy and proxy._running:
                import aiohttp
                async with aiohttp.ClientSession() as http:
                    async with http.get(
                        f"http://127.0.0.1:{config.get('proxy_port', 8400)}/v1/models",
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            models = [{"id": m["id"], "name": m["id"]} for m in data.get("data", [])]
                            if models:
                                return {"ok": True, "models": models}
        except Exception:
            pass

        # Fallback: curated list of models known to work with ChatGPT subscription
        return {
            "ok": True,
            "models": [
                {"id": "gpt-5.4", "name": "GPT-5.4"},
                {"id": "gpt-5.4-mini", "name": "GPT-5.4 Mini"},
                {"id": "gpt-5.4-nano", "name": "GPT-5.4 Nano"},
                {"id": "gpt-5.3-codex", "name": "GPT-5.3 Codex"},
                {"id": "gpt-5.3-codex-spark", "name": "GPT-5.3 Codex Spark"},
                {"id": "gpt-5.2-codex", "name": "GPT-5.2 Codex"},
                {"id": "gpt-5.2", "name": "GPT-5.2"},
                {"id": "gpt-5.1-codex", "name": "GPT-5.1 Codex"},
                {"id": "gpt-5.1-codex-mini", "name": "GPT-5.1 Codex Mini"},
                {"id": "gpt-5.1", "name": "GPT-5.1"},
            ],
        }
