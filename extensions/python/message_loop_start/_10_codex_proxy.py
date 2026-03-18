"""Start the Codex proxy on first agent message loop if auto_configure is enabled."""

import sys
from pathlib import Path

from helpers.extension import Extension
from helpers import plugins
from agent import LoopData

_plugin_root = Path(__file__).resolve().parents[3]  # up from extensions/python/message_loop_start/
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))


class CodexProxyStart(Extension):

    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        config = plugins.get_plugin_config("codex_provider", self.agent)
        if not config:
            return

        if not config.get("auto_configure", False):
            return

        # Only start if credentials are present
        has_creds = config.get("api_key") or config.get("oauth_access_token")
        if not has_creds:
            return

        from codex_helpers.proxy_server import ensure_running, get_proxy

        proxy = get_proxy()
        if proxy and proxy._running:
            return  # Already running

        await ensure_running(config)
