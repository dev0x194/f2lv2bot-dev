"use strict";
// Remember the source folder locally; only restore a same-origin public folder URL.
document.querySelectorAll("[data-folder-file]").forEach((a) =>
  a.addEventListener("click", () => {
    try {
      sessionStorage.setItem(
        "filestream-folder",
        JSON.stringify({
          file: new URL(a.href).pathname,
          folder: location.pathname,
        }),
      );
    } catch (_) {}
  }),
);
if (location.pathname.startsWith("/gen/")) {
  try {
    const entry = JSON.parse(sessionStorage.getItem("filestream-folder"));
    if (
      entry &&
      entry.file === location.pathname &&
      /^\/gfolder\/[A-Za-z0-9_-]+$/.test(entry.folder)
    ) {
      const a = document.createElement("a");
      a.href = entry.folder;
      a.textContent = "← Back to Folder";
      document.getElementById("main").prepend(a);
    }
  } catch (_) {}
}
