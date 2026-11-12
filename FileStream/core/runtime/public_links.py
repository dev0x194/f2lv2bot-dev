from __future__ import annotations

import urllib.parse

from pyrogram import Client

from FileStream.config import Server
from FileStream.bot.helpers.client_identity import build_start_link


def _normalized_host_variants(value: str | None) -> set[str]:
    raw = str(value or "").strip().lower()
    if not raw:
        return set()

    parsed = urllib.parse.urlparse(raw if "://" in raw else f"//{raw}")
    netloc = str(parsed.netloc or parsed.path or "").strip().lower()
    hostname = str(parsed.hostname or "").strip().lower()

    variants = {item for item in (raw, netloc, hostname) if item}
    if hostname and parsed.port:
        variants.add(f"{hostname}:{parsed.port}")
    return variants


def resolve_bot_client(
    bot: Client | None = None,
    *,
    file_info: dict | None = None,
    folder: dict | None = None,
    bot_id: int | str | None = None,
) -> Client | None:
    if bot is not None:
        return bot

    raw_bot_id = bot_id
    if raw_bot_id in (None, "") and file_info:
        raw_bot_id = file_info.get("source_bot_id")
    if raw_bot_id in (None, "") and folder:
        raw_bot_id = folder.get("bot_id")

    try:
        normalized_bot_id = int(raw_bot_id) if raw_bot_id not in (None, "") else None
    except Exception:
        normalized_bot_id = None

    if normalized_bot_id is None:
        return None

    from FileStream.bot import FileStream, multi_clients

    for client in multi_clients.values():
        try:
            if int(getattr(client, "id", 0) or 0) == normalized_bot_id:
                return client
        except Exception:
            continue

    try:
        if int(getattr(FileStream, "id", 0) or 0) == normalized_bot_id:
            return FileStream
    except Exception:
        pass

    return None


def resolve_bot_client_for_host(host: str | None) -> Client | None:
    expected_hosts = _normalized_host_variants(host)
    if not expected_hosts:
        return None

    from FileStream.bot import FileStream, multi_clients

    candidates: list[tuple[int, Client]] = [(0, FileStream)]
    for idx, client in sorted(multi_clients.items(), key=lambda item: item[0]):
        if int(idx) == 0:
            continue
        candidates.append((int(idx), client))

    for client_index, client in candidates:
        if expected_hosts & _normalized_host_variants(Server.url_for_client_index(client_index)):
            return client

    return None


def build_base_url(
    bot: Client | None = None,
    *,
    file_info: dict | None = None,
    folder: dict | None = None,
    bot_id: int | str | None = None,
    request: any = None,
) -> str:
    resolved_bot = resolve_bot_client(bot, file_info=file_info, folder=folder, bot_id=bot_id)
    if resolved_bot is not None:
        url = Server.url_for_bot(resolved_bot)
    else:
        url = Server.URL

    if request is not None:
        headers = getattr(request, "headers", {})
        forwarded_proto = headers.get("X-Forwarded-Proto")
        forwarded_host = headers.get("X-Forwarded-Host") or headers.get("Host")
        if forwarded_proto and forwarded_host:
            return f"{forwarded_proto}://{forwarded_host}/"
        elif getattr(request, "scheme", None) and getattr(request, "host", None) and not url:
            return f"{request.scheme}://{request.host}/"

    return f"{str(url or '').rstrip('/')}/"


def build_public_file_url(public_id: str, bot: Client | None = None, *, file_info: dict | None = None, bot_id: int | str | None = None) -> str:
    base = build_base_url(bot, file_info=file_info, bot_id=bot_id).rstrip("/")
    return f"{base}/gen/{str(public_id or '').strip()}"


def build_public_folder_url(public_id: str, bot: Client | None = None, *, folder: dict | None = None, bot_id: int | str | None = None) -> str:
    base = build_base_url(bot, folder=folder, bot_id=bot_id).rstrip("/")
    return f"{base}/gfolder/{str(public_id or '').strip()}"


def build_public_stream_url(public_id: str, bot: Client | None = None, *, file_info: dict | None = None, bot_id: int | str | None = None) -> str:
    base = build_base_url(bot, file_info=file_info, bot_id=bot_id).rstrip("/")
    return f"{base}/dl/{str(public_id or '').strip()}"


def build_public_download_token_path(public_id: str) -> str:
    return f"/get-download-token/{str(public_id or '').strip()}"


def build_public_stream_token_path(public_id: str) -> str:
    return f"/get-stream-token/{str(public_id or '').strip()}"


def build_public_bot_open_link(public_id: str, bot: Client | None = None) -> str:
    return build_start_link(f"open_{str(public_id or '').strip()}", bot)


def build_telegram_share_link(url: str, text: str = "") -> str:
    params = {"url": str(url or "").strip()}
    if str(text or "").strip():
        params["text"] = str(text or "").strip()
    return "https://t.me/share/url?" + urllib.parse.urlencode(params)
