import datetime
import logging
import math
import os
import html
from FileStream import __version__
from FileStream.bot import FileStream
from FileStream.config import Telegram, Server
from FileStream.bot.messages import ErrorMessages, FileMessages, PublicMessages, LANG
from FileStream.bot.helpers.translation import BUTTON
from FileStream.bot.helpers.bot_utils import (
    build_forward_share_text,
    cache_media_file_id,
    describe_public_link_expiry,
    file_visible_to_bot,
    gen_link,
    gen_file_list_button,
    get_cached_media_file_id,
    get_public_file_context,
    get_sync_client,
    handle_force_sub_retry,
    invalidate_cached_media_file_id,
    is_local_media_path,
    resolve_media_source,
)
from FileStream.database.database import Database
from FileStream.core.runtime.human_readable import humanbytes
from FileStream.core.runtime.file_cleanup import delete_file_entry
from FileStream.core.media.file_properties import ensure_flog_media_exists, get_file_ids
from FileStream.bot import multi_clients
from FileStream.server.exceptions import FileNotFound
from FileStream.bot.helpers.client_identity import get_bot_name, get_bot_username
from FileStream.bot.handlers.user.donation import open_donation_menu
from FileStream.bot.helpers.legal import build_bot_policy_text
from FileStream.core.runtime.public_links import build_telegram_share_link
from pyrogram import filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, InputMediaPhoto
from pyrogram.file_id import FileId, FileType, PHOTO_TYPES
from pyrogram.enums.parse_mode import ParseMode

db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)

async def edit_message(
    update: CallbackQuery,
    text: str,
    reply_markup=None,
    parse_mode=ParseMode.MARKDOWN,
    disable_web_page_preview=True,
    photo: str | None = None,
    bot=None,
):
    try:
        if getattr(update.message, "photo", None) or getattr(update.message, "caption", None):
            resolved_photo = resolve_media_source(photo) if photo else None
            target_bot = (
                bot
                or getattr(update, "_client", None)
                or getattr(getattr(update, "message", None), "_client", None)
                or FileStream
            )
            if resolved_photo and hasattr(update.message, "edit_media"):
                media_to_send = resolved_photo
                is_local = is_local_media_path(resolved_photo)
                if is_local and target_bot:
                    cached_id = get_cached_media_file_id(target_bot, resolved_photo) or get_cached_media_file_id(target_bot, photo)
                    if cached_id:
                        curr_file_id = getattr(getattr(update.message, "photo", None), "file_id", None)
                        if curr_file_id and curr_file_id == cached_id:
                            await update.message.edit_caption(caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
                            return
                        media_to_send = cached_id
                try:
                    res = await update.message.edit_media(
                        media=InputMediaPhoto(
                            media=media_to_send,
                            caption=text,
                            parse_mode=parse_mode,
                        ),
                        reply_markup=reply_markup,
                    )
                    if target_bot and res and getattr(res, "photo", None):
                        fid = getattr(res.photo, "file_id", None)
                        if isinstance(fid, str) and fid.strip():
                            cache_media_file_id(target_bot, resolved_photo, fid, raw_source=photo)
                    return
                except MessageNotModified:
                    return
                except Exception as exc:
                    if media_to_send != resolved_photo and target_bot:
                        invalidate_cached_media_file_id(target_bot, resolved_photo, raw_source=photo)
                        try:
                            res = await update.message.edit_media(
                                media=InputMediaPhoto(
                                    media=resolved_photo,
                                    caption=text,
                                    parse_mode=parse_mode,
                                ),
                                reply_markup=reply_markup,
                            )
                            if res and getattr(res, "photo", None):
                                fid = getattr(res.photo, "file_id", None)
                                if isinstance(fid, str) and fid.strip():
                                    cache_media_file_id(target_bot, resolved_photo, fid, raw_source=photo)
                            return
                        except MessageNotModified:
                            return
                        except Exception as retry_exc:
                            logging.warning("Failed to edit media photo on retry: %s", retry_exc)
                    else:
                        logging.warning("Failed to edit media photo: %s", exc)
            await update.message.edit_caption(caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            await update.message.edit_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
    except MessageNotModified:
        pass


def _is_owner(file_info: dict, user_id: int) -> bool:
    try:
        return int(file_info.get("user_id")) == int(user_id)
    except Exception:
        return False

#---------------------[ START CMD ]---------------------#
@FileStream.on_callback_query(
    filters.regex(r"^(home|help|about|legal|privacy|terms|support|donation|forcesub_retry|N/A|close|my_files|my_folders|myfiles|myfolders|msgdelete_|msgdelyes_|msgdelpvt_|msgdelpvtyes_|mainstream_|userfiles_|myfile_|sendfile_)")
)
async def cb_data(bot, update: CallbackQuery):
    try:
        data = update.data or ""
    except Exception:
        return

    # Immediate acknowledgement for navigation callbacks to guarantee 0ms UI lag
    if data in {"home", "help", "about", "legal", "privacy", "terms", "support", "donation", "my_files", "my_folders", "myfiles", "myfolders", "help_send"} or data.startswith("userfiles_"):
        try:
            await update.answer()
        except Exception:
            pass

    if data == "help_send":
        try:
            await update.answer("Send or forward any file to this chat 📤", show_alert=True)
        except Exception:
            pass
        return

    if data in {"my_files", "myfiles"}:
        file_list, total_files = await gen_file_list_button(1, update.from_user.id, bot=bot)
        await edit_message(update, PublicMessages.DASHBOARD_FILES.format(total_files), InlineKeyboardMarkup(file_list), parse_mode=ParseMode.HTML)
        return

    elif data in {"my_folders", "myfolders"}:
        try:
            await update.answer()
        except Exception:
            pass
        from FileStream.bot.handlers.folder.folder_manager import _send_folder_list
        await _send_folder_list(bot, update.message, page=1, edit=True, user_id=update.from_user.id)
        return
    elif data in {"my_stats", "mystats"}:
        try:
            await update.answer()
        except Exception:
            pass
        from FileStream.bot.handlers.user.stats import user_stats_cmd
        await user_stats_cmd(bot, update.message)
        return
    elif data.startswith("tip:dismiss:"):
        feature_key = data.split(":", 2)[2]
        user_id = update.from_user.id if update.from_user else None
        if user_id:
            await db.dismiss_feature_recommendation(user_id, feature_key)
            from FileStream.services.analytics import track_event, AnalyticsEvents
            await track_event(db, AnalyticsEvents.TIP_DISMISSED, user_id=user_id, properties={"feature": feature_key})
        try:
            await update.answer("Tip dismissed ✅", show_alert=False)
            await update.message.delete()
        except Exception:
            pass
        return

    try:
        usr_cmd = data.split("_")
    except Exception:
        return

    if not usr_cmd or not usr_cmd[0]:
        return

    if usr_cmd[0] == "home":
        from FileStream.bot.handlers.user.start import show_home
        await show_home(bot, update)
    elif usr_cmd[0] == "forcesub" and len(usr_cmd) > 1 and usr_cmd[1] == "retry":
        await handle_force_sub_retry(bot, update)
    elif usr_cmd[0] == "help":
        await edit_message(
            update,
            text=LANG.HELP_TEXT.format(Telegram.OWNER_ID),
            reply_markup=BUTTON.help_buttons(bot),
            parse_mode=ParseMode.HTML,
            photo=Telegram.bot_media(bot, "HELP_PIC"),
            bot=bot,
        )
    elif usr_cmd[0] == "about":
        await edit_message(
            update,
            text=LANG.ABOUT_TEXT.format(get_bot_name(bot), __version__),
            reply_markup=BUTTON.about_buttons(bot),
            parse_mode=ParseMode.HTML,
            photo=Telegram.bot_media(bot, "ABOUT_PIC"),
            bot=bot,
        )
    elif usr_cmd[0] == "privacy":
        await edit_message(
            update,
            text=build_bot_policy_text("privacy", bot),
            reply_markup=BUTTON.legal_buttons(bot, command="privacy"),
            parse_mode=ParseMode.HTML,
            photo=Telegram.bot_media(bot, "PRIVACY_PIC"),
            bot=bot,
        )
    elif usr_cmd[0] in ("legal", "terms"):
        await edit_message(
            update,
            text=build_bot_policy_text("legal", bot),
            reply_markup=BUTTON.legal_buttons(bot, command="legal"),
            parse_mode=ParseMode.HTML,
            photo=Telegram.bot_media(bot, "LEGAL_PIC"),
            bot=bot,
        )
    elif usr_cmd[0] in ("support", "donation"):
        await open_donation_menu(update.message, bot, edit=True)


    #---------------------[ MY FILES CMD ]---------------------#

    elif usr_cmd[0] == "N/A":
        await update.answer("This action is currently unavailable.", show_alert=False)

    elif usr_cmd[0] == "close":
        try:
            await update.answer()
        except Exception:
            pass
        try:
            await update.message.delete()
        except Exception:
            pass
    elif usr_cmd[0] == "msgdelete":
        if len(usr_cmd) < 3:
            await update.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
            return
        try:
            await update.answer()
        except Exception:
            pass
        from FileStream.bot.helpers.ui import make_file_delete_confirm_keyboard
        await edit_message(
            update,
            FileMessages.DELETE_CONFIRM,
            make_file_delete_confirm_keyboard(usr_cmd[1], int(usr_cmd[2])),
            parse_mode=ParseMode.HTML,
        )
    elif usr_cmd[0] == "msgdelyes":
        if len(usr_cmd) < 3:
            await update.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
            return
        await delete_user_file(bot, usr_cmd[1], int(usr_cmd[2]), update)
        return
    elif usr_cmd[0] == "msgdelpvt":
        if len(usr_cmd) < 2:
            await update.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
            return
        try:
            await update.answer()
        except Exception:
            pass
        _id = usr_cmd[1]
        try:
            myfile_info = await db.get_file(_id)
            safe_fname = html.escape(myfile_info.get("file_name") or "file")
            confirm_text = (
                "🗑️ <b>Remove file?</b>\n\n"
                f"<b>{safe_fname}</b>\n\n"
                "This will remove the generated link."
            )
        except Exception:
            confirm_text = FileMessages.REVOKE_CONFIRM
        await edit_message(
            update,
            confirm_text,
            InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Remove", callback_data=f"msgdelpvtyes_{_id}"), InlineKeyboardButton("✕ Cancel", callback_data=f"mainstream_{_id}")]]),
            parse_mode=ParseMode.HTML,
        )
    elif usr_cmd[0] == "msgdelpvtyes":
        if len(usr_cmd) < 2:
            await update.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
            return
        await delete_user_filex(bot, usr_cmd[1], update)
        return

    elif usr_cmd[0] == "mainstream":
        if len(usr_cmd) < 2:
            await update.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
            return
        _id = usr_cmd[1]
        try:
            myfile_info = await db.get_file(_id)
        except FileNotFound:
            await update.answer(ErrorMessages.FILE_NOT_FOUND_ALERT, show_alert=True)
            return
        if not file_visible_to_bot(myfile_info, bot):
            await update.answer(ErrorMessages.FILE_NOT_FOUND_ALERT, show_alert=True)
            return
        try:
            await update.answer()
        except Exception:
            pass
        # gen_link from bot_utils handles Ads/Shortener internally
        reply_markup, stream_text = await gen_link(_id=_id, bot=bot)
        await update.message.edit_text(
            text=stream_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )

    elif usr_cmd[0] == "userfiles":
        if len(usr_cmd) < 2:
            await update.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
            return
        try:
            await update.answer()
        except Exception:
            pass
        try:
            page_no = int(usr_cmd[1])
        except Exception:
            page_no = 1
        file_list, total_files = await gen_file_list_button(page_no, update.from_user.id, bot=bot)
        await edit_message(update, PublicMessages.DASHBOARD_FILES.format(total_files), InlineKeyboardMarkup(file_list), parse_mode=ParseMode.HTML)
    elif usr_cmd[0] == "myfile":
        if len(usr_cmd) < 3:
            await update.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
            return
        await gen_file_menu(usr_cmd[1], usr_cmd[2], update, bot)
        return
    elif usr_cmd[0] == "sendfile":
        if len(usr_cmd) < 2:
            await update.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
            return
        try:
            myfile = await db.get_file(usr_cmd[1])
            await ensure_flog_media_exists(myfile, bot=get_sync_client(bot), prune_stale=True, db_instance=db)
            await get_file_ids(bot, str(myfile["_id"]), multi_clients, update.message)
            myfile = await db.get_file(usr_cmd[1])
        except FileNotFound:
            await update.answer(ErrorMessages.FILE_NOT_FOUND_ALERT, show_alert=True)
            return
        if not file_visible_to_bot(myfile, bot):
            await update.answer(ErrorMessages.FILE_NOT_FOUND_ALERT, show_alert=True)
            return
        if not _is_owner(myfile, update.from_user.id):
            await update.answer(ErrorMessages.ACCESS_DENIED, show_alert=True)
            return
        file_name = myfile.get('file_name') or "file"
        await update.answer(FileMessages.SENDING)
        safe_name = html.escape(file_name)
        if len(safe_name) > 1000:
            safe_name = safe_name[:1000] + "…"
        cached_file_id = (myfile.get("file_ids") or {}).get(str(bot.id)) or myfile.get("file_id")
        try:
            sent_media = await update.message.reply_cached_media(
                cached_file_id,
                caption=FileMessages.CAPTION.format(safe_name),
                parse_mode=ParseMode.HTML,
            )
            from FileStream.bot.handlers.user.start import delete_later
            asyncio.create_task(delete_later(sent_media, None))
        except Exception:
            await update.answer(FileMessages.SEND_FAILED, show_alert=True)
    else:
        return


    #---------------------[ MY FILES FUNC ]---------------------#

async def gen_file_menu(_id, file_list_no, update: CallbackQuery, bot):
    try:
        myfile_info=await db.get_file(_id)
        await ensure_flog_media_exists(myfile_info, bot=get_sync_client(bot), prune_stale=True, db_instance=db)
    except FileNotFound:
        await update.answer(ErrorMessages.FILE_NOT_FOUND_ALERT, show_alert=True)
        try:
            page_no = int(file_list_no)
        except Exception:
            page_no = 1
        file_list, total_files = await gen_file_list_button(page_no, update.from_user.id, bot=bot)
        await edit_message(update, PublicMessages.DASHBOARD_FILES.format(total_files), InlineKeyboardMarkup(file_list), parse_mode=ParseMode.HTML)
        return

    if not file_visible_to_bot(myfile_info, bot):
        await update.answer(ErrorMessages.FILE_NOT_FOUND_ALERT, show_alert=True)
        return

    if not _is_owner(myfile_info, update.from_user.id):
        await update.answer(ErrorMessages.ACCESS_DENIED, show_alert=True)
        return

    # Folder files cannot be managed as standalone files
    is_folder = bool(
        myfile_info.get("is_folder_file")
        or myfile_info.get("folder_session_id")
        or myfile_info.get("folder_id")
        or (isinstance(myfile_info.get("folders"), list) and len(myfile_info.get("folders")) > 0)
    )
    if is_folder:
        await update.answer(ErrorMessages.FILE_NOT_FOUND_ALERT, show_alert=True)
        try:
            page_no = int(file_list_no)
        except Exception:
            page_no = 1
        file_list, total_files = await gen_file_list_button(page_no, update.from_user.id, bot=bot)
        await edit_message(update, PublicMessages.DASHBOARD_FILES.format(total_files), InlineKeyboardMarkup(file_list), parse_mode=ParseMode.HTML)
        return

    try:
        file_id = FileId.decode(myfile_info['file_id'])
    except Exception:
        await update.answer(ErrorMessages.FILE_NOT_FOUND_ALERT, show_alert=True)
        return

    try:
        await update.answer()
    except Exception:
        pass

    if file_id.file_type in PHOTO_TYPES:
        file_type = "Image"
    elif file_id.file_type == FileType.VOICE:
        file_type = "Voice"
    elif file_id.file_type in (FileType.VIDEO, FileType.ANIMATION, FileType.VIDEO_NOTE):
        file_type = "Video"
    elif file_id.file_type == FileType.DOCUMENT:
        file_type = "Document"
    elif file_id.file_type == FileType.STICKER:
        file_type = "Sticker"
    elif file_id.file_type == FileType.AUDIO:
        file_type = "Audio"
    else:
        file_type = "Unknown"

    file_name = (myfile_info.get("file_name") or "").lower()
    ext = os.path.splitext(file_name)[1]
    video_ext = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".mpeg", ".mpg"}
    audio_ext = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".wav", ".opus", ".oga"}
    is_streamable = file_type in ("Video", "Audio") or ext in video_ext or ext in audio_ext

    _, link_doc, public_url = await get_public_file_context(myfile_info, bot=bot)
    expiry_info = describe_public_link_expiry(myfile_info, link_doc)
    share_link = build_telegram_share_link(
        public_url,
        text=build_forward_share_text(myfile_info.get("file_name") or "file", get_bot_username(bot), expiry_info),
    )

    from FileStream.bot.helpers.ui import make_file_menu_keyboard
    MYFILES_BUTTONS = make_file_menu_keyboard(
        file_id=str(myfile_info['_id']),
        file_list_no=int(file_list_no),
        public_url=public_url,
        share_link=share_link,
        is_streamable=is_streamable,
    )

    TiMe = myfile_info['time']
    if isinstance(TiMe, (int, float)):
        date = datetime.datetime.fromtimestamp(TiMe)
        readable_time = date.date()
    else:
        readable_time = TiMe

    safe_name = html.escape(myfile_info.get('file_name') or "file")
    safe_type = html.escape(file_type)
    safe_time = html.escape(str(readable_time))
    safe_category = html.escape(myfile_info.get('category') or "Other")

    await edit_message(
        update,
        FileMessages.DETAILS.format(
            safe_name,
            humanbytes(int(myfile_info.get('file_size') or 0)),
            safe_type,
            safe_category,
            safe_time,
        ),
        MYFILES_BUTTONS,
        parse_mode=ParseMode.HTML,
    )


async def delete_user_file(bot, _id, file_list_no: int, update: CallbackQuery):

    try:
        myfile_info = await db.get_file(_id)
    except FileNotFound:
        await update.answer(FileMessages.ALREADY_DELETED, show_alert=True)
        return

    if not file_visible_to_bot(myfile_info, bot):
        await update.answer(ErrorMessages.FILE_NOT_FOUND_ALERT, show_alert=True)
        return

    if not _is_owner(myfile_info, update.from_user.id):
        await update.answer(ErrorMessages.ACCESS_DENIED, show_alert=True)
        return

    await delete_file_entry(db, myfile_info, bot=bot)
    try:
        await update.answer(FileMessages.DELETED, show_alert=False)
    except Exception:
        pass

    file_list, total_files = await gen_file_list_button(file_list_no, update.from_user.id, bot=bot)
    await edit_message(
        update,
        PublicMessages.DASHBOARD_FILES.format(total_files),
        InlineKeyboardMarkup(file_list),
        parse_mode=ParseMode.HTML,
    )

async def delete_user_filex(bot, _id, update: CallbackQuery):

    try:
        myfile_info = await db.get_file(_id)
    except FileNotFound:
        await update.answer(FileMessages.ALREADY_DELETED, show_alert=True)
        return

    if not file_visible_to_bot(myfile_info, bot):
        await update.answer(ErrorMessages.FILE_NOT_FOUND_ALERT, show_alert=True)
        return

    if not _is_owner(myfile_info, update.from_user.id):
        await update.answer(ErrorMessages.ACCESS_DENIED, show_alert=True)
        return

    await delete_file_entry(db, myfile_info, bot=bot)
    try:
        await update.answer(FileMessages.DELETED, show_alert=False)
    except Exception:
        pass
    from FileStream.bot.helpers.ui import format_home_text, make_home_keyboard
    await edit_message(
        update,
        format_home_text(update.from_user),
        make_home_keyboard(bot),
        parse_mode=ParseMode.HTML,
    )
