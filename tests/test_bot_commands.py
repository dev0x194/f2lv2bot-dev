import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pyrogram.errors import FloodWait, PeerIdInvalid
from pyrogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)

from FileStream.config import Telegram
from FileStream.utils.bot_commands import (
    ADMIN_COMMANDS,
    CONTEXTUAL_COMMANDS,
    OWNER_COMMANDS,
    PRIMARY_PUBLIC_COMMANDS,
    PUBLIC_COMMANDS,
    _unique_admin_ids,
    build_admin_help_text,
    register_bot_commands,
)


EXPECTED_PUBLIC_ORDER = [
    "donation",
    "ping",
    "start",
    "help",
    "files",
    "folders",
    "folder",
    "done",
    "cancel",
    "about",
    "privacy",
    "legal",
    "id",
]

KNOWN_ADMIN_OWNER_COMMANDS = {
    "admin",
    "flogstorage",
    "urlshortener",
    "ads",
    "webads",
    "speedtest",
    "botstatus",
    "rm_adult",
    "status",
    "ban",
    "unban",
    "broadcast",
    "del",
    "linkinfo",
    "revoke_link",
    "regen_link",
    "expire_link",
}


def test_public_command_order():
    """1. Test public command order matches the exact target sequence."""
    actual_order = [cmd.command for cmd in PUBLIC_COMMANDS]
    assert actual_order == EXPECTED_PUBLIC_ORDER


def test_donation_is_position_1():
    """2. Test /donation is at position 1 (0-indexed position 0)."""
    assert PUBLIC_COMMANDS[0].command == "donation"
    assert PUBLIC_COMMANDS[0].description == "Support the bot with Stars"


def test_ping_is_position_2():
    """3. Test /ping is at position 2 (0-indexed position 1)."""
    assert PUBLIC_COMMANDS[1].command == "ping"
    assert PUBLIC_COMMANDS[1].description == "Check bot response"


def test_start_is_position_3():
    """4. Test /start is at position 3 (0-indexed position 2)."""
    assert PUBLIC_COMMANDS[2].command == "start"
    assert PUBLIC_COMMANDS[2].description == "Start using File To Link V2"


def test_all_required_public_commands_exist():
    """5. Test that all required public commands exist and backwards-compat aliases work."""
    actual_commands = {cmd.command for cmd in PUBLIC_COMMANDS}
    for expected in EXPECTED_PUBLIC_ORDER:
        assert expected in actual_commands, f"Missing required command: /{expected}"

    assert PRIMARY_PUBLIC_COMMANDS == PUBLIC_COMMANDS
    contextual_names = [cmd.command for cmd in CONTEXTUAL_COMMANDS]
    assert contextual_names == ["done", "cancel"]


def test_no_duplicate_public_commands():
    """6. Test that no duplicate public commands exist."""
    command_names = [cmd.command for cmd in PUBLIC_COMMANDS]
    assert len(command_names) == len(set(command_names))
    assert len(command_names) == len(EXPECTED_PUBLIC_ORDER)


def test_admin_commands_not_exposed_publicly():
    """7. Test that admin/owner commands are not exposed in public commands list."""
    public_command_names = {cmd.command for cmd in PUBLIC_COMMANDS}
    for admin_cmd in KNOWN_ADMIN_OWNER_COMMANDS:
        assert (
            admin_cmd not in public_command_names
        ), f"Admin command /{admin_cmd} must not be exposed in PUBLIC_COMMANDS"


def test_command_descriptions_are_valid():
    """8. Test that all command names and descriptions satisfy Telegram API constraints."""
    cmd_name_pattern = re.compile(r"^[a-z0-9_]{1,32}$")

    all_commands = PUBLIC_COMMANDS + ADMIN_COMMANDS + OWNER_COMMANDS
    for cmd in all_commands:
        assert isinstance(cmd, BotCommand)
        assert cmd_name_pattern.match(
            cmd.command
        ), f"Invalid command name syntax: '{cmd.command}'"
        assert (
            3 <= len(cmd.description) <= 256
        ), f"Description for /{cmd.command} outside length bounds (3-256): '{cmd.description}'"
        assert (
            cmd.description == cmd.description.strip()
        ), f"Description for /{cmd.command} has untrimmed whitespace"


def test_command_registration_is_deterministic():
    """9. Test command registration ordering is deterministic."""
    runs = [[cmd.command for cmd in PUBLIC_COMMANDS] for _ in range(10)]
    for r in runs:
        assert r == EXPECTED_PUBLIC_ORDER


@pytest.mark.asyncio
async def test_multi_client_registration_no_conflicts():
    """10. Test multi-client registration: non-bot skipped, bot registered with proper scopes."""
    # A. Non-bot client (user session string) must be skipped
    user_client = MagicMock()
    user_client.me = MagicMock(is_bot=False, username="user_session")
    user_client.set_bot_commands = AsyncMock()

    await register_bot_commands(user_client)
    user_client.set_bot_commands.assert_not_called()

    # B. None client check
    await register_bot_commands(None)

    # C. Primary bot client registration
    bot_client = MagicMock()
    bot_client.me = MagicMock(is_bot=True, username="file2linkv2bot")
    bot_client.set_bot_commands = AsyncMock()

    with patch.object(Telegram, "OWNER_ID", 111111), patch.object(
        Telegram, "AUTH_USERS", [222222]
    ):
        await register_bot_commands(bot_client)

        calls = bot_client.set_bot_commands.call_args_list
        assert len(calls) >= 4  # Default, PrivateChats, Owner, Admin

        # 1st call: Default scope with PUBLIC_COMMANDS
        assert calls[0].args[0] == PUBLIC_COMMANDS
        assert isinstance(calls[0].kwargs["scope"], BotCommandScopeDefault)

        # 2nd call: AllPrivateChats scope with PUBLIC_COMMANDS
        assert calls[1].args[0] == PUBLIC_COMMANDS
        assert isinstance(calls[1].kwargs["scope"], BotCommandScopeAllPrivateChats)

        # 3rd call: Owner scope with OWNER_COMMANDS
        assert calls[2].args[0] == OWNER_COMMANDS
        assert isinstance(calls[2].kwargs["scope"], BotCommandScopeChat)
        assert calls[2].kwargs["scope"].chat_id == 111111

        # 4th call: Admin scope with ADMIN_COMMANDS
        assert calls[3].args[0] == ADMIN_COMMANDS
        assert isinstance(calls[3].kwargs["scope"], BotCommandScopeChat)
        assert calls[3].kwargs["scope"].chat_id == 222222

    # D. Secondary bot client registration works without conflict
    secondary_bot = MagicMock()
    secondary_bot.me = MagicMock(is_bot=True, username="secondarybot")
    secondary_bot.set_bot_commands = AsyncMock()

    with patch.object(Telegram, "OWNER_ID", 111111), patch.object(
        Telegram, "AUTH_USERS", []
    ):
        await register_bot_commands(secondary_bot)
        assert secondary_bot.set_bot_commands.call_count == 3
        # Ensure public commands are identical
        assert secondary_bot.set_bot_commands.call_args_list[0].args[0] == PUBLIC_COMMANDS


@pytest.mark.asyncio
async def test_admin_registration_resilience():
    """Test that errors on individual admin scopes (PeerIdInvalid, FloodWait) do not break registration."""
    bot = MagicMock()
    bot.me = MagicMock(is_bot=True, username="testbot")

    async def side_effect(commands, scope=None):
        if isinstance(scope, BotCommandScopeChat) and scope.chat_id == 999:
            raise PeerIdInvalid()
        if isinstance(scope, BotCommandScopeChat) and scope.chat_id == 888:
            raise FloodWait(5)

    bot.set_bot_commands = AsyncMock(side_effect=side_effect)

    with patch.object(Telegram, "OWNER_ID", 999), patch.object(
        Telegram, "AUTH_USERS", [888, 777]
    ):
        # Must complete without unhandled exception
        await register_bot_commands(bot)
        # Default and AllPrivateChats calls still executed
        assert bot.set_bot_commands.call_count == 5


def test_unique_admin_ids_deduplication():
    """Test that _unique_admin_ids handles deduplication, invalid strings, and non-positive IDs."""
    with patch.object(Telegram, "OWNER_ID", 12345), patch.object(
        Telegram, "AUTH_USERS", [12345, "67890", -1, 0, "invalid", 67890]
    ):
        admin_ids = _unique_admin_ids()
        assert admin_ids == [12345, 67890]


def test_build_admin_help_text():
    """Test that build_admin_help_text returns formatted help covering admin and owner commands."""
    help_text = build_admin_help_text()
    assert "<b>🛠 Admin Commands</b>" in help_text
    assert "<b>Shared Admin</b>" in help_text
    assert "<b>Owner Only</b>" in help_text
    assert "/admin" in help_text
    assert "/flogstorage" in help_text
    assert "/urlshortener" in help_text
    assert "/speedtest" in help_text
    assert "/status" in help_text
    assert "/broadcast" in help_text
