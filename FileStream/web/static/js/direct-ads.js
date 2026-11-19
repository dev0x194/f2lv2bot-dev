/* UNSAFE experimental parent-context integration. This is not an isolation layer. */
(() => {
  if (window.FileStreamDirectAds) return;
  const mobile = matchMedia('(max-width: 767px)').matches;
  const seen = new Set();
  const write = (html) => {
    // Never use document.write after parsing: it could replace the application.
    if (document.readyState !== 'loading') return false;
    document.write(html);
    return true;
  };
  const escape = value => String(value).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
  const script = url => '<script referrerpolicy="origin" src="' + escape(url) + '"></script>';
  window.FileStreamDirectAds = Object.freeze({
    banner(config, placement) {
      if (!config.enabled) return;
      const device = mobile ? config.mobile : config.desktop;
      const slot = placement === 'top' ? device.top_banner : mobile ? device.bottom_banner : device.inline_banner;
      if (!slot?.enabled) return;
      const identity = slot.key;
      if (seen.has(identity) || document.readyState !== 'loading') return;
      seen.add(identity);
      document.currentScript?.parentElement?.classList.add("loaded");
      const options = JSON.stringify({key: slot.key, format: 'iframe', height: slot.height, width: slot.width, params: {}}).replace(/</g, '\\u003c');
      // Both emitted scripts are parser inserted and blocking, matching Test A.
      write('<span id="ad-label-' + escape(placement) + '" class="ad-label">Advertisement</span><div class="direct-ad-unit" style="width:' + slot.width + 'px;max-width:100%;margin-inline:auto;min-height:' + slot.height + 'px">' +
        '<script>var atOptions=' + options + ';</script>' + script(slot.invoke_url) + '</div>');
    },
    optional(url, name) {
      if (url && typeof url === "object") url = mobile ? url.mobile : url.desktop;
      if (!url || seen.has(name)) return;
      seen.add(name);
      write(script(url));
    }
  });
})();
