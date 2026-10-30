// Kowan documentation: search, copy buttons on code, the current section in
// "On this page", and the menu folded on small screens. No dependencies.
(function () {
  "use strict";

  // ── Menu: open on wide screens (the sidebar), folded on phones.
  var navWrap = document.querySelector(".dnav-wrap");
  if (navWrap && window.matchMedia("(max-width: 860px)").matches) navWrap.removeAttribute("open");

  // ── Copy buttons on code blocks.
  document.querySelectorAll(".prose pre").forEach(function (pre) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "dcopy";
    btn.textContent = "Copy";
    btn.setAttribute("aria-label", "Copy code");
    btn.addEventListener("click", function () {
      var code = pre.querySelector("code") || pre;
      var text = code.innerText.replace(/^\$ /gm, "");
      var done = function (label) {
        btn.textContent = label;
        btn.setAttribute("data-done", "");
        setTimeout(function () { btn.textContent = "Copy"; btn.removeAttribute("data-done"); }, 1600);
      };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(function () { done("Copied"); }, function () { done("Failed"); });
      } else {
        var range = document.createRange();
        range.selectNodeContents(code);
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        done("Selected");
      }
    });
    pre.appendChild(btn);
  });

  // ── The section being read, marked in "On this page".
  var tocLinks = Array.prototype.slice.call(document.querySelectorAll(".dtoc a"));
  if (tocLinks.length && "IntersectionObserver" in window) {
    var heads = tocLinks.map(function (a) { return document.getElementById(a.getAttribute("href").slice(1)); }).filter(Boolean);
    var visible = new Set();
    var mark = function () {
      var current = heads.find(function (h) { return visible.has(h); });
      if (!current) return;
      tocLinks.forEach(function (a) {
        a.setAttribute("aria-current", a.getAttribute("href") === "#" + current.id ? "true" : "false");
      });
    };
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) visible.add(e.target); else visible.delete(e.target); });
      mark();
    }, { rootMargin: "-70px 0px -60% 0px" });
    heads.forEach(function (h) { io.observe(h); });
  }

  // ── Search: the index loads on first use; results rank title, then
  // headings, then text matches.
  var input = document.getElementById("dsearch-input");
  var list = document.getElementById("dsearch-results");
  if (!input || !list) return;
  var index = null, loading = null, active = -1;

  function load() {
    if (index) return Promise.resolve(index);
    if (!loading) {
      loading = fetch("/docs/search.json").then(function (r) { return r.json(); }).then(function (data) {
        index = data;
        return data;
      });
    }
    return loading;
  }
  function esc(s) {
    return s.replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; });
  }
  function highlight(text, terms) {
    var out = esc(text);
    terms.forEach(function (t) {
      out = out.replace(new RegExp("(" + t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "gi"), "<mark>$1</mark>");
    });
    return out;
  }
  function snippet(text, term) {
    var i = text.toLowerCase().indexOf(term);
    if (i < 0) return "";
    var start = Math.max(0, i - 50);
    return (start > 0 ? "…" : "") + text.slice(start, i + 90).trim() + "…";
  }
  function search(q) {
    var terms = q.toLowerCase().split(/\s+/).filter(function (t) { return t.length > 1; });
    if (!terms.length) return [];
    var hits = [];
    index.forEach(function (p) {
      var title = p.t.toLowerCase(), text = p.x.toLowerCase(), score = 0, anchor = null, ok = true;
      terms.forEach(function (t) {
        var s = 0;
        if (title.indexOf(t) >= 0) s += 12;
        p.h.forEach(function (h) {
          if (h[0].toLowerCase().indexOf(t) >= 0) { s += 5; if (!anchor) anchor = h; }
        });
        if (p.d.toLowerCase().indexOf(t) >= 0) s += 3;
        if (text.indexOf(t) >= 0) s += 1 + Math.min(3, text.split(t).length - 2);
        if (!s) ok = false;
        score += s;
      });
      if (!ok) return;
      hits.push({
        page: p, score: score,
        url: p.u + (anchor ? "#" + anchor[1] : ""),
        label: anchor ? p.t + " › " + anchor[0] : p.t,
        excerpt: snippet(p.x, terms[0]) || p.d,
      });
    });
    hits.sort(function (a, b) { return b.score - a.score; });
    return hits.slice(0, 8).map(function (h) { h.terms = terms; return h; });
  }
  function show(hits, q) {
    active = -1;
    if (!q.trim()) { close(); return; }
    list.innerHTML = hits.length
      ? hits.map(function (h, i) {
          return '<li><a id="dsr-' + i + '" role="option" aria-selected="false" href="' + h.url + '"><b>' +
            highlight(h.label, h.terms) + "</b><small>" + highlight(h.excerpt, h.terms) + "</small></a></li>";
        }).join("")
      : '<li class="empty">No results for “' + esc(q) + '”.</li>';
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
  }
  function close() {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  }
  function move(step) {
    var items = list.querySelectorAll("a");
    if (!items.length) return;
    if (active >= 0) items[active].setAttribute("aria-selected", "false");
    active = (active + step + items.length) % items.length;
    items[active].setAttribute("aria-selected", "true");
    items[active].scrollIntoView({ block: "nearest" });
    input.setAttribute("aria-activedescendant", items[active].id);
  }

  input.addEventListener("focus", load);
  input.addEventListener("input", function () {
    var q = input.value;
    load().then(function () { if (input.value === q) show(search(q), q); });
  });
  input.addEventListener("keydown", function (e) {
    if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
    else if (e.key === "Enter") {
      var items = list.querySelectorAll("a");
      var target = items[active >= 0 ? active : 0];
      if (target && !list.hidden) { e.preventDefault(); window.location.href = target.href; }
    } else if (e.key === "Escape") { input.value = ""; close(); input.blur(); }
  });
  document.addEventListener("keydown", function (e) {
    var tag = (e.target && e.target.tagName) || "";
    if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(tag) && !e.metaKey && !e.ctrlKey) {
      e.preventDefault();
      input.focus();
    }
  });
  document.addEventListener("click", function (e) {
    if (!e.target.closest(".dsearch")) close();
  });
})();
