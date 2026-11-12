# FileToLinkV2bot - Development Deployment Guide

This guide walks you through deploying and managing the **isolated development stack** (`filetolinkv2bot-dev`) alongside the production stack (`filetolinkv2bot`) on the same VPS without downtime, cross-talk, or resource collisions.

---

## 1. Architecture & Port Allocation

| Component | Production Stack | Development Stack |
| :--- | :--- | :--- |
| **Compose Command** | `docker compose up -d` | `docker compose -f docker-compose.dev.yml --env-file dev.env up -d` |
| **Project Name** | `filetolinkv2bot` | `filetolinkv2bot-dev` |
| **Containers** | `filetolinkv2bot-filestreambot-1`<br>`filetolinkv2bot-traefik-1` | `filetolinkv2bot-filestreambot-dev` |
| **Host Web Port** | `80`, `443` (Traefik) | **`8081`** (Direct HTTP) |
| **Network** | `filestreambot-proxy` | `filestreambot-dev-network` |
| **Database** | External Production Cluster (`FileStream`) | External Development Cluster / Atlas (`FileStreamDev`) |
| **Volumes** | `filestream-cache`, `letsencrypt` | `filestream_cache_dev`, `filestream_sessions_dev` |
| **Session File** | `FileStream.session` | `/app/sessions/FileStreamDev.session` |
| **Log File** | `streambot.log` | `streambot_dev.log` |

---

## 2. Prerequisites

1. **Dedicated Telegram Bot**: Create a separate test bot via [@BotFather](https://t.me/BotFather).
   > [!IMPORTANT]
   > **NEVER** use the production bot token in `dev.env`.
2. **Telegram API Credentials**: An `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org).
3. **VPS Host Port 8081**: Make sure firewall/security groups allow inbound traffic on port `8081` (or your chosen `DEV_HOST_PORT`) if testing web links externally.

---

## 3. Quickstart: Step-by-Step Deployment

### Step 1: Create your local `dev.env`
Copy the template:
```bash
cp dev.env.example dev.env
```
*(Note: `dev.env` is strictly ignored by `.gitignore` so your test credentials will never be committed).*

### Step 2: Configure your Test Credentials
Edit `dev.env` using `nano` or your editor:
```bash
nano dev.env
```

Fill in your development bot credentials:
```ini
# --- TELEGRAM BOT CREDENTIALS ---
API_ID=1234567
API_HASH=your_telegram_api_hash
BOT_TOKEN=123456789:ABCDefghIJKlmnoPQRstuvWXyz
OWNER_ID=your_telegram_user_id

# --- SERVER & PORT ---
PORT=8081
DEV_HOST_PORT=8081
FQDN=YOUR_VPS_IP_OR_DEV_DOMAIN
HAS_SSL=False
NO_PORT=False
```
*(Set `DATABASE_URL` to your MongoDB connection string, e.g. MongoDB Atlas cluster).*

### Step 3: Validate the Compose Configuration
Before starting, verify that the environment variables and services parse cleanly:
```bash
docker compose -f docker-compose.dev.yml --env-file dev.env config
```

### Step 4: Build and Start the Development Stack
```bash
docker compose -f docker-compose.dev.yml --env-file dev.env up -d --build
```

---

## 4. Verification & Testing

### 1. Check Container Status
Verify that the development container is running:
```bash
docker compose -f docker-compose.dev.yml --env-file dev.env ps
```
You should see:
- `filetolinkv2bot-filestreambot-dev`: `Up`

### 2. Follow Development Logs
Stream the live logs to confirm clean startup:
```bash
docker compose -f docker-compose.dev.yml --env-file dev.env logs -f filestreambot-dev
```
Look for:
```
[FileStream] Telegram Bot started as @YourDevBot
[FileStream] Web server listening on 0.0.0.0 (port(s): 8081)
```

### 3. Verify Web Application
Open your browser or run curl:
```bash
curl http://localhost:8081/
```
Or open in your browser:
```
http://<YOUR_VPS_IP>:8081/
```

### 4. Verify Telegram Bot
Open your development bot on Telegram and send:
```
/start
```
Confirm the bot responds and functions independently from production.

---

## 5. Daily Development Workflow & Commands

| Action | Safe Command |
| :--- | :--- |
| **Start / Run Dev** | `docker compose -f docker-compose.dev.yml --env-file dev.env up -d` |
| **Rebuild Dev** | `docker compose -f docker-compose.dev.yml --env-file dev.env up -d --build` |
| **View Live Logs** | `docker compose -f docker-compose.dev.yml --env-file dev.env logs -f` |
| **Restart Dev** | `docker compose -f docker-compose.dev.yml --env-file dev.env restart` |
| **Stop Dev ONLY** | `docker compose -f docker-compose.dev.yml --env-file dev.env down` |
| **Inspect Containers**| `docker compose -f docker-compose.dev.yml --env-file dev.env ps` |

---

## 6. Critical Production Safety Rules

> [!CAUTION]
> **To protect the live production environment:**
> 1. **ALWAYS** specify `-f docker-compose.dev.yml --env-file dev.env` when executing dev commands.
> 2. **NEVER** run bare `docker compose down` (this stops the production environment).
> 3. **NEVER** run `docker volume prune` or `docker system prune -a` on the production VPS.
> 4. **NEVER** paste production bot tokens or production MongoDB URIs into `dev.env`.
