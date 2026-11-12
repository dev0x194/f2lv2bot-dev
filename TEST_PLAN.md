# COMPREHENSIVE TEST PLAN & REGRESSION MATRIX

**Author:** Agent 6 — Quality Assurance & Testing Specialist  
**Project:** File To Link V2 (`FileStream`)  
**Date:** October 2026  
**Status:** Certified Test Plan  

---

## 1. Scope & Verification Strategy

The testing strategy validates the end-to-end media delivery lifecycle across both backend streaming endpoints and frontend browser components.

### Quality Gates:
1. **Existing Test Suite:** All 506 existing automated tests in `tests/` must pass without regressions.
2. **New Regression Tests:** Comprehensive test suite in `tests/test_vidstack_player_modernization.py` covering player lifecycle, source resolution, mobile accessibility, codec compatibility, and network fault tolerance.
3. **Static & Syntax Validation:** Strict syntax and linting checks across Python and JavaScript.

---

## 2. Test Matrix

| Area | Test ID | Description | Expected Outcome | Verification Method |
| :--- | :--- | :--- | :--- | :--- |
| **Backend** | `BE-01` | Valid progressive stream request (`/stream/{token}`) | HTTP 200 OK, `Accept-Ranges: bytes`, proper Content-Type & Length | Automated pytest |
| **Backend** | `BE-02` | Valid byte-range request (`Range: bytes=0-1023`) | HTTP 206 Partial Content, `Content-Range: bytes 0-1023/{total}` | Automated pytest |
| **Backend** | `BE-03` | Out-of-bounds byte-range request (`Range: bytes=999999999-`) | HTTP 416 Range Not Satisfiable, `Content-Range: bytes */{total}` | Automated pytest |
| **Backend** | `BE-04` | Malformed range header (`Range: invalid_header`) | HTTP 416 Range Not Satisfiable | Automated pytest |
| **Backend** | `BE-05` | Invalid or expired stream token | HTTP 403 Forbidden with `STREAM_EXPIRED` message | Automated pytest |
| **Backend** | `BE-06` | Missing session cookie on stream token endpoint | HTTP 403 Forbidden | Automated pytest |
| **Backend** | `BE-07` | HLS Master Playlist generation (`/hls/{token}/master.m3u8`) | HTTP 200 OK, `application/vnd.apple.mpegurl`, contains quality variants | Automated pytest |
| **Backend** | `BE-08` | HLS Media Playlist VOD generation (`/hls/{token}/720p/index.m3u8`)| HTTP 200 OK, `#EXT-X-PLAYLIST-TYPE:VOD`, contains `#EXTINF` segments | Automated pytest |
| **Backend** | `BE-09` | Thumbnail VTT request (`/thumbnails/{token}/thumbnails.vtt`) | HTTP 200 OK, `text/vtt`, valid WEBVTT format | Automated pytest |
| **Backend** | `BE-10` | Non-blocking poster endpoint (`/thumb/{public_id}`) | HTTP 200 (if cached/extracted) or HTTP 404 with no 500 crash | Automated pytest |
| **Frontend** | `FE-01` | Template renders Vidstack Web Components `<media-player>` | HTML contains `<media-player>`, `<media-provider>`, `<media-video-layout>` | Automated pytest |
| **Frontend** | `FE-02` | Audio layout rendered for audio media (`<media-audio-layout>`) | HTML contains `<media-audio-layout>`, excludes `<media-video-layout>` | Automated pytest |
| **Frontend** | `FE-03` | Pinned Vidstack dependency in HTML (`v1.15.6`) | `<script src="https://cdn.vidstack.io/player@1.15.6">` present | Automated pytest |
| **Frontend** | `FE-04` | Plyr legacy assets excluded from active playback DOM | Neither `/static/plyr.js` nor `/static/plyr.css` in `play.html` | Automated pytest |
| **Frontend** | `FE-05` | Dedicated `player.js` loaded in `play.html` | `<script src="/static/player.js?v=...">` present | Automated pytest |
| **Frontend** | `FE-06` | State Machine initializes in `IDLE` / `LOADING` without infinite spinner | Status overlay dismisses on `can-play` / `loaded-metadata` | Automated pytest / JS unit |
| **Frontend** | `FE-07` | Source Resolver chooses direct MP4 for native video | Returns direct stream type for MP4/WebM | Automated JS test |
| **Frontend** | `FE-08` | Source Resolver chooses HLS for MKV/HEVC | Returns HLS stream type for MKV/HEVC | Automated JS test |
| **Frontend** | `FE-09` | Watchdog timeout (15s) triggers user-friendly retry overlay | Transitions to `ERROR` state with retry button | Automated JS test |
| **Frontend** | `FE-10` | Mobile touch targets enforce $\ge 44\text{px}$ | All player controls conform to minimum touch target sizing | Automated CSS test |

---

## 3. Regression Execution Commands

```bash
# 1. Run all existing tests to ensure no regressions
pytest tests/ -v

# 2. Run new dedicated Vidstack player and streaming tests
pytest tests/test_vidstack_player_modernization.py -v

# 3. Syntax & lint checks
python3 -m py_compile FileStream/server/stream_routes.py
python3 -m py_compile FileStream/server/render_template.py
node -c FileStream/web/static/player.js FileStream/web/static/app.js
```

---

**Report Approved by:** Agent 6 (Quality Assurance & Testing Specialist)
