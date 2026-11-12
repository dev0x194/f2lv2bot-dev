# FileToLinkV2bot — Migration Map (Old Path &rarr; New Path)

**Document Version:** 1.0.0
**Phase:** Phase 2 (Planning)

This document defines the deterministic mapping for every module, template, asset, and document moved during the repository reorganization.

---

## 1. Documentation & Reports

| Old Path | New Path | Rationale |
| :--- | :--- | :--- |
| `ADMIN_SYSTEM_COMPLETE_ANALYSIS.txt` | `docs/reports/ADMIN_SYSTEM_COMPLETE_ANALYSIS.txt` | Detailed architectural analysis of admin commands and permissions. |
| `DEV_DEPLOY_GUIDE.md` | `docs/development/DEV_DEPLOY_GUIDE.md` | Deployment guide for isolated local and VPS dev environment. |
| `FILE_TO_LINK_V2_COMPLETE_PROJECT_WORKFLOW.txt` | `docs/workflows/FILE_TO_LINK_V2_COMPLETE_PROJECT_WORKFLOW.txt` | End-to-end workflow documentation. |
| `V6_PRODUCTION_VALIDATION_REPORT.md` | `docs/reports/V6_PRODUCTION_VALIDATION_REPORT.md` | Production validation test suite audit report. |
| `V6_STARTUP_BUG_FIX_REPORT.md` | `docs/reports/V6_STARTUP_BUG_FIX_REPORT.md` | Typing evaluation & startup bug fix report. |

---

## 2. Benchmarks

| Old Path | New Path | Rationale |
| :--- | :--- | :--- |
| `benchmark/run_benchmark.py` | `benchmarks/scripts/run_benchmark.py` | Benchmark execution script. |
| `benchmark/folder_cancellation_benchmark.json` | `benchmarks/data/folder_cancellation_benchmark.json` | Benchmark execution data. |
| `benchmark/folder_mode_after.json` | `benchmarks/data/folder_mode_after.json` | Benchmark execution data. |
| `benchmark/folder_mode_baseline.json` | `benchmarks/data/folder_mode_baseline.json` | Benchmark execution data. |
| `benchmark/folder_mode_multi_client_after.json` | `benchmarks/data/folder_mode_multi_client_after.json` | Benchmark execution data. |
| `benchmark/folder_mode_single_client_benchmark.json` | `benchmarks/data/folder_mode_single_client_benchmark.json` | Benchmark execution data. |

*(A symlink `benchmark -> benchmarks` is preserved for backward compatibility).*

---

## 3. Assets

| Old Path | New Path | Rationale |
| :--- | :--- | :--- |
| `images/about.png` | `assets/bot/about.png` | Bot banner image for `/about`. |
| `images/help.png` | `assets/bot/help.png` | Bot banner image for `/help`. |
| `images/legal.png` | `assets/bot/legal.png` | Bot banner image for legal dialogs. |
| `images/privacy.png` | `assets/bot/privacy.png` | Bot banner image for `/privacy`. |
| `images/start.png` | `assets/bot/start.png` | Bot banner image for `/start`. |

*(A symlink `images -> assets/bot` and candidate fallback resolution in `bot_utils.py` and `config.py` are preserved).*

---

## 4. Web Presentation Tier (`FileStream/web/`)

| Old Path | New Path | Rationale |
| :--- | :--- | :--- |
| `FileStream/template/dl.html` | `FileStream/web/templates/dl.html` | Download landing template. |
| `FileStream/template/folder.html` | `FileStream/web/templates/folder.html` | Multi-file folder view template. |
| `FileStream/template/landing.html` | `FileStream/web/templates/landing.html` | Root landing page template. |
| `FileStream/template/legal.html` | `FileStream/web/templates/legal.html` | Terms & Privacy page template. |
| `FileStream/template/play.html` | `FileStream/web/templates/play.html` | Video & media player template. |
| `FileStream/static/app.js` | `FileStream/web/static/app.js` | Web application JavaScript. |
| `FileStream/static/hls.min.js` | `FileStream/web/static/hls.min.js` | HLS.js player library. |
| `FileStream/static/main.css` | `FileStream/web/static/main.css` | Global styling. |
| `FileStream/static/player.css` | `FileStream/web/static/player.css` | Media player UI styles. |
| `FileStream/static/plyr.css` | `FileStream/web/static/plyr.css` | Plyr player stylesheet. |
| `FileStream/static/plyr.js` | `FileStream/web/static/plyr.js` | Plyr player script. |
| `FileStream/static/tokens.css` | `FileStream/web/static/tokens.css` | Design token variables. |

*(Symlinks `FileStream/template -> FileStream/web/templates` and `FileStream/static -> FileStream/web/static` are preserved).*

---

## 5. Bot Handlers (`FileStream/bot/handlers/`)

| Old Path | New Path | Responsibility Category |
| :--- | :--- | :--- |
| `FileStream/bot/plugins/admin.py` | `FileStream/bot/handlers/admin/admin.py` | Admin commands & dashboards |
| `FileStream/bot/plugins/start.py` | `FileStream/bot/handlers/user/start.py` | User onboarding & help commands |
| `FileStream/bot/plugins/settings.py` | `FileStream/bot/handlers/user/settings.py` | User preferences & toggles |
| `FileStream/bot/plugins/referral.py` | `FileStream/bot/handlers/user/referral.py` | User referral management |
| `FileStream/bot/plugins/donation.py` | `FileStream/bot/handlers/user/donation.py` | Donations & payment prompts |
| `FileStream/bot/plugins/stats.py` | `FileStream/bot/handlers/user/stats.py` | User stats command |
| `FileStream/bot/plugins/stream.py` | `FileStream/bot/handlers/media/stream.py` | Single file uploads & links |
| `FileStream/bot/plugins/batch.py` | `FileStream/bot/handlers/media/batch.py` | Batch file forwarding & collections |
| `FileStream/bot/plugins/folder_manager.py` | `FileStream/bot/handlers/folder/folder_manager.py` | Folder browsing & mutations |
| `FileStream/bot/plugins/callback.py` | `FileStream/bot/handlers/system/callback.py` | Central callback query routing |
| `FileStream/bot/plugins/nsfw_report.py` | `FileStream/bot/handlers/system/nsfw_report.py` | Adult content reporting |

*(Compatibility facade modules remain in `FileStream/bot/plugins/` to guarantee that all test imports and monkey-patching succeed).*

---

## 6. Bot Clients & Helpers (`FileStream/bot/clients/` & `FileStream/bot/helpers/`)

| Old Path | New Path | Rationale |
| :--- | :--- | :--- |
| `FileStream/bot/clients.py` | `FileStream/bot/clients/manager.py` | Multi-client lifecycle & worker handler management |
| `FileStream/utils/bot_commands.py` | `FileStream/bot/helpers/bot_commands.py` | BotFather command menu registration |
| `FileStream/utils/bot_utils.py` | `FileStream/bot/helpers/bot_utils.py` | User verification, chat checks, photo resolution |
| `FileStream/utils/broadcast_helper.py` | `FileStream/bot/helpers/broadcast_helper.py` | Broadcast message sender with rate limiting |
| `FileStream/utils/client_identity.py` | `FileStream/bot/helpers/client_identity.py` | Bot username & ID resolution cache |
| `FileStream/utils/legal.py` | `FileStream/bot/helpers/legal.py` | Legal Terms of Service & Privacy text generation |

---

## 7. Core Infrastructure Layer (`FileStream/core/`)

### 7.1 Security (`FileStream/core/security/`)
| Old Path | New Path |
| :--- | :--- |
| `FileStream/utils/load_shedding.py` | `FileStream/core/security/load_shedding.py` |
| `FileStream/utils/nsfw.py` | `FileStream/core/security/nsfw.py` |

### 7.2 Resilience (`FileStream/core/resilience/`)
| Old Path | New Path |
| :--- | :--- |
| `FileStream/utils/circuit_breaker.py` | `FileStream/core/resilience/circuit_breaker.py` |
| `FileStream/utils/floodwait.py` | `FileStream/core/resilience/floodwait.py` |
| `FileStream/utils/workload.py` | `FileStream/core/resilience/workload.py` |
| `FileStream/utils/disaster_recovery.py` | `FileStream/core/resilience/disaster_recovery.py` |
| `FileStream/utils/testing_harness.py` | `FileStream/core/resilience/testing_harness.py` |

### 7.3 Monitoring (`FileStream/core/monitoring/`)
| Old Path | New Path |
| :--- | :--- |
| `FileStream/utils/metrics.py` | `FileStream/core/monitoring/metrics.py` |
| `FileStream/utils/alert_manager.py` | `FileStream/core/monitoring/alert_manager.py` |
| `FileStream/utils/audit_manager.py` | `FileStream/core/monitoring/audit_manager.py` |
| `FileStream/utils/admin_audit.py` | `FileStream/core/monitoring/admin_audit.py` |
| `FileStream/utils/log_reader.py` | `FileStream/core/monitoring/log_reader.py` |
| `FileStream/utils/system_info.py` | `FileStream/core/monitoring/system_info.py` |
| `FileStream/utils/speedtest.py` | `FileStream/core/monitoring/speedtest.py` |

### 7.4 Runtime Infrastructure (`FileStream/core/runtime/`)
| Old Path | New Path |
| :--- | :--- |
| `FileStream/utils/runtime_cache.py` | `FileStream/core/runtime/runtime_cache.py` |
| `FileStream/utils/stream_cache.py` | `FileStream/core/runtime/stream_cache.py` |
| `FileStream/utils/client_balance.py` | `FileStream/core/runtime/client_balance.py` |
| `FileStream/utils/flog_channels.py` | `FileStream/core/runtime/flog_channels.py` |
| `FileStream/utils/flog_sync.py` | `FileStream/core/runtime/flog_sync.py` |
| `FileStream/utils/file_cleanup.py` | `FileStream/core/runtime/file_cleanup.py` |
| `FileStream/utils/optional_channels.py` | `FileStream/core/runtime/optional_channels.py` |
| `FileStream/utils/public_links.py` | `FileStream/core/runtime/public_links.py` |
| `FileStream/utils/upload_progress.py` | `FileStream/core/runtime/upload_progress.py` |

### 7.5 Media Infrastructure (`FileStream/core/media/`)
| Old Path | New Path |
| :--- | :--- |
| `FileStream/utils/mp4_metadata.py` | `FileStream/core/media/mp4_metadata.py` |
| `FileStream/utils/custom_dl.py` | `FileStream/core/media/custom_dl.py` |
| `FileStream/utils/file_properties.py` | `FileStream/core/media/file_properties.py` |
| `FileStream/utils/category.py` | `FileStream/core/media/category.py` |

---

## 8. Generic Utilities Layer (`FileStream/utils/`)

Only genuinely generic helpers are retained:
- `FileStream/utils/human_readable.py`
- `FileStream/utils/time_format.py`
- `FileStream/utils/translation.py`

*(All other modules in `FileStream/utils/` will serve as backwards-compatibility facade modules re-exporting from `FileStream.core.*` and `FileStream.bot.helpers.*`)*.
