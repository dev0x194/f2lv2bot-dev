import os
import sys
import asyncio
import logging
import traceback
import logging.handlers as handlers
from dotenv import load_dotenv

# Ensure stdout/stderr flush immediately in Docker and non-interactive environments
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

# Configure root logging immediately before any project or 3rd-party imports
logging.basicConfig(
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
    format="[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
    handlers=[
        logging.StreamHandler(stream=sys.stdout),
        handlers.RotatingFileHandler(os.environ.get("STREAMBOT_LOG_PATH", "streambot.log"), mode="a", maxBytes=104857600, backupCount=2, encoding="utf-8")
    ],
    force=True
)

logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("aiohttp.web").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.INFO)

logger = logging.getLogger("FileStream")

_root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_env_path_override = os.environ.get("ENV_FILE")
if _env_path_override and os.path.isfile(_env_path_override):
    load_dotenv(dotenv_path=_env_path_override)
elif _env_path_override and os.path.isfile(os.path.join(_root_dir, _env_path_override)):
    load_dotenv(dotenv_path=os.path.join(_root_dir, _env_path_override))
elif os.path.isfile(os.path.join(_root_dir, ".env")):
    load_dotenv(dotenv_path=os.path.join(_root_dir, ".env"))
elif os.path.isfile(".env"):
    load_dotenv(dotenv_path=".env")


from FileStream.config import Telegram, Server
from aiohttp import web
from pyrogram import idle
from pyrogram.errors import FloodWait

from FileStream.bot import FileStream, multi_clients
from FileStream.server import web_server
from FileStream.bot.clients import initialize_clients
from FileStream.bot.helpers.bot_commands import register_bot_commands
from FileStream.database.database import Database
from FileStream.core.runtime.flog_sync import reconcile_flog_storage, start_flog_sync_task, stop_flog_sync_task
from FileStream.core.runtime.flog_channels import configured_flog_channels, optional_channel_name_for_mode
from FileStream.core.runtime.optional_channels import warm_optional_channel_peer
from FileStream.core.runtime.stream_cache import warm_stream_cache
from FileStream.services.storage_service import storage_service
from FileStream.core.monitoring.audit_manager import audit_manager

server = web.AppRunner(web_server())
sites = []
db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)


async def _sleep_with_progress(total_seconds: int):
    remaining = max(int(total_seconds), 0)
    while remaining > 0:
        step = 60 if remaining > 60 else remaining
        await asyncio.sleep(step)
        remaining -= step
        if remaining > 0:
            print(f"Startup FloodWait remaining: {remaining}s", flush=True)
            logger.info("Startup FloodWait remaining: %ss", remaining)


async def start_services():
    server_mode = "Secondary" if Telegram.SECONDARY else "Primary"
    logger.info("Starting FileStream as %s server...", server_mode)
    print(flush=True)
    if Telegram.SECONDARY:
        print("------------------ Starting as Secondary Server ------------------", flush=True)
    else:
        print("------------------- Starting as Primary Server -------------------", flush=True)

    print(flush=True)
    print("--------------------- Initializing Web Server ---------------------", flush=True)
    logger.info("Initializing Database indexes and stream cache...")
    await db.ensure_indexes()
    await warm_stream_cache()
    logger.info("Database initialized.")
    await server.setup()
    listen_ports = [Server.PORT, *Server.extra_ports()]
    for port in listen_ports:
        site = web.TCPSite(server, Server.BIND_ADDRESS, port)
        await site.start()
        sites.append(site)
    logger.info("Web server listening on %s (port(s): %s)", Server.BIND_ADDRESS, ", ".join(str(p) for p in listen_ports))
    print("------------------------------ DONE ------------------------------", flush=True)
    if len(listen_ports) > 1:
        print(f"Web listeners active on ports: {', '.join(str(port) for port in listen_ports)}", flush=True)

    print(flush=True)
    print("-------------------- Initializing Telegram Bot --------------------", flush=True)
    logger.info("Connecting Telegram Bot client...")

    current_loop = asyncio.get_running_loop()
    if getattr(FileStream, "loop", None) is not current_loop:
        logger.info("Synchronizing FileStream client loop to active event loop...")
        FileStream.loop = current_loop
        if hasattr(FileStream, "dispatcher"):
            FileStream.dispatcher.loop = current_loop
            FileStream.dispatcher.updates_queue = asyncio.Queue()
            FileStream.dispatcher.locks_list.clear()

    while True:
        try:
            await FileStream.start()
            break
        except FloodWait as e:
            wait_for = int(getattr(e, "value", 0)) + 1
            mins = wait_for // 60
            logger.warning("FloodWait during startup: sleeping %ss (~%sm)", wait_for, mins)
            print(f"FloodWait during startup: sleeping {wait_for}s (~{mins}m)", flush=True)
            await _sleep_with_progress(wait_for)

    bot_info = await FileStream.get_me()
    FileStream.id = bot_info.id
    FileStream.username = bot_info.username
    FileStream.fname = bot_info.first_name
    logger.info("Telegram Bot started as @%s (%s, ID: %s)", bot_info.username, bot_info.first_name, bot_info.id)
    for mode, channel_id in configured_flog_channels().items():
        await warm_optional_channel_peer(FileStream, optional_channel_name_for_mode(mode), channel_id)
    await warm_optional_channel_peer(FileStream, "ULOG_CHANNEL", Telegram.ULOG_CHANNEL)
    if not Telegram.SECONDARY:
        await register_bot_commands(FileStream)

    await asyncio.sleep(0.1)
    total_handlers = sum(len(h) for h in FileStream.dispatcher.groups.values()) if hasattr(FileStream, "dispatcher") else 0
    msg_handlers = sum(
        1 for h_list in getattr(FileStream.dispatcher, "groups", {}).values()
        for h in h_list if type(h).__name__ == "MessageHandler"
    )
    cmd_handlers = sum(
        1 for h_list in getattr(FileStream.dispatcher, "groups", {}).values()
        for h in h_list if type(h).__name__ == "MessageHandler" and getattr(h, "filters", None) and any("command" in str(f).lower() for f in [h.filters])
    )
    logger.info("Bot identity: @%s (ID: %s)", bot_info.username, bot_info.id)
    logger.info("Bot client: CONNECTED")
    logger.info("Plugin loading: SUCCESS (%s handlers loaded across %s groups)", total_handlers, len(getattr(FileStream.dispatcher, "groups", {})))
    logger.info("Registered message handlers: %s", msg_handlers)
    logger.info("Command handlers: %s", cmd_handlers)
    logger.info("Update dispatcher: RUNNING (workers: %s)", getattr(FileStream, "workers", 0))
    logger.info("Command registration: SUCCESS")
    print("------------------------------ DONE ------------------------------", flush=True)

    print(flush=True)
    print("---------------------- Initializing Clients ----------------------", flush=True)
    logger.info("Initializing multi-clients...")
    await initialize_clients()
    await storage_service.startup_recovery()
    for client in multi_clients.values():
        for mode, channel_id in configured_flog_channels().items():
            await warm_optional_channel_peer(client, optional_channel_name_for_mode(mode), channel_id)
    await reconcile_flog_storage(FileStream, force=True)
    start_flog_sync_task(FileStream)
    logger.info("Clients initialized successfully (total active: %s, multi-client mode: %s)", len(multi_clients), Telegram.MULTI_CLIENT)
    print("------------------------------ DONE ------------------------------", flush=True)

    print(flush=True)
    print("------------------------- Service Started -------------------------", flush=True)
    print("                        bot =>> {}".format(bot_info.first_name), flush=True)
    if bot_info.dc_id:
        print("                        DC ID =>> {}".format(str(bot_info.dc_id)), flush=True)
    print(" URL =>> {}".format(Server.URL), flush=True)
    print("------------------------------------------------------------------", flush=True)
    logger.info("FileStream started successfully! URL => %s", Server.URL)
    await idle()

async def cleanup():
    logger.info("Stopping background tasks and shutting down services...")
    try:
        await stop_flog_sync_task()
    except Exception:
        pass

    try:
        await storage_service.shutdown(timeout=5.0)
    except Exception as exc:
        logger.warning("Error shutting down storage_service: %s", exc)

    try:
        await audit_manager.shutdown(timeout=3.0)
    except Exception as exc:
        logger.warning("Error shutting down audit_manager: %s", exc)

    for site in sites:
        try:
            await site.stop()
        except Exception:
            pass
    sites.clear()

    try:
        await server.cleanup()
    except Exception:
        pass

    for client in list(multi_clients.values()):
        try:
            if getattr(client, "is_connected", False):
                await client.stop()
        except Exception:
            pass

    try:
        if FileStream.is_connected:
            await FileStream.stop()
    except Exception:
        pass

    try:
        await db.close()
    except Exception:
        pass
    logger.info("FileStream services stopped.")


async def main():
    try:
        await start_services()
    finally:
        await cleanup()
        print("------------------------ Stopped Services ------------------------", flush=True)


try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)


if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as err:
        logging.critical("Fatal error running FileStream: %s", err)
        logging.error(traceback.format_exc())

