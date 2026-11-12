"""Settings and user preferences plugin for FileToLinkV2bot (Phase 4).

Provides user-facing controls for notifications, personalization, privacy information,
and preference resets in strict accordance with ethical product guidelines.
"""

from __future__ import annotations

import logging
from pyrogram import filters, Client
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode

from FileStream.bot import FileStream
from FileStream.config import Telegram, FeatureFlags
from FileStream.database.database import Database
from FileStream.bot.messages import SettingsMessages, ErrorMessages
from FileStream.bot.helpers.bot_utils import verify_user
from FileStream.services.analytics import track_event, AnalyticsEvents

logger = logging.getLogger(__name__)
db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)


def build_settings_markup(prefs: dict) -> InlineKeyboardMarkup:
    """Construct inline keyboard for /settings based on current user preferences."""
    notif_on = prefs.get("notifications_enabled", True)
    pers_on = prefs.get("personalization_enabled", True)

    notif_btn_text = "🔔 Notifications: ON" if notif_on else "🔕 Notifications: OFF"
    pers_btn_text = "🧠 Personalization: ON" if pers_on else "⏸️ Personalization: OFF"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(notif_btn_text, callback_data="set:toggle_notif")],
        [InlineKeyboardButton(pers_btn_text, callback_data="set:toggle_pers")],
        [
            InlineKeyboardButton("🌐 Language", callback_data="set:lang"),
            InlineKeyboardButton("🔒 Privacy & Data", callback_data="set:privacy"),
        ],
        [InlineKeyboardButton("🔄 Reset Defaults", callback_data="set:reset")],
        [InlineKeyboardButton("✖️ Close", callback_data="set:close")],
    ])


def build_subpage_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Back to Settings", callback_data="set:main")],
        [InlineKeyboardButton("✖️ Close", callback_data="set:close")],
    ])


async def render_settings_text(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    prefs = await db.get_user_preferences(user_id)
    notif_status = "Enabled ✅" if prefs.get("notifications_enabled", True) else "Disabled 🔕"
    pers_status = "Enabled ✅" if prefs.get("personalization_enabled", True) else "Disabled ⏸️"
    lang = str(prefs.get("language") or "en").upper()

    text = SettingsMessages.MAIN.format(notif_status, pers_status, lang)
    markup = build_settings_markup(prefs)
    return text, markup


@FileStream.on_message(filters.command(["settings", "preferences"]) & filters.private)
async def settings_cmd(bot: Client, message: Message):
    """Handle /settings command."""
    if not getattr(FeatureFlags, "SETTINGS", True):
        return

    if not await verify_user(bot, message):
        return

    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return

    text, markup = await render_settings_text(user_id)
    await message.reply_text(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
        quote=True,
    )
    await track_event(db, AnalyticsEvents.SETTINGS_VIEW, user_id=user_id)


async def _safe_edit_settings_message(callback_query: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup):
    try:
        msg = callback_query.message
        if getattr(msg, "photo", None) or getattr(msg, "caption", None):
            await msg.edit_caption(caption=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        else:
            await callback_query.edit_message_text(text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception:
        pass


@FileStream.on_callback_query(filters.regex(r"^set:(toggle_notif|toggle_pers|notif_off|lang|privacy|reset|main|close)$"))
async def settings_callback(bot: Client, callback_query: CallbackQuery):
    """Handle inline button actions for /settings."""
    action = callback_query.data.split(":")[1]
    user_id = callback_query.from_user.id if callback_query.from_user else None
    if not user_id:
        await callback_query.answer(ErrorMessages.GENERIC, show_alert=True)
        return

    try:
        await callback_query.answer()
    except Exception:
        pass

    if action == "close":
        try:
            await callback_query.message.delete()
        except Exception:
            pass
        return

    if action == "notif_off":
        await db.update_user_preferences(user_id, {"notifications_enabled": False})
        await track_event(db, AnalyticsEvents.NOTIFICATION_OPT_OUT, user_id=user_id)
        try:
            await callback_query.answer("🔕 Notifications turned off.", show_alert=True)
            text, markup = await render_settings_text(user_id)
            await _safe_edit_settings_message(callback_query, text=text, reply_markup=markup)
        except Exception:
            pass
        return

    if action == "toggle_notif":
        prefs = await db.get_user_preferences(user_id)
        new_val = not prefs.get("notifications_enabled", True)
        await db.update_user_preferences(user_id, {"notifications_enabled": new_val})
        await track_event(db, AnalyticsEvents.SETTINGS_CHANGE, user_id=user_id, properties={"setting": "notifications", "value": new_val})
        text, markup = await render_settings_text(user_id)
        await _safe_edit_settings_message(callback_query, text=text, reply_markup=markup)
        return

    if action == "toggle_pers":
        prefs = await db.get_user_preferences(user_id)
        new_val = not prefs.get("personalization_enabled", True)
        await db.update_user_preferences(user_id, {"personalization_enabled": new_val})
        await track_event(db, AnalyticsEvents.SETTINGS_CHANGE, user_id=user_id, properties={"setting": "personalization", "value": new_val})
        text, markup = await render_settings_text(user_id)
        await _safe_edit_settings_message(callback_query, text=text, reply_markup=markup)
        return

    if action == "lang":
        await _safe_edit_settings_message(
            callback_query,
            text=SettingsMessages.LANG_MENU,
            reply_markup=build_subpage_markup(),
        )
        return

    if action == "privacy":
        await _safe_edit_settings_message(
            callback_query,
            text=SettingsMessages.PRIVACY_INFO,
            reply_markup=build_subpage_markup(),
        )
        return

    if action == "reset":
        await db.reset_user_preferences(user_id)
        await track_event(db, AnalyticsEvents.SETTINGS_CHANGE, user_id=user_id, properties={"action": "reset_defaults"})
        try:
            await callback_query.answer("✅ Preferences reset to default.", show_alert=True)
            text, markup = await render_settings_text(user_id)
            await _safe_edit_settings_message(callback_query, text=text, reply_markup=markup)
        except Exception:
            pass
        return

    if action == "main":
        text, markup = await render_settings_text(user_id)
        await _safe_edit_settings_message(callback_query, text=text, reply_markup=markup)
        return
