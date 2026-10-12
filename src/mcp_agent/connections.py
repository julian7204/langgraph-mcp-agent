"""Where the tools come from: the local demo server over stdio, or a remote server over HTTP + OAuth."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp import ClientSession

SERVER = "tasks"


def demo_connection(seed: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-m", "mcp_agent.demo_server"],
        "env": {"MCP_AGENT_DEMO_SEED": json.dumps(seed or [])},
    }


def http_connection(url: str) -> dict[str, Any]:
    from mcp_agent.oauth import oauth_for

    return {"transport": "streamable_http", "url": url, "auth": oauth_for(url)}


@asynccontextmanager
async def mcp_tools(connection: dict[str, Any]) -> AsyncIterator[tuple[ClientSession, list[BaseTool]]]:
    """One MCP session for the whole conversation.

    `MultiServerMCPClient.get_tools()` opens a fresh session per tool call, which would restart a
    stdio server (and lose its state) on every call. Holding one session avoids that.
    """
    client = MultiServerMCPClient({SERVER: connection})  # type: ignore[dict-item]
    async with client.session(SERVER) as session:
        yield session, await load_mcp_tools(session, server_name=SERVER)
