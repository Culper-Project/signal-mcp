"""Coverage tests for signal_mcp/server.py — uncovered lines."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
import httpx

import signal_mcp.store as _store_mod
import signal_mcp.server as server_mod
from signal_mcp.config import DAEMON_URL
from signal_mcp.client import SignalClient, SignalError
from tests.conftest import call_tool, get_client, TOOLS
from mcp.types import Tool


@pytest.fixture(autouse=True)
def reset_server(monkeypatch, tmp_path):
    monkeypatch.setattr(_store_mod, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(_store_mod, "_initialized_paths", set())
    if getattr(_store_mod._thread_local, "conn", None) is not None:
        _store_mod._thread_local.conn.close()
        _store_mod._thread_local.conn = None

    test_client = SignalClient(account="+10000000000")
    monkeypatch.setattr(server_mod, "_client", test_client)

    async def noop():
        pass

    monkeypatch.setattr(test_client, "ensure_daemon", noop)
    return test_client


# ── get_client lazy init ──────────────────────────────────────────────────────

def test_get_client_lazy_init(monkeypatch):
    """get_client() creates SignalClient once and caches it."""
    monkeypatch.setattr(server_mod, "_client", None)
    mock_instance = MagicMock(spec=SignalClient)
    with patch("signal_mcp.server.SignalClient", return_value=mock_instance) as mock_cls:
        c1 = get_client()
        c2 = get_client()
    # Constructor called only once despite two get_client() calls
    mock_cls.assert_called_once()
    assert c1 is c2


# ── TOOLS list ────────────────────────────────────────────────────────────────

def test_tools_is_non_empty_list_of_tool_instances():
    assert isinstance(TOOLS, list)
    assert len(TOOLS) > 0
    for t in TOOLS:
        assert isinstance(t, Tool)


# ── Culper read-only hardening guard ──────────────────────────────────────────
# This fork must never register write/destructive/egress tools. If this test
# fails, a write tool has crept back in (e.g. via an unaudited upstream merge).

_FORBIDDEN_TOOLS = {
    "send_message", "send_group_message", "send_note_to_self", "edit_message",
    "send_attachment", "send_group_attachment", "react_to_message", "set_typing",
    "send_sticker", "send_group_sticker",
    "delete_message", "delete_group_message", "admin_delete_message",
    "send_read_receipt", "send_message_request_response",
    "block_contact", "unblock_contact", "remove_contact", "update_contact",
    "update_profile", "send_contacts_sync",
    "create_group", "join_group", "update_group", "leave_group",
    "pin_message", "unpin_message",
    "add_device", "remove_device", "update_device",
    "create_poll", "vote_poll", "terminate_poll",
    "set_expiration_timer", "trust_identity", "update_configuration",
    "update_account", "set_pin", "remove_pin",
    "start_change_number", "finish_change_number", "submit_rate_limit_challenge",
    "add_sticker_pack", "upload_sticker_pack",
    "set_webhook", "get_webhook",
    "schedule_message", "list_scheduled_messages", "cancel_scheduled_message",
    "run_scheduled_messages",
}

_ALLOWED_SEND_PREFIXED = {
    "send_sync_request",       # pulls history from own primary device
    "delete_local_messages",   # local signal-mcp store only — never touches Signal
}


def test_no_forbidden_tools_registered():
    names = {t.name for t in TOOLS}
    leaked = names & _FORBIDDEN_TOOLS
    assert not leaked, f"Write/destructive tools registered in read-only fork: {sorted(leaked)}"


def test_no_unexpected_send_tools():
    names = {t.name for t in TOOLS}
    sends = {n for n in names if n.startswith(("send_", "create_", "update_", "delete_", "remove_", "set_"))}
    assert sends <= _ALLOWED_SEND_PREFIXED, f"Unexpected mutating-looking tools: {sorted(sends - _ALLOWED_SEND_PREFIXED)}"


@pytest.mark.asyncio
async def test_forbidden_tools_not_dispatchable():
    """Even a hand-crafted call to a removed tool must hit 'Unknown tool'."""
    for name in ("send_message", "delete_message", "update_account", "set_webhook"):
        result = await call_tool(name, {"recipient": "+10000000000", "message": "x"})
        assert "Unknown tool" in result[0].text


def test_webhook_module_gone():
    import importlib.util
    assert importlib.util.find_spec("signal_mcp.webhook") is None


@pytest.mark.asyncio
async def test_list_tools_handler_returns_tools():
    """list_tools() registered handler returns the TOOLS list."""
    from signal_mcp.server import _list_tools as list_tools
    from mcp.types import RequestParams
    result = await list_tools(None, RequestParams())
    assert result.tools == TOOLS
    assert len(result.tools) > 0


# ── receive_messages handler re-raise when not background service ─────────────

@pytest.mark.asyncio
async def test_receive_messages_reraises_non_background_error(reset_server):
    """When receive_messages raises a SignalError that is NOT about background service,
    the exception is re-raised (not swallowed)."""
    client = reset_server

    async def bad_receive(**kwargs):
        raise SignalError("daemon not running")

    client.receive_messages = bad_receive

    result = await call_tool("receive_messages", {"timeout": 1})
    # The re-raised error should be caught by the outer try/except in call_tool
    # and returned as an error TextContent
    assert result[0].text.startswith("Error:")
    assert "daemon not running" in result[0].text
