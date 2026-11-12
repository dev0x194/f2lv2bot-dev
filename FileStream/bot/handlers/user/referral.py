"""Referral and invite infrastructure plugin for FileToLinkV2bot (Phase 4).

Allows users to invite friends, track verified referral counts, and share direct deep-links
with anti-abuse protections and zero spam mechanics.
"""

from __future__ import annotations

import logging
from urllib.parse import quote_plus
from pyrogram import filters, Client
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode

from FileStream.bot import FileStream
from FileStream.config import Telegram, FeatureFlags
from FileStream.database.database import Database
from FileStream.bot.messages import ReferralMessages, ErrorMessages
from FileStream.bot.helpers.bot_utils import verify_user
from FileStream.bot.helpers.client_identity import get_bot_username
from FileStream.services.analytics import track_event, AnalyticsEvents

logger = logging.getLogger(__name__)
db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)


def build_referral_link(bot_username: str, user_id: int) -> str:
    clean_bot = str(bot_username or "").strip().lstrip("@")
    return f"https://t.me/{clean_bot}?start=ref_{user_id}"


def build_referral_markup(invite_link: str) -> InlineKeyboardMarkup:
    share_text = quote_plus("Fast file streaming and direct download links on Telegram!")
    share_url = f"https://t.me/share/url?url={quote_plus(invite_link)}&text={share_text}"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Share Invite Link", url=share_url)],
        [
            InlineKeyboardButton("📊 View My Stats", callback_data="my_stats"),
            InlineKeyboardButton("✖️ Close", callback_data="ref:close"),
        ],
    ])


@FileStream.on_message(filters.command(["referral", "invite", "ref"]) & filters.private)
async def referral_cmd(bot: Client, message: Message):
    """Handle /referral command."""
    if not getattr(FeatureFlags, "REFERRALS", True):
        return

    if not await verify_user(bot, message):
        return

    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return

    bot_username = get_bot_username(bot) or "FileStreamBot"
    invite_link = build_referral_link(bot_username, user_id)
    ref_data = await db.get_user_referrals(user_id)
    count = ref_data.get("count", 0)

    text = ReferralMessages.CARD.format(invite_link, count)
    markup = build_referral_markup(invite_link)

    await message.reply_text(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
        disable_web_page_preview=True,
        quote=True,
    )
    await track_event(db, AnalyticsEvents.REFERRAL_VIEW, user_id=user_id)


@FileStream.on_callback_query(filters.regex(r"^ref:close$"))
async def referral_callback(bot: Client, callback_query: CallbackQuery):
    try:
        await callback_query.answer()
        await callback_query.message.delete()
    except Exception:
        pass
