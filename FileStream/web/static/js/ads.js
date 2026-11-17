(() => {
  "use strict";
  const node = document.getElementById("ad-config");
  if (!node) return;
  const config = JSON.parse(node.textContent);
  if (!config.enabled) return;
  if (node.dataset.initialized) return;
  node.dataset.initialized = "true";
  const mobile = matchMedia("(max-width: 767px)").matches;
  const device = mobile ? config.mobile : config.desktop;
  const safeUrl = (value) => {
    try {
      const url = new URL(value);
      return url.protocol === "https:" && !url.username && !url.password ? url.href : null;
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
    // Provider script failure/outage is unknown, never evidence of an ad blocker.
    let timeout;
    const fail = (reason) => {
      clearTimeout(timeout);
      frame.remove();
      label.hidden = true;
      placement.classList.remove("loaded");
      placement.dataset.adState = reason;
      console.info("[FileStream ads]", placement.dataset.adPlacement || "social", reason);
      window.removeEventListener("message", listener);
    };
    const listener = (event) => {
      if (event.source !== frame.contentWindow || event.data?.type !== "filestream-ad") return;
      if (event.data.state === "error" || event.data.state === "empty") fail(event.data.state);
      if (event.data.state === "content") {
        clearTimeout(timeout);
        // Content was inserted; cross-origin creative fill remains unverifiable.
        placement.classList.add("loaded");
        placement.dataset.adState = "content-unverified";
      }
    };
    window.addEventListener("message", listener);
    // A timeout does not prove blocking or no-fill. Late provider content may still appear.
    timeout = setTimeout(() => {
      placement.classList.remove("loaded");
      placement.dataset.adState = "slow-unverified";
      console.info("[FileStream ads] slow provider; fill unknown");
    }, 30000);
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
      "const report=state=>parent.postMessage({type:'filestream-ad',state},'*');" +
      "const check=()=>{if(document.querySelector('iframe,img,video,object') || document.body?.innerText.trim()) report('content');};" +
      "new MutationObserver(check).observe(document.documentElement,{childList:true,subtree:true});" +
      '<\/script><script src="' +
      encode(url) +
      "\" onload=\"setTimeout(()=>parent.postMessage({type:'filestream-ad',state:(document.querySelector('iframe,img,video,object') || document.body?.innerText.trim())?'content':'empty'},'*'),60000)\" onerror=\"parent.postMessage({type:'filestream-ad',state:'error'},'*')\"><\/script>";
    placement.append(label, frame);
    placement.classList.add("loaded");
    const fit = () => {
      if (typeof width !== "number") return;
      const scale = Math.min(1, placement.clientWidth / width);
      frame.style.maxWidth = "none";
      frame.style.transformOrigin = "top left";
      frame.style.transform = `scale(${scale})`;
      frame.style.marginBottom = `${height * (scale - 1)}px`;
      frame.style.marginLeft = `${Math.max(0, (placement.clientWidth - width * scale) / 2)}px`;
    };
    new ResizeObserver(fit).observe(placement);
    fit();
  }
  // Choose placements once per page; resizing never initializes the shared unit twice.
  for (const placement of document.querySelectorAll("[data-ad-placement]")) {
    const slot =
      placement.dataset.adPlacement === "top"
        ? device.top_banner
        : mobile
          ? device.bottom_banner
          : device.inline_banner;
    if (!slot || !slot.enabled || !slot.key || !(slot.width > 0 && slot.height > 0)) continue;
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
