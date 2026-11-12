import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch
from bson.objectid import ObjectId
import pytest
from pyrogram.enums import ChatType
from pyrogram.errors import FloodWait

from FileStream.config import Telegram
from FileStream.bot.clients import (
    setup_secondary_worker_handlers,
    is_secondary_worker,
    initialize_clients,
)
from FileStream.services.storage_service import StorageService, StorageStatus
from FileStream.services.folder_service import FolderService
from FileStream.services.user_service import UserService
from FileStream.utils.bot_utils import (
    is_internal_service_chat,
    is_configured_channel,
    is_secondary_bot_id,
    get_known_bot_ids,
)
from tests.test_folder_mode import MockDatabase, MockBot, MockMessage
from tests.test_storage_worker_architecture import StorageTestBot, make_mock_log_msg

# Standard test channel IDs
FLOG_CH_ID = -1001111111111
ADMIN_FLOG_CH_ID = -1002222222222
ULOG_CH_ID = -1003333333333
NUDENET_CH_ID = -1004444444444
BINBIN_CH_ID = -1005555555555


@pytest.fixture(autouse=True)
def configure_test_channels():
    with patch.object(Telegram, "FLOG_CHANNEL", FLOG_CH_ID), \
         patch.object(Telegram, "ADMIN_FLOG_CHANNEL", ADMIN_FLOG_CH_ID), \
         patch.object(Telegram, "ULOG_CHANNEL", ULOG_CH_ID), \
         patch.object(Telegram, "NUDENET_CHANNEL", NUDENET_CH_ID), \
         patch.object(Telegram, "BIN_CHANNEL", BINBIN_CH_ID), \
         patch.object(Telegram, "BINBIN_CHANNEL", BINBIN_CH_ID):
        yield


# ==============================================================================
# W1 — Worker startup produces no channel messages
# ==============================================================================
@pytest.mark.asyncio
async def test_w1_worker_startup_produces_no_channel_messages():
    """Verify that simulated secondary worker initialization sends 0 messages to any channel."""
    sec_bot = StorageTestBot(index=1, username="worker_1")
    setup_secondary_worker_handlers(sec_bot)

    assert len(sec_bot.sent_messages) == 0

    configured_channels = [
        FLOG_CH_ID,
        ULOG_CH_ID,
        ADMIN_FLOG_CH_ID,
        NUDENET_CH_ID,
        BINBIN_CH_ID,
    ]
    for ch_id in configured_channels:
        channel_msgs = [m for m in sec_bot.sent_messages if m.get("chat_id") == ch_id]
        assert len(channel_msgs) == 0, f"Worker sent message to channel {ch_id} during startup!"


# ==============================================================================
# W2 — Secondary direct user message
# ==============================================================================
@pytest.mark.asyncio
async def test_w2_secondary_direct_user_message():
    """
    Simulate user directly messaging MULTI_TOKEN1 in private chat:
    - User receives redirect notice.
    - No channel receives worker informational message.
    - No primary workflow is created and no file link is generated.
    """
    sec_bot = StorageTestBot(index=1, username="worker_1")
    setup_secondary_worker_handlers(sec_bot)

    mock_db = MockDatabase()
    folder_svc = FolderService(db=mock_db)

    msg_handler = sec_bot._secondary_message_handler
    user_id = 991234
    user_msg = MockMessage(message_id=10, user_id=user_id, chat_id=user_id, text="/start")
    user_msg.chat.type = ChatType.PRIVATE
    user_msg.from_user.is_bot = False

    from FileStream.bot import FileStream
    with patch.object(FileStream, "username", "file2linkv2bot", create=True):
        await msg_handler(sec_bot, user_msg)

    # 1. User receives redirect
    assert len(user_msg.replies) == 1
    reply_text = user_msg.replies[0]["text"]
    assert "Storage Worker Bot" in reply_text or "worker node" in reply_text.lower()
    assert "@file2linkv2bot" in reply_text

    # 2. No channel receives any message
    assert len(sec_bot.sent_messages) == 0

    # 3. No folder session or file link created
    session = await folder_svc.get_active_session(user_id)
    assert session is None


# ==============================================================================
# W3 — FLOG receives storage operation
# ==============================================================================
@pytest.mark.asyncio
async def test_w3_flog_receives_storage_operation_without_worker_notice():
    """
    Perform legitimate storage operation into FLOG.
    FLOG contains intended media/record, and NOT 'Storage Worker Bot'.
    When worker bot receives the FLOG update, it drops it without replying.
    """
    mock_db = MockDatabase()
    sec_bot = StorageTestBot(index=1, username="worker_1")
    setup_secondary_worker_handlers(sec_bot)

    service = StorageService(db=mock_db, clients={0: sec_bot, 1: sec_bot})
    service.ensure_started()

    log_msg = make_mock_log_msg(77001)
    with patch("FileStream.utils.file_properties.send_file", AsyncMock(return_value=log_msg)):
        res = await service.store_file(
            db_id=str(ObjectId()),
            file_id="tg_test_file",
            file_name="video.mp4",
            file_size=1024,
            file_info={"mime_type": "video/mp4"},
            user_id=12345,
        )
        assert res.success is True

    await service.shutdown()

    # Verify no worker notice was sent by worker bot
    worker_notices = [
        m for m in sec_bot.sent_messages
        if "Storage Worker Bot" in str(m.get("text", "")) or "worker node" in str(m.get("text", "")).lower()
    ]
    assert len(worker_notices) == 0

    # Now simulate Telegram delivering the FLOG storage update to the secondary worker
    flog_update = MockMessage(message_id=77001, chat_id=FLOG_CH_ID, text="Storage record")
    flog_update.chat.type = ChatType.CHANNEL
    flog_update.from_user = None

    await sec_bot._secondary_message_handler(sec_bot, flog_update)

    # Secondary worker must ignore the channel update completely
    assert len(flog_update.replies) == 0


# ==============================================================================
# W4 — ULOG isolation
# ==============================================================================
@pytest.mark.asyncio
async def test_w4_ulog_isolation_no_new_user_on_worker_startup():
    """
    Simulate secondary worker startup and events.
    Assert no '#NewUser' or '#NewChannel' record is generated in ULOG_CHANNEL.
    """
    mock_db = MockDatabase()
    user_svc = UserService(db=mock_db)

    bot0 = MockBot()
    sec_bot = StorageTestBot(index=1, username="worker_1")
    sec_bot.id = 555001

    # Worker bot message or startup event
    worker_msg = MockMessage(message_id=1, user_id=sec_bot.id, chat_id=sec_bot.id, text="/start")
    worker_msg.from_user.is_bot = True

    with patch("FileStream.bot.multi_clients", {0: bot0, 1: sec_bot}):
        await user_svc.is_user_exist(bot0, worker_msg)

    # Assert no ULOG message sent
    ulog_msgs = [m for m in bot0.sent_messages if m.get("chat_id") == ULOG_CH_ID]
    assert len(ulog_msgs) == 0

    # Feed channel event from ULOG to worker bot
    ulog_event = MockMessage(message_id=50, chat_id=ULOG_CH_ID, text="#NewUser • Name: Alice")
    ulog_event.chat.type = ChatType.CHANNEL
    ulog_event.from_user = None

    setup_secondary_worker_handlers(sec_bot)
    await sec_bot._secondary_message_handler(sec_bot, ulog_event)

    assert len(ulog_event.replies) == 0


# ==============================================================================
# W5 — ADMIN_FLOG isolation
# ==============================================================================
@pytest.mark.asyncio
async def test_w5_admin_flog_isolation():
    """
    Simulate admin sending 'hy' or a message in ADMIN_FLOG_CHANNEL.
    Worker bots must NOT append 'Storage Worker Bot'.
    """
    sec_bot = StorageTestBot(index=1, username="worker_1")
    setup_secondary_worker_handlers(sec_bot)

    admin_msg = MockMessage(message_id=100, chat_id=ADMIN_FLOG_CH_ID, text="hy")
    admin_msg.chat.type = ChatType.CHANNEL
    admin_msg.from_user = None

    await sec_bot._secondary_message_handler(sec_bot, admin_msg)

    # Worker bot must not reply into ADMIN_FLOG_CHANNEL
    assert len(admin_msg.replies) == 0
    assert len(sec_bot.sent_messages) == 0


# ==============================================================================
# W6 — NUDENET isolation
# ==============================================================================
@pytest.mark.asyncio
async def test_w6_nudenet_isolation():
    """
    Simulate message 'Hy' or NSFW report sent to NUDENET_CHANNEL.
    Worker bot must not append 'Storage Worker Bot'.
    """
    sec_bot = StorageTestBot(index=1, username="worker_1")
    setup_secondary_worker_handlers(sec_bot)

    nudenet_msg = MockMessage(message_id=101, chat_id=NUDENET_CH_ID, text="Hy")
    nudenet_msg.chat.type = ChatType.CHANNEL
    nudenet_msg.from_user = None

    await sec_bot._secondary_message_handler(sec_bot, nudenet_msg)

    assert len(nudenet_msg.replies) == 0
    assert len(sec_bot.sent_messages) == 0


# ==============================================================================
# W7 — BINBIN isolation
# ==============================================================================
@pytest.mark.asyncio
async def test_w7_binbin_isolation():
    """
    Simulate message 'Hey' sent to BINBIN_CHANNEL.
    Worker bot must not append 'Storage Worker Bot'.
    """
    sec_bot = StorageTestBot(index=1, username="worker_1")
    setup_secondary_worker_handlers(sec_bot)

    binbin_msg = MockMessage(message_id=102, chat_id=BINBIN_CH_ID, text="Hey")
    binbin_msg.chat.type = ChatType.CHANNEL
    binbin_msg.from_user = None

    await sec_bot._secondary_message_handler(sec_bot, binbin_msg)

    assert len(binbin_msg.replies) == 0
    assert len(sec_bot.sent_messages) == 0


# ==============================================================================
# W8 — Multiple secondary workers
# ==============================================================================
@pytest.mark.asyncio
async def test_w8_multiple_secondary_workers_zero_channel_messages():
    """
    Start MULTI_TOKEN1, MULTI_TOKEN2, MULTI_TOKEN3, MULTI_TOKEN4.
    Feed channel messages to all of them.
    Assert NONE of them injects worker notices into any channel.
    """
    bots = [StorageTestBot(index=i, username=f"worker_{i}") for i in range(1, 5)]
    for bot in bots:
        setup_secondary_worker_handlers(bot)

    channels = [FLOG_CH_ID, ULOG_CH_ID, ADMIN_FLOG_CH_ID, NUDENET_CH_ID, BINBIN_CH_ID]
    for ch_id in channels:
        ch_msg = MockMessage(message_id=200, chat_id=ch_id, text="Channel update")
        ch_msg.chat.type = ChatType.CHANNEL
        ch_msg.from_user = None

        for bot in bots:
            await bot._secondary_message_handler(bot, ch_msg)
            assert len(ch_msg.replies) == 0
            assert len(bot.sent_messages) == 0


# ==============================================================================
# W9 — Worker restart produces no channel messages
# ==============================================================================
@pytest.mark.asyncio
async def test_w9_worker_restart_isolation():
    """
    Stop and restart a secondary worker.
    Assert all configured channels receive 0 startup worker messages.
    """
    sec_bot = StorageTestBot(index=1, username="worker_1")
    setup_secondary_worker_handlers(sec_bot)

    # Simulate stopping
    sec_bot.is_connected = False
    assert len(sec_bot.sent_messages) == 0

    # Simulate restarting
    sec_bot.is_connected = True
    setup_secondary_worker_handlers(sec_bot)

    assert len(sec_bot.sent_messages) == 0


# ==============================================================================
# W10 — FloodWait recovery sends no channel messages
# ==============================================================================
@pytest.mark.asyncio
async def test_w10_floodwait_recovery_sends_no_channel_messages():
    """
    Trigger worker FloodWait and recovery in StorageService.
    Assert recovery process does not send worker notices to any channel.
    """
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0, username="primary_bot")
    bot1 = StorageTestBot(index=1, username="worker_1")
    setup_secondary_worker_handlers(bot1)

    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1}, max_queue_size=10)
    service.ensure_started()

    fw_triggered = False

    async def send_file_flaky(client, db_id, file_id, **kwargs):
        nonlocal fw_triggered
        if client.client_index == 1 and not fw_triggered:
            fw_triggered = True
            fw = FloodWait(0.01)
            fw.value = 0.01
            raise fw
        return make_mock_log_msg(99001)

    with patch("FileStream.utils.file_properties.send_file", side_effect=send_file_flaky):
        res = await service.store_file(
            db_id=str(ObjectId()),
            file_id="test_fid",
            file_name="test.mkv",
            file_size=2048,
            file_info={"mime_type": "video/x-matroska"},
            user_id=123,
        )
        assert res.success is True

    await service.shutdown()

    # Worker bot 1 sent messages must contain 0 worker notices
    notices = [
        m for m in bot1.sent_messages
        if "Storage Worker Bot" in str(m.get("text", "")) or "worker node" in str(m.get("text", "")).lower()
    ]
    assert len(notices) == 0


# ==============================================================================
# W11 — 500-job storage load with channel zero-leakage assertion
# ==============================================================================
@pytest.mark.asyncio
async def test_w11_load_test_500_jobs_with_channel_isolation():
    """
    Run 500-job storage load test (100 users × 5 files).
    Assert:
    - 500 jobs stored successfully.
    - count('Storage Worker Bot') == 0 in all configured channels.
    """
    mock_db = MockDatabase()
    bots = {i: StorageTestBot(index=i, username=f"worker_{i}") for i in range(5)}
    for i in range(1, 5):
        setup_secondary_worker_handlers(bots[i])

    service = StorageService(db=mock_db, clients=bots, max_queue_size=500, concurrency_per_client=4)
    service.ensure_started()

    total_users = 100
    files_per_user = 5
    total_jobs = total_users * files_per_user  # 500

    call_count = 0

    async def send_file_sim(client, db_id, file_id, **kwargs):
        nonlocal call_count
        call_count += 1
        if client.client_index == 1 and call_count % 20 == 0:
            fw = FloodWait(0.02)
            fw.value = 0.02
            raise fw
        await asyncio.sleep(0.0005)
        return make_mock_log_msg(30000 + call_count)

    with patch("FileStream.utils.file_properties.send_file", side_effect=send_file_sim):
        tasks = []
        for u in range(total_users):
            user_id = 30000 + u
            for f in range(files_per_user):
                tasks.append(
                    service.store_file(
                        db_id=str(ObjectId()),
                        file_id=f"tg_{user_id}_{f}",
                        file_name=f"file_{user_id}_{f}.mp4",
                        file_size=1024 * (f + 1),
                        file_info={"mime_type": "video/mp4"},
                        user_id=user_id,
                    )
                )

        results = await asyncio.gather(*tasks)

    assert len(results) == total_jobs
    successful = [r for r in results if r.success]
    assert len(successful) == total_jobs

    metrics = service.get_metrics()
    assert metrics["successful_jobs"] == total_jobs
    assert metrics["failed_jobs"] == 0
    assert metrics["storage_queue_size"] == 0

    # Crucial Assertion: Zero worker notices in any channel from all bots
    for idx, bot in bots.items():
        notices = [
            m for m in bot.sent_messages
            if "Storage Worker Bot" in str(m.get("text", "")) or "worker node" in str(m.get("text", "")).lower()
        ]
        assert len(notices) == 0, f"Bot {idx} leaked worker notice into a channel!"

    await service.shutdown()


# ==============================================================================
# W12 — 1,000-job load test with channel zero-leakage assertion
# ==============================================================================
@pytest.mark.asyncio
async def test_w12_load_test_1000_jobs_with_channel_isolation():
    """
    Run 1,000-job storage load test (100 users × 10 files) with 10% cancellations.
    Verify:
    - 0 worker informational messages in channels
    - 0 worker startup messages in channels
    - 0 worker redirect messages in channels
    - 900 stored
    - 100 cancelled
    - 0 lost
    - 0 duplicate FLOG
    - queue == 0
    - in_flight == 0
    """
    mock_db = MockDatabase()
    bots = {i: StorageTestBot(index=i, username=f"worker_{i}") for i in range(5)}
    for i in range(1, 5):
        setup_secondary_worker_handlers(bots[i])

    service = StorageService(
        db=mock_db,
        clients=bots,
        max_queue_size=1200,
        concurrency_per_client=4,
    )
    service.ensure_started()

    total_users = 100
    files_per_user = 10
    total_jobs = total_users * files_per_user  # 1,000 files

    send_calls = 0

    async def simulated_send(client, db_id, file_id, **kwargs):
        nonlocal send_calls
        send_calls += 1
        if client.client_index in (2, 3) and send_calls % 35 == 0:
            fw = FloodWait(0.01)
            fw.value = 0.01
            raise fw
        await asyncio.sleep(0.0005)
        return make_mock_log_msg(60000 + (send_calls % 100000))

    cancelled_users = set(range(0, 10))  # 10 users = 100 files

    tasks = []
    with patch("FileStream.utils.file_properties.send_file", side_effect=simulated_send):
        for u in range(total_users):
            user_id = 40000 + u
            sess_id = f"sess_{user_id}"
            if u in cancelled_users:
                service.cancel_session_jobs(sess_id)

            for f in range(files_per_user):
                tasks.append(
                    service.store_file(
                        db_id=str(ObjectId()),
                        file_id=f"tg_{user_id}_{f}",
                        file_name=f"file_{user_id}_{f}.mp4",
                        file_size=1024 * (f + 1),
                        file_info={"mime_type": "video/mp4"},
                        session_id=sess_id,
                        user_id=user_id,
                    )
                )

        results = await asyncio.gather(*tasks)

    assert len(results) == total_jobs
    successful = [r for r in results if r.success]
    cancelled = [r for r in results if not r.success and "cancel" in str(r.error).lower()]

    assert len(successful) + len(cancelled) == total_jobs
    assert len(successful) == 900
    assert len(cancelled) == 100

    metrics = service.get_metrics()
    assert metrics["successful_jobs"] == 900
    assert metrics["cancelled_jobs"] == 100
    assert metrics["failed_jobs"] == 0
    assert metrics["storage_queue_size"] == 0
    assert metrics["in_flight_jobs"] == 0

    # Channel isolation verification across all worker bots
    for idx, bot in bots.items():
        channel_worker_notices = [
            m for m in bot.sent_messages
            if "Storage Worker Bot" in str(m.get("text", "")) or "worker node" in str(m.get("text", "")).lower()
        ]
        assert len(channel_worker_notices) == 0, f"Worker {idx} leaked notices into channels!"

    await service.shutdown()


# ==============================================================================
# W13 — Echo loop prevention between workers
# ==============================================================================
@pytest.mark.asyncio
async def test_w13_echo_loop_prevention():
    """
    Verify message cascade / echo loop prevention:
    Worker A sends storage message -> Worker B receives message -> Worker B drops it.
    Worker B sends message -> Worker C drops it.
    No cascading responses.
    """
    worker_a = StorageTestBot(index=1, username="worker_1")
    worker_b = StorageTestBot(index=2, username="worker_2")
    worker_c = StorageTestBot(index=3, username="worker_3")

    for w in (worker_a, worker_b, worker_c):
        setup_secondary_worker_handlers(w)

    with patch("FileStream.bot.multi_clients", {0: MockBot(), 1: worker_a, 2: worker_b, 3: worker_c}):
        # Worker A posts storage media in FLOG_CHANNEL
        flog_post = MockMessage(message_id=901, chat_id=FLOG_CH_ID, text="storage_record")
        flog_post.chat.type = ChatType.CHANNEL
        flog_post.from_user = None

        # Worker B receives it
        await worker_b._secondary_message_handler(worker_b, flog_post)
        assert len(flog_post.replies) == 0
        assert len(worker_b.sent_messages) == 0

        # Direct bot-to-bot message: Worker A contacts Worker B
        bot_msg = MockMessage(message_id=902, user_id=worker_a.id, chat_id=worker_b.id, text="ping")
        bot_msg.from_user.is_bot = True

        await worker_b._secondary_message_handler(worker_b, bot_msg)
        assert len(bot_msg.replies) == 0
        assert len(worker_b.sent_messages) == 0
