(() => {
  "use strict";
  const node = document.getElementById("ad-config");
  if (!node) return;
  const config = JSON.parse(node.textContent);
  if (!config.enabled) return;
  const mobile = matchMedia("(max-width: 767px)").matches;
  const device = mobile ? config.mobile : config.desktop;
  const safeUrl = (value) => {
    try {
      const url = new URL(value, location.href);
      return url.protocol === "https:" ? url.href : null;
    } catch (_) {
      return null;
    }
  };
  function mount(placement, url, width, height, options) {
    const frame = document.createElement("iframe");
    frame.title = "Advertisement";
    frame.width = width;
    frame.height = height;
    frame.sandbox = "allow-scripts allow-popups";
    frame.referrerPolicy = "no-referrer";
    const label = document.createElement("span");
    label.className = "ad-label";
    label.textContent = "Advertisement";
    const status = document.createElement("p");
    status.className = "ad-status";
    status.hidden = true;
    // Provider script failure/outage is unknown, never evidence of an ad blocker.
    const timeout = setTimeout(() => {
      status.textContent =
        "Advertisement unavailable or slow. File access still works.";
      status.hidden = false;
    }, 8000);
    const listener = (event) => {
      if (
        event.source !== frame.contentWindow ||
        !event.data ||
        event.data.type !== "filestream-ad"
      )
        return;
      clearTimeout(timeout);
      if (event.data.state === "error") {
        status.textContent =
          "Advertisement could not load. File access still works.";
        status.hidden = false;
      }
      window.removeEventListener("message", listener);
    };
    window.addEventListener("message", listener);
    const encode = (text) =>
      text.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
    const setup = options
      ? "var atOptions=" +
        JSON.stringify(options).replace(/</g, "\\u003c") +
        ";"
      : "";
    // Opaque-origin frames keep third-party scripts away from page tokens and cookies.
    frame.srcdoc =
      '<!doctype html><meta name="referrer" content="no-referrer"><style>body{margin:0;overflow:hidden}</style><script>' +
      setup +
      '<\/script><script src="' +
      encode(url) +
      "\" onload=\"parent.postMessage({type:'filestream-ad',state:'loaded'},'*')\" onerror=\"parent.postMessage({type:'filestream-ad',state:'error'},'*')\"><\/script>";
    placement.append(label, frame, status);
    placement.classList.add("loaded");
  }
  // Choose placements once per page; resizing never initializes the shared unit twice.
  for (const placement of document.querySelectorAll("[data-ad-placement]")) {
    const slot =
      placement.dataset.adPlacement === "top"
        ? device.top_banner
        : mobile
          ? device.bottom_banner
          : device.inline_banner;
    if (!slot || !slot.enabled) continue;
    const url = safeUrl(slot.invoke_url);
    if (!url) continue;
    mount(placement, url, slot.width, slot.height, {
      key: slot.key,
      format: "iframe",
      height: slot.height,
      width: slot.width,
      params: {},
    });
  }
  const social = safeUrl(device.social_bar_url);
  if (social) {
    const placement = document.createElement("aside");
    placement.className = "ad-placement ad-social";
    placement.setAttribute("aria-label", "Advertisement");
    document.querySelector("main").append(placement);
    mount(placement, social, "100%", 60, null);
  }
  // Smartlinks and popunder scripts are deliberately not executed. No site policy
  // permits popunders, and core actions should complete without extra windows.
})();
