"""Notification service for FileToLinkV2bot (Phase 4).

Handles safe, non-manipulative, opt-out friendly notification delivery with strict
frequency caps, failure handling, and zero notification loops.
"""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode

from FileStream.config import Telegram, FeatureFlags
from FileStream.bot.messages import NotificationMessages
from FileStream.services.analytics import track_event, AnalyticsEvents

if TYPE_CHECKING:
    from pyrogram import Client
    from FileStream.database.database import Database

logger = logging.getLogger(__name__)


def build_notification_markup(existing_markup: InlineKeyboardMarkup | None = None) -> InlineKeyboardMarkup:
    """Ensure an immediate, clear opt-out action is present on any proactive notification."""
    opt_out_button = InlineKeyboardButton("🔕 Turn Off Notifications", callback_data="set:notif_off")
    if not existing_markup:
        return InlineKeyboardMarkup([[opt_out_button]])

    rows = list(existing_markup.inline_keyboard)
    # Check if opt-out button is already present
    has_opt_out = any(
        any("notif_off" in getattr(btn, "callback_data", "") for btn in row)
        for row in rows
    )
    if not has_opt_out:
        rows.append([opt_out_button])
    return InlineKeyboardMarkup(rows)


async def send_safe_notification(
    bot: Client,
    db: Database,
    user_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    notif_type: str = "general",
    cooldown_sec: int = 172800,  # 48 hour default cooldown
) -> bool:
    """Deliver a proactive notification with safety checks, opt-out controls, and strict rate limits."""
    if not getattr(FeatureFlags, "NOTIFICATIONS", True):
        logger.debug("Notifications feature flag is disabled.")
        return False

    try:
        can_send = await db.can_send_notification(user_id, notif_type=notif_type, cooldown_sec=cooldown_sec)
        if not can_send:
            logger.debug("Notification suppressed for user %s (cooldown or opt-out).", user_id)
            return False

        markup = build_notification_markup(reply_markup)
        await bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )

        await db.record_notification_sent(user_id, notif_type)
        await track_event(
            db,
            AnalyticsEvents.NOTIFICATION_SENT,
            user_id=user_id,
            properties={"notif_type": notif_type},
        )
        return True

    except Exception as e:
        logger.info("Notification delivery skipped or failed for user %s: %s", user_id, e)
        return False


async def send_expiration_reminder(
    bot: Client,
    db: Database,
    user_id: int,
    file_name: str,
    countdown_text: str,
) -> bool:
    """Send an accurate expiration warning for temporary files nearing deletion."""
    clean_name = html.escape(file_name)
    if len(clean_name) > 60:
        clean_name = clean_name[:57] + "…"

    text = NotificationMessages.EXPIRATION_WARNING.format(clean_name, countdown_text)
    return await send_safe_notification(
        bot=bot,
        db=db,
        user_id=user_id,
        text=text,
        notif_type="expiration",
        cooldown_sec=86400,  # Max 1 expiration warning per 24 hours
    )


async def send_new_user_reminder(
    bot: Client,
    db: Database,
    user_id: int,
) -> bool:
    """Send a one-time non-invasive reminder to users who started but never uploaded."""
    lifecycle = await db.get_user_lifecycle_state(user_id)
    if lifecycle != "New":
        return False

    text = NotificationMessages.LIFECYCLE_NEW_USER
    return await send_safe_notification(
        bot=bot,
        db=db,
        user_id=user_id,
        text=text,
        notif_type="new_user_onboard",
        cooldown_sec=604800,  # 7-day cooldown (effectively 1-time)
    )
