# 🚀 FileStreamBot

A powerful, high-performance Telegram bot that streams files directly from Telegram channels to the web. Watch videos and download files instantly without waiting, with no server storage required.

<p align="center">
  <img src="https://graph.org/file/80d1f94e81bbc1acadb36.jpg" alt="FileStreamBot Cover" width="600">
</p>

<p align="center">
    <a href="https://github.com/Avipatilpro/FileStreamBot/issues">Report Bug</a>
    ·
    <a href="https://github.com/Avipatilpro/FileStreamBot/issues">Request Feature</a>
</p>

## ✨ Features

- **Stream Instantly:** Stream directly from Telegram, with optional local hot-chunk caching for repeat downloads.
- **Multi-Client Support:** Add multiple bot sessions to handle heavy traffic and bypass limits.
- **Web Player:** Locally hosted Video.js player for seamless watching.
- **Direct Download Links:** Generate fast, resume-supported download links.
- **Force Subscribe:** Require users to join a channel before using the bot.
- **NSFW Detection:** Optional automatic scanning and blocking of NSFW content (using NudeNet).
- **URL Shortener:** Monetize links with integrated shortener support.
- **Banning System:** Ban abusive users or channels.
- **Broadcast:** Send messages to all bot users.
- **Customizable:** Easily configure images, text, and settings via environment variables.

## 🛠 Deployment

Install Docker Engine with the Compose plugin on Ubuntu. Point the application
DNS name at the server; allow inbound TCP 80/443 and outbound
Telegram/MongoDB/ACME access. Configure a working ACME email in `.env`. Keep `.env` private (mode 600).

```bash
git clone <repository-url>
cd f2lv2bot-dev
git switch dev
cp example.env .env
nano .env
sudo docker compose up -d --build
```

Updates use the same Compose file:

```bash
git pull --ff-only origin dev
sudo docker compose up -d --build
```

Only `filestreambot` and `traefik` run. No advertising exporter, extra hostname,
host Python, or manually generated files are required. Existing installations must
follow [the migration and rollback guide](deploy/ads/README.md) before cutting over.
Never delete persistent volumes during an update.

## ⚙️ Environment Variables

Create a `.env` file with the following (or set them in your cloud provider):

### Core Required
- `API_ID`: Your Telegram API ID.
- `API_HASH`: Your Telegram API Hash.
- `BOT_TOKEN`: Your Bot Token from @BotFather.
  The bot username is auto-detected from this token at runtime. No separate bot username setting is required.
- `OWNER_ID`: Your Telegram User ID.
- `DATABASE_URL`: MongoDB Connection URI.
- `FQDN`: Your public domain or IP (e.g., `files.example.com`).

### Recommended Channels
- `FLOG_CHANNEL`: Strongly recommended primary cache channel used to keep per-client file IDs in sync for stable streaming and multi-client mode.
- `ADMIN_FLOG_CHANNEL`: Optional second cache/storage channel. Admins can switch new uploads between `FLOG_CHANNEL` and `ADMIN_FLOG_CHANNEL` with `/flogstorage`.
- `ULOG_CHANNEL`: Optional channel ID for new user logs and error logs.
- `BIN_CHANNEL`: Optional legacy storage channel. Current code primarily uses `FLOG_CHANNEL`.
- `NUDENET_CHANNEL`: Optional review/moderation channel for NSFW reports.

### Optional
- `MULTI_TOKEN1`, `MULTI_TOKEN2`: Additional bot tokens for load balancing and user-facing bot replies.
- `MULTI_TOKEN3` ... `MULTI_TOKENN`: You can continue numbering upward for more bots.
- `WORKERS`: Number of workers (Default: 6).
- `PORT`: Web server port (Default: `8080`).
- `BIND_ADDRESS`: Web server bind address (Default: `0.0.0.0`).
- `PING_INTERVAL`: Health/ping interval for the web process (Default: 1200).
- `HAS_SSL`: Generate `https://` links when your reverse proxy/domain uses SSL.
- `NO_PORT`: Hide the explicit port from all generated links when your proxy already terminates on the public host. This applies to the primary bot and all `MULTI_TOKEN*` bot links too.
- `STREAM_CHUNK_SIZE_MB`: Telegram stream chunk size in MB (Default: `1`). Telegram only allows `1 MB` here, so higher values are clamped to `1`.
- `STREAM_PREFETCH_CHUNKS`: Number of Telegram chunks to keep in-flight per stream (Default: `4`, max: `8`).
- `STREAM_LOCAL_CACHE_ENABLED`: Enable local disk cache for hot stream chunks (Default: `True`).
- `STREAM_CACHE_DIR`: Directory for local stream chunk cache (Default: `/tmp/filestream_stream_cache`).
- `STREAM_CACHE_MAX_GB`: Max local stream cache size in GB before old chunks are pruned (Default: `10`).
- `STREAM_CACHE_TTL_HOURS`: How long cached chunks stay valid before pruning (Default: `24`).
- `SESSION_NAME`: Mongo database/session namespace (Default: `FileStream`).
- `ACME_EMAIL`: Email address used by Traefik/Let's Encrypt when deploying with `docker-compose.yml`.
- Traefik uses the `traefik:v3` image specified in the canonical Compose file.
- `TRAEFIK_NETWORK`: Docker network name shared by Traefik and the bot (Default: `filestreambot-proxy`).
- `TRAEFIK_CERT_RESOLVER`: Traefik certificate resolver name (Default: `letsencrypt`).
- `TRAEFIK_HTTP_PORT`, `TRAEFIK_HTTPS_PORT`: Host ports published by Traefik (Defaults: `80`, `443`).
- `FILESTREAM_IMAGE`: Local app image tag built by Compose (Default: `filestreambot:local`).
- `FILESTREAM_HAS_SSL`, `FILESTREAM_NO_PORT`: Compose-only overrides mapped to the app's `HAS_SSL`/`NO_PORT` values for HTTPS links behind Traefik.
- `AUTH_USERS`: Space-separated user IDs allowed to use the bot when access restriction is enabled.
- `MODE`: Use `secondary` if you only want server-side file serving.
- `UPDATES_CHANNEL`: Public updates channel username shown to users.
- Multi-token note: every `MULTI_TOKEN*` bot now starts with updates enabled, uses the same handlers as the primary bot, and generates its own bot-specific `/start` links/buttons during active chats.
- `FORCE_SUB_ID`: Channel ID for force subscription.
- `FORCE_SUB`: Set to `True` to enable.
- `FORCE_SUB_FREE_TASKS`: Number of free file/folder tasks a user can complete before force-sub is required (Default: `10`).
- `FORCE_UPDATES_CHANNEL`: Legacy alias for `FORCE_SUB`.
- `NUDENET_ENABLE`: Set to `True` to enable NSFW scanning.
- `NUDENET_BLOCK_ON_ERROR`: Block content if a scan fails.
- `NUDENET_THRESHOLD`: NSFW score threshold (Default: `0.6`).
- `NUDENET_SCAN_IMAGES`: Scan images for adult content.
- `NUDENET_SCAN_VIDEOS`: Scan videos for adult content.
- `NUDENET_MAX_VIDEO_FRAMES`: Max sampled frames per video.
- `NUDENET_FRAME_INTERVAL`: Seconds between sampled frames.
- `NUDENET_TEMP_DIR`: Temporary directory used during scans.
- `URL_SHORTENER_SITE`: Shortener domain (e.g., `api.gplinks.com`).
- `URL_SHORTENER_API_KEY`: API Key for the shortener.
- `URL_SHORTENER_TIMEOUT`: Shortener request timeout in seconds.
- `URL_SHORTENER_FAIL_THRESHOLD`: Failures before shortener cooldown starts.
- `URL_SHORTENER_COOLDOWN`: Cooldown duration in seconds after repeated failures.
- `PUBLIC_FILE_EXPIRE_HOURS`: Default lifetime for newly generated public file links. Set `0` to disable automatic expiry.
- `FILE_PIC`: Image for `/files` command. Supports an HTTP URL, Telegram `file_id`, or a local path like `images/files.png`.
- `FOLDERS_PIC`: Image for `/folders` command. Supports an HTTP URL, Telegram `file_id`, or a local path like `images/folders.png`.
- `START_PIC`: Image for `/start` command. Supports an HTTP URL, Telegram `file_id`, or a local path like `images/start.png`.
- `ABOUT_PIC`: Default/global image for `/about` command. Defaults to `START_PIC` if not set. Supports an HTTP URL, Telegram `file_id`, or a local path like `images/about.png`.
- `HELP_PIC`: Default/global image for `/help` command and Help button. Defaults to `images/help.png` if present, otherwise `START_PIC`. Supports an HTTP URL, Telegram `file_id`, or a local path like `images/help.png`.
- `LEGAL_PIC`: Image for `/legal`. Defaults to `START_PIC` if not set.
- `PRIVACY_PIC`: Image for `/privacy`. Defaults to `LEGAL_PIC`, then `START_PIC`.
- `VERIFY_PIC`: Image for force subscribe verification. Supports an HTTP URL, Telegram `file_id`, or a local path like `images/verify.png`.
- `MULTI_TOKEN1_FILE_PIC`, `MULTI_TOKEN1_START_PIC`, `MULTI_TOKEN1_ABOUT_PIC`, `MULTI_TOKEN1_HELP_PIC`, `MULTI_TOKEN1_VERIFY_PIC`, `MULTI_TOKEN1_LEGAL_PIC`, `MULTI_TOKEN1_PRIVACY_PIC`, `MULTI_TOKEN1_FOLDERS_PIC`: Optional per-bot media overrides for `MULTI_TOKEN1`. Keep the same pattern for `MULTI_TOKEN2`, `MULTI_TOKEN3`, and so on (`MULTI_TOKEN<N>_*`). For `MULTI_TOKEN<N>_ABOUT_PIC` and `MULTI_TOKEN<N>_HELP_PIC`, a configured non-empty value overrides the image for that bot; if blank or unset, it follows existing fallback behavior (falling back to the global variable and then `START_PIC`). For other media variables, leaving a value blank disables that picture for that specific bot.
- `MULTI_TOKEN1_FQDN`, `MULTI_TOKEN1_PORT`: Optional per-bot web URL overrides for `MULTI_TOKEN1`. Use these when a multi bot should generate links on its own domain or port. Keep the same pattern for `MULTI_TOKEN2`, `MULTI_TOKEN3`, and so on.
  With Docker Compose + Traefik, set `MULTI_TOKEN*_FQDN` as bare hosts and leave `MULTI_TOKEN*_PORT` blank; Traefik forwards every configured domain to the app's internal `PORT`. The Compose Traefik rule includes `MULTI_TOKEN1_FQDN` through `MULTI_TOKEN20_FQDN`.
  If you run without Traefik and set a different `MULTI_TOKEN*_PORT`, the same web app listens on that extra port automatically.
- `WEB_ADS_ENABLED`: the sole master advertising switch. Only `True`/`1` enables
  advertising; missing, empty, invalid or `False`/`0` disables every format.
- Each `WEB_ADS_<PLACEMENT>_BANNER_{KEY,INVOKE_URL,WIDTH,HEIGHT}` group activates
  automatically only when all four fields are valid. Placements: MOBILE_TOP 320x50,
  MOBILE_BOTTOM 300x250, DESKTOP_TOP 728x90, DESKTOP_INLINE 300x250.
- `WEB_ADS_POPUNDER_URL`, `WEB_ADS_DESKTOP_SOCIAL_BAR_URL`,
  `WEB_ADS_MOBILE_SOCIAL_BAR_URL`: explicit valid URLs activate those optional formats
  under the master switch. Empty means off. Clear previously inactive populated
  URLs during migration so they cannot unexpectedly activate.
- `WEB_ADS_SMARTLINK_URL`: optional labelled Sponsored Link, requiring a visitor click.
- Per-client zone fields may use `MULTI_TOKEN<n>_WEB_ADS_...`; there is no per-client
  advertising switch. Existing database/admin disable controls are retained.

Direct provider scripts have same-origin browser authority, including token access;
this is a known security downgrade, not isolated advertising. Provider parser scripts
can also delay initialization. See [the final advertising guide](deploy/ads/README.md)
and [security evidence](deploy/ads/DIRECT-INTEGRATION-AUDIT.md). No production cutover
is authorized by changing source files.

## 🧰 Admin Toggles

- `/urlshortener` → Show URL shortener status.
- `/urlshortener on|off` → Enable/disable URL shortener monetization.

> Legacy `/ads` is still accepted as an alias for `/urlshortener`.

## 🤝 Contributing

Contributions are welcome! Please fork the repository and submit a pull request.

## 📜 License

Distributed under the MIT License. See `LICENSE` for more information.

<p align="center">© 2024 FileStreamBot</p>
