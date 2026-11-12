/**
 * File To Link V2 — Motion & Micro-Interactions
 * Smooth cursor spotlight tracking and touch-responsive spring transitions
 */
(function (global) {
  'use strict';

  const Motion = {
    initSpotlight(selector = '.spotlight-card, .fs-card') {
      if (typeof window === 'undefined') return;

      const cards = document.querySelectorAll(selector);
      window.addEventListener('mousemove', (e) => {
        cards.forEach((card) => {
          const rect = card.getBoundingClientRect();
          const x = e.clientX - rect.left;
          const y = e.clientY - rect.top;
          card.style.setProperty('--mouse-x', `${x}px`);
          card.style.setProperty('--mouse-y', `${y}px`);
        });
      }, { passive: true });
    },
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { Motion };
  } else {
    global.FSMotion = Motion;
  }
})(typeof window !== 'undefined' ? window : globalThis);
