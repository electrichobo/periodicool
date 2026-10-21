// PeriodiCool page script: effects switch and the CSRF header for htmx.
(function () {
  "use strict";

  var root = document.documentElement;
  var KEY = "periodicool.effects";

  function readEffects() {
    try {
      return window.localStorage.getItem(KEY) || "on";
    } catch (e) {
      return "on";
    }
  }

  function setEffects(value) {
    root.setAttribute("data-effects", value);
    try {
      window.localStorage.setItem(KEY, value);
    } catch (e) {
      /* storage blocked: the switch still works for this page */
    }
    var box = document.getElementById("effects-switch");
    if (box) box.checked = value === "on";
  }

  setEffects(readEffects());

  document.addEventListener("DOMContentLoaded", function () {
    var box = document.getElementById("effects-switch");
    if (box) {
      box.checked = root.getAttribute("data-effects") === "on";
      box.addEventListener("change", function () {
        setEffects(box.checked ? "on" : "off");
      });
    }
  });

  // Every htmx request carries the CSRF token from the page.
  document.addEventListener("htmx:configRequest", function (event) {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) event.detail.headers["X-CSRF-Token"] = meta.getAttribute("content");
  });
})();
