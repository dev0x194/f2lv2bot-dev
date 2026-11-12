from __future__ import annotations

import logging

from pyrogram.errors import ChannelInvalid, ChannelPrivate, ChatIdInvalid, PeerIdInvalid


_DISABLED_OPTIONAL_CHANNELS: set[tuple[str, int]] = set()
_WARMED_OPTIONAL_CHANNELS: set[tuple[int, str, int]] = set()

_INVALID_CHANNEL_MARKERS = (
    "channel_invalid",
    "channel invalid",
    "channel_private",
    "channel private",
    "channel parameter is invalid",
    "chat_id_invalid",
    "chat id invalid",
    "chat not found",
    "peer id invalid",
)


def is_invalid_optional_channel_error(exc: Exception) -> bool:
    if isinstance(exc, (ChannelInvalid, ChannelPrivate, ChatIdInvalid, PeerIdInvalid)):
        return True

    error_text = str(exc or "").lower()
    return any(marker in error_text for marker in _INVALID_CHANNEL_MARKERS)


def optional_channel_available(name: str, channel_id: int | None) -> bool:
    if not channel_id:
        return False
    try:
        key = (str(name), int(channel_id))
    except Exception:
        return False
    return key not in _DISABLED_OPTIONAL_CHANNELS


def disable_optional_channel(name: str, channel_id: int | None, exc: Exception) -> None:
    if not channel_id:
        return

    try:
        key = (str(name), int(channel_id))
    except Exception:
        return

    if key in _DISABLED_OPTIONAL_CHANNELS:
        return

    _DISABLED_OPTIONAL_CHANNELS.add(key)
    logging.warning(
        "Optional channel %s=%s disabled for this runtime: %s",
        name,
        channel_id,
        exc,
    )


def _warm_key(bot, name: str, channel_id: int | None) -> tuple[int, str, int] | None:
    if not channel_id:
        return None
    try:
        return (id(bot), str(name), int(channel_id))
    except Exception:
        return None


async def warm_optional_channel_peer(bot, name: str, channel_id: int | None) -> bool:
    if not optional_channel_available(name, channel_id):
        return False

    warm_key = _warm_key(bot, name, channel_id)
    if warm_key and warm_key in _WARMED_OPTIONAL_CHANNELS:
        return True

    try:
        await bot.get_chat(channel_id)
        if warm_key:
            _WARMED_OPTIONAL_CHANNELS.add(warm_key)
        return True
    except Exception as exc:
        if not is_invalid_optional_channel_error(exc):
            logging.debug("Optional channel %s=%s warm-up failed", name, channel_id, exc_info=True)
            return False

    try:
        async for dialog in bot.get_dialogs(limit=0):
            chat = getattr(dialog, "chat", None)
            if chat and int(getattr(chat, "id", 0)) == int(channel_id):
                try:
                    await bot.get_chat(channel_id)
                    if warm_key:
                        _WARMED_OPTIONAL_CHANNELS.add(warm_key)
                    logging.info("Primed optional channel %s=%s from dialogs", name, channel_id)
                    return True
                except Exception:
                    logging.debug(
                        "Optional channel %s=%s still unresolved after dialog scan",
                        name,
                        channel_id,
                        exc_info=True,
                    )
                    return False
    except Exception:
        logging.debug("Optional channel %s=%s dialog scan failed", name, channel_id, exc_info=True)

    return False


async def safe_send_optional_message(bot, name: str, channel_id: int | None, **kwargs):
    if not optional_channel_available(name, channel_id):
        return None

    async def _send():
        return await bot.send_message(chat_id=channel_id, **kwargs)

    try:
        return await _send()
    except Exception as exc:
        if is_invalid_optional_channel_error(exc):
            if await warm_optional_channel_peer(bot, name, channel_id):
                try:
                    return await _send()
                except Exception as retry_exc:
                    if is_invalid_optional_channel_error(retry_exc):
                        disable_optional_channel(name, channel_id, retry_exc)
                        return None
                    raise
            disable_optional_channel(name, channel_id, exc)
            return None
        raise
