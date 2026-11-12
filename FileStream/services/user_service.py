from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums.parse_mode import ParseMode
from pyrogram.errors import FloodWait

if TYPE_CHECKING:
    from FileStream.database.database import Database


class UserService:
    def __init__(self, db: Database | None = None):
        self._db = db

    @property
    def db(self):
        if self._db is None:
            from FileStream.config import Telegram
            from FileStream.database.database import Database
            self._db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)
        return self._db

    async def is_banned(self, user_id: int) -> bool:
        return await self.db.is_user_banned(int(user_id))

    def is_authorized(self, user_id: int) -> bool:
        from FileStream.config import Telegram
        if hasattr(Telegram, 'AUTH_USERS') and Telegram.AUTH_USERS:
            if user_id == Telegram.OWNER_ID:
                return True
            return user_id in Telegram.AUTH_USERS
        return True

    async def ensure_user(self, user_id: int) -> bool:
        return await self.db.add_user(int(user_id))

    async def is_user_banned(self, message: Message) -> bool:
        from FileStream.config import Telegram
        from FileStream.bot.messages import LANG
        if not getattr(message, "from_user", None):
            return True
        if await self.is_banned(message.from_user.id):
            await message.reply_text(
                text=LANG.BAN_TEXT.format(Telegram.OWNER_ID),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            return True
        return False

    async def is_channel_banned(self, bot, message: Message) -> bool:
        if await self.is_banned(message.chat.id):
            try:
                await bot.edit_message_reply_markup(
                    chat_id=message.chat.id,
                    message_id=message.id,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🚫 Restricted", callback_data="N/A")]])
                )
            except Exception:
                pass
            return True
        return False

    async def is_user_authorized(self, message: Message) -> bool:
        from FileStream.config import Telegram
        from FileStream.bot.messages import ErrorMessages
        if not getattr(message, "from_user", None):
            return False
        if not self.is_authorized(message.from_user.id):
            owner_id = getattr(Telegram, "OWNER_ID", 0)
            await message.reply_text(
                text=ErrorMessages.PRIVATE_BOT.format(owner_id),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            return False
        return True

    async def is_user_exist(self, bot, message: Message) -> None:
        from FileStream.config import Telegram
        from FileStream.bot.messages import ChannelLogMessages
        from FileStream.bot.helpers.bot_utils import is_internal_service_chat, is_secondary_bot_id, get_known_bot_ids

        user = getattr(message, "from_user", None)
        if not user:
            return

        if getattr(user, "is_bot", None) is True:
            return

        user_id = getattr(user, "id", None)
        if user_id is None:
            return

        try:
            numeric_user_id = int(user_id)
        except Exception:
            return

        if numeric_user_id in get_known_bot_ids() or is_secondary_bot_id(numeric_user_id):
            return

        if is_internal_service_chat(numeric_user_id):
            return

        if await self.ensure_user(numeric_user_id):
            if Telegram.ULOG_CHANNEL:
                try:
                    await bot.send_message(
                        Telegram.ULOG_CHANNEL,
                        ChannelLogMessages.new_user_log(user.first_name, numeric_user_id)
                    )
                except Exception:
                    pass

    async def is_channel_exist(self, bot, message: Message) -> None:
        from FileStream.config import Telegram
        from FileStream.bot.messages import ChannelLogMessages
        from FileStream.bot.helpers.bot_utils import is_internal_service_chat, is_configured_channel

        chat = getattr(message, "chat", None)
        if not chat:
            return

        chat_id = getattr(chat, "id", None)
        if chat_id is None:
            return

        if is_internal_service_chat(chat_id) or is_configured_channel(chat_id):
            return

        try:
            numeric_chat_id = int(chat_id)
        except Exception:
            return

        if await self.ensure_user(numeric_chat_id):
            if Telegram.ULOG_CHANNEL:
                members = "N/A"
                try:
                    members = await bot.get_chat_members_count(numeric_chat_id)
                except FloodWait as e:
                    await asyncio.sleep(e.value)
                except Exception:
                    pass
                try:
                    await bot.send_message(
                        Telegram.ULOG_CHANNEL,
                        ChannelLogMessages.new_channel_log(chat.title, numeric_chat_id, members)
                    )
                except Exception:
                    pass

    async def verify_user(self, bot, message: Message) -> bool:
        from FileStream.config import Telegram
        from FileStream.bot.helpers.bot_utils import (
            _is_force_sub_exempt_request,
            get_force_sub_trial_usage,
            is_user_joined,
        )
        if not getattr(message, "from_user", None):
            return False

        if not await self.is_user_authorized(message):
            return False

        if await self.is_user_banned(message):
            return False

        await self.is_user_exist(bot, message)

        user_id = message.from_user.id
        is_auth = (user_id == Telegram.OWNER_ID) or (
            hasattr(Telegram, 'AUTH_USERS') and Telegram.AUTH_USERS and user_id in Telegram.AUTH_USERS
        )

        if Telegram.FORCE_SUB and not is_auth:
            if _is_force_sub_exempt_request(message):
                return True
            used_tasks, free_tasks = await get_force_sub_trial_usage(bot, user_id)
            if free_tasks > 0 and used_tasks < free_tasks:
                return True
            if not await is_user_joined(bot, message):
                return False

        return True


user_service = UserService()
