/* Mesh Tracker front end: live map of Meshtastic nodes. */
(function () {
  "use strict";

  const STALE_AFTER_S = 120;
  const COLORS = ["#ff4d4d", "#ffb020", "#3fd0ff", "#b36bff", "#4dff88", "#ff6bd5", "#f5f55a", "#7f9cff"];
  const PLANE_SVG = '<svg viewBox="0 0 24 24"><path d="M21 16v-2l-8-5V3.5a1.5 1.5 0 00-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z"/></svg>';
  const DOT_SVG = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="5"/></svg>';
  const BASE_SVG = '<svg viewBox="0 0 24 24"><path d="M11 9h2v13h-2zM7.8 5.6l1.4 1.4a4 4 0 000 6l-1.4 1.4a6 6 0 010-8.8zm8.4 0a6 6 0 010 8.8L14.8 13a4 4 0 000-6zM5 2.8l1.4 1.4a8 8 0 000 11.6L5 17.2a10 10 0 010-14.4zm14 0a10 10 0 010 14.4l-1.4-1.4a8 8 0 000-11.6z"/></svg>';

  const state = {
    nodes: new Map(),     // num -> node summary
    tracks: new Map(),    // num -> [[t, lat, lon, alt, speed, course], ...]
    markers: new Map(),   // num -> L.marker
    lines: new Map(),     // num -> L.polyline
    colors: new Map(),
    localNum: null,
    home: null,
    selected: null,
    follow: false,
    units: localStorage.getItem("units") || "metric",
    fittedOnce: false,
    tilesInfo: null,
  };

  // ------------------------------------------------------------------ helpers
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const isNum = (v) => typeof v === "number" && isFinite(v);
  const now = () => Date.now() / 1000;

  function colorFor(num) {
    if (!state.colors.has(num)) {
      if (num === state.localNum) state.colors.set(num, "#3fa7ff");
      else {
        const used = state.colors.size;
        state.colors.set(num, COLORS[used % COLORS.length]);
      }
    }
    return state.colors.get(num);
  }

  const imperial = () => state.units === "imperial";
  function fmtAlt(m) { if (!isNum(m)) return "—"; return imperial() ? `${Math.round(m * 3.28084)} ft` : `${Math.round(m)} m`; }
  function fmtDist(m) {
    if (!isNum(m)) return "—";
    if (imperial()) { const ft = m * 3.28084; return ft < 1000 ? `${Math.round(ft)} ft` : `${(m / 1609.344).toFixed(2)} mi`; }
    return m < 1000 ? `${Math.round(m)} m` : `${(m / 1000).toFixed(2)} km`;
  }
  function fmtSpeed(ms) { if (!isNum(ms)) return "—"; return imperial() ? `${(ms * 2.23694).toFixed(1)} mph` : `${(ms * 3.6).toFixed(1)} km/h`; }
  function fmtClimb(ms) {
    if (!isNum(ms)) return "—";
    const s = ms > 0.05 ? "▲ " : ms < -0.05 ? "▼ " : "";
    return imperial() ? `${s}${Math.abs(ms * 196.85).toFixed(0)} ft/min` : `${s}${Math.abs(ms).toFixed(1)} m/s`;
  }
  function fmtDeg(d) { if (!isNum(d)) return "—"; const dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]; return `${Math.round(d)}° ${dirs[Math.round(d / 45) % 8]}`; }
  function fmtTemp(c) { if (!isNum(c)) return "—"; return imperial() ? `${(c * 9 / 5 + 32).toFixed(1)} °F` : `${c.toFixed(1)} °C`; }
  function fmtDuration(s) {
    if (!isNum(s)) return "—";
    s = Math.max(0, Math.round(s));
    const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
    if (d) return `${d}d ${h}h ${m}m`;
    if (h) return `${h}h ${m}m`;
    if (m) return `${m}m ${sec}s`;
    return `${sec}s`;
  }
  function fmtAgo(ts) { if (!isNum(ts)) return "never"; return `${fmtDuration(now() - ts)} ago`; }
  const ago = (ts) => `<span data-ago="${isNum(ts) ? ts : ""}">${fmtAgo(ts)}</span>`;
  function fmtTime(ts) { return isNum(ts) ? new Date(ts * 1000).toLocaleTimeString() : "—"; }

  function battery(node) {
    const dm = (node.metrics || {}).deviceMetrics || {};
    const lvl = dm.batteryLevel;
    if (!isNum(lvl)) return { text: "", low: false, level: null };
    if (lvl > 100) return { text: "⚡ ext", low: false, level: lvl };
    return { text: `🔋 ${lvl}%`, low: lvl <= 20, level: lvl };
  }
  const hasPos = (n) => n.position && isNum(n.position.latitude) && isNum(n.position.longitude);
  const isStale = (n) => !isNum(n.lastHeard) || now() - n.lastHeard > STALE_AFTER_S;
  const label = (k) => k.replace(/^_/, "").replace(/([a-z])([A-Z])/g, "$1 $2").replace(/^./, (c) => c.toUpperCase());

  // ------------------------------------------------------------------ map
  const map = L.map("map", { zoomControl: true, maxZoom: 21, worldCopyJump: true }).setView([20, 0], 3);
  const tileLayer = L.tileLayer("/tiles/{z}/{x}/{y}", {
    maxZoom: 21,
    attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics",
  }).addTo(map);
  let scale = null;
  function updateScale() {
    if (scale) scale.remove();
    scale = L.control.scale({ metric: !imperial(), imperial: imperial(), position: "bottomleft" }).addTo(map);
  }
  updateScale();

  let homeMarker = null;
  let distLine = null;

  map.on("contextmenu", (e) => {
    const html = `<div>${e.latlng.lat.toFixed(6)}, ${e.latlng.lng.toFixed(6)}</div>
      <button class="btn small" id="set-home-btn" style="margin-top:6px">Set home here</button>`;
    const popup = L.popup().setLatLng(e.latlng).setContent(html).openOn(map);
    setTimeout(() => {
      const b = document.getElementById("set-home-btn");
      if (b) b.onclick = () => {
        api("POST", "/api/home", { lat: e.latlng.lat, lon: e.latlng.lng, alt: state.home && state.home.alt });
        map.closePopup(popup);
      };
    }, 0);
  });
  map.on("dragstart", () => { if (state.follow) setFollow(false); });

  function markerHtml(node) {
    const b = battery(node);
    const isLocal = node.num === state.localNum;
    const course = (node.derived || {}).course;
    const moving = isNum((node.derived || {}).speed) && node.derived.speed > 1;
    let glyph = isLocal ? BASE_SVG : DOT_SVG;
    if (!isLocal && isNum(course) && moving) glyph = `<div style="transform:rotate(${course}deg);display:flex">${PLANE_SVG}</div>`;
    const cls = ["mk", isLocal ? "local" : "", isStale(node) ? "stale" : "", node.positionFromDb ? "dbpos" : "",
      state.selected === node.num ? "selected" : ""].join(" ");
    return `<div class="${cls}" style="--c:${colorFor(node.num)}">
      <div class="mk-dot">${glyph}</div>
      <div class="mk-label"><b>${esc(node.name)}</b>${b.text ? `<span class="batt ${b.low ? "low" : ""}">${b.text}</span>` : ""}</div>
    </div>`;
  }

  function renderMarker(node) {
    let m = state.markers.get(node.num);
    if (!hasPos(node)) { if (m) { m.remove(); state.markers.delete(node.num); } return; }
    const ll = [node.position.latitude, node.position.longitude];
    const icon = L.divIcon({ html: markerHtml(node), className: "", iconSize: [0, 0] });
    if (!m) {
      m = L.marker(ll, { icon, riseOnHover: true }).addTo(map);
      m.on("click", () => select(node.num, window.innerWidth <= 760));
      state.markers.set(node.num, m);
    } else {
      m.setLatLng(ll);
      m.setIcon(icon);
    }
    m.setZIndexOffset(state.selected === node.num ? 1000 : node.num === state.localNum ? -100 : 0);
  }

  function renderTrack(num) {
    const pts = state.tracks.get(num) || [];
    const latlngs = pts.map((p) => [p[1], p[2]]);
    let line = state.lines.get(num);
    if (!line) {
      line = L.polyline(latlngs, { color: colorFor(num), weight: 3, opacity: 0.85 }).addTo(map);
      state.lines.set(num, line);
      line.on("click", () => select(num, false));
    } else {
      line.setLatLngs(latlngs);
    }
  }

  function renderHome() {
    const h = state.home;
    if (!h || h.source !== "manual") {
      if (homeMarker) { homeMarker.remove(); homeMarker = null; }
    } else {
      const icon = L.divIcon({ html: '<div class="home-icon">🏠</div>', className: "", iconSize: [22, 22], iconAnchor: [11, 11] });
      if (!homeMarker) {
        homeMarker = L.marker([h.lat, h.lon], { icon }).addTo(map);
        homeMarker.bindPopup(() => {
          const div = document.createElement("div");
          div.innerHTML = `<b>Home (manual)</b><br>Distances and heights are measured from here.<br>
            <button class="btn small" style="margin-top:6px">Use base station GPS instead</button>`;
          div.querySelector("button").onclick = () => { api("DELETE", "/api/home"); map.closePopup(); };
          return div;
        });
      } else homeMarker.setLatLng([h.lat, h.lon]);
    }
    renderDistLine();
  }

  function renderDistLine() {
    const n = state.nodes.get(state.selected);
    const h = state.home;
    if (!n || !h || !hasPos(n) || n.num === state.localNum && h.source !== "manual") {
      if (distLine) { distLine.remove(); distLine = null; }
      return;
    }
    const ll = [[h.lat, h.lon], [n.position.latitude, n.position.longitude]];
    const txt = fmtDist((n.derived || {}).distanceFromHome);
    if (!distLine) {
      distLine = L.polyline(ll, { color: "#fff", weight: 1.5, dashArray: "6 6", opacity: 0.8, interactive: false }).addTo(map);
      distLine.bindTooltip(txt, { permanent: true, direction: "center", className: "dist-label" });
    } else {
      distLine.setLatLngs(ll);
      distLine.setTooltipContent(txt);
    }
  }

  function fitAll() {
    const pts = [];
    state.nodes.forEach((n) => { if (hasPos(n)) pts.push([n.position.latitude, n.position.longitude]); });
    state.tracks.forEach((t) => t.forEach((p) => pts.push([p[1], p[2]])));
    if (pts.length === 1) map.setView(pts[0], 17);
    else if (pts.length > 1) map.fitBounds(L.latLngBounds(pts).pad(0.15), { maxZoom: 18 });
    return pts.length > 0;
  }

  // ------------------------------------------------------------------ list panel
  function renderList() {
    const nodes = [...state.nodes.values()].sort((a, b) => {
      if (a.num === state.localNum) return -1;
      if (b.num === state.localNum) return 1;
      return (b.lastHeard || 0) - (a.lastHeard || 0);
    });
    $("node-count").textContent = `(${nodes.length})`;
    $("node-list").innerHTML = nodes.map((n) => {
      const b = battery(n);
      const d = n.derived || {};
      const bits = [];
      if (n.num === state.localNum) bits.push("this radio");
      else if (isNum(d.distanceFromHome)) bits.push(fmtDist(d.distanceFromHome));
      if (isNum(d.altitude) && n.num !== state.localNum) bits.push(fmtAlt(isNum(d.heightAboveLaunch) ? d.heightAboveLaunch : d.altitude) + (isNum(d.heightAboveLaunch) ? " AGL" : ""));
      if (!hasPos(n)) bits.push("no GPS");
      return `<div class="node-row ${state.selected === n.num ? "selected" : ""} ${isStale(n) ? "stale" : ""}" data-num="${n.num}">
        <span class="swatch" style="background:${colorFor(n.num)}"></span>
        <div class="info">
          <div class="nm">${esc(n.name)}${n.shortName ? `<span class="tag">${esc(n.shortName)}</span>` : ""}</div>
          <div class="sub">${esc(bits.join(" · "))}${bits.length ? " · " : ""}${ago(n.lastHeard)}</div>
        </div>
        <div class="small ${b.low ? "bad" : "muted"}">${b.text}</div>
      </div>`;
    }).join("") || `<div class="muted small" style="padding:12px">No devices heard yet.</div>`;
    $("node-list").querySelectorAll(".node-row").forEach((el) => {
      el.onclick = () => select(Number(el.dataset.num), true);
    });
  }

  function renderBaseChip() {
    const n = state.nodes.get(state.localNum);
    $("base-name").textContent = n ? n.name : "—";
    $("base-batt").textContent = n ? battery(n).text : "";
  }

  // ------------------------------------------------------------------ detail panel
  function kv(rows) {
    const html = rows.filter((r) => r && r[1] !== undefined && r[1] !== null && r[1] !== "" && r[1] !== "—")
      .map(([k, v]) => `<div class="k">${esc(k)}</div><div class="v">${v}</div>`).join("");
    return html ? `<div class="kv">${html}</div>` : "";
  }
  const section = (title, body) => body ? `<div class="section"><h4>${esc(title)}</h4>${body}</div>` : "";
  const v = (x) => (x === undefined || x === null ? null : esc(x));
  const num = (x, digits = 0, unit = "") => (isNum(x) ? `${x.toFixed(digits)}${unit}` : null);

  function genericMetrics(obj, skip = []) {
    if (!obj) return "";
    return kv(Object.entries(obj).filter(([k]) => !skip.includes(k) && k !== "_updated" && k !== "time")
      .map(([k, val]) => [label(k), isNum(val) ? String(Math.round(val * 1000) / 1000) : esc(JSON.stringify(val))]));
  }

  function renderDetail() {
    const n = state.nodes.get(state.selected);
    const panel = $("detail-panel");
    if (!n) { panel.classList.add("hidden"); renderDistLine(); return; }
    panel.classList.remove("hidden");
    const u = n.user || {};
    const p = n.position || {};
    const d = n.derived || {};
    const st = n.stats || {};
    const m = n.metrics || {};
    const dm = m.deviceMetrics || {};
    const env = m.environmentMetrics || {};
    const r = n.radio || {};
    const b = battery(n);
    const isLocal = n.num === state.localNum;

    $("detail-title").innerHTML = `<div class="big"><span class="swatch" style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${colorFor(n.num)};margin-right:6px"></span>${esc(n.name)}</div>
      <div class="small muted">${esc(n.id)}${u.shortName ? " · " + esc(u.shortName) : ""}${isLocal ? " · connected radio" : ""} · heard ${ago(n.lastHeard)}</div>`;
    $("btn-gpx").href = `/api/nodes/${encodeURIComponent(n.num)}/track.gpx`;
    $("btn-csv").href = `/api/nodes/${encodeURIComponent(n.num)}/track.csv`;
    $("btn-follow").classList.toggle("active", state.follow);

    const alt = isNum(d.altitude) ? d.altitude : p.altitude;
    const height = isNum(d.heightAboveHome) ? d.heightAboveHome : d.heightAboveLaunch;
    const heightLbl = isNum(d.heightAboveHome) ? "Height (above home)" : "Height (above launch)";
    let hero = "";
    if (hasPos(n)) {
      const cells = [
        [heightLbl, fmtAlt(height)],
        ["Speed", fmtSpeed(d.speed)],
        ["Climb", fmtClimb(d.climbRate)],
        ["Distance", fmtDist(d.distanceFromHome)],
        ["Altitude (MSL)", fmtAlt(alt)],
        ["Battery", b.text || "—"],
      ].filter((c) => !(isLocal && ["Distance", "Speed", "Climb", heightLbl].includes(c[0]) && c[1] === "—"));
      hero = `<div class="hero">${cells.map(([l, val]) => `<div class="cell"><div class="lbl">${esc(l)}</div><div class="val">${esc(val)}</div></div>`).join("")}</div>`;
    } else {
      hero = `<p class="muted small" style="margin-top:12px">No GPS position received from this device yet. Make sure its GPS is enabled,
        it has a fix, and position sharing is on for the channel.</p>`;
    }

    const html = [
      n.positionFromDb ? `<p class="small" style="color:var(--warn)">Showing last known position from the radio's memory; nothing new heard since connecting.</p>` : "",
      hero,
      section("Position", hasPos(n) ? kv([
        ["Latitude", num(p.latitude, 6)],
        ["Longitude", num(p.longitude, 6)],
        ["Altitude (MSL)", fmtAlt(alt)],
        ["Altitude (HAE)", isNum(p.altitudeHae) ? fmtAlt(p.altitudeHae) : null],
        ["Height above launch", isNum(d.heightAboveLaunch) ? fmtAlt(d.heightAboveLaunch) : null],
        ["Height above home", isNum(d.heightAboveHome) ? fmtAlt(d.heightAboveHome) : null],
        ["Pressure altitude", isNum(d.pressureAltitude) ? fmtAlt(d.pressureAltitude) : null],
        ["Ground speed (GPS)", isNum(d.reportedSpeed) ? fmtSpeed(d.reportedSpeed) : null],
        ["Speed (from fixes)", isNum(d.computedSpeed) ? fmtSpeed(d.computedSpeed) : null],
        ["Climb rate", isNum(d.climbRate) ? fmtClimb(d.climbRate) : null],
        ["Course (GPS)", isNum(d.reportedCourse) ? fmtDeg(d.reportedCourse) : null],
        ["Course (from fixes)", isNum(d.computedCourse) ? fmtDeg(d.computedCourse) : null],
        ["Distance from home", isNum(d.distanceFromHome) ? fmtDist(d.distanceFromHome) : null],
        ["Bearing from home", isNum(d.bearingFromHome) ? fmtDeg(d.bearingFromHome) : null],
        ["Satellites", v(p.satsInView)],
        ["PDOP / HDOP / VDOP", [p.PDOP, p.HDOP, p.VDOP].some(isNum) ? [p.PDOP, p.HDOP, p.VDOP].map((x) => isNum(x) ? (x / 100).toFixed(2) : "–").join(" / ") : null],
        ["Fix type", isNum(p.fixType) ? ({ 0: "none", 1: "none", 2: "2D", 3: "3D", 4: "3D+DGPS", 5: "RTK" }[p.fixType] || p.fixType) : null],
        ["Fix quality", v(p.fixQuality)],
        ["Precision", isNum(p.precisionBits) ? (p.precisionBits >= 32 ? "full" : `${p.precisionBits} bits (reduced)`) : null],
        ["Location source", v(p.locationSource)],
        ["Altitude source", v(p.altitudeSource)],
        ["GPS time", isNum(p.time) ? fmtTime(p.time) : null],
        ["Position seq #", v(p.seqNumber)],
        ["Position received", isNum(n.lastPositionTime) ? ago(n.lastPositionTime) : null],
      ]) : ""),
      section("Flight since connected", st.fixCount ? kv([
        ["Max height (launch)", isNum(st.maxHeightAboveLaunch) ? fmtAlt(st.maxHeightAboveLaunch) : null],
        ["Max altitude (MSL)", isNum(st.maxAltitude) ? fmtAlt(st.maxAltitude) : null],
        ["Launch altitude", isNum(st.launchAltitude) ? fmtAlt(st.launchAltitude) : null],
        ["Max speed", isNum(st.maxSpeed) ? fmtSpeed(st.maxSpeed) : null],
        ["Max climb", isNum(st.maxClimb) ? fmtClimb(st.maxClimb) : null],
        ["Max sink", isNum(st.maxSink) ? fmtClimb(st.maxSink) : null],
        ["Max distance from home", isNum(st.maxDistanceFromHome) ? fmtDist(st.maxDistanceFromHome) : null],
        ["Distance travelled", isNum(st.distanceTravelled) ? fmtDist(st.distanceTravelled) : null],
        ["Tracking time", fmtDuration(st.trackDuration)],
        ["Position fixes", v(st.fixCount)],
        ["Avg fix interval", st.fixCount > 1 && isNum(st.trackDuration) ? `${(st.trackDuration / (st.fixCount - 1)).toFixed(1)} s` : null],
      ]) : ""),
      section("Power & device", kv([
        ["Battery", b.level === null ? null : b.level > 100 ? "External power" : `${b.level}%`],
        ["Voltage", num(dm.voltage, 2, " V")],
        ["Uptime", isNum(dm.uptimeSeconds) ? fmtDuration(dm.uptimeSeconds) : null],
        ["Channel utilisation", num(dm.channelUtilization, 1, " %")],
        ["Air util TX", num(dm.airUtilTx, 2, " %")],
        ["Telemetry received", isNum(dm._updated) ? ago(dm._updated) : null],
      ])),
      section("Identity", kv([
        ["Long name", v(u.longName)],
        ["Short name", v(u.shortName)],
        ["Node ID", v(n.id)],
        ["Node number", v(n.num)],
        ["Hardware", v(u.hwModel)],
        ["Role", v(u.role || n.extra.role)],
        ["Licensed (ham)", u.isLicensed ? "yes" : null],
        ["MAC", v(u.macaddr)],
        ["Firmware", v(n.extra.firmwareVersion)],
        ["Region", v(n.extra.region)],
        ["Modem preset", v(n.extra.modemPreset)],
        ["Position interval", isNum(n.extra.positionBroadcastSecs) && n.extra.positionBroadcastSecs ? `${n.extra.positionBroadcastSecs} s` : null],
        ["GPS mode", v(n.extra.gpsMode)],
        ["Reboot count", v(n.extra.rebootCount)],
        ["Public key", u.publicKey ? `<span class="small">${esc(u.publicKey)}</span>` : null],
      ])),
      isLocal ? "" : section("Radio link", kv([
        ["SNR", num(r.rxSnr ?? r.snr, 2, " dB")],
        ["RSSI", num(r.rxRssi, 0, " dBm")],
        ["Hops away", v(r.hopsAway)],
        ["Hop limit / start", isNum(r.hopLimit) ? `${r.hopLimit} / ${r.hopStart ?? "?"}` : null],
        ["Via MQTT", r.viaMqtt ? "yes" : null],
        ["Relayed by", isNum(r.relayNode) && r.relayNode ? `…${r.relayNode.toString(16)}` : null],
        ["Channel", v(r.channel)],
        ["First heard", isNum(n.firstHeard) ? fmtTime(n.firstHeard) : null],
        ["Last heard", ago(n.lastHeard)],
        ["Packets received", v(n.packetCount)],
        ["Packet types", Object.entries(n.packetTypes || {}).map(([k, c]) => `${esc(k.replace("_APP", ""))}: ${c}`).join("<br>") || null],
      ])),
      section("Environment sensors", Object.keys(env).length ? kv([
        ["Temperature", isNum(env.temperature) ? fmtTemp(env.temperature) : null],
        ["Humidity", num(env.relativeHumidity, 0, " %")],
        ["Pressure", num(env.barometricPressure, 1, " hPa")],
      ]) + genericMetrics(env, ["temperature", "relativeHumidity", "barometricPressure"]) : ""),
      ...Object.entries(m).filter(([k]) => !["deviceMetrics", "environmentMetrics"].includes(k))
        .map(([k, obj]) => section(label(k), genericMetrics(obj))),
      `<details id="raw-details" ${state.rawOpen ? "open" : ""}><summary>All raw data</summary><pre class="raw">${esc(JSON.stringify(n, null, 2))}</pre></details>`,
    ].join("");
    $("detail-body").innerHTML = html;
    $("raw-details").ontoggle = (e) => { state.rawOpen = e.target.open; };
    renderDistLine();
  }

  // Pan so a point is visible; on phones the details sheet covers the bottom of the map.
  function centerOn(lat, lon) {
    const covered = window.innerWidth <= 760 && state.selected !== null ? $("map").clientHeight * 0.275 : 0;
    const z = map.getZoom();
    map.panTo(map.unproject(map.project([lat, lon], z).add([0, covered]), z));
  }

  function select(num, pan) {
    state.selected = num;
    const n = state.nodes.get(num);
    if (n && pan && hasPos(n)) centerOn(n.position.latitude, n.position.longitude);
    if (window.innerWidth <= 760) $("list-panel").classList.add("collapsed");
    state.nodes.forEach(renderMarker);
    renderList();
    renderDetail();
  }

  function setFollow(on) {
    state.follow = on;
    $("btn-follow").classList.toggle("active", on);
    const n = state.nodes.get(state.selected);
    if (on && n && hasPos(n)) centerOn(n.position.latitude, n.position.longitude);
  }

  // ------------------------------------------------------------------ data
  let listTimer = null;
  function scheduleList() {
    if (listTimer) return;
    listTimer = setTimeout(() => { listTimer = null; renderList(); renderBaseChip(); }, 250);
  }

  function setConnection(c) {
    const el = $("conn");
    el.className = `pill ${c.status || ""}`;
    $("conn-text").textContent = c.detail || c.status;
    el.title = c.detail || "";
  }

  function onNode(n, point) {
    state.nodes.set(n.num, n);
    if (point) {
      let t = state.tracks.get(n.num);
      if (!t) { t = []; state.tracks.set(n.num, t); }
      if (!t.length || point[0] > t[t.length - 1][0]) {
        t.push(point);
        const line = state.lines.get(n.num);
        if (line) line.addLatLng([point[1], point[2]]); else renderTrack(n.num);
      }
    }
    renderMarker(n);
    if (n.num === state.selected) {
      renderDetail();
      if (state.follow && hasPos(n)) centerOn(n.position.latitude, n.position.longitude);
    }
    if (!state.fittedOnce && hasPos(n)) { state.fittedOnce = fitAll(); }
    scheduleList();
  }

  function onMessage(msg) {
    switch (msg.type) {
      case "snapshot":
        state.localNum = msg.localNum;
        state.home = msg.home;
        setConnection(msg.connection);
        state.tracks = new Map(Object.entries(msg.tracks).map(([k, val]) => [Number(k), val]));
        state.lines.forEach((l) => l.remove());
        state.lines.clear();
        state.tracks.forEach((_, num) => renderTrack(num));
        msg.nodes.forEach((n) => onNode(n, null));
        renderHome();
        renderList(); renderBaseChip();
        $("list-footer").textContent = msg.logPath ? `Logging fixes to ${msg.logPath.split(/[\\/]/).pop()}` : "";
        $("list-footer").title = msg.logPath || "";
        break;
      case "node":
        if (msg.node.isLocal && state.localNum !== msg.node.num) state.localNum = msg.node.num;
        onNode(msg.node, msg.point || null);
        break;
      case "connection":
        setConnection(msg.connection);
        break;
      case "home":
        state.home = msg.home;
        renderHome();
        break;
      case "reset":
        state.tracks.set(msg.num, []);
        renderTrack(msg.num);
        break;
    }
  }

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onmessage = (e) => onMessage(JSON.parse(e.data));
    ws.onclose = () => {
      setConnection({ status: "error", detail: "Lost contact with tracker program, retrying…" });
      setTimeout(connect, 2000);
    };
  }

  async function api(method, url, body) {
    const res = await fetch(url, {
      method, headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || res.statusText);
    return data;
  }

  // ------------------------------------------------------------------ offline map dialog
  function lon2tile(lon, z) { return Math.floor((lon + 180) / 360 * 2 ** z); }
  function lat2tile(lat, z) { const r = lat * Math.PI / 180; return Math.floor((1 - Math.asinh(Math.tan(r)) / Math.PI) / 2 * 2 ** z); }
  function tileCount(b, zmin, zmax) {
    let total = 0;
    for (let z = zmin; z <= zmax; z++) {
      const nx = lon2tile(b.getEast(), z) - lon2tile(b.getWest(), z) + 1;
      const ny = lat2tile(b.getSouth(), z) - lat2tile(b.getNorth(), z) + 1;
      total += nx * ny;
    }
    return total;
  }
  function updateEstimate() {
    const zmin = Number($("dl-min").value), zmax = Number($("dl-max").value);
    const n = tileCount(map.getBounds(), zmin, zmax);
    $("dl-estimate").innerHTML = `About <b>${n.toLocaleString()}</b> tiles (~${Math.max(1, Math.round(n * 25 / 1024))} MB) for the visible area.` +
      (n > 100000 ? ` <span style="color:var(--bad)">Too many — zoom the map in or lower the max zoom.</span>` : "");
    $("dl-start").disabled = n > 100000 || zmin > zmax;
  }
  let pollTimer = null;
  async function pollDownload() {
    const s = await api("GET", "/api/tiles/download").catch(() => null);
    if (!s) return;
    if (s.total) {
      $("dl-progress").classList.remove("hidden");
      $("dl-bar").style.width = `${(100 * s.done / s.total).toFixed(1)}%`;
      $("dl-text").textContent = `${s.done.toLocaleString()} / ${s.total.toLocaleString()} — ${s.downloaded} new, ${s.skipped} already saved, ${s.failed} failed` +
        (s.running ? "" : s.cancelled ? " (cancelled)" : " — done") + (s.error && s.failed ? `. Last error: ${s.error.slice(0, 160)}` : "");
    }
    $("dl-start").textContent = s.running ? "Stop" : "Download";
    if (s.running) pollTimer = setTimeout(pollDownload, 1000);
    else { pollTimer = null; tileLayer.redraw(); loadConfig(); }
  }
  $("btn-offline").onclick = () => {
    const z = map.getZoom();
    $("dl-min").value = Math.max(1, Math.min(z, 12) - 2);
    $("dl-max").value = Math.max(z, 18);
    updateEstimate();
    const ti = state.tilesInfo;
    $("dl-tiles-info").textContent = ti ? `Saved so far: ${ti.tileCount.toLocaleString()} tiles` +
      (ti.minZoom !== null ? ` (zoom ${ti.minZoom}–${ti.maxZoom})` : "") + ` in ${ti.dir}` : "";
    $("offline-dialog").showModal();
    pollDownload();
  };
  $("dl-min").oninput = $("dl-max").oninput = updateEstimate;
  $("dl-cancel").onclick = () => $("offline-dialog").close();
  $("dl-start").onclick = async () => {
    if ($("dl-start").textContent === "Stop") { await api("DELETE", "/api/tiles/download"); return; }
    const b = map.getBounds();
    try {
      await api("POST", "/api/tiles/download", {
        west: b.getWest(), south: b.getSouth(), east: b.getEast(), north: b.getNorth(),
        minZoom: Number($("dl-min").value), maxZoom: Number($("dl-max").value),
      });
      if (!pollTimer) pollDownload();
    } catch (e) { $("dl-estimate").textContent = `Error: ${e.message}`; }
  };

  // ------------------------------------------------------------------ buttons
  $("btn-list").onclick = () => $("list-panel").classList.toggle("collapsed");
  $("btn-fit").onclick = () => { setFollow(false); fitAll(); };
  $("btn-close-detail").onclick = () => { state.selected = null; setFollow(false); state.nodes.forEach(renderMarker); renderList(); renderDetail(); };
  $("btn-follow").onclick = () => setFollow(!state.follow);
  $("btn-reset").onclick = () => {
    const n = state.nodes.get(state.selected);
    if (n && confirm(`Clear the trail and flight stats for ${n.name}?`)) api("POST", `/api/nodes/${n.num}/reset`);
  };
  $("base-chip").onclick = () => { if (state.localNum !== null) select(state.localNum, true); };
  function applyUnits() {
    $("btn-units").textContent = imperial() ? "Imperial" : "Metric";
    updateScale();
    renderList(); renderDetail(); renderDistLine();
  }
  $("btn-units").onclick = () => {
    state.units = imperial() ? "metric" : "imperial";
    try { localStorage.setItem("units", state.units); } catch (e) { /* private mode */ }
    applyUnits();
  };
  if (window.innerWidth <= 760) $("list-panel").classList.add("collapsed");

  // refresh "x ago" labels and stale styling
  setInterval(() => {
    document.querySelectorAll("[data-ago]").forEach((el) => {
      const ts = parseFloat(el.dataset.ago);
      el.textContent = fmtAgo(isNaN(ts) ? null : ts);
    });
  }, 1000);
  setInterval(() => { state.nodes.forEach(renderMarker); scheduleList(); }, 10000);

  async function loadConfig() {
    const cfg = await api("GET", "/api/config").catch(() => null);
    if (!cfg) return null;
    state.tilesInfo = cfg.tiles;
    const banner = $("banner");
    if (!cfg.tiles.tileCount && !cfg.tiles.fetchMissing) {
      banner.innerHTML = `No offline map saved yet. While online, pan to your flying field and press <b>Offline map</b>
        (or run <code>python -m tracker.tiles --lat … --lon …</code>). Tracking works without a map.`;
      banner.classList.remove("hidden");
    } else banner.classList.add("hidden");
    return cfg;
  }

  (async () => {
    applyUnits();
    const cfg = await loadConfig();
    if (cfg && !state.fittedOnce) {
      if (cfg.initialView) map.setView([cfg.initialView.lat, cfg.initialView.lon], cfg.initialView.zoom);
      else if (cfg.tiles.bounds) {
        const [w, s, e, n] = cfg.tiles.bounds;
        map.fitBounds([[s, w], [n, e]]);
      }
    }
    connect();
  })();
})();
