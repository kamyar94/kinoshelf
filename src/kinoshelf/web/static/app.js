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
  document.querySelectorAll("form[data-section]:not([data-net])").forEach(function (form) {
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
  // ---- settings: Network. Secrets are write-only: after a save the page only learns "set" or "not set".
  var net = document.querySelector("form[data-net]");
  if (net) {
    var netOut = net.querySelector(".status");
    var netTimer = null;
    var clear = [];
    var mode = function () {
      var m = net.querySelector('input[name="mode"]:checked');
      return m ? m.value : "direct";
    };
    var showMode = function () {
      var m = mode();
      net.querySelectorAll("[data-when]").forEach(function (el) { el.hidden = el.getAttribute("data-when") !== m; });
      net.querySelector("fieldset.uses").disabled = m === "direct";
    };
    var showSecret = function (box, isSet) {
      box.setAttribute("data-set", isSet ? "yes" : "no");
      box.querySelector(".secret-state").hidden = !isSet;
      box.querySelector(".secret-edit").hidden = isSet;
      box.querySelector("[data-keep]").hidden = !isSet;
      var inp = box.querySelector("input");
      inp.value = "";
      inp.placeholder = box.getAttribute("data-secret") === "address" ? "proxy.example.invalid:8080" :
        (isSet ? "Type the new one" : "Not set");
    };
    var netBody = function () {
      var body = { mode: mode(), use: {}, clear: clear.slice() };
      var k = net.querySelector('input[name="kind"]:checked');
      body.kind = k ? k.value : "http";
      net.querySelectorAll('input[name^="use."]').forEach(function (c) { body.use[c.name.slice(4)] = c.checked; });
      body.timeout_seconds = net.querySelector('[name="timeout_seconds"]').value;
      net.querySelectorAll("[data-secret-input]").forEach(function (inp) { if (inp.value.trim()) { body[inp.name] = inp.value; } });
      return body;
    };
    var netSave = function () {
      window.clearTimeout(netTimer);
      netTimer = null;
      say(netOut, "Saving…");
      var body = netBody();
      clear = [];
      return post("/api/settings/network", body).then(function (j) {
        say(netOut, j.note || "Saved", j.note ? "err" : "ok");
        if (j.set) {
          net.querySelectorAll("[data-secret]").forEach(function (box) {
            var f = box.getAttribute("data-secret");
            var typed = box.querySelector("input").value.trim() !== "";
            var editing = !box.querySelector(".secret-edit").hidden;
            if (typed || !editing || !j.set[f]) { showSecret(box, !!j.set[f]); }
          });
        }
      }).catch(function (e) { say(netOut, e.message, "err"); });
    };
    var later = function (ms) { window.clearTimeout(netTimer); netTimer = window.setTimeout(netSave, ms); };
    net.addEventListener("submit", function (ev) { ev.preventDefault(); netSave(); });
    net.addEventListener("change", function (ev) {
      if (ev.target.name === "mode") { showMode(); }
      later(250);
    });
    net.addEventListener("input", function (ev) {
      if (ev.target.hasAttribute("data-secret-input")) { return; }  // secrets save when you leave the box
      later(1200);
    });
    net.querySelectorAll("[data-secret]").forEach(function (box) {
      var inp = box.querySelector("input");
      box.querySelector("[data-replace]").addEventListener("click", function () {
        box.querySelector(".secret-state").hidden = true;
        box.querySelector(".secret-edit").hidden = false;
        box.querySelector("[data-keep]").hidden = false;
        inp.placeholder = box.getAttribute("data-secret") === "address" ? "proxy.example.invalid:8080" : "Type the new one";
        inp.focus();
      });
      box.querySelector("[data-keep]").addEventListener("click", function () { showSecret(box, true); });
      box.querySelector("[data-remove]").addEventListener("click", function () {
        clear.push(box.getAttribute("data-secret"));
        netSave();
      });
      inp.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") { ev.preventDefault(); inp.blur(); }
      });
    });
    var testBtn = net.querySelector("[data-nettest]");
    var results = net.querySelector("[data-results]");
    testBtn.addEventListener("click", function () {
      testBtn.disabled = true;
      results.setAttribute("aria-busy", "true");
      results.innerHTML = '<p class="empty-box small">Trying each source…</p>';
      (netTimer ? netSave() : Promise.resolve()).then(function () {
        return fetch("/api/network/test", {
          method: "POST", headers: { "X-CSRF-Token": csrf }, credentials: "same-origin"
        });
      }).then(function (r) {
        if (!r.ok) { throw new Error("Error " + r.status); }
        return r.text();
      }).then(function (html) { results.innerHTML = html; })
        .catch(function (e) { results.innerHTML = ""; say(netOut, "Test failed: " + e.message, "err"); })
        .then(function () { results.removeAttribute("aria-busy"); testBtn.disabled = false; });
    });
  }

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
