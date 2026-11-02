// Settings > Filing rules: a list of rules, an editor for the picked one, a live preview, and the shared policies.
// Every change is checked and saved by the server (POST /api/filing); nothing here builds HTML from text.
(function () {
  "use strict";
  var root = document.querySelector("[data-filing]");
  if (!root) { return; }
  var csrf = (document.querySelector('meta[name="csrf"]') || {}).content || "";
  var data = JSON.parse(document.getElementById("filing-data").textContent);
  var cfg = data.filing || {};
  cfg.rules = cfg.rules || []; cfg.policies = cfg.policies || {};
  var defaults = data.policies || {};
  var sel = 0, timer = null;
  var FIELDS = [["type", "Media type"], ["genres", "Genre"], ["keywords", "Keyword"], ["original_language", "Original language"],
    ["countries", "Country"], ["audio_languages", "Audio language"], ["subtitle_languages", "Subtitle language"],
    ["dubbed", "Dubbed into (language or any)"], ["collection", "In a collection"], ["quality", "Quality word in the name"],
    ["picture", "Picture size"], ["year", "Year (from-to)"], ["runtime", "Running time in minutes (from-to)"],
    ["in_library", "Already in the library"], ["details", "Details known"], ["name", "Release name matches"]];
  var LISTS = ["genres", "keywords", "original_language", "countries", "audio_languages", "subtitle_languages", "quality", "picture"];
  var BOOLS = ["collection", "in_library", "details"];
  var CHOICES = {quality_from: ["name-or-picture", "name", "picture"], second_copy: ["version", "folder"],
    other_audio_copy: ["folder", "version"], trailers: ["suffix", "folder"]};
  var status = document.getElementById("fl-status");

  function el(tag, attrs, kids) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") { e.textContent = attrs[k]; } else if (k === "on") {
        Object.keys(attrs.on).forEach(function (ev) { e.addEventListener(ev, attrs.on[ev]); });
      } else if (attrs[k] === true) { e.setAttribute(k, ""); } else if (attrs[k] !== false && attrs[k] != null) { e.setAttribute(k, attrs[k]); }
    });
    (kids || []).forEach(function (c) { if (c) { e.appendChild(typeof c === "string" ? document.createTextNode(c) : c); } });
    return e;
  }
  function say(t, cls) { status.textContent = t; status.classList.remove("ok", "err"); if (cls) { status.classList.add(cls); } }
  function post(url, body) {
    return fetch(url, {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf}, body: JSON.stringify(body)})
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) { throw new Error(j.error || r.status); } return j; }); });
  }
  function save() {
    window.clearTimeout(timer);
    timer = window.setTimeout(function () {
      say("Saving…");
      post("/api/filing", {filing: cfg}).then(function (j) { say("Saved", "ok"); preview(j.preview); })
        .catch(function (e) { say(e.message, "err"); });
    }, 600);
  }
  function types(r) { var t = (r.when || {}).type; return t ? [].concat(t) : []; }
  function summary(r) {
    var parts = []; var w = r.when || {};
    Object.keys(w).forEach(function (k) {
      if (k === "any") { parts.push("one of " + w.any.length); } else if (k === "not") { parts.push("not " + Object.keys(w.not).join(", ")); }
      else { parts.push(k.replace(/_/g, " ") + " " + text(k, w[k])); }
    });
    if (r.hold) { parts.push("waits for review"); }
    return parts.join(" · ") || "every item";
  }
  function text(k, v) {
    if (BOOLS.indexOf(k) >= 0) { return v ? "yes" : "no"; }
    if (k === "year" || k === "runtime") { return (v.min == null ? "" : v.min) + "-" + (v.max == null ? "" : v.max); }
    return [].concat(v).join(", ");
  }
  function parse(k, s) {
    s = String(s || "").trim();
    if (BOOLS.indexOf(k) >= 0) { return s === "yes"; }
    if (LISTS.indexOf(k) >= 0) { return s.split(",").map(function (x) { return x.trim(); }).filter(Boolean); }
    if (k === "year" || k === "runtime") {
      var m = s.split("-"); var o = {};
      if (m[0] && !isNaN(+m[0])) { o.min = +m[0]; } if (m[1] && !isNaN(+m[1])) { o.max = +m[1]; } return o;
    }
    return s;
  }
  function rowsOf(w) {
    var rows = [], extra = [];
    Object.keys(w || {}).forEach(function (k) {
      if (k === "not") { Object.keys(w.not).forEach(function (n) { rows.push({f: n, op: "not", v: w.not[n]}); }); }
      else if (k === "any") { w.any.forEach(function (b) { var ks = Object.keys(b); if (ks.length === 1) { rows.push({f: ks[0], op: "or", v: b[ks[0]]}); } else { extra.push(b); } }); }
      else { rows.push({f: k, op: "is", v: w[k]}); }
    });
    return {rows: rows, extra: extra};
  }

  function renderList() {
    var ol = document.getElementById("fl-rules"); ol.textContent = "";
    document.getElementById("fl-enabled").checked = !!cfg.enabled;
    cfg.rules.forEach(function (r, i) {
      var move = function (d) { return function () {
        var j = i + d; if (j < 0 || j >= cfg.rules.length) { return; }
        var t = cfg.rules[i]; cfg.rules[i] = cfg.rules[j]; cfg.rules[j] = t; sel = j; renderAll(); save(); }; };
      ol.appendChild(el("li", {"class": "fl-rule" + (i === sel ? " on" : "") + (r.enabled === false ? " off" : "")}, [
        el("button", {type: "button", "class": "fl-pick", "aria-pressed": i === sel ? "true" : "false", on: {click: function () { sel = i; renderAll(); }}}, [
          el("span", {"class": "fl-name"}, [el("span", {"class": "mono small muted", text: String(i + 1).padStart(2, "0")}), " ",
            el("span", {"class": "strong", text: r.name || "Rule"}), r.enabled === false ? el("span", {"class": "badge warn", text: "off"}) : null]),
          el("span", {"class": "small soft", text: summary(r)})]),
        el("span", {"class": "fl-moves"}, [
          el("button", {type: "button", "class": "btn small", "aria-label": "Move " + (r.name || "rule") + " up", text: "↑", on: {click: move(-1)}}),
          el("button", {type: "button", "class": "btn small", "aria-label": "Move " + (r.name || "rule") + " down", text: "↓", on: {click: move(1)}})])]));
    });
    if (!cfg.rules.length) { ol.appendChild(el("li", {"class": "empty-box small", text: "No rules yet: add one, or start from a preset."})); }
  }

  function field(label, value, onInput, opts) {
    opts = opts || {};
    var input = opts.choices ? el("select", {}, opts.choices.map(function (c) { return el("option", {value: c, text: c, selected: c === value}); }))
      : el("input", {value: value == null ? "" : value, "class": opts.mono === false ? "" : "mono", type: opts.type || "text", placeholder: opts.placeholder || ""});
    input.addEventListener(opts.choices ? "change" : "input", function () { onInput(input.value); save(); });
    return el("label", {}, [label, input]);
  }

  function renderEditor() {
    var box = document.getElementById("fl-edit"); box.textContent = "";
    var r = cfg.rules[sel];
    if (!r) { box.appendChild(el("p", {"class": "soft", text: "Pick or add a rule."})); return; }
    var head = el("div", {"class": "row between baseline wrap-gap"}, [
      el("h2", {"class": "h3", id: "h-edit", text: "Rule " + (sel + 1) + ": " + (r.name || "")}),
      el("span", {"class": "row wrap-gap"}, [
        el("label", {"class": "check"}, [el("input", {type: "checkbox", checked: r.enabled !== false, on: {change: function (e) { r.enabled = e.target.checked; renderList(); save(); }}}), " On"]),
        el("button", {type: "button", "class": "btn small", text: "Duplicate", on: {click: function () { cfg.rules.splice(sel + 1, 0, JSON.parse(JSON.stringify(r))); cfg.rules[sel + 1].name += " (copy)"; sel += 1; renderAll(); save(); }}}),
        el("button", {type: "button", "class": "link", text: "Delete", on: {click: function () { cfg.rules.splice(sel, 1); sel = Math.max(0, sel - 1); renderAll(); save(); }}})])]);
    box.appendChild(head);
    box.appendChild(field("Name", r.name, function (v) { r.name = v; renderList(); }, {mono: false}));
    var parsed = rowsOf(r.when);
    var conds = el("fieldset", {"class": "fl-conds"}, [el("legend", {text: "When (all \"is\" and \"is not\" rows; at least one \"or\" row)"})]);
    var rebuild = function () {
      var w = {}, not = {}, any = parsed.extra.slice();
      conds.querySelectorAll(".fl-cond").forEach(function (row) {
        var f = row.querySelector("[data-f]").value, op = row.querySelector("[data-op]").value, v = parse(f, row.querySelector("[data-v]").value);
        if (op === "is") { w[f] = v; } else if (op === "not") { not[f] = v; } else { var b = {}; b[f] = v; any.push(b); }
      });
      if (any.length) { w.any = any; } if (Object.keys(not).length) { w.not = not; }
      r.when = w; renderList(); save();
    };
    parsed.rows.forEach(function (row) { conds.appendChild(condRow(row, rebuild)); });
    if (parsed.extra.length) { conds.appendChild(el("p", {"class": "small muted", text: parsed.extra.length + " grouped \"one of\" block(s) kept as they are; edit them in YAML."})); }
    conds.appendChild(el("button", {type: "button", "class": "link", text: "Add a condition", on: {click: function () {
      conds.insertBefore(condRow({f: "genres", op: "is", v: []}, rebuild), conds.lastChild); }}}));
    box.appendChild(conds);
    var t = types(r), ep = t.indexOf("episode") >= 0 || t.indexOf("special") >= 0;
    var outs = el("fieldset", {"class": "fl-outs"}, [el("legend", {text: "Then file it"})]);
    outs.appendChild(field("Root folder", r.root, function (v) { r.root = v; }, {placeholder: "X:\\Movies"}));
    if (ep) {
      outs.appendChild(field("Show folder", r.show_folder, function (v) { r.show_folder = v; }, {placeholder: "{Title} ({Year})"}));
      outs.appendChild(field("Season folder", r.season_folder, function (v) { r.season_folder = v; }, {placeholder: "Season {Season:00}"}));
      outs.appendChild(field("Episode file", r.episode_file, function (v) { r.episode_file = v; }, {placeholder: "{Series} {Code}{ - |EpisodeTitle}"}));
    } else {
      outs.appendChild(field("Folder", r.folder, function (v) { r.folder = v; }, {placeholder: "{Title} ({Year}){ |Tags}"}));
      outs.appendChild(field("File", r.file, function (v) { r.file = v; }, {placeholder: "{Folder}"}));
      var c = r.collections || (r.collections = {});
      outs.appendChild(el("label", {"class": "check"}, [el("input", {type: "checkbox", checked: c.existing !== false, on: {change: function (e) { c.existing = e.target.checked; save(); }}}), " Use existing collection folders"]));
      outs.appendChild(el("label", {"class": "check"}, [el("input", {type: "checkbox", checked: !!c.create, on: {change: function (e) { c.create = e.target.checked; save(); }}}), " Make new collection folders"]));
      outs.appendChild(field("…from this many released films", c.min_released == null ? 2 : c.min_released, function (v) { c.min_released = +v || 2; }, {type: "number"}));
    }
    var plain = (r.tags || []).filter(function (x) { return typeof x === "string"; });
    var conditional = (r.tags || []).filter(function (x) { return typeof x !== "string"; });
    outs.appendChild(field("Tags, comma separated (use {Tags} in a template)", plain.join(", "), function (v) {
      r.tags = v.split(",").map(function (x) { return x.trim(); }).filter(Boolean).concat(conditional); }, {placeholder: "{Quality}"}));
    if (conditional.length) { outs.appendChild(el("p", {"class": "small muted", text: conditional.length + " tag(s) with their own condition kept; edit them in YAML."})); }
    outs.appendChild(field("Send to review instead, with this reason (empty: file it)", r.hold, function (v) { if (v) { r.hold = v; } else { delete r.hold; } }, {mono: false}));
    box.appendChild(outs);
    box.appendChild(el("p", {"class": "small muted", text: "Tokens: {Title} {OriginalTitle} {TranslatedTitle:ja} {Year} {Quality} {Picture} {Tags} {Collection} {Folder} {Series} {Season:00} {Episode:00} {Code} {EpisodeTitle}. A part like { - |TranslatedTitle:ja} shows only when the token has a value."}));
  }

  function condRow(row, rebuild) {
    var f = el("select", {"data-f": "", "aria-label": "Condition"}, FIELDS.map(function (x) { return el("option", {value: x[0], text: x[1], selected: x[0] === row.f}); }));
    var op = el("select", {"data-op": "", "aria-label": "How"}, [["is", "is"], ["not", "is not"], ["or", "or"]].map(function (x) { return el("option", {value: x[0], text: x[1], selected: x[0] === row.op}); }));
    var v = el("input", {"data-v": "", "class": "mono", value: text(row.f, row.v), "aria-label": "Value", placeholder: "comma separated"});
    var div = el("div", {"class": "fl-cond"}, [f, op, v, el("button", {type: "button", "class": "link", text: "Remove", on: {click: function () { div.remove(); rebuild(); }}})]);
    [f, op].forEach(function (s) { s.addEventListener("change", rebuild); });
    v.addEventListener("input", rebuild);
    return div;
  }

  function preview(rows) {
    var ul = document.getElementById("fl-preview"); ul.textContent = "";
    (rows || []).forEach(function (p) {
      ul.appendChild(el("li", {"class": "netrow"}, [
        el("span", {"class": "nr-who"}, [el("span", {"class": "strong", text: p.item}), el("span", {"class": "small muted", text: p.from === "inbox" ? "in the Inbox" : "sample"})]),
        el("span", {"class": "badge " + (p.ok && !p.hold ? "good" : "warn"), text: p.ok ? (p.rule || "") + (p.hold ? " · waits" : "") : "No rule"}),
        el("span", {"class": "mono small fl-dest", text: p.dest})]));
    });
    if (!rows || !rows.length) { ul.appendChild(el("li", {"class": "netrow small muted", text: "Add a rule to see where items would go."})); }
  }

  function renderPolicies() {
    var box = document.getElementById("fl-policies"); box.textContent = "";
    Object.keys(defaults).forEach(function (k) {
      var d = defaults[k], v = cfg.policies[k] != null ? cfg.policies[k] : d, label = k.replace(/_/g, " ");
      label = label.charAt(0).toUpperCase() + label.slice(1);
      var set = function (x) { if (JSON.stringify(x) === JSON.stringify(d)) { delete cfg.policies[k]; } else { cfg.policies[k] = x; } };
      if (typeof d === "boolean") {
        box.appendChild(el("label", {"class": "check"}, [el("input", {type: "checkbox", checked: !!v, on: {change: function (e) { set(e.target.checked); save(); }}}), " " + label]));
      } else if (Array.isArray(d)) {
        box.appendChild(field(label, v.join(", "), function (x) { set(x.split(",").map(function (s) { return s.trim(); }).filter(Boolean)); }));
      } else {
        box.appendChild(field(label, v, function (x) { set(typeof d === "number" ? +x : x); }, {choices: CHOICES[k], type: typeof d === "number" ? "number" : "text"}));
      }
    });
  }

  function renderAll() { renderList(); renderEditor(); }
  document.getElementById("fl-enabled").addEventListener("change", function (e) { cfg.enabled = e.target.checked; save(); });
  document.getElementById("fl-add").addEventListener("click", function () {
    cfg.rules.push({name: "New rule", when: {type: "film"}, root: "", folder: "{Title} ({Year})"}); sel = cfg.rules.length - 1; renderAll();
  });
  var presetSel = document.getElementById("fl-preset"), langBox = document.getElementById("fl-lang-box");
  presetSel.addEventListener("change", function () {
    langBox.hidden = presetSel.value !== "by-language";
    if (!presetSel.value || (presetSel.value === "by-language" && !document.getElementById("fl-lang").value.trim())) { return; }
    applyPreset();
  });
  document.getElementById("fl-lang").addEventListener("change", applyPreset);
  function applyPreset() {
    post("/api/filing/preset", {name: presetSel.value, language: document.getElementById("fl-lang").value})
      .then(function (j) { cfg.rules = j.filing.rules; sel = 0; renderAll(); preview(j.preview); save(); })
      .catch(function (e) { say(e.message, "err"); });
  }
  document.getElementById("fl-import-open").addEventListener("click", function () { document.getElementById("fl-import").hidden = false; });
  document.getElementById("fl-import-go").addEventListener("click", function () {
    post("/api/filing/import", {yaml: document.getElementById("fl-yaml").value}).then(function (j) {
      cfg = j.filing; sel = 0; renderAll(); renderPolicies(); preview(j.preview); say("Imported", "ok");
      document.getElementById("fl-import").hidden = true;
    }).catch(function (e) { say(e.message, "err"); });
  });
  renderAll(); renderPolicies(); preview(data.preview);
})();
