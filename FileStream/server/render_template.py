import os
import re
import json
import time
import mimetypes
import jinja2
import urllib.parse

from FileStream import __version__
from FileStream.config import Telegram, Server, WebAds
from FileStream.bot import FileStream
from FileStream.database.database import Database
from FileStream.services.token_service import token_service
from FileStream.core.runtime.human_readable import humanbytes
from FileStream.core.media.category import detect_category
from FileStream.core.media.file_properties import ensure_flog_media_exists
from FileStream.core.runtime.public_links import (
    build_base_url,
    build_public_bot_open_link,
    build_public_download_token_path,
    build_public_file_url,
    build_public_folder_url,
    build_public_stream_token_path,
    resolve_bot_client,
    resolve_bot_client_for_host,
)
from FileStream.bot.helpers.client_identity import build_add_to_group_link, build_start_link, get_bot_name, get_bot_username
from FileStream.bot.helpers.legal import (
    build_policy_page_context,
    build_policy_url,
    build_updates_channel_url,
)
from FileStream.server.exceptions import FileNotFound


env = jinja2.Environment(autoescape=True)
db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)


_NAT_NUM_RE = re.compile(r"(\d+)")
_SERIES_RE_1 = re.compile(r"[sS](\d{1,2})[\s._-]*[eE](\d{1,3})")
_SERIES_RE_2 = re.compile(r"\b(\d{1,2})x(\d{1,3})\b", re.IGNORECASE)
_EPISODE_ONLY_RE = re.compile(r"\b(?:ep|episode)[\s._-]*(\d{1,3})\b", re.IGNORECASE)


def _natural_key(text: str):
    cleaned = str(text or "").replace("_", " ").strip().lower()
    parts = _NAT_NUM_RE.split(cleaned)
    key = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part))
    return tuple(key)


def _series_episode_key(name: str):
    base = os.path.splitext(str(name or ""))[0]

    m = _SERIES_RE_1.search(base)
    if m:
        return int(m.group(1)), int(m.group(2))

    m = _SERIES_RE_2.search(base)
    if m:
        return int(m.group(1)), int(m.group(2))

    m = _EPISODE_ONLY_RE.search(base)
    if m:
        return 0, int(m.group(1))

    return None


def _folder_sort_key(item: dict):
    raw_name = item.get("_sort_name") or item.get("name") or ""
    category = str(item.get("category") or "").lower()

    if category in {"tv-series", "anime"}:
        series_key = _series_episode_key(raw_name)
        if series_key:
            season, episode = series_key
            return (0, season, episode, _natural_key(raw_name))

    return (1, _natural_key(raw_name))


def _safe_text(value, fallback: str) -> str:
    text = str(value or fallback)
    text = text.replace("\n", " ").replace("\r", " ").strip()
    return text or fallback


def _sanitize_uploader(name: str | None) -> str | None:
    if not name or not isinstance(name, str):
        return None
    cleaned = name.replace("\n", " ").replace("\r", " ").strip()
    cleaned = cleaned.strip(" ,._-;:~|/\\")
    if not any(c.isalnum() for c in cleaned):
        return None
    if cleaned.lower() in {"unknown", "unknown uploader", "none", "null", "user", "anonymous"}:
        return None
    if len(cleaned) > 80:
        cleaned = cleaned[:80] + "…"
    return cleaned


def format_clock(seconds: int | float | None) -> str:
    try:
        s = int(float(seconds or 0))
    except Exception:
        return "--:--"
    if s <= 0:
        return "--:--"
    hrs = s // 3600
    mins = (s % 3600) // 60
    secs = s % 60
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def _bot_join_url(bot=None) -> str | None:
    username = get_bot_username(bot or FileStream)
    if not username:
        return None
    return f"https://t.me/{username}"


def _bot_brand(bot=None) -> str:
    username = get_bot_username(bot)
    if username:
        return f"@{username}"
    return get_bot_name(bot) or "File To Link V2 bot"


def _http_media_url(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if raw.lower().startswith(("http://", "https://")):
        return raw
    return None


async def _template_context(**kwargs):
    web_link_context = kwargs.pop("_web_link_context", {}) or {}
    bot_client = kwargs.pop("_bot_client", None) or FileStream
    web_ads_enabled = await db.get_web_ads_status(bot=bot_client)
    bot_username = get_bot_username(bot_client)
    bot_name = get_bot_name(bot_client)
    return {
        **kwargs,
        "ad_config": WebAds.template_context(enabled_override=web_ads_enabled, bot=bot_client),
        "bot_username": bot_username,
        "bot_name": bot_name,
        "bot_brand": _bot_brand(bot_client),
        "bot_join_url": _bot_join_url(bot_client),
        "legal_url": build_policy_url("legal", **web_link_context),
        "privacy_url": build_policy_url("privacy", **web_link_context),
        "updates_channel_url": build_updates_channel_url(),
        "version": __version__,
    }


def _resolve_template_path(template_name: str) -> str:
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    primary = os.path.join(base_dir, "web", "templates", template_name)
    if os.path.isfile(primary):
        return primary
    return os.path.join(base_dir, "template", template_name)


_TEMPLATE_CACHE: dict[str, jinja2.Template] = {}


def _get_compiled_template(template_name: str) -> jinja2.Template:
    if template_name not in _TEMPLATE_CACHE:
        template_file = _resolve_template_path(template_name)
        with open(template_file, "r", encoding="utf-8") as f:
            _TEMPLATE_CACHE[template_name] = env.from_string(f.read())
    return _TEMPLATE_CACHE[template_name]


async def render_page(
    db_id,
    file_data: dict | None = None,
    public_link: dict | None = None,
    *,
    session_id: str | None = None,
):
    file_data = file_data or await db.get_file(db_id)
    if not file_data:
        raise FileNotFound
    public_link = public_link or await db.ensure_public_link_for_file(file_data)
    public_id = public_link["public_id"]

    template = _get_compiled_template("play.html")

    raw_name = file_data.get("file_name") or "file"
    file_name = raw_name.replace("_", " ")
    if len(file_name) > 75:
        file_name = file_name[:75] + "…"

    page_bot = resolve_bot_client(file_info=file_data) or FileStream
    share_url = build_public_file_url(public_id, file_info=file_data)
    file_size = humanbytes(file_data.get("file_size") or 0)
    page_token = token_service.create_access_token(
        public_id,
        kind="page",
        expires_in_seconds=1800,
        metadata={"session_id": str(session_id or "").strip()},
    )

    mime_type = (file_data.get("mime_type") or "").lower()
    primary = mime_type.split("/")[0].strip() if mime_type else ""
    ext = os.path.splitext(raw_name)[1].lower()

    video_ext = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".mpeg", ".mpg"}
    audio_ext = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".wav", ".opus", ".oga"}

    category = file_data.get("category") or detect_category(file_name=raw_name, mime_type=mime_type, file_ext=ext)
    uploader = _sanitize_uploader(file_data.get("uploader"))

    container_format = ext.lstrip(".").upper() if ext else (primary.upper() or "FILE")

    video_codec = file_data.get("video_codec")
    audio_codec = file_data.get("audio_codec")
    resolution = file_data.get("resolution")

    if not resolution:
        w = int(file_data.get("width") or 0)
        h = int(file_data.get("height") or 0)
        if w and h:
            max_d = max(w, h)
            min_d = min(w, h)
            if min_d >= 2160 or max_d >= 3840:
                resolution = "4K / 2160p"
            elif min_d >= 1440 or max_d >= 2560:
                resolution = "1440p / 2K"
            elif min_d >= 1080 or max_d >= 1920:
                resolution = "1080p Full HD"
            elif min_d >= 720 or max_d >= 1280:
                resolution = "720p HD"
            elif min_d >= 480:
                resolution = "480p SD"
            else:
                resolution = f"{w}x{h}"
        else:
            q_match = re.search(r"\b(2160p|4k|1440p|2k|1080p|720p|480p|360p)\b", raw_name, re.IGNORECASE)
            if q_match:
                q_str = q_match.group(1).upper()
                resolution = "2160p (4K)" if q_str == "4K" else q_str

    if not video_codec:
        v_match = re.search(r"\b(x265|h265|hevc|x264|h264|avc|vp9|av1|10bit)\b", raw_name, re.IGNORECASE)
        if v_match:
            v_str = v_match.group(1).upper()
            video_codec = "HEVC / H.265" if v_str in ("X265", "H265", "HEVC") else ("AVC / H.264" if v_str in ("X264", "H264", "AVC") else v_str)

    if not audio_codec:
        a_match = re.search(r"\b(aac|ac3|eac3|dts|flac|mp3|opus|ddp5\.1|dd5\.1)\b", raw_name, re.IGNORECASE)
        if a_match:
            audio_codec = a_match.group(1).upper()

    message_id = file_data.get("message_id")

    is_audio = primary == "audio" or ext in audio_ext
    is_video = primary == "video" or ext in video_ext
    is_playable = is_audio or is_video

    if is_audio:
        resolved_mime = mime_type or "audio/mpeg"
    elif is_video:
        resolved_mime = mime_type or "video/mp4"
    else:
        guessed, _ = mimetypes.guess_type(raw_name)
        resolved_mime = guessed or mime_type or "application/octet-stream"

    report_url = None
    if get_bot_username(page_bot):
        report_url = build_start_link(f"report_file_{public_id}", page_bot)

    return template.render(
        **await _template_context(
            _web_link_context={"file_info": file_data},
            _bot_client=page_bot,
            file_name=file_name,
            file_id=public_id,
            share_url=share_url,
            telegram_url=build_public_bot_open_link(public_id, page_bot),
            public_id=public_id,
            download_token_path=build_public_download_token_path(public_id),
            stream_token_path=build_public_stream_token_path(public_id),
            page_token=page_token,
            file_size=file_size,
            mime_type=resolved_mime,
            category=category,
            uploader=uploader,
            message_id=message_id,
            is_audio=is_audio,
            is_video=is_video,
            is_playable=is_playable,
            report_url=report_url,
            file_duration=int(file_data.get("duration") or 0),
            formatted_duration=format_clock(file_data.get("duration")) if int(file_data.get("duration") or 0) > 0 else None,
            container_format=container_format,
            resolution=resolution,
            video_codec=video_codec,
            audio_codec=audio_codec,
            is_native_playable=bool(ext in {".mp4", ".m4v", ".webm"} or "mp4" in mime_type or "webm" in mime_type),
            thumbnails_enabled=bool(Server.THUMBNAILS_ENABLED),
        )
    )


async def render_public_page(public_id: str, *, session_id: str | None = None):
    file_data, public_link = await db.resolve_public_file(public_id, increment_click=True)
    return await render_page(
        str(file_data["_id"]),
        file_data=file_data,
        public_link=public_link,
        session_id=session_id,
    )


async def render_folder(
    folder_id: str,
    title: str = "Folder",
    folder_doc: dict | None = None,
    public_link: dict | None = None,
    *,
    session_id: str | None = None,
):
    folder_doc = folder_doc or await db.get_folder(folder_id)
    if not folder_doc:
        raise FileNotFound
    public_link = public_link or await db.ensure_public_link_for_folder(folder_doc)
    folder_public_id = public_link["public_id"]
    folder_bot = resolve_bot_client(folder=folder_doc) or FileStream

    file_ids = folder_doc.get("files") or []
    files = []

    for fid in file_ids:
        file_data = await db.get_file(fid)
        if not file_data:
            continue
        file_public = await db.ensure_public_link_for_file(file_data)
        file_public_id = file_public["public_id"]

        raw_name = file_data.get("file_name") or "file"
        file_name = raw_name.replace("_", " ")
        mime = (file_data.get("mime_type") or "").lower()
        primary = mime.split("/")[0].strip() if mime else ""
        ext = os.path.splitext(raw_name)[1].lower()

        video_ext = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".mpeg", ".mpg"}
        audio_ext = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".wav", ".opus", ".oga"}

        kind = "file"
        if primary == "video" or ext in video_ext:
            kind = "video"
            if not mime:
                mime = "video/mp4"
        elif primary == "audio" or ext in audio_ext:
            kind = "audio"
            if not mime:
                mime = "audio/mpeg"

        playable = kind in ("video", "audio")
        category = file_data.get("category") or detect_category(file_name=raw_name, mime_type=mime, file_ext=ext)
        uploader = _sanitize_uploader(file_data.get("uploader"))

        files.append(
            {
                "id": file_public_id,
                "name": file_name,
                "_sort_name": raw_name,
                "size": humanbytes(file_data.get("file_size") or 0),
                "mime": mime,
                "kind": kind,
                "category": category,
                "uploader": uploader,
                "playable": playable,
                "share_url": build_public_file_url(file_public_id, file_info=file_data),
                "download_id": file_public_id,
                "stream_token_path": build_public_stream_token_path(file_public_id),
                "download_token_path": build_public_download_token_path(file_public_id),
                "page_token": token_service.create_access_token(
                    file_public_id,
                    kind="page",
                    expires_in_seconds=1800,
                    metadata={"session_id": str(session_id or "").strip()},
                ),
                "telegram_url": build_public_bot_open_link(file_public_id, resolve_bot_client(file_info=file_data) or folder_bot),
            }
        )

    if not files:
        raise FileNotFound

    files.sort(key=_folder_sort_key)
    for item in files:
        item.pop("_sort_name", None)

    folder_json = json.dumps(files)

    template_file = _resolve_template_path("folder.html")
    with open(template_file, "r", encoding="utf-8") as f:
        template = env.from_string(f.read())

    report_url = None
    if get_bot_username(folder_bot):
        report_url = build_start_link(f"report_folder_{folder_public_id}", folder_bot)

    return template.render(
        **await _template_context(
            _web_link_context={"folder": folder_doc},
            _bot_client=folder_bot,
            folder_id=folder_public_id,
            folder_share_url=build_public_folder_url(folder_public_id, folder=folder_doc),
            folder_json=folder_json,
            files=files,
            count=len(files),
            page_title=title,
            report_url=report_url,
        )
    )


async def render_public_folder(public_id: str, title: str | None = None, *, session_id: str | None = None):
    folder_doc, public_link = await db.resolve_public_folder(public_id, increment_click=True)
    folder_title = title if (title and title != "Folder") else ((folder_doc.get("title") if folder_doc else None) or title or "Folder")
    return await render_folder(
        str(folder_doc["_id"]),
        title=folder_title,
        folder_doc=folder_doc,
        public_link=public_link,
        session_id=session_id,
    )


async def render_policy_page(page: str, *, bot=None) -> str:
    template_file = _resolve_template_path("legal.html")
    with open(template_file, "r", encoding="utf-8") as f:
        template = env.from_string(f.read())

    bot_client = bot or FileStream
    context = build_policy_page_context(
        page,
        bot_username=get_bot_username(bot_client),
        bot_name=get_bot_name(bot_client),
        bot=bot_client,
    )
    return template.render(**await _template_context(_bot_client=bot_client, **context))


async def render_landing_page(*, bot=None) -> str:
    template_file = _resolve_template_path("landing.html")
    with open(template_file, "r", encoding="utf-8") as f:
        template = env.from_string(f.read())

    bot_client = bot or FileStream
    bot_username = get_bot_username(bot_client)
    bot_name = get_bot_name(bot_client)
    brand_name = bot_username or bot_name.replace(" ", "") or "FileToLinkBot"
    canonical_url = str(Server.url_for_bot(bot_client) or "").rstrip("/")
    legal_url = build_policy_url("legal", bot=bot_client)
    privacy_url = build_policy_url("privacy", bot=bot_client)
    updates_channel_url = build_updates_channel_url()
    bot_join_url = _bot_join_url(bot_client)
    preview_image_url = _http_media_url(Telegram.bot_media(bot_client, "START_PIC"))
    lastmod = time.strftime("%Y-%m-%d")

    title = f"{brand_name} | Telegram File to Link & Direct Download Bot"
    description = (
        f"{brand_name} turns Telegram files into clean share pages with fast direct downloads, folder links, and Telegram reopen actions."
    )
    keywords = [
        brand_name,
        bot_username,
        "FileToLink",
        "Telegram file to link",
        "Telegram direct download link",
        "Telegram folder share bot",
    ]

    faq_items = [
        {
            "question": f"What is {brand_name}?",
            "answer": (
                f"{brand_name} is a Telegram bot that turns uploaded files into public download and folder links."
            ),
        },
        {
            "question": f"How does {brand_name} work?",
            "answer": (
                "Send a file to the bot, let it create a public share page, then open the generated link to download it or reopen it in Telegram."
            ),
        },
        {
            "question": "Can I share one clean link?",
            "answer": (
                "Yes. The bot is built around generated public share routes so users can share one clean page instead of raw internal file links."
            ),
        },
    ]

    schema = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebSite",
                "name": title,
                "url": canonical_url,
                "description": description,
            },
            {
                "@type": "SoftwareApplication",
                "name": brand_name,
                "alternateName": list(dict.fromkeys([item for item in [brand_name, bot_username, "File To Link V2 bot", "FileToLink"] if item])),
                "applicationCategory": "ProductivityApplication",
                "operatingSystem": "Telegram Web, Android, iOS, Desktop",
                "description": description,
                "url": canonical_url,
                "softwareVersion": __version__,
                "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
            },
            {
                "@type": "FAQPage",
                "mainEntity": [
                    {
                        "@type": "Question",
                        "name": item["question"],
                        "acceptedAnswer": {"@type": "Answer", "text": item["answer"]},
                    }
                    for item in faq_items
                ],
            },
        ],
    }

    features = [
        {
            "title": "One Clean Share Link",
            "text": "Generate a single public page for each file instead of exposing raw stream endpoints.",
        },
        {
            "title": "Watch, Download, Reopen",
            "text": "Users can watch media in the browser, start downloads, or reopen the file inside Telegram.",
        },
        {
            "title": "Folder Sharing",
            "text": "Bundle multiple files into one folder page and share the whole set with one URL.",
        },
        {
            "title": "Telegram-Powered Delivery",
            "text": "The platform uses Telegram delivery with secure page-bound web access.",
        },
    ]

    steps = [
        "Send or forward a file to the bot.",
        "Receive a generated public share page instantly.",
        "Share the link anywhere or open it again in Telegram.",
    ]

    return template.render(
        **await _template_context(
            page_title=title,
            meta_description=description,
            meta_keywords=", ".join(dict.fromkeys(keywords)),
            canonical_url=canonical_url,
            preview_image_url=preview_image_url,
            schema_json=json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
            brand_name=brand_name,
            bot_name=bot_name,
            bot_username=bot_username,
            bot_join_url=bot_join_url,
            add_to_group_url=build_add_to_group_link(bot_client),
            updates_channel_url=updates_channel_url,
            legal_url=legal_url,
            privacy_url=privacy_url,
            features=features,
            steps=steps,
            faq_items=faq_items,
            search_aliases=list(dict.fromkeys([item for item in [brand_name, bot_username, "FileToLink", "Telegram direct download link", "Telegram folder share bot"] if item])),
            lastmod=lastmod,
        )
    )


def render_robots_txt(*, bot=None) -> str:
    base_url = str(Server.url_for_bot(bot) if bot else Server.URL).rstrip("/")
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /dl/",
        "Disallow: /file/",
        "Disallow: /stream/",
        "Disallow: /get-download-token/",
        "Disallow: /get-stream-token/",
        "Disallow: /gen/",
        "Disallow: /gfolder/",
        "Disallow: /status",
        f"Sitemap: {base_url}/sitemap.xml",
    ]
    return "\n".join(lines) + "\n"


def render_sitemap_xml(*, bot=None) -> str:
    today = time.strftime("%Y-%m-%d")
    base_url = str(Server.url_for_bot(bot) if bot else Server.URL).rstrip("/") + "/"
    urls = (
        base_url,
        build_policy_url("legal", bot=bot),
        build_policy_url("privacy", bot=bot),
    )
    items = "\n".join(
        f"  <url><loc>{url}</loc><lastmod>{today}</lastmod><changefreq>weekly</changefreq><priority>{priority}</priority></url>"
        for url, priority in zip(urls, ("1.0", "0.5", "0.5"))
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{items}\n"
        "</urlset>\n"
    )


async def render_public_error_page(title: str, message: str, *, bot=None) -> str:
    template_file = _resolve_template_path("error.html")
    with open(template_file, "r", encoding="utf-8") as f:
        template = env.from_string(f.read())
    return template.render(
        **await _template_context(
            _bot_client=bot or FileStream,
            title=title,
            message=message,
        )
    )


def resolve_request_bot(host: str | None):
    return resolve_bot_client_for_host(host) or FileStream
