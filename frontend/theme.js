(function () {
  "use strict";

  var STORAGE_KEY = "imgLab.theme.v1";
  var MODES = ["auto", "light", "dark"];
  var mediaQuery = null;

  function safeRead(storage, key) {
    try {
      return storage ? storage.getItem(key) : null;
    } catch (error) {
      return null;
    }
  }

  function storedMode() {
    var value = safeRead(window.localStorage, STORAGE_KEY);
    return MODES.indexOf(value) >= 0 ? value : "auto";
  }

  function parentMode() {
    try {
      var value = window.parent.localStorage.getItem("themeMode");
      if (value === "light" || value === "dark") {
        return value;
      }
      if (value === "auto" || value === "system") {
        return "auto";
      }
    } catch (error) {
      return "";
    }
    return "";
  }

  function systemPrefersDark() {
    try {
      return Boolean(
        window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
      );
    } catch (error) {
      return false;
    }
  }

  function resolveMode(mode) {
    if (mode === "light" || mode === "dark") {
      return mode;
    }
    var inherited = parentMode();
    if (inherited === "light" || inherited === "dark") {
      return inherited;
    }
    return systemPrefersDark() ? "dark" : "light";
  }

  function apply(mode) {
    var safeMode = MODES.indexOf(mode) >= 0 ? mode : "auto";
    var resolved = resolveMode(safeMode);
    document.documentElement.setAttribute("data-theme", resolved);
    document.documentElement.setAttribute("data-theme-mode", safeMode);
    try {
      window.dispatchEvent(
        new CustomEvent("imglab:themechange", {
          detail: { mode: safeMode, resolved: resolved },
        })
      );
    } catch (error) {
      /* CustomEvent is unavailable only in very old browsers. */
    }
  }

  function setMode(mode) {
    var safeMode = MODES.indexOf(mode) >= 0 ? mode : "auto";
    try {
      window.localStorage.setItem(STORAGE_KEY, safeMode);
    } catch (error) {
      /* Private browsing may reject writes; the current page still updates. */
    }
    apply(safeMode);
  }

  function cycle() {
    var current = storedMode();
    var next = MODES[(MODES.indexOf(current) + 1) % MODES.length];
    setMode(next);
    return next;
  }

  function refresh() {
    apply(storedMode());
  }

  function label(mode) {
    if (mode === "light") {
      return "浅色主题";
    }
    if (mode === "dark") {
      return "深色主题";
    }
    return "跟随系统主题";
  }

  window.ImgLabTheme = {
    getMode: storedMode,
    setMode: setMode,
    cycle: cycle,
    refresh: refresh,
    label: label,
    MODES: MODES.slice(),
  };

  apply(storedMode());

  try {
    mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
    if (mediaQuery && mediaQuery.addEventListener) {
      mediaQuery.addEventListener("change", refresh);
    } else if (mediaQuery && mediaQuery.addListener) {
      mediaQuery.addListener(refresh);
    }
  } catch (error) {
    /* matchMedia is optional. */
  }

  window.addEventListener("storage", function (event) {
    if (!event.key || event.key === STORAGE_KEY || event.key === "themeMode") {
      refresh();
    }
  });
})();
