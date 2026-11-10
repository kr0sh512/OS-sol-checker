"use strict";

(() => {
  const storageKey = "os-sol-checker-theme";
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  let preference = null;

  try {
    const saved = localStorage.getItem(storageKey);
    if (saved === "light" || saved === "dark") preference = saved;
  } catch (_) {
    // The selected theme still works for this page when storage is unavailable.
  }

  function currentTheme() {
    return preference || (system.matches ? "dark" : "light");
  }

  function applyTheme() {
    const theme = currentTheme();
    document.documentElement.dataset.theme = theme;
    const toggle = document.getElementById("theme-toggle");
    if (toggle) toggle.setAttribute("aria-pressed", String(theme === "dark"));
  }

  applyTheme();

  system.addEventListener("change", () => {
    if (!preference) applyTheme();
  });

  document.addEventListener("DOMContentLoaded", () => {
    const toggle = document.getElementById("theme-toggle");
    applyTheme();
    toggle.addEventListener("click", () => {
      preference = currentTheme() === "dark" ? "light" : "dark";
      try {
        localStorage.setItem(storageKey, preference);
      } catch (_) {
        // Keep the in-memory preference when persistent storage is blocked.
      }
      applyTheme();
    });
  });
})();
