import datetime
import time
import html
import logging
from collections import Counter

logger = logging.getLogger(__name__)
from pyrogram import filters, Client
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message
from pyrogram.enums.parse_mode import ParseMode

from FileStream.bot import FileStream
from FileStream.database.database import Database
from FileStream.bot.helpers.bot_utils import verify_user, get_public_folder_context, reply_with_optional_photo, format_expiry_countdown, get_sync_client
from FileStream.core.media.file_properties import ensure_flog_media_exists
from FileStream.core.runtime.flog_sync import reconcile_flog_storage
from FileStream.core.runtime.human_readable import humanbytes
from FileStream.core.runtime.public_links import build_telegram_share_link
from FileStream.bot.helpers.client_identity import get_bot_username
from FileStream.core.runtime.flog_channels import resolve_file_flog_mode
from FileStream.config import Telegram, Server
from FileStream.server.exceptions import FileNotFound
from FileStream.bot.messages import ErrorMessages, FolderMessages


db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)

PAGE_SIZE = 5
rename_pending: dict[int, dict] = {}


async def _edit_message(msg: Message, text: str, reply_markup=None, parse_mode=ParseMode.MARKDOWN):
    if getattr(msg, "photo", None) or getattr(msg, "caption", None):
        await msg.edit_caption(caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        await msg.edit_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode, disable_web_page_preview=True)


def _folder_name(folder: dict) -> str:
    title = (folder.get("title") or "").strip()
    title = " ".join(title.split())
    if not title:
        return "Shared Folder"
    return title


def _fmt_title(folder):
    title = _folder_name(folder)
    if len(title) > 30:
        title = title[:30] + "…"
    return title


def _fmt_date(ts):
    if not ts:
        return "N/A"
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        return "N/A"


def _folder_button_label(folder: dict, item_no: int) -> str:
    title = _fmt_title(folder)
    count = len(folder.get("files") or [])
    suffix = "file" if count == 1 else "files"
    label = f"📁 {item_no}. {title} • {count} {suffix}"
    if len(label) > 40:
        label = label[:39] + "…"
    return label


def _folder_category_label(file_docs: list[dict]) -> str:
    categories = [
        str(doc.get("category") or "").strip() or "Other"
        for doc in file_docs
    ]
    if not categories:
        return "Other"
    unique = sorted(set(categories))
    if len(unique) == 1:
        return unique[0]
    primary = Counter(categories).most_common(1)[0][0]
    return f"Mixed ({primary}+)"


def _folder_expiry_label(file_docs: list[dict], folder: dict | None = None) -> tuple[str, str]:
    if folder and "expires_at" in folder and folder.get("expires_at") is None:
        return "Never expires", "Permanent"
    ttl_hours = int(Server.PUBLIC_FILE_EXPIRE_HOURS or 0)
    if ttl_hours <= 0:
        return "Never expires", "Permanent"

    expiry_candidates: list[float] = []
    for doc in file_docs:
        if resolve_file_flog_mode(doc) == "admin":
            continue
        try:
            base_time = float(doc.get("time") or 0)
        except Exception:
            base_time = 0
        if base_time > 0:
            expiry_candidates.append(base_time + ttl_hours * 3600)

    if not expiry_candidates:
        return "Never expires", "Permanent"

    expires_at = min(expiry_candidates)
    countdown_text = format_expiry_countdown(expires_at - time.time())
    absolute_text = datetime.datetime.utcfromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M UTC")
    return countdown_text, absolute_text


def _build_folder_detail_text(folder: dict, file_docs: list[dict], link: str, bot: Client) -> tuple[str, str]:
    title = html.escape(_folder_name(folder))
    total_files = len(file_docs) or len(folder.get("files") or [])
    total_size_bytes = sum(int(doc.get("file_size") or 0) for doc in file_docs)
    total_size_text = html.escape(humanbytes(total_size_bytes))
    category_text = html.escape(_folder_category_label(file_docs))
    availability_text, expiry_text = _folder_expiry_label(file_docs, folder=folder)
    created_text = html.escape(_fmt_date(folder.get("created_at")))
    bot_username = html.escape(get_bot_username(bot) or "File To Link V2 bot")
    safe_link = html.escape(link)

    text = FolderMessages.DETAILS.format(
        title,
        total_files,
        total_size_text,
        html.escape(availability_text),
    )
    unescaped_bot = html.unescape(bot_username)
    bot_ref = f"@{unescaped_bot}" if (" " not in unescaped_bot and not unescaped_bot.startswith("@")) else unescaped_bot
    share_text = "\n".join(
        [
            f"📁 {html.unescape(title)}",
            f"📦 {total_files} files · {html.unescape(total_size_text)}",
            f"🤖 {bot_ref}",
        ]
    )
    return text, build_telegram_share_link(link, text=share_text)


def _parse_page(value, default: int = 1) -> int:
    try:
        page = int(value)
        return page if page > 0 else default
    except Exception:
        return default


async def _resolve_folder_bot_id(folder: dict) -> int | None:
    raw_bot_id = folder.get("bot_id")
    if raw_bot_id not in (None, ""):
        try:
            return int(raw_bot_id)
        except Exception:
            return None

    inferred_bot_id = None
    for file_id in (folder.get("files") or []):
        try:
            file_info = await db.get_file(file_id)
        except Exception:
            continue
        raw_source_bot_id = file_info.get("source_bot_id")
        if raw_source_bot_id in (None, ""):
            continue
        try:
            inferred_bot_id = int(raw_source_bot_id)
            break
        except Exception:
            continue

    if inferred_bot_id is None and (folder.get("files") or []):
        try:
            inferred_bot_id = int(getattr(FileStream, "id", 0) or 0) or None
        except Exception:
            inferred_bot_id = None

    if inferred_bot_id is not None:
        try:
            await db.folders.update_one({"_id": str(folder.get("_id"))}, {"$set": {"bot_id": inferred_bot_id}})
            folder["bot_id"] = inferred_bot_id
        except Exception:
            pass

    return inferred_bot_id


async def _folder_visible_to_bot(folder: dict, bot: Client) -> bool:
    folder_bot_id = await _resolve_folder_bot_id(folder)
    if folder_bot_id is None:
        return int(getattr(bot, "client_index", 0) or 0) == 0
    try:
        return int(folder_bot_id) == int(getattr(bot, "id", 0) or 0)
    except Exception:
        return False


async def _sync_folder_files(folder: dict, bot: Client) -> dict | None:
    folder_id = str(folder.get("_id") or "")
    user_id = int(folder.get("user_id") or 0)
    original_files = [str(fid) for fid in (folder.get("files") or []) if fid]
    active_files = []
    active_docs = []
    changed = len(original_files) != len(folder.get("files") or [])

    for file_id in original_files:
        try:
            file_info = await db.get_file(file_id)
            await ensure_flog_media_exists(file_info, bot=get_sync_client(bot), prune_stale=True, db_instance=db)
        except FileNotFound:
            changed = True
            continue
        active_files.append(str(file_info["_id"]))
        active_docs.append(file_info)

    if not active_files:
        try:
            await db.delete_folder(folder_id, user_id)
        except Exception:
            pass
        return None

    if changed or active_files != original_files:
        await db.folders.update_one(
            {"_id": folder_id, "user_id": user_id},
            {"$set": {"files": active_files}},
        )
        folder["files"] = active_files

    folder["_resolved_files"] = active_docs
    return folder


async def _get_folder_page(user_id: int, bot: Client, page: int) -> tuple[list[dict], int, int, int]:
    await reconcile_flog_storage(get_sync_client(bot), user_id=int(user_id))

    if page < 1:
        page = 1

    all_cursor = db.folders.find(
        {"user_id": int(user_id)},
        {"_id": 1, "title": 1, "created_at": 1, "files": 1, "bot_id": 1},
    ).sort("created_at", -1)
    visible_all = []
    async for folder in all_cursor:
        if await _folder_visible_to_bot(folder, bot):
            visible_all.append(folder)

    total = len(visible_all)
    max_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1) if total > 0 else 1
    if page > max_pages:
        page = max_pages

    start_idx = (page - 1) * PAGE_SIZE
    end_idx = start_idx + PAGE_SIZE
    folders = visible_all[start_idx:end_idx]
    return folders, total, page, max_pages


async def _send_folder_list(bot: Client, message: Message, page: int = 1, edit: bool = False, user_id: int | None = None):
    if user_id is None:
        user_id = message.from_user.id
    if page < 1:
        page = 1

    visible_folders, total, page, max_pages = await _get_folder_page(user_id, bot, page)

    buttons = []
    summary_lines = []
    start_no = (page - 1) * PAGE_SIZE + 1
    for offset, f in enumerate(visible_folders):
        item_no = start_no + offset
        title = _folder_button_label(f, item_no)
        buttons.append([
            InlineKeyboardButton(title, callback_data=f"fld:open:{f['_id']}:{page}")
        ])
        summary_lines.append(
            f"<b>{item_no}.</b> {html.escape(_fmt_title(f))} • <code>{len(f.get('files') or [])}</code> files"
        )

    if total > PAGE_SIZE:
        prev_btn = InlineKeyboardButton("◀️", callback_data=f"fld:list:{page-1}") if page > 1 else InlineKeyboardButton("· ◀️ ·", callback_data="fld:noop:prev")
        page_btn = InlineKeyboardButton(f"{page} / {max_pages}", callback_data=f"fld:noop:page:{page}:{max_pages}")
        next_btn = InlineKeyboardButton("▶️", callback_data=f"fld:list:{page+1}") if page < max_pages else InlineKeyboardButton("· ▶️ ·", callback_data="fld:noop:next")
        buttons.append([prev_btn, page_btn, next_btn])

    if not buttons:
        buttons.append([
            InlineKeyboardButton("➕ Create Folder", callback_data="fld:new"),
            InlineKeyboardButton("◀️ Home", callback_data="home"),
        ])
    else:
        buttons.append([
            InlineKeyboardButton("➕ New Folder", callback_data="fld:new"),
            InlineKeyboardButton("◀️ Home", callback_data="home"),
        ])

    caption = FolderMessages.list_caption(total, bool(summary_lines), summary_lines)

    if edit:
        try:
            await _edit_message(message, caption, InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)
            return
        except Exception:
            pass

    await reply_with_optional_photo(
        message,
        Telegram.bot_media(bot, "FOLDERS_PIC"),
        caption,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
        quote=True,
    )


@FileStream.on_message(filters.command("folders") & filters.private)
async def folders_cmd(bot: Client, message: Message):
    if not await verify_user(bot, message):
        return
    await _send_folder_list(bot, message, page=1, user_id=message.from_user.id)


@FileStream.on_callback_query(filters.regex(r"^fld:"))
async def folder_callbacks(bot: Client, cq: CallbackQuery):
    if not cq.from_user:
        return

    data = cq.data.split(":")
    if len(data) < 2:
        await cq.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
        return

    action = data[1]
    user_id = cq.from_user.id

    bot_user_id = getattr(bot, "id", None) or getattr(getattr(bot, "me", None), "id", None)
    callback_message_chat_id = getattr(getattr(cq.message, "chat", None), "id", None)
    callback_message_from_user_id = getattr(getattr(cq.message, "from_user", None), "id", None)

    logger.info(
        "[FOLDER] FOLDER_CALLBACK_IDENTITY: callback_user_id=%s callback_message_chat_id=%s callback_message_from_user_id=%s bot_user_id=%s",
        user_id,
        callback_message_chat_id,
        callback_message_from_user_id,
        bot_user_id,
    )

    if bot_user_id and user_id == bot_user_id:
        logger.error("[FOLDER] FOLDER_USER_ID_MISMATCH: Callback received from bot identity %s", user_id)
        await cq.answer(ErrorMessages.ACCESS_DENIED, show_alert=True)
        return

    # Simple auth guard for callbacks
    if Telegram.AUTH_USERS and user_id not in Telegram.AUTH_USERS and user_id != Telegram.OWNER_ID:
        await cq.answer(ErrorMessages.ACCESS_DENIED, show_alert=True)
        return
    if await db.is_user_banned(user_id):
        await cq.answer(ErrorMessages.ACCESS_DENIED, show_alert=True)
        return

    if action == "noop":
        sub = data[2] if len(data) > 2 else ""
        if sub == "prev":
            await cq.answer("No previous folders.", show_alert=False)
        elif sub == "next":
            await cq.answer("No more folders.", show_alert=False)
        elif sub == "page" and len(data) > 4:
            await cq.answer(f"Page {data[3]} of {data[4]}", show_alert=False)
        else:
            await cq.answer("This action is unavailable.", show_alert=False)
        return

    if action == "close":
        await cq.answer()
        try:
            await cq.message.delete()
        except Exception:
            pass
        return

    if action == "new":
        await cq.answer()
        from FileStream.bot.handlers.media.batch import start_folderm
        cq.message.from_user = cq.from_user
        if not getattr(cq.message, "chat", None):
            class _ChatFallback:
                id = user_id
            cq.message.chat = _ChatFallback()
        await start_folderm(bot, cq.message, user_id=user_id)
        return

    if action == "list":
        page = _parse_page(data[2], 1) if len(data) > 2 else 1
        await cq.answer()
        await _send_folder_list(bot, cq.message, page=page, edit=True, user_id=user_id)
        return

    if action == "open":
        if len(data) < 4:
            await cq.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
            return
        folder_id = data[2]
        page = _parse_page(data[3], 1)
        try:
            folder = await db.get_folder_for_user(folder_id, user_id)
            if not await _folder_visible_to_bot(folder, bot):
                raise FileNotFound
            folder = await _sync_folder_files(folder, bot)
        except Exception:
            await cq.answer(FolderMessages.NOT_FOUND_ALERT, show_alert=True)
            return
        if not folder:
            await cq.answer(FolderMessages.NOT_FOUND_ALERT, show_alert=True)
            await _send_folder_list(bot, cq.message, page=page, edit=True, user_id=user_id)
            return

        _, _, link = await get_public_folder_context(folder, bot=bot)
        file_docs = folder.get("_resolved_files") or []
        details_text, share_link = _build_folder_detail_text(folder, file_docs, link, bot)

        from FileStream.bot.helpers.ui import make_folder_menu_keyboard
        buttons = make_folder_menu_keyboard(folder_id, page=page, link=link, share_link=share_link)
        try:
            await cq.answer()
        except Exception:
            pass
        await _edit_message(
            cq.message,
            details_text,
            reply_markup=buttons,
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "add":
        await cq.answer("Send or forward files to add them 📤", show_alert=False)
        return

    if action == "del":
        if len(data) < 4:
            await cq.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
            return
        folder_id = data[2]
        page = _parse_page(data[3], 1)
        try:
            folder = await db.get_folder_for_user(folder_id, user_id)
            if not await _folder_visible_to_bot(folder, bot):
                raise FileNotFound
        except Exception:
            await cq.answer(FolderMessages.NOT_FOUND_ALERT, show_alert=True)
            return
        from FileStream.bot.helpers.ui import make_folder_delete_confirm_keyboard
        buttons = make_folder_delete_confirm_keyboard(folder_id, page=page)

        try:
            await cq.answer()
        except Exception:
            pass
        await _edit_message(
            cq.message,
            FolderMessages.DELETE_CONFIRM,
            reply_markup=buttons,
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "delyes":
        if len(data) < 4:
            await cq.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
            return
        folder_id = data[2]
        page = _parse_page(data[3], 1)
        try:
            folder = await db.get_folder_for_user(folder_id, user_id)
            if not await _folder_visible_to_bot(folder, bot):
                raise FileNotFound
            await db.delete_folder(folder_id, user_id)
        except Exception:
            await cq.answer(FolderMessages.NOT_FOUND_ALERT, show_alert=True)
            return
        await cq.answer(FolderMessages.DELETED, show_alert=False)
        await _send_folder_list(bot, cq.message, page=page, edit=True, user_id=user_id)
        return

    if action == "ren":
        if len(data) < 4:
            await cq.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
            return
        folder_id = data[2]
        page = _parse_page(data[3], 1)
        try:
            folder = await db.get_folder_for_user(folder_id, user_id)
            if not await _folder_visible_to_bot(folder, bot):
                raise FileNotFound
        except Exception:
            await cq.answer(FolderMessages.NOT_FOUND_ALERT, show_alert=True)
            return
        rename_pending[user_id] = {
            "folder_id": folder_id,
            "page": page,
            "chat_id": cq.message.chat.id,
            "message_id": cq.message.id,
            "created_at": time.time(),
        }
        await cq.answer()
        await cq.message.reply_text(
            FolderMessages.RENAME_PROMPT,
            quote=True,
            parse_mode=ParseMode.HTML,
        )
        return


@FileStream.on_message(filters.private & filters.text, group=3)
async def rename_folder_text(bot: Client, message: Message):
    if not message.from_user:
        return

    user_id = message.from_user.id
    if user_id not in rename_pending:
        return

    meta = rename_pending.get(user_id) or {}
    if time.time() - meta.get("created_at", time.time()) > 300:
        rename_pending.pop(user_id, None)
        await message.reply_text("⏳ Rename session expired. Please tap Rename again.", quote=True)
        return

    if not await verify_user(bot, message):
        return

    text = (message.text or "").strip()
    if text.lower().startswith("/cancel"):
        rename_pending.pop(user_id, None)
        await message.reply_text(FolderMessages.RENAME_CANCELLED, quote=True)
        return

    if text.lower().startswith("/start"):
        rename_pending.pop(user_id, None)
        return

    if text.startswith("/"):
        # Ignore other commands while waiting for a name
        return

    meta = rename_pending.get(user_id) or {}
    folder_id = meta.get("folder_id")
    page = meta.get("page", 1)
    chat_id = meta.get("chat_id")
    message_id = meta.get("message_id")

    title = text.strip()
    if not title:
        await message.reply_text(FolderMessages.INVALID_NAME, quote=True)
        return
    if len(title) > 50:
        await message.reply_text(FolderMessages.INVALID_NAME, quote=True)
        return

    try:
        folder = await db.get_folder_for_user(folder_id, user_id)
        if not await _folder_visible_to_bot(folder, bot):
            raise FileNotFound
        await db.update_folder_title(folder_id, user_id, title)
    except Exception:
        rename_pending.pop(user_id, None)
        await message.reply_text(FolderMessages.RENAME_CANCELLED, quote=True)
        return

    rename_pending.pop(user_id, None)
    await message.reply_text(FolderMessages.RENAMED, quote=True)

    # Refresh the folder details message if possible
    try:
        folder = await db.get_folder_for_user(folder_id, user_id)
        if not await _folder_visible_to_bot(folder, bot):
            raise FileNotFound
        _, _, link = await get_public_folder_context(folder, bot=bot)
        file_docs = []
        for file_id in folder.get("files", []):
            try:
                file_docs.append(await db.get_file(file_id))
            except Exception:
                continue
        from FileStream.bot.helpers.ui import make_folder_menu_keyboard
        buttons = make_folder_menu_keyboard(folder_id, page=page, link=link, share_link=share_link)
        if chat_id and message_id:
            target = await bot.get_messages(chat_id, message_id)
            await _edit_message(
                target,
                details_text,
                reply_markup=buttons,
                parse_mode=ParseMode.HTML,
            )
    except Exception:
        pass
