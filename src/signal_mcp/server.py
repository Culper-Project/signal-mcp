"""MCP server exposing all Signal tools to Claude."""

import asyncio
import json
from datetime import datetime

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    RequestParams,
    TextContent,
    Tool,
)

from .client import SignalClient, SignalError
from .config import check_signal_cli_version, is_service_installed
from . import store as _store

app = Server("signal-mcp")

_client: SignalClient | None = None

# Tools that don't need the signal-cli daemon (read from local store only)
_DAEMON_FREE = {
    "import_desktop", "sync_desktop", "store_stats",
    "get_conversation", "search_messages", "get_own_number",
    "list_attachments", "get_attachment",
    "clear_local_store", "delete_local_messages", "export_messages",
    "prune_store", "mark_as_unread",
}
# Tools NOT in _DAEMON_FREE call ensure_daemon() automatically before executing.
# get_unread calls _freshen_store() (which may call receive_messages) if no
# background service is running.
# list_accounts, list_conversations, get_configuration etc. call signal-cli JSON-RPC.


def get_client() -> SignalClient:
    global _client
    if _client is None:
        _client = SignalClient()
    return _client


def _ok(data) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, indent=2, default=str))])


def _err(msg: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=f"Error: {msg}")], is_error=True)


def _require(arguments: dict, *keys: str) -> str | None:
    """Return an error string if any required key is missing, else None."""
    missing = [k for k in keys if k not in arguments]
    if missing:
        return f"Missing required parameter(s): {', '.join(missing)}"
    return None


# ── Tool definitions ───────────────────────────────────────────────────────────

TOOLS = [
    Tool(
        name="receive_messages",
        description=(
            "Manually poll signal-cli for new messages and store them. "
            "Prefer get_unread — it does this automatically and returns results in one call. "
            "Use receive_messages only if you want to poll without reading results."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "timeout": {"type": "integer", "description": "Seconds to wait for messages (default: 5)", "default": 5},
            },
        },
    ),
    Tool(
        name="list_contacts",
        description=(
            "List all Signal contacts known to this account, including names and phone numbers. "
            "Use the optional search parameter to filter by name or number substring. "
            "Returns contacts from signal-cli's local contact store. "
            "Use get_profile to fetch the current Signal profile for a specific contact."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Filter contacts by name or number (case-insensitive substring match)"},
            },
        },
    ),
    Tool(
        name="list_groups",
        description=(
            "List all Signal groups this account belongs to, including group name, ID, members, and admin list. "
            "The group_id returned here is required for send_group_message, send_group_attachment, and update_group. "
            "Use update_group to modify a group, or leave_group to exit."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_conversation",
        description="Get recent message history with a contact or group from local store. Automatically marks returned messages as read in the local store (does NOT send a Signal read receipt — call send_read_receipt for that).",
        inputSchema={
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Phone number or group ID"},
                "limit": {"type": "integer", "description": "Max messages to return (default: 50)", "default": 50},
                "offset": {"type": "integer", "description": "Number of messages to skip for pagination (default: 0)", "default": 0},
                "since": {"type": "string", "description": "Only messages after this ISO datetime (e.g. 2024-01-01T00:00:00)"},
            },
            "required": ["recipient"],
        },
    ),
    Tool(
        name="search_messages",
        description=(
            "Full-text search across all locally stored messages by keyword or phrase. "
            "Searches message bodies using SQLite FTS — results are ranked by relevance. "
            "Only messages in the local store are searchable; messages never received on this device are excluded. "
            "Use sender to narrow results to a specific conversation. "
            "Use limit and offset to paginate through large result sets. "
            "Use when looking for a specific message or topic across all Signal conversations. "
            "Do NOT use to browse a conversation chronologically — use get_conversation for that."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword or phrase to search for"},
                "sender": {"type": "string", "description": "Filter results to messages from this phone number (E.164)"},
                "limit": {"type": "integer", "description": "Maximum results to return (default 50)"},
                "offset": {"type": "integer", "description": "Skip this many results for pagination (default 0)", "default": 0},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="get_profile",
        description=(
            "Fetch the Signal profile for a contact, including their display name, about text, and avatar. "
            "Profile data is fetched live from the Signal network (not local cache). "
            "Use this to verify a contact's current name or check if they have a profile set up. "
            "Use update_profile to update your own profile."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "number": {"type": "string", "description": "Phone number in E.164 format"},
            },
            "required": ["number"],
        },
    ),
    Tool(
        name="list_devices",
        description=(
            "List all devices currently linked to your Signal account, including the primary device and any linked secondaries. "
            "Returns each device's ID, name, and last-seen timestamp. "
            "Device ID 1 is always the primary device (your registered phone). "
            "Use the returned device_id values with update_device (rename), remove_device (unlink). "
            "Use when auditing which devices have access to your Signal account, "
            "or to find the ID of a device you want to rename or remove."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_own_number",
        description="Get your own Signal phone number (the account this server is running as)",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="store_stats",
        description="Get statistics about locally stored messages (count, unread count, DB size on disk, date range)",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_unread",
        description=(
            "Get new unread messages. If the background service (signal-mcp install-service) is running, "
            "reads directly from the local store. Otherwise polls signal-cli first to fetch any messages "
            "that arrived since the last check, then returns unread. Always use this to check for new messages. "
            "Messages are marked as read after retrieval. Response includes has_more=true if more unread messages "
            "exist beyond the limit — call again with a higher limit or paginate."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max messages to return (default: 50)", "default": 50},
            },
        },
    ),
    Tool(
        name="import_desktop",
        description="Full one-time import of all historical messages from Signal Desktop (macOS/Linux). Requires sqlcipher. On macOS prompts for Keychain access; on Linux uses libsecret/GNOME Keyring. For ongoing sync use sync_desktop instead.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="sync_desktop",
        description="Incremental sync from Signal Desktop: imports only messages newer than the last sync. Fast on repeat calls. On first call behaves like import_desktop (imports everything). Requires sqlcipher.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_conversations",
        description=(
            "List all conversations (both direct and group) ordered by most recent message. "
            "Returns contact/group name, phone number or group_id, last message preview, timestamp, and unread count. "
            "Use this to get an inbox overview before reading specific conversations with get_conversation. "
            "Contact and group names are resolved from local signal-cli contacts and groups. "
            "Use get_unread to fetch only unread messages across all conversations. "
            "Do NOT use this to read message history — use get_conversation for that."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_user_status",
        description=(
            "Check whether one or more phone numbers are registered Signal users. "
            "Queries Signal's servers for each number and returns a registered/unregistered status. "
            "Accepts a list so you can batch-check multiple numbers in a single call. "
            "Useful before sending to an unknown number to avoid 'unregistered user' delivery failures. "
            "Note: privacy-mode accounts or numbers that have opted out of discoverability may show as unregistered "
            "even if they actively use Signal. "
            "Use before sending to a new contact to confirm they are reachable on Signal. "
            "Do NOT use to look up contact profile details — use get_profile for that."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "recipients": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of phone numbers (E.164) to check",
                },
            },
            "required": ["recipients"],
        },
    ),
    Tool(
        name="send_sync_request",
        description=(
            "Request a full sync of messages, contacts, and groups from your primary Signal device to this linked device. "
            "Signal's linked-device architecture stores history on the primary device; a sync pulls that data here. "
            "Use when list_conversations shows no history, list_contacts returns fewer contacts than expected, "
            "or list_groups is missing groups that exist on your phone. "
            "The sync is asynchronous — data arrives in the background over the next few seconds. "
            "Do NOT use to receive new incoming messages — use receive_messages for that."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="mark_as_unread",
        description=(
            "Mark one or more messages as unread in the local signal-mcp store. "
            "This updates only the local database — it does not affect read receipts already sent "
            "to the sender, nor does it change how messages appear on other devices. "
            "message_ids are the internal signal-mcp IDs returned by get_conversation or search_messages. "
            "Messages marked unread are returned by get_unread on the next call. "
            "Use when you want to flag a message for follow-up later."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "message_ids": {"type": "array", "items": {"type": "string"}, "description": "List of message IDs to mark as unread"},
            },
            "required": ["message_ids"],
        },
    ),
    Tool(
        name="get_avatar",
        description=(
            "Retrieve the profile photo for a contact or group as base64-encoded image data. "
            "Pass a phone number (E.164) for contacts or a group ID (from list_groups) for groups. "
            "Returns raw image bytes encoded as base64 — decode to get a JPEG or PNG. "
            "Returns an error if no avatar is set for the identifier. "
            "Use get_profile to also read name and about text alongside the avatar. "
            "Use update_profile with avatar_path to set your own profile photo."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "Phone number (E.164) for a contact or group ID for a group"},
            },
            "required": ["identifier"],
        },
    ),
    Tool(
        name="list_identities",
        description=(
            "List the Signal identity keys (safety numbers) and trust levels for one or all contacts. "
            "Each contact has a unique identity key; Signal uses these to verify end-to-end encryption integrity. "
            "Trust levels: TRUSTED_VERIFIED (manually verified), TRUSTED_UNVERIFIED (trusted on first use, TOFU), "
            "or UNTRUSTED (key changed — sending is blocked until re-trusted). "
            "Omit number to inspect all stored identities; provide number to filter to a specific contact. "
            "Use before calling trust_identity to check the current trust state and key fingerprint. "
            "Use when Signal reports 'safety number changed' to identify which contact needs re-verification. "
            "Do NOT use to trust or change trust levels — use trust_identity for that."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "number": {"type": "string", "description": "Filter to a specific contact (optional)"},
            },
        },
    ),
]


TOOLS += [
    Tool(
        name="clear_local_store",
        description="Delete ALL locally stored messages from the signal-mcp database. This does NOT delete messages from Signal — only from the local store. Requires confirm=true.",
        inputSchema={
            "type": "object",
            "properties": {
                "confirm": {"type": "boolean", "description": "Must be true to proceed — prevents accidental deletion"},
            },
            "required": ["confirm"],
        },
    ),
    Tool(
        name="delete_local_messages",
        description="Delete locally stored messages for one contact or group. Does NOT unsend from Signal — only removes from local store.",
        inputSchema={
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Phone number or group ID whose messages to delete"},
            },
            "required": ["recipient"],
        },
    ),
    Tool(
        name="export_messages",
        description=(
            "Export locally stored messages as a JSON or CSV string for archiving, analysis, or migration. "
            "Returns all messages in the local store by default; use recipient to restrict to one conversation. "
            "Use since (ISO 8601 datetime) to export only messages after a given point in time. "
            "JSON output preserves all fields (sender, timestamp, body, group_id); "
            "CSV output is flat and suitable for spreadsheets. "
            "Only messages already in the local store are included — messages never received on this device are absent. "
            "Use when you need a full or filtered dump of conversation history in machine-readable form. "
            "Do NOT use to read individual messages interactively — use get_conversation or search_messages for that."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "format": {"type": "string", "enum": ["json", "csv"], "description": "Output format (default: json)"},
                "recipient": {"type": "string", "description": "Export only this conversation (phone number or group ID)"},
                "since": {"type": "string", "description": "Only include messages at or after this ISO datetime"},
            },
        },
    ),
    Tool(
        name="get_configuration",
        description="Get current Signal account configuration (read receipts, typing indicators, link previews)",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_sticker_packs",
        description=(
            "List all sticker packs installed on this Signal account. "
            "Returns pack_id and sticker_id values needed for send_sticker and send_group_sticker. "
            "Use add_sticker_pack to install a new pack from a signal.art URL."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_attachments",
        description=(
            "List all Signal attachments that have been downloaded and saved to the local store. "
            "Returns filenames, MIME types, file sizes, and the associated message timestamp for each attachment. "
            "Only attachments explicitly downloaded (via receive_messages or import) appear here — "
            "attachments not yet fetched from Signal's servers are not listed. "
            "Use the returned filename with get_attachment to retrieve the actual file content. "
            "Use when you need to discover what media files are available locally before reading them. "
            "Do NOT use to download new attachments from Signal servers — use receive_messages for that."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_attachment",
        description=(
            "Retrieve metadata and the base64-encoded content of a locally saved Signal attachment by filename. "
            "Returns MIME type, file size, local path, and the raw bytes as base64 so the caller can read or display the file. "
            "Only attachments already downloaded to the local store are accessible — "
            "attachments expire on Signal's servers after ~30 days if not downloaded first. "
            "Use list_attachments to discover available filenames before calling. "
            "Use when you need to read, display, or forward the contents of a received file or image. "
            "Do NOT use to send an attachment — use send_attachment or send_group_attachment for that."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Attachment filename (get from list_attachments)"},
            },
            "required": ["filename"],
        },
    ),
    Tool(
        name="get_sticker",
        description="Retrieve a single sticker image as base64. Use list_sticker_packs to find pack_id and sticker_id values.",
        inputSchema={
            "type": "object",
            "properties": {
                "pack_id": {"type": "string", "description": "Sticker pack ID (hex string from list_sticker_packs)"},
                "sticker_id": {"type": "integer", "description": "Sticker ID within the pack"},
            },
            "required": ["pack_id", "sticker_id"],
        },
    ),
    Tool(
        name="list_accounts",
        description=(
            "List all Signal accounts (phone numbers) registered in signal-cli on this machine. "
            "Returns each account's E.164 phone number and its registration status. "
            "Most setups have a single account; multiple accounts appear when signal-cli manages more than one number. "
            "Use get_own_number to get the active account's number in single-account setups. "
            "Use when you need to confirm which accounts are available before sending or receiving messages."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="prune_store",
        description=(
            "Delete locally stored messages older than a given number of days (default: 180). "
            "Does NOT delete messages from Signal servers — only the local history cache. "
            "Useful for keeping the store from growing unbounded."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Delete messages older than this many days (default: 180)", "default": 180},
            },
        },
    ),
    Tool(
        name="find_contact",
        description=(
            "Search contacts by name or phone number fragment. "
            "Returns all contacts whose name or number contains the query string (case-insensitive). "
            "Use this to look up a phone number when you only know a name, or to verify a contact exists."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Name or phone number fragment to search for"},
            },
            "required": ["query"],
        },
    ),
]


async def _list_tools(ctx, params: RequestParams) -> ListToolsResult:
    # mcp >= 2.0 invokes request handlers as (ctx, params)
    return ListToolsResult(tools=TOOLS)


_TOOL_NAMES = {t.name for t in TOOLS}


async def call_tool(ctx, params: CallToolRequestParams) -> CallToolResult:
    name = params.name
    arguments = params.arguments or {}

    # Culper read-only fork: reject unregistered tools (including all removed
    # write/destructive tools) before any daemon interaction.
    if name not in _TOOL_NAMES:
        return _err(f"Unknown tool: {name}")

    client = get_client()  # noqa: F841 — used throughout the giant match below

    try:
        if name not in _DAEMON_FREE:
            await client.ensure_daemon()

        # Validate required parameters up front (gives clean error instead of KeyError)
        _REQUIRED: dict[str, list[str]] = {
            "get_conversation":     ["recipient"],
            "search_messages":      ["query"],
            "get_profile":          ["number"],
            "get_attachment":       ["filename"],
            "get_sticker":          ["pack_id", "sticker_id"],
            "clear_local_store":    ["confirm"],
            "delete_local_messages":["recipient"],
            "get_user_status":      ["recipients"],
            "mark_as_unread":                 ["message_ids"],
            "get_avatar":                     ["identifier"],
        }
        if name in _REQUIRED:
            err = _require(arguments, *_REQUIRED[name])
            if err:
                return _err(err)

        if name == "list_attachments":
            return _ok(client.list_attachments())

        elif name == "get_attachment":
            return _ok(client.get_attachment(arguments["filename"]))

        elif name == "receive_messages":
            await client._ensure_caches()
            try:
                timeout = int(arguments.get("timeout", 5))
            except (TypeError, ValueError):
                return _err("timeout must be an integer number of seconds")
            try:
                messages = await client.receive_messages(timeout=timeout)
                return _ok([client._enrich_message(m) for m in messages])
            except Exception as e:
                if "already being received" in str(e):
                    # Background service is running — read from store instead
                    from signal_mcp.store import get_unread_messages as _get_unread
                    msgs = await asyncio.to_thread(_get_unread, client.account, 50)
                    return _ok({
                        "note": "Background service is running — returning unread messages from store instead.",
                        "messages": [client._enrich_message(m) for m in msgs],
                    })
                raise

        elif name == "list_contacts":
            contacts = await client.list_contacts(search=arguments.get("search"))
            return _ok([c.to_dict() for c in contacts])

        elif name == "list_groups":
            groups = await client.list_groups()
            return _ok([g.to_dict() for g in groups])

        elif name == "get_conversation":
            since = None
            if arguments.get("since"):
                try:
                    since = datetime.fromisoformat(arguments["since"])
                except ValueError:
                    return _err(f"Invalid since date: {arguments['since']}")
            limit = arguments.get("limit", 50)
            offset = arguments.get("offset", 0)
            await client._ensure_caches()
            messages, total = await asyncio.gather(
                client.get_conversation(
                    arguments["recipient"], limit=limit, offset=offset, since=since,
                ),
                asyncio.to_thread(
                    _store.count_conversation, arguments["recipient"], since=since
                ),
            )
            # client.get_conversation already marks incoming messages as read
            return _ok({
                "messages": [client._enrich_message(m) for m in messages],
                "total": total,
                "has_more": total > offset + len(messages),
                "limit": limit,
                "offset": offset,
            })

        elif name == "search_messages":
            await client._ensure_caches()
            messages = await client.search_messages(
                arguments["query"],
                limit=int(arguments.get("limit", 50)),
                offset=int(arguments.get("offset", 0)),
                sender=arguments.get("sender"),
            )
            return _ok([client._enrich_message(m) for m in messages])

        elif name == "get_profile":
            contact = await client.get_profile(arguments["number"])
            return _ok(contact.to_dict())

        elif name == "list_devices":
            devices = await client.list_devices()
            return _ok(devices)

        elif name == "get_own_number":
            return _ok({"number": client.get_own_number()})

        elif name == "get_unread":
            await client._ensure_caches()
            warning = await _freshen_store(client)
            limit = int(arguments.get("limit", 50))
            # Fetch one extra to detect whether more exist without a COUNT query
            messages = await client.get_unread_messages(limit=limit + 1)
            has_more = len(messages) > limit
            messages = messages[:limit]
            # Mark as read — Claude has now seen these messages
            unread_ids = [m.id for m in messages]
            if unread_ids:
                await asyncio.to_thread(_store.mark_as_read, unread_ids)
            result: dict = {
                "messages": [client._enrich_message(m) for m in messages],
                "has_more": has_more,
            }
            if warning:
                result["_warning"] = warning
            return _ok(result)

        elif name == "store_stats":
            return _ok(_store.get_stats(own_number=client.account))

        elif name == "import_desktop":
            from .desktop import import_from_desktop, DesktopImportError
            try:
                result = import_from_desktop()
                return _ok(result)
            except DesktopImportError as e:
                return _err(str(e))

        elif name == "sync_desktop":
            from .desktop import sync_from_desktop, DesktopImportError
            try:
                result = sync_from_desktop()
                return _ok(result)
            except DesktopImportError as e:
                return _err(str(e))

        elif name == "list_conversations":
            await client._ensure_caches()
            # client.list_conversations() already resolves names via resolve_name/resolve_group_name
            conversations = await client.list_conversations()
            return _ok(conversations)

        elif name == "list_identities":
            identities = await client.list_identities(number=arguments.get("number"))
            return _ok(identities)

        elif name == "get_configuration":
            return _ok(await client.get_configuration())

        elif name == "list_sticker_packs":
            return _ok(await client.list_sticker_packs())

        elif name == "get_sticker":
            data = await client.get_sticker(arguments["pack_id"], int(arguments["sticker_id"]))
            return _ok({"base64": data})

        elif name == "list_accounts":
            accounts = await client.list_accounts()
            return _ok(accounts)

        elif name == "clear_local_store":
            if not arguments.get("confirm"):
                return _err("confirm must be true to delete all local messages")
            count = await client.clear_local_store()
            return _ok({"deleted": count, "status": "cleared"})

        elif name == "delete_local_messages":
            count = await client.delete_local_messages(arguments["recipient"])
            return _ok({"deleted": count, "status": "deleted"})

        elif name == "get_user_status":
            statuses = await client.get_user_status(arguments["recipients"])
            return _ok(statuses)

        elif name == "send_sync_request":
            await client.send_sync_request()
            return _ok({"status": "sync requested"})

        elif name == "mark_as_unread":
            await client.mark_as_unread(arguments["message_ids"])
            return _ok({"status": "marked as unread", "count": len(arguments["message_ids"])})

        elif name == "get_avatar":
            avatar_data = await client.get_avatar(arguments["identifier"])
            return _ok({"identifier": arguments["identifier"], "base64": avatar_data, "has_avatar": bool(avatar_data)})

        elif name == "export_messages":
            fmt = arguments.get("format", "json")
            if fmt not in ("json", "csv"):
                return _err("format must be 'json' or 'csv'")
            since_str = arguments.get("since")
            since = None
            if since_str:
                try:
                    since = datetime.fromisoformat(since_str)
                except ValueError:
                    return _err(f"Invalid since datetime: {since_str!r}")
            data = await client.export_messages(
                fmt=fmt,
                recipient=arguments.get("recipient"),
                since=since,
            )
            return _ok({"format": fmt, "data": data})

        elif name == "prune_store":
            days = int(arguments.get("days", 180))
            if days <= 0:
                return _err("days must be a positive integer")
            count = await asyncio.to_thread(_store.prune_old_messages, days)
            return _ok({"deleted": count, "older_than_days": days})

        elif name == "find_contact":
            err = _require(arguments, "query")
            if err:
                return _err(err)
            await client.ensure_daemon()
            contacts = await client.list_contacts(search=arguments["query"])
            return _ok([c.to_dict() for c in contacts])

        else:
            return _err(f"Unknown tool: {name}")

    except SignalError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"Unexpected error: {e}")


_SERVICE_WARNING = (
    "Background service is not installed. Messages are only captured when this tool is called. "
    "Run 'signal-mcp install-service' to capture messages automatically in the background."
)

_FRESHEN_COOLDOWN = 30.0   # seconds — don't poll more than once per 30s
_last_freshen_at: float = 0.0


async def _freshen_store(client: SignalClient) -> str | None:
    """Poll signal-cli for new messages if no background service is running.

    Debounced: skips the poll if one completed within the last 30 seconds,
    so back-to-back tool calls (get_unread → list_conversations) only poll once.

    Returns a warning string when the service is absent, None when it is present.
    """
    global _last_freshen_at
    if is_service_installed():
        return None
    import time
    now = time.monotonic()
    if now - _last_freshen_at < _FRESHEN_COOLDOWN:
        return _SERVICE_WARNING  # still fresh from recent poll
    _last_freshen_at = now   # stamp BEFORE the await — concurrent calls see it as in-flight
    try:
        await client.receive_messages(timeout=2)
    except Exception:
        pass  # service just started receiving, or daemon not ready — best effort
    return _SERVICE_WARNING


app.add_request_handler("tools/list", RequestParams, _list_tools)
app.add_request_handler("tools/call", CallToolRequestParams, call_tool)


async def serve() -> None:  # pragma: no cover
    _store.init_db()
    try:
        check_signal_cli_version()
        client = get_client()
        # Pre-warm: start daemon in background so first tool call doesn't cold-start
        await client.prewarm()
        # Pre-load contact + group names concurrently in background
        _t = asyncio.create_task(client._ensure_caches())
        client._background_tasks.append(_t)
        # Watchdog is already started by prewarm() via _start_watchdog() (idempotent)
    except RuntimeError as exc:
        import sys
        print(f"[signal-mcp] WARNING: {exc}", file=sys.stderr)
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())
