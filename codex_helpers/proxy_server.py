"""
Codex subscription proxy — bridges LiteLLM to ChatGPT backend Responses API.

Handles two paths:
- /v1/responses  → near-passthrough (LiteLLM sends Responses API format for Codex models)
- /v1/chat/completions → transforms chat format to Responses API format

Both route to https://chatgpt.com/backend-api/codex/responses with proper auth headers.
"""

import asyncio
import json
import logging
import threading
import time
import uuid

from aiohttp import web, ClientSession, ClientTimeout

logger = logging.getLogger("codex-provider")

_proxy_instance = None
_proxy_lock = threading.Lock()

# ChatGPT backend constants (from numman-ali/opencode-openai-codex-auth + Codex CLI)
CODEX_BASE_URL = "https://chatgpt.com/backend-api"
CODEX_RESPONSES_PATH = "/codex/responses"


class CodexProxy:
    def __init__(self, config: dict):
        self.config = config
        self.app = None
        self.runner = None
        self.session = None
        self.port = int(config.get("proxy_port", 8400))
        self._running = False

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    async def start(self):
        if self._running:
            return

        self.app = web.Application()
        self.app.router.add_get("/health", self._health)
        self.app.router.add_get("/v1/models", self._models)
        self.app.router.add_post("/v1/responses", self._responses)
        self.app.router.add_post("/v1/chat/completions", self._chat_completions)
        self.app.router.add_route("*", "/v1/{path:.*}", self._passthrough)

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", self.port)
        await site.start()
        self._running = True
        logger.info(f"Codex proxy listening on 127.0.0.1:{self.port}")

    async def stop(self):
        if self.session:
            await self.session.close()
            self.session = None
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
        self._running = False

    def _get_session(self) -> ClientSession:
        if not self.session or self.session.closed:
            timeout = ClientTimeout(total=300, connect=10)
            self.session = ClientSession(timeout=timeout)
        return self.session

    def _get_access_token(self) -> str:
        mode = self.config.get("auth_mode", "api_key")
        if mode == "oauth":
            return self.config.get("oauth_access_token", "")
        return self.config.get("api_key", "")

    def _get_account_id(self) -> str:
        """Get chatgpt-account-id from stored config or extract from JWT."""
        account_id = self.config.get("chatgpt_account_id", "")
        if account_id:
            return account_id
        # Extract from JWT on the fly
        token = self._get_access_token()
        if token:
            info = _extract_account_id_from_jwt(token)
            if info:
                self.config["chatgpt_account_id"] = info
                return info
        return ""

    def _build_codex_headers(self) -> dict:
        """Build headers for ChatGPT backend Codex API."""
        token = self._get_access_token()
        account_id = self._get_account_id()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
            "accept": "text/event-stream",
        }
        if account_id:
            headers["chatgpt-account-id"] = account_id
        return headers

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "auth_mode": self.config.get("auth_mode", "api_key"),
            "upstream": CODEX_BASE_URL,
            "account_id": bool(self._get_account_id()),
            "running": self._running,
        })

    async def _models(self, request: web.Request) -> web.Response:
        """Return available Codex models (hardcoded, no upstream fetch needed)."""
        models = [
            {"id": "gpt-5.3-codex", "object": "model", "owned_by": "openai"},
            {"id": "gpt-5.2-codex", "object": "model", "owned_by": "openai"},
            {"id": "gpt-5.1-codex", "object": "model", "owned_by": "openai"},
            {"id": "gpt-5.1-codex-mini", "object": "model", "owned_by": "openai"},
            {"id": "gpt-5.2", "object": "model", "owned_by": "openai"},
            {"id": "gpt-5.1", "object": "model", "owned_by": "openai"},
        ]
        return web.json_response({"object": "list", "data": models})

    async def _chat_completions(self, request: web.Request) -> web.Response:
        """Translate /v1/chat/completions → ChatGPT backend /codex/responses."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": {"message": "Invalid JSON body", "type": "invalid_request"}},
                status=400,
            )

        # Transform request
        codex_body = _chat_to_responses(body)
        is_streaming = body.get("stream", False)

        target_url = f"{CODEX_BASE_URL}{CODEX_RESPONSES_PATH}"
        headers = self._build_codex_headers()
        session = self._get_session()

        logger.info(f"Codex proxy → {target_url} model={codex_body.get('model')} stream={is_streaming}")

        try:
            async with session.post(
                target_url,
                json=codex_body,
                headers=headers,
            ) as resp:
                content_type = resp.headers.get("Content-Type", "")

                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"Codex API error {resp.status}: {error_text[:500]}")
                    return web.json_response(
                        {"error": {"message": f"Codex API error ({resp.status}): {error_text[:200]}", "type": "upstream_error"}},
                        status=resp.status,
                    )

                if is_streaming:
                    return await self._stream_responses_to_chat(request, resp, body)
                else:
                    return await self._collect_responses_to_chat(resp, body)

        except asyncio.TimeoutError:
            return web.json_response(
                {"error": {"message": "Upstream timeout", "type": "timeout"}},
                status=504,
            )
        except Exception as e:
            logger.exception("Proxy error")
            return web.json_response(
                {"error": {"message": str(e), "type": "proxy_error"}},
                status=502,
            )

    async def _responses(self, request: web.Request) -> web.Response:
        """Handle /v1/responses — near-passthrough for LiteLLM's Responses API calls."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": {"message": "Invalid JSON body", "type": "invalid_request"}},
                status=400,
            )

        # Augment body with required Codex fields
        body.setdefault("store", False)
        if "include" not in body:
            body["include"] = ["reasoning.encrypted_content"]
        if "reasoning" not in body:
            body["reasoning"] = {"effort": "medium", "summary": "auto"}

        # ChatGPT backend requires instructions field
        if "instructions" not in body:
            body["instructions"] = "You are a helpful assistant."

        # ChatGPT backend REQUIRES stream=true; we handle non-streaming locally
        is_streaming = body.get("stream", True)
        body["stream"] = True  # Always stream from backend
        target_url = f"{CODEX_BASE_URL}{CODEX_RESPONSES_PATH}"
        headers = self._build_codex_headers()
        session = self._get_session()

        logger.info(f"Codex proxy /v1/responses → {target_url} model={body.get('model')} stream={is_streaming}")

        try:
            async with session.post(
                target_url,
                json=body,
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"Codex API error {resp.status}: {error_text[:500]}")
                    return web.json_response(
                        {"error": {"message": f"Codex API error ({resp.status}): {error_text[:200]}", "type": "upstream_error"}},
                        status=resp.status,
                    )

                if is_streaming:
                    # Stream SSE back as-is — LiteLLM expects Responses API SSE format
                    return await self._stream_passthrough(request, resp)
                else:
                    # Collect and return the final response object
                    return await self._collect_response(resp)

        except asyncio.TimeoutError:
            return web.json_response(
                {"error": {"message": "Upstream timeout", "type": "timeout"}},
                status=504,
            )
        except Exception as e:
            logger.exception("Proxy error in /v1/responses")
            return web.json_response(
                {"error": {"message": str(e), "type": "proxy_error"}},
                status=502,
            )

    async def _stream_passthrough(self, request: web.Request, resp) -> web.StreamResponse:
        """Pass SSE stream from upstream back to client as-is."""
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await response.prepare(request)

        async for chunk in resp.content.iter_any():
            await response.write(chunk)

        await response.write_eof()
        return response

    async def _collect_response(self, resp) -> web.Response:
        """Collect SSE stream and return the final response.completed object."""
        final_response = None
        async for chunk in resp.content.iter_any():
            for line in chunk.decode("utf-8", errors="replace").split("\n"):
                line = line.strip()
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    continue
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "response.completed":
                    final_response = event.get("response", {})

        if final_response:
            return web.json_response(final_response)

        return web.json_response(
            {"error": {"message": "No response.completed event received", "type": "upstream_error"}},
            status=502,
        )

    async def _stream_responses_to_chat(self, request: web.Request, resp, orig_body: dict) -> web.StreamResponse:
        """Transform Responses API SSE stream → chat/completions SSE stream."""
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await response.prepare(request)

        chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        model = orig_body.get("model", "codex-mini-latest")
        sent_role = False
        buffer = b""

        async for chunk in resp.content.iter_any():
            buffer += chunk
            # Process complete lines
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line_str = line.decode("utf-8", errors="replace").strip()

                if not line_str.startswith("data: "):
                    continue

                data_str = line_str[6:]
                if data_str == "[DONE]":
                    # Send final stop event
                    stop_chunk = _make_chat_chunk(chat_id, model, finish_reason="stop")
                    await response.write(f"data: {json.dumps(stop_chunk)}\n\n".encode())
                    await response.write(b"data: [DONE]\n\n")
                    continue

                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type", "")

                # Send role on first output item
                if event_type == "response.output_item.added" and not sent_role:
                    role_chunk = _make_chat_chunk(chat_id, model, role="assistant")
                    await response.write(f"data: {json.dumps(role_chunk)}\n\n".encode())
                    sent_role = True

                # Stream text deltas
                elif event_type == "response.output_text.delta":
                    delta_text = event.get("delta", "")
                    if delta_text:
                        delta_chunk = _make_chat_chunk(chat_id, model, content=delta_text)
                        await response.write(f"data: {json.dumps(delta_chunk)}\n\n".encode())

                # Handle completion
                elif event_type == "response.completed":
                    if not sent_role:
                        # Extract text from completed response
                        text = _extract_text_from_response(event.get("response", {}))
                        if text:
                            role_chunk = _make_chat_chunk(chat_id, model, role="assistant")
                            await response.write(f"data: {json.dumps(role_chunk)}\n\n".encode())
                            content_chunk = _make_chat_chunk(chat_id, model, content=text)
                            await response.write(f"data: {json.dumps(content_chunk)}\n\n".encode())
                    stop_chunk = _make_chat_chunk(chat_id, model, finish_reason="stop")
                    await response.write(f"data: {json.dumps(stop_chunk)}\n\n".encode())
                    await response.write(b"data: [DONE]\n\n")

        await response.write_eof()
        return response

    async def _collect_responses_to_chat(self, resp, orig_body: dict) -> web.Response:
        """Collect full Responses API response → chat/completions JSON."""
        # Read SSE stream and collect text
        full_text = ""
        async for chunk in resp.content.iter_any():
            for line in chunk.decode("utf-8", errors="replace").split("\n"):
                line = line.strip()
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    continue
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "response.output_text.delta":
                    full_text += event.get("delta", "")
                elif event.get("type") == "response.completed":
                    text = _extract_text_from_response(event.get("response", {}))
                    if text and not full_text:
                        full_text = text

        model = orig_body.get("model", "codex-mini-latest")
        return web.json_response({
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": full_text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    async def _passthrough(self, request: web.Request) -> web.Response:
        """Pass through other /v1/ endpoints with auth."""
        return web.json_response(
            {"error": {"message": "Only /v1/chat/completions and /v1/models are supported", "type": "not_supported"}},
            status=404,
        )


# ── Request Transformation ──

def _chat_to_responses(body: dict) -> dict:
    """Transform chat/completions request body to Responses API format."""
    messages = body.get("messages", [])
    model = body.get("model", "codex-mini-latest")

    # Extract system message as instructions
    instructions = ""
    input_items = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        # Handle content as string or array
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif isinstance(part, str):
                    text_parts.append(part)
            content = "\n".join(text_parts)

        if role == "system":
            instructions = content
        elif role == "user":
            input_items.append({
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": content}],
            })
        elif role == "assistant":
            input_items.append({
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content}],
            })

    result = {
        "model": model,
        "store": False,
        "stream": True,  # Always stream from backend; proxy handles non-streaming
        "input": input_items,
        "include": ["reasoning.encrypted_content"],
        "reasoning": {"effort": "medium", "summary": "auto"},
        "text": {"verbosity": "medium"},
    }
    # ChatGPT backend requires instructions field
    result["instructions"] = instructions or "You are a helpful assistant."

    return result


# ── Response Helpers ──

def _make_chat_chunk(chat_id: str, model: str, role: str = None, content: str = None, finish_reason: str = None) -> dict:
    """Build a chat.completion.chunk SSE event."""
    delta = {}
    if role:
        delta["role"] = role
        delta["content"] = ""
    if content is not None:
        delta["content"] = content
    if finish_reason:
        delta = {}

    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }


def _extract_text_from_response(response: dict) -> str:
    """Extract text content from a completed Responses API response object."""
    output = response.get("output", [])
    texts = []
    for item in output:
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") in ("output_text", "text"):
                    texts.append(part.get("text", ""))
    return "".join(texts)


# ── JWT Helper ──

def _extract_account_id_from_jwt(token: str) -> str:
    """Extract chatgpt_account_id from JWT without verification."""
    try:
        import base64
        parts = token.split(".")
        if len(parts) < 2:
            return ""
        payload = parts[1]
        payload += "=" * (4 - len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        auth_info = data.get("https://api.openai.com/auth", {})
        return auth_info.get("chatgpt_account_id", "")
    except Exception as e:
        logger.warning(f"Failed to extract account_id from JWT: {e}")
        return ""


# ── Module API ──

def get_proxy(config: dict | None = None) -> CodexProxy | None:
    global _proxy_instance
    if config is not None:
        with _proxy_lock:
            _proxy_instance = CodexProxy(config)
    return _proxy_instance


async def ensure_running(config: dict) -> CodexProxy:
    """Get or create and start the proxy."""
    global _proxy_instance
    with _proxy_lock:
        if _proxy_instance is None:
            _proxy_instance = CodexProxy(config)
    if not _proxy_instance._running:
        await _proxy_instance.start()
    return _proxy_instance
