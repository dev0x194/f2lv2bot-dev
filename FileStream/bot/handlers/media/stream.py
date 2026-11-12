import asyncio
import logging
import traceback

logger = logging.getLogger(__name__)
from FileStream.bot import FileStream, multi_clients
from FileStream.bot.clients import is_secondary_worker
from FileStream.bot.helpers.bot_utils import verify_user, gen_link, is_channel_banned, is_channel_exist, get_public_file_context, is_internal_service_chat
from FileStream.core.media.file_properties import get_message_media_type
from FileStream.database.database import Database
from FileStream.services.file_service import FileService
from FileStream.core.runtime.optional_channels import safe_send_optional_message
from FileStream.config import Telegram
from FileStream.bot.messages import ChannelLogMessages, FileMessages, ErrorMessages, MilestoneMessages
from FileStream.services.analytics import track_event, AnalyticsEvents
from pyrogram import filters, Client
from pyrogram.errors import FloodWait
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ChatAction, ParseMode

db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)
file_service = FileService(db, multi_clients)

MILESTONE_THRESHOLDS = (10, 25, 50, 100)


async def append_post_success_tip_if_eligible(user_id: int | None, stream_text: str) -> str:
    """Contextual onboarding tip and informational milestones for users."""
    if not user_id:
        return stream_text
    try:
        file_count = await db.total_files(user_id)

        # 1. Informational milestone check (fires once per threshold)
        if file_count in MILESTONE_THRESHOLDS:
            if not await db.has_shown_milestone(user_id, file_count):
                await db.record_shown_milestone(user_id, file_count)
                await track_event(db, AnalyticsEvents.MILESTONE_REACHED, user_id=user_id, properties={"milestone": file_count})
                return f"{stream_text}\n\n{MilestoneMessages.format(file_count)}"

        # 2. Contextual onboarding tip for early active users (1-3 files and 0 folders)
        if 1 <= file_count <= 3:
            folder_count = await db.total_folders(user_id)
            if folder_count == 0:
                return f"{stream_text}\n\n{FileMessages.POST_SUCCESS_TIP}"
    except Exception:
        pass
    return stream_text

@FileStream.on_message(
    filters.private
    & (
            filters.document
            | filters.video
            | filters.video_note
            | filters.audio
            | filters.voice
            | filters.animation
            | filters.photo
    ),
    group=4,
)
async def private_receive_handler(bot: Client, message: Message):
    if is_secondary_worker(bot):
        return
    if not await verify_user(bot, message):
        return

    bot_id = getattr(bot, "id", None) or getattr(getattr(bot, "me", None), "id", None)
    raw_user_id = getattr(getattr(message, "from_user", None), "id", None)
    chat_id = getattr(getattr(message, "chat", None), "id", None)

    if raw_user_id and raw_user_id != bot_id:
        user_id = raw_user_id
    elif chat_id and chat_id != bot_id:
        user_id = chat_id
    else:
        user_id = raw_user_id or chat_id

    msg_id = getattr(message, "id", None)
    media_type = get_message_media_type(message)

    # CRITICAL ROUTING RULE: Determine active mode BEFORE entering single-file pipeline!
    from FileStream.bot.handlers.media.batch import handle_folder_file, folder_service, is_user_in_folder_mode
    from FileStream.services.folder_service import FolderState
    session = await folder_service.get_active_session_for_user(user_id, bot=bot)
    folder_active = bool(
        session
        and (session.get("state") or session.get("status")) in (FolderState.ACTIVE, FolderState.ADDING, FolderState.PROCESSING)
        and not (session.get("completing") or session.get("cancelling") or session.get("completed") or session.get("cancelled"))
    )
    session_id = session.get("session_id") if session else None

    logger.info(
        "[FOLDER] FOLDER_ROUTE_CHECK: user=%s session=%s msg_id=%s media_type=%s folder_active=%s handler=private_receive_handler",
        user_id,
        session_id,
        msg_id,
        media_type,
        folder_active,
    )

    if folder_active and session:
        logger.info(
            "[FOLDER] FOLDER_MEDIA_CAPTURED: user=%s session=%s msg_id=%s media_type=%s",
            user_id,
            session_id,
            msg_id,
            media_type,
        )
        await handle_folder_file(bot, message, user_id=user_id)
        return

    # 1. Telegram chat action for responsive visual feedback
    try:
        action = ChatAction.UPLOAD_DOCUMENT
        if getattr(message, "video", None) or getattr(message, "video_note", None) or getattr(message, "animation", None):
            action = ChatAction.UPLOAD_VIDEO
        elif getattr(message, "audio", None) or getattr(message, "voice", None):
            action = ChatAction.UPLOAD_AUDIO
        elif getattr(message, "photo", None):
            action = ChatAction.UPLOAD_PHOTO
        await bot.send_chat_action(message.chat.id, action)
    except Exception:
        pass

    # 2. Stage 1: Immediate acknowledgement / processing feedback
    status_msg = None
    try:
        status_msg = await message.reply_text(
            text=FileMessages.PROCESSING,
            quote=True,
            parse_mode=ParseMode.HTML,
        )
    except FloodWait as fw:
        logger.warning("FloodWait while sending processing status: %ss", fw.value)
        await asyncio.sleep(fw.value)
        try:
            status_msg = await message.reply_text(
                text=FileMessages.PROCESSING,
                quote=True,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    except Exception as e:
        logger.debug("Failed to send initial processing message: %s", e)

    # 3. Stage 2: Processing and link generation with FloodWait handling
    max_flood_retries = 1
    flood_attempts = 0
    inserted_id = None

    while True:
        try:
            inserted_id, _ = await file_service.process_new_file(message, user_id=user_id)
            break
        except FloodWait as e:
            flood_attempts += 1
            wait_sec = e.value or 5
            logger.warning("FloodWait in private_receive_handler: %ss (attempt %d)", wait_sec, flood_attempts)
            if Telegram.ULOG_CHANNEL:
                user_name = getattr(message.from_user, "first_name", "") or "User"
                user_id = getattr(message.from_user, "id", 0)
                await safe_send_optional_message(
                    bot,
                    "ULOG_CHANNEL",
                    Telegram.ULOG_CHANNEL,
                    text=ChannelLogMessages.upload_delayed(wait_sec, user_id, user_name),
                    disable_web_page_preview=True,
                    parse_mode=ParseMode.HTML,
                )

            # Inform user of rate-limit wait
            flood_notice = ErrorMessages.flood_wait(wait_sec)
            try:
                if status_msg:
                    await status_msg.edit_text(text=flood_notice, parse_mode=ParseMode.HTML)
                else:
                    status_msg = await message.reply_text(text=flood_notice, quote=True, parse_mode=ParseMode.HTML)
            except Exception as edit_err:
                logger.debug("Failed to send/edit FloodWait notification to user: %s", edit_err)

            if flood_attempts > max_flood_retries or wait_sec > getattr(Telegram, "SLEEP_THRESHOLD", 60):
                fail_text = ErrorMessages.UPLOAD_FAILED
                try:
                    if status_msg:
                        await status_msg.edit_text(text=fail_text, parse_mode=ParseMode.HTML)
                    else:
                        await message.reply_text(text=fail_text, quote=True, parse_mode=ParseMode.HTML)
                except Exception:
                    pass
                return

            await asyncio.sleep(wait_sec)

            # Re-update to processing message before retrying
            try:
                if status_msg:
                    await status_msg.edit_text(text=FileMessages.PROCESSING, parse_mode=ParseMode.HTML)
            except Exception:
                pass

        except RuntimeError as e:
            if Telegram.ULOG_CHANNEL:
                await safe_send_optional_message(
                    bot,
                    "ULOG_CHANNEL",
                    Telegram.ULOG_CHANNEL,
                    text=ChannelLogMessages.UPLOAD_FAILED,
                    disable_web_page_preview=True,
                    parse_mode=ParseMode.HTML,
                )
            logger.warning("Storage processing failed in private_receive_handler: %s", e)
            fail_text = ErrorMessages.UPLOAD_FAILED
            try:
                await track_event(db, AnalyticsEvents.UPLOAD_FAILURE, user_id=getattr(message.from_user, "id", None))
            except Exception:
                pass
            try:
                if status_msg:
                    await status_msg.edit_text(text=fail_text, parse_mode=ParseMode.HTML)
                else:
                    await message.reply_text(text=fail_text, quote=True, parse_mode=ParseMode.HTML)
            except Exception:
                pass
            return

        except Exception as e:
            if Telegram.ULOG_CHANNEL:
                await safe_send_optional_message(
                    bot,
                    "ULOG_CHANNEL",
                    Telegram.ULOG_CHANNEL,
                    text=ChannelLogMessages.UPLOAD_FAILED,
                    disable_web_page_preview=True,
                    parse_mode=ParseMode.HTML,
                )
            logger.error("Unexpected error in private_receive_handler processing file: %s", e, exc_info=True)
            fail_text = ErrorMessages.UPLOAD_FAILED
            try:
                await track_event(db, AnalyticsEvents.UPLOAD_FAILURE, user_id=getattr(message.from_user, "id", None))
            except Exception:
                pass
            try:
                if status_msg:
                    await status_msg.edit_text(text=fail_text, parse_mode=ParseMode.HTML)
                else:
                    await message.reply_text(text=fail_text, quote=True, parse_mode=ParseMode.HTML)
            except Exception:
                pass
            return

    if not inserted_id:
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
        return

    # 4. Stage 3: Replace/edit the temporary status message with Link Ready card
    user_id = message.from_user.id if getattr(message, "from_user", None) else None
    try:
        reply_markup, stream_text = await gen_link(_id=inserted_id, bot=bot)
        stream_text = await append_post_success_tip_if_eligible(user_id, stream_text)
        try:
            await track_event(db, AnalyticsEvents.UPLOAD_SUCCESS, user_id=user_id, properties={"file_id": str(inserted_id)})
        except Exception:
            pass
        if status_msg:
            try:
                await status_msg.edit_text(
                    text=stream_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=reply_markup,
                )
                return
            except Exception as edit_err:
                logger.debug("Failed to edit processing message with link, falling back to reply_text: %s", edit_err)

        await message.reply_text(
            text=stream_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
            quote=True
        )
    except FloodWait as e:
        logger.warning("FloodWait while generating/sending link: %ss", e.value)
        await asyncio.sleep(e.value)
        try:
            reply_markup, stream_text = await gen_link(_id=inserted_id, bot=bot)
            stream_text = await append_post_success_tip_if_eligible(user_id, stream_text)
            if status_msg:
                try:
                    await status_msg.edit_text(
                        text=stream_text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                        reply_markup=reply_markup,
                    )
                    return
                except Exception:
                    pass
            await message.reply_text(
                text=stream_text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
                quote=True
            )
        except Exception as retry_err:
            logger.error("Error after FloodWait in gen_link: %s", retry_err, exc_info=True)
            fail_text = ErrorMessages.UPLOAD_FAILED
            try:
                if status_msg:
                    await status_msg.edit_text(text=fail_text, parse_mode=ParseMode.HTML)
                else:
                    await message.reply_text(text=fail_text, quote=True, parse_mode=ParseMode.HTML)
            except Exception:
                pass
    except Exception as e:
        if Telegram.ULOG_CHANNEL:
            await safe_send_optional_message(
                bot,
                "ULOG_CHANNEL",
                Telegram.ULOG_CHANNEL,
                text=ChannelLogMessages.UPLOAD_FAILED,
                disable_web_page_preview=True,
                parse_mode=ParseMode.HTML,
            )
        logger.error("Error generating link in private_receive_handler: %s", e, exc_info=True)
        fail_text = ErrorMessages.UPLOAD_FAILED
        try:
            if status_msg:
                await status_msg.edit_text(text=fail_text, parse_mode=ParseMode.HTML)
            else:
                await message.reply_text(text=fail_text, quote=True, parse_mode=ParseMode.HTML)
        except Exception:
            pass


@FileStream.on_message(
    filters.channel
    & ~filters.forwarded
    & (
            filters.document
            | filters.video
            | filters.video_note
            | filters.audio
            | filters.voice
            | filters.photo
    )
)
async def channel_receive_handler(bot: Client, message: Message):
    if is_secondary_worker(bot):
        return
    if is_internal_service_chat(getattr(message.chat, "id", None)):
        return
    if await is_channel_banned(bot, message):
        return
    await is_channel_exist(bot, message)

    try:
        inserted_id, _ = await file_service.process_new_file(message)
        if not inserted_id:
            return
        _, _, public_url = await get_public_file_context(str(inserted_id), bot=bot)
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=message.id,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("▶️ Watch Now", url=public_url)]])
        )

    except FloodWait as w:
        logger.warning("Sleeping for %ss due to FloodWait in channel_receive_handler", w.value)
        print(f"Sleeping for {str(w.value)}s", flush=True)
        await asyncio.sleep(w.value)
        if Telegram.ULOG_CHANNEL:
            await safe_send_optional_message(
                bot,
                "ULOG_CHANNEL",
                Telegram.ULOG_CHANNEL,
                text=ChannelLogMessages.channel_upload_delayed(w.value, message.chat.title),
                disable_web_page_preview=True,
                parse_mode=ParseMode.HTML,
            )
        try:
            inserted_id, _ = await file_service.process_new_file(message)
            if inserted_id:
                _, _, public_url = await get_public_file_context(str(inserted_id), bot=bot)
                await bot.edit_message_reply_markup(
                    chat_id=message.chat.id,
                    message_id=message.id,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("▶️ Watch Now", url=public_url)]])
                )
        except Exception as retry_err:
            logger.error("Error retrying channel message edit after FloodWait: %s", retry_err, exc_info=True)
    except Exception as e:
        if Telegram.ULOG_CHANNEL:
            await safe_send_optional_message(
                bot,
                "ULOG_CHANNEL",
                Telegram.ULOG_CHANNEL,
                text=ChannelLogMessages.CHANNEL_UPLOAD_FAILED,
                disable_web_page_preview=True,
                parse_mode=ParseMode.HTML,
            )
        logger.error("Can't Edit Broadcast Message! Error: %s", e, exc_info=True)
        print(ChannelLogMessages.channel_edit_permission_error(e), flush=True)
