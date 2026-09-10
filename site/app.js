// 絞り込み。依存なし。JSが無くても全件は表示される (HTMLは静的に全部出ている)。
// 状態は URL のハッシュに持たせて、絞り込んだ状態を共有できるようにする。
(function () {
  "use strict";
  var bar = document.getElementById("filter");
  if (!bar) return;
  var prefSel = document.getElementById("f-pref");
  var muniSel = document.getElementById("f-muni");
  var text = document.getElementById("f-text");
  var clear = document.getElementById("f-clear");
  var count = document.getElementById("f-count");
  var munis = {};
  try { munis = JSON.parse(bar.getAttribute("data-munis") || "{}"); } catch (e) { munis = {}; }

  var prefs = [].slice.call(document.querySelectorAll("section.prefecture"));
  var sections = [].slice.call(document.querySelectorAll("section.municipality"));
  var cards = [].slice.call(document.querySelectorAll("section.municipality article"));

  function norm(s) {
    return (s || "").normalize("NFKC").toLowerCase().replace(/[\s　]/g, "");
  }

  function fillMunis(pref) {
    var list = pref ? (munis[pref] || []) : [];
    var keep = muniSel.value;
    muniSel.innerHTML = "";
    var o = document.createElement("option");
    o.value = ""; o.textContent = "すべて";
    muniSel.appendChild(o);
    list.forEach(function (m) {
      var opt = document.createElement("option");
      opt.value = m; opt.textContent = m;
      muniSel.appendChild(opt);
    });
    muniSel.disabled = !pref;
    if (list.indexOf(keep) >= 0) muniSel.value = keep;
  }

  function apply(fromHash) {
    var p = prefSel.value, m = muniSel.value, q = norm(text.value);
    var shown = 0;
    cards.forEach(function (c) {
      var ok = (!p || c.getAttribute("data-pref") === p)
            && (!m || c.getAttribute("data-muni") === m)
            && (!q || norm(c.getAttribute("data-name")).indexOf(q) >= 0);
      c.hidden = !ok;
      if (ok) shown++;
    });
    // 空になった市町村・県の見出しは畳む。件数も見えている分に合わせる。
    sections.forEach(function (s) {
      var n = s.querySelectorAll("article:not([hidden])").length;
      s.hidden = n === 0;
      var c = s.querySelector(".count");
      if (c) c.textContent = n + "件";
    });
    prefs.forEach(function (s) {
      var n = s.querySelectorAll("article:not([hidden])").length;
      s.hidden = n === 0;
      var c = s.querySelector(".prefecture-head .count");
      if (c) c.textContent = n + "件";
    });
    var active = p || m || q;
    count.textContent = active ? shown + "件を表示" : "";
    bar.classList.toggle("active", !!active);
    if (!fromHash) {
      var h = [];
      if (p) h.push("pref=" + encodeURIComponent(p));
      if (m) h.push("muni=" + encodeURIComponent(m));
      if (q) h.push("q=" + encodeURIComponent(text.value));
      var next = h.length ? "#" + h.join("&") : location.pathname + location.search;
      history.replaceState(null, "", next);
    }
  }

  function readHash() {
    var out = {};
    location.hash.replace(/^#/, "").split("&").forEach(function (kv) {
      var i = kv.indexOf("=");
      if (i > 0) out[kv.slice(0, i)] = decodeURIComponent(kv.slice(i + 1));
    });
    return out;
  }

  prefSel.addEventListener("change", function () { fillMunis(prefSel.value); apply(); });
  muniSel.addEventListener("change", function () { apply(); });
  var t = null;
  text.addEventListener("input", function () { clearTimeout(t); t = setTimeout(function () { apply(); }, 120); });
  clear.addEventListener("click", function () {
    prefSel.value = ""; fillMunis(""); text.value = ""; apply();
  });

  var init = readHash();
  if (init.pref && munis[init.pref]) prefSel.value = init.pref;
  fillMunis(prefSel.value);
  if (init.muni) muniSel.value = init.muni;
  if (init.q) text.value = init.q;
  apply(true);
})();
