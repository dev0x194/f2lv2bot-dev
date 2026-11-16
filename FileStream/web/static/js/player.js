(() => {
  "use strict";
  const node = document.getElementById("file-config");
  if (!node) return;
  const config = JSON.parse(node.textContent);
  const status = document.getElementById("action-status");
  let player,
    lastAction,
    refreshes = 0;
  const resumeKey = "filestream-position:" + encodeURIComponent(config.id);
  const retry = document.getElementById("retry-action");
  async function issue(kind) {
    const response = await fetch(
      kind === "download" ? config.downloadPath : config.streamPath,
      {
        credentials: "same-origin",
        headers: { "X-Page-Token": config.pageToken },
        cache: "no-store",
      },
    );
    if (!response.ok)
      throw Error(
        response.status === 403
          ? "Access expired. Reload this share page to renew it."
          : response.status === 404
            ? "This file is no longer available."
            : "The service could not prepare this file. Please retry.",
      );
    const data = await response.json();
    if (data.success !== true)
      throw Error(data.error || "The service could not prepare this file.");
    const url = new URL(
      kind === "download" ? data.download_url : data.stream_url,
      location.origin,
    );
    if (
      url.origin !== location.origin ||
      !/^\/(stream|file)\//.test(url.pathname)
    )
      throw Error("The service returned an unexpected media URL.");
    return data;
  }
  async function play() {
    if (typeof videojs !== "function")
      throw Error(
        "The player could not load. Retry, download the file, or open it in VLC.",
      );
    const data = await issue("stream");
    document.getElementById("player-region").hidden = false;
    if (!player) {
      player = videojs("player", {
        controls: true,
        fluid: true,
        playbackRates: [0.5, 1, 1.25, 1.5, 2],
        enableDocumentPictureInPicture: true,
      });
      let saved = 0;
      try {
        saved = Number(localStorage.getItem(resumeKey)) || 0;
      } catch (_) {}
      player.on("loadedmetadata", () => {
        if (saved > 5 && saved < player.duration() - 5) {
          player.currentTime(saved);
          saved = 0;
        }
      });
      let lastSave = 0;
      player.on("timeupdate", () => {
        if (Date.now() - lastSave < 5000) return;
        lastSave = Date.now();
        try {
          localStorage.setItem(resumeKey, String(player.currentTime()));
        } catch (_) {}
      });
      player.on("ended", () => {
        try {
          localStorage.removeItem(resumeKey);
        } catch (_) {}
      });
      player.on("error", () => {
        status.textContent =
          "Playback failed. The codec may be unsupported or the link expired. Retry playback, download, or use VLC.";
        retry.hidden = false;
        if (refreshes++ < 1) setTimeout(() => run("play"), 500);
      });
    }
    if (data.thumbnails_url && window.filestreamPreviews)
      window.filestreamPreviews(player, data.thumbnails_url);
    const position = player.currentTime() || 0;
    player.error(null);
    player.src({ src: data.stream_url, type: data.mime_type || config.mime });
    if (position)
      player.one("loadedmetadata", () => player.currentTime(position));
    await player.play();
  }
  async function run(action) {
    lastAction = action;
    retry.hidden = true;
    status.textContent = "Preparing…";
    try {
      if (
        ["play", "download", "vlc"].includes(action) &&
        window.filestreamAccessAllowed &&
        !window.filestreamAccessAllowed()
      ) {
        status.textContent =
          "Review the ad blocker notice below, then retry or continue.";
        return;
      }
      if (action === "share") {
        if (navigator.share)
          await navigator.share({
            title: document.title,
            url: config.shareUrl,
          });
        else {
          try {
            await navigator.clipboard.writeText(config.shareUrl);
            status.textContent = "Share link copied.";
          } catch (_) {
            document.getElementById("share-fallback").hidden = false;
            document.getElementById("share-url").select();
            status.textContent = "Select and copy the public share link.";
          }
          return;
        }
      } else if (action === "download") {
        const data = await issue("download");
        location.assign(data.download_url);
      } else if (action === "play") {
        await play();
      } else if (action === "vlc") {
        const data = await issue("stream");
        const fallback = document.getElementById("external-fallback");
        fallback.hidden = false;
        document.getElementById("external-url").value = data.stream_url;
        document.getElementById("launch-vlc").href = "vlc://" + data.stream_url;
        document.getElementById("external-url").focus();
      }
      status.textContent =
        action === "download"
          ? "Download prepared. Your browser will start it."
          : "";
    } catch (error) {
      if (error.name === "AbortError") {
        status.textContent = "Sharing cancelled.";
        return;
      }
      status.textContent = error.message;
      retry.hidden = false;
    }
  }
  document
    .querySelectorAll("[data-action]")
    .forEach((button) =>
      button.addEventListener("click", () => run(button.dataset.action)),
    );
  retry.addEventListener("click", () => run(lastAction));
  document.getElementById("copy-stream").addEventListener("click", async () => {
    const input = document.getElementById("external-url");
    try {
      await navigator.clipboard.writeText(input.value);
      status.textContent = "Stream URL copied. It expires after 30 minutes.";
    } catch (_) {
      input.select();
      status.textContent = "Select and copy the stream URL.";
    }
  });
})();
