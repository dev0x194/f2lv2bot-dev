import os
from ..config import Telegram
from pyrogram import Client

BASE_DIR = os.path.dirname(os.path.dirname(__file__))  # FileStream/
PROJECT_ROOT = os.path.dirname(BASE_DIR)               # project root
SESSION_DIR = os.getenv("SESSION_DIR", PROJECT_ROOT)
if SESSION_DIR and not os.path.exists(SESSION_DIR):
    try:
        os.makedirs(SESSION_DIR, exist_ok=True)
    except Exception:
        pass

if Telegram.SECONDARY:
    plugins = None
    no_updates = True
else:    
    # Use Python package dot notation for pyrogram plugin root
    plugins = {"root": "FileStream.bot.handlers"}
    no_updates = None

session_name = getattr(Telegram, "SESSION_NAME", "FileStream") or "FileStream"

FileStream = Client(
    name=session_name,
    api_id=Telegram.API_ID,
    api_hash=Telegram.API_HASH,
    workdir=SESSION_DIR,
    plugins=plugins,
    bot_token=Telegram.BOT_TOKEN,
    sleep_threshold=getattr(Telegram, "PYROGRAM_SLEEP_THRESHOLD", 0),
    workers=Telegram.WORKERS,
    no_updates=no_updates
)
FileStream.client_index = 0


multi_clients = {}
work_loads = {}
