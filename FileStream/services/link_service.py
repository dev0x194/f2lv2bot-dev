from __future__ import annotations

import os
import math
import html
import time
import datetime
from typing import TYPE_CHECKING
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message

if TYPE_CHECKING:
    from FileStream.database.database import Database


class LinkService:
    def __init__(self, db: Database | None = None):
        self._db = db
        self.file_list_page_size = 10

    @property
    def db(self):
        if self._db is None:
            from FileStream.config import Telegram
            from FileStream.database.database import Database
            self._db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)
        return self._db

    async def get_public_file_context(self, file_ref, bot=None) -> tuple[dict, dict, str]:
        from FileStream.core.runtime.public_links import build_public_file_url
        file_info = file_ref if isinstance(file_ref, dict) else await self.db.get_file(file_ref)
        link_doc = await self.db.ensure_public_link_for_file(file_info)
        return file_info, link_doc, build_public_file_url(link_doc["public_id"], bot=bot, file_info=file_info)

    def format_expiry_countdown(self, total_seconds: float | int) -> str:
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

    def describe_public_link_expiry(self, file_info: dict, link_doc: dict) -> dict[str, str | bool]:
        from FileStream.config import Server
        from FileStream.core.runtime.flog_channels import resolve_file_flog_mode
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
        absolute_text = datetime.datetime.fromtimestamp(expires_at_value, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        countdown_text = self.format_expiry_countdown(remaining)
        main_label = f"MAIN ({ttl_hours}h)" if ttl_hours > 0 else "MAIN"
        if remaining <= 0:
            share_text = "⏳ Link expired"
        else:
            share_text = f"⏳ Available for {self.format_expiry_countdown(remaining).replace(' left', '')}"

        return {
            "storage_label": main_label,
            "countdown_text": countdown_text,
            "absolute_text": absolute_text,
            "share_text": share_text,
            "is_permanent": False,
        }

    def build_forward_share_text(self, file_name: str, bot_username: str | None, expiry_info: dict[str, str | bool]) -> str:
        normalized_bot = str(bot_username or "").strip().lstrip("@")
        lines = [
            f"📄 {file_name or 'file'}",
            str(expiry_info.get("share_text") or "⏳ Limited-time link"),
        ]
        if normalized_bot:
            if " " in normalized_bot:
                lines.append(f"🤖 {normalized_bot}")
            else:
                lines.append(f"🤖 @{normalized_bot}")
        return "\n".join(lines)

    async def gen_link(self, _id, bot=None) -> tuple[InlineKeyboardMarkup, str]:
        from FileStream.core.runtime.human_readable import humanbytes
        from FileStream.core.media.category import detect_category
        from FileStream.bot.helpers.client_identity import get_bot_username
        from FileStream.core.runtime.public_links import build_telegram_share_link
        from FileStream.bot.messages import LANG

        file_info, link_doc, public_url = await self.get_public_file_context(_id, bot=bot)
        file_name = file_info.get('file_name') or "file"
        file_size = humanbytes(file_info.get('file_size') or 0)
        mime_type = (file_info.get('mime_type') or "").lower()
        ext = os.path.splitext(file_name)[1].lower()
        video_ext = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".mpeg", ".mpg"}
        audio_ext = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".wav", ".opus", ".oga"}
        is_streamable = ("video" in mime_type or "audio" in mime_type or ext in video_ext or ext in audio_ext)

        category = file_info.get("category") or detect_category(file_name=file_name, mime_type=mime_type, file_ext=ext)

        safe_name = html.escape(file_name)
        safe_category = html.escape(category)
        safe_public_url = html.escape(public_url)
        bot_username = get_bot_username(bot)
        safe_bot_username = html.escape(bot_username or "File To Link V2 bot")
        expiry_info = self.describe_public_link_expiry(file_info, link_doc)
        share_link = build_telegram_share_link(
            public_url,
            text=self.build_forward_share_text(file_name, bot_username, expiry_info),
        )
        if len(safe_name) > 200:
            safe_name = safe_name[:200] + "…"

        from FileStream.bot.helpers.ui import TelegramCapabilities

        stream_text = LANG.STREAM_TEXT.format(
            safe_name,
            file_size,
            safe_category,
            html.escape(str(expiry_info["countdown_text"])),
            html.escape(str(expiry_info["absolute_text"])),
            safe_bot_username,
            safe_public_url,
            safe_bot_username,
        )

        primary_btn = InlineKeyboardButton("▶️ Stream" if is_streamable else "📄 View", url=public_url)
        share_btn = InlineKeyboardButton("📤 Share", url=share_link)
        remove_btn = InlineKeyboardButton("🗑 Remove", callback_data=f"msgdelpvt_{_id}")

        rows = [[primary_btn, share_btn]]
        if TelegramCapabilities.supports_copy_text_button():
            rows.append([InlineKeyboardButton("📋 Copy Link", copy_text=public_url)])
        rows.append([remove_btn])
        reply_markup = InlineKeyboardMarkup(rows)
        return reply_markup, stream_text


    async def gen_linkx(self, m: Message, _id, bot=None) -> tuple[InlineKeyboardMarkup, str]:
        from FileStream.core.runtime.human_readable import humanbytes
        from FileStream.core.media.category import detect_category
        from FileStream.bot.helpers.client_identity import get_bot_username
        from FileStream.bot.helpers.ui import TelegramCapabilities
        from FileStream.bot.messages import LANG

        file_info, link_doc, public_url = await self.get_public_file_context(_id, bot=bot)
        file_name = file_info.get('file_name') or "file"
        mime_type = (file_info.get('mime_type') or "").lower()
        file_size = humanbytes(file_info.get('file_size') or 0)
        ext = os.path.splitext(file_name)[1].lower()
        video_ext = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".mpeg", ".mpg"}
        audio_ext = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".wav", ".opus", ".oga"}
        is_streamable = ("video" in mime_type or "audio" in mime_type or ext in video_ext or ext in audio_ext)

        category = file_info.get("category") or detect_category(file_name=file_name, mime_type=mime_type, file_ext=ext)

        safe_name = html.escape(file_name)
        safe_category = html.escape(category)
        safe_public_url = html.escape(public_url)
        bot_username = get_bot_username(bot)
        safe_bot_username = html.escape(bot_username or "File To Link V2 bot")
        expiry_info = self.describe_public_link_expiry(file_info, link_doc)
        if len(safe_name) > 200:
            safe_name = safe_name[:200] + "…"

        stream_text = LANG.STREAM_TEXT.format(
            safe_name,
            file_size,
            safe_category,
            html.escape(str(expiry_info["countdown_text"])),
            html.escape(str(expiry_info["absolute_text"])),
            safe_bot_username,
            safe_public_url,
            safe_bot_username,
        )

        primary_btn = InlineKeyboardButton("▶️ Stream" if is_streamable else "📄 View", url=public_url)
        rows = [[primary_btn]]
        if TelegramCapabilities.supports_copy_text_button():
            rows.append([InlineKeyboardButton("📋 Copy Link", copy_text=public_url)])
        reply_markup = InlineKeyboardMarkup(rows)
        return reply_markup, stream_text

    async def gen_file_list_button(self, file_list_no: int, user_id: int, bot=None) -> tuple[list, int]:
        from FileStream.core.runtime.flog_sync import reconcile_flog_storage
        from FileStream.bot.helpers.bot_utils import get_sync_client, get_bot_scope

        if file_list_no < 1:
            file_list_no = 1

        await reconcile_flog_storage(get_sync_client(bot), user_id=int(user_id))
        bot_id, include_legacy = get_bot_scope(bot)
        start_idx = (file_list_no - 1) * self.file_list_page_size + 1
        end_idx = start_idx + self.file_list_page_size - 1
        cursor, total_files = await self.db.find_files(
            int(user_id),
            (start_idx, end_idx),
            bot_id=bot_id,
            include_legacy=include_legacy,
        )

        if total_files > 0:
            max_pages = math.ceil(total_files / self.file_list_page_size)
            if file_list_no > max_pages:
                file_list_no = max_pages
                start_idx = (file_list_no - 1) * self.file_list_page_size + 1
                end_idx = start_idx + self.file_list_page_size - 1
                cursor, total_files = await self.db.find_files(
                    int(user_id),
                    (start_idx, end_idx),
                    bot_id=bot_id,
                    include_legacy=include_legacy,
                )
        else:
            max_pages = 1

        file_list = []
        async for x in cursor:
            name = x.get("file_name") or "file"
            if len(name) > 50:
                name = name[:50] + "…"
            file_list.append([InlineKeyboardButton(name, callback_data=f"myfile_{x['_id']}_{file_list_no}")])

        if not file_list:
            file_list = [
                [InlineKeyboardButton("📭 Empty", callback_data="N/A")],
                [InlineKeyboardButton("📤 Send a File", callback_data="help_send")],
                [InlineKeyboardButton("◀️ Home", callback_data="home")],
            ]
            return file_list, total_files

        if total_files > self.file_list_page_size:
            prev_btn = InlineKeyboardButton("◀️ Prev", callback_data=f"userfiles_{file_list_no-1}") if file_list_no > 1 else InlineKeyboardButton("· ◀️ ·", callback_data="N/A")
            page_btn = InlineKeyboardButton(f"📄 {file_list_no}/{max_pages}", callback_data="N/A")
            next_btn = InlineKeyboardButton("Next ▶️", callback_data=f"userfiles_{file_list_no+1}") if total_files > file_list_no * self.file_list_page_size else InlineKeyboardButton("· ▶️ ·", callback_data="N/A")
            file_list.append([prev_btn, page_btn, next_btn])

        file_list.append([InlineKeyboardButton("◀️ Home", callback_data="home")])
        return file_list, total_files



link_service = LinkService()
