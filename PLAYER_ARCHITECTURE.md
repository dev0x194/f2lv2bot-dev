# PLAYER ARCHITECTURE: VIDSTACK INTEGRATION & FRONTEND LIFECYCLE

**Author:** Agent 3 — Frontend & Vidstack Integration Specialist  
**Project:** File To Link V2 (`FileStream`)  
**Date:** October 2026  
**Status:** Certified Frontend Player Architecture  

---

## 1. Primary Objectives & Player Modernization

The objective is to replace the fragmented, brittle frontend player code (Plyr legacy residue, raw video elements, and untyped DOM handlers) with a unified, state-machine driven **Vidstack Player** architecture (pinned version `1.15.6`).

### Key Design Goals:
1. **Zero Indefinite Loading:** Eliminate the generic, infinite `"Connecting Secure Edge Stream…"` overlay. Replace with deterministic status messaging (e.g. "Connecting...", "Preparing stream...", "Buffering...") backed by a 15-second watchdog timer with clear user recovery actions.
2. **Deterministic State Machine:** Single source of truth for player states: `IDLE`, `LOADING`, `READY`, `PLAYING`, `PAUSED`, `BUFFERING`, `STALLED`, `RECONNECTING`, `ERROR`, `ENDED`.
3. **Smart Source Resolution:** Clean abstraction that inspects container/mime/codec information and tests actual browser support before binding sources.
4. **Clean Code Separation:** Move all player logic out of `play.html` into a dedicated, modular static asset: `/static/player.js` with styling in `/static/player.css`.
5. **Mobile & Accessibility First:** Minimum 44px touch targets, orientation-aware mobile fullscreen, keyboard navigation, and picture-in-picture (PiP).

---

## 2. Vidstack Architecture & Component Hierarchy

We select **Vidstack Player v1.15.6** (Web Components) pinned to prevent breaking CDN shifts:

```html
<!-- Pinned Vidstack Stylesheets & Scripts -->
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/vidstack@1.15.6/player/styles/default/theme.css" />
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/vidstack@1.15.6/player/styles/default/layouts/video.css" />
{% if is_audio %}
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/vidstack@1.15.6/player/styles/default/layouts/audio.css" />
{% endif %}
<link rel="stylesheet" href="/static/player.css?v={{ version }}" />

<script type="module" src="https://cdn.vidstack.io/player@1.15.6"></script>
<script src="/static/app.js?v={{ version }}"></script>
<script src="/static/player.js?v={{ version }}"></script>
```

### Component Structure in DOM:
```html
<section class="fs-player-container" id="playerView" aria-label="Media Player Section">
  <!-- Top Navigation & Info Header -->
  <div class="fs-player-header">
    <button id="backBtn" class="fs-btn fs-btn-ghost" type="button" aria-label="Back to Overview">
      <i data-lucide="arrow-left" aria-hidden="true"></i>
      <span>Back</span>
    </button>
    <div class="fs-player-meta">
      <h2 class="fs-player-title" title="{{ file_name }}">{{ file_name }}</h2>
    </div>
  </div>

  <!-- Player Shell -->
  <div class="fs-player-shell">
    <!-- Non-blocking Status & Error Indicator (never traps controls or hides poster) -->
    <div id="playerStatusOverlay" class="fs-player-overlay hidden" role="status" aria-live="polite">
      <div id="playerStatusSpinner" class="fs-player-spinner"></div>
      <div id="playerStatusMessage" class="fs-player-status-text">Preparing media...</div>
      <div id="playerStatusSub" class="fs-player-status-sub"></div>
      <button id="playerRetryBtn" class="fs-btn fs-btn-sm fs-btn-outline hidden" type="button">
        Try Again
      </button>
    </div>

    <!-- Vidstack Custom Elements -->
    {% if is_audio %}
    <media-player id="vidstackPlayer" title="{{ file_name }}" playsinline class="w-full">
      <media-provider></media-provider>
      <media-audio-layout id="vidstackLayout"></media-audio-layout>
    </media-player>
    {% else %}
    <media-player id="vidstackPlayer" title="{{ file_name }}" playsinline class="w-full aspect-video">
      <media-provider>
        <media-poster id="vidstackPoster" src="/thumb/{{ public_id }}" alt="{{ file_name }}"></media-poster>
      </media-provider>
      <media-video-layout id="vidstackLayout"></media-video-layout>
    </media-player>
    {% endif %}
  </div>
</section>
```

---

## 3. Finite State Machine (FSM) Lifecycle

```
    ┌──────────┐
    │   IDLE   │
    └────┬─────┘
         │ startStreaming()
         ▼
    ┌──────────┐      timeout (>15s) / HTTP 4xx,5xx
    │ LOADING  ├──────────────────────────────────────┐
    └────┬─────┘                                      │
         │ can-play / loaded-metadata                 ▼
         ▼                                      ┌───────────┐
    ┌──────────┐   play()                       │   ERROR   │
    │  READY   ├───────────────┐                └─────┬─────┘
    └────┬─────┘               │                      │
         │                     ▼                      │ retry()
         │               ┌───────────┐                │
         │               │  PLAYING  │◄───────────────┘
         │               └─────┬─────┘
         │                     │ waiting / stalling
         │                     ▼
         │               ┌───────────┐
         │               │ BUFFERING │
         │               └─────┬─────┘
         │                     │ can-play
         │                     ▼
         │               ┌───────────┐
         │               │  PLAYING  │
         │               └─────┬─────┘
         │                     │ ended
         │                     ▼
         │               ┌───────────┐
         └──────────────►│   ENDED   │
                         └───────────┘
```

### State Definitions & Behavior:
* `IDLE`: Clean reset. Player src unhooked, abort controllers cleared, overlays hidden.
* `LOADING`: Fetching stream token and initializing media source. Status displayed: *"Connecting to media stream..."*. 15-second watchdog timer armed.
* `READY`: Metadata loaded, first frame/poster prepared. Controls active. Watchdog timer disarmed. Status overlay dismissed.
* `PLAYING`: Playback active. Resumes watch progress if previous timestamp exists. Overlays dismissed.
* `BUFFERING` / `STALLED`: Network waiting. Subdued non-blocking indicator displayed; controls remain interactive.
* `ERROR`: Specific error displayed (e.g. "Stream token expired", "Browser cannot decode HEVC", "Network interrupted"). A prominent **Try Again** button and fallback **Direct Download** button are presented.
* `ENDED`: Playback finished. Watch progress cleared.

---

## 4. Source Resolution Layer (`resolveMediaSource`)

The resolver accepts the raw server media descriptor and evaluates browser compatibility:

```javascript
export function resolveMediaSource(data, options = {}) {
  const ext = (options.fileName || '').split('.').pop().toLowerCase();
  const mimeType = (options.mimeType || '').toLowerCase();
  const isVideo = !options.isAudio;

  // 1. Direct Progressive Playback (MP4 / WebM with native browser capability)
  if (data.is_native_playable && data.stream_url) {
    return {
      type: 'direct',
      src: data.stream_url,
      mimeType: mimeType || 'video/mp4',
      provider: 'video',
      transcoded: false,
    };
  }

  // 2. Adaptive HLS (Preferred for MKV, HEVC, and multi-bitrate streams)
  if (data.hls_url) {
    const isHlsSupported = Boolean(
      window.MediaSource ||
      (document.createElement('video').canPlayType('application/vnd.apple.mpegurl'))
    );
    if (isHlsSupported) {
      return {
        type: 'hls',
        src: data.hls_url,
        mimeType: 'application/x-mpegurl',
        provider: 'hls',
        transcoded: true,
      };
    }
  }

  // 3. Fallback to Direct Stream if HLS is unavailable
  if (data.stream_url) {
    return {
      type: 'direct',
      src: data.stream_url,
      mimeType: mimeType || 'video/mp4',
      provider: isVideo ? 'video' : 'audio',
      transcoded: false,
    };
  }

  return null;
}
```

---

## 5. Mobile & Fullscreen UX Specifications

1. **Orientation & Fullscreen:**
   * Vidstack automatically requests native fullscreen on iOS (`webkitEnterFullscreen`) for `<video>` elements while maintaining responsive controls on Android/Desktop.
2. **Touch Targets:**
   * All clickable buttons, scrubber handles, and menu items enforce a minimum bounding box of **$44\text{px} \times 44\text{px}$** via CSS rules.
3. **Viewport & Safe Areas:**
   * Container uses `max-width: 100vw; overflow-x: hidden;` and respects `env(safe-area-inset-top)` and `env(safe-area-inset-bottom)`.
4. **Picture-in-Picture (PiP):**
   * Accessible via keyboard shortcut `P` or toolbar button when supported by browser.

---

**Report Approved by:** Agent 3 (Frontend & Vidstack Specialist)
