/* Mass River Flow — maps and charts. Data is embedded in each page as JSON. */
(function () {
  "use strict";
  const COLORS = { site: "#1d6e8f", none: "#9aa5ab" };
  const RIVER = "#1d6e8f", NAVY = "#0e2f44", LEAF = "#5f9131";
  // USGS National Map basemaps: public domain, no API key needed.
  const TNM = (svc) => `https://basemap.nationalmap.gov/arcgis/rest/services/${svc}/MapServer/tile/{z}/{y}/{x}`;
  const TNM_ATTR = 'Basemap: <a href="https://www.usgs.gov/programs/national-geospatial-program/national-map">USGS The National Map</a>';

  const readJSON = (id) => { const el = document.getElementById(id); return el ? JSON.parse(el.textContent) : null; };
  const fmtQ = (x) => x == null ? "—" : x >= 100 ? Math.round(x).toLocaleString() : x >= 10 ? x.toFixed(1) : x.toFixed(2);
  const logTicks = (v) => { const m = v / Math.pow(10, Math.floor(Math.log10(v))); return [1, 2, 5].some((k) => Math.abs(m - k) < 1e-6) ? (v >= 1 ? v.toLocaleString() : String(+v.toPrecision(1))) : ""; };
  const pts = (t, v) => t.map((ti, i) => ({ x: ti, y: v[i] })).filter((p) => p.y != null);

  if (window.Chart) {
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.color = "#5b6b75";
  }

  function baseMap(el, opts) {
    const map = L.map(el, Object.assign({ scrollWheelZoom: false }, opts || {}));
    const opt = { attribution: TNM_ATTR, maxNativeZoom: 16, maxZoom: 18 };
    const topo = L.tileLayer(TNM("USGSTopo"), opt).addTo(map);
    const imagery = L.tileLayer(TNM("USGSImageryTopo"), opt);
    L.control.layers({ "Topographic": topo, "Imagery": imagery }, null, { position: "topright" }).addTo(map);
    return map;
  }
  function marker(lat, lon, cond, r) {
    return L.circleMarker([lat, lon], { radius: r || 10, color: "#fff", weight: 3, fillColor: COLORS[cond] || COLORS.none, fillOpacity: 1 });
  }

  /* ---------------- home page ---------------- */
  const home = readJSON("home-data");
  if (home && document.getElementById("home-map")) {
    const map = baseMap("home-map");
    const bounds = [];
    home.filter((g) => g.lat != null).forEach((g) => {
      bounds.push([g.lat, g.lon]);
      marker(g.lat, g.lon, "site", 11).addTo(map).bindPopup(
        `<h4>${g.name}</h4><span style="color:#5b6b75">${g.region}</span><br><a class="btn btn-accent" href="${g.url}">View gauge</a>`);
    });
    if (bounds.length) map.fitBounds(bounds, { padding: [50, 50], maxZoom: 10 });
    else map.setView([42.25, -71.8], 8);
  }

  document.querySelectorAll("canvas.spark").forEach((c) => {
    const t = JSON.parse(c.dataset.t), q = JSON.parse(c.dataset.q);
    new Chart(c, {
      type: "line",
      data: { labels: t, datasets: [{ data: q, borderColor: RIVER, borderWidth: 2, pointRadius: 0, tension: 0.25, spanGaps: true,
        fill: true, backgroundColor: "rgba(29,110,143,.10)" }] },
      options: { responsive: true, maintainAspectRatio: false, animation: false, events: [],
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
        scales: { x: { display: false }, y: { display: false, type: "logarithmic" } } },
    });
  });

  /* ---------------- gauge page ---------------- */
  const g = readJSON("gauge-data");
  if (!g) return;
  const U = g.units;

  if (g.lat != null && document.getElementById("gauge-map")) {
    const map = baseMap("gauge-map", { zoomControl: true });
    map.setView([g.lat, g.lon], 13);
    marker(g.lat, g.lon, "site", 12).addTo(map).bindTooltip(g.name);
  }

  // ---- time series chart
  const d = g.daily;
  const lastT = d.t.length ? d.t[d.t.length - 1] : null;
  const flowSets = () => [
    { label: "~95% range (upper)", data: pts(d.t, d.q_hi), borderWidth: 0, pointRadius: 0, fill: false, order: 4 },
    { label: "~95% range", data: pts(d.t, d.q_lo), borderWidth: 0, pointRadius: 0, fill: "-1", backgroundColor: "rgba(29,110,143,.18)", order: 4 },
    { label: "Streamflow", data: pts(d.t, d.q), borderColor: RIVER, borderWidth: 2.2, pointRadius: 0, order: 2 },
    { label: "Flow measurements", type: "scatter", data: pts(g.meas.t, g.meas.q), pointRadius: 5, pointBackgroundColor: LEAF, pointBorderColor: "#fff", pointBorderWidth: 1.5, order: 1 },
  ];
  const levelSets = () => [
    ...(g.zero_flow_stage != null && d.t.length ? [{ label: "Zero-flow stage (dry below)", data: [{ x: d.t[0], y: g.zero_flow_stage }, { x: d.t[d.t.length - 1], y: g.zero_flow_stage }],
      borderColor: "#d9822b", borderDash: [6, 4], borderWidth: 1.6, pointRadius: 0, order: 3 }] : []),
    { label: "Water level", data: pts(d.t, d.stage), borderColor: NAVY, borderWidth: 2.2, pointRadius: 0, fill: "start", backgroundColor: "rgba(14,47,68,.08)", order: 2 },
    { label: "Flow measurements", type: "scatter", data: pts(g.meas.t, g.meas.stage), pointRadius: 5, pointBackgroundColor: LEAF, pointBorderColor: "#fff", pointBorderWidth: 1.5, order: 1 },
  ];
  let series = "flow", rangeDays = 365;
  const y_log = () => series === "flow" && document.getElementById("flow-log").checked;
  const logBox = document.getElementById("flow-log");
  const ts = new Chart(document.getElementById("ts-chart"), {
    type: "line",
    data: { datasets: flowSets() },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false, spanGaps: false,
      interaction: { mode: "nearest", axis: "x", intersect: false },
      plugins: {
        legend: { position: "bottom", labels: { usePointStyle: true, filter: (it) => !it.text.includes("(upper)") } },
        tooltip: { filter: (it) => !it.dataset.label.includes("range"),
          callbacks: { label: (c) => `${c.dataset.label}: ${series === "flow" ? fmtQ(c.parsed.y) + " " + U.discharge : c.parsed.y.toFixed(2) + " " + U.stage}` } },
      },
      scales: {
        x: { type: "time", time: { tooltipFormat: "MMM d, yyyy" }, grid: { display: false } },
        y: { type: "linear", title: { display: true, text: `Streamflow (${U.discharge})` }, grid: { color: "#eee9df" },
          ticks: { callback: (v) => y_log() ? logTicks(v) : Number(v).toLocaleString() } },
      },
    },
  });
  function applyRange() {
    const x = ts.options.scales.x;
    if (rangeDays && lastT) {
      const end = new Date(lastT + "T12:00:00");
      x.min = new Date(end.getTime() - rangeDays * 864e5).toISOString().slice(0, 10);
      x.max = lastT;
    } else { delete x.min; delete x.max; }
  }
  function redraw() {
    ts.data.datasets = series === "flow" ? flowSets() : levelSets();
    const y = ts.options.scales.y;
    y.type = series === "flow" && logBox.checked ? "logarithmic" : "linear";
    y.title.text = series === "flow" ? `Streamflow (${U.discharge})` : `Water level (${U.stage})`;
    y.beginAtZero = false;
    logBox.parentElement.style.visibility = series === "flow" ? "visible" : "hidden";
    applyRange();
    ts.update();
  }
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
    series = b.dataset.series; redraw();
  }));
  document.querySelectorAll(".ranges button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll(".ranges button").forEach((x) => x.classList.toggle("active", x === b));
    rangeDays = +b.dataset.range; redraw();
  }));
  logBox.addEventListener("change", redraw);
  if (!d.t.length) {
    document.getElementById("ts-note").textContent = "No water-level data yet for this gauge.";
    rangeDays = 0;
  }
  redraw();

  // ---- rating curve chart (stage on y, discharge on x)
  const R = g.rating;
  const rp = (q, h) => q.map((qi, i) => ({ x: qi, y: h[i] })).filter((p) => p.x != null && p.x > 0);
  const rLog = document.getElementById("rating-log");
  const rc = new Chart(document.getElementById("rating-chart"), {
    type: "scatter",
    data: { datasets: [
      { label: "upper", data: rp(R.hi, R.stage), showLine: true, borderWidth: 0, pointRadius: 0, fill: false },
      { label: "~95% range", data: rp(R.lo, R.stage), showLine: true, borderWidth: 0, pointRadius: 0, fill: "-1", backgroundColor: "rgba(29,110,143,.18)" },
      { label: "Rating curve", data: rp(R.q, R.stage), showLine: true, borderColor: RIVER, borderWidth: 2.5, pointRadius: 0 },
      { label: "Flow measurements", data: rp(g.meas.q, g.meas.stage), pointRadius: 5, pointBackgroundColor: LEAF, pointBorderColor: "#fff", pointBorderWidth: 1.5 },
    ] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {
        legend: { position: "bottom", labels: { usePointStyle: true, filter: (it) => it.text !== "upper" } },
        tooltip: { filter: (it) => it.dataset.label !== "upper" && it.dataset.label !== "~95% range",
          callbacks: { label: (c) => `${c.dataset.label}: ${c.parsed.y.toFixed(2)} ${U.stage} → ${fmtQ(c.parsed.x)} ${U.discharge}` } },
      },
      scales: {
        x: { type: "logarithmic", title: { display: true, text: `Streamflow (${U.discharge})` }, grid: { color: "#eee9df" },
          ticks: { maxRotation: 0, callback: (v) => rLog.checked ? logTicks(v) : Number(v).toLocaleString() } },
        y: { title: { display: true, text: `Water level (${U.stage})` }, grid: { color: "#eee9df" } },
      },
    },
  });
  rLog.addEventListener("change", () => { rc.options.scales.x.type = rLog.checked ? "logarithmic" : "linear"; rc.update(); });
})();
