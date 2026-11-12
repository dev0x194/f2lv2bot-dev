/**
 * File To Link V2 — Media Source Resolver & Browser Codec Capability Detector
 * Architecture: Resolves direct HTTP media stream with range support & capability detection
 */
(function (global) {
  'use strict';

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

    // 1. Direct container format checks for legacy formats without browser support
    if (isAvi || isWmv || isFlv) {
      const canPlayFormat = mediaEl.canPlayType(rawMime);
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

    // 2. Modern browsers (Chromium, Edge, etc.) natively decode Matroska (MKV) containers
    // and HEVC/H.265 video via hardware or platform demuxers. canPlayType() often returns empty
    // string for video/x-matroska even when playback succeeds. Do not block upfront;
    // allow the player to attempt playback and only transition to MEDIA_UNSUPPORTED
    // if the browser media element encounters a genuine fatal decode error (code 4).
    return {
      supported: true,
      canPlay: 'maybe',
      isMkv: isMkv,
      isHevc: isHevc,
      formatBadge: isMkv ? (isHevc ? 'MKV • HEVC' : 'MKV') : (isHevc ? 'HEVC' : ''),
    };
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

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { resolveMediaSource, checkBrowserPlaybackSupport };
  } else {
    global.FSResolveMediaSource = resolveMediaSource;
    global.FSCheckBrowserPlaybackSupport = checkBrowserPlaybackSupport;
  }
})(typeof window !== 'undefined' ? window : globalThis);
