/**
 * File To Link V2 — Core Application Bootstrap & Utilities
 * Architecture: Clean Modular Services (Toasts, Sharing, Progress, Theme, Ads)
 */
(function (window, document) {
  'use strict';

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  // 1. Toast Notification System
  const FSToast = {
    container: null,

    _init() {
      if (this.container || typeof document === 'undefined') return;
      this.container = document.createElement('div');
      this.container.className = 'fs-toast-container';
      this.container.setAttribute('role', 'status');
      this.container.setAttribute('aria-live', 'polite');
      document.body.appendChild(this.container);
    },

    show(message, type = 'info', duration = 3000) {
      this._init();
      if (!this.container) return;

      const toast = document.createElement('div');
      toast.className = `fs-toast fs-toast-${type}`;

      let icon = 'ℹ️';
      if (type === 'success') icon = '✓';
      if (type === 'error') icon = '✕';
      if (type === 'warning') icon = '⚠';

      toast.innerHTML = `
        <span class="fs-toast-icon" aria-hidden="true" style="font-weight: 700; font-family: monospace;">${icon}</span>
        <span>${escapeHtml(message)}</span>
      `;
      this.container.appendChild(toast);

      const dismiss = () => {
        toast.classList.add('hide');
        setTimeout(() => toast.remove(), 220);
      };

      const timer = setTimeout(dismiss, duration);
      toast.addEventListener('click', () => {
        clearTimeout(timer);
        dismiss();
      });
    },

    success(message, duration) {
      this.show(message, 'success', duration);
    },

    error(message, duration) {
      this.show(message, 'error', duration);
    },

    info(message, duration) {
      this.show(message, 'info', duration);
    },
  };

  // 2. Share & Clipboard System
  const FSShare = {
    async copy(text, label = 'Link') {
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text);
          FSToast.success(`✓ ${label} copied to clipboard`);
          return true;
        }
      } catch (err) {
        console.warn('Clipboard write failed, using fallback:', err);
      }

      window.prompt(`Copy ${label.toLowerCase()}:`, text);
      FSToast.info(`${label} ready to copy`);
      return false;
    },

    async share(options = {}) {
      const url = options.url || window.location.href;
      const title = options.title || document.title;
      const text = options.text || title;

      if (navigator.share) {
        try {
          await navigator.share({ title, text, url });
          FSToast.success('✓ Shared successfully');
          return true;
        } catch (err) {
          if (err.name !== 'AbortError') {
            return this.copy(url, 'Link');
          }
          return false;
        }
      }

      return this.copy(url, 'Link');
    },
  };

  // 3. Theme Toggle & Persistence
  const FSTheme = {
    STORAGE_KEY: 'fs_theme_preference',

    init() {
      let saved = null;
      try {
        saved = localStorage.getItem(this.STORAGE_KEY);
      } catch (_) {}

      if (saved === 'light' || saved === 'dark') {
        this.setTheme(saved, false);
      } else {
        const prefersLight = Boolean(
          window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches
        );
        this.setTheme(prefersLight ? 'light' : 'dark', false);
      }

      if (window.matchMedia) {
        const mql = window.matchMedia('(prefers-color-scheme: light)');
        const handler = (e) => {
          let hasPref = false;
          try { hasPref = Boolean(localStorage.getItem(this.STORAGE_KEY)); } catch (_) {}
          if (!hasPref) {
            this.setTheme(e.matches ? 'light' : 'dark', false);
          }
        };
        if (mql.addEventListener) {
          mql.addEventListener('change', handler);
        } else if (mql.addListener) {
          mql.addListener(handler);
        }
      }

      document.querySelectorAll('[data-fs-theme-toggle]').forEach((btn) => {
        btn.addEventListener('click', () => this.toggle());
      });
    },

    getTheme() {
      return document.documentElement.getAttribute('data-theme') || 'dark';
    },

    setTheme(theme, save = true) {
      const safeTheme = theme === 'light' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', safeTheme);
      if (safeTheme === 'dark') {
        document.documentElement.classList.add('dark');
      } else {
        document.documentElement.classList.remove('dark');
      }
      if (save) {
        try {
          localStorage.setItem(this.STORAGE_KEY, safeTheme);
        } catch (_) {}
      }
      this._updateButtons(safeTheme);
    },

    toggle() {
      const next = this.getTheme() === 'dark' ? 'light' : 'dark';
      this.setTheme(next, true);
      FSToast.info(`${next === 'dark' ? '🌙 Dark' : '☀️ Light'} theme enabled`, 1800);
    },

    _updateButtons(theme) {
      document.querySelectorAll('[data-fs-theme-toggle]').forEach((btn) => {
        btn.innerHTML = theme === 'dark' ? '☀️' : '🌙';
        btn.setAttribute('aria-label', `Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`);
      });
    },
  };

  // 4. Watch Progress Tracking
  const FSProgress = {
    MIN_SECONDS: 8,
    MAX_ENTRIES: 50,
    EXPIRY_MS: 30 * 24 * 60 * 60 * 1000,

    _storageKey(shareUrl) {
      return `fsb_progress::${encodeURIComponent(shareUrl || window.location.pathname)}`;
    },

    read(shareUrl) {
      try {
        const raw = localStorage.getItem(this._storageKey(shareUrl));
        if (!raw) return null;
        const obj = JSON.parse(raw);
        return (obj && typeof obj === 'object') ? obj : null;
      } catch (_) {
        return null;
      }
    },

    write(shareUrl, current, duration, name = '') {
      try {
        const c = Number(current);
        const d = Number(duration);
        if (!Number.isFinite(c) || !Number.isFinite(d) || d <= 0) return;

        if (c >= d - 5 || (d > 30 && (c / d) > 0.95)) {
          this.clear(shareUrl);
          return;
        }

        const payload = {
          current: Math.max(0, Math.min(c, d)),
          duration: d,
          name: String(name || ''),
          updatedAt: Date.now(),
        };
        localStorage.setItem(this._storageKey(shareUrl), JSON.stringify(payload));
        this._prune();
      } catch (_) {}
    },

    clear(shareUrl) {
      try {
        localStorage.removeItem(this._storageKey(shareUrl));
      } catch (_) {}
    },

    _prune() {
      try {
        const prefix = 'fsb_progress::';
        const entries = [];
        const now = Date.now();
        for (let i = 0; i < localStorage.length; i++) {
          const key = localStorage.key(i);
          if (key && key.startsWith(prefix)) {
            try {
              const val = JSON.parse(localStorage.getItem(key));
              if (!val || typeof val !== 'object' || (now - (val.updatedAt || 0) > this.EXPIRY_MS)) {
                localStorage.removeItem(key);
              } else {
                entries.push({ key, updatedAt: val.updatedAt || 0 });
              }
            } catch (_) {
              localStorage.removeItem(key);
            }
          }
        }
        if (entries.length > this.MAX_ENTRIES) {
          entries.sort((a, b) => a.updatedAt - b.updatedAt);
          const toRemove = entries.slice(0, entries.length - this.MAX_ENTRIES);
          toRemove.forEach((e) => localStorage.removeItem(e.key));
        }
      } catch (_) {}
    },

    getResumeSeconds(shareUrl) {
      const p = this.read(shareUrl);
      if (!p) return 0;
      const d = Number(p.duration || 0);
      const c = Number(p.current || 0);
      if (!Number.isFinite(d) || !Number.isFinite(c) || d <= 0) return 0;

      if (c >= d - 5 || (d > 30 && (c / d) > 0.95)) return 0;

      const minSec = d < 15 ? 4 : this.MIN_SECONDS;
      const safe = Math.min(c, Math.max(d - 4, 0));
      return safe >= minSec ? safe : 0;
    },

    formatClock(seconds) {
      const s = Math.max(0, Math.floor(Number(seconds) || 0));
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      const sec = s % 60;
      if (h > 0) {
        return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
      }
      return `${m}:${String(sec).padStart(2, '0')}`;
    },
  };

  // 5. Media Session API
  const FSMediaSession = {
    setMetadata(title, artist = 'File To Link V2', artworkUrl = null) {
      if (!('mediaSession' in navigator)) return;
      try {
        const artwork = artworkUrl ? [{ src: artworkUrl, sizes: '512x512', type: 'image/png' }] : [];
        navigator.mediaSession.metadata = new MediaMetadata({
          title: String(title || 'Media File'),
          artist: String(artist || 'File To Link V2'),
          artwork,
        });
      } catch (err) {
        console.debug('MediaSession setup:', err);
      }
    },
  };

  // 6. Non-blocking Ad & Banner Utilities
  const FSAds = {
    isMobileView() {
      return window.matchMedia ? window.matchMedia('(max-width: 767px)').matches : (window.innerWidth <= 767);
    },

    slotMatchesDevice(slot, isMobile) {
      const device = (slot.getAttribute('data-device') || 'all').toLowerCase();
      if (device === 'desktop' && isMobile) return false;
      if (device === 'mobile' && !isMobile) return false;
      return true;
    },

    renderBanners() {
      const mobile = this.isMobileView();
      const slots = Array.from(document.querySelectorAll('.ad-slot')).filter((slot) => {
        if (slot.dataset.loaded === '1') return false;
        return this.slotMatchesDevice(slot, mobile);
      });

      let index = 0;
      const loadNext = () => {
        if (index >= slots.length) return;

        const slot = slots[index++];
        const key = slot.getAttribute('data-key');
        const invokeUrl = slot.getAttribute('data-invoke-url');
        const width = parseInt(slot.getAttribute('data-width') || '0', 10);
        const height = parseInt(slot.getAttribute('data-height') || '0', 10);

        if (!key || !invokeUrl || !width || !height) {
          slot.dataset.loaded = '1';
          loadNext();
          return;
        }

        slot.innerHTML = '';
        const inner = document.createElement('div');
        inner.className = 'ad-slot-inner';
        inner.style.width = `${width}px`;
        inner.style.height = `${height}px`;

        const s1 = document.createElement('script');
        s1.type = 'text/javascript';
        s1.text = `atOptions = {'key' : '${key}','format' : 'iframe','height' : ${height},'width' : ${width},'params' : {}};`;

        const s2 = document.createElement('script');
        s2.type = 'text/javascript';
        s2.src = invokeUrl;
        s2.async = false;

        let settled = false;
        const settle = (ok) => {
          if (settled) return;
          settled = true;
          clearTimeout(failsafe);
          slot.dataset.loaded = '1';
          if (ok) {
            slot.classList.add('loaded');
          } else {
            slot.classList.add('ad-collapsed');
            const parentCard = slot.closest('.fs-ad-card');
            if (parentCard && !parentCard.querySelector('.ad-slot.loaded')) {
              parentCard.classList.add('ad-card-collapsed');
            }
          }
          loadNext();
        };

        const failsafe = setTimeout(() => settle(false), 5000);
        s2.onload = () => settle(true);
        s2.onerror = () => settle(false);

        inner.appendChild(s1);
        inner.appendChild(s2);
        slot.appendChild(inner);
      };

      if (slots.length > 0) {
        loadNext();
      }
    },

    initPopunder(popunderUrl) {
      if (!popunderUrl || document.getElementById('adsterra-popunder-script')) return;
      const script = document.createElement('script');
      script.id = 'adsterra-popunder-script';
      script.async = true;
      script.src = popunderUrl;
      document.body.appendChild(script);
    },

    initSocialBar(desktopUrl, mobileUrl) {
      const isMobile = this.isMobileView();
      const socialBarUrl = (isMobile ? mobileUrl : desktopUrl || '').trim();
      if (!socialBarUrl || document.getElementById('adsterra-socialbar-script')) return;

      const script = document.createElement('script');
      script.id = 'adsterra-socialbar-script';
      script.src = socialBarUrl;
      document.body.appendChild(script);
    },
  };

  // 7. Interactive Spotlight System
  const FSSpotlight = {
    init() {
      const cards = document.querySelectorAll('.fs-card, .spotlight-card');
      cards.forEach((card) => {
        card.addEventListener('mousemove', (e) => {
          const rect = card.getBoundingClientRect();
          const x = e.clientX - rect.left;
          const y = e.clientY - rect.top;
          card.style.setProperty('--mouse-x', `${x}px`);
          card.style.setProperty('--mouse-y', `${y}px`);
        });
      });
    },
  };

  window.FS = {
    Toast: FSToast,
    Share: FSShare,
    Theme: FSTheme,
    Progress: FSProgress,
    MediaSession: FSMediaSession,
    Ads: FSAds,
    Spotlight: FSSpotlight,
  };

  function initAll() {
    FSTheme.init();
    FSSpotlight.init();
    if (window.lucide) {
      lucide.createIcons();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAll);
  } else {
    initAll();
  }
})(window, document);
