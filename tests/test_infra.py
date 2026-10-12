from __future__ import annotations

import asyncio
import stat
import urllib.request
from pathlib import Path

import pytest
from mcp.shared.auth import OAuthToken

from mcp_agent import oauth
from mcp_agent.llm import build_model


async def test_token_storage_round_trip_is_private(tmp_path: Path) -> None:
    storage = oauth.FileTokenStorage("https://example.com:8443/mcp", tmp_path)
    assert await storage.get_tokens() is None
    await storage.set_tokens(OAuthToken(access_token="a", token_type="Bearer", refresh_token="r"))
    loaded = await storage.get_tokens()
    assert loaded is not None and loaded.refresh_token == "r"
    assert storage.path.name == "example.com_8443.json"
    assert stat.S_IMODE(storage.path.stat().st_mode) == 0o600
    storage.clear()
    assert not storage.path.exists()


async def test_loopback_callback_returns_code_and_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oauth, "CALLBACK_PORT", 8799)
    waiter = asyncio.create_task(oauth._wait_for_callback())
    await asyncio.sleep(0.2)
    url = "http://127.0.0.1:8799/callback?code=abc&state=xyz"
    body = await asyncio.to_thread(lambda: urllib.request.urlopen(url).read().decode())
    assert "Signed in" in body
    assert await waiter == ("abc", "xyz")


def test_anthropic_request_uses_effort_and_refusal_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    model = build_model("anthropic")
    payload = model._get_request_payload("hi")  # type: ignore[attr-defined]
    assert payload["model"] == "claude-opus-5-5"
    assert payload["output_config"] == {"effort": "medium"}
    assert payload["fallbacks"] == "default"
    assert payload["betas"] == ["server-side-fallback-2026-07-01"]
