import datetime
import html
import secrets

from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified
from pyrogram.enums.parse_mode import ParseMode
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from FileStream.bot import FileStream
from FileStream.config import Telegram
from FileStream.bot.helpers.bot_utils import reply_with_optional_photo, verify_user
from FileStream.bot.helpers.client_identity import get_bot_name, get_bot_username
from FileStream.database.database import Database
from FileStream.bot.messages import DonationMessages, ErrorMessages


db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)

MIN_DONATION_STARS = 50
MAX_DONATION_STARS = 10000
DONATION_PRESETS = (50, 100, 250)
pending_custom_amounts: dict[int, dict] = {}


def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"{amount} ⭐", callback_data=f"donate:amount:{amount}") for amount in DONATION_PRESETS],
            [InlineKeyboardButton("✏️ Custom", callback_data="donate:custom")],
            [
                InlineKeyboardButton("🧾 History", callback_data="donate:history"),
                InlineKeyboardButton("◀️ Back", callback_data="home"),
            ],
        ]
    )


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="donate:menu")]])



def _donation_brand(bot: Client) -> str:
    username = get_bot_username(bot)
    if username:
        return f"@{username}"
    return get_bot_name(bot) or "File To Link V2 bot"


def _donation_invoice_title(bot: Client) -> str:
    return DonationMessages.invoice_title(_donation_brand(bot))


def _donation_invoice_description(bot: Client) -> str:
    return DonationMessages.invoice_description(_donation_brand(bot))


def _donation_text(bot: Client, note: str | None = None) -> str:
    bot_name = html.escape(_donation_brand(bot))
    lines = [DonationMessages.MENU]
    if note:
        lines.extend(["", html.escape(note)])
    return "\n".join(lines)


def _custom_amount_text() -> str:
    return DonationMessages.CUSTOM_AMOUNT.format(MIN_DONATION_STARS, MAX_DONATION_STARS)


async def _history_text(user_id: int) -> str:
    stats = await db.get_user_donation_stats(user_id)
    donations = await db.get_user_donations(user_id, limit=10)
    return DonationMessages.history_text(stats, donations)


async def _send_invoice(
    bot: Client,
    *,
    chat_id: int,
    user_id: int,
    amount: int,
    reply_to_message_id: int | None = None,
):
    payload = f"donation:{user_id}:{amount}:{secrets.token_hex(4)}"
    return await bot.send_invoice(
        chat_id=chat_id,
        title=_donation_invoice_title(bot),
        description=_donation_invoice_description(bot),
        currency="XTR",
        prices=[LabeledPrice(label=f"{amount} Telegram Stars", amount=int(amount))],
        provider="",
        payload=payload,
        reply_to_message_id=reply_to_message_id,
    )


def _parse_payload(payload: str) -> tuple[int, int] | None:
    raw = str(payload or "").strip()
    parts = raw.split(":")
    if len(parts) != 4 or parts[0] != "donation":
        return None
    try:
        user_id = int(parts[1])
        amount = int(parts[2])
    except ValueError:
        return None
    return user_id, amount


def _is_custom_amount_valid(amount: int) -> bool:
    return MIN_DONATION_STARS <= int(amount) <= MAX_DONATION_STARS


async def _show_menu(message: Message, bot: Client, note: str | None = None):
    await reply_with_optional_photo(
        message,
        Telegram.bot_media(bot, "START_PIC"),
        _donation_text(bot, note=note),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=_main_keyboard(),
        quote=True,
    )


async def _edit_message_text(message: Message, text: str, reply_markup: InlineKeyboardMarkup):
    try:
        if getattr(message, "photo", None) or getattr(message, "caption", None):
            await message.edit_caption(
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        else:
            await message.edit_text(
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
    except MessageNotModified:
        pass



async def open_donation_menu(message: Message, bot: Client, note: str | None = None, *, edit: bool = False):
    if edit:
        await _edit_message_text(message, _donation_text(bot, note=note), _main_keyboard())
        return
    await _show_menu(message, bot, note=note)


@FileStream.on_message(filters.command("donation") & filters.private)
async def donation_command(bot: Client, message: Message):
    if not await verify_user(bot, message):
        return
    pending_custom_amounts.pop(message.from_user.id, None)
    await open_donation_menu(message, bot)


@FileStream.on_callback_query(filters.regex(r"^donate:"))
async def donation_callbacks(bot: Client, query: CallbackQuery):
    if not query.from_user or not query.message:
        return

    user_id = int(query.from_user.id)
    if await db.is_user_banned(user_id):
        await query.answer(ErrorMessages.ACCESS_DENIED, show_alert=True)
        return

    data = str(query.data or "")
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "menu"

    if action == "menu":
        pending_custom_amounts.pop(user_id, None)
        try:
            await query.answer()
        except Exception:
            pass
        await open_donation_menu(query.message, bot, edit=True)
        return

    if action == "custom":
        pending_custom_amounts[user_id] = {
            "chat_id": query.message.chat.id,
            "message_id": query.message.id,
        }
        try:
            await query.answer()
        except Exception:
            pass
        await _edit_message_text(query.message, _custom_amount_text(), _back_keyboard())
        return

    if action == "history":
        pending_custom_amounts.pop(user_id, None)
        try:
            await query.answer()
        except Exception:
            pass
        await _edit_message_text(query.message, await _history_text(user_id), _back_keyboard())
        return

    if action == "amount":
        pending_custom_amounts.pop(user_id, None)
        if len(parts) < 3:
            await query.answer(DonationMessages.invalid_amount_alert(MIN_DONATION_STARS, MAX_DONATION_STARS), show_alert=True)
            return
        try:
            amount = int(parts[2])
        except ValueError:
            await query.answer(DonationMessages.invalid_amount_alert(MIN_DONATION_STARS, MAX_DONATION_STARS), show_alert=True)
            return

        if not _is_custom_amount_valid(amount):
            await query.answer(DonationMessages.invalid_amount_alert(MIN_DONATION_STARS, MAX_DONATION_STARS), show_alert=True)
            return

        try:
            await _send_invoice(
                bot,
                chat_id=query.message.chat.id,
                user_id=user_id,
                amount=amount,
                reply_to_message_id=query.message.id,
            )
        except Exception:
            await query.answer(DonationMessages.INVOICE_FAILED, show_alert=True)
            return

        await query.answer(DonationMessages.INVOICE_CREATED_ALERT)
        await _edit_message_text(
            query.message,
            _donation_text(bot, note=DonationMessages.invoice_created_note(amount)),
            _main_keyboard(),
        )
        return

    await query.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)


@FileStream.on_message(filters.private & filters.text, group=4)
async def donation_custom_amount_input(bot: Client, message: Message):
    if not getattr(message, "from_user", None):
        return

    state = pending_custom_amounts.get(message.from_user.id)
    if not state:
        return

    if not await verify_user(bot, message):
        return

    text = (message.text or "").strip()
    if not text:
        return

    if text.startswith("/"):
        pending_custom_amounts.pop(message.from_user.id, None)
        return

    try:
        amount = int(text)
    except ValueError:
        await message.reply_text(
            DonationMessages.INVALID_AMOUNT.format(MIN_DONATION_STARS, MAX_DONATION_STARS),
            quote=True,
            reply_markup=_back_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return

    if not _is_custom_amount_valid(amount):
        await message.reply_text(
            DonationMessages.INVALID_AMOUNT.format(MIN_DONATION_STARS, MAX_DONATION_STARS),
            quote=True,
            reply_markup=_back_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return

    pending_custom_amounts.pop(message.from_user.id, None)

    try:
        await _send_invoice(
            bot,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            amount=amount,
            reply_to_message_id=message.id,
        )
    except Exception:
        await message.reply_text(DonationMessages.INVOICE_FAILED, quote=True)
        return

    try:
        await bot.edit_message_text(
            chat_id=state["chat_id"],
            message_id=state["message_id"],
            text=_donation_text(bot, note=DonationMessages.invoice_created_note(amount)),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=_main_keyboard(),
        )
    except Exception:
        pass

    await message.reply_text(DonationMessages.invoice_created_text(amount), quote=True, parse_mode=ParseMode.HTML)


@FileStream.on_pre_checkout_query()
async def donation_pre_checkout(bot: Client, query: PreCheckoutQuery):
    payload_data = _parse_payload(query.payload)
    if payload_data is None:
        await query.answer(success=False, error=DonationMessages.PRECHECKOUT_INVALID)
        return

    expected_user_id, expected_amount = payload_data
    if int(query.from_user.id) != expected_user_id:
        await query.answer(success=False, error=DonationMessages.PRECHECKOUT_USER_MISMATCH)
        return

    if query.currency != "XTR":
        await query.answer(success=False, error=DonationMessages.PRECHECKOUT_CURRENCY_UNSUPPORTED)
        return

    if int(query.total_amount) != expected_amount or not _is_custom_amount_valid(expected_amount):
        await query.answer(success=False, error=DonationMessages.PRECHECKOUT_AMOUNT_INVALID)
        return

    await query.answer(success=True)


@FileStream.on_message(filters.private & filters.successful_payment)
async def donation_success(bot: Client, message: Message):
    if not getattr(message, "successful_payment", None) or not getattr(message, "from_user", None):
        return

    payment = message.successful_payment
    payload_data = _parse_payload(payment.payload)
    amount = int(payment.total_amount or 0)
    user_id = int(message.from_user.id)

    if payload_data is not None:
        payload_user_id, payload_amount = payload_data
        if payload_user_id == user_id:
            amount = int(payload_amount)

    await db.record_donation(
        user_id=user_id,
        amount=amount,
        currency=payment.currency,
        payload=payment.payload,
        telegram_payment_charge_id=payment.telegram_payment_charge_id,
        provider_payment_charge_id=payment.provider_payment_charge_id,
        first_name=message.from_user.first_name or "",
        username=message.from_user.username or "",
    )

    stats = await db.get_user_donation_stats(user_id)
    await message.reply_text(
        (
            DonationMessages.THANK_YOU.format(
                amount,
                int(stats.get('total_stars', 0)),
            )
        ),
        parse_mode=ParseMode.HTML,
        quote=True,
        disable_web_page_preview=True,
        reply_markup=_main_keyboard(),
    )
