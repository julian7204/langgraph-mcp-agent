"""OAuth 2.1 for remote MCP servers, from the client side.

The MCP SDK's `OAuthClientProvider` is an httpx.Auth that does discovery, dynamic client
registration, PKCE and refresh. We supply three things: where to keep tokens, how to open the
browser, and a loopback HTTP listener that catches the redirect with the authorization code.
"""

from __future__ import annotations

import asyncio
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl

CALLBACK_PORT = 8765
REDIRECT_URI = f"http://127.0.0.1:{CALLBACK_PORT}/callback"
CONFIG_DIR = Path.home() / ".config" / "mcp-agent"


class FileTokenStorage(TokenStorage):
    """Keeps client registration and tokens per server in a 0600 JSON file."""

    def __init__(self, server_url: str, directory: Path = CONFIG_DIR) -> None:
        host = urlparse(server_url).netloc.replace(":", "_")
        self.path = directory / f"{host}.json"

    def _read(self) -> dict[str, dict[str, object]]:
        if not self.path.exists():
            return {}
        data: dict[str, dict[str, object]] = json.loads(self.path.read_text())
        return data

    def _write(self, key: str, value: dict[str, object]) -> None:
        data = self._read()
        data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2))

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._write("tokens", tokens.model_dump(mode="json", exclude_none=True))

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._read().get("client")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._write("client", client_info.model_dump(mode="json", exclude_none=True))

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class _CallbackHandler(BaseHTTPRequestHandler):
    result: asyncio.Future[tuple[str, str | None]]
    loop: asyncio.AbstractEventLoop

    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        code, state, error = (query.get(k, [None])[0] for k in ("code", "state", "error"))
        ok = code is not None
        body = "Signed in. You can close this tab." if ok else f"Authorization failed: {error}"
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())
        if not self.result.done():
            if ok and code is not None:
                self.loop.call_soon_threadsafe(self.result.set_result, (code, state))
            else:
                self.loop.call_soon_threadsafe(
                    self.result.set_exception, RuntimeError(f"Authorization failed: {error}")
                )

    def log_message(self, format: str, *args: object) -> None:
        return


async def _wait_for_callback() -> tuple[str, str | None]:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[tuple[str, str | None]] = loop.create_future()
    handler = type("Handler", (_CallbackHandler,), {"result": future, "loop": loop})
    server = HTTPServer(("127.0.0.1", CALLBACK_PORT), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        return await asyncio.wait_for(future, timeout=300)
    finally:
        server.shutdown()


async def _open_browser(url: str) -> None:
    print(f"Opening your browser to authorize this agent:\n  {url}")
    webbrowser.open(url)


def oauth_for(server_url: str) -> OAuthClientProvider:
    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=OAuthClientMetadata(
            client_name="langgraph-mcp-agent",
            redirect_uris=[AnyUrl(REDIRECT_URI)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=FileTokenStorage(server_url),
        redirect_handler=_open_browser,
        callback_handler=_wait_for_callback,
    )
