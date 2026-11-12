/**
 * File To Link V2 — Player State Machine Definition
 */
(function (global) {
  'use strict';

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
    ENDED: 'ENDED',

    // Explicit failure states
    AUTH_FAILED: 'AUTH_FAILED',
    TOKEN_EXPIRED: 'TOKEN_EXPIRED',
    SOURCE_NOT_FOUND: 'SOURCE_NOT_FOUND',
    STREAM_UNAVAILABLE: 'STREAM_UNAVAILABLE',
    MEDIA_UNSUPPORTED: 'MEDIA_UNSUPPORTED',
    NETWORK_ERROR: 'NETWORK_ERROR',
    PLAYBACK_ERROR: 'PLAYBACK_ERROR',

    // Backward-compatibility aliases
    UNSUPPORTED: 'MEDIA_UNSUPPORTED',
    AUTH_ERROR: 'AUTH_FAILED',
    ERROR: 'PLAYBACK_ERROR',
  });

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { PlayerState };
  } else {
    global.FSPlayerState = PlayerState;
  }
})(typeof window !== 'undefined' ? window : globalThis);
