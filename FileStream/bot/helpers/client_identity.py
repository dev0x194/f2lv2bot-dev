from pyrogram import Client

from FileStream.bot import FileStream
from FileStream.config import Telegram


def get_bot_username(bot: Client | None = None) -> str:
    client = bot or FileStream
    me = getattr(client, "me", None)
    if me:
        me_username = getattr(me, "username", None)
        if isinstance(me_username, str) and me_username.strip():
            return me_username.lstrip("@").strip()
    username = getattr(client, "username", None)
    if isinstance(username, str) and username.strip():
        return username.lstrip("@").strip()
    fs_me = getattr(FileStream, "me", None)
    if fs_me:
        fs_me_username = getattr(fs_me, "username", None)
        if isinstance(fs_me_username, str) and fs_me_username.strip():
            return fs_me_username.lstrip("@").strip()
    fs_username = getattr(FileStream, "username", None)
    if isinstance(fs_username, str) and fs_username.strip():
        return fs_username.lstrip("@").strip()
    return ""


def get_bot_name(bot: Client | None = None) -> str:
    client = bot or FileStream
    fname = getattr(client, "fname", None)
    if isinstance(fname, str) and fname.strip():
        return fname.strip()
    fs_fname = getattr(FileStream, "fname", None)
    if isinstance(fs_fname, str) and fs_fname.strip():
        return fs_fname.strip()
    me = getattr(client, "me", None)
    if me:
        first_name = getattr(me, "first_name", None)
        if isinstance(first_name, str) and first_name.strip():
            return first_name.strip()
    return "File To Link V2 bot"


def get_bot_id(bot: Client | None = None) -> int | None:
    client = bot or FileStream
    me = getattr(client, "me", None)
    if me:
        me_id = getattr(me, "id", None)
        if me_id not in (None, "") and not hasattr(me_id, "_mock_name"):
            try:
                return int(me_id)
            except Exception:
                pass
    raw_bot_id = getattr(client, "id", None)
    if raw_bot_id not in (None, "") and not hasattr(raw_bot_id, "_mock_name"):
        try:
            return int(raw_bot_id)
        except Exception:
            pass
    fs_me = getattr(FileStream, "me", None)
    if fs_me:
        fs_me_id = getattr(fs_me, "id", None)
        if fs_me_id not in (None, "") and not hasattr(fs_me_id, "_mock_name"):
            try:
                return int(fs_me_id)
            except Exception:
                pass
    return None


def build_start_link(payload: str, bot: Client | None = None) -> str:
    username = get_bot_username(bot)
    return f"https://t.me/{username}?start={payload}" if username else "https://t.me"


def build_add_to_group_link(bot: Client | None = None) -> str:
    username = get_bot_username(bot)
    if username:
        return f"https://t.me/{username}?startgroup=true"

    token = str(getattr(Telegram, "BOT_TOKEN", "") or "")
    bot_id = token.split(":", 1)[0].strip()
    return f"https://t.me/{bot_id}?startgroup=true" if bot_id else "https://t.me"
