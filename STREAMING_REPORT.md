# STREAMING REPORT: BACKEND STREAMING, RANGE REQUESTS & HLS PIPELINE

**Author:** Agent 2 — Streaming & Backend Specialist  
**Project:** File To Link V2 (`FileStream`)  
**Date:** October 2026  
**Status:** Certified Backend Streaming Analysis  

---

## 1. Backend Route & Streaming Endpoints Audit

We conducted an exhaustive audit of `FileStream/server/stream_routes.py`, `hls_service.py`, `mp4_metadata.py`, and `thumbnail_service.py`.

### 1.1 Endpoint Contracts & Behavior

| Endpoint | Method | Auth / Token Requirement | Response Type & Status | Range Support | Caching Headers |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `GET /gen/{public_id}` | GET, HEAD | Public ID lookup in DB; session cookie issued (`fs_page_session`) | `text/html` (200, 404, 503) | N/A | Default / session cookie |
| `GET /get-stream-token/{path}` | GET | Valid `page_token` (X-Page-Token header or query param) matching path & session | `application/json` (200, 403, 404, 500) | N/A | No-cache |
| `GET /get-download-token/{path}`| GET | Valid `page_token` matching path & session | `application/json` (200, 403, 404, 500) | N/A | No-cache |
| `GET /stream/{token}` | GET, HEAD | Valid `stream` token + session cookie validation | `media_streamer` (200, 206, 416) | `bytes` (Accept-Ranges, Content-Range) | Inline disposition, dynamic headers |
| `GET /file/{token}` | GET, HEAD | Valid `download` token + session cookie validation | `media_streamer` (200, 206, 416) | `bytes` (Accept-Ranges, Content-Range) | Attachment disposition |
| `GET /hls/{token}/master.m3u8` | GET, HEAD | Valid `stream` token + session cookie | `application/vnd.apple.mpegurl` (200, 403, 500) | N/A | `no-cache, no-store, must-revalidate` |
| `GET /hls/{token}/{quality}/index.m3u8` | GET, HEAD | Valid `stream` token + session cookie | `application/vnd.apple.mpegurl` (200, 403, 404, 503) | N/A | `no-cache, no-store, must-revalidate` |
| `GET /hls/{token}/{quality}/seg_{id}.ts` | GET, HEAD | Valid `stream` token + session cookie | `video/MP2T` (200, 404, 503) | N/A | `public, max-age=3600` |
| `GET /thumbnails/{token}/thumbnails.vtt` | GET, HEAD | Valid `stream` token + session cookie | `text/vtt` (200, 403) | N/A | `public, max-age=86400` |
| `GET /thumbnails/{token}/sprites.webp` | GET, HEAD | Valid `stream` token + session cookie | `image/webp` (200, 403, 404) | N/A | `public, max-age=86400` |
| `GET /thumb/{path}` | GET, HEAD | Public ID lookup | `image/jpeg` (200, 404) | N/A | `public, max-age=86400` |

---

## 2. HTTP Byte-Range Streaming Semantics (`media_streamer`)

The core streaming engine in `media_streamer` implements RFC 7233 byte-range specifications:

### 2.1 Range Header Parsing (`_parse_range`)
* Correctly parses `bytes=start-end`, `bytes=start-`, and suffix ranges `bytes=-suffix_len`.
* Boundary validation:
  - If `start < 0` or `end >= file_size` or `start > end`, returns **HTTP 416 Range Not Satisfiable** with `Content-Range: bytes */{file_size}`.
  - If no Range header is supplied, serves from byte `0` to `file_size - 1` with **HTTP 200 OK**.
  - If valid Range is supplied, responds with **HTTP 206 Partial Content**, `Content-Range: bytes {from_bytes}-{until_bytes}/{file_size}`, and `Accept-Ranges: bytes`.

### 2.2 Streaming Memory Safety (No Full-File Buffering)
* Chunks are served via `tg_connect.yield_file(internal_db_id, file_id, index, offset, first_part_cut, last_part_cut, part_count, chunk_size)`.
* Chunk size is clamped to `min(STREAM_CHUNK_SIZE_MB * 1024 * 1024, 1024 * 1024)` (1 MiB max per Telegram `upload.GetFile` MTProto constraint).
* Memory usage per stream is $O(\text{chunk\_size})$ (approx. 1 MiB), regardless of whether the file is 50 MiB or 4 GiB.
* `TCP_NODELAY` is enabled on the underlying socket transport to prevent Nagle's algorithm from buffering small frame packets.

### 2.3 MP4 Fast-Scrub & Virtual MOOV Atom Relocation
* For MP4 containers where the `moov` atom is positioned at the tail of the file (preventing standard browser playback until the whole file is downloaded), `mp4_metadata_manager` parses the `moov` atom from the end of the file in the background, patches chunk offset tables (`stco` and `co64`), and virtualizes the stream so the browser receives the `moov` atom at byte 0.
* In-memory LRU caching preserves indexed `moov` metadata across seek requests.

---

## 3. Dynamic HLS Transcoding & Remuxing (`HLSService`)

### 3.1 Adaptive Bitrate Ladder
The service defines three quality variants in `QUALITIES`:
* `1080p`: 3500 kbps (video: 3000k, audio: 128k, scale: `-2:1080`)
* `720p`: 1800 kbps (video: 1500k, audio: 128k, scale: `-2:720`)
* `480p`: 800 kbps (video: 700k, audio: 96k, scale: `-2:480`)

### 3.2 Codec Passthrough vs Transcode
* When the source video stream is H.264 (`avc1`) and audio is AAC, `can_copy` is enabled for 1080p (`-c:v copy -c:a copy`), requiring virtually zero CPU transcoding overhead.
* When the source video is HEVC/H.265 (such as `The.Last.Ship...x265.HEVC-PSA.mkv`), FFmpeg encodes to `libx264` with `-preset ultrafast` and `-crf 23`.
* Segments are generated on demand via `ensure_segment(segment_id)` and cached in `STREAM_CACHE_DIR/hls/{file_id}/{quality}/seg_{segment_id}.ts`.

### 3.3 Identified Backend HLS Latency & Improvements
1. **Startup Race Condition Mitigated:** `hls_media_playlist_handler` generates a full VOD playlist (`generate_vod_playlist`) upfront when duration is known, allowing the player to schedule segment fetches immediately without waiting for the full video to transcode.
2. **Predictive Segment Pre-fetching:** When `seg_{n}.ts` completes, `ensure_segment` schedules `seg_{n+1}.ts` in the background asynchronously, ensuring smooth playback without buffer stalls.
3. **Reaper Safeguards:** Idle HLS jobs and orphaned processes are terminated after `HLS_IDLE_TIMEOUT` (default 600s), and segment disk cache is bounded by LRU pruning.

---

## 4. Client Disconnect & Socket Abort Behavior

* In `media_streamer`, when a client disconnects (user navigates away, pauses, or seeks):
  - `aiohttp` raises `ConnectionResetError` or `asyncio.CancelledError`.
  - The generator loop terminates cleanly, releasing the MTProto client slot back to `work_loads`.
* In `HLSService`, job cancellation terminates FFmpeg subprocesses gracefully with `SIGTERM`, escalating to `SIGKILL` after a 2.0s timeout if unyielding, ensuring no zombie processes linger on the host.

---

## 5. Security & Token Verification

* Stream tokens are 256-bit cryptographically random tokens (`secrets.token_urlsafe(32)`).
* Validated server-side against expected kind (`stream` vs `download` vs `page`).
* Tied to `session_id` stored in the HTTP-only `fs_page_session` cookie to prevent link stealing or cross-site scraping.
* No internal Telegram file IDs, Bot tokens, channel IDs, or filesystem paths are ever exposed in any response header or body.

---

**Report Approved by:** Agent 2 (Streaming & Backend Specialist)
