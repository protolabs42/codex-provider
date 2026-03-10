"""Status API handler — connection health, upstream test, model listing."""

import sys
from pathlib import Path
from helpers.api import ApiHandler, Request, Response

_plugin_root = Path(__file__).parent.parent
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))

from codex_helpers.proxy_server import get_proxy, ensure_running, CODEX_BASE_URL


class StatusHandler(ApiHandler):

    async def process(self, input: dict, request: Request) -> dict | Response:
        action = input.get("action", "status")

        if action == "status":
            return await self._status()
        elif action == "test":
            return await self._test_connection(input)
        elif action == "start_proxy":
            return await self._start_proxy(input)
        else:
            return {"ok": False, "error": f"Unknown action: {action}"}

    async def _status(self) -> dict:
        proxy = get_proxy()
        if proxy is None:
            return {
                "ok": True,
                "proxy_running": False,
                "message": "Proxy not started. Configure credentials and enable auto_configure.",
            }
        return {
            "ok": True,
            "proxy_running": proxy._running,
            "proxy_port": proxy.port,
            "proxy_url": proxy.base_url,
            "auth_mode": proxy.config.get("auth_mode", "api_key"),
            "upstream": CODEX_BASE_URL,
        }

    async def _test_connection(self, input: dict) -> dict:
        """Test the upstream connection with current credentials."""
        from helpers import plugins

        config = plugins.get_plugin_config("codex-provider") or {}
        if not config.get("api_key") and not config.get("oauth_access_token"):
            return {"ok": False, "error": "No credentials configured"}

        proxy = await ensure_running(config)

        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{proxy.base_url}/models",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        models = [m.get("id") for m in data.get("data", [])]
                        return {"ok": True, "models": models, "count": len(models)}
                    else:
                        text = await resp.text()
                        return {"ok": False, "error": f"HTTP {resp.status}: {text[:200]}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _start_proxy(self, input: dict) -> dict:
        """Manually start/restart the proxy with current config."""
        from helpers import plugins

        config = plugins.get_plugin_config("codex-provider") or {}
        proxy = get_proxy()
        if proxy and proxy._running:
            await proxy.stop()

        proxy = await ensure_running(config)
        return {
            "ok": True,
            "proxy_url": proxy.base_url,
            "message": "Proxy started",
        }
