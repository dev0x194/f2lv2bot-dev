import asyncio
import secrets
import html
from datetime import datetime

from pyrogram import filters, Client
from pyrogram.types import Message
from pyrogram.enums.parse_mode import ParseMode

from FileStream.bot import FileStream
from FileStream.config import Telegram, NSFW
from FileStream.database.database import Database
from FileStream.core.security.nsfw import scan_message
from FileStream.server.exceptions import FileNotFound
from FileStream.bot.helpers.bot_utils import verify_user
from FileStream.core.runtime.file_cleanup import delete_file_entry
from FileStream.core.runtime.flog_channels import resolve_file_flog_channel_id
from FileStream.bot.messages import ErrorMessages, SafetyMessages


db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)


def _report_id() -> str:
    return secrets.token_urlsafe(6).replace("-", "").replace("_", "")


def _is_object_id(value: str) -> bool:
    return bool(value) and len(value) == 24 and all(c in "0123456789abcdef" for c in value.lower())


async def _delete_file(bot: Client, file_info: dict):
    await delete_file_entry(db, file_info, bot=bot)


async def _delete_folder(bot: Client, folder: dict):
    file_ids = list(folder.get("files", []))
    for fid in file_ids:
        try:
            file_info = await db.get_file(fid)
        except Exception:
            continue
        await _delete_file(bot, file_info)

    try:
        await db.delete_folder_by_id(folder.get("_id"))
    except Exception:
        pass


async def _scan_file_by_id(bot: Client, file_id: str) -> tuple[bool, str]:
    try:
        file_info = await db.get_file(file_id)
    except Exception:
        raise FileNotFound

    flog_channel_id = resolve_file_flog_channel_id(file_info)
    if not flog_channel_id or not file_info.get("flog_msg_id"):
        return False, "no_flog"

    try:
        log_msg = await bot.get_messages(flog_channel_id, int(file_info["flog_msg_id"]))
        blocked, reason = await scan_message(log_msg)
        return blocked, reason
    except Exception:
        return False, "scan_error"


async def _scan_folder_by_id(bot: Client, folder_id: str) -> tuple[bool, str]:
    folder = await db.get_folder(folder_id)
    for fid in folder.get("files", []):
        try:
            blocked, reason = await _scan_file_by_id(bot, fid)
        except Exception:
            continue
        if blocked:
            return True, reason
    return False, "clean"


async def process_report(bot: Client, message: Message, target_type: str, target_id: str):
    if not Telegram.NUDENET_CHANNEL:
        await message.reply_text(SafetyMessages.REPORT_UNAVAILABLE, quote=True)
        return

    report_id = _report_id()
    reporter = message.from_user
    report_by = reporter.id if reporter else 0

    if target_type == "file":
        try:
            try:
                file_info = await db.get_file(target_id)
            except Exception:
                file_info, _ = await db.resolve_public_file(target_id)
        except Exception:
            await message.reply_text(ErrorMessages.FILE_NOT_FOUND, quote=True)
            return
        uploader_id = file_info.get("user_id")
        uploader_ref = f"<code>{uploader_id}</code>"
        target_label = f"File Ref: <code>{target_id}</code>"
        target_name = html.escape(file_info.get("file_name") or "file")
    else:
        try:
            try:
                folder = await db.get_folder(target_id)
            except Exception:
                folder, _ = await db.resolve_public_folder(target_id)
        except Exception:
            await message.reply_text(ErrorMessages.FOLDER_NOT_FOUND, quote=True)
            return
        uploader_id = folder.get("user_id")
        uploader_ref = f"<code>{uploader_id}</code>"
        target_label = f"Folder Ref: <code>{target_id}</code>"
        target_name = html.escape(folder.get("title") or "Folder")

    doc = {
        "_id": report_id,
        "type": target_type,
        "target_id": str(target_id),
        "report_by": report_by,
        "uploader_id": uploader_id,
        "status": "scanning",
        "created_at": datetime.utcnow().timestamp(),
    }
    await db.add_nsfw_report(doc)

    report_text = SafetyMessages.channel_report_text(
        report_id=report_id,
        report_by=report_by,
        uploader_ref=uploader_ref,
        target_label=target_label,
        target_name=target_name,
        status="Reviewing...",
    )

    report_msg = await bot.send_message(
        chat_id=Telegram.NUDENET_CHANNEL,
        text=report_text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

    await db.update_nsfw_report(report_id, {"report_msg_id": report_msg.id})

    await message.reply_text(SafetyMessages.REPORT_RECEIVED, quote=True)

    async def _finalize():
        try:
            if target_type == "file":
                blocked, reason = await _scan_file_by_id(bot, target_id)
            else:
                blocked, reason = await _scan_folder_by_id(bot, target_id)

            if blocked:
                if target_type == "file":
                    await _delete_file(bot, await db.get_file(target_id))
                else:
                    await _delete_folder(bot, await db.get_folder(target_id))

                # warn uploader
                if uploader_id:
                    try:
                        await bot.send_message(
                            chat_id=uploader_id,
                            text=SafetyMessages.CONTENT_REMOVED,
                        )
                    except Exception:
                        pass

                result_text = report_text.replace(
                    "<code>Reviewing...</code>",
                    "<code>Removed (NSFW)</code>",
                )

                await bot.send_message(
                    chat_id=report_by,
                    text=SafetyMessages.REPORT_REMOVED,
                )
            else:
                result_text = report_text.replace(
                    "<code>Reviewing...</code>",
                    "<code>Reviewed (kept)</code>",
                )
                await bot.send_message(
                    chat_id=report_by,
                    text=SafetyMessages.REPORT_CLEAN,
                )

            try:
                await report_msg.edit_text(result_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except Exception:
                pass

            await db.update_nsfw_report(report_id, {"status": "removed" if blocked else "kept", "reason": reason})
        except Exception as exc:
            try:
                await report_msg.edit_text(
                    report_text.replace(
                        "<code>Reviewing...</code>",
                        "<code>Review failed</code>",
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

    asyncio.create_task(_finalize())


@FileStream.on_message(filters.command("rm_adult") & filters.private)
async def rm_adult_cmd(bot: Client, message: Message):
    if not await verify_user(bot, message):
        return

    from FileStream.bot.handlers.admin.admin import ADMIN_IDS
    if message.from_user.id not in ADMIN_IDS:
        await message.reply_text(ErrorMessages.ACCESS_DENIED, quote=True)
        return

    if len(message.command) < 2:
        await message.reply_text(SafetyMessages.RM_ADULT_USAGE, quote=True, parse_mode=ParseMode.HTML)
        return

    target = message.command[1].strip()
    target_type = "file" if _is_object_id(target) else "folder"

    await process_report(bot, message, target_type, target)
