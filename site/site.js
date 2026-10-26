// Copy buttons, the quick-start tabs and the screenshot viewer. Nothing else runs on this page.
(function () {
  "use strict";

  function textOf(pre) {
    var clone = pre.cloneNode(true);
    clone.querySelectorAll(".p").forEach(function (n) { n.remove(); });
    return clone.textContent.trim();
  }

  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    ta.remove();
    return ok ? Promise.resolve() : Promise.reject(new Error("copy failed"));
  }

  var live = document.createElement("p");
  live.className = "sr-only";
  live.setAttribute("aria-live", "polite");
  document.body.appendChild(live);

  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    var label = btn.querySelector("span");
    var use = btn.querySelector("use");
    var timer;
    btn.addEventListener("click", function () {
      var pre = document.getElementById(btn.getAttribute("data-copy"));
      if (!pre) return;
      var text = textOf(pre);
      var p = (navigator.clipboard && window.isSecureContext
        ? navigator.clipboard.writeText(text)
        : Promise.reject(new Error("no clipboard api")))
        .catch(function () { return fallbackCopy(text); });
      p.then(function () {
        clearTimeout(timer);
        btn.removeAttribute("data-state");
        void btn.offsetWidth; // restart the beat animation on repeat clicks
        btn.setAttribute("data-state", "done");
        label.textContent = "Copied";
        use.setAttribute("href", "#i-check");
        live.textContent = "Command copied to clipboard";
        timer = setTimeout(function () {
          btn.removeAttribute("data-state");
          label.textContent = "Copy";
          use.setAttribute("href", "#i-copy");
        }, 1800);
      }, function () {
        clearTimeout(timer);
        label.textContent = "Selected";
        timer = setTimeout(function () { label.textContent = "Copy"; }, 1800);
        var range = document.createRange();
        range.selectNodeContents(pre);
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        live.textContent = "Copy is unavailable; the command is selected";
      });
    });
  });

  var tabs = Array.prototype.slice.call(document.querySelectorAll('[role="tab"]'));
  function select(tab, focus) {
    tabs.forEach(function (t) {
      var on = t === tab;
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
    });
    if (focus) tab.focus();
  }
  tabs.forEach(function (tab, i) {
    tab.addEventListener("click", function () { select(tab, false); });
    tab.addEventListener("keydown", function (e) {
      var next = null;
      if (e.key === "ArrowRight") next = tabs[(i + 1) % tabs.length];
      else if (e.key === "ArrowLeft") next = tabs[(i - 1 + tabs.length) % tabs.length];
      else if (e.key === "Home") next = tabs[0];
      else if (e.key === "End") next = tabs[tabs.length - 1];
      if (next) { e.preventDefault(); select(next, true); }
    });
  });
  // Screenshot viewer: every screenshot opens full size in a modal dialog,
  // in the light or dark variant the page shows, with its caption. Arrows
  // step through all of them; Escape, the close button or a click outside
  // closes it and focus returns to the screenshot.
  var shots = Array.prototype.slice.call(document.querySelectorAll("img.shot"));
  if (shots.length && typeof HTMLDialogElement === "function") {
    var dlg = document.createElement("dialog");
    dlg.className = "viewer";
    dlg.setAttribute("aria-label", "Screenshot");
    dlg.innerHTML =
      '<div class="viewer-bar">' +
      '<span class="viewer-count" aria-live="polite"></span>' +
      '<button type="button" class="viewer-btn" data-step="-1" aria-label="Previous screenshot">&larr;</button>' +
      '<button type="button" class="viewer-btn" data-step="1" aria-label="Next screenshot">&rarr;</button>' +
      '<button type="button" class="viewer-btn viewer-close" aria-label="Close">&times;</button>' +
      '</div>' +
      '<figure class="viewer-fig"><figcaption class="viewer-cap"></figcaption></figure>';
    document.body.appendChild(dlg);
    var cap = dlg.querySelector(".viewer-cap");
    // The image gets its source from the screenshot that opens the viewer.
    var img = document.createElement("img");
    img.className = "viewer-img";
    cap.parentNode.insertBefore(img, cap);
    var count = dlg.querySelector(".viewer-count");
    var current = 0;
    var opener = null;

    function captionOf(shot) {
      var fig = shot.closest("figure");
      var fc = fig && fig.querySelector("figcaption");
      return fc ? fc.textContent.replace(/\s+/g, " ").trim() : "";
    }
    function show(i) {
      current = (i + shots.length) % shots.length;
      var shot = shots[current];
      img.src = shot.currentSrc || shot.src;
      img.alt = shot.alt;
      img.classList.toggle("tall", shot.classList.contains("tall"));
      cap.textContent = captionOf(shot) || shot.alt;
      count.textContent = (current + 1) + " / " + shots.length;
    }
    function open(i, from) {
      opener = from;
      show(i);
      document.documentElement.classList.add("viewer-open");
      dlg.showModal();
    }
    dlg.addEventListener("close", function () {
      document.documentElement.classList.remove("viewer-open");
      img.removeAttribute("src");
      if (opener) opener.focus();
    });
    dlg.addEventListener("click", function (e) {
      var step = e.target.closest("[data-step]");
      if (step) { show(current + Number(step.getAttribute("data-step"))); return; }
      if (!e.target.closest(".viewer-btn") || e.target.closest(".viewer-close")) dlg.close();
    });
    dlg.addEventListener("keydown", function (e) {
      if (e.key === "ArrowRight") { e.preventDefault(); show(current + 1); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); show(current - 1); }
    });

    shots.forEach(function (shot, i) {
      var target = shot.closest("picture") || shot;
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "zoom";
      btn.setAttribute("aria-label", "Enlarge: " + shot.alt);
      target.parentNode.insertBefore(btn, target);
      btn.appendChild(target);
      btn.addEventListener("click", function () { open(i, btn); });
    });
  }
})();
