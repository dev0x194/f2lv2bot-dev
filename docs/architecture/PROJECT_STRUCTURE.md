# FileToLinkV2bot — Canonical Architecture & Project Structure Guide

**Version:** 2.0.0 (Post-Reorganization Canonical Baseline)
**Status:** Validated Production Architecture
**Test Suite:** 449 / 449 Passing (100% Pass Rate)

---

## 1. Directory Tree & Canonical Organization

```
FileToLinkV2bot/
├── FileStream/
│   ├── __init__.py                     # Package initialization, __version__, StartTime
│   ├── __main__.py                     # Production startup entrypoint (Dockerfile / Procfile)
│   ├── main.py                         # Backward-compatible CLI entrypoint proxy
│   ├── config.py                       # Configuration parsing, dataclasses, validation
│   │
│   ├── bot/                            # Telegram Bot Tier
│   │   ├── __init__.py                 # Primary bot client instance configuration
│   │   ├── messages.py                 # Single source of truth for all bot messages & strings
│   │   ├── clients/                    # Multi-client management
│   │   │   ├── __init__.py             # Package init aliasing manager module
│   │   │   └── manager.py              # Secondary worker pool initialization (MULTI_TOKEN1..20)
│   │   ├── handlers/                   # Grouped Pyrogram command & event handlers
│   │   │   ├── admin/                  # Admin commands & dashboard (admin.py)
│   │   │   ├── folder/                 # Folder mode & file management (folder_manager.py)
│   │   │   ├── media/                  # Media ingestion (stream.py, batch.py)
│   │   │   ├── system/                 # Callback routing & reports (callback.py, nsfw_report.py)
│   │   │   └── user/                   # User commands (start.py, settings.py, stats.py, donation.py, referral.py)
│   │   ├── helpers/                    # Bot-specific helper utilities
│   │   │   ├── bot_commands.py         # Bot command menu registration
│   │   │   ├── bot_utils.py            # User verification, ban checks, message rendering
│   │   │   ├── broadcast_helper.py     # Broadcast dispatch & rate limiting
│   │   │   ├── client_identity.py      # Client worker identity & routing resolution
│   │   │   ├── legal.py                # Legal & Terms text generation
│   │   │   └── translation.py          # Bot UI inline keyboard markup buttons (BUTTON)
│   │   └── plugins/                    # Minimal backward-compatibility facades for tests & shims
│   │
│   ├── core/                           # Core Infrastructure Tier
│   │   ├── media/                      # Media inspection, categorization, MP4 metadata
│   │   │   ├── category.py             # File extension to MIME & category classification
│   │   │   ├── custom_dl.py            # Download session & stream chunk handling
│   │   │   ├── file_properties.py      # Pyrogram message media attribute extraction
│   │   │   └── mp4_metadata.py         # MP4 fast-start (moov atom) analysis
│   │   ├── monitoring/                 # Health, metrics, and observability
│   │   │   ├── admin_audit.py          # Admin action logging & auditing
│   │   │   ├── alert_manager.py        # System alert dispatch
│   │   │   ├── audit_manager.py        # Generic event auditing
│   │   │   ├── log_reader.py           # Structured log tailing & filtering
│   │   │   ├── metrics.py              # In-memory performance & throughput metrics
│   │   │   ├── speedtest.py            # Network speed test integration
│   │   │   └── system_info.py          # Host CPU, memory, and disk diagnostics
│   │   ├── resilience/                 # Fault tolerance & load management
│   │   │   ├── circuit_breaker.py      # Circuit breaker pattern for external dependencies
│   │   │   ├── disaster_recovery.py    # Backup & state recovery helpers
│   │   │   ├── floodwait.py            # FloodWait handling & exponential backoff
│   │   │   ├── load_shedding.py        # Traffic shaping, security headers, rate limits
│   │   │   ├── testing_harness.py      # Chaos testing & injection harness
│   │   │   └── workload.py             # Workload tracker for concurrent requests
│   │   ├── runtime/                    # Worker coordination & caching
│   │   │   ├── client_balance.py       # Client load balancing & token assignment
│   │   │   ├── file_cleanup.py         # Periodic temp file & cache purger
│   │   │   ├── flog_channels.py        # FLOG channel assignment & round-robin routing
│   │   │   ├── flog_sync.py            # FLOG channel synchronization & reconciliation
│   │   │   ├── human_readable.py       # Human-readable byte size formatting (humanbytes)
│   │   │   ├── optional_channels.py    # Optional auxiliary channel routing
│   │   │   ├── public_links.py         # Public URL generation & signing
│   │   │   ├── runtime_cache.py        # Fast in-memory TTL cache
│   │   │   ├── stream_cache.py         # On-disk streaming chunk cache
│   │   │   ├── time_format.py          # Time duration formatting (get_readable_time)
│   │   │   └── upload_progress.py      # Upload progress tracker
│   │   └── security/                   # Content safety
│   │       ├── load_shedding.py        # Backward-compat shim -> core.resilience.load_shedding
│   │       └── nsfw.py                 # NudeNet adult content detection & moderation
│   │
│   ├── database/                       # Persistence Tier
│   │   ├── __init__.py                 # Database singleton re-export
│   │   ├── database.py                 # Async Motor MongoDB client, collections, indexing
│   │   └── storage_jobs.py             # Asynchronous storage job queue
│   │
│   ├── server/                         # Web Serving Tier
│   │   ├── __init__.py                 # aiohttp Application factory & middleware setup
│   │   ├── exceptions.py               # HTTP custom exceptions (FileNotFound, InvalidToken)
│   │   ├── render_template.py          # Jinja2 template rendering engine with dual path resolution
│   │   └── stream_routes.py            # Web routes: /watch, /dl, /folder, /stream, /api
│   │
│   ├── services/                       # Business Logic Tier
│   │   ├── analytics.py                # Telemetry & event tracking service
│   │   ├── experiments.py              # Feature flags & A/B testing
│   │   ├── file_service.py             # File ingestion, deduplication, and persistence
│   │   ├── folder_service.py           # Multi-file folder sessions & management
│   │   ├── hls_service.py              # On-the-fly HLS m3u8 playlist generation
│   │   ├── link_service.py             # Token generation, validation, and link signing
│   │   ├── notifications.py            # User notification dispatch
│   │   ├── shortener_service.py        # Third-party link shortener integration
│   │   ├── storage_service.py          # Primary & secondary FLOG storage router
│   │   ├── thumbnail_service.py        # Video thumbnail extraction & caching
│   │   ├── token_service.py            # Temporary access token service
│   │   └── user_service.py             # User profile, ban state, and settings service
│   │
│   ├── web/                            # Presentation Tier
│   │   ├── static/                     # CSS stylesheets, JavaScript bundles, player assets
│   │   └── templates/                  # Jinja2 HTML templates (landing, play, dl, folder, legal)
│   │
│   └── utils/                          # Intentional Backward-Compatibility Shims
│       └── [37 shims]                  # Re-exports canonical core.* and bot.helpers.* symbols
│
├── assets/                             # Static Assets
│   └── bot/                            # Telegram UI banners & help images
├── benchmarks/                         # Benchmark Harness & Baselines
│   ├── data/                           # Baseline JSON benchmark reports
│   └── scripts/                        # Benchmark runner (run_benchmark.py)
├── docs/                               # Project Documentation
│   ├── architecture/                   # Architectural audits & canonical structure
│   ├── development/                    # Deployment & development guides
│   ├── reports/                        # Forensic analysis & validation reports
│   └── workflows/                      # Bot workflow specifications
├── tests/                              # Pytest Test Suite (449 tests)
├── Dockerfile                          # Production container specification (Python 3.11-slim)
├── docker-compose.yml                  # Production compose with Traefik reverse proxy
├── docker-compose.dev.yml              # Local development compose configuration
├── Procfile                            # Heroku / Dokploy entrypoint definition
├── app.json                            # Cloud deployment manifest
├── pyproject.toml                      # Project metadata & pytest configuration
└── requirements.txt                    # Pinned Python package dependencies
```

---

## 2. Architectural Layer Hierarchy

To ensure high maintainability, strict separation of concerns, and clean dependency flow, `FileToLinkV2bot` enforces the following unidirectional architectural dependency model:

```
[Presentation Tier: FileStream/web/]
             │
             ▼
[Web Server Tier: FileStream/server/]  ◄────►  [Bot Layer: FileStream/bot/handlers/]
             │                                                │
             └───────────────────────┬────────────────────────┘
                                     ▼
                        [Business Services Tier: FileStream/services/]
                                     │
                                     ▼
                        [Persistence Tier: FileStream/database/]
                                     │
                                     ▼
                        [Core Infrastructure Tier: FileStream/core/]
```

### Architectural Rules
1. **Presentation (`FileStream/web/`)**: Contains only HTML templates and static assets (CSS/JS). No business logic.
2. **Web Server (`FileStream/server/`)**: Handles incoming HTTP requests, session validation, range queries, chunk streaming, and HTML rendering. Calls `FileStream.services` for business operations.
3. **Bot Layer (`FileStream/bot/`)**: Contains Telegram bot clients, grouped handlers (`FileStream.bot.handlers.*`), and helper functions (`FileStream.bot.helpers.*`). Handlers coordinate user commands and call `FileStream.services`.
4. **Services Layer (`FileStream/services/`)**: Encapsulates all domain logic (file management, folder sessions, token generation, storage orchestration). Never directly imports bot handlers.
5. **Database Layer (`FileStream/database/`)**: Encapsulates MongoDB collections, queries, indexes, and persistence queues.
6. **Core Infrastructure (`FileStream/core/`)**: Low-level domain-agnostic capabilities:
   - `resilience/`: Circuit breaker, load shedding, floodwait, workload trackers.
   - `security/`: NSFW scanning and content verification.
   - `runtime/`: Caching, FLOG synchronization, and load balancing.
   - `monitoring/`: Auditing, metrics, and system diagnostics.
   - `media/`: MP4 atoms, category resolution, media properties.

---

## 3. Backward Compatibility & Symlinks Reference

To guarantee 100% backward compatibility for production environments, legacy plugins, and third-party integrations, the repository maintains intentional, lightweight compatibility layers:

### 3.1 Intentional Compatibility Shims (`FileStream/utils/`)
The modules in `FileStream/utils/` are clean 1-line to 3-line facades that forward all attributes to their canonical locations in `FileStream.core.*` and `FileStream.bot.helpers.*`. Existing external scripts or older plugins importing from `FileStream.utils` continue to function identically with zero degradation.

### 3.2 Legacy Plugins Shims (`FileStream/bot/plugins/`)
The Pyrogram bot client's canonical plugins configuration is set to `FileStream.bot.handlers`. The `FileStream/bot/plugins/` directory contains backward-compatibility facades aliasing `FileStream.bot.handlers.*`, ensuring that any unit test or external monkeypatch patching `FileStream.bot.plugins.*` behaves identically.

### 3.3 Filesystem Compatibility Symlinks
1. **`images -> assets/bot`**: Retained because standard production configurations (`.env.example`, `dev.env.example`, `app.json`, `README.md`) default to `START_PIC="images/start.png"` and `HELP_PIC="images/help.png"`.
2. **`FileStream/static -> web/static` & `FileStream/template -> web/templates`**: Retained as filesystem links. In addition, `render_template.py` and `server/__init__.py` dynamically check `web/templates` and `web/static` first with transparent fallback.
3. **`benchmark -> benchmarks`**: Retained as a directory symlink for legacy performance benchmark references.

---

## 4. Verification & Validation Summary

- **Total Unit & Integration Tests:** 449 passed, 0 failed.
- **Bytecode Compilation (`compileall`):** 100% clean compilation across all Python packages.
- **Circular Imports:** Verified 0 circular dependencies across all 77 modules.
- **Web Server Initialization:** 50 routes verified on startup dry-run.
- **Bot Initialization:** Handlers discovery and secondary worker pool verified on startup dry-run.
