// WayDine frontend: plain JS + Leaflet. Talks to the FastAPI backend (POST /trip).

const map = L.map("map").setView([17.3, 73.4], 8);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

const layers = {
  route: L.layerGroup().addTo(map),
  gaps: L.layerGroup().addTo(map),
  rejected: L.layerGroup().addTo(map),
  accepted: L.layerGroup().addTo(map),
  top: L.layerGroup().addTo(map),
};
const markerById = {};
let presets = {};

const $ = (id) => document.getElementById(id);

// OSM names are user-contributed text: always escape before inserting as HTML
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function badge(field, r) {
  return (r.unverified || []).includes(field) ? '<span class="badge">unverified</span>' : "";
}

// ------------------------------------------------------------------ form
async function loadPresets() {
  presets = await (await fetch("/presets")).json();
  for (const sel of [$("src"), $("dst")]) {
    for (const [key, p] of Object.entries(presets)) {
      sel.add(new Option(p.label, key));
    }
    sel.add(new Option("Custom lat/lon", "custom"));
  }
  $("src").value = "mumbai_panvel";
  $("dst").value = "goa_panaji";
  $("src").onchange = () => ($("custom-src").hidden = $("src").value !== "custom");
  $("dst").onchange = () => ($("custom-dst").hidden = $("dst").value !== "custom");
}

function point(selId, latId, lonId) {
  const key = $(selId).value;
  if (key === "custom") return { lat: parseFloat($(latId).value), lon: parseFloat($(lonId).value) };
  return { lat: presets[key].lat, lon: presets[key].lon };
}

function requestBody() {
  return {
    source: point("src", "src-lat", "src-lon"),
    destination: point("dst", "dst-lat", "dst-lon"),
    departure_time: $("dep").value,
    date: $("trip-date").value || undefined,   // weekday, monsoon and festival rules use it
    current_km: parseFloat($("cur-km").value) || 0,
    night_mode: $("night").checked,
    preferences: { veg_pref: $("veg").value, max_budget_band: $("budget").value },
  };
}

// --------------------------------------------------------------- popups
function restaurantPopup(it, rejected) {
  const r = it.restaurant;
  const hours = r.is_24x7 ? "Open 24x7" : `${esc(r.opens)}-${esc(r.closes)}`;
  const lit = r.is_well_lit === true ? "yes" : r.is_well_lit === false ? "no" : "unknown";
  let html = `<h4>${esc(r.name)}</h4>
    <div>${esc(r.amenity_type.replace("_", " "))} &middot; ${esc((r.cuisines || []).join(", "))}</div>`;
  if (rejected) {
    html += `<div class="r" style="color:#c4171d;font-weight:700;margin:4px 0">Rejected: ${esc(it.reason)}</div>`;
  } else {
    html += `<div style="margin:4px 0"><b>${esc(it.explanation || "")}</b></div>`;
  }
  html += `<div>Hours: ${hours}${badge("hours", r)}</div>
    <div>Veg: ${esc(r.veg_type)}${badge("veg", r)}</div>
    <div>Cost band: ${esc(r.avg_cost_band)}${badge("cost_band", r)}</div>
    <div>Parking: ${r.has_parking ? "yes" : "no"}${badge("parking", r)}</div>
    <div>Road lit: ${lit}${badge("lighting", r)}</div>`;
  if (it.chainage_km != null) html += `<div>At km ${it.chainage_km.toFixed(1)}, ${it.offset_m} m from road${it.side ? ", " + it.side + " side" : ""}</div>`;
  if (it.typical_items && it.typical_items.length) {
    html += `<div class="items"><b>Typical items</b> (not the actual menu)${badge("menu", r)}: ${it.typical_items.map((x) => esc(x.item) + " ~&#8377;" + x.typical_price).join(", ")}</div>`;
  }
  (it.warnings || []).forEach((w) => (html += `<div class="warn">&#9888; ${esc(w)}</div>`));
  html += `<div class="popup-src">Source: OpenStreetMap (${esc(r.osm_id)})</div>`;
  return html;
}

// --------------------------------------------------------------- render
function render(data) {
  Object.values(layers).forEach((l) => l.clearLayers());
  for (const k in markerById) delete markerById[k];

  // route
  const line = L.polyline(data.route.coords, { color: "#1557d6", weight: 6, opacity: 0.85 });
  line.addTo(layers.route);
  map.fitBounds(line.getBounds(), { padding: [30, 30] });

  // food gaps in red
  data.gaps.forEach((g) => {
    L.polyline(g.coords, { color: "#c4171d", weight: 11, opacity: 0.75 })
      .bindPopup(`<h4>Food gap: ${g.length_km} km</h4>km ${g.start_km} to ${g.end_km}<br>
        Last open stop: <b>${esc(g.last_stop_before_gap)}</b><br>Next: ${esc(g.next_stop_after_gap)}`)
      .addTo(layers.gaps);
  });

  // rejected (grey), accepted (green), top 5 (yellow, numbered)
  data.rejected.forEach((it) => {
    const r = it.restaurant;
    const m = L.circleMarker([r.lat, r.lon], { radius: 5, color: "#333", weight: 1, fillColor: "#7b8490", fillOpacity: 0.8 })
      .bindPopup(restaurantPopup(it, true)).addTo(layers.rejected);
    markerById[r.id] = m;
  });
  const topIds = new Set(data.top.map((it) => it.restaurant.id));
  data.accepted.forEach((it) => {
    const r = it.restaurant;
    if (topIds.has(r.id)) return;
    const m = L.circleMarker([r.lat, r.lon], { radius: 7, color: "#000", weight: 2, fillColor: "#0f8a3c", fillOpacity: 0.95 })
      .bindPopup(restaurantPopup(it, false)).addTo(layers.accepted);
    markerById[r.id] = m;
  });
  data.top.forEach((it, i) => {
    const r = it.restaurant;
    const icon = L.divIcon({
      className: "",
      html: `<div style="background:#ffd400;border:3px solid #000;border-radius:50%;width:28px;height:28px;display:flex;align-items:center;justify-content:center;font-weight:800">${i + 1}</div>`,
      iconSize: [28, 28], iconAnchor: [14, 14],
    });
    markerById[r.id] = L.marker([r.lat, r.lon], { icon, zIndexOffset: 1000 })
      .bindPopup(restaurantPopup(it, false)).addTo(layers.top);
  });

  renderBanners(data);
  renderSummary(data);
  renderTop(data.top);
  renderTimeline(data.plan);
  renderRejected(data);
}

function renderBanners(data) {
  const b = $("banners");
  b.innerHTML = "";
  const nightStops = data.accepted.some((it) => it.night) || data.rejected.some((it) => it.stage === "Night");
  if (data.night_mode.active_at_departure || nightStops) {
    const why = data.night_mode.enabled_by_user ? "switched on" : `auto: arrival between ${data.night_mode.night_start} and ${data.night_mode.night_end}`;
    const n = data.rejected.filter((it) => it.stage === "Night").length;
    b.innerHTML += `<div class="banner night">&#127769; Night safety mode active (${esc(why)}). ${n} unsafe place(s) greyed out.</div>`;
  }
  data.gaps.forEach((g) => {
    b.innerHTML += `<div class="banner gap">&#9888; Last open stop for ${Math.round(g.length_km)} km: ${esc(g.last_stop_before_gap)} (km ${g.start_km.toFixed(0)} &rarr; ${g.end_km.toFixed(0)})</div>`;
  });
}

function renderSummary(data) {
  const p = data.plan;
  const mins = data.route.duration_min;
  $("empty").hidden = true;
  $("results").hidden = false;
  $("summary").hidden = false;
  $("summary").innerHTML = `
    <div class="stats">
      <div class="stat"><b>${data.route.distance_km} km</b><small>distance</small></div>
      <div class="stat"><b>${Math.floor(mins / 60)} h ${String(mins % 60).padStart(2, "0")}</b><small>driving</small></div>
      <div class="stat"><b>${esc(p.arrival.replace(" (+1 day)", "+1"))}</b><small>arrive</small></div>
    </div>
    ${trafficLine(data.route)}
    <div class="chips">
      <span class="chip ok">${data.accepted.length} recommended</span>
      <span class="chip no">${data.rejected.length} rejected</span>
      ${data.gaps.length ? `<span class="chip gap">${data.gaps.length} food gap(s)</span>` : ""}
    </div>`;
}

// "+42 min traffic (Town evening peak 18 min, Ghat section 15 min ...)" from the congestion rules
function trafficLine(route) {
  if (route.traffic_delay_min == null || !route.traffic || !route.traffic.length) return "";
  const sign = route.traffic_delay_min >= 0 ? "+" : "";
  const parts = route.traffic.slice(0, 3)
    .map((t) => `${esc(t.rules)} ${t.minutes > 0 ? "+" : ""}${Math.round(t.minutes)} min`).join(", ");
  return `<span class="traffic">&#128678; <b>${sign}${route.traffic_delay_min} min traffic</b>
    (free-flow ${route.free_flow_min} min): ${parts}</span><br>`;
}

function focus(id) {
  const m = markerById[id];
  if (m) { map.setView(m.getLatLng(), 15); m.openPopup(); }
}

function renderTop(top) {
  const el = $("tab-top");
  if (!top.length) { el.innerHTML = '<p class="empty">No place passes all filters for this trip.</p>'; return; }
  el.innerHTML = top.map((it, i) => {
    const r = it.restaurant;
    const items = (it.typical_items || []).map((x) => esc(x.item)).join(", ");
    return `<div class="card" data-id="${r.id}">
      <h3><span><span class="rank">#${i + 1}</span> ${esc(r.name)}</span><span>${it.score}</span></h3>
      <div class="why">${esc(it.explanation)}</div>
      <div class="meta">km ${it.chainage_km.toFixed(1)} &middot; ${esc(r.amenity_type.replace("_", " "))} &middot; ${esc((r.cuisines || []).join(", "))}
        &middot; Source: OpenStreetMap</div>
      ${items ? `<div class="items"><b>Typical items:</b> ${items}</div>` : ""}
      ${(it.warnings || []).map((w) => `<div class="warn">&#9888; ${esc(w)}</div>`).join("")}
    </div>`;
  }).join("");
  el.querySelectorAll(".card").forEach((c) => (c.onclick = () => focus(+c.dataset.id)));
}

function renderTimeline(plan) {
  const el = $("tab-timeline");
  let html = `<p class="summary">Meals needed: <b>${plan.meals_required.join(", ") || "none"}</b>
    ${plan.meals_missed.length ? `<br><span class="warn">&#9888; No open stop for: ${plan.meals_missed.join(", ")}</span>` : ""}
    ${plan.overdue_legs ? `<br><span class="warn">&#9888; ${plan.overdue_legs} leg(s) longer than the rest interval</span>` : ""}
    <br>Total detour ${plan.total_detour_km} km &middot; longest drive ${Math.round(plan.longest_leg_min)} min</p>
    ${plan.timeline.length === 2 && !plan.meals_missed.length && !plan.overdue_legs
      ? `<p class="empty">No stop needed: the drive is under the ${plan.rest_interval_min} min rest interval and covers no full meal window (${plan.meal_stop_min} min). Try a longer trip, e.g. Demo 1.</p>` : ""}
    <ul class="timeline">`;
  plan.timeline.forEach((s) => {
    const label = s.type === "both" ? "meal + rest" : s.type;
    const items = (s.typical_items || []).map((x) => esc(x.item)).join(", ");
    html += `<li ${s.restaurant_id ? `data-id="${s.restaurant_id}"` : ""}>
      <div class="t">${esc(s.time)}</div>
      <div><span class="type ${s.type}">${label}</span> <b>${esc(s.name)}</b>
        ${s.meal && s.type !== "rest" ? `&middot; ${esc(s.meal)}` : ""}
        <div class="meta">km ${s.chainage_km.toFixed(1)}${s.depart ? ` &middot; leave ${esc(s.depart)}` : ""}${s.detour_km != null ? ` &middot; ${s.detour_km} km detour` : ""}
          ${s.hours_verified === false ? '<span class="badge">hours unverified</span>' : ""}</div>
        ${items ? `<div class="items"><b>Typical items:</b> ${items}</div>` : ""}
      </div></li>`;
  });
  el.innerHTML = html + "</ul>";
  el.querySelectorAll("li[data-id]").forEach((li) => (li.onclick = () => focus(+li.dataset.id)));
}

function renderRejected(data) {
  const el = $("tab-rejected");
  const summary = Object.entries(data.rejection_summary)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `<span>${esc(k)}: <b>${v}</b></span>`).join("");
  const rows = data.rejected.slice().sort((a, b) => a.chainage_km - b.chainage_km).map((it) =>
    `<div class="rej-row" data-id="${it.restaurant.id}">km ${it.chainage_km.toFixed(1)} &middot; <b>${esc(it.restaurant.name)}</b> &mdash; <span class="r">${esc(it.reason)}</span></div>`).join("");
  el.innerHTML = `<div class="rej-summary">${summary}</div>${rows || '<p class="empty">Nothing rejected.</p>'}`;
  el.querySelectorAll(".rej-row").forEach((r) => (r.onclick = () => focus(+r.dataset.id)));
}

// ----------------------------------------------------------------- wiring
let busy = false;

async function run(ev) {
  if (ev) ev.preventDefault();
  if (busy) return;  // a request is already running (live tracking can fire often)
  busy = true;
  $("loading").hidden = false;
  try {
    const res = await fetch("/trip", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(requestBody()),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    render(data);
  } catch (e) {
    $("banners").innerHTML = `<div class="banner gap">Error: ${esc(e.message)}</div>`;
  } finally {
    $("loading").hidden = true;
    busy = false;
  }
}

// ------------------------------------------------ current location / live
// Uses the browser's own GPS (navigator.geolocation). The detected point
// becomes the trip start, departure becomes "now", and the backend routes
// from here to the destination - so only places ahead of you are suggested.
const meLayer = L.layerGroup().addTo(map);
let watchId = null;
let lastRunPos = null;
let lastRunTime = 0;
const LIVE_REPLAN_KM = 1;      // re-plan after moving this far ...
const LIVE_REPLAN_MS = 60000;  // ... or after this much time

function haversineKm([a1, o1], [a2, o2]) {
  const r = Math.PI / 180;
  const h = Math.sin((a2 - a1) * r / 2) ** 2 +
    Math.cos(a1 * r) * Math.cos(a2 * r) * Math.sin((o2 - o1) * r / 2) ** 2;
  return 12742 * Math.asin(Math.sqrt(h));
}

function nowHHMM() {
  return new Date().toTimeString().slice(0, 5);
}

function locateStatus(text, isError) {
  const el = $("locate-status");
  el.hidden = false;
  el.className = "locate-status" + (isError ? " error" : "");
  el.textContent = text;
}

function useLocation(pos) {
  const { latitude: lat, longitude: lon, accuracy } = pos.coords;
  $("src").value = "custom";
  $("custom-src").hidden = false;
  $("src-lat").value = lat.toFixed(6);
  $("src-lon").value = lon.toFixed(6);
  $("dep").value = nowHHMM();
  $("trip-date").value = todayISO();
  $("cur-km").value = 0;  // the route now starts where you are

  meLayer.clearLayers();
  L.circle([lat, lon], { radius: accuracy, color: "#1557d6", weight: 1, fillOpacity: 0.08 }).addTo(meLayer);
  L.circleMarker([lat, lon], { radius: 9, color: "#fff", weight: 3, fillColor: "#1557d6", fillOpacity: 1 })
    .bindTooltip("You are here").addTo(meLayer);

  locateStatus(`You are at ${lat.toFixed(5)}, ${lon.toFixed(5)} (accuracy ${Math.round(accuracy)} m), leaving now ${$("dep").value}` +
    ($("live").checked ? " - live tracking on" : ""));
  lastRunPos = [lat, lon];
  lastRunTime = Date.now();
  run();
}

function geoError(e) {
  locateStatus(`Could not get your location (${e.message}). Allow location access for this site in the browser.`, true);
}

// While tracking, a slow or unchanged GPS fix can report TIMEOUT (3) or
// POSITION_UNAVAILABLE (2). Those are temporary: keep the last plan and keep
// watching. Only a denied permission (1) is shown as an error.
function liveError(e) {
  if (e.code === 1) { stopLive(); geoError(e); }
}

function stopLive() {
  if (watchId !== null) navigator.geolocation.clearWatch(watchId);
  watchId = null;
  $("live").checked = false;
}

$("locate-btn").onclick = () => {
  if (!navigator.geolocation) return locateStatus("This browser cannot share its location.", true);
  locateStatus("Detecting your location...");
  navigator.geolocation.getCurrentPosition(useLocation, geoError, { enableHighAccuracy: true, timeout: 15000 });
};

$("live").onchange = () => {
  if (!$("live").checked) { stopLive(); locateStatus("Live tracking off."); return; }
  if (!navigator.geolocation) { stopLive(); return locateStatus("This browser cannot share its location.", true); }
  locateStatus("Live tracking on: re-planning every 1 km or every minute...");
  lastRunPos = null;
  watchId = navigator.geolocation.watchPosition((pos) => {
    const here = [pos.coords.latitude, pos.coords.longitude];
    const moved = lastRunPos ? haversineKm(lastRunPos, here) : Infinity;
    if (moved >= LIVE_REPLAN_KM || Date.now() - lastRunTime >= LIVE_REPLAN_MS) useLocation(pos);
  }, liveError, { enableHighAccuracy: true, maximumAge: 10000 });
};

document.querySelectorAll(".tabs button").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll(".tabs button, .tab").forEach((x) => x.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
  };
});

// "Now" fills today's date and the current time
$("now-btn").onclick = () => {
  $("trip-date").value = todayISO();
  $("dep").value = nowHHMM();
};

// Picking a preset start point ends live tracking and removes the "you are here" dot
$("src").addEventListener("change", () => {
  if ($("src").value !== "custom") { stopLive(); meLayer.clearLayers(); $("locate-status").hidden = true; }
});

$("trip-form").addEventListener("submit", run);

function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

(async function init() {
  $("trip-date").value = todayISO();
  $("dep").value = nowHHMM();  // a real user usually leaves now
  await loadPresets();
  try {
    const q = await (await fetch("/data-quality")).json();
    $("dq").innerHTML = `Data: OpenStreetMap &middot; ${q.total_restaurants} places &middot;
      hours verified ${q.hours_verified} &middot; veg info ${q.veg_info_known} &middot; lighting ${q.lighting_verified}`;
  } catch (e) { /* data quality is optional */ }
})();
