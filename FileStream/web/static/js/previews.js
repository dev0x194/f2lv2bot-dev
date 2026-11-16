(() => {
  "use strict";
  let cleanup = () => {};
  window.filestreamPreviews = async (player, url) => {
    cleanup();
    try {
      const target = new URL(url, location.href);
      if (target.origin !== location.origin) return;
      const response = await fetch(target, {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!response.ok) return;
      const text = await response.text();
      const time = (s) => s.split(":").reduce((a, v) => a * 60 + Number(v), 0);
      const cues = [];
      for (const block of text.split(/\r?\n\r?\n/)) {
        const lines = block.trim().split(/\r?\n/);
        const i = lines.findIndex((line) => line.includes(" --> "));
        if (i < 0 || !lines[i + 1]) continue;
        const [from, to] = lines[i].split(" --> ");
        const match = lines[i + 1].match(
          /^(.*?)#xywh=(\d+),(\d+),(\d+),(\d+)$/,
        );
        if (!match) continue;
        const image = new URL(match[1], target);
        if (image.origin !== location.origin) continue;
        cues.push({
          from: time(from),
          to: time(to),
          url: image.href,
          x: +match[2],
          y: +match[3],
          w: +match[4],
          h: +match[5],
        });
      }
      if (!cues.length) return;
      const progress = player
        .getChild("controlBar")
        .getChild("progressControl")
        .el();
      const preview = document.createElement("div");
      preview.className = "scrub-preview";
      preview.setAttribute("aria-hidden", "true");
      preview.hidden = true;
      progress.append(preview);
      const show = (event) => {
        const box = progress.getBoundingClientRect();
        const ratio = Math.max(
          0,
          Math.min(1, (event.clientX - box.left) / box.width),
        );
        const cue = cues.find(
          (c) =>
            c.from <= ratio * player.duration() &&
            c.to > ratio * player.duration(),
        );
        if (!cue) {
          preview.hidden = true;
          return;
        }
        preview.style.width = cue.w + "px";
        preview.style.height = cue.h + "px";
        preview.style.backgroundImage =
          'url("' + cue.url.replace(/"/g, "%22") + '")';
        preview.style.backgroundPosition = "-" + cue.x + "px -" + cue.y + "px";
        preview.style.left =
          Math.max(
            0,
            Math.min(box.width - cue.w, ratio * box.width - cue.w / 2),
          ) + "px";
        preview.hidden = false;
      };
      const hide = () => (preview.hidden = true);
      progress.addEventListener("pointermove", show);
      progress.addEventListener("pointerleave", hide);
      cleanup = () => {
        progress.removeEventListener("pointermove", show);
        progress.removeEventListener("pointerleave", hide);
        preview.remove();
      };
    } catch (_) {}
  };
})();
