import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch
from bson.objectid import ObjectId
import pytest

from FileStream.config import Telegram
from FileStream.services.storage_service import StorageService, StorageStatus
from FileStream.utils.circuit_breaker import get_circuit_breaker_registry, CircuitBreakerState
from FileStream.utils.file_properties import send_file, get_file_info
from FileStream.server.stream_routes import _eligible_client_indexes
from FileStream.bot import multi_clients
from tests.test_folder_mode import MockDatabase, MockBot, MockMessage


@pytest.fixture(autouse=True)
def reset_cb():
    reg = get_circuit_breaker_registry()
    reg.reset_all()
    yield
    reg.reset_all()


@pytest.mark.asyncio
async def test_dev_direct_storage_mode_without_flog():
    """Verify that in DEV when no FLOG channel is configured, files store directly without error."""
    mock_db = MockDatabase()
    bot0 = MockBot(bot_id=5000, username="dev_bot")
    clients = {0: bot0}

    with patch.object(Telegram, "FLOG_CHANNEL", None), \
         patch.object(Telegram, "ADMIN_FLOG_CHANNEL", None):

        mock_doc = MagicMock()
        mock_doc.file_id = "tg_file_id_123"
        mock_doc.file_name = "test_video.mp4"
        mock_doc.file_size = 1024
        mock_doc.mime_type = "video/mp4"
        mock_doc.file_unique_id = "uniq_123"

        msg = MockMessage(message_id=42, bot=bot0)
        msg.document = mock_doc
        msg._client = bot0

        file_info = get_file_info(msg)
        assert file_info is not None
        assert file_info["source_bot_index"] == 0

        # send_file returns None (direct storage mode) instead of raising FileNotFound
        res = await send_file(bot0, "507f1f77bcf86cd799439011", "tg_file_id_123", message=msg, file_info=file_info)
        assert res is None

        # StorageService.store_file handles direct storage gracefully
        svc = StorageService(db=mock_db, clients=clients, max_queue_size=10)
        db_id = str(ObjectId())
        await mock_db.file.insert_one({"_id": ObjectId(db_id), "file_id": "tg_file_id_123"})

        store_res = await svc.store_file(
            db_id=db_id,
            file_id="tg_file_id_123",
            file_name="test_video.mp4",
            file_size=1024,
            file_info=file_info,
            message=msg,
            user_id=12345,
        )

        assert store_res.success is True
        assert store_res.error is None
        doc = await mock_db.file.find_one({"_id": ObjectId(db_id)})
        assert doc["storage_status"] == "stored"
        assert doc.get("file_ids", {}).get(str(bot0.id)) == "tg_file_id_123"


def test_secondary_worker_not_selected_for_private_chat():
    """When a preferred client is specified for private user files, secondary workers must be excluded."""
    mock_db = MockDatabase()
    bot0 = MockBot(bot_id=5000, username="primary_dev_bot")
    bot0.client_index = 0
    bot1 = MockBot(bot_id=5001, username="secondary_dev_worker")
    bot1.client_index = 1

    clients = {0: bot0, 1: bot1}
    svc = StorageService(db=mock_db, clients=clients)

    # When preferred_id=0 (or 5000), bot 0 is selected
    sel = svc.select_best_client(preferred_id=0)
    assert sel is not None
    assert sel[0] == 0

    # If bot 0 is busy/in cooldown, secondary worker bot 1 must NOT be selected as fallback
    stat0 = svc._ensure_client_stat(0, bot0)
    stat0.cooldown_until = time.monotonic() + 999
    sel_blocked = svc.select_best_client(preferred_id=0)
    assert sel_blocked is None  # Must wait for bot 0 rather than assigning to bot 1


def test_circuit_breaker_file_not_found_is_non_fatal():
    """FileNotFound errors should not trigger permanent circuit breaker opening."""
    reg = get_circuit_breaker_registry()
    cb = reg.get(1)

    cb.record_failure(FileNotFoundError("File not found."), is_fatal=True)
    # Because 'file not found' is in non_fatal_media_patterns, is_fatal was overridden to False
    assert cb.state == CircuitBreakerState.CLOSED
    assert cb.failure_count == 1


def test_eligible_client_indexes_direct_storage():
    """Verify that direct storage files without flog_msg_id are only routed to the receiving client."""
    with patch.dict(multi_clients, {0: MagicMock(), 1: MagicMock(), 2: MagicMock()}, clear=True):
        # Direct-storage file (no flog_msg_id)
        direct_file = {"_id": "123", "source_bot_index": 0}
        assert _eligible_client_indexes(direct_file) == [0]

        # Replicated FLOG file (has flog_msg_id)
        flog_file = {"_id": "123", "flog_msg_id": 999}
        assert set(_eligible_client_indexes(flog_file)) == {0, 1, 2}
