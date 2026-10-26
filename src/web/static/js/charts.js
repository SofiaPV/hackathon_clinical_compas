/* Self-contained SVG charts for the patient state tracker.
   No external dependencies, so it works offline on the local network. */
(function (global) {
  "use strict";

  var META = [
    { key: "mood", color: "#2b5ce6" },
    { key: "anxiety", color: "#e5484d" },
    { key: "energy", color: "#2fa36b" }
  ];

  function pad2(n) {
    return (n < 10 ? "0" : "") + n;
  }

  function fmtDate(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    return pad2(d.getDate()) + "." + pad2(d.getMonth() + 1);
  }

  function scaleX(i, n, padL, innerW) {
    if (n <= 1) return padL + innerW / 2;
    return padL + (i * innerW) / (n - 1);
  }

  function scaleY(v, padT, innerH) {
    return padT + (1 - v / 10) * innerH;
  }

  // Returns array of "x,y" strings for defined values of one metric.
  function points(entries, key, padL, padT, innerW, innerH) {
    var out = [];
    for (var i = 0; i < entries.length; i++) {
      var v = entries[i][key];
      if (v === null || v === undefined) continue;
      out.push(
        scaleX(i, entries.length, padL, innerW).toFixed(1) +
          "," +
          scaleY(v, padT, innerH).toFixed(1)
      );
    }
    return out;
  }

  function buildFull(entries) {
    var W = 640, H = 280, padL = 32, padR = 16, padT = 16, padB = 34;
    var innerW = W - padL - padR, innerH = H - padT - padB;
    var n = entries.length;
    var svg =
      '<svg class="mc-svg" viewBox="0 0 ' + W + " " + H +
      '" preserveAspectRatio="xMidYMid meet" role="img" aria-label="График динамики состояния">';

    for (var v = 0; v <= 10; v += 2) {
      var y = scaleY(v, padT, innerH);
      svg +=
        '<line class="mc-grid" x1="' + padL + '" y1="' + y.toFixed(1) +
        '" x2="' + (W - padR) + '" y2="' + y.toFixed(1) + '"/>';
      svg +=
        '<text class="mc-axis" x="' + (padL - 6) + '" y="' + (y + 3).toFixed(1) +
        '" text-anchor="end">' + v + "</text>";
    }

    var idxs = n <= 1 ? [0] : [0, Math.floor((n - 1) / 2), n - 1];
    var seen = {};
    for (var k = 0; k < idxs.length; k++) {
      var idx = idxs[k];
      if (seen[idx]) continue;
      seen[idx] = true;
      var x = scaleX(idx, n, padL, innerW);
      svg +=
        '<text class="mc-axis" x="' + x.toFixed(1) + '" y="' + (H - padB + 16) +
        '" text-anchor="middle">' + fmtDate(entries[idx].t) + "</text>";
    }

    META.forEach(function (m) {
      var pts = points(entries, m.key, padL, padT, innerW, innerH);
      if (!pts.length) return;
      var active = m.key === "mood" ? " is-active" : "";
      if (pts.length > 1) {
        svg +=
          '<polyline class="mc-series mc-' + m.key + active +
          '" data-series="' + m.key + '" points="' + pts.join(" ") +
          '" fill="none" stroke="' + m.color + '"/>';
      }
      var dots =
        '<g class="mc-dots mc-dots-' + m.key + active + '" data-series="' +
        m.key + '" fill="' + m.color + '">';
      for (var i = 0; i < pts.length; i++) {
        var xy = pts[i].split(",");
        dots += '<circle cx="' + xy[0] + '" cy="' + xy[1] + '" r="3.2"/>';
      }
      dots += "</g>";
      svg += dots;
    });

    return svg + "</svg>";
  }

  function buildPreview(entries) {
    var W = 320, H = 110, padL = 6, padR = 6, padT = 10, padB = 10;
    var innerW = W - padL - padR, innerH = H - padT - padB;
    var svg =
      '<svg class="mc-svg mc-preview-svg" viewBox="0 0 ' + W + " " + H +
      '" preserveAspectRatio="none" role="img" aria-label="Превью динамики состояния">';

    for (var v = 0; v <= 10; v += 5) {
      var y = scaleY(v, padT, innerH);
      svg +=
        '<line class="mc-grid" x1="' + padL + '" y1="' + y.toFixed(1) +
        '" x2="' + (W - padR) + '" y2="' + y.toFixed(1) + '"/>';
    }

    META.forEach(function (m) {
      var pts = points(entries, m.key, padL, padT, innerW, innerH);
      if (pts.length > 1) {
        svg +=
          '<polyline class="mc-prev mc-' + m.key + '" points="' + pts.join(" ") +
          '" fill="none" stroke="' + m.color + '"/>';
      } else if (pts.length === 1) {
        var xy = pts[0].split(",");
        svg +=
          '<circle class="mc-prev-dot" cx="' + xy[0] + '" cy="' + xy[1] +
          '" r="3" fill="' + m.color + '"/>';
      }
    });

    return svg + "</svg>";
  }

  function parseEntries(el) {
    try {
      return JSON.parse(el.getAttribute("data-entries")) || [];
    } catch (e) {
      return [];
    }
  }

  function setActive(root, key) {
    var nodes = root.querySelectorAll(".mc-series, .mc-dots");
    for (var i = 0; i < nodes.length; i++) {
      var match = nodes[i].getAttribute("data-series") === key;
      nodes[i].classList.toggle("is-active", match);
    }
  }

  function initPreviews() {
    var els = document.querySelectorAll(".chart-preview[data-entries]");
    for (var i = 0; i < els.length; i++) {
      var entries = parseEntries(els[i]);
      if (entries.length) els[i].innerHTML = buildPreview(entries);
    }
  }

  function initTrackerChart() {
    var mount = document.getElementById("tracker-chart");
    if (!mount) return;
    var entries = parseEntries(mount);
    if (!entries.length) return;
    mount.innerHTML = buildFull(entries);

    var legend = document.getElementById("tracker-legend");
    if (!legend) return;
    var chips = legend.querySelectorAll(".legend-chip");
    for (var i = 0; i < chips.length; i++) {
      chips[i].addEventListener("click", function () {
        var key = this.getAttribute("data-series");
        for (var j = 0; j < chips.length; j++) {
          chips[j].classList.toggle("is-active", chips[j] === this);
        }
        setActive(mount, key);
      });
    }
  }

  global.MedCharts = { initPreviews: initPreviews, initTrackerChart: initTrackerChart };
})(window);
