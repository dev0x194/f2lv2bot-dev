import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch
from bson.objectid import ObjectId
import pytest
from pyrogram.errors import FloodWait, RPCError

from FileStream.config import Telegram
from FileStream.services.storage_service import (
    StorageService,
    StorageStatus,
    StorageJob,
    StorageResult,
)
from FileStream.utils.circuit_breaker import (
    CircuitBreakerState,
    get_circuit_breaker_registry,
)
from FileStream.utils.file_properties import (
    _resolve_source_client,
    send_file,
)
from FileStream.bot import FileStream, multi_clients
from tests.test_folder_mode import MockDatabase, MockBot, MockMessage


class DummyMediaEmpty(RPCError):
    CODE = 400
    NAME = "MediaEmpty"
    MESSAGE = "The media you tried to send is invalid."


class DummyFileReferenceInvalid(RPCError):
    CODE = 400
    NAME = "FileReferenceInvalid"
    MESSAGE = "The file reference has expired and is now invalid."


class DummyUnauthorized(RPCError):
    CODE = 401
    NAME = "Unauthorized"
    MESSAGE = "The bot token is invalid or has been revoked."


@pytest.fixture(autouse=True)
def reset_cb():
    reg = get_circuit_breaker_registry()
    reg.reset_all()
    yield
    reg.reset_all()


# ==============================================================================
# 1. MTProto Scoping & Source Client Resolution
# ==============================================================================
def test_resolve_source_client_prefers_message_client_for_private():
    """In Telegram MTProto, the client that received the message must be the source client."""
    bot0 = MockBot(bot_id=1000, username="primary_bot")
    bot1 = MockBot(bot_id=1001, username="worker_1")
    msg = MockMessage(message_id=42, user_id=9999, chat_id=9999)
    msg._client = bot0

    # Even if bot1 (secondary) is passed as preferred_client, msg._client (bot0) must be used!
    resolved = _resolve_source_client(file_info={}, preferred_client=bot1, message=msg)
    assert resolved == bot0


def test_resolve_source_client_uses_preferred_when_no_message():
    """When no incoming message is present, preferred_client can be used."""
    bot1 = MockBot(bot_id=1001, username="worker_1")
    resolved = _resolve_source_client(file_info={}, preferred_client=bot1, message=None)
    assert resolved == bot1


def test_resolve_source_client_matches_source_bot_id():
    """When source_bot_id matches a client, it resolves to that client."""
    bot1 = MockBot(bot_id=1001, username="worker_1")
    with patch.dict(multi_clients, {1: bot1}, clear=False):
        resolved = _resolve_source_client(file_info={"source_bot_id": 1001}, preferred_client=None, message=None)
        assert resolved == bot1


# ==============================================================================
# 2. send_file uses copy_message when available
# ==============================================================================
@pytest.mark.asyncio
async def test_send_file_uses_copy_message_when_available():
    """send_file should prefer copy_message to preserve media and avoid MEDIA_EMPTY."""
    mock_db = MockDatabase()
    bot = MockBot(bot_id=1000)
    copy_res = MagicMock()
    copy_res.id = 7777
    bot.copy_message = AsyncMock(return_value=copy_res)
    bot.send_cached_media = AsyncMock()

    msg = MockMessage(message_id=55, user_id=123, chat_id=123)
    msg._client = bot

    file_info = {
        "_id": ObjectId(),
        "file_name": "test_video.mp4",
        "file_id": "test_fid_1",
    }
    await mock_db.file.insert_one(file_info)

    with patch("FileStream.utils.file_properties.db", mock_db), \
         patch("FileStream.utils.file_properties.Telegram.FLOG_CHANNEL", -1001234567890):
        log_msg = await send_file(
            client=bot,
            db_id=str(file_info["_id"]),
            file_id="test_fid_1",
            message=msg,
            file_info=file_info,
        )

    assert log_msg.id == 7777
    bot.copy_message.assert_called_once()
    bot.send_cached_media.assert_not_called()


@pytest.mark.asyncio
async def test_send_file_fallback_to_send_cached_media():
    """If copy_message fails or is unavailable, fallback to send_cached_media."""
    mock_db = MockDatabase()
    bot = MockBot(bot_id=1000)
    cached_res = MagicMock()
    cached_res.id = 8888
    bot.copy_message = AsyncMock(side_effect=Exception("Copy forbidden"))
    bot.send_cached_media = AsyncMock(return_value=cached_res)

    msg = MockMessage(message_id=56, user_id=123, chat_id=123)
    msg._client = bot

    file_info = {
        "_id": ObjectId(),
        "file_name": "test_audio.mp3",
        "file_id": "test_fid_2",
    }
    await mock_db.file.insert_one(file_info)

    with patch("FileStream.utils.file_properties.db", mock_db), \
         patch("FileStream.utils.file_properties.Telegram.FLOG_CHANNEL", -1001234567890):
        log_msg = await send_file(
            client=bot,
            db_id=str(file_info["_id"]),
            file_id="test_fid_2",
            message=msg,
            file_info=file_info,
        )

    assert log_msg.id == 8888
    bot.copy_message.assert_called_once()
    bot.send_cached_media.assert_called_once()


# ==============================================================================
# 3. Circuit Breaker Error Classification
# ==============================================================================
def test_circuit_breaker_media_empty_not_fatal():
    """MediaEmpty error must not trip circuit breaker permanently."""
    reg = get_circuit_breaker_registry()
    breaker = reg.get(1)

    exc = DummyMediaEmpty("Telegram says: [400 MEDIA_EMPTY]")
    # Even if someone accidentally passed is_fatal=True, the safeguard prevents 365-day trip
    reg.record_failure(1, exc, is_fatal=True)

    assert breaker.state == CircuitBreakerState.CLOSED
    assert breaker.cooldown_until < time.time() + 100
    assert breaker.can_execute() is True


def test_circuit_breaker_unauthorized_is_fatal():
    """Unauthorized error must trip circuit breaker permanently."""
    reg = get_circuit_breaker_registry()
    breaker = reg.get(1)

    exc = DummyUnauthorized("Telegram says: [401 UNAUTHORIZED]")
    reg.record_failure(1, exc, is_fatal=True)

    assert breaker.state == CircuitBreakerState.OPEN
    assert breaker.cooldown_until > time.time() + 86400 * 300
    assert breaker.can_execute() is False

    # Reset recovers the breaker
    reg.reset_client(1)
    assert breaker.state == CircuitBreakerState.CLOSED
    assert breaker.can_execute() is True


# ==============================================================================
# 4. StorageService Client Selection & Error Handling
# ==============================================================================
@pytest.mark.asyncio
async def test_storage_service_selects_client_0_for_incoming_media():
    """Client 0 is selected for incoming media storage jobs when healthy."""
    mock_db = MockDatabase()
    c0 = MagicMock(id=1000, client_index=0, is_connected=True)
    c1 = MagicMock(id=1001, client_index=1, is_connected=True)
    svc = StorageService(db=mock_db, clients={0: c0, 1: c1}, max_queue_size=10)

    msg = MockMessage(message_id=99, user_id=123, chat_id=123)
    msg._client = c0

    selection = await svc.reserve_client(preferred_id=0)
    assert selection is not None
    client_idx, selected_client, stat = selection
    assert client_idx == 0
    assert selected_client == c0
    svc.release_client(client_idx)
    await svc.shutdown(timeout=0.1)


@pytest.mark.asyncio
async def test_storage_service_media_empty_does_not_mark_fatal():
    """When MediaEmpty occurs, client circuit breaker is NOT tripped permanently."""
    mock_db = MockDatabase()
    c0 = MagicMock(id=1000, client_index=0, is_connected=True)
    c1 = MagicMock(id=1001, client_index=1, is_connected=True)
    svc = StorageService(db=mock_db, clients={0: c0, 1: c1}, max_queue_size=10)

    reg = get_circuit_breaker_registry()
    reg.reset_all()

    exc = DummyMediaEmpty("Telegram says: [400 MEDIA_EMPTY]")
    assert svc._is_permanent_error(exc) is False
    assert svc._is_fatal_client_error(exc) is False

    # Simulate failure in _process_job
    job = StorageJob(
        db_id=str(ObjectId()),
        file_id="bad_file_id",
        file_name="bad.mp4",
        file_size=1000,
        file_info={},
        max_retries=1,
    )

    with patch("FileStream.utils.file_properties.send_file", side_effect=exc):
        res = await svc._process_job(job)
        assert res is False

    breaker = reg.get(job.client_id)
    assert breaker.cooldown_until < time.time() + 100
    assert breaker.can_execute() is True
    await svc.shutdown(timeout=0.1)


# ==============================================================================
# 5. Stream Handler Exception Handling
# ==============================================================================
@pytest.mark.asyncio
async def test_stream_handler_catches_runtime_error_cleanly():
    """private_receive_handler catches storage RuntimeError and edits status_msg cleanly."""
    from FileStream.bot.plugins.stream import private_receive_handler
    from FileStream.bot.messages import ErrorMessages

    bot = MockBot(bot_id=1000)
    msg = MockMessage(message_id=101, user_id=5555, chat_id=5555)
    msg.document = MagicMock(file_id="doc_id_1", file_size=1024, file_name="sample.pdf", mime_type="application/pdf")
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    msg.reply_text = AsyncMock(return_value=status_msg)

    with patch("FileStream.bot.plugins.stream.verify_user", AsyncMock(return_value=True)), \
         patch("FileStream.bot.plugins.batch.folder_service.get_active_session_for_user", AsyncMock(return_value=None)), \
         patch("FileStream.bot.plugins.stream.track_event", AsyncMock()), \
         patch("FileStream.bot.plugins.stream.file_service.process_new_file", AsyncMock(side_effect=RuntimeError("Storage failed for file 6ab52fed054353f7f4c47d92: Storage failed"))):
        # Should not raise RuntimeError unhandled
        await private_receive_handler(bot, msg)

    status_msg.edit_text.assert_called_with(text=ErrorMessages.UPLOAD_FAILED, parse_mode=pytest.importorskip("pyrogram.enums").ParseMode.HTML)
