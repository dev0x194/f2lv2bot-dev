/**
 * File To Link V2 — Modern Vidstack & Native Media Player Engine
 * Architecture: Deterministic State Machine, Smart Capability Resolver, External Player Launchers, Safe Telemetry
 * Pinned Dependency: Vidstack Player v1.15.6 + Native HTML5 <video> / <audio>
 */
(function (global) {
  'use strict';

  // 1. Explicit Player State Machine Enum
  const PlayerState = Object.freeze({
    IDLE: 'IDLE',
    AUTHENTICATING: 'AUTHENTICATING',
    RESOLVING_SOURCE: 'RESOLVING_SOURCE',
    PREPARING_MEDIA: 'PREPARING_MEDIA',
    LOADING: 'LOADING',
    READY: 'READY',
    PLAYING: 'PLAYING',
    PAUSED: 'PAUSED',
    BUFFERING: 'BUFFERING',
    STALLED: 'STALLED',
    RECONNECTING: 'RECONNECTING',
    AUTH_FAILED: 'AUTH_FAILED',
    TOKEN_EXPIRED: 'TOKEN_EXPIRED',
    SOURCE_NOT_FOUND: 'SOURCE_NOT_FOUND',
    STREAM_UNAVAILABLE: 'STREAM_UNAVAILABLE',
    MEDIA_UNSUPPORTED: 'MEDIA_UNSUPPORTED',
    NETWORK_ERROR: 'NETWORK_ERROR',
    PLAYBACK_ERROR: 'PLAYBACK_ERROR',
    // Backward compatibility aliases
    UNSUPPORTED: 'MEDIA_UNSUPPORTED',
    AUTH_ERROR: 'AUTH_FAILED',
    ERROR: 'PLAYBACK_ERROR',
    ENDED: 'ENDED',
  });

  // Safe Redaction Helper for Development Diagnostics (Strict Security: No Tokens/Secrets)
  function _redact(val) {
    if (typeof val === 'string') {
      return val.replace(/([?&](?:page_token|token|stream_token)=)[^&]+/gi, '$1[REDACTED]')
                .replace(/(\/(?:stream|thumbnails|file)\/)[a-zA-Z0-9_\-\.]+/gi, '$1[REDACTED]');
    }
    if (val && typeof val === 'object') {
      const clean = Array.isArray(val) ? [] : {};
      for (const [k, v] of Object.entries(val)) {
        if (/token|cookie|secret|auth|pass|hash/i.test(k)) {
          clean[k] = '[REDACTED]';
        } else if (typeof v === 'string') {
          clean[k] = _redact(v);
        } else if (typeof v === 'object' && v !== null) {
          clean[k] = _redact(v);
        } else {
          clean[k] = v;
        }
      }
      return clean;
    }
    return val;
  }

  function _log(checkpoint, message, meta) {
    const prefix = `[FSPlayer] ${checkpoint}: ${message}`;
    if (meta !== undefined) {
      console.log(prefix, _redact(meta));
    } else {
      console.log(prefix);
    }
  }

  function getPlatform() {
    if (typeof navigator === 'undefined') return { isAndroid: false, isIOS: false, isDesktop: true };
    const ua = navigator.userAgent || '';
    const isAndroid = /Android/i.test(ua);
    const isIOS = /iPhone|iPad|iPod/i.test(ua);
    const isDesktop = !isAndroid && !isIOS;
    return { isAndroid, isIOS, isDesktop };
  }

  function buildVlcUri(streamUrl) {
    const platform = getPlatform();
    try {
      const parsed = new URL(streamUrl);
      if (platform.isAndroid) {
        const streamWithoutScheme = parsed.host + parsed.pathname + parsed.search;
        const scheme = parsed.protocol.replace(':', '');
        return `intent://${streamWithoutScheme}#Intent;package=org.videolan.vlc;type=video/*;scheme=${scheme};end;`;
      }
      if (platform.isIOS) {
        return `vlc-x-callback://x-callback-url/stream?url=${encodeURIComponent(streamUrl)}`;
      }
      return `vlc://${streamUrl}`;
    } catch (_) {
      return `vlc://${streamUrl}`;
    }
  }

  function buildExternalPlayerUri(streamUrl) {
    const platform = getPlatform();
    if (platform.isAndroid) {
      return `intent:${streamUrl}#Intent;action=android.intent.action.VIEW;type=video/*;end;`;
    }
    if (platform.isIOS) {
      return `infuse://open?url=${encodeURIComponent(streamUrl)}`;
    }
    return buildVlcUri(streamUrl);
  }

  async function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (_) {}
    }
    try {
      const textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.select();
      const successful = document.execCommand('copy');
      document.body.removeChild(textarea);
      return successful;
    } catch (_) {
      return false;
    }
  }

  // 2. Browser Capability Detection & Smart Media Source Resolver
  function checkBrowserPlaybackSupport(options = {}) {
    const fileName = String(options.fileName || '').toLowerCase();
    const rawMime = String(options.mimeType || '').toLowerCase();
    const videoCodec = String(options.videoCodec || '').toLowerCase();
    const audioCodec = String(options.audioCodec || '').toLowerCase();
    const isAudio = Boolean(options.isAudio || rawMime.startsWith('audio/'));
    const ext = fileName.split('.').pop() || '';

    if (typeof document === 'undefined') {
      return { supported: true, canPlay: 'maybe' };
    }

    const mediaEl = document.createElement(isAudio ? 'audio' : 'video');
    if (!mediaEl.canPlayType) {
      return {
        supported: false,
        reason: 'unsupported_browser',
        message: 'Your browser does not support HTML5 media playback.',
      };
    }

    const isMkv = ext === 'mkv' || rawMime.includes('matroska') || rawMime.includes('mkv');
    const isHevc = videoCodec.includes('hevc') || videoCodec.includes('265') ||
                   fileName.includes('hevc') || fileName.includes('x265') || fileName.includes('h265');
    const isAvi = ext === 'avi' || rawMime.includes('msvideo') || rawMime.includes('avi');
    const isWmv = ext === 'wmv' || rawMime.includes('wmv');
    const isFlv = ext === 'flv' || rawMime.includes('flv');

    // MKV format: Many modern Chromium and Safari builds demux and decode MKV natively via <video>
    // but report empty string for mediaEl.canPlayType('video/x-matroska').
    // Do NOT block MKV upfront. Return supported: true with canPlay: 'maybe'.
    // Real incompatibility is detected when the media engine emits a fatal error during playback.
    if (isMkv) {
      return {
        supported: true,
        canPlay: 'maybe',
        isMkv: true,
        isHevc: isHevc,
      };
    }

    if (isAvi || isWmv || isFlv) {
      const canPlayFormat = rawMime ? mediaEl.canPlayType(rawMime) : '';
      if (!canPlayFormat) {
        return {
          supported: false,
          formatBadge: `${ext.toUpperCase()} is not supported here`,
          reason: 'unsupported_container',
          title: 'Browser cannot play this format',
          message: `Browser playback is not supported for ${ext.toUpperCase()} files.`,
        };
      }
    }

    return { supported: true, canPlay: 'maybe' };
  }

  function resolveMediaSource(data, options = {}) {
    if (!data || typeof data !== 'object' || data.success === false) return null;

    const rawMime = String(options.mimeType || data.mime_type || '').toLowerCase();
    const isAudio = Boolean(options.isAudio || rawMime.startsWith('audio/'));
    const resolvedMime = rawMime || (isAudio ? 'audio/mpeg' : 'video/mp4');
    const fileName = options.fileName || data.file_name || '';
    const videoCodec = options.videoCodec || data.video_codec || '';
    const audioCodec = options.audioCodec || data.audio_codec || '';

    const capability = checkBrowserPlaybackSupport({
      fileName,
      mimeType: resolvedMime,
      videoCodec,
      audioCodec,
      isAudio,
    });

    console.log('[Vidstack] canPlay:', capability.supported ? 'yes' : 'no', capability.supported ? '' : `(${capability.reason || 'unsupported'})`);

    if (data.stream_url) {
      return {
        type: 'direct',
        src: data.stream_url,
        downloadUrl: data.download_url || null,
        mimeType: resolvedMime,
        title: fileName,
        duration: Number(data.file_duration || options.serverDuration || 0),
        thumbnails: data.thumbnails_url || null,
        isSupported: capability.supported,
        unsupportedReason: capability.message || '',
        unsupportedTitle: capability.title || 'Browser playback unavailable',
        formatBadge: capability.formatBadge || '',
        isMkv: Boolean(capability.isMkv),
        isHevc: Boolean(capability.isHevc),
      };
    }

    return null;
  }

  // 3. Controller Implementation
  class PlayerController {
    constructor(config = {}) {
      this.config = {
        publicId: config.publicId || config.fileId || '',
        shareUrl: config.shareUrl || '',
        downloadTokenPath: config.downloadTokenPath || '',
        streamTokenPath: config.streamTokenPath || '',
        pageToken: config.pageToken || '',
        smartlinkUrl: config.smartlinkUrl || '',
        fileName: config.fileName || 'Media File',
        fileSize: config.fileSize || '',
        serverDuration: Number(config.serverDuration || config.fileDuration) || 0,
        mimeType: config.mimeType || 'video/mp4',
        isAudio: Boolean(config.isAudio),
        isVideo: Boolean(config.isVideo),
        isPlayable: Boolean(config.isPlayable),
        playerElement: config.playerElement || null,
        layoutElement: config.layoutElement || null,
        onStateChange: typeof config.onStateChange === 'function' ? config.onStateChange : null,
        ...config,
      };

      this.state = PlayerState.IDLE;
      this.failsafeTimer = null;
      this.streamAbortController = null;
      this.cachedStreamData = null;
      this.streamTokenFetchTime = 0;
      this.hasResumed = false;
      this.activeSource = null;
      this.mediaRequestStarted = false;
      this.lastSavedProgress = 0;
      this.currentEpoch = Date.now();
      this.platform = getPlatform();

      this.watchdog = {
        arm: () => { _log('27', 'watchdog start'); },
        disarm: () => { _log('28', 'watchdog cancel'); },
      };
      this._currentStreamingPromise = null;

      _log('3', 'controller initialization', {
        publicId: this.config.publicId,
        fileName: this.config.fileName,
        isAudio: this.config.isAudio,
        isVideo: this.config.isVideo,
        duration: this.config.serverDuration,
      });

      this._cacheElements();
      this._bindEvents();
      this._initUI();
      console.log('[Vidstack] player initialized');
    }

    _getMediaDiagnostics() {
      const { playerEl } = this.dom;
      if (!playerEl) return { error: 'No player element in DOM' };
      const mediaEl = playerEl.querySelector('video, audio') || playerEl;

      const readyStates = ['0 (HAVE_NOTHING)', '1 (HAVE_METADATA)', '2 (HAVE_CURRENT_DATA)', '3 (HAVE_FUTURE_DATA)', '4 (HAVE_ENOUGH_DATA)'];
      const networkStates = ['0 (NETWORK_EMPTY)', '1 (NETWORK_IDLE)', '2 (NETWORK_LOADING)', '3 (NETWORK_NO_SOURCE)'];

      const bufferedRanges = [];
      try {
        if (mediaEl.buffered) {
          for (let i = 0; i < mediaEl.buffered.length; i++) {
            bufferedRanges.push({
              start: Number(mediaEl.buffered.start(i).toFixed(2)),
              end: Number(mediaEl.buffered.end(i).toFixed(2)),
            });
          }
        }
      } catch (_) {}

      const err = mediaEl.error;
      return {
        currentSrc: _redact(mediaEl.currentSrc || playerEl.src || ''),
        readyState: readyStates[mediaEl.readyState] || String(mediaEl.readyState),
        networkState: networkStates[mediaEl.networkState] || String(mediaEl.networkState),
        duration: mediaEl.duration || playerEl.duration || 0,
        currentTime: mediaEl.currentTime || playerEl.currentTime || 0,
        paused: mediaEl.paused,
        ended: mediaEl.ended,
        buffered: bufferedRanges,
        errorCode: err ? err.code : null,
        errorMessage: err ? (err.message || 'MediaError') : null,
      };
    }

    _log(checkpoint, message, meta) {
      _log(checkpoint, message, meta);
    }

    _cacheElements() {
      const cfg = this.config;
      const playerEl = cfg.playerElement ||
        document.getElementById('video-player') ||
        document.getElementById('player') ||
        document.getElementById('vidstackPlayer') ||
        document.querySelector('media-player') ||
        document.querySelector('video, audio');

      const layoutEl = cfg.layoutElement ||
        document.getElementById('vidstackLayout') ||
        (playerEl ? playerEl.querySelector('media-video-layout, media-audio-layout') : null) ||
        document.querySelector('media-video-layout, media-audio-layout');

      this.dom = {
        playerEl: playerEl,
        layoutEl: layoutEl,
        managerView: cfg.managerView || document.getElementById('managerView'),
        playerView: cfg.playerView || document.getElementById('playerView'),
        showcaseBox: cfg.showcaseBox || document.getElementById('showcaseBox'),
        posterImg: cfg.posterImg || document.getElementById('posterImg'),
        posterBg: cfg.posterBg || document.getElementById('posterBg'),
        posterPlaceholder: cfg.posterPlaceholder || document.getElementById('posterPlaceholder'),
        playBtn: cfg.playBtn || document.getElementById('playBtn'),
        downloadBtn: cfg.downloadBtn || document.getElementById('downloadBtn'),
        shareBtn: cfg.shareBtn || document.getElementById('shareBtn'),
        playerDownloadBtn: cfg.playerDownloadBtn || document.getElementById('playerDownloadBtn'),
        playerShareBtn: cfg.playerShareBtn || document.getElementById('playerShareBtn'),
        backBtn: cfg.backBtn || document.getElementById('backBtn'),
        playerOverlay: cfg.playerOverlay || document.getElementById('playerOverlay'),
        playerOverlaySpinner: cfg.playerOverlaySpinner || document.getElementById('playerOverlaySpinner'),
        playerOverlayIcon: cfg.playerOverlayIcon || document.getElementById('playerOverlayIcon'),
        playerOverlayText: cfg.playerOverlayText || document.getElementById('playerOverlayText'),
        playerOverlaySub: cfg.playerOverlaySub || document.getElementById('playerOverlaySub'),
        playerOverlayBadge: cfg.playerOverlayBadge || document.getElementById('playerOverlayBadge'),
        unsupportedBadgeText: cfg.unsupportedBadgeText || document.getElementById('unsupportedBadgeText'),
        playerOverlayActions: cfg.playerOverlayActions || document.getElementById('playerOverlayActions'),
        unsupportedActions: cfg.unsupportedActions || document.getElementById('unsupportedActions'),
        errorActions: cfg.errorActions || document.getElementById('errorActions'),
        unsupportedBox: cfg.unsupportedBox || document.getElementById('unsupportedBox'),
        errorBox: cfg.errorBox || document.getElementById('errorBox'),
        loadingBox: cfg.loadingBox || document.getElementById('loadingBox'),
        overlayVlcBtn: cfg.overlayVlcBtn || document.getElementById('overlayVlcBtn'),
        overlayExtBtn: cfg.overlayExtBtn || document.getElementById('overlayExtBtn'),
        overlayDownloadBtn: cfg.overlayDownloadBtn || document.getElementById('overlayDownloadBtn'),
        overlayCopyBtn: cfg.overlayCopyBtn || document.getElementById('overlayCopyBtn'),
        overlayNotice: cfg.overlayNotice || document.getElementById('overlayNotice'),
        overlayNoticeText: cfg.overlayNoticeText || document.getElementById('overlayNoticeText'),
        noticeCloseBtn: cfg.noticeCloseBtn || document.getElementById('noticeCloseBtn'),
        playerRetryBtn: cfg.playerRetryBtn || document.getElementById('playerRetryBtn'),
        fallbackDirectBtn: cfg.fallbackDirectBtn || document.getElementById('fallbackDirectBtn'),
        playerStatePill: cfg.playerStatePill || document.getElementById('playerStatePill'),
        playerStateText: cfg.playerStateText || document.getElementById('playerStateText'),
        playerSourceText: cfg.playerSourceText || document.getElementById('playerSourceText'),
        metaDuration: cfg.metaDuration || document.getElementById('metaDuration'),
        detectedQuality: cfg.detectedQuality || document.getElementById('detectedQuality'),
        detectedCodec: cfg.detectedCodec || document.getElementById('detectedCodec'),
        detectedAudio: cfg.detectedAudio || document.getElementById('detectedAudio'),
        vlcQuickBtn: cfg.vlcQuickBtn || document.getElementById('vlcQuickBtn'),
        extQuickBtn: cfg.extQuickBtn || document.getElementById('extQuickBtn'),
        playerVlcBtn: cfg.playerVlcBtn || document.getElementById('playerVlcBtn'),
        playerExtBtn: cfg.playerExtBtn || document.getElementById('playerExtBtn'),
        playerCopyUrlBtn: cfg.playerCopyUrlBtn || document.getElementById('playerCopyUrlBtn'),
      };
    }

    _bindEvents() {
      const {
        playBtn,
        showcaseBox,
        downloadBtn,
        shareBtn,
        playerDownloadBtn,
        playerShareBtn,
        backBtn,
        playerRetryBtn,
        fallbackDirectBtn,
        overlayVlcBtn,
        overlayExtBtn,
        overlayDownloadBtn,
        overlayCopyBtn,
        vlcQuickBtn,
        extQuickBtn,
        playerVlcBtn,
        playerExtBtn,
        playerCopyUrlBtn,
      } = this.dom;

      if (playBtn) playBtn.addEventListener('click', () => this.startStreaming());
      if (showcaseBox) showcaseBox.addEventListener('click', () => this.startStreaming());

      if (playerRetryBtn) {
        playerRetryBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          this.retry();
        });
      }
      if (fallbackDirectBtn) {
        fallbackDirectBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          this.triggerDownload(fallbackDirectBtn);
        });
      }
      if (overlayVlcBtn) {
        overlayVlcBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          this.openVLC();
        });
      }
      if (overlayExtBtn) {
        overlayExtBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          this.openExternalPlayer();
        });
      }
      if (overlayDownloadBtn) {
        overlayDownloadBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          this.triggerDownload(overlayDownloadBtn);
        });
      }
      if (overlayCopyBtn) {
        overlayCopyBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          this.copyStreamUrl();
        });
      }

      if (vlcQuickBtn) vlcQuickBtn.addEventListener('click', () => this.openVLC());
      if (extQuickBtn) extQuickBtn.addEventListener('click', () => this.openExternalPlayer());
      if (playerVlcBtn) playerVlcBtn.addEventListener('click', () => this.openVLC());
      if (playerExtBtn) playerExtBtn.addEventListener('click', () => this.openExternalPlayer());
      if (playerCopyUrlBtn) playerCopyUrlBtn.addEventListener('click', () => this.copyStreamUrl());

      if (downloadBtn) downloadBtn.addEventListener('click', () => this.triggerDownload(downloadBtn));
      if (playerDownloadBtn) playerDownloadBtn.addEventListener('click', () => this.triggerDownload(playerDownloadBtn));

      if (shareBtn) shareBtn.addEventListener('click', () => this.share());
      if (playerShareBtn) playerShareBtn.addEventListener('click', () => this.share());

      if (backBtn) backBtn.addEventListener('click', () => this.destroy());

      this._bindVidstackEvents();
    }

    _bindVidstackEvents() {
      const { playerEl } = this.dom;
      if (!playerEl || playerEl._fsEventsBound) return;
      playerEl._fsEventsBound = true;

      const on = (evt, fn) => playerEl.addEventListener(evt, fn);

      on('source-change', (e) => {
        _log('11', 'media-player sourcechange', e && e.detail ? { src: _redact(e.detail.src) } : undefined);
      });
      on('sourcechange', (e) => {
        _log('11', 'media-player sourcechange', e && e.detail ? { src: _redact(e.detail.src) } : undefined);
      });

      on('provider-change', (e) => {
        _log('12', 'provider change', e && e.detail ? { provider: e.detail.type || e.detail.name || typeof e.detail } : undefined);
        const mediaEl = playerEl.querySelector('video, audio');
        if (mediaEl) this._bindNativeMediaEvents(mediaEl);
      });

      on('load-start', () => _log('13', 'loadstart'));
      on('loadstart', () => _log('13', 'loadstart'));

      on('loaded-metadata', () => { _log('14', 'loadedmetadata'); this._onLoadedMetadata(); });
      on('loadedmetadata', () => { _log('14', 'loadedmetadata'); this._onLoadedMetadata(); });

      on('duration-change', () => _log('15', 'durationchange', { duration: playerEl.duration }));
      on('durationchange', () => _log('15', 'durationchange', { duration: playerEl.duration }));

      on('can-play', () => { _log('16', 'canplay'); this._onCanPlay(); });
      on('canplay', () => { _log('16', 'canplay'); this._onCanPlay(); });

      on('can-play-through', () => _log('17', 'canplaythrough'));
      on('canplaythrough', () => _log('17', 'canplaythrough'));

      on('playing', () => { _log('18', 'playing'); this._onPlaying(); });
      on('waiting', () => { _log('19', 'waiting'); this._onWaiting(); });
      on('stalled', () => _log('20', 'stalled'));
      on('suspend', () => _log('21', 'suspend'));

      on('error', (event) => { _log('22', 'error', { error: event }); this._onError(event); });

      on('abort', () => _log('23', 'abort'));
      on('emptied', () => _log('24', 'emptied'));
      on('progress', () => _log('25', 'progress'));

      on('time-update', () => { _log('26', 'timeupdate'); this._onTimeUpdate(); });
      on('timeupdate', () => { _log('26', 'timeupdate'); this._onTimeUpdate(); });

      on('seeking', () => this._onSeeking());
      on('seeked', () => this._onSeeked());

      on('pause', () => this._onPause());
      on('ended', () => this._onEnded());

      const innerMedia = playerEl.querySelector('video, audio');
      if (innerMedia) this._bindNativeMediaEvents(innerMedia);
    }

    _bindNativeMediaEvents(mediaEl) {
      if (!mediaEl || mediaEl._fsNativeEventsBound) return;
      mediaEl._fsNativeEventsBound = true;

      mediaEl.addEventListener('loadedmetadata', () => { _log('14', 'loadedmetadata'); this._onLoadedMetadata(); });
      mediaEl.addEventListener('canplay', () => { _log('16', 'canplay'); this._onCanPlay(); });
      mediaEl.addEventListener('playing', () => { _log('18', 'playing'); this._onPlaying(); });
      mediaEl.addEventListener('pause', () => this._onPause());
      mediaEl.addEventListener('waiting', () => { _log('19', 'waiting'); this._onWaiting(); });
      mediaEl.addEventListener('seeking', () => this._onSeeking());
      mediaEl.addEventListener('seeked', () => this._onSeeked());
      mediaEl.addEventListener('ended', () => this._onEnded());
      mediaEl.addEventListener('error', (e) => { _log('22', 'error', { error: e }); this._onError(e); });
      mediaEl.addEventListener('timeupdate', () => { _log('26', 'timeupdate'); this._onTimeUpdate(); });
    }

    _onSeeking() {
      if (this.state === PlayerState.PLAYING || this.state === PlayerState.PAUSED) {
        this.setState(PlayerState.BUFFERING, 'Seeking media stream…');
      }
    }

    _onSeeked() {
      const { playerEl } = this.dom;
      const media = (playerEl && playerEl.querySelector('video, audio')) || playerEl;
      const isPaused = media && (media.paused ?? true);
      this.setState(isPaused ? PlayerState.PAUSED : PlayerState.PLAYING);
    }

    _initUI() {
      if (this.config.serverDuration > 0) {
        this.updateDurationDisplay(this.config.serverDuration);
      }
      this._detectPillsFromName(this.config.fileName);
      this._initPoster();
      this._initPlatformUI();
    }

    _initPlatformUI() {
      const { extQuickBtn, playerExtBtn } = this.dom;
      if (this.platform.isIOS) {
        if (extQuickBtn && extQuickBtn.querySelector('span')) extQuickBtn.querySelector('span').textContent = 'Open in Infuse';
        if (playerExtBtn && playerExtBtn.querySelector('span')) playerExtBtn.querySelector('span').textContent = 'Infuse';
      }
    }

    _detectPillsFromName(name) {
      if (!name) return;
      const qualMatch = name.match(/(\b2160p|\b1080p|\b720p|\b480p|\b4k\b)/i);
      const codecMatch = name.match(/(\bx265\b|\bx264\b|\bhevc\b|\bavc\b|\b10bit\b)/i);
      const audioMatch = name.match(/(\baac\b|\bac3\b|\beac3\b|\bdts\b|\bopus\b|\bflac\b|\bmp3\b)/i);

      if (qualMatch) this.showPill(this.dom.detectedQuality, qualMatch[0].toUpperCase());
      if (codecMatch) this.showPill(this.dom.detectedCodec, codecMatch[0].toUpperCase());
      if (audioMatch) this.showPill(this.dom.detectedAudio, audioMatch[0].toUpperCase());
    }

    _initPoster() {
      const { posterImg, posterBg, posterPlaceholder } = this.dom;
      if (!posterImg) return;

      const testImg = new Image();
      testImg.src = posterImg.src;
      testImg.onload = () => {
        posterImg.classList.remove('hidden');
        if (posterBg) posterBg.classList.remove('hidden');
        if (posterPlaceholder) posterPlaceholder.classList.add('hidden');
      };
      testImg.onerror = () => {
        posterImg.classList.add('hidden');
        if (posterBg) posterBg.classList.add('hidden');
        if (posterPlaceholder) posterPlaceholder.classList.remove('hidden');
      };
    }

    showPill(el, text) {
      if (!el || !text) return;
      el.textContent = text;
      el.classList.remove('hidden');
    }

    updateDurationDisplay(seconds) {
      const { metaDuration } = this.dom;
      if (!metaDuration || !seconds || isNaN(seconds) || seconds <= 0) return;
      const s = Math.floor(seconds);
      const hrs = Math.floor(s / 3600);
      const mins = Math.floor((s % 3600) / 60);
      const secs = s % 60;
      const mm = String(mins).padStart(2, '0');
      const ss = String(secs).padStart(2, '0');
      metaDuration.textContent = hrs > 0 ? `${String(hrs).padStart(2, '0')}:${mm}:${ss}` : `${mm}:${ss}`;
    }

    setState(newState, statusMessage = '', isError = false, details = {}) {
      this.state = newState;
      this._emitTelemetry('player_state_change', { state: newState, message: statusMessage, isError, details });
      _log('29', 'State Transition', { state: newState, message: statusMessage, isError });

      if (typeof this.config.onStateChange === 'function') {
        try {
          this.config.onStateChange(newState, { message: statusMessage, isError, details });
        } catch (cbErr) {
          console.error('[FSPlayer] onStateChange callback error:', cbErr);
        }
      }

      const {
        playerOverlay,
        playerOverlaySpinner,
        playerOverlayIcon,
        playerOverlayText,
        playerOverlaySub,
        playerOverlayBadge,
        playerOverlayActions,
        unsupportedActions,
        errorActions,
        overlayNotice,
        playerStatePill,
        playerStateText
      } = this.dom;

      if (playerStateText) playerStateText.textContent = newState;
      if (playerStatePill) {
        playerStatePill.className = 'w-2 h-2 rounded-full ' +
          (newState === PlayerState.PLAYING ? 'bg-emerald-400' :
           newState === PlayerState.READY ? 'bg-emerald-300' :
           newState === PlayerState.BUFFERING || newState === PlayerState.LOADING || newState === PlayerState.AUTHENTICATING || newState === PlayerState.RESOLVING_SOURCE || newState === PlayerState.PREPARING_MEDIA ? 'bg-amber-400 animate-pulse' :
           newState === PlayerState.MEDIA_UNSUPPORTED ? 'bg-purple-400' :
           newState === PlayerState.ERROR || newState === PlayerState.PLAYBACK_ERROR || newState === PlayerState.NETWORK_ERROR || newState === PlayerState.AUTH_FAILED || newState === PlayerState.AUTH_ERROR || newState === PlayerState.TOKEN_EXPIRED || newState === PlayerState.STREAM_UNAVAILABLE || newState === PlayerState.SOURCE_NOT_FOUND ? 'bg-rose-500' : 'bg-zinc-600');
      }

      if (!playerOverlay) return;

      if (newState === PlayerState.PLAYING || newState === PlayerState.READY || newState === PlayerState.PAUSED) {
        playerOverlay.classList.add('hidden');
        return;
      }

      playerOverlay.classList.remove('hidden');
      if (overlayNotice) overlayNotice.classList.add('hidden');

      if (newState === PlayerState.LOADING || newState === PlayerState.AUTHENTICATING || newState === PlayerState.RESOLVING_SOURCE || newState === PlayerState.PREPARING_MEDIA) {
        if (playerOverlaySpinner) playerOverlaySpinner.classList.remove('hidden');
        if (this.dom.loadingBox) this.dom.loadingBox.classList.remove('hidden');
        if (this.dom.unsupportedBox) this.dom.unsupportedBox.classList.add('hidden');
        if (this.dom.errorBox) this.dom.errorBox.classList.add('hidden');
        if (playerOverlayIcon) playerOverlayIcon.classList.add('hidden');
        if (playerOverlayBadge) playerOverlayBadge.classList.add('hidden');
        if (playerOverlayText) playerOverlayText.textContent = 'Connecting Direct Stream…';
        if (playerOverlaySub) playerOverlaySub.textContent = statusMessage || 'Authenticating page session and preparing direct stream.';
        if (playerOverlayActions) playerOverlayActions.classList.add('hidden');
      } else if (newState === PlayerState.BUFFERING || newState === PlayerState.STALLED) {
        if (playerOverlaySpinner) playerOverlaySpinner.classList.remove('hidden');
        if (this.dom.loadingBox) this.dom.loadingBox.classList.remove('hidden');
        if (this.dom.unsupportedBox) this.dom.unsupportedBox.classList.add('hidden');
        if (this.dom.errorBox) this.dom.errorBox.classList.add('hidden');
        if (playerOverlayIcon) playerOverlayIcon.classList.add('hidden');
        if (playerOverlayBadge) playerOverlayBadge.classList.add('hidden');
        if (playerOverlayText) playerOverlayText.textContent = 'Buffering stream…';
        if (playerOverlaySub) playerOverlaySub.textContent = statusMessage || 'Preparing range buffers for smooth playback.';
        if (playerOverlayActions) playerOverlayActions.classList.add('hidden');
      } else if (newState === PlayerState.MEDIA_UNSUPPORTED || newState === PlayerState.UNSUPPORTED) {
        if (playerOverlaySpinner) playerOverlaySpinner.classList.add('hidden');
        if (this.dom.loadingBox) this.dom.loadingBox.classList.add('hidden');
        if (this.dom.errorBox) this.dom.errorBox.classList.add('hidden');
        if (this.dom.unsupportedBox) this.dom.unsupportedBox.classList.remove('hidden');

        if (playerOverlayIcon) playerOverlayIcon.classList.remove('hidden');
        if (playerOverlayText) playerOverlayText.textContent = details.title || 'Browser cannot play this format';
        if (playerOverlaySub) playerOverlaySub.textContent = statusMessage || 'Browser playback is not supported for this file.';

        const badgeText = details.formatBadge || (this.activeSource && this.activeSource.formatBadge) || 'MKV • HEVC is not supported here';
        if (this.dom.unsupportedBadgeText) this.dom.unsupportedBadgeText.textContent = badgeText;
        if (playerOverlayBadge) {
          const badgeSpan = playerOverlayBadge.querySelector('span') || playerOverlayBadge;
          badgeSpan.textContent = badgeText;
          playerOverlayBadge.classList.remove('hidden');
        }

        if (playerOverlayActions) playerOverlayActions.classList.remove('hidden');
        if (unsupportedActions) unsupportedActions.classList.remove('hidden');
        if (errorActions) errorActions.classList.add('hidden');
      } else if (newState === PlayerState.NETWORK_ERROR || newState === PlayerState.STREAM_UNAVAILABLE) {
        if (playerOverlaySpinner) playerOverlaySpinner.classList.add('hidden');
        if (this.dom.loadingBox) this.dom.loadingBox.classList.add('hidden');
        if (this.dom.unsupportedBox) this.dom.unsupportedBox.classList.add('hidden');
        if (this.dom.errorBox) this.dom.errorBox.classList.remove('hidden');

        if (playerOverlayIcon) playerOverlayIcon.classList.remove('hidden');
        if (playerOverlayBadge) playerOverlayBadge.classList.add('hidden');
        const title = newState === PlayerState.STREAM_UNAVAILABLE ? 'Streaming Service Unavailable' : 'Unable to connect to the media server.';
        if (playerOverlayText) playerOverlayText.textContent = title;
        if (playerOverlaySub) playerOverlaySub.textContent = statusMessage || 'A network error occurred while streaming.';
        const errTitle = document.getElementById('errorTitle');
        const errDesc = document.getElementById('errorDesc');
        if (errTitle) errTitle.textContent = title;
        if (errDesc) errDesc.textContent = statusMessage || 'A network error occurred while streaming.';

        if (playerOverlayActions) playerOverlayActions.classList.remove('hidden');
        if (unsupportedActions) unsupportedActions.classList.add('hidden');
        if (errorActions) errorActions.classList.remove('hidden');
      } else if (newState === PlayerState.AUTH_FAILED || newState === PlayerState.TOKEN_EXPIRED || newState === PlayerState.AUTH_ERROR) {
        if (playerOverlaySpinner) playerOverlaySpinner.classList.add('hidden');
        if (this.dom.loadingBox) this.dom.loadingBox.classList.add('hidden');
        if (this.dom.unsupportedBox) this.dom.unsupportedBox.classList.add('hidden');
        if (this.dom.errorBox) this.dom.errorBox.classList.remove('hidden');

        if (playerOverlayIcon) playerOverlayIcon.classList.remove('hidden');
        if (playerOverlayBadge) playerOverlayBadge.classList.add('hidden');
        if (playerOverlayText) playerOverlayText.textContent = 'Stream access expired';
        if (playerOverlaySub) playerOverlaySub.textContent = statusMessage || 'Your temporary stream token has expired. Please reload the page.';
        const errTitle = document.getElementById('errorTitle');
        const errDesc = document.getElementById('errorDesc');
        if (errTitle) errTitle.textContent = 'Stream access expired';
        if (errDesc) errDesc.textContent = statusMessage || 'Your temporary stream token has expired. Please reload the page.';

        if (playerOverlayActions) playerOverlayActions.classList.remove('hidden');
        if (unsupportedActions) unsupportedActions.classList.add('hidden');
        if (errorActions) errorActions.classList.remove('hidden');
      } else if (newState === PlayerState.SOURCE_NOT_FOUND) {
        if (playerOverlaySpinner) playerOverlaySpinner.classList.add('hidden');
        if (this.dom.loadingBox) this.dom.loadingBox.classList.add('hidden');
        if (this.dom.unsupportedBox) this.dom.unsupportedBox.classList.add('hidden');
        if (this.dom.errorBox) this.dom.errorBox.classList.remove('hidden');

        if (playerOverlayIcon) playerOverlayIcon.classList.remove('hidden');
        if (playerOverlayBadge) playerOverlayBadge.classList.add('hidden');
        if (playerOverlayText) playerOverlayText.textContent = 'Media Not Found';
        if (playerOverlaySub) playerOverlaySub.textContent = statusMessage || 'Media file not found or has been removed.';
        const errTitle = document.getElementById('errorTitle');
        const errDesc = document.getElementById('errorDesc');
        if (errTitle) errTitle.textContent = 'Media Not Found';
        if (errDesc) errDesc.textContent = statusMessage || 'Media file not found or has been removed.';

        if (playerOverlayActions) playerOverlayActions.classList.remove('hidden');
        if (unsupportedActions) unsupportedActions.classList.add('hidden');
        if (errorActions) errorActions.classList.remove('hidden');
      } else if (newState === PlayerState.PLAYBACK_ERROR || newState === PlayerState.ERROR || isError) {
        if (playerOverlaySpinner) playerOverlaySpinner.classList.add('hidden');
        if (this.dom.loadingBox) this.dom.loadingBox.classList.add('hidden');
        if (this.dom.unsupportedBox) this.dom.unsupportedBox.classList.add('hidden');
        if (this.dom.errorBox) this.dom.errorBox.classList.remove('hidden');

        if (playerOverlayIcon) playerOverlayIcon.classList.remove('hidden');
        if (playerOverlayBadge) playerOverlayBadge.classList.add('hidden');
        if (playerOverlayText) playerOverlayText.textContent = details.title || 'Playback Error';
        if (playerOverlaySub) playerOverlaySub.textContent = statusMessage || 'Playback failed. Please retry.';
        const errTitle = document.getElementById('errorTitle');
        const errDesc = document.getElementById('errorDesc');
        if (errTitle) errTitle.textContent = details.title || 'Playback Error';
        if (errDesc) errDesc.textContent = statusMessage || 'Playback failed. Please retry.';

        if (playerOverlayActions) playerOverlayActions.classList.remove('hidden');
        if (unsupportedActions) unsupportedActions.classList.add('hidden');
        if (errorActions) errorActions.classList.remove('hidden');
      }
    }

    async getStreamUrl() {
      if (this.cachedStreamData && this.cachedStreamData.stream_url) {
        return this.cachedStreamData.stream_url;
      }
      const sep = this.config.streamTokenPath.includes('?') ? '&' : '?';
      const fetchUrl = `${this.config.streamTokenPath}${sep}page_token=${encodeURIComponent(this.config.pageToken)}`;
      const res = await fetch(fetchUrl);
      if (!res.ok) throw new Error(`Token request failed: ${res.status}`);
      const data = await res.json();
      if (!data || !data.success || !data.stream_url) throw new Error('Failed to resolve stream URL');
      this.cachedStreamData = data;
      this.streamTokenFetchTime = Date.now();
      return data.stream_url;
    }

    async openVLC() {
      try {
        const streamUrl = await this.getStreamUrl();
        const vlcUri = buildVlcUri(streamUrl);
        _log('VLC', 'Launching VLC', { uri: _redact(vlcUri) });

        const opened = window.open(vlcUri, '_blank');
        if (!opened) {
          window.location.href = vlcUri;
        }

        this.showNotice('Attempting to open in VLC… If VLC did not open, click "Copy Stream URL" and open via Media > Open Network Stream in VLC.');
      } catch (err) {
        console.warn('[FSPlayer] openVLC error:', err);
        this.showNotice('Could not open VLC automatically. Please copy the stream URL below.');
      }
    }

    async openExternalPlayer() {
      try {
        const streamUrl = await this.getStreamUrl();
        const extUri = buildExternalPlayerUri(streamUrl);
        _log('EXT', 'Launching External Player', { uri: _redact(extUri) });

        const opened = window.open(extUri, '_blank');
        if (!opened) {
          window.location.href = extUri;
        }
        this.showNotice('Launching external player… If it does not open, copy the stream URL below.');
      } catch (err) {
        console.warn('[FSPlayer] openExternalPlayer error:', err);
        this.showNotice('Could not open player automatically. Please copy the stream URL.');
      }
    }

    async copyStreamUrl() {
      try {
        const streamUrl = await this.getStreamUrl();
        const success = await copyToClipboard(streamUrl);
        if (success) {
          this.showNotice('Stream URL copied to clipboard! In VLC: Media > Open Network Stream and paste the link.');
          if (window.FS && FS.Toast) {
            FS.Toast.info('Stream URL copied! Paste into VLC or external player.');
          }
        } else {
          prompt('Copy this stream URL to open in VLC or external player:', streamUrl);
        }
      } catch (err) {
        console.warn('[FSPlayer] copyStreamUrl error:', err);
        this.showNotice('Failed to obtain stream URL. Please reload the page.');
      }
    }

    showNotice(text) {
      const { overlayNotice } = this.dom;
      if (overlayNotice) {
        overlayNotice.textContent = text;
        overlayNotice.classList.remove('hidden');
      }
    }

    async initialize() {
      return this.startStreaming();
    }

    async startStreaming() {
      _log('1', 'player bootstrap initiated');
      this.openSmartlink();
      this.currentEpoch = Date.now();

      const { managerView, playerView, layoutEl } = this.dom;
      let playerEl = this.dom.playerEl;
      if (!playerEl) {
        this._cacheElements();
        playerEl = this.dom.playerEl;
      }

      if (managerView) managerView.classList.add('hidden');
      if (playerView) playerView.classList.remove('hidden');
      window.scrollTo({ top: 0, behavior: 'smooth' });

      if (this.state === PlayerState.LOADING || this.state === PlayerState.AUTHENTICATING) {
        return this._currentStreamingPromise || Promise.resolve();
      }

      this.setState(PlayerState.AUTHENTICATING, 'Connecting to stream…');

      if (this.streamAbortController) {
        this.streamAbortController.abort();
      }
      this.streamAbortController = new AbortController();
      const signal = this.streamAbortController.signal;

      if (this.watchdog) {
        this.watchdog.arm();
      }

      this._currentStreamingPromise = (async () => {
        try {
          const hasCustomElements = typeof window !== 'undefined' && Boolean(window.customElements);
          const isDefined = hasCustomElements && Boolean(customElements.get('media-player'));
          _log('2', 'custom element availability', { hasCustomElements, isDefined });

          if (hasCustomElements && !isDefined) {
            _log('2', 'Awaiting customElements.whenDefined("media-player")');
            try {
              await Promise.race([
                customElements.whenDefined('media-player'),
                new Promise((_, reject) => setTimeout(() => reject(new Error('CDN_TIMEOUT')), 3000)),
              ]);
            } catch (_) {}
          }

          if (!playerEl) {
            this._cacheElements();
            playerEl = this.dom.playerEl;
          }

          if (!playerEl) {
            throw new Error('Media player element not found in DOM.');
          }

          this._bindVidstackEvents();

          let data = this.cachedStreamData;
          const now = Date.now();
          if (!data || (now - this.streamTokenFetchTime > 1500 * 1000)) {
            this.setState(PlayerState.AUTHENTICATING, 'Authorizing secure stream…');
            const sep = this.config.streamTokenPath.includes('?') ? '&' : '?';
            const fetchUrl = `${this.config.streamTokenPath}${sep}page_token=${encodeURIComponent(this.config.pageToken)}`;

            _log('4', 'token request start', { path: _redact(fetchUrl) });

            const res = await fetch(fetchUrl, { signal });
            _log('5', 'token response HTTP status', { status: res.status, ok: res.ok });

            if (!res.ok) {
              if (res.status === 401) {
                this.setState(PlayerState.AUTH_FAILED, 'Stream authorization failed. Please reload the page.', true);
                throw new Error('Stream authorization failed. Please reload the page.');
              } else if (res.status === 403) {
                this.setState(PlayerState.TOKEN_EXPIRED, 'Stream access link expired. Please reload the page.', true);
                throw new Error('Stream access link expired. Please reload the page.');
              } else if (res.status === 404) {
                this.setState(PlayerState.SOURCE_NOT_FOUND, 'Media file not found or has been removed.', true);
                throw new Error('Media file not found or has been removed.');
              } else if (res.status >= 500) {
                this.setState(PlayerState.STREAM_UNAVAILABLE, 'Streaming edge gateway is temporarily unavailable. Please retry shortly.', true);
                throw new Error('Streaming edge gateway is temporarily unavailable. Please retry shortly.');
              } else {
                this.setState(PlayerState.PLAYBACK_ERROR, `Streaming request failed (${res.status}).`, true);
                throw new Error(`Streaming request failed (${res.status}).`);
              }
            }

            data = await res.json();
            _log('6', 'complete token response shape, excluding secrets', data);

            if (!data || !data.success) {
              throw new Error((data && data.message) || (data && data.error) || 'Secure access link expired');
            }
            this.cachedStreamData = data;
            this.streamTokenFetchTime = now;
          }

          if (data.file_duration && data.file_duration > 0) {
            this.updateDurationDisplay(data.file_duration);
          }
          if (data.resolution) {
            this.showPill(this.dom.detectedQuality, data.resolution);
            if (typeof data.resolution === 'string' && playerEl) {
              const resParts = data.resolution.toLowerCase().split('x');
              if (resParts.length === 2) {
                const rw = parseInt(resParts[0], 10);
                const rh = parseInt(resParts[1], 10);
                if (rh > rw) {
                  playerEl.classList.remove('is-landscape');
                  playerEl.classList.add('is-portrait');
                }
              }
            }
          }
          if (data.video_codec) this.showPill(this.dom.detectedCodec, data.video_codec);
          if (data.audio_codec) this.showPill(this.dom.detectedAudio, data.audio_codec);

          this.setState(PlayerState.RESOLVING_SOURCE, 'Resolving media stream source…');
          const resolved = resolveMediaSource(data, {
            fileName: this.config.fileName,
            mimeType: this.config.mimeType,
            isAudio: this.config.isAudio,
            isVideo: this.config.isVideo,
            videoCodec: data.video_codec,
            audioCodec: data.audio_codec,
          });

          _log('7', 'resolved stream URL/path', { src: _redact(resolved ? resolved.src : '') });
          _log('8', 'resolved MIME type', { mimeType: resolved ? resolved.mimeType : '' });
          _log('9', 'selected playback mode', { mode: resolved ? resolved.type : 'none', direct: true });

          if (!resolved || !resolved.src) {
            throw new Error('No playable media stream found for this file.');
          }

          this.activeSource = resolved;
          this.hasResumed = false;

          if (layoutEl && resolved.thumbnails) {
            layoutEl.thumbnails = resolved.thumbnails;
          }

          if (this.dom.playerSourceText) {
            this.dom.playerSourceText.textContent = 'Direct Edge Stream';
          }

          // CAPABILITY CHECK: IF BROWSER CANNOT DECODE (e.g. MKV + HEVC)
          if (!resolved.isSupported) {
            _log('CAPABILITY', 'Browser cannot decode media directly', {
              reason: resolved.unsupportedReason,
              isMkv: resolved.isMkv,
              isHevc: resolved.isHevc,
            });

            if (this.watchdog) this.watchdog.disarm();
            this.setState(
              PlayerState.MEDIA_UNSUPPORTED,
              resolved.unsupportedReason || 'Your browser cannot play this video format directly.',
              false,
              {
                formatBadge: resolved.formatBadge || '',
                title: resolved.unsupportedTitle || 'Browser playback unavailable',
              }
            );
            return resolved;
          }

          // Browser CAN play this stream: assign source
          _log('10', 'assignment of source to media-provider/player', {
            src: _redact(resolved.src),
            type: resolved.mimeType,
          });
          console.log('[Vidstack] source:', _redact(resolved.src));
          console.log('[Vidstack] media type:', resolved.mimeType);

          this.setState(PlayerState.PREPARING_MEDIA, 'Preparing video stream…');
          this.mediaRequestStarted = true;
          this.setState(PlayerState.LOADING, 'Loading stream…');

          // If browser canPlayType returns empty (like video/x-matroska), don't pass type to Vidstack,
          // as Vidstack will reject it upfront before letting the browser video engine probe it.
          const testVideo = typeof document !== 'undefined' ? document.createElement('video') : null;
          const hasNativeMimeSupport = Boolean(testVideo && resolved.mimeType && testVideo.canPlayType(resolved.mimeType));

          if (hasNativeMimeSupport) {
            playerEl.src = {
              src: resolved.src,
              type: resolved.mimeType,
            };
          } else {
            playerEl.src = resolved.src;
          }

          if (typeof playerEl.setAttribute === 'function') {
            playerEl.setAttribute('src', resolved.src);
          }

          const innerVideo = playerEl.querySelector('video, audio');
          if (innerVideo) {
            innerVideo.src = resolved.src;
            if (typeof innerVideo.load === 'function') {
              try { innerVideo.load(); } catch (_) {}
            }
          }

          if (typeof playerEl.load === 'function') {
            try { playerEl.load(); } catch (_) {}
          }

          this._emitTelemetry('source_selected', {
            type: resolved.type,
            mimeType: resolved.mimeType,
          });

          return resolved;
        } catch (err) {
          if (err.name === 'AbortError') return;
          if (this.watchdog) this.watchdog.disarm();
          console.warn('Streaming initialization error:', err);
          const failureStates = [
            PlayerState.AUTH_FAILED,
            PlayerState.AUTH_ERROR,
            PlayerState.TOKEN_EXPIRED,
            PlayerState.SOURCE_NOT_FOUND,
            PlayerState.STREAM_UNAVAILABLE,
            PlayerState.NETWORK_ERROR,
            PlayerState.MEDIA_UNSUPPORTED,
            PlayerState.UNSUPPORTED,
            PlayerState.PLAYBACK_ERROR,
            PlayerState.ERROR,
          ];
          if (!failureStates.includes(this.state)) {
            this.setState(PlayerState.PLAYBACK_ERROR, err.message || 'Unable to play this media. Please try again.', true);
          }
          throw err;
        } finally {
          this._currentStreamingPromise = null;
        }
      })();

      return this._currentStreamingPromise;
    }

    _onCanPlay() {
      const terminalStates = [
        PlayerState.MEDIA_UNSUPPORTED,
        PlayerState.UNSUPPORTED,
        PlayerState.AUTH_FAILED,
        PlayerState.AUTH_ERROR,
        PlayerState.TOKEN_EXPIRED,
        PlayerState.SOURCE_NOT_FOUND,
      ];
      if (terminalStates.includes(this.state)) {
        return;
      }

      if (this.watchdog) this.watchdog.disarm();
      this.setState(PlayerState.READY);

      const { playerEl } = this.dom;
      if (!playerEl) return;

      const resumeSec = window.FS && FS.Progress ? FS.Progress.getResumeSeconds(this.config.shareUrl) : 0;
      if (resumeSec > 0 && !this.hasResumed) {
        this.hasResumed = true;
        try {
          playerEl.currentTime = resumeSec;
        } catch (_) {}
        if (window.FS && FS.Toast) {
          FS.Toast.info(`Resuming from ${FS.Progress.formatClock(resumeSec)}`, 2400);
        }
      }

      playerEl.play().catch((err) => {
        console.log('[FSPlayer] Autoplay prevented by browser policy (user can tap play):', err && err.message);
      });
    }

    _onPlaying() {
      const terminalStates = [
        PlayerState.MEDIA_UNSUPPORTED,
        PlayerState.UNSUPPORTED,
        PlayerState.AUTH_FAILED,
        PlayerState.AUTH_ERROR,
        PlayerState.TOKEN_EXPIRED,
        PlayerState.SOURCE_NOT_FOUND,
      ];
      if (terminalStates.includes(this.state)) {
        return;
      }

      if (this.watchdog) this.watchdog.disarm();
      this.setState(PlayerState.PLAYING);
      this._emitTelemetry('playback_started');
    }

    _onPause() {
      if (this.state === PlayerState.PLAYING) {
        this.setState(PlayerState.PAUSED);
      }
    }

    _onWaiting() {
      if (this.state === PlayerState.PLAYING) {
        this.setState(PlayerState.BUFFERING, 'Buffering stream…');
      }
    }

    _onLoadedMetadata() {
      const terminalStates = [
        PlayerState.MEDIA_UNSUPPORTED,
        PlayerState.UNSUPPORTED,
        PlayerState.AUTH_FAILED,
        PlayerState.AUTH_ERROR,
        PlayerState.TOKEN_EXPIRED,
        PlayerState.SOURCE_NOT_FOUND,
      ];
      if (terminalStates.includes(this.state)) {
        return;
      }

      if (this.watchdog) this.watchdog.disarm();
      this.setState(PlayerState.READY);

      const { playerEl } = this.dom;
      if (!playerEl) return;

      const dur = playerEl.duration || this.config.serverDuration;
      if (dur > 0) {
        this.updateDurationDisplay(dur);
      }

      const video = playerEl.querySelector('video');
      if (video && video.videoWidth && video.videoHeight) {
        const w = video.videoWidth;
        const h = video.videoHeight;
        if (h > w) {
          playerEl.classList.remove('is-landscape');
          playerEl.classList.add('is-portrait');
        } else {
          playerEl.classList.remove('is-portrait');
          playerEl.classList.add('is-landscape');
        }
        let qual = '';
        if (h >= 2160 || w >= 3840) qual = '4K';
        else if (h >= 1440 || w >= 2560) qual = '1440P';
        else if (h >= 1080 || w >= 1920) qual = '1080P';
        else if (h >= 720 || w >= 1280) qual = '720P';
        else if (h >= 480) qual = '480P';
        else qual = `${w}x${h}`;
        this.showPill(this.dom.detectedQuality, qual);
      }
    }

    _onError(event) {
      if (this.watchdog) this.watchdog.disarm();
      const diag = this._getMediaDiagnostics();
      console.warn('[FSPlayer] Direct stream playback error encountered:', event, diag);
      const detailMsg = (event && event.detail && (event.detail.message || event.detail.error)) || '';
      console.error('[Vidstack] error:', detailMsg || diag.errorMessage || 'Playback error');

      const errCode = diag.errorCode;
      if (errCode === 1) {
        // MEDIA_ERR_ABORTED - Transient abort during source change or user stop; ignore
        return;
      }

      if (errCode === 4) {
        // MEDIA_ERR_SRC_NOT_SUPPORTED - Actual browser rejection
        this.setState(
          PlayerState.MEDIA_UNSUPPORTED,
          'Your browser cannot play this video format directly.',
          false,
          {
            title: 'Browser playback unavailable',
            formatBadge: (this.config.fileName.split('.').pop() || 'MKV').toUpperCase(),
          }
        );
      } else if (errCode === 2) {
        // MEDIA_ERR_NETWORK
        this.setState(
          PlayerState.NETWORK_ERROR,
          'Unable to connect to the video stream.',
          true
        );
      } else if (errCode === 3) {
        // MEDIA_ERR_DECODE
        this.setState(
          PlayerState.PLAYBACK_ERROR,
          'Error decoding media stream.',
          true
        );
      } else {
        this.setState(PlayerState.PLAYBACK_ERROR, 'Unable to connect to the video stream.', true);
      }

      this._emitTelemetry('stream_error', { error: detailMsg || 'playback_error', diagnostics: diag });
    }

    _onTimeUpdate() {
      const { playerEl } = this.dom;
      if (!playerEl) return;

      const cur = playerEl.currentTime;
      const dur = playerEl.duration || this.config.serverDuration;
      if (Math.abs(cur - this.lastSavedProgress) >= 3 && window.FS && FS.Progress) {
        this.lastSavedProgress = cur;
        FS.Progress.write(this.config.shareUrl, cur, dur, this.config.fileName);
      }
    }

    _onEnded() {
      this.setState(PlayerState.ENDED);
      if (window.FS && FS.Progress) {
        FS.Progress.clear(this.config.shareUrl);
      }
      this._emitTelemetry('playback_ended');
    }

    retry() {
      this.cachedStreamData = null;
      this.startStreaming();
    }

    teardown() {
      this.destroy();
    }

    destroy() {
      const { playerEl, layoutEl, managerView, playerView } = this.dom;

      if (this.failsafeTimer) {
        clearTimeout(this.failsafeTimer);
        this.failsafeTimer = null;
      }
      if (this.watchdog) {
        this.watchdog.disarm();
      }

      if (this.streamAbortController) {
        this.streamAbortController.abort();
        this.streamAbortController = null;
      }

      if (playerEl) {
        try {
          playerEl.pause();
          playerEl.src = '';
          const inner = playerEl.querySelector('video, audio');
          if (inner) inner.src = '';
        } catch (_) {}
      }

      if (layoutEl) {
        layoutEl.thumbnails = '';
      }

      if (playerView) playerView.classList.add('hidden');
      if (managerView) {
        managerView.classList.remove('hidden');
        managerView.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }

      this.setState(PlayerState.IDLE);
    }

    triggerDownload(btn) {
      this.openSmartlink();
      if (window.FS && window.FS.triggerDownload) {
        window.FS.triggerDownload(this.config.downloadTokenPath, this.config.pageToken);
      }
    }

    share() {
      if (window.FS && window.FS.Share) {
        window.FS.Share.copy(this.config.shareUrl, 'Link copied to clipboard!');
      }
    }

    openSmartlink() {
      if (this.config.smartlinkUrl) {
        window.open(this.config.smartlinkUrl, '_blank', 'noopener,noreferrer');
      }
    }

    _emitTelemetry(event, data = {}) {
      if (typeof window !== 'undefined' && window.FSPlayer && typeof window.FSPlayer.onTelemetry === 'function') {
        window.FSPlayer.onTelemetry(event, { ...data, fileId: this.config.publicId });
      }
    }
  }

  // Export globally & module
  const FSPlayer = {
    PlayerState,
    PlayerController,
    resolveMediaSource,
    checkBrowserPlaybackSupport,
    getPlatform,
    buildVlcUri,
    buildExternalPlayerUri,
    copyToClipboard,
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = FSPlayer;
  } else {
    global.FSPlayer = FSPlayer;
    global.FSPlayerState = PlayerState;
    global.FSResolveMediaSource = resolveMediaSource;
    global.FSCheckBrowserPlaybackSupport = checkBrowserPlaybackSupport;
  }
})(typeof window !== 'undefined' ? window : globalThis);
