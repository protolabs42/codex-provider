"""OAuth API handler — device code flow, PKCE callback, token management."""

import json
import sys
import time
from pathlib import Path
from helpers.api import ApiHandler, Request, Response

_plugin_root = Path(__file__).parent.parent
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))

from helpers import oauth_manager
from helpers.proxy_server import ensure_running, get_proxy


class OAuthHandler(ApiHandler):

    @classmethod
    def get_methods(cls) -> list[str]:
        return ["POST", "GET"]

    async def process(self, input: dict, request: Request) -> dict | Response:
        action = input.get("action", "")

        # GET requests are for the OAuth callback
        if request.method == "GET":
            return await self._handle_callback(request)

        if action == "start_device_flow":
            return await self._start_device_flow()
        elif action == "poll":
            return await self._poll_device_flow(input)
        elif action == "start_pkce_flow":
            return await self._start_pkce_flow(input, request)
        elif action == "exchange_manual_code":
            return await self._exchange_manual_code(input)
        elif action == "save_tokens":
            return await self._save_tokens(input)
        elif action == "refresh":
            return await self._refresh_token()
        elif action == "disconnect":
            return await self._disconnect()
        elif action == "connection_status":
            return await self._connection_status()
        else:
            return {"ok": False, "error": f"Unknown action: {action}"}

    async def _start_device_flow(self) -> dict:
        """Start a device code flow. Returns user_code and verification URL."""
        oauth_manager.cleanup_expired()
        session = await oauth_manager.start_device_flow()

        if session.status == "error":
            return {"ok": False, "error": session.error}

        return {
            "ok": True,
            "session_id": session.session_id,
            "user_code": session.user_code,
            "verification_uri": session.verification_uri,
            "verification_uri_complete": session.verification_uri_complete,
            "interval": session.interval,
            "expires_in": session.expires_in,
        }

    async def _poll_device_flow(self, input: dict) -> dict:
        """Poll for device code completion."""
        session_id = input.get("session_id", "")
        if not session_id:
            return {"ok": False, "error": "Missing session_id"}

        session = await oauth_manager.poll_device_flow(session_id)
        if not session:
            return {"ok": False, "error": "Session not found or expired"}

        result = {"ok": True, **session.to_dict()}

        # If complete, save tokens and configure
        if session.status == "complete":
            await self._persist_tokens(session)
            result["message"] = "Connected successfully!"

        return result

    async def _start_pkce_flow(self, input: dict, request: Request) -> dict:
        """Start a PKCE authorization code flow with localhost redirect."""
        session = oauth_manager.start_pkce_flow()
        auth_url = oauth_manager.get_pkce_authorize_url(session)

        return {
            "ok": True,
            "session_id": session.session_id,
            "auth_url": auth_url,
        }

    async def _exchange_manual_code(self, input: dict) -> dict:
        """Exchange a manually pasted authorization code/URL for tokens."""
        session_id = input.get("session_id", "")
        raw_input = input.get("code", "")

        if not session_id or not raw_input:
            return {"ok": False, "error": "Missing session_id or code"}

        code, state = oauth_manager.parse_authorization_input(raw_input)
        if not code:
            return {"ok": False, "error": "Could not extract authorization code from input"}

        session = oauth_manager.get_session(session_id)
        if not session:
            return {"ok": False, "error": "Session expired. Please start a new sign-in."}

        # If state was parsed from input, validate it
        if state and state != session.state:
            return {"ok": False, "error": "State mismatch. Please try again."}
        # Use the session's state for the exchange
        result = await oauth_manager.exchange_pkce_code(session.session_id, code, session.state)
        if result and result.status == "complete":
            await self._persist_tokens(result)
            return {
                "ok": True,
                "message": "Connected!",
                "account_info": result.account_info,
            }
        else:
            err = result.error if result else "Unknown error"
            return {"ok": False, "error": err}

    async def _handle_callback(self, request: Request) -> dict | Response:
        """Handle OAuth redirect callback (PKCE flow)."""
        code = request.args.get("code", "")
        state = request.args.get("state", "")
        error = request.args.get("error", "")

        if error:
            return Response(
                f"""<html><body><h2>Authentication Failed</h2>
                <p>{error}: {request.args.get('error_description', '')}</p>
                <script>window.close();</script></body></html>""",
                content_type="text/html",
            )

        # Find the session by state
        session = None
        for s in oauth_manager._active_flows.values():
            if s.state == state:
                session = s
                break

        if not session:
            return Response(
                """<html><body><h2>Session Expired</h2>
                <p>Please try connecting again.</p>
                <script>window.close();</script></body></html>""",
                content_type="text/html",
            )

        result = await oauth_manager.exchange_pkce_code(session.session_id, code, state)
        if result and result.status == "complete":
            await self._persist_tokens(result)
            email = result.account_info.get("email", "") if result.account_info else ""
            plan = result.account_info.get("plan", "") if result.account_info else ""
            return Response(
                f"""<html><body style="background:#0d0d1a;color:#e0e0e0;font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;">
                <div style="text-align:center;">
                <h2 style="color:#4ade80;">Connected!</h2>
                <p>{email} ({plan} plan)</p>
                <p style="opacity:0.6;">This window will close automatically.</p>
                </div>
                <script>
                if (window.opener) window.opener.postMessage({{type:'codex-connected'}}, '*');
                setTimeout(() => window.close(), 2000);
                </script></body></html>""",
                content_type="text/html",
            )
        else:
            err = result.error if result else "Unknown error"
            return Response(
                f"""<html><body><h2>Connection Failed</h2><p>{err}</p>
                <script>window.close();</script></body></html>""",
                content_type="text/html",
            )

    async def _save_tokens(self, input: dict) -> dict:
        """Manually save tokens (import from auth.json or paste)."""
        access_token = input.get("access_token", "")
        refresh_token = input.get("refresh_token", "")
        auth_json = input.get("auth_json")

        if auth_json:
            if isinstance(auth_json, str):
                auth_json = json.loads(auth_json)
            tokens = auth_json.get("tokens", {})
            access_token = tokens.get("access_token", access_token)
            refresh_token = tokens.get("refresh_token", refresh_token)

        if not access_token:
            return {"ok": False, "error": "No access token provided"}

        # Create a mock session to persist
        session = oauth_manager.OAuthSession()
        session.access_token = access_token
        session.refresh_token = refresh_token
        session.account_info = oauth_manager._extract_account_info(access_token)
        session.expires_at = session.account_info.get("expires_at") if session.account_info else None
        session.status = "complete"

        await self._persist_tokens(session)
        return {
            "ok": True,
            "message": "Tokens saved successfully",
            "account_info": session.account_info,
        }

    async def _refresh_token(self) -> dict:
        """Refresh the OAuth access token."""
        from helpers import plugins

        config = plugins.get_plugin_config("codex-provider") or {}
        refresh_token = config.get("oauth_refresh_token", "")
        if not refresh_token:
            return {"ok": False, "error": "No refresh token stored"}

        result = await oauth_manager.refresh_access_token(refresh_token)
        if not result:
            return {"ok": False, "error": "Token refresh failed. Please reconnect."}

        config["oauth_access_token"] = result["access_token"]
        config["oauth_refresh_token"] = result["refresh_token"]
        config["token_expires_at"] = result["expires_at"]
        plugins.save_plugin_config("codex-provider", "", "", config)

        # Update running proxy
        proxy = get_proxy()
        if proxy:
            proxy.config = config

        return {"ok": True, "message": "Token refreshed", "expires_at": result["expires_at"]}

    async def _disconnect(self) -> dict:
        """Disconnect — revoke tokens and clear config."""
        from helpers import plugins

        config = plugins.get_plugin_config("codex-provider") or {}

        # Revoke tokens
        if config.get("oauth_refresh_token"):
            await oauth_manager.revoke_token(config["oauth_refresh_token"])
        if config.get("oauth_access_token"):
            await oauth_manager.revoke_token(config["oauth_access_token"])

        # Clear auth from config
        config["oauth_access_token"] = ""
        config["oauth_refresh_token"] = ""
        config["token_expires_at"] = None
        config["auth_mode"] = "none"
        config["auto_configure"] = False
        plugins.save_plugin_config("codex-provider", "", "", config)

        # Stop proxy
        proxy = get_proxy()
        if proxy and proxy._running:
            await proxy.stop()

        return {"ok": True, "message": "Disconnected. Tokens revoked."}

    async def _connection_status(self) -> dict:
        """Get current connection status."""
        from helpers import plugins

        config = plugins.get_plugin_config("codex-provider") or {}
        access_token = config.get("oauth_access_token", "")
        has_refresh = bool(config.get("oauth_refresh_token", ""))
        expires_at = config.get("token_expires_at")

        if not access_token:
            return {"ok": True, "connected": False}

        account_info = oauth_manager._extract_account_info(access_token)
        expired = expires_at and time.time() > expires_at

        return {
            "ok": True,
            "connected": True,
            "expired": expired,
            "account_info": account_info,
            "has_refresh_token": has_refresh,
            "expires_at": expires_at,
        }

    async def _persist_tokens(self, session: oauth_manager.OAuthSession):
        """Save tokens to plugin config and start proxy."""
        from helpers import plugins

        config = plugins.get_plugin_config("codex-provider") or {}
        config["auth_mode"] = "oauth"
        config["oauth_access_token"] = session.access_token
        config["oauth_refresh_token"] = session.refresh_token or ""
        config["token_expires_at"] = session.expires_at
        config["auto_configure"] = True

        # Extract chatgpt_account_id from JWT (required for ChatGPT backend API)
        if session.access_token:
            from helpers.proxy_server import _extract_account_id_from_jwt
            account_id = _extract_account_id_from_jwt(session.access_token)
            if account_id:
                config["chatgpt_account_id"] = account_id

        plugins.save_plugin_config("codex-provider", "", "", config)

        # Start/restart proxy with new tokens
        proxy = get_proxy()
        if proxy and proxy._running:
            await proxy.stop()
        await ensure_running(config)
