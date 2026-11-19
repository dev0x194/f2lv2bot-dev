(() => {
  "use strict";
  const node = document.getElementById("ad-config");
  if (!node) return;
  const config = JSON.parse(node.textContent);
  if (!config.enabled) return;
  if (node.dataset.initialized) return;
  node.dataset.initialized = "true";
  const mode = config.mode || "isolated_documents";
  if (!["isolated_documents", "provider_iframe"].includes(mode)) return;
  if (mode === "provider_iframe" && config.provider_publisher_origin !== location.origin) return;
  const viewport = matchMedia("(max-width: 767px)");
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
    const banner = Boolean(options);
    const provider = options?.providerContract;
    const messageOrigin = banner ? new URL(url).origin : "null";
    if (provider) frame.setAttribute("sandbox", provider.sandbox);
    if (!banner) frame.sandbox = "allow-scripts allow-popups";
    frame.referrerPolicy = provider ? provider.referrer_policy : banner ? "origin" : "no-referrer";
    frame.allow = "camera 'none'; microphone 'none'; geolocation 'none'; payment 'none'; usb 'none'";
    const label = document.createElement("span");
    label.className = "ad-label";
    label.textContent = "Advertisement";
    // Keep initialized documents attached: detaching/reinserting iframes reloads them.
    let active = false;
    let state = "loading";
    const renderState = () => {
      const visible = active && !["error", "empty", "slow-unverified"].includes(state);
      label.hidden = !visible;
      frame.hidden = !visible;
      if (!active) return;
      placement.dataset.adState = state;
      placement.classList.toggle("loaded", visible);
    };
    // Provider script failure/outage is unknown, never evidence of an ad blocker.
    let timeout;
    const fail = (reason) => {
      clearTimeout(timeout);
      // No content yet is provisional. Keep the isolated document and observer
      // alive so a delayed creative can restore the collapsed placement.
      if (reason === "error") frame.remove();
      state = reason;
      renderState();
      console.info("[FileStream ads]", placement.dataset.adPlacement || "social", reason);
      if (reason === "error") window.removeEventListener("message", listener);
    };
    const listener = (event) => {
      if (event.source !== frame.contentWindow || event.origin !== messageOrigin || event.data?.type !== "filestream-ad") return;
      // Diagnostic runtime-error messages are nonterminal: a creative may still work.
      if (event.data.state === "error" || event.data.state === "empty") fail(event.data.state);
      if (event.data.state === "content") {
        clearTimeout(timeout);
        // Content was inserted; cross-origin creative fill remains unverifiable.
        state = "content-unverified";
        renderState();
      }
    };
    if (!provider) window.addEventListener("message", listener);
    else {
      frame.addEventListener("load", () => {
        clearTimeout(timeout);
        // HTTP errors/no-fill can also fire load. Never claim verified content.
        state = "loaded-unverified";
        renderState();
      });
      frame.addEventListener("error", () => fail("error"));
    }
    // A timeout does not prove blocking or no-fill. Late provider content may still appear.
    timeout = setTimeout(() => {
      state = provider ? "slow-loading" : "slow-unverified";
      renderState();
      console.info("[FileStream ads] slow provider; fill unknown");
    }, 30000);
    if (banner) {
      // Regular navigation preserves the provider's origin/referrer behavior.
      // A distinct origin, not a same-origin sandbox exemption, protects the app.
      frame.src = url;
    } else {
      // Optional Social Bar keeps its existing opaque sandbox, independently of banners.
      const encode = (text) =>
        text.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
      // Optional Social Bar remains parser-inserted inside its opaque document.
      frame.srcdoc =
        '<!doctype html><html><head><meta name="referrer" content="no-referrer"><style>body{margin:0;overflow:hidden}</style></head><body><script>' +
        "const report=state=>parent.postMessage({type:'filestream-ad',state},'*');" +
        "const hasContent=()=>{if(document.querySelector('iframe,img,video,object'))return true;if(!document.body)return false;" +
        "const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);let text;while(text=walker.nextNode()){if(text.textContent.trim()&&!text.parentElement.closest('script,style,template,noscript'))return true;}return false;};" +
        "const check=()=>{if(hasContent()) report('content');};" +
        "new MutationObserver(check).observe(document.documentElement,{childList:true,subtree:true});" +
        '<\/script><script src="' +
        encode(url) +
        "\" onload=\"setTimeout(()=>parent.postMessage({type:'filestream-ad',state:hasContent()?'content':'empty'},'*'),60000)\" onerror=\"parent.postMessage({type:'filestream-ad',state:'error'},'*')\"><\/script></body></html>";
    }
    label.hidden = true;
    frame.hidden = true;
    placement.append(label, frame);
    const fit = () => {
      if (!active || !placement.clientWidth || typeof width !== "number") return;
      const scale = Math.min(1, placement.clientWidth / width);
      frame.style.maxWidth = "none";
      frame.style.transformOrigin = "top left";
      frame.style.transform = `scale(${scale})`;
      frame.style.marginBottom = `${height * (scale - 1)}px`;
      frame.style.marginLeft = `${Math.max(0, (placement.clientWidth - width * scale) / 2)}px`;
    };
    new ResizeObserver(fit).observe(placement);
    return {
      activate(value) {
        active = value;
        renderState();
        fit();
      },
    };
  }
  const placements = Array.from(document.querySelectorAll("[data-ad-placement]"),
    (element) => ({ element, units: new Map() }));
  const selectBanners = () => {
    const mobile = viewport.matches;
    const device = mobile ? config.mobile : config.desktop;
    for (const { element, units } of placements) {
      for (const unit of units.values()) unit.activate(false);
      element.classList.remove("loaded");
      element.dataset.adDevice = mobile ? "mobile" : "desktop";
      element.dataset.adState = "disabled";
      const slot = element.dataset.adPlacement === "top"
        ? device.top_banner : mobile ? device.bottom_banner : device.inline_banner;
      if (!slot || !slot.enabled || !slot.key || !(slot.width > 0 && slot.height > 0)) continue;
      const url = safeUrl(slot.document_url);
      if (!url) continue;
      const documentUrl = new URL(url);
      // Reject the main host. Test F uses fixed hashes; Test G needs an exact contract.
      if (documentUrl.hostname === location.hostname) continue;
      let providerContract;
      if (mode === "provider_iframe") {
        providerContract = (config.provider_iframe_contracts || []).find(contract =>
          contract.url === url && contract.width === slot.width && contract.height === slot.height);
        if (!providerContract || documentUrl.hash || documentUrl.pathname.endsWith(".js") ||
            /^\/22\//.test(documentUrl.pathname) ||
            !["origin", "no-referrer"].includes(providerContract.referrer_policy) ||
            !providerContract.sandbox || providerContract.sandbox.split(/\s+/).some(token =>
              !["allow-scripts", "allow-same-origin", "allow-popups"].includes(token))) continue;
      } else if (!/^\/banner\/[a-f0-9]{64}\.html$/.test(documentUrl.pathname) ||
                 documentUrl.search || documentUrl.hash) continue;
      // Identical 300x250 configurations share one document across breakpoints;
      // Test F keeps keyed configurations; Test G identifies zones by approved URL/size.
      const identity = JSON.stringify([mode === "provider_iframe" ? mode : slot.key, url, slot.width, slot.height]);
      if (!units.has(identity)) {
        units.set(identity, mount(element, url, slot.width, slot.height, {
          providerContract,
          key: slot.key,
          format: "iframe",
          height: slot.height,
          width: slot.width,
          params: {},
        }));
      }
      units.get(identity).activate(true);
    }
  };
  selectBanners();
  viewport.addEventListener("change", selectBanners);
  const device = viewport.matches ? config.mobile : config.desktop;
  const social = mode === "isolated_documents" ? safeUrl(device.social_bar_url) : null;
  if (social) {
    const placement = document.createElement("aside");
    placement.className = "ad-placement ad-social";
    placement.setAttribute("aria-label", "Advertisement");
    document.querySelector("main").append(placement);
    mount(placement, social, "100%", 60, null).activate(true);
  }
  // Smartlinks are optional server-rendered anchors, never scripts or media gates.
  // Popunder scripts remain inactive; core actions do not open advertising windows.
})();
