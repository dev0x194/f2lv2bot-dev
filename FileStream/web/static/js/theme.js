(() => {
  "use strict";
  const media = matchMedia("(prefers-color-scheme: dark)");
  let preference = "dark";
  try {
    preference = localStorage.getItem("filestream-theme") || "dark";
  } catch (_) {}
  if (!["system", "light", "dark"].includes(preference)) preference = "system";
  const apply = () => {
    document.documentElement.dataset.theme =
      preference === "system" ? (media.matches ? "dark" : "light") : preference;
  };
  apply();
  media.addEventListener("change", apply);
  addEventListener("DOMContentLoaded", () => {
    const select = document.getElementById("theme");
    select.value = preference;
    select.addEventListener("change", () => {
      preference = select.value;
      try {
        localStorage.setItem("filestream-theme", preference);
      } catch (_) {}
      apply();
    });
  });
  addEventListener("storage", (e) => {
    if (e.key === "filestream-theme") {
      preference = ["light", "dark"].includes(e.newValue)
        ? e.newValue
        : "system";
      apply();
      const select = document.getElementById("theme");
      if (select) select.value = preference;
    }
  });
})();
