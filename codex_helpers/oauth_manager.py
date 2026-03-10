"""
OAuth manager for OpenAI Codex subscription authentication.

Supports three auth flows:
1. Device Code Flow (primary, smoothest UX)
2. Authorization Code + PKCE (redirect-based alternative)
3. Manual token import (fallback)

Token refresh is handled automatically.
"""

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path

import aiohttp

logger = logging.getLogger("codex-provider.oauth")

# Mimic Codex CLI User-Agent — Cloudflare blocks aiohttp's default UA on auth.openai.com
_HEADERS = {"User-Agent": "codex-cli/1.0"}

# OpenAI OAuth endpoints (from Codex CLI source: codex-rs/login/src/device_code_auth.rs)
DEVICE_CODE_URL = "https://auth.openai.com/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = "https://auth.openai.com/api/accounts/deviceauth/token"
TOKEN_URL = "https://auth.openai.com/oauth/token"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
REVOKE_URL = "https://auth.openai.com/oauth/revoke"
DEVICE_VERIFICATION_URI = "https://auth.openai.com/codex/device"

# Codex CLI client ID (public client)
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

# Redirect URIs
# Device code flow uses a special callback on the auth server itself
DEVICE_REDIRECT_URI = "https://auth.openai.com/deviceauth/callback"
# PKCE browser flow uses localhost redirect
PKCE_REDIRECT_URI = "http://localhost:1455/auth/callback"

# Scopes for Codex API access
SCOPES = "openid profile email offline_access"

# In-memory state for active auth flows
_active_flows = {}


class OAuthSession:
    """Represents an active device code or PKCE auth session."""

    def __init__(self):
        self.session_id = secrets.token_urlsafe(16)
        self.created_at = time.time()
        self.status = "pending"  # pending | polling | complete | error | expired
        self.error = None

        # Device code flow
        self.device_code = None
        self.user_code = None
        self.verification_uri = None
        self.verification_uri_complete = None
        self.interval = 5
        self.expires_in = 900

        # PKCE flow
        self.code_verifier = None
        self.code_challenge = None
        self.state = None
        self.redirect_uri = None

        # Result
        self.access_token = None
        self.refresh_token = None
        self.id_token = None
        self.expires_at = None
        self.account_info = None

    def to_dict(self) -> dict:
        """Public-safe representation (no tokens in status responses)."""
        d = {
            "session_id": self.session_id,
            "status": self.status,
            "error": self.error,
            "interval": self.interval,
        }
        if self.status in ("pending", "polling"):
            d["user_code"] = self.user_code
            d["verification_uri"] = self.verification_uri
            d["verification_uri_complete"] = self.verification_uri_complete
            d["expires_in"] = max(0, int(self.expires_in - (time.time() - self.created_at)))
        if self.status == "complete":
            d["account_info"] = self.account_info
        return d

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > self.expires_in


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    import base64
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _extract_account_info(access_token: str) -> dict | None:
    """Extract account info from the JWT access token (without verification)."""
    try:
        import base64
        parts = access_token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        # Add padding
        payload += "=" * (4 - len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        auth_info = data.get("https://api.openai.com/auth", {})
        profile_info = data.get("https://api.openai.com/profile", {})
        return {
            "email": profile_info.get("email", ""),
            "plan": auth_info.get("chatgpt_plan_type", "unknown"),
            "account_id": auth_info.get("chatgpt_account_id", ""),
            "user_id": auth_info.get("chatgpt_user_id", ""),
            "expires_at": data.get("exp"),
        }
    except Exception as e:
        logger.warning(f"Failed to extract account info: {e}")
        return None


async def start_device_flow() -> OAuthSession:
    """Start a device code authorization flow via Codex's custom endpoint."""
    session = OAuthSession()

    async with aiohttp.ClientSession() as http:
        # Per Codex CLI source: only client_id, no scope/audience
        # Scopes are server-assigned based on client_id registration
        async with http.post(
            DEVICE_CODE_URL,
            json={"client_id": CLIENT_ID},
            headers={**_HEADERS, "Content-Type": "application/json"},
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                session.status = "error"
                session.error = f"Device code request failed ({resp.status}): {text[:200]}"
                return session

            data = await resp.json()
            session.device_code = data.get("device_auth_id", "")
            session.user_code = data.get("user_code", "")
            session.verification_uri = DEVICE_VERIFICATION_URI
            session.verification_uri_complete = DEVICE_VERIFICATION_URI
            session.interval = int(data.get("interval", 5))
            # expires_at is ISO timestamp; convert to seconds from now
            expires_at = data.get("expires_at", "")
            if expires_at:
                from datetime import datetime, timezone
                try:
                    exp_dt = datetime.fromisoformat(expires_at)
                    session.expires_in = max(60, (exp_dt - datetime.now(timezone.utc)).total_seconds())
                except Exception:
                    session.expires_in = 900
            else:
                session.expires_in = data.get("expires_in", 900)

    _active_flows[session.session_id] = session
    return session


def _get_error_code(data: dict) -> str:
    """Extract error code from OpenAI's nested error response format.
    Handles both flat ('error': 'string') and nested ('error': {'code': '...'})."""
    err = data.get("error")
    if isinstance(err, dict):
        return err.get("code", err.get("type", ""))
    return str(err) if err else ""


async def poll_device_flow(session_id: str) -> OAuthSession | None:
    """Poll for device code completion. Two-phase: get auth code, then exchange for tokens."""
    session = _active_flows.get(session_id)
    if not session:
        return None

    if session.is_expired:
        session.status = "expired"
        session.error = "Authentication session expired. Please start a new one."
        return session

    if session.status in ("complete", "error", "expired"):
        return session

    session.status = "polling"

    async with aiohttp.ClientSession() as http:
        # Phase 1: Poll for authorization code
        async with http.post(
            DEVICE_TOKEN_URL,
            json={
                "client_id": CLIENT_ID,
                "device_auth_id": session.device_code,
                "user_code": session.user_code,
            },
            headers={**_HEADERS, "Content-Type": "application/json"},
        ) as resp:
            data = await resp.json()
            error_code = _get_error_code(data)

            if resp.status == 200:
                logger.debug(f"Device poll success keys: {list(data.keys())}")
                auth_code = data.get("authorization_code", "")
                code_verifier = data.get("code_verifier", "")

                if not auth_code:
                    # Fallback: maybe tokens are returned directly
                    if data.get("access_token"):
                        session.access_token = data["access_token"]
                        session.refresh_token = data.get("refresh_token")
                        session.id_token = data.get("id_token")
                        session.expires_at = time.time() + data.get("expires_in", 86400)
                        session.account_info = _extract_account_info(session.access_token)
                        session.status = "complete"
                        return session
                    session.status = "error"
                    session.error = "Unexpected response from auth server"
                    return session

                # Phase 2: Exchange authorization code for tokens
                # Per Codex CLI source (codex-rs/login/src/device_code_auth.rs):
                # Device flow uses auth server's own callback as redirect_uri
                code_challenge = data.get("code_challenge", "")
                exchange_body = (
                    f"grant_type=authorization_code"
                    f"&code={auth_code}"
                    f"&redirect_uri={DEVICE_REDIRECT_URI}"
                    f"&client_id={CLIENT_ID}"
                    f"&code_verifier={code_verifier}"
                )
                logger.debug(f"Token exchange: POST {TOKEN_URL}")
                async with http.post(
                    TOKEN_URL,
                    data=exchange_body,
                    headers={**_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
                ) as token_resp:
                    if token_resp.status == 200:
                        tokens = await token_resp.json()
                        logger.debug(f"Token exchange OK")
                        session.access_token = tokens.get("access_token")
                        session.refresh_token = tokens.get("refresh_token")
                        session.id_token = tokens.get("id_token")
                        session.expires_at = time.time() + tokens.get("expires_in", 86400)
                        session.account_info = _extract_account_info(session.access_token)
                        session.status = "complete"
                    else:
                        text = await token_resp.text()
                        logger.error(f"Token exchange failed ({token_resp.status})")
                        # Exchange failed — fall back: save the JWT from the poll
                        # (it may still be usable even without exchange)
                        session.status = "error"
                        session.error = f"Token exchange failed ({token_resp.status}): {text[:200]}"

            elif error_code in ("deviceauth_authorization_unknown", "authorization_pending"):
                # User hasn't entered the code yet — keep polling
                session.status = "pending"

            elif error_code == "slow_down":
                session.interval = min(session.interval + 2, 30)
                session.status = "pending"

            elif error_code in ("expired_token", "deviceauth_expired"):
                session.status = "expired"
                session.error = "Session expired. Please start a new sign-in."

            elif error_code == "access_denied":
                session.status = "error"
                session.error = "Access denied. Please try again."

            else:
                err = data.get("error", {})
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                session.status = "error"
                session.error = msg or "Unknown error"

    return session


def start_pkce_flow() -> OAuthSession:
    """Start a PKCE authorization code flow. Uses fixed localhost redirect_uri."""
    session = OAuthSession()
    session.code_verifier, session.code_challenge = _generate_pkce()
    session.state = secrets.token_urlsafe(32)
    session.redirect_uri = PKCE_REDIRECT_URI
    _active_flows[session.session_id] = session
    return session


def get_pkce_authorize_url(session: OAuthSession) -> str:
    """Get the authorization URL for the PKCE flow."""
    from urllib.parse import urlencode
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": session.redirect_uri,
        "scope": SCOPES,
        "state": session.state,
        "code_challenge": session.code_challenge,
        "code_challenge_method": "S256",
        "audience": "https://api.openai.com/v1",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_pkce_code(session_id: str, code: str, state: str) -> OAuthSession | None:
    """Exchange authorization code for tokens (PKCE flow)."""
    session = _active_flows.get(session_id)
    if not session:
        return None

    if session.state != state:
        session.status = "error"
        session.error = "State mismatch - possible CSRF attack"
        return session

    async with aiohttp.ClientSession() as http:
        async with http.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": session.redirect_uri,
            "code_verifier": session.code_verifier,
        }, headers=_HEADERS) as resp:
            if resp.status == 200:
                data = await resp.json()
                session.access_token = data.get("access_token")
                session.refresh_token = data.get("refresh_token")
                session.id_token = data.get("id_token")
                expires_in = data.get("expires_in", 86400)
                session.expires_at = time.time() + expires_in
                session.account_info = _extract_account_info(session.access_token)
                session.status = "complete"
            else:
                text = await resp.text()
                session.status = "error"
                session.error = f"Token exchange failed ({resp.status}): {text[:200]}"

    return session


async def refresh_access_token(refresh_token: str) -> dict | None:
    """Refresh an expired access token."""
    async with aiohttp.ClientSession() as http:
        async with http.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
        }, headers=_HEADERS) as resp:
            if resp.status == 200:
                data = await resp.json()
                return {
                    "access_token": data.get("access_token"),
                    "refresh_token": data.get("refresh_token", refresh_token),
                    "expires_at": time.time() + data.get("expires_in", 86400),
                }
            else:
                text = await resp.text()
                logger.error(f"Token refresh failed ({resp.status}): {text[:200]}")
                return None


async def revoke_token(token: str) -> bool:
    """Revoke an access or refresh token."""
    async with aiohttp.ClientSession() as http:
        async with http.post(REVOKE_URL, data={
            "client_id": CLIENT_ID,
            "token": token,
        }, headers=_HEADERS) as resp:
            return resp.status == 200


def parse_authorization_input(raw: str) -> tuple[str, str | None]:
    """Parse pasted authorization input — URL, code#state, query string, or raw code.
    Returns (code, state_or_None)."""
    from urllib.parse import urlparse, parse_qs
    raw = raw.strip()

    # Full redirect URL: http://localhost:1455/auth/callback?code=...&state=...
    if raw.startswith("http"):
        parsed = urlparse(raw)
        qs = parse_qs(parsed.query)
        code = qs.get("code", [""])[0]
        state = qs.get("state", [None])[0]
        if code:
            return code, state

    # code#state format
    if "#" in raw and len(raw.split("#")) == 2:
        parts = raw.split("#")
        return parts[0], parts[1]

    # Query string: code=...&state=...
    if "code=" in raw:
        from urllib.parse import parse_qs
        qs = parse_qs(raw)
        code = qs.get("code", [""])[0]
        state = qs.get("state", [None])[0]
        if code:
            return code, state

    # Raw authorization code
    return raw, None


def get_session(session_id: str) -> OAuthSession | None:
    return _active_flows.get(session_id)


def cleanup_expired():
    """Remove expired sessions from memory."""
    expired = [k for k, v in _active_flows.items() if v.is_expired]
    for k in expired:
        del _active_flows[k]
