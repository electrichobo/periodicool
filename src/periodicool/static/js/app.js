// PeriodiCool page script. Plain JS, no build step.
// Effects switch, CSRF header for htmx, modal, toasts, confirm dialog,
// table sort / filter / columns / bulk select, and tabs.
(function () {
  "use strict";

  var root = document.documentElement;

  function store(key, value) {
    try {
      if (value === undefined) return window.localStorage.getItem(key);
      window.localStorage.setItem(key, value);
    } catch (e) {
      return null; // storage blocked: features still work for this page
    }
    return null;
  }

  // ---- Effects ----
  var EFFECTS = "periodicool.effects";
  root.setAttribute("data-effects", store(EFFECTS) || "on");

  // ---- CSRF for htmx ----
  document.addEventListener("htmx:configRequest", function (event) {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) event.detail.headers["X-CSRF-Token"] = meta.getAttribute("content");
  });

  // ---- Toasts ----
  function toast(kind, text) {
    var box = document.getElementById("toasts");
    if (!box || !text) return;
    var el = document.createElement("div");
    el.className = "crt toast";
    el.setAttribute("role", kind === "bad" ? "alert" : "status");
    var led = document.createElement("span");
    led.className = "led led--" + (kind || "info");
    led.textContent = { ok: "Done", bad: "Error", warn: "Note", info: "Info" }[kind] || "Info";
    var msg = document.createElement("span");
    msg.className = "toast__text";
    msg.textContent = text;
    var close = document.createElement("button");
    close.className = "toast__close";
    close.type = "button";
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "✕";
    close.addEventListener("click", function () { el.remove(); });
    el.append(led, msg, close);
    box.append(el);
    if (kind !== "bad") window.setTimeout(function () { el.remove(); }, 7000);
  }
  window.periodicoolToast = toast;
  // Server actions send HX-Trigger: {"toast": {"kind": "ok", "text": "..."}}
  document.addEventListener("toast", function (event) {
    var d = event.detail || {};
    toast(d.kind, d.text);
  });

  // ---- Modal ----
  var lastTrigger = null;
  function modal() { return document.getElementById("modal"); }
  document.addEventListener("htmx:beforeRequest", function (event) {
    var target = event.detail.target;
    if (target && target.id === "modal-body") lastTrigger = event.detail.elt;
  });
  document.addEventListener("htmx:afterSwap", function (event) {
    if (event.detail.target && event.detail.target.id === "modal-body") {
      var dlg = modal();
      if (dlg && !dlg.open) dlg.showModal();
      var first = dlg && dlg.querySelector("[autofocus], .modal__title");
      if (first) {
        if (!first.hasAttribute("tabindex") && first.matches(".modal__title")) first.setAttribute("tabindex", "-1");
        first.focus();
      }
      init(event.detail.target);
    } else {
      init(event.detail.target);
    }
  });
  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-close-modal]")) {
      var dlg = modal();
      if (dlg) dlg.close();
    }
  });
  document.addEventListener("closeModal", function () {
    var dlg = modal();
    if (dlg && dlg.open) dlg.close();
  });

  // ---- Confirm dialog (replaces window.confirm for hx-confirm) ----
  document.addEventListener("htmx:confirm", function (event) {
    var question = event.detail.question;
    if (!question) return;
    event.preventDefault();
    var dlg = document.getElementById("confirm");
    var typed = event.detail.elt.getAttribute("data-confirm-type");
    dlg.querySelector(".confirm__text").textContent = question;
    var typedBox = dlg.querySelector(".confirm__typed");
    var input = dlg.querySelector(".confirm__input");
    var ok = dlg.querySelector(".confirm__ok");
    typedBox.hidden = !typed;
    input.value = "";
    dlg.querySelector(".confirm__word").textContent = typed || "";
    ok.disabled = Boolean(typed);
    input.oninput = function () { ok.disabled = typed && input.value !== typed; };
    var trigger = event.detail.elt;
    ok.onclick = function () {
      dlg.close();
      event.detail.issueRequest(true);
    };
    dlg.onclose = function () { if (trigger && trigger.focus) trigger.focus(); };
    dlg.showModal();
    (typed ? input : ok).focus();
  });

  // ---- Tables ----
  function cellValue(row, index) {
    var cell = row.cells[index];
    if (!cell) return "";
    var v = cell.getAttribute("data-sort");
    return v !== null ? v : cell.textContent.trim().toLowerCase();
  }

  function initTable(table) {
    if (table.dataset.ready) return;
    table.dataset.ready = "1";
    var id = table.id;
    var heads = Array.prototype.slice.call(table.tHead ? table.tHead.rows[0].cells : []);

    // Sorting
    heads.forEach(function (th, index) {
      var btn = th.querySelector("button.sort");
      if (!btn) return;
      btn.addEventListener("click", function () {
        var dir = th.getAttribute("aria-sort") === "ascending" ? "descending" : "ascending";
        heads.forEach(function (h) { h.removeAttribute("aria-sort"); });
        th.setAttribute("aria-sort", dir);
        var body = table.tBodies[0];
        var rows = Array.prototype.slice.call(body.rows);
        var numeric = th.hasAttribute("data-numeric");
        rows.sort(function (a, b) {
          var x = cellValue(a, index), y = cellValue(b, index);
          if (numeric) { x = parseFloat(x) || 0; y = parseFloat(y) || 0; }
          var c = x < y ? -1 : x > y ? 1 : 0;
          return dir === "ascending" ? c : -c;
        });
        rows.forEach(function (r) { body.appendChild(r); });
      });
    });

    // Column chooser
    if (id) {
      var hidden = JSON.parse(store("periodicool.cols." + id) || "[]");
      applyColumns(table, hidden);
      document.querySelectorAll('[data-columns-for="' + id + '"] input[data-col]').forEach(function (box) {
        box.checked = hidden.indexOf(box.getAttribute("data-col")) === -1;
        box.addEventListener("change", function () {
          var off = [];
          document.querySelectorAll('[data-columns-for="' + id + '"] input[data-col]').forEach(function (b) {
            if (!b.checked) off.push(b.getAttribute("data-col"));
          });
          store("periodicool.cols." + id, JSON.stringify(off));
          applyColumns(table, off);
        });
      });
    }

    // Bulk select
    var all = table.querySelector("input.select-all");
    var bar = id && document.querySelector('[data-bulk-for="' + id + '"]');
    function updateBar() {
      var picked = table.querySelectorAll("input.row-select:checked");
      table.querySelectorAll("input.row-select").forEach(function (b) {
        b.closest("tr").classList.toggle("is-selected", b.checked);
      });
      if (bar) {
        bar.hidden = picked.length === 0;
        var count = bar.querySelector(".bulkbar__count");
        if (count) count.textContent = picked.length + " selected";
      }
    }
    if (all) {
      all.addEventListener("change", function () {
        table.querySelectorAll("tbody tr:not([hidden]) input.row-select").forEach(function (b) { b.checked = all.checked; });
        updateBar();
      });
    }
    table.addEventListener("change", function (e) { if (e.target.matches("input.row-select")) updateBar(); });
    updateBar();
  }

  function applyColumns(table, hidden) {
    var heads = table.tHead ? table.tHead.rows[0].cells : [];
    Array.prototype.forEach.call(heads, function (th, index) {
      var name = th.getAttribute("data-col");
      var off = name && hidden.indexOf(name) !== -1;
      Array.prototype.forEach.call(table.rows, function (row) {
        var cell = row.cells[index];
        if (cell) cell.toggleAttribute("data-hidden-col", Boolean(off));
      });
    });
  }

  // Text and attribute filters: <input data-filter-for="list-id">,
  // <select data-filter-for="list-id" data-filter-attr="state">. The rows are elements
  // marked data-filter-row="list-id" (so cards, table and overview filter together),
  // or else the rows of the table with that id. Attribute values hold tokens split by
  // spaces, or by "|" when they start with one (tags may contain spaces).
  document.addEventListener("input", function (e) {
    var id = e.target.getAttribute && e.target.getAttribute("data-filter-for");
    if (id) filterTable(id);
  });
  document.addEventListener("change", function (e) {
    var id = e.target.getAttribute && e.target.getAttribute("data-filter-for");
    if (id) filterTable(id);
  });
  function tokens(value) {
    value = (value || "").toLowerCase();
    return value.charAt(0) === "|" ? value.split("|") : value.split(/\s+/);
  }
  function filterTable(id) {
    var rows = document.querySelectorAll('[data-filter-row="' + id + '"]');
    if (!rows.length) {
      var table = document.getElementById(id);
      if (!table) return;
      rows = table.tBodies[0].rows;
    }
    var controls = document.querySelectorAll('[data-filter-for="' + id + '"]');
    Array.prototype.forEach.call(rows, function (row) {
      var show = true;
      controls.forEach(function (c) {
        var v = (c.value || "").trim().toLowerCase();
        if (!v) return;
        var attr = c.getAttribute("data-filter-attr");
        if (attr) {
          if (tokens(row.getAttribute("data-" + attr)).indexOf(v) === -1) show = false;
        } else if (row.textContent.toLowerCase().indexOf(v) === -1) {
          show = false;
        }
      });
      row.hidden = !show;
    });
  }

  // ---- Tabs ----
  function initTabs(list) {
    if (list.dataset.ready) return;
    list.dataset.ready = "1";
    var tabs = Array.prototype.slice.call(list.querySelectorAll('[role="tab"]'));
    function select(tab) {
      tabs.forEach(function (t) {
        var on = t === tab;
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.tabIndex = on ? 0 : -1;
        var panel = document.getElementById(t.getAttribute("aria-controls"));
        if (panel) panel.hidden = !on;
      });
      tab.focus();
    }
    tabs.forEach(function (tab, i) {
      tab.addEventListener("click", function () { select(tab); });
      tab.addEventListener("keydown", function (e) {
        var next = null;
        if (e.key === "ArrowRight") next = tabs[(i + 1) % tabs.length];
        if (e.key === "ArrowLeft") next = tabs[(i - 1 + tabs.length) % tabs.length];
        if (e.key === "Home") next = tabs[0];
        if (e.key === "End") next = tabs[tabs.length - 1];
        if (next) { e.preventDefault(); select(next); }
      });
    });
  }

  // ---- View switcher: cards / table / overview, remembered per page ----
  function showView(box, view) {
    box.setAttribute("data-view", view);
    document.querySelectorAll('[data-view-switch][aria-controls="' + box.id + '"]').forEach(function (b) {
      b.setAttribute("aria-pressed", b.getAttribute("data-view-switch") === view ? "true" : "false");
    });
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-view-switch]");
    if (!btn) return;
    var box = document.getElementById(btn.getAttribute("aria-controls"));
    if (!box) return;
    showView(box, btn.getAttribute("data-view-switch"));
    if (box.dataset.remember) store(box.dataset.remember, btn.getAttribute("data-view-switch"));
  });

  // "Use" buttons in lookup suggestions: copy a value into a form field.
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-fill]");
    if (!btn) return;
    var field = document.getElementById(btn.getAttribute("data-fill"));
    if (!field) return;
    var value = btn.getAttribute("data-value") || "";
    if (btn.hasAttribute("data-append") && field.value.trim()) {
      var parts = field.value.split(",").map(function (p) { return p.trim().toLowerCase(); });
      if (parts.indexOf(value.toLowerCase()) === -1) field.value = field.value.replace(/\s*$/, "") + ", " + value;
    } else {
      field.value = value;
    }
    field.dispatchEvent(new Event("change", { bubbles: true }));
    btn.setAttribute("aria-pressed", "true");
  });

  // Console: follow the newest line, unless the reader scrolled up to look back.
  var consoleBox = null;
  var consoleFollow = true;
  document.addEventListener("htmx:beforeSwap", function (e) {
    if (e.detail.target && e.detail.target.id === "console-lines") {
      consoleBox = e.detail.target.parentElement;
      consoleFollow = consoleBox.scrollHeight - consoleBox.scrollTop - consoleBox.clientHeight < 40;
    }
  });
  document.addEventListener("htmx:afterSwap", function (e) {
    if (e.detail.target && e.detail.target.id === "console-lines" && consoleFollow) {
      consoleBox.scrollTop = consoleBox.scrollHeight;
    }
  });
  document.addEventListener("DOMContentLoaded", function () {
    var box = document.querySelector(".console");
    if (box) box.scrollTop = box.scrollHeight;
  });

  function init(scope) {
    (scope || document).querySelectorAll("[data-remember]").forEach(function (box) {
      var saved = store(box.dataset.remember);
      if (saved) showView(box, saved);
    });
    (scope || document).querySelectorAll("table.table").forEach(initTable);
    (scope || document).querySelectorAll('[role="tablist"]').forEach(initTabs);
  }

  document.addEventListener("DOMContentLoaded", function () {
    var box = document.getElementById("effects-switch");
    if (box) {
      box.checked = root.getAttribute("data-effects") === "on";
      box.addEventListener("change", function () {
        var v = box.checked ? "on" : "off";
        root.setAttribute("data-effects", v);
        store(EFFECTS, v);
      });
    }
    document.querySelectorAll("[data-toast]").forEach(function (el) {
      toast(el.getAttribute("data-toast-kind"), el.getAttribute("data-toast"));
    });
    init(document);
  });
})();
