(() => {
  "use strict";
  const notice = document.getElementById("adblock-notice");
  if (!notice) return;
  let blocked = false;
  let bypass = false;
  window.filestreamAccessAllowed = () => !blocked || bypass;
  const detect = () => {
    const bait = document.createElement("div");
    bait.className = "adsbox ad-banner adsbygoogle";
    bait.setAttribute("aria-hidden", "true");
    bait.style.cssText =
      "position:absolute;left:-10000px;width:10px;height:10px;pointer-events:none";
    document.body.append(bait);
    setTimeout(() => {
      blocked =
        getComputedStyle(bait).display === "none" ||
        bait.getBoundingClientRect().height === 0;
      bait.remove();
      notice.hidden = !blocked;
    }, 150);
  };
  document.getElementById("adblock-retry").addEventListener("click", () => {
    bypass = false;
    detect();
  });
  document.getElementById("adblock-continue").addEventListener("click", () => {
    bypass = true;
    notice.hidden = true;
  });
  document.querySelectorAll("[data-folder-file]").forEach((a) =>
    a.addEventListener("click", (event) => {
      if (blocked && !bypass) {
        event.preventDefault();
        notice.hidden = false;
        notice.scrollIntoView({ block: "center" });
        document.getElementById("adblock-continue").focus();
      }
    }),
  );
  detect();
})();
