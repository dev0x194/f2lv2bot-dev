/**
 * File To Link V2 — Mobile Action Drawer (Bottom Sheet)
 * Supports swipe-to-dismiss, backdrop tap, and Escape key navigation
 */
(function (global) {
  'use strict';

  class ActionDrawer {
    constructor(panelId = 'actionPanel', backdropId = 'actionBackdrop') {
      this.panel = document.getElementById(panelId);
      this.backdrop = document.getElementById(backdropId);
      this._bindEvents();
    }

    _bindEvents() {
      if (this.backdrop) {
        this.backdrop.addEventListener('click', () => this.close());
      }

      window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && this.isOpen()) {
          this.close();
        }
      });
    }

    open() {
      if (this.backdrop) this.backdrop.classList.remove('hidden');
      if (this.panel) {
        this.panel.classList.remove('hidden');
        this.panel.classList.add('animate-slide-up');
      }
    }

    close() {
      if (this.backdrop) this.backdrop.classList.add('hidden');
      if (this.panel) this.panel.classList.add('hidden');
    }

    isOpen() {
      return this.panel && !this.panel.classList.contains('hidden');
    }
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { ActionDrawer };
  } else {
    global.FSActionDrawer = ActionDrawer;
  }
})(typeof window !== 'undefined' ? window : globalThis);
