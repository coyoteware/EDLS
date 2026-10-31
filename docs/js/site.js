/* EDLS site: the day/night toggle and the latest-release fill. Nothing else.
   The page works without this file: dark theme, generic download links. */
(function () {
  "use strict";
  var root = document.documentElement;
  var KEY = "edls-theme";

  function stored() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function store(value) {
    try { localStorage.setItem(KEY, value); } catch (e) { /* private mode: fine */ }
  }

  // ---- Night Sky toggle --------------------------------------------------
  var toggle = document.querySelector(".ed-theme-toggle");
  var themeColor = document.querySelector('meta[name="theme-color"]');

  function sync() {
    var light = root.getAttribute("data-theme") === "light";
    if (toggle) toggle.setAttribute("aria-checked", light ? "true" : "false");
    if (themeColor) themeColor.setAttribute("content", light ? "#D7DBE0" : "#090905");
  }

  if (toggle) {
    toggle.hidden = false;
    toggle.addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
      root.setAttribute("data-theme", next);
      store(next);
      sync();
    });
  }

  // Follow the OS while the visitor hasn't picked a theme here.
  if (window.matchMedia) {
    var mq = window.matchMedia("(prefers-color-scheme: light)");
    var onChange = function (e) {
      var s = stored();
      if (s !== "light" && s !== "dark") {
        root.setAttribute("data-theme", e.matches ? "light" : "dark");
        sync();
      }
    };
    if (mq.addEventListener) mq.addEventListener("change", onChange);
    else if (mq.addListener) mq.addListener(onChange);
  }
  sync();

  // ---- Latest release ----------------------------------------------------
  // GitHub's releases/latest skips drafts and pre-releases, as the app's own
  // update check does. On any failure the static links (releases/latest) stay.
  var API = "https://api.github.com/repos/coyoteware/EDLS/releases/latest";
  var OURS = /^https:\/\/github\.com\/coyoteware\/EDLS\//i;

  function each(selector, fn) {
    Array.prototype.forEach.call(document.querySelectorAll(selector), fn);
  }

  if (!window.fetch) return;
  fetch(API, { headers: { Accept: "application/vnd.github+json" } })
    .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
    .then(function (rel) {
      var tag = String(rel.tag_name || "").replace(/^v/i, "");
      if (!/^\d+(\.\d+){0,3}$/.test(tag)) return;
      var exe = (rel.assets || []).filter(function (a) {
        return /\.exe$/i.test(a.name || "") && OURS.test(a.browser_download_url || "");
      })[0];

      each("[data-release-version]", function (el) {
        el.textContent = tag;
        el.hidden = false;
      });
      each("[data-release-file]", function (el) {
        el.textContent = exe ? exe.name : "EDLS-Setup-" + tag + ".exe";
      });
      if (exe) {
        each("[data-release-download]", function (a) { a.href = exe.browser_download_url; });
      }
      if (OURS.test(rel.html_url || "")) {
        each("[data-release-notes]", function (a) { a.href = rel.html_url; });
      }
    })
    .catch(function () { /* offline or rate-limited: keep the static links */ });
})();
