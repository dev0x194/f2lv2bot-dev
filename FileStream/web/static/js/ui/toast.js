/**
 * File To Link V2 — Accessible Toast Notification System
 */
(function (global) {
  'use strict';

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  const Toast = {
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

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { Toast };
  } else {
    global.FSToast = Toast;
  }
})(typeof window !== 'undefined' ? window : globalThis);
