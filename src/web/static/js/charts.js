/* Self-contained SVG charts for the state tracker and test results.
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

  function svgElement(tag, attributes, text) {
    var el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.keys(attributes).forEach(function (name) {
      el.setAttribute(name, attributes[name]);
    });
    if (text !== undefined) el.textContent = text;
    return el;
  }

  function testDate(iso, withTime) {
    // Keep the server's display timezone, just like the adjacent history.
    var label = iso.slice(0, 10).split("-").reverse().join(".");
    return withTime ? label + " " + iso.slice(11, 19) : label;
  }

  function buildTestChart(entries, minScore, maxScore, title, width) {
    var W = width, H = W < 480 ? 230 : 280;
    var padL = 32, padR = 18, padT = 28, padB = 38;
    var innerW = W - padL - padR, innerH = H - padT - padB;
    var start = Date.parse(entries[0].t);
    var end = Date.parse(entries[entries.length - 1].t);
    var svg = svgElement("svg", {
      "class": "mc-svg",
      viewBox: "0 0 " + W + " " + H,
      role: "img",
      "aria-label": "Динамика результатов: " + title
    });
    svg.appendChild(svgElement("title", {}, "Результаты «" + title +
      "». Даты и баллы доступны в истории прохождений."));

    function x(timestamp) {
      return start === end ? padL + innerW / 2 :
        padL + ((timestamp - start) / (end - start)) * innerW;
    }
    function y(score) {
      return padT + (1 - (score - minScore) / (maxScore - minScore)) * innerH;
    }

    svg.appendChild(svgElement("text", {
      "class": "mc-axis", x: padL, y: 14
    }, "Баллы"));
    var step = Math.max(1, Math.ceil((maxScore - minScore) / 5));
    var ticks = [];
    for (var score = minScore; score < maxScore; score += step) ticks.push(score);
    // Always show the test's actual maximum, even if it is not a round number.
    if (ticks.length > 1 && maxScore - ticks[ticks.length - 1] < step / 2) ticks.pop();
    ticks.push(maxScore);
    ticks.forEach(function (value) {
      svg.appendChild(svgElement("line", {
        "class": "mc-grid", x1: padL, y1: y(value), x2: W - padR, y2: y(value)
      }));
      svg.appendChild(svgElement("text", {
        "class": "mc-axis", x: padL - 8, y: y(value) + 4, "text-anchor": "end"
      }, value));
    });

    var labels = [entries[0]];
    var middle = entries[Math.floor((entries.length - 1) / 2)];
    var middleX = x(Date.parse(middle.t));
    if (start !== end && middleX - padL > 100 && W - padR - middleX > 100) {
      labels.push(middle);
    }
    if (start !== end) labels.push(entries[entries.length - 1]);
    var sameDay = testDate(entries[0].t) === testDate(entries[entries.length - 1].t);
    labels.forEach(function (entry, i) {
      var label = sameDay && start !== end ?
        entry.t.slice(11, 19) : testDate(entry.t);
      svg.appendChild(svgElement("text", {
        "class": "mc-axis", x: x(Date.parse(entry.t)), y: H - 14,
        "text-anchor": labels.length === 1 ? "middle" :
          (i === 0 ? "start" : (i === labels.length - 1 ? "end" : "middle"))
      }, label));
    });

    if (entries.length > 1) {
      svg.appendChild(svgElement("polyline", {
        "class": "mc-series is-active",
        stroke: "#2b5ce6",
        points: entries.map(function (entry) {
          return x(Date.parse(entry.t)).toFixed(1) + "," + y(entry.score).toFixed(1);
        }).join(" ")
      }));
    }
    entries.forEach(function (entry) {
      var label = testDate(entry.t, true) + ": " + entry.score +
        " из " + maxScore;
      var dot = svgElement("circle", {
        cx: x(Date.parse(entry.t)), cy: y(entry.score), r: 4,
        fill: "#2b5ce6", tabindex: "0", "aria-label": label
      });
      dot.appendChild(svgElement("title", {}, label));
      svg.appendChild(dot);
    });
    return svg;
  }

  function mountTestChart(mount) {
    var entries = parseEntries(mount);
    var minScore = Number(mount.getAttribute("data-min-score"));
    var maxScore = Number(mount.getAttribute("data-max-score"));
    if (!Array.isArray(entries) || !entries.length ||
        !Number.isFinite(minScore) || !Number.isFinite(maxScore) || maxScore <= minScore) return;
    var renderedWidth;
    function render() {
      var width = Math.max(200, Math.min(640, mount.clientWidth));
      if (width === renderedWidth) return;
      renderedWidth = width;
      mount.replaceChildren(buildTestChart(entries, minScore, maxScore,
        mount.getAttribute("data-title") || "Тест", width));
    }
    render();
    if (global.ResizeObserver) new global.ResizeObserver(render).observe(mount);
  }

  function initTestCharts() {
    var mounts = document.querySelectorAll(".test-score-chart[data-entries]");
    for (var i = 0; i < mounts.length; i++) mountTestChart(mounts[i]);
  }

  global.MedCharts = {
    initPreviews: initPreviews,
    initTrackerChart: initTrackerChart,
    initTestCharts: initTestCharts
  };
})(window);
