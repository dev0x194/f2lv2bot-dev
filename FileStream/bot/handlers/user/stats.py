import datetime
import time
import logging
from pyrogram import filters, Client
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums.parse_mode import ParseMode

from FileStream.bot import FileStream
from FileStream.database.database import Database
from FileStream.config import Telegram
from FileStream.bot.helpers.bot_utils import verify_user, reply_with_optional_photo
from FileStream.bot.messages import StatsMessages
from FileStream.services.analytics import track_event, AnalyticsEvents

logger = logging.getLogger(__name__)
db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)


def format_time_ago(ts: float | int | None) -> str:
    if not ts:
        return ""
    try:
        diff = max(0, time.time() - float(ts))
        if diff < 60:
            return "🕒 Last activity: <b>just now</b>"
        elif diff < 3600:
            mins = int(diff / 60)
            return f"🕒 Last activity: <b>{mins}m ago</b>"
        elif diff < 86400:
            hours = int(diff / 3600)
            return f"🕒 Last activity: <b>{hours}h ago</b>"
        else:
            days = int(diff / 86400)
            return f"🕒 Last activity: <b>{days}d ago</b>"
    except Exception:
        return ""


def build_stats_content(stats: dict) -> tuple[str, InlineKeyboardMarkup]:
    total_files = stats.get("total_files", 0)
    total_folders = stats.get("total_folders", 0)
    join_date_ts = stats.get("join_date")

    if total_files == 0 and total_folders == 0:
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("❓ How It Works", callback_data="help")]
        ])
        return StatsMessages.EMPTY, markup

    if join_date_ts:
        try:
            join_date_str = datetime.datetime.fromtimestamp(float(join_date_ts)).strftime("%Y-%m-%d")
        except Exception:
            join_date_str = "N/A"
    else:
        join_date_str = "N/A"

    recent_line = format_time_ago(stats.get("latest_file_ts"))
    ref_count = stats.get("referral_count", 0)
    ref_line = f"\n👥 Friends invited: <b>{ref_count}</b>" if ref_count > 0 else ""
    recent_activity = f"\n{recent_line}{ref_line}" if (recent_line or ref_line) else ""

    text = StatsMessages.CARD.format(
        total_files=total_files,
        total_folders=total_folders,
        total_links=total_files,
        join_date=join_date_str,
        recent_activity=recent_activity,
    )

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📄 My Links", callback_data="my_files"),
            InlineKeyboardButton("📁 My Folders", callback_data="my_folders"),
        ],
        [
            InlineKeyboardButton("⚙️ Settings", callback_data="set:main"),
            InlineKeyboardButton("❌ Close", callback_data="close"),
        ],
    ])
    return text, markup


@FileStream.on_message(filters.command(["stats", "mystats"]) & filters.private)
async def user_stats_cmd(bot: Client, message: Message):
    if not await verify_user(bot, message):
        return

    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return

    try:
        stats = await db.get_user_stats(user_id)
        text, markup = build_stats_content(stats)
        await track_event(db, AnalyticsEvents.STATS_VIEW, user_id=user_id)
        await reply_with_optional_photo(
            message,
            Telegram.bot_media(bot, "ABOUT_PIC"),
            text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
            quote=True,
        )
    except Exception as e:
        logger.error("Failed to render /stats for user %s: %s", user_id, e, exc_info=True)
        await message.reply_text(StatsMessages.FAILED, parse_mode=ParseMode.HTML, quote=True)
