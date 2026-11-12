import logging
import math
import html
import time
import asyncio
from FileStream import __version__
from FileStream.bot import FileStream, multi_clients
from FileStream.server.exceptions import FileNotFound
from FileStream.bot.helpers.bot_utils import gen_linkx, verify_user, gen_file_list_button, reply_with_optional_photo, get_sync_client, file_visible_to_bot
from FileStream.config import Telegram
from FileStream.database.database import Database
from FileStream.bot.helpers.translation import BUTTON
from FileStream.bot.messages import ErrorMessages, FileMessages, PublicMessages, LANG
from FileStream.bot.helpers.client_identity import get_bot_name, get_bot_username
from FileStream.core.media.file_properties import ensure_flog_media_exists, get_file_ids
from FileStream.bot.helpers.legal import build_bot_policy_text
from pyrogram import filters, Client, raw
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from pyrogram.enums.parse_mode import ParseMode
from FileStream.bot.clients import is_secondary_worker

db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)

async def delete_later(filex, message=None, delay=3600):
    await asyncio.sleep(delay)
    try:
        if filex:
            await filex.delete()
    except Exception:
        pass
    try:
        if message:
            await message.delete()
    except Exception:
        pass

import re
from dataclasses import dataclass
from FileStream.bot.helpers.bot_utils import (
    gen_linkx,
    verify_user,
    gen_file_list_button,
    reply_with_optional_photo,
    get_sync_client,
    file_visible_to_bot,
    reset_user_interaction_state,
)

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class StartContext:
    command: str
    bot_username: str | None
    payload: str
    is_home: bool


_START_CMD_RE = re.compile(
    r"^/([a-zA-Z0-9_]+)(?:@([a-zA-Z0-9_]+))?(?:[\s\xa0]+([\s\S]*))?$",
    re.UNICODE,
)


def parse_start_context(message: Message) -> StartContext:
    """Safely parse command, bot username, and deep-link payload from a Telegram message."""
    raw_text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    raw_text = raw_text.strip()

    match = _START_CMD_RE.match(raw_text)
    if match:
        cmd, bot_user, payload_part = match.groups()
        cmd = (cmd or "").lower()
        payload = (payload_part or "").strip()
        return StartContext(
            command=cmd,
            bot_username=bot_user,
            payload=payload,
            is_home=(not bool(payload)),
        )

    command_list = getattr(message, "command", None) or []
    if command_list:
        cmd = str(command_list[0]).lstrip("/").split("@", 1)[0].lower()
        payload = " ".join(command_list[1:]).strip() if len(command_list) > 1 else ""
        return StartContext(
            command=cmd,
            bot_username=None,
            payload=payload,
            is_home=(not bool(payload)),
        )

    return StartContext(
        command="",
        bot_username=None,
        payload="",
        is_home=True,
    )


def render_home_content(
    bot: Client,
    user=None,
    is_returning: bool = False,
    recent_info: str | None = None,
) -> tuple[str, InlineKeyboardMarkup, str]:
    """Single canonical builder for Home presentation text, reply markup, and media."""
    bot_username = get_bot_username(bot)
    text = LANG.START_TEXT.format(user, bot_username)
    reply_markup = BUTTON.start_buttons(bot)
    photo = Telegram.bot_media(bot, "START_PIC")
    return text, reply_markup, photo


async def show_home(bot: Client, target: Message | CallbackQuery, is_returning: bool | None = None) -> Message | None:
    """Render canonical Home screen to a Message (via reply) or CallbackQuery (via edit)."""
    if isinstance(target, CallbackQuery):
        try:
            await target.answer()
        except Exception:
            pass

    user = getattr(target, "from_user", None)
    text, reply_markup, photo = render_home_content(bot, user)

    if isinstance(target, CallbackQuery):
        from FileStream.bot.handlers.system import callback
        return await callback.edit_message(
            target,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
            photo=photo,
            bot=bot,
        )
    else:
        return await reply_with_optional_photo(
            target,
            photo,
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
            bot=bot,
        )



async def handle_stream_payload(bot: Client, message: Message, file_id: str) -> None:
    try:
        file_check = await db.get_file(file_id)
        file_id_str = str(file_check['_id'])
        reply_markup, stream_text = await gen_linkx(m=message, _id=file_id_str, bot=bot)
        await message.reply_text(
            text=stream_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
            quote=True,
        )
    except FileNotFound:
        await message.reply_text(ErrorMessages.FILE_NOT_FOUND, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error("Error handling stream payload %r: %s", file_id, e)
        await message.reply_text(ErrorMessages.GENERIC, parse_mode=ParseMode.HTML)


async def handle_file_payload(bot: Client, message: Message, file_id: str) -> None:
    try:
        file_check = await db.get_file(file_id)
        await ensure_flog_media_exists(file_check, bot=get_sync_client(bot), prune_stale=True, db_instance=db)
        await get_file_ids(bot, str(file_check["_id"]), multi_clients, message)
        file_check = await db.get_file(file_id)
        if not file_visible_to_bot(file_check, bot):
            raise FileNotFound
        cached_file_id = (file_check.get("file_ids") or {}).get(str(bot.id)) or file_check.get("file_id")
        file_name = file_check.get('file_name') or "file"
        safe_name = html.escape(file_name)
        if len(safe_name) > 1000:
            safe_name = safe_name[:1000] + "…"
        filex = await message.reply_cached_media(
            file_id=cached_file_id,
            caption=FileMessages.CAPTION.format(safe_name),
            parse_mode=ParseMode.HTML,
        )
        asyncio.create_task(delete_later(filex, message))
    except FileNotFound:
        await message.reply_text(ErrorMessages.FILE_NOT_FOUND, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error("Error handling file payload %r: %s", file_id, e)
        await message.reply_text(ErrorMessages.GENERIC, parse_mode=ParseMode.HTML)


async def handle_open_payload(bot: Client, message: Message, public_id: str) -> None:
    try:
        file_check, _ = await db.resolve_public_file(public_id)
        await ensure_flog_media_exists(file_check, bot=get_sync_client(bot), prune_stale=True, db_instance=db)
        await get_file_ids(bot, str(file_check["_id"]), multi_clients, message)
        file_check = await db.get_file(str(file_check["_id"]))
        if not file_visible_to_bot(file_check, bot):
            raise FileNotFound
        cached_file_id = (file_check.get("file_ids") or {}).get(str(bot.id)) or file_check.get("file_id")
        file_name = file_check.get('file_name') or "file"
        safe_name = html.escape(file_name)
        if len(safe_name) > 1000:
            safe_name = safe_name[:1000] + "…"
        filex = await message.reply_cached_media(
            file_id=cached_file_id,
            caption=FileMessages.CAPTION.format(safe_name),
            parse_mode=ParseMode.HTML,
        )
        asyncio.create_task(delete_later(filex, message))
    except FileNotFound:
        await message.reply_text(ErrorMessages.FILE_NOT_FOUND, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error("Error handling open payload %r: %s", public_id, e)
        await message.reply_text(ErrorMessages.GENERIC, parse_mode=ParseMode.HTML)


async def handle_report_payload(bot: Client, message: Message, payload: str) -> None:
    from FileStream.bot.handlers.system.nsfw_report import process_report
    target_type = "file" if payload.startswith("report_file_") else "folder"
    target_id = payload.split("_", 2)[2]
    await process_report(bot, message, target_type, target_id)


async def handle_referral_payload(bot: Client, message: Message, payload: str) -> None:
    parts = payload.split("_", 1)
    referrer_str = parts[1] if len(parts) > 1 else ""
    user_id = message.from_user.id if message.from_user else None
    if user_id and referrer_str.isdigit():
        referrer_id = int(referrer_str)
        attributed = await db.attribute_referral(user_id, referrer_id)
        if attributed:
            from FileStream.services.analytics import track_event, AnalyticsEvents
            await track_event(
                db,
                AnalyticsEvents.REFERRAL_ATTRIBUTED,
                user_id=user_id,
                properties={"referrer_id": referrer_id},
            )
            try:
                from FileStream.bot.messages import ReferralMessages
                await message.reply_text(ReferralMessages.WELCOME, quote=True)
            except Exception:
                pass
    await show_home(bot, message)


async def handle_invalid_payload(bot: Client, message: Message, payload: str) -> None:
    await message.reply_text(ErrorMessages.INVALID_LINK, parse_mode=ParseMode.HTML)


async def handle_start_payload(bot: Client, message: Message, payload: str) -> None:
    """Route deep-link payload to the appropriate dedicated sub-handler."""
    if payload.startswith("stream_"):
        file_id = payload[7:]
        await handle_stream_payload(bot, message, file_id)
    elif payload.startswith("file_"):
        file_id = payload[5:]
        await handle_file_payload(bot, message, file_id)
    elif payload.startswith("open_"):
        public_id = payload[5:]
        await handle_open_payload(bot, message, public_id)
    elif payload.startswith("ref_") or payload.startswith("referral_"):
        await handle_referral_payload(bot, message, payload)
    elif payload.startswith("report_file_") or payload.startswith("report_folder_"):
        await handle_report_payload(bot, message, payload)
    else:
        await handle_invalid_payload(bot, message, payload)


@FileStream.on_message(filters.command('start') & filters.private)
async def start(bot: Client, message: Message):
    if is_secondary_worker(bot):
        return
    # Step 1: Reset transient interaction state (folder builder sessions, rename, donation)
    user_id = message.from_user.id if getattr(message, "from_user", None) else None
    if user_id:
        await reset_user_interaction_state(bot=bot, user_id=user_id)

    # Step 2: Verify and authorize user (auth, ban, and force-sub checks)
    if not await verify_user(bot, message):
        return

    # Step 3: Parse /start command context safely
    context = parse_start_context(message)

    # Step 4: Route according to context
    if context.is_home:
        await show_home(bot, message)
    else:
        await handle_start_payload(bot, message, context.payload)

@FileStream.on_message(filters.private & filters.command(["about"]))
async def about(bot, message):
    if is_secondary_worker(bot):
        return
    if not await verify_user(bot, message):
        return
    await reply_with_optional_photo(
        message,
        Telegram.bot_media(bot, "ABOUT_PIC"),
        LANG.ABOUT_TEXT.format(get_bot_name(bot), __version__),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=BUTTON.about_buttons(bot),
        bot=bot,
    )

@FileStream.on_message((filters.command('help')) & filters.private)
async def help_handler(bot, message):
    if is_secondary_worker(bot):
        return
    if not await verify_user(bot, message):
        return
    await reply_with_optional_photo(
        message,
        Telegram.bot_media(bot, "HELP_PIC"),
        LANG.HELP_TEXT.format(Telegram.OWNER_ID),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=BUTTON.help_buttons(bot),
        bot=bot,
    )


@FileStream.on_message(filters.command(["ping"]) & filters.private)
async def ping_handler(bot: Client, message: Message):
    if is_secondary_worker(bot):
        return
    try:
        if not await verify_user(bot, message):
            return

        start = time.perf_counter()
        if getattr(bot, "is_connected", False) is True:
            try:
                ping_id = 0
                if hasattr(bot, "rnd_id") and callable(bot.rnd_id):
                    res = bot.rnd_id()
                    if asyncio.iscoroutine(res):
                        await res
                    elif isinstance(res, int):
                        ping_id = res
                await asyncio.wait_for(
                    bot.invoke(raw.functions.ping.Ping(ping_id=ping_id)),
                    timeout=2.0,
                )
            except Exception:
                pass
        latency_ms = max(0.0, (time.perf_counter() - start) * 1000)

        await message.reply_text(
            text=PublicMessages.PING.format(latency=f"{latency_ms:.1f}"),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=BUTTON.ping_buttons(bot),
            quote=True,
        )
    except Exception as e:
        logging.error("Error handling /ping: %s", e)
        try:
            await message.reply_text(ErrorMessages.GENERIC, parse_mode=ParseMode.HTML)
        except Exception:
            pass


@FileStream.on_message(filters.command(["legal", "privacy", "terms"]) & filters.private)
async def legal_handler(bot, message):
    if is_secondary_worker(bot):
        return
    if not await verify_user(bot, message):
        return
    command = (getattr(message, "command", None) or ["legal"])[0].lower()
    media_key = "PRIVACY_PIC" if command == "privacy" else "LEGAL_PIC"
    await reply_with_optional_photo(
        message,
        Telegram.bot_media(bot, media_key),
        build_bot_policy_text(command, bot),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=BUTTON.legal_buttons(bot, command=command),
        bot=bot,
    )


# ---------------------------------------------------------------------------------------------------

@FileStream.on_message(filters.command('files') & filters.private)
async def my_files(bot: Client, message: Message):
    if is_secondary_worker(bot):
        return
    if not await verify_user(bot, message):
        return

    file_list, total_files = await gen_file_list_button(1, message.from_user.id, bot=bot)
    await reply_with_optional_photo(
        message,
        Telegram.bot_media(bot, "FILE_PIC"),
        PublicMessages.DASHBOARD_FILES.format(total_files),
        reply_markup=InlineKeyboardMarkup(file_list),
        parse_mode=ParseMode.HTML,
        bot=bot,
    )
