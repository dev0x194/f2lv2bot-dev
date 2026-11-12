
import asyncio
import math
import html
import logging
import os
import time
import datetime
from typing import Union
from FileStream import __version__
from pyrogram.errors import UserNotParticipant, FloodWait
from pyrogram.enums.parse_mode import ParseMode
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from FileStream.bot.helpers.translation import BUTTON
from FileStream.bot.messages import ErrorMessages, ForceSubMessages, PublicMessages, LANG
from FileStream.database.database import Database
from FileStream.core.runtime.human_readable import humanbytes
from FileStream.core.media.category import detect_category
from FileStream.config import Telegram, Server
from FileStream.bot import FileStream
from FileStream.core.runtime.public_links import build_public_file_url, build_public_folder_url, build_telegram_share_link
from FileStream.core.runtime.flog_sync import reconcile_flog_storage
from FileStream.bot.helpers.client_identity import get_bot_name, get_bot_username
from FileStream.core.runtime.flog_channels import resolve_file_flog_mode
from FileStream.bot.helpers.legal import build_bot_policy_text

logger = logging.getLogger(__name__)
db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
FILE_LIST_PAGE_SIZE = 10
_FORCE_SUB_PROMPTS: dict[int, dict[str, object]] = {}
FORCE_SUB_PROMPT_TTL = 600
_FORCE_SUB_CHANNEL_PLACEHOLDERS = {
    "",
    "telegram",
    "your_updates_channel_username",
    "your updates channel username",
    "updates_channel_username",
}


def get_sync_client(bot=None):
    if getattr(FileStream, "is_connected", False):
        return FileStream
    return bot or FileStream


def get_bot_scope(bot=None) -> tuple[int | None, bool]:
    scoped_bot = bot or FileStream
    bot_id = getattr(scoped_bot, "id", None)
    include_legacy = int(getattr(scoped_bot, "client_index", 0) or 0) == 0
    try:
        bot_id = int(bot_id) if bot_id not in (None, "") else None
    except Exception:
        bot_id = None
    return bot_id, include_legacy


def file_visible_to_bot(file_info: dict | None, bot=None) -> bool:
    if not file_info:
        return False
    bot_id, include_legacy = get_bot_scope(bot)
    raw_source_bot_id = file_info.get("source_bot_id")
    if raw_source_bot_id in (None, ""):
        return include_legacy
    try:
        return bot_id is not None and int(raw_source_bot_id) == int(bot_id)
    except Exception:
        return False


async def get_force_sub_trial_usage(bot, user_id: int) -> tuple[int, int]:
    free_tasks = max(int(getattr(Telegram, "FORCE_SUB_FREE_TASKS", 0) or 0), 0)
    if free_tasks <= 0:
        return 0, 0
    bot_id, _ = get_bot_scope(bot)
    used_tasks = await db.get_bot_task_count(int(user_id), bot_id)
    return used_tasks, free_tasks


def resolve_media_source(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None

    lowered = raw.lower()
    if lowered.startswith(("http://", "https://")):
        return raw

    candidates = []
    if os.path.isabs(raw):
        candidates.append(raw)
    else:
        candidates.append(os.path.join(PROJECT_ROOT, raw))
        candidates.append(os.path.join(PROJECT_ROOT, "assets", "bot", raw))
        candidates.append(os.path.join(PROJECT_ROOT, "images", raw))

    for candidate in candidates:
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)

    return raw


def get_bot_media_cache(bot) -> dict:
    if bot is None:
        return {}
    try:
        cache = getattr(bot, "_media_file_id_cache", None)
    except Exception:
        cache = None
    if not isinstance(cache, dict):
        cache = {}
        try:
            bot._media_file_id_cache = cache
        except Exception:
            pass
    return cache


def is_local_media_path(source: str | None) -> bool:
    if not source or not isinstance(source, str):
        return False
    lowered = source.strip().lower()
    if lowered.startswith(("http://", "https://")):
        return False
    return os.path.isfile(source)


def get_cached_media_file_id(bot, media_source: str | None) -> str | None:
    if not media_source or not bot:
        return None
    cache = get_bot_media_cache(bot)
    val = cache.get(media_source)
    return str(val) if isinstance(val, str) and val.strip() else None


def cache_media_file_id(bot, media_source: str | None, file_id: str | None, raw_source: str | None = None) -> None:
    if not media_source or not file_id or not bot:
        return
    if not isinstance(file_id, str) or not file_id.strip():
        return
    if str(media_source).strip().lower().startswith(("http://", "https://")):
        return
    if raw_source and str(raw_source).strip().lower().startswith(("http://", "https://")):
        return
    cache = get_bot_media_cache(bot)
    clean_id = file_id.strip()
    cache[media_source] = clean_id
    if raw_source and raw_source != media_source:
        cache[raw_source] = clean_id


def invalidate_cached_media_file_id(bot, media_source: str | None, raw_source: str | None = None) -> None:
    if not media_source or not bot:
        return
    cache = get_bot_media_cache(bot)
    cache.pop(media_source, None)
    if raw_source:
        cache.pop(raw_source, None)


async def reply_with_optional_photo(
    message: Message,
    photo: str | None,
    text: str,
    *,
    reply_markup=None,
    parse_mode=None,
    disable_web_page_preview: bool = True,
    quote: bool | None = None,
    bot=None,
):
    resolved_photo = resolve_media_source(photo)
    target_bot = bot or getattr(message, "_client", None)
    if resolved_photo:
        photo_to_send = resolved_photo
        is_local = is_local_media_path(resolved_photo)
        if is_local and target_bot:
            cached_id = get_cached_media_file_id(target_bot, resolved_photo) or get_cached_media_file_id(target_bot, photo)
            if cached_id:
                photo_to_send = cached_id
        try:
            sent = await message.reply_photo(
                photo=photo_to_send,
                caption=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                quote=quote,
            )
            if target_bot and sent and getattr(sent, "photo", None):
                fid = getattr(sent.photo, "file_id", None)
                if isinstance(fid, str) and fid.strip():
                    cache_media_file_id(target_bot, resolved_photo, fid, raw_source=photo)
            return sent
        except Exception as exc:
            if photo_to_send != resolved_photo and target_bot:
                invalidate_cached_media_file_id(target_bot, resolved_photo, raw_source=photo)
                try:
                    sent = await message.reply_photo(
                        photo=resolved_photo,
                        caption=text,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                        quote=quote,
                    )
                    if sent and getattr(sent, "photo", None):
                        fid = getattr(sent.photo, "file_id", None)
                        if isinstance(fid, str) and fid.strip():
                            cache_media_file_id(target_bot, resolved_photo, fid, raw_source=photo)
                    return sent
                except Exception as retry_exc:
                    logging.warning("Failed to send configured photo %r on retry: %s", photo, retry_exc)
            else:
                logging.warning("Failed to send configured photo %r: %s", photo, exc)

    return await message.reply_text(
        text=text,
        parse_mode=parse_mode,
        disable_web_page_preview=disable_web_page_preview,
        reply_markup=reply_markup,
        quote=quote,
    )


async def send_with_optional_photo(
    bot,
    chat_id: int,
    photo: str | None,
    text: str,
    *,
    parse_mode=ParseMode.HTML,
    disable_web_page_preview: bool = True,
    reply_markup=None,
):
    resolved_photo = resolve_media_source(photo)
    if resolved_photo:
        photo_to_send = resolved_photo
        is_local = is_local_media_path(resolved_photo)
        if is_local and bot:
            cached_id = get_cached_media_file_id(bot, resolved_photo) or get_cached_media_file_id(bot, photo)
            if cached_id:
                photo_to_send = cached_id
        try:
            sent = await bot.send_photo(
                chat_id=chat_id,
                photo=photo_to_send,
                caption=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
            if bot and sent and getattr(sent, "photo", None):
                fid = getattr(sent.photo, "file_id", None)
                if isinstance(fid, str) and fid.strip():
                    cache_media_file_id(bot, resolved_photo, fid, raw_source=photo)
            return sent
        except Exception as exc:
            if photo_to_send != resolved_photo and bot:
                invalidate_cached_media_file_id(bot, resolved_photo, raw_source=photo)
                try:
                    sent = await bot.send_photo(
                        chat_id=chat_id,
                        photo=resolved_photo,
                        caption=text,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                    )
                    if sent and getattr(sent, "photo", None):
                        fid = getattr(sent.photo, "file_id", None)
                        if isinstance(fid, str) and fid.strip():
                            cache_media_file_id(bot, resolved_photo, fid, raw_source=photo)
                    return sent
                except Exception as retry_exc:
                    logging.warning("Failed to send configured photo %r on retry: %s", photo, retry_exc)
            else:
                logging.warning("Failed to send configured photo %r: %s", photo, exc)

    return await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
        disable_web_page_preview=disable_web_page_preview,
        reply_markup=reply_markup,
    )


async def get_public_file_context(file_ref, bot=None) -> tuple[dict, dict, str]:
    from FileStream.services.link_service import link_service
    return await link_service.get_public_file_context(file_ref, bot=bot)


async def get_public_folder_context(folder_ref, bot=None) -> tuple[dict, dict, str]:
    from FileStream.services.folder_service import folder_service
    return await folder_service.get_public_folder_context(folder_ref, bot=bot)


def format_expiry_countdown(total_seconds: float | int) -> str:
    try:
        remaining = max(int(total_seconds), 0)
    except Exception:
        return "Unknown"

    if remaining <= 0:
        return "Expired"

    days, remainder = divmod(remaining, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts[:2]) + " left"


def describe_public_link_expiry(file_info: dict, link_doc: dict) -> dict[str, str | bool]:
    storage_mode = resolve_file_flog_mode(file_info)
    ttl_hours = int(Server.PUBLIC_FILE_EXPIRE_HOURS or 0)
    expires_at = link_doc.get("expires_at")

    if storage_mode == "admin" or expires_at is None:
        return {
            "storage_label": "ADMIN (Permanent)",
            "countdown_text": "Never expires",
            "absolute_text": "Permanent",
            "share_text": "♾ Permanent link",
            "is_permanent": True,
        }

    expires_at_value = float(expires_at)
    remaining = expires_at_value - time.time()
    absolute_text = datetime.datetime.utcfromtimestamp(expires_at_value).strftime("%Y-%m-%d %H:%M UTC")
    countdown_text = format_expiry_countdown(remaining)
    main_label = f"MAIN ({ttl_hours}h)" if ttl_hours > 0 else "MAIN"
    if remaining <= 0:
        share_text = "⏳ Link expired"
    else:
        share_text = f"⏳ Available for {format_expiry_countdown(remaining).replace(' left', '')}"

    return {
        "storage_label": main_label,
        "countdown_text": countdown_text,
        "absolute_text": absolute_text,
        "share_text": share_text,
        "is_permanent": False,
    }


def build_forward_share_text(file_name: str, bot_username: str | None, expiry_info: dict[str, str | bool]) -> str:
    normalized_bot = str(bot_username or "").strip().lstrip("@")
    lines = [
        f"📄 {file_name or 'file'}",
        str(expiry_info.get("share_text") or "⏳ Limited-time link"),
    ]
    if normalized_bot:
        lines.append(f"🤖 @{normalized_bot}")
    return "\n".join(lines)

async def get_invite_link(bot, chat_id: Union[str, int]):
    while True:
        try:
            invite_link = await bot.create_chat_invite_link(chat_id=chat_id)
            return invite_link
        except FloodWait as e:
            logger.warning("Sleep of %ss caused by FloodWait in get_invite_link...", e.value)
            print(f"Sleep of {e.value}s caused by FloodWait ...", flush=True)
            await asyncio.sleep(e.value)
            continue
        except Exception as e:
            logger.error("Failed to create invite link: %s", e, exc_info=True)
            print(f"Failed to create invite link: {e}", flush=True)
            return None


def _resolve_force_sub_chat() -> Union[str, int, None]:
    if Telegram.FORCE_SUB_ID:
        fsid = str(Telegram.FORCE_SUB_ID).lstrip("@").strip()
        if fsid.startswith("-100") and fsid.lstrip("-").isdigit():
            return int(fsid)
        if fsid.lstrip("-").isdigit():
            return int(fsid)
        if fsid:
            return fsid

    channel = str(Telegram.UPDATES_CHANNEL or "").lstrip("@").strip()
    if channel.lower() in _FORCE_SUB_CHANNEL_PLACEHOLDERS:
        return None
    return channel or None


async def _build_force_sub_join_url(bot, channel_chat_id: Union[str, int, None]) -> str | None:
    if not channel_chat_id:
        return None

    invite_link = await get_invite_link(bot, chat_id=channel_chat_id)
    if invite_link and getattr(invite_link, "invite_link", None):
        return invite_link.invite_link

    if isinstance(channel_chat_id, str) and channel_chat_id and not channel_chat_id.lstrip("-").isdigit():
        return f"https://t.me/{channel_chat_id.lstrip('@')}"

    channel = str(Telegram.UPDATES_CHANNEL or "").lstrip("@").strip()
    if channel.lower() in _FORCE_SUB_CHANNEL_PLACEHOLDERS:
        return None
    if channel and not channel.lstrip("-").isdigit():
        return f"https://t.me/{channel}"

    return None


def _force_sub_markup(join_url: str | None):
    rows = []
    if join_url:
        rows.append([InlineKeyboardButton("🔔 Join Channel", url=join_url)])
    rows.append([
        InlineKeyboardButton("✅ I Joined", callback_data="forcesub_retry"),
        InlineKeyboardButton("❌ Close", callback_data="close"),
    ])
    return InlineKeyboardMarkup(rows)


def _get_force_sub_prompt(user_id: int) -> dict[str, object] | None:
    prompt = _FORCE_SUB_PROMPTS.get(int(user_id))
    if not prompt:
        return None
    if float(prompt.get("expires_at", 0)) <= time.time():
        _FORCE_SUB_PROMPTS.pop(int(user_id), None)
        return None
    return prompt


def _extract_force_sub_request(message: Message | None) -> dict[str, str]:
    if not message:
        return {}

    raw_text = str(getattr(message, "text", "") or getattr(message, "caption", "") or "").strip()
    if not raw_text.startswith("/"):
        return {}

    parts = raw_text.split(None, 1)
    command = parts[0].split("@", 1)[0].lstrip("/").strip().lower()
    payload = parts[1].strip() if len(parts) > 1 else ""

    if command not in {"start", "help", "about", "legal", "privacy", "files"}:
        return {}

    return {"command": command, "payload": payload}


def _is_force_sub_exempt_request(message: Message | None) -> bool:
    request = _extract_force_sub_request(message)
    return str(request.get("command") or "").lower() in {"legal", "privacy"}


def _store_force_sub_prompt(user_id: int, sent_message: Message, source_message: Message | None = None) -> None:
    request = _extract_force_sub_request(source_message)
    _FORCE_SUB_PROMPTS[int(user_id)] = {
        "chat_id": int(sent_message.chat.id),
        "message_id": int(sent_message.id),
        "kind": "photo" if getattr(sent_message, "photo", None) else "text",
        "expires_at": time.time() + FORCE_SUB_PROMPT_TTL,
        "command": request.get("command", ""),
        "payload": request.get("payload", ""),
    }


def clear_force_sub_prompt(user_id: int) -> None:
    _FORCE_SUB_PROMPTS.pop(int(user_id), None)


async def reset_user_interaction_state(
    first_arg: int | object = None,
    user_id: int | None = None,
    bot: object = None,
    bot_id: int | None = None,
) -> None:
    """Reset transient user interaction state (folder builder sessions, rename pending,
    donation custom input).

    Idempotent and safe to call multiple times. Does NOT clear permanent DB records,
    files, or force-sub continuation state.
    """
    if isinstance(first_arg, int):
        normalized_user_id = first_arg
    elif user_id is not None:
        normalized_user_id = int(user_id)
    else:
        return

    client_bot = bot if bot is not None else (first_arg if not isinstance(first_arg, int) else None)

    try:
        from FileStream.bot.handlers.media.batch import folderm_sessions

        session = folderm_sessions.pop(normalized_user_id, None)
        if isinstance(session, dict) and client_bot is not None:
            chat_id = session.get("chat_id")
            message_id = session.get("status_msg_id")
            if chat_id and message_id:
                try:
                    await client_bot.delete_messages(chat_id=chat_id, message_ids=message_id)
                except Exception:
                    pass
    except Exception:
        pass

    try:
        from FileStream.bot.handlers.folder.folder_manager import rename_pending

        rename_pending.pop(normalized_user_id, None)
    except Exception:
        pass

    try:
        from FileStream.bot.handlers.user.donation import pending_custom_amounts

        pending_custom_amounts.pop(normalized_user_id, None)
    except Exception:
        pass


clear_user_temporary_tasks = reset_user_interaction_state


def get_all_internal_channel_ids() -> set[int]:
    from FileStream.config import Telegram
    from os import environ as env

    internal_ids: set[int] = set()

    for raw in (
        getattr(Telegram, "BIN_CHANNEL", None),
        getattr(Telegram, "BINBIN_CHANNEL", None),
        getattr(Telegram, "FLOG_CHANNEL", None),
        getattr(Telegram, "ADMIN_FLOG_CHANNEL", None),
        getattr(Telegram, "ULOG_CHANNEL", None),
        getattr(Telegram, "NUDENET_CHANNEL", None),
        getattr(Telegram, "FORCE_SUB_ID", None),
    ):
        if raw not in (None, ""):
            try:
                internal_ids.add(int(raw))
            except Exception:
                pass

    for raw_env_key in (
        "BIN_CHANNEL",
        "BINBIN_CHANNEL",
        "FLOG_CHANNEL",
        "ADMIN_FLOG_CHANNEL",
        "FLOG_CHANNEL_ADMIN",
        "ULOG_CHANNEL",
        "NUDENET_CHANNEL",
    ):
        raw_val = env.get(raw_env_key)
        if raw_val not in (None, ""):
            try:
                internal_ids.add(int(raw_val))
            except Exception:
                pass

    try:
        from FileStream.core.runtime.flog_channels import configured_flog_channels
        for ch_id in configured_flog_channels().values():
            if ch_id not in (None, ""):
                internal_ids.add(int(ch_id))
    except Exception:
        pass

    return internal_ids


def is_internal_service_chat(chat_id: int | str | None) -> bool:
    if chat_id in (None, ""):
        return False
    try:
        normalized = int(chat_id)
    except Exception:
        return False

    return normalized in get_all_internal_channel_ids()


def is_configured_channel(chat_id: int | str | None) -> bool:
    return is_internal_service_chat(chat_id)


def get_known_bot_ids() -> set[int]:
    from FileStream.bot import FileStream, multi_clients
    from FileStream.config import Telegram
    from os import environ as env
    import re

    bot_ids: set[int] = set()

    clients_to_check = [FileStream]
    try:
        clients_to_check.extend(multi_clients.values())
    except Exception:
        pass

    for c in clients_to_check:
        if c is not None:
            cid = getattr(c, "id", None) or getattr(getattr(c, "me", None), "id", None)
            if cid not in (None, "") and not hasattr(cid, "_mock_name"):
                try:
                    bot_ids.add(int(cid))
                except Exception:
                    pass

    tokens = [getattr(Telegram, "BOT_TOKEN", "")]
    for k, v in env.items():
        if re.fullmatch(r"MULTI_TOKEN\d+", str(k)):
            tokens.append(str(v or ""))

    for tok in tokens:
        if tok and ":" in tok:
            prefix = tok.split(":", 1)[0].strip()
            if prefix.isdigit():
                bot_ids.add(int(prefix))

    return bot_ids


def is_secondary_bot_id(user_id: int | str | None) -> bool:
    if user_id in (None, ""):
        return False
    try:
        uid = int(user_id)
    except Exception:
        return False

    from FileStream.bot import multi_clients
    from os import environ as env
    import re

    try:
        for idx, c in multi_clients.items():
            if idx != 0 and c is not None:
                cid = getattr(c, "id", None) or getattr(getattr(c, "me", None), "id", None)
                if cid not in (None, "") and not hasattr(cid, "_mock_name"):
                    try:
                        if int(cid) == uid:
                            return True
                    except Exception:
                        pass
    except Exception:
        pass

    for k, v in env.items():
        if re.fullmatch(r"MULTI_TOKEN\d+", str(k)):
            tok = str(v or "")
            if ":" in tok:
                prefix = tok.split(":", 1)[0].strip()
                if prefix.isdigit() and int(prefix) == uid:
                    return True

    return False


async def _delete_force_sub_prompt_message(bot, prompt: dict[str, object] | None) -> None:
    if not prompt:
        return
    try:
        await bot.delete_messages(
            chat_id=int(prompt["chat_id"]),
            message_ids=int(prompt["message_id"]),
        )
    except Exception:
        pass


async def _edit_force_sub_prompt(bot, prompt: dict[str, object], text: str, reply_markup) -> bool:
    try:
        chat_id = int(prompt["chat_id"])
        message_id = int(prompt["message_id"])
        if prompt.get("kind") == "photo":
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        else:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
        prompt["expires_at"] = time.time() + FORCE_SUB_PROMPT_TTL
        return True
    except Exception:
        return False


async def _send_or_refresh_force_sub_prompt(bot, message: Message, text: str, reply_markup):
    user_id = int(message.from_user.id)
    prompt = _get_force_sub_prompt(user_id)
    if prompt:
        await _delete_force_sub_prompt_message(bot, prompt)
        clear_force_sub_prompt(user_id)

    sent = await reply_with_optional_photo(
        message,
        Telegram.bot_media(bot, "VERIFY_PIC"),
        text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )
    if sent:
        _store_force_sub_prompt(user_id, sent, source_message=message)


async def _check_force_sub_membership(bot, user_id: int) -> str:
    channel_chat_id = _resolve_force_sub_chat()
    if not channel_chat_id:
        return "joined"
    try:
        user = await bot.get_chat_member(chat_id=channel_chat_id, user_id=user_id)
        if str(getattr(user, "status", "")).upper() == "BANNED":
            return "banned"
        return "joined"
    except UserNotParticipant:
        return "missing"
    except FloodWait as exc:
        await asyncio.sleep(exc.value)
        try:
            user = await bot.get_chat_member(chat_id=channel_chat_id, user_id=user_id)
            if str(getattr(user, "status", "")).upper() == "BANNED":
                return "banned"
            return "joined"
        except UserNotParticipant:
            return "missing"
        except Exception as retry_exc:
            logging.warning("Force-sub membership retry failed for %s in %r: %s", user_id, channel_chat_id, retry_exc)
            return "error"
    except Exception:
        logging.warning("Force-sub membership check failed for %s in %r", user_id, channel_chat_id, exc_info=True)
        return "error"

async def is_user_joined(bot, message: Message):
    if not getattr(message, "from_user", None):
        return False
    status = await _check_force_sub_membership(bot, message.from_user.id)
    if status == "joined":
        clear_force_sub_prompt(message.from_user.id)
        return True

    if status == "banned":
        await message.reply_text(
            text=LANG.BAN_TEXT.format(Telegram.OWNER_ID),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
        return False

    channel_chat_id = _resolve_force_sub_chat()
    join_url = await _build_force_sub_join_url(bot, channel_chat_id)
    reply_markup = _force_sub_markup(join_url)

    if status == "missing":
        await clear_user_temporary_tasks(bot, message.from_user.id)
        await _send_or_refresh_force_sub_prompt(
            bot,
            message,
            LANG.FORCE_SUB_TEXT.format(max(int(getattr(Telegram, "FORCE_SUB_FREE_TASKS", 0) or 0), 0)),
            reply_markup,
        )
        return False

    await clear_user_temporary_tasks(bot, message.from_user.id)
    await _send_or_refresh_force_sub_prompt(
        bot,
        message,
        LANG.FORCE_SUB_ERROR,
        reply_markup,
    )
    return False


async def _resume_force_sub_command(bot, query, prompt: dict[str, object] | None) -> bool:
    if not prompt:
        return False

    command = str(prompt.get("command") or "").strip().lower()
    payload = str(prompt.get("payload") or "").strip()
    msg = getattr(query, "message", None)
    chat = getattr(msg, "chat", None)
    chat_id = int(getattr(chat, "id", None) or getattr(query.from_user, "id", 0))
    user = query.from_user

    if command == "start":
        if not payload:
            await send_with_optional_photo(
                bot,
                chat_id,
                Telegram.bot_media(bot, "START_PIC"),
                LANG.START_TEXT.format(user.mention, get_bot_username(bot)),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=BUTTON.start_buttons(bot),
            )
            return True
        else:
            try:
                from FileStream.bot.handlers.user.start import handle_start_payload
                class _SyntheticResumeMessage:
                    def __init__(self, bot_inst, target_chat_id: int, target_user):
                        self._bot = bot_inst
                        self.chat = type("Chat", (), {"id": int(target_chat_id)})()
                        self.from_user = target_user
                        self.id = 0
                        self.text = ""

                    async def reply_text(self, text, **kwargs):
                        kwargs.pop("quote", None)
                        return await self._bot.send_message(chat_id=self.chat.id, text=text, **kwargs)

                    async def reply_cached_media(self, file_id, **kwargs):
                        return await self._bot.send_cached_media(chat_id=self.chat.id, file_id=file_id, **kwargs)

                    async def delete(self):
                        pass

                synthetic_msg = _SyntheticResumeMessage(bot, chat_id, user)
                await handle_start_payload(bot, synthetic_msg, payload)
                return True
            except Exception as exc:
                logging.error("Failed to resume start payload %r on force-sub retry: %s", payload, exc)
                return False

    if command == "help":
        await send_with_optional_photo(
            bot,
            chat_id,
            Telegram.bot_media(bot, "HELP_PIC"),
            LANG.HELP_TEXT.format(Telegram.OWNER_ID),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=BUTTON.help_buttons(bot),
        )
        return True

    if command == "about":
        await send_with_optional_photo(
            bot,
            chat_id,
            Telegram.bot_media(bot, "ABOUT_PIC"),
            LANG.ABOUT_TEXT.format(get_bot_name(bot), __version__),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=BUTTON.about_buttons(bot),
        )
        return True

    if command in {"legal", "privacy"}:
        media_key = "PRIVACY_PIC" if command == "privacy" else "LEGAL_PIC"
        await send_with_optional_photo(
            bot,
            chat_id,
            Telegram.bot_media(bot, media_key),
            build_bot_policy_text(command, bot),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=BUTTON.legal_buttons(bot),
        )
        return True

    if command == "files":
        file_list, total_files = await gen_file_list_button(1, user.id, bot=bot)
        await send_with_optional_photo(
            bot,
            chat_id,
            Telegram.bot_media(bot, "FILE_PIC"),
            PublicMessages.DASHBOARD_FILES.format(total_files),
            reply_markup=InlineKeyboardMarkup(file_list),
            parse_mode=ParseMode.HTML,
        )
        return True

    return False


async def handle_force_sub_retry(bot, query) -> None:
    if not getattr(query, "from_user", None):
        await query.answer(ForceSubMessages.ACCOUNT_MISMATCH_ALERT, show_alert=True)
        return

    status = await _check_force_sub_membership(bot, query.from_user.id)
    if status == "joined":
        prompt = _get_force_sub_prompt(query.from_user.id)
        clear_force_sub_prompt(query.from_user.id)
        try:
            await query.message.delete()
        except Exception:
            pass

        resumed = False
        try:
            resumed = await _resume_force_sub_command(bot, query, prompt)
        except Exception:
            resumed = False

        if not resumed:
            success_markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Close", callback_data="close")]]
            )
            msg = getattr(query, "message", None)
            chat = getattr(msg, "chat", None)
            target_chat_id = int(getattr(chat, "id", None) or getattr(query.from_user, "id", 0))
            await send_with_optional_photo(
                bot,
                target_chat_id,
                Telegram.bot_media(bot, "VERIFY_PIC"),
                LANG.FORCE_SUB_SUCCESS,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=success_markup,
            )

        await query.answer(ForceSubMessages.SUCCESS_ALERT, show_alert=True)
        return

    channel_chat_id = _resolve_force_sub_chat()
    join_url = await _build_force_sub_join_url(bot, channel_chat_id)
    retry_markup = _force_sub_markup(join_url)
    try:
        if getattr(query.message, "photo", None) or getattr(query.message, "caption", None):
            await query.message.edit_caption(
                caption=LANG.FORCE_SUB_STILL_REQUIRED,
                parse_mode=ParseMode.HTML,
                reply_markup=retry_markup,
            )
        else:
            await query.message.edit_text(
                text=LANG.FORCE_SUB_STILL_REQUIRED,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=retry_markup,
            )
    except Exception:
        pass
    await query.answer(ForceSubMessages.STILL_REQUIRED_ALERT, show_alert=True)

#---------------------[ LINK SERVICE DELEGATIONS ]---------------------#

from FileStream.services.link_service import link_service

gen_link = link_service.gen_link
gen_linkx = link_service.gen_linkx
gen_file_list_button = link_service.gen_file_list_button

#---------------------[ USER SERVICE DELEGATIONS ]---------------------#

from FileStream.services.user_service import user_service

is_user_banned = user_service.is_user_banned
is_channel_banned = user_service.is_channel_banned
is_user_authorized = user_service.is_user_authorized
is_user_exist = user_service.is_user_exist
is_channel_exist = user_service.is_channel_exist
verify_user = user_service.verify_user
