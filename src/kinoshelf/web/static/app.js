// Kinoshelf pages: pick a match, decide, search, and settings that save by themselves.
(function () {
  "use strict";
  var csrf = (document.querySelector('meta[name="csrf"]') || {}).content || "";

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(body || {}),
      credentials: "same-origin"
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) { throw new Error(j.error || ("Error " + r.status)); }
        return j;
      });
    });
  }
  function say(el, text, cls) {
    if (!el) { return; }
    el.textContent = text;
    el.classList.remove("ok", "err");
    if (cls) { el.classList.add(cls); }
  }

  // ---- review page
  var item = document.getElementById("item");
  if (item) {
    var id = item.getAttribute("data-item");
    var status = document.getElementById("status");
    var picked = 0;
    var bindCards = function () {
      var cards = item.querySelectorAll(".card");
      picked = cards.length ? Number(cards[0].getAttribute("data-index")) : -1;
      if (cards.length) { document.getElementById("dest").textContent = cards[0].getAttribute("data-dest"); }
      cards.forEach(function (c) {
        c.addEventListener("click", function () {
          cards.forEach(function (x) { x.classList.remove("on"); x.setAttribute("aria-checked", "false"); });
          c.classList.add("on");
          c.setAttribute("aria-checked", "true");
          picked = Number(c.getAttribute("data-index"));
          document.getElementById("dest").textContent = c.getAttribute("data-dest");
        });
      });
    };
    bindCards();

    item.querySelectorAll("[data-act]").forEach(function (b) {
      b.addEventListener("click", function () {
        var act = b.getAttribute("data-act");
        var body = {};
        if (act === "approve") {
          if (picked < 0) { say(status, "Pick a match first, or search for it.", "err"); return; }
          var g = document.getElementById("group"), r = document.getElementById("replace");
          body = { index: picked, group: g ? g.checked : false, replace: r ? r.checked : false };
        }
        item.querySelectorAll("[data-act]").forEach(function (x) { x.disabled = true; });
        say(status, act === "approve" ? "Moving it under its new name…" : "Saving…");
        post("/api/items/" + id + "/" + act, body).then(function (j) {
          window.location.href = j.next || "/review";
        }).catch(function (e) {
          say(status, e.message, "err");
          item.querySelectorAll("[data-act]").forEach(function (x) { x.disabled = false; });
        });
      });
    });

    var form = document.getElementById("search");
    if (form) {
      form.addEventListener("submit", function (ev) {
        ev.preventDefault();
        var q = form.querySelector('[name="q"]').value, src = form.querySelector('[name="source"]').value;
        var box = document.getElementById("cards");
        box.setAttribute("aria-busy", "true");
        fetch("/api/items/" + id + "/search?q=" + encodeURIComponent(q) + "&source=" + encodeURIComponent(src),
          { credentials: "same-origin" })
          .then(function (r) { return r.text(); })
          .then(function (html) { box.innerHTML = html; box.removeAttribute("aria-busy"); bindCards(); })
          .catch(function () { box.removeAttribute("aria-busy"); say(status, "Search failed", "err"); });
      });
    }
  }

  // ---- buttons that POST (week page, settings)
  document.querySelectorAll("[data-post]").forEach(function (b) {
    b.addEventListener("click", function () {
      var msg = b.getAttribute("data-confirm");
      if (msg && !window.confirm(msg)) { return; }
      var out = b.closest(".panel, section, form");
      out = out ? out.querySelector(".status, #status") : null;
      b.disabled = true;
      var body = b.getAttribute("data-body");
      post(b.getAttribute("data-post"), body ? JSON.parse(body) : {}).then(function (j) {
        var text = j.note || (j.run_id ? ((j.dry_run ? "Dry run: " + j.moved + " would move" : j.moved + " moved") +
          ", " + j.problems + " held back") : (j.done !== undefined ? j.done + " steps put back" : "Done"));
        say(out, text, "ok");
        if (b.hasAttribute("data-reload")) { window.setTimeout(function () { window.location.reload(); }, 900); }
      }).catch(function (e) { say(out, e.message, "err"); }).then(function () { b.disabled = false; });
    });
  });

  // ---- settings: every change saves by itself
  function collect(form) {
    var section = form.getAttribute("data-section");
    var body = {};
    if (section === "paths") {
      body.map = [];
      form.querySelectorAll("[data-row]").forEach(function (row) {
        body.map.push({
          shown: row.querySelector('[name="shown"]').value,
          container: row.querySelector('[name="container"]').value,
          label: row.querySelector('[name="label"]').value
        });
      });
      body.inbox = form.querySelector('[name="inbox"]').value;
      body.set_aside = form.querySelector('[name="set_aside"]').value;
      return body;
    }
    form.querySelectorAll("input, select, textarea").forEach(function (el) {
      if (!el.name) { return; }
      var v = el.type === "checkbox" ? el.checked : el.value;
      if (form.hasAttribute("data-nested") && el.name.indexOf(".") > 0) {
        var p = el.name.split(".");
        body[p[0]] = body[p[0]] || {};
        body[p[0]][p[1]] = v;
      } else {
        body[el.name] = v;
      }
    });
    return body;
  }
  document.querySelectorAll("form[data-section]").forEach(function (form) {
    var timer = null;
    var out = form.querySelector(".status");
    var save = function () {
      say(out, "Saving…");
      post("/api/settings/" + form.getAttribute("data-section"), collect(form)).then(function (j) {
        say(out, j.note || "Saved", j.note ? "err" : "ok");
        form.querySelectorAll('input[type="password"]').forEach(function (p) {
          if (p.value) { p.value = ""; p.placeholder = "saved"; }
        });
        if (j.preview) {
          Object.keys(j.preview).forEach(function (k) {
            var el = document.querySelector('[data-preview="' + k + '"]');
            if (el) { el.textContent = j.preview[k].text; el.classList.toggle("warn", !j.preview[k].ok); }
          });
        }
      }).catch(function (e) { say(out, e.message, "err"); });
    };
    form.addEventListener("submit", function (ev) { ev.preventDefault(); save(); });
    form.addEventListener("change", function () { window.clearTimeout(timer); timer = window.setTimeout(save, 250); });
    form.addEventListener("input", function (ev) {
      if (ev.target.type === "password") { return; }
      window.clearTimeout(timer); timer = window.setTimeout(save, 1200);
    });
    form.querySelectorAll("[data-test]").forEach(function (b) {
      b.addEventListener("click", function () {
        b.disabled = true;
        say(out, "Testing…");
        post("/api/test/" + b.getAttribute("data-test")).then(function (j) { say(out, j.note, "ok"); })
          .catch(function (e) { say(out, e.message, "err"); }).then(function () { b.disabled = false; });
      });
    });
  });
  var acct = document.querySelector("form[data-account]");
  if (acct) {
    acct.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var out = acct.querySelector(".status");
      post("/api/account/password", {
        current: acct.querySelector('[name="current"]').value, "new": acct.querySelector('[name="new"]').value
      }).then(function (j) { say(out, j.note, "ok"); window.setTimeout(function () { window.location.href = "/login"; }, 1200); })
        .catch(function (e) { say(out, e.message, "err"); });
    });
  }
})();
