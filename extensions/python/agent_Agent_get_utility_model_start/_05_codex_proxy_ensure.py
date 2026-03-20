"""Ensure the Codex proxy is running before _model_config builds the LiteLLM model.

This must run BEFORE _10_model_config.py which builds the chat model pointing at
http://127.0.0.1:8400/v1. Without the proxy listening, LiteLLM sends the local
API key directly to OpenAI and gets a 401.
"""

import sys
import asyncio
import logging
from pathlib import Path

from helpers.extension import Extension
from helpers import plugins

_plugin_root = Path(__file__).resolve().parents[3]
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))

logger = logging.getLogger("codex_provider")


class CodexProxyEnsure(Extension):

    async def execute(self, data: dict = {}, **kwargs):
        config = plugins.get_plugin_config("codex_provider", self.agent)
        if not config:
            return

        if not config.get("auto_configure", False):
            return

        has_creds = config.get("api_key") or config.get("oauth_access_token")
        if not has_creds:
            return

        from codex_helpers.proxy_server import ensure_running, get_proxy

        proxy = get_proxy()
        if proxy and proxy._running:
            return

        logger.info("Starting Codex proxy before model build...")
        await ensure_running(config)
