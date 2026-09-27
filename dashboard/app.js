/* Диспетчерский дашборд — ядро: карта, живой поток снимков, поиск/отслеживание ТС, управление потоком,
   карточки инцидентов, детали ТС (ETA). Модули timeline.js / whatif.js / routes.js подключаются через window.MT. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const LEVEL_TXT = { red: "Высокий риск", yellow: "Внимание", green: "В графике", gray: "Нет данных" };
  const LEVEL_ORDER = { red: 3, yellow: 2, green: 1, gray: 0 };

  // ------------------------------------------------------------ общие утилиты
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  function fmtDelay(s) {
    if (s === null || s === undefined) return "—";
    const sign = s > 0 ? "+" : s < 0 ? "−" : "";
    const a = Math.abs(Math.round(s));
    const m = Math.floor(a / 60), sec = a % 60;
    return m ? `${sign}${m} мин ${String(sec).padStart(2, "0")} с` : `${sign}${sec} с`;
  }
  const delayCls = (s) => (s === null || s === undefined ? "" : s >= 240 ? "d-late" : s >= 120 || s <= -60 ? "d-warn" : "d-ok");
  const pct = (p) => (p === null || p === undefined ? "—" : `${Math.round(p * 100)}%`);
  const lvlIcon = (l) => `<i class="lvl lvl-${l}" aria-hidden="true"></i>`;

  // SHAP: вклад паттернов поведения ТС в прогноз ML, с (+ — к опозданию, − — к опережению)
  const shapCls = (s) => (s > 0 ? "up" : "down");
  function shapTags(e) {
    if (!e) return "";
    return e.patterns.filter((p) => Math.abs(p.seconds) >= 5).slice(0, 3)
      .map((p) => `<span class="tag shap ${shapCls(p.seconds)}">${esc(p.title)} ${fmtDelay(p.seconds)}</span>`).join(" ");
  }
  function shapBlock(e, predicted) {
    if (!e) return "";
    const shown = e.patterns.filter((p) => Math.abs(p.seconds) >= 1);
    const max = Math.max(1, ...shown.map((p) => Math.abs(p.seconds)));
    const rows = shown.map((p) => `<div class="shap-row"><span>${esc(p.title)}</span>
      <span class="shap-bar"><i class="${shapCls(p.seconds)}" style="width:${Math.round(Math.abs(p.seconds) / max * 100)}%"></i></span>
      <span class="num">${fmtDelay(p.seconds)}</span></div>`).join("");
    return `<div><h5>Почему такой прогноз (SHAP)</h5>
      <div class="delay-sub">Сейчас ${fmtDelay(e.cur_dev_s)} · средняя поправка модели ${fmtDelay(e.base_s)} · вклады паттернов ниже → прогноз ${fmtDelay(predicted)}</div>
      ${rows}</div>`;
  }
  const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  function store(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage недоступен */ } }
  function load(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  async function api(path, opts = {}) {
    const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    const txt = await r.text();
    let body = null;
    try { body = txt ? JSON.parse(txt) : null; } catch (e) { body = txt; }
    if (!r.ok) throw new Error((body && body.detail) || `HTTP ${r.status}`);
    return body;
  }
  const vname = (v) => (v.scheduled ? `ТС ${v.tr_id}` : `Терминал ${v.unit_id}`) + (v.route_ref ? ` · ${v.route_ref}` : v.route_id ? ` · ${v.route_id}` : "");

  const MT = window.MT = {
    esc, fmtDelay, pct, lvlIcon, cssVar, api, delayCls, vname, LEVEL_TXT,
    snap: null, region: null, map: null, layers: {}, tracked: null, scrubbing: false,
    handlers: {}, on(ev, fn) { (this.handlers[ev] = this.handlers[ev] || []).push(fn); },
    emit(ev, x) { (this.handlers[ev] || []).forEach((f) => { try { f(x); } catch (e) { console.error(e); } }); },
  };

  let tab = "open";
  let levelFilter = null;
  let drawerId = null;
  let drawerTimer = null;
  const markers = new Map();
  const ghosts = new Map();
  const segLayers = new Map();
  let network = null;

  // ------------------------------------------------------------ карта (регион + провайдеры)
  function providerOf() {
    const r = MT.region;
    const p = load("provider");
    return p && r.provider_defs[p] ? p : r.default_provider;
  }

  function buildMap() {
    const r = MT.region;
    const prov = r.provider_defs[providerOf()];
    const center = MT.map ? MT.map.getCenter() : L.latLng(r.center);
    const zoom = MT.map ? MT.map.getZoom() : r.zoom;
    if (MT.map) { MT.map.remove(); markers.clear(); ghosts.clear(); segLayers.clear(); }
    const bounds = L.latLngBounds(r.bounds);
    const map = MT.map = L.map("map", {
      crs: prov.crs === "EPSG3395" ? L.CRS.EPSG3395 : L.CRS.EPSG3857,
      center, zoom, minZoom: r.min_zoom, maxZoom: Math.min(r.max_zoom, prov.max_zoom),
      maxBounds: bounds.pad(0.05), maxBoundsViscosity: 1.0, worldCopyJump: false,
    });
    // без флагов и сторонней символики в атрибуции
    map.attributionControl.setPrefix('<a href="https://leafletjs.com" target="_blank" rel="noopener">Leaflet</a>');
    L.tileLayer(prov.url, { subdomains: prov.subdomains || "abc", maxZoom: prov.max_zoom, attribution: prov.attribution,
      bounds: bounds.pad(0.1), keepBuffer: 2 }).addTo(map);
    MT.layers = {
      net: L.layerGroup(), real: L.layerGroup(), stops: L.layerGroup(), overlay: L.layerGroup().addTo(map),
      path: L.layerGroup().addTo(map), ghost: L.layerGroup().addTo(map), veh: L.layerGroup().addTo(map),
    };
    if ($("lyr-net").checked) MT.layers.net.addTo(map);
    if ($("lyr-real").checked) MT.layers.real.addTo(map);
    map.on("zoomend", () => {
      if (map.getZoom() >= 14) MT.layers.stops.addTo(map); else map.removeLayer(MT.layers.stops);
    });
    map.on("dragstart", () => { if (MT.tracked !== null) setFollow(false); });
    if (network) drawNetwork();
    MT.emit("map", map);
  }

  function initProviders() {
    const sel = $("provider");
    sel.innerHTML = Object.entries(MT.region.provider_defs).map(([k, p]) => `<option value="${k}">${esc(p.name)}</option>`).join("");
    sel.value = providerOf();
    sel.onchange = () => { store("provider", sel.value); buildMap(); if (MT.snap) renderMap(); };
  }

  function applyTheme(t) {
    document.documentElement.dataset.theme = t;
    store("theme", t);
    recolorSegments();
    MT.emit("theme", t);
  }
  $("theme-btn").onclick = () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  document.documentElement.dataset.theme = load("theme") || "dark";

  // ------------------------------------------------------------ маршрутная сеть
  async function loadNetwork() {
    try {
      network = await api("/api/network");
      drawNetwork();
    } catch (e) { setTimeout(loadNetwork, 3000); }
  }
  MT.reloadNetwork = loadNetwork;

  function drawNetwork() {
    const L_ = MT.layers;
    L_.net.clearLayers(); L_.real.clearLayers(); L_.stops.clearLayers(); segLayers.clear();
    network.segments.forEach((s) => {
      const casing = L.polyline(s.coords, { weight: 6, opacity: 0.9, interactive: false, lineCap: "round" });
      const pl = L.polyline(s.coords, { weight: 3, opacity: 0.9, interactive: false, lineCap: "round" });
      casing.addTo(L_.net); pl.addTo(L_.net);
      segLayers.set(s.id, { pl, casing });
    });
    network.stops.forEach((s) => {
      L.circleMarker([s.lat, s.lon], { radius: 3, weight: 1.5, fillOpacity: 1 })
        .bindTooltip(esc(s.address) || "Остановка", { direction: "top" }).addTo(L_.stops);
    });
    (network.real_routes || []).forEach((r) => {
      if (!r.geometry || r.geometry.length < 2) return;
      L.polyline(r.geometry, { color: r.color || cssVar("--route"), weight: 4, opacity: 0.55, dashArray: "1 7", lineCap: "round" })
        .bindTooltip(`${esc(r.ref)} · ${esc(r.name || "")}`, { sticky: true }).addTo(L_.real);
      const mid = r.geometry[Math.floor(r.geometry.length / 2)];
      L.marker(mid, { interactive: false, icon: L.divIcon({ className: "", html: `<span class="route-label">${esc(r.ref)}</span>`, iconSize: null }) }).addTo(L_.real);
    });
    recolorSegments();
  }
  $("lyr-net").onchange = (e) => { e.target.checked ? MT.layers.net.addTo(MT.map) : MT.map.removeLayer(MT.layers.net); };
  $("lyr-real").onchange = (e) => { e.target.checked ? MT.layers.real.addTo(MT.map) : MT.map.removeLayer(MT.layers.real); };

  function recolorSegments() {
    if (!segLayers.size) return;
    const base = cssVar("--route"), casing = cssVar("--route-casing");
    const levels = (MT.snap && MT.snap.segments) || {};
    const colors = { red: cssVar("--critical"), yellow: cssVar("--warning") };
    segLayers.forEach(({ pl, casing: cs }, id) => {
      const lvl = levels[id];
      if (lvl) { pl.setStyle({ color: colors[lvl], weight: lvl === "red" ? 6 : 5, opacity: 1 }); cs.setStyle({ color: casing, weight: lvl === "red" ? 10 : 9, opacity: 0.9 }); pl.bringToFront(); }
      else { pl.setStyle({ color: base, weight: 3, opacity: 0.85 }); cs.setStyle({ color: casing, weight: 6, opacity: 0.6 }); }
    });
    MT.layers.stops.eachLayer((m) => m.setStyle({ color: base, fillColor: cssVar("--surface") }));
  }

  // ------------------------------------------------------------ маркеры ТС
  function vehIcon(v) {
    const tracked = MT.tracked === v.unit_id ? " tracked" : "";
    const cls = `${v.level}${v.stale ? " stale" : ""}${tracked}`;
    const arrow = v.state === "moving" && !v.stale ? `<span class="veh-arrow" style="transform: translate(-50%,-100%) rotate(${v.heading}deg)"></span>` : "";
    const pulse = v.level === "red" && !v.stale ? '<span class="veh-pulse"></span>' : "";
    const label = v.level === "red" || v.level === "yellow" || tracked
      ? `<span class="veh-label">${v.route_ref ? esc(v.route_ref) + " · " : ""}${fmtDelay(v.predicted_delay_s)}</span>` : "";
    return L.divIcon({ className: "veh-marker", iconSize: [22, 22], iconAnchor: [11, 11], html: `${pulse}<div class="veh-dot ${cls}">${arrow}</div>${label}` });
  }

  function vehTooltip(v) {
    const lines = [`<b>${esc(vname(v))}</b>`, `${lvlIcon(v.level)} ${LEVEL_TXT[v.level]}`];
    if (v.predicted_delay_s !== null) lines.push(`Прогноз (10–15 мин): <b>${fmtDelay(v.predicted_delay_s)}</b> · P(опозд.) ${pct(v.p_late)}`);
    if (v.cause) lines.push(`Причина: ${esc(v.cause)}`);
    if (v.next_stops && v.next_stops.length) {
      lines.push("Ближайшие остановки:");
      v.next_stops.forEach((s) => lines.push(`&nbsp;· ${esc(s.address)} — ${s.eta} (${fmtDelay(s.delay_s)})`));
    }
    lines.push(`Скорость ${v.speed ?? "—"} км/ч · данные ${v.last_seen ?? "—"}${v.stale ? " (устарели)" : ""}`);
    return lines.join("<br>");
  }

  function renderMap() {
    if (MT.scrubbing) return;
    const seen = new Set();
    MT.snap.vehicles.forEach((v) => {
      if (v.lat === null || v.lon === null) return;
      const hidden = levelFilter && !(levelFilter === "stale" ? v.stale || v.level === "gray" : v.level === levelFilter);
      const key = v.unit_id;
      seen.add(key);
      let m = markers.get(key);
      if (!m) {
        m = L.marker([v.lat, v.lon], { icon: vehIcon(v) }).addTo(MT.layers.veh);
        m.bindTooltip("", { direction: "top", offset: [0, -10] });
        m.on("click", () => openDrawer(key));
        markers.set(key, m);
      }
      m.setLatLng([v.lat, v.lon]);
      const sig = `${v.level}|${v.stale}|${v.state}|${Math.round(v.heading / 10)}|${Math.round((v.predicted_delay_s || 0) / 5)}|${MT.tracked === key}|${v.route_ref}`;
      if (m._sig !== sig) { m.setIcon(vehIcon(v)); m._sig = sig; }
      m.setTooltipContent(vehTooltip(v));
      m.setZIndexOffset(LEVEL_ORDER[v.level] * 1000 + (MT.tracked === key ? 10000 : 0));
      m.setOpacity(hidden ? 0.15 : 1);
      // при обрыве связи — расчётное положение (счисление пути по маршруту)
      let g = ghosts.get(key);
      if (v.est_lat !== null && v.est_lat !== undefined) {
        if (!g) {
          g = { m: L.marker([v.est_lat, v.est_lon], { icon: L.divIcon({ className: "", html: '<div class="veh-ghost"></div>', iconSize: [18, 18], iconAnchor: [9, 9] }) }).addTo(MT.layers.ghost),
                l: L.polyline([[v.lat, v.lon], [v.est_lat, v.est_lon]], { dashArray: "3 6", weight: 2, color: cssVar("--ink-2") }).addTo(MT.layers.ghost) };
          g.m.bindTooltip("", { direction: "top" });
          ghosts.set(key, g);
        }
        g.m.setLatLng([v.est_lat, v.est_lon]);
        g.l.setLatLngs([[v.lat, v.lon], [v.est_lat, v.est_lon]]);
        g.m.setTooltipContent(`<b>${esc(vname(v))}</b><br>Расчётное положение (нет связи ${Math.round((v.age_s || 0) / 60)} мин)<br>по последней точке, темпу и графику`);
      } else if (g) { MT.layers.ghost.removeLayer(g.m); MT.layers.ghost.removeLayer(g.l); ghosts.delete(key); }
    });
    markers.forEach((m, k) => { if (!seen.has(k)) { MT.layers.veh.removeLayer(m); markers.delete(k); } });
    recolorSegments();
    followTracked();
  }
  MT.markers = markers;
  MT.renderLive = () => { markers.forEach((m) => { m._sig = null; }); if (MT.snap) renderMap(); };

  // кадр таймлайна (история / прогноз) вместо живой картины
  MT.renderFrame = (frame) => {
    MT.layers.ghost.clearLayers(); ghosts.clear();
    const seen = new Set();
    frame.v.forEach(([unit, lat, lon, level, delay, stale]) => {
      seen.add(unit);
      let m = markers.get(unit);
      const v = (MT.snap.vehicles.find((x) => x.unit_id === unit)) || { unit_id: unit, tr_id: unit, scheduled: true };
      const fv = { ...v, level, predicted_delay_s: delay, stale: !!stale, state: "moving", heading: 0 };
      if (!m) { m = L.marker([lat, lon]).addTo(MT.layers.veh); m.bindTooltip(""); markers.set(unit, m); m.on("click", () => openDrawer(unit)); }
      m.setLatLng([lat, lon]);
      m.setIcon(vehIcon(fv)); m._sig = null;
      m.setTooltipContent(`<b>${esc(vname(fv))}</b><br>${frame.future ? "Прогноз" : "История"} ${frame.t}: ${fmtDelay(delay)}`);
      m.setOpacity(1);
    });
    markers.forEach((m, k) => m.setOpacity(seen.has(k) ? 1 : 0.1));
  };

  // ------------------------------------------------------------ отслеживание ТС
  let follow = true;
  function setFollow(on) { follow = on; renderTrackPill(); }
  function renderTrackPill() {
    const pill = $("track-pill");
    if (MT.tracked === null) { pill.classList.add("hidden"); return; }
    const v = MT.snap && MT.snap.vehicles.find((x) => x.unit_id === MT.tracked);
    pill.classList.remove("hidden");
    pill.innerHTML = `◎ ${esc(v ? vname(v) : MT.tracked)} ${v ? `· ${fmtDelay(v.predicted_delay_s)}` : ""}
      <button id="track-follow">${follow ? "следую" : "следить"}</button><button id="track-stop">✕</button>`;
    $("track-follow").onclick = () => { setFollow(!follow); followTracked(true); };
    $("track-stop").onclick = () => MT.track(null);
  }
  function followTracked(force) {
    if (MT.tracked === null || !follow || MT.scrubbing) return;
    const v = MT.snap && MT.snap.vehicles.find((x) => x.unit_id === MT.tracked);
    if (!v || v.lat === null) return;
    const ll = v.est_lat ? [v.est_lat, v.est_lon] : [v.lat, v.lon];
    if (force || !MT.map.getBounds().pad(-0.25).contains(ll)) MT.map.panTo(ll, { animate: true, duration: 0.5 });
  }
  MT.track = (unit) => {
    MT.tracked = unit;
    follow = true;
    try { history.replaceState(null, "", unit === null ? location.pathname : `#track=${unit}`); } catch (e) { /* */ }
    markers.forEach((m) => { m._sig = null; });
    if (unit !== null) {
      const v = MT.snap && MT.snap.vehicles.find((x) => x.unit_id === unit);
      if (v && v.lat !== null) MT.map.setView(v.est_lat ? [v.est_lat, v.est_lon] : [v.lat, v.lon], Math.max(MT.map.getZoom(), 14));
      openDrawer(unit);
    }
    renderTrackPill();
    if (MT.snap) renderMap();
  };

  // ------------------------------------------------------------ поиск
  let searchTimer = null, searchIdx = -1, searchItems = [];
  $("search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 150); });
  $("search").addEventListener("keydown", (e) => {
    const list = $("search-list");
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      searchIdx = Math.max(0, Math.min(searchItems.length - 1, searchIdx + (e.key === "ArrowDown" ? 1 : -1)));
      [...list.children].forEach((c, i) => c.classList.toggle("active", i === searchIdx));
      e.preventDefault();
    } else if (e.key === "Enter") {
      pickSearch(searchItems[Math.max(0, searchIdx)]);
    } else if (e.key === "Escape") { list.classList.add("hidden"); }
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".search")) $("search-list").classList.add("hidden"); });
  async function runSearch() {
    const q = $("search").value.trim();
    const list = $("search-list");
    if (!q) { list.classList.add("hidden"); return; }
    const ql = q.toLowerCase();
    const local = (MT.snap ? MT.snap.vehicles : []).filter((v) =>
      [String(v.tr_id || ""), String(v.unit_id), (v.route_ref || "").toLowerCase(), (v.route_id || "").toLowerCase()]
        .some((k) => k && (k === ql || k.startsWith(ql)))).slice(0, 12);
    let routes = [];
    try { routes = (await api(`/api/search?q=${encodeURIComponent(q)}`)).routes; } catch (e) { /* */ }
    searchItems = [...local.map((v) => ({ kind: "v", v })), ...routes.map((r) => ({ kind: "r", r }))];
    searchIdx = searchItems.length ? 0 : -1;
    list.innerHTML = searchItems.length ? searchItems.map((it, i) => it.kind === "v"
      ? `<div class="search-item${i === 0 ? " active" : ""}" data-i="${i}">${lvlIcon(it.v.level)}<b>${esc(vname(it.v))}</b><span class="s">${it.v.predicted_delay_s !== null ? fmtDelay(it.v.predicted_delay_s) : ""}</span></div>`
      : `<div class="search-item${i === 0 ? " active" : ""}" data-i="${i}"><span class="tag route">${esc(it.r.ref)}</span>${esc(it.r.name || "")}<span class="s">${it.r.vehicles.length} ТС</span></div>`).join("")
      : '<div class="search-item">Ничего не найдено</div>';
    list.classList.remove("hidden");
    [...list.querySelectorAll("[data-i]")].forEach((el) => { el.onclick = () => pickSearch(searchItems[+el.dataset.i]); });
  }
  function pickSearch(it) {
    if (!it) return;
    $("search-list").classList.add("hidden");
    if (it.kind === "v") MT.track(it.v.unit_id);
    else MT.emit("route-focus", it.r.id);
  }

  // ------------------------------------------------------------ управление потоком (фидер)
  let feederStatus = null;
  async function refreshFeeder() {
    try { feederStatus = await api("/feeder/status"); } catch (e) { feederStatus = null; }
    [...$("speed").querySelectorAll("button")].forEach((b) => {
      const s = +b.dataset.speed;
      b.classList.toggle("active", !!feederStatus && (s === 0 ? !feederStatus.running : feederStatus.running && Math.abs(feederStatus.speed - s) < 0.01));
      b.disabled = !feederStatus;
    });
    const ob = $("outage-btn");
    ob.disabled = !feederStatus;
    const down = feederStatus && feederStatus.outage_remaining_s > 0;
    ob.classList.toggle("on", !!down);
    ob.textContent = down ? "Восстановить поток" : "Обрыв потока";
    MT.feeder = feederStatus;
  }
  $("speed").addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const s = +b.dataset.speed;
    try {
      if (s === 0) await api("/feeder/pause", { method: "POST" });
      else { await api(`/feeder/speed/${s}`, { method: "POST" }); if (feederStatus && !feederStatus.running) await api("/feeder/resume", { method: "POST" }); }
    } catch (err) { /* фидер недоступен — реальные терминалы */ }
    refreshFeeder();
  });
  $("outage-btn").onclick = async () => {
    const down = feederStatus && feederStatus.outage_remaining_s > 0;
    try { await api(`/feeder/outage/${down ? "stop" : "start"}`, { method: "POST" }); } catch (e) { /* */ }
    refreshFeeder();
  };
  setInterval(refreshFeeder, 3000);

  // ------------------------------------------------------------ шапка / KPI
  function renderHeader() {
    const s = MT.snap.status;
    $("clock").textContent = MT.snap.data_time_local.split(" ")[1];
    $("clock-date").textContent = `${MT.snap.data_time_local.split(" ")[0]} МСК · поток ×${s.clock_rate}`;
    const mode = $("mode");
    mode.className = `mode ${s.mode}`;
    mode.querySelector(".mode-text").textContent = s.mode === "online" ? "Онлайн" : s.mode === "waiting" ? "Ожидание потока" : "Деградация";
    const banner = $("banner");
    const live = (MT.snap.live_scenarios || []).map((x) => `${x.title} (до ${x.until})`);
    const reasons = [...(s.mode !== "online" ? s.reasons : []), ...(live.length ? [`Применены сценарии: ${live.join("; ")}`] : [])];
    if (reasons.length) {
      banner.textContent = reasons.join(" · ") + (s.link === "lost" ? ` (нет пакетов ${Math.round(s.last_packet_age_s)} с; ТС показаны по расчётному положению)` : "");
      banner.classList.remove("hidden");
    } else banner.classList.add("hidden");
    $("chip-link").className = `chip ${s.link === "online" ? "ok" : s.link === "lost" ? "bad" : ""}`;
    $("chip-ml").className = `chip ${s.ml_available ? "ok" : "bad"}`;
    $("chip-ml").title = s.ml_available ? "ML-сервис доступен" : `ML недоступен: ${s.ml_last_error || ""}`;
    const k = MT.snap.kpi;
    $("k-red").textContent = k.red; $("k-yellow").textContent = k.yellow; $("k-green").textContent = k.green; $("k-gray").textContent = k.gray;
    $("k-gray-sub").textContent = `${k.stale} без связи · ${k.vehicles_total - k.scheduled} без наряда · ${k.no_target} вне рейса`;
    document.querySelector(".kpi-red").classList.toggle("hot", k.red > 0);
    const acc = MT.snap.perf.accuracy;
    $("k-mae").textContent = acc.n ? `${acc.mae_model_s} с` : "—";
    $("k-mae-sub").textContent = acc.n ? `baseline ${acc.mae_baseline_s} с · n=${acc.n}` : "накапливается по факту прибытий";
    $("k-lat").textContent = MT.snap.perf.e2e_p95_ms !== null ? `${Math.round(MT.snap.perf.e2e_p95_ms)} мс` : "—";
    $("k-lat-sub").textContent = `пакет→прогноз p95 · ML p95 ${MT.snap.perf.ml_p95_ms !== null ? Math.round(MT.snap.perf.ml_p95_ms) + " мс" : "—"}`;
    document.querySelectorAll(".kpi[data-filter]").forEach((b) => b.classList.toggle("active", b.dataset.filter === levelFilter));
    $("map-filter").innerHTML = levelFilter ? `<span class="chip" id="clear-filter">Фильтр: ${esc(LEVEL_TXT[levelFilter] || "нет связи")} ✕</span>` : "";
    renderTrackPill();
  }
  document.querySelectorAll(".kpi[data-filter]").forEach((b) => b.addEventListener("click", () => {
    levelFilter = levelFilter === b.dataset.filter ? null : b.dataset.filter;
    if (MT.snap) { renderHeader(); renderMap(); renderSide(); }
  }));
  $("map-filter").addEventListener("click", () => { levelFilter = null; if (MT.snap) { renderHeader(); renderMap(); renderSide(); } });

  // ------------------------------------------------------------ боковая панель
  function incidentCard(i, resolved = false) {
    const c = i.causes[0] || {};
    const more = i.causes.slice(1).map((x) => `<span class="tag">${esc(x.title)}</span>`).join(" ");
    const v = MT.snap.vehicles.find((x) => x.unit_id === i.unit_id) || {};
    const route = v.route_ref || i.route_id || "";
    return `<article class="card ${resolved ? "green" : i.level}${i.acknowledged ? " acked" : ""}">
      <div class="card-top">
        <span class="badge ${resolved ? "green" : i.level}">${lvlIcon(resolved ? "green" : i.level)}${resolved ? "Закрыт" : LEVEL_TXT[i.level]}</span>
        <span class="card-veh">ТС ${i.tr_id} ${route ? `<span class="tag route">${esc(route)}</span>` : ""}</span>
        ${i.source === "fallback" ? '<span class="tag">baseline</span>' : ""}
        ${i.recovering ? '<span class="tag">восстанавливается</span>' : ""}
        <span class="card-time">${resolved ? `${i.opened}–${i.closed}` : `с ${i.opened}`}</span>
      </div>
      <div class="card-main">
        <div><div class="delay">${fmtDelay(resolved ? i.peak_delay_s : i.predicted_delay_s)}</div>
          <div class="delay-sub">${resolved ? "макс. прогноз опоздания" : `прогноз опоздания · сейчас ${fmtDelay(i.cur_dev_s)}`}</div></div>
        ${resolved ? "" : `<div class="prob"><div class="prob-val">${pct(i.p_late)}</div><div class="delay-sub">P(опоздание &gt;2 мин)</div>
          <div class="prob-bar"><i style="width:${Math.round((i.p_late || 0) * 100)}%"></i></div></div>`}
      </div>
      ${resolved ? "" : `
      <div class="row"><span class="k">Остановка</span><span><b>${esc(i.target.address || "—")}</b> · план ${i.target.plan || "—"} → прогноз ${i.target.eta || "—"}</span></div>
      <div class="row"><span class="k">Участок</span><span>${esc(i.segment.from || "—")} → ${esc(i.segment.to || "—")}${i.segment.avg_speed_kmh !== null ? ` · ${i.segment.avg_speed_kmh} км/ч${i.segment.plan_speed_kmh ? ` (план ${i.segment.plan_speed_kmh})` : ""}` : ""}</span></div>
      <div class="cause"><b>${esc(c.title || "")}</b><span class="detail">${esc(c.detail || "")}</span></div>
      ${more ? `<div>${more}</div>` : ""}
      ${i.explanation ? `<div class="shap-tags"><span class="k">ML-факторы</span> ${shapTags(i.explanation)}</div>` : ""}
      <div class="reco">${esc(c.recommendation || "")}</div>
      <div class="card-actions">
        <button class="btn" data-act="track" data-unit="${i.unit_id}">Отслеживать</button>
        <button class="btn" data-act="whatif" data-unit="${i.unit_id}">What-if</button>
        ${i.acknowledged ? '<span class="tag">принят в работу</span>' : `<button class="btn primary" data-act="ack" data-id="${esc(i.id)}">Принять</button>`}
      </div>`}
    </article>`;
  }

  function vehicleRow(v) {
    const sub = v.scheduled ? (v.no_target ? "нет рейса в горизонте 10–15 мин" : `${esc(v.cause || "в графике")}${v.stale ? " · нет связи" : ""}`) : "без наряда (нет расписания)";
    return `<div class="vrow" data-act="track" data-unit="${v.unit_id}">${lvlIcon(v.level)}
      <div><div class="t">${esc(vname(v))}</div><div class="s">${sub}</div></div>
      <div class="v">${v.predicted_delay_s !== null ? fmtDelay(v.predicted_delay_s) : "—"}</div></div>`;
  }

  function renderSide() {
    $("inc-count").textContent = MT.snap.kpi.incidents_unacked;
    if (tab === "whatif" || tab === "routes") return;
    const body = $("side-body");
    const top = body.scrollTop;
    let html = "";
    if (tab === "open") {
      const list = MT.snap.incidents.filter((i) => !levelFilter || levelFilter === "stale" || i.level === levelFilter);
      html = list.length ? list.map((i) => incidentCard(i)).join("") : `<div class="empty">${lvlIcon("green")} Нет ТС с риском отклонения в горизонте 10–15 минут</div>`;
    } else if (tab === "vehicles") {
      const list = MT.snap.vehicles.filter((v) => !levelFilter || (levelFilter === "stale" ? v.stale || v.level === "gray" : v.level === levelFilter));
      html = list.map(vehicleRow).join("") || '<div class="empty">Нет ТС</div>';
    } else {
      html = MT.snap.resolved.length ? MT.snap.resolved.map((i) => incidentCard(i, true)).join("") : '<div class="empty">Закрытых инцидентов пока нет</div>';
    }
    body.innerHTML = html;
    body.scrollTop = top;
  }
  MT.setTab = (t) => {
    tab = t;
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === t));
    $("side-body").classList.toggle("hidden", t === "whatif" || t === "routes");
    $("whatif-body").classList.toggle("hidden", t !== "whatif");
    $("routes-body").classList.toggle("hidden", t !== "routes");
    MT.emit("tab", t);
    if (MT.snap) renderSide();
  };
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => MT.setTab(t.dataset.tab)));
  document.body.addEventListener("click", async (e) => {
    const el = e.target.closest("[data-act]");
    if (!el || !el.closest("#side-body, .drawer-body")) return;
    const unit = Number(el.dataset.unit);
    if (el.dataset.act === "ack") {
      el.disabled = true;
      await fetch(`/api/incidents/${encodeURIComponent(el.dataset.id)}/ack`, { method: "POST" }).catch(() => {});
    } else if (el.dataset.act === "track") {
      MT.track(unit);
    } else if (el.dataset.act === "whatif") {
      MT.setTab("whatif");
      MT.emit("whatif-prefill", { unit_id: unit, type: el.dataset.type });
    }
  });

  // ------------------------------------------------------------ нижняя панель
  function renderBottom() {
    $("routes").innerHTML = `<h4>Риск по маршрутам</h4><div class="route-chips">${MT.snap.routes.map((r) => {
      const v = MT.snap.vehicles.find((x) => x.route_id === r.id);
      const name = (v && v.route_ref) || r.id;
      return `<span class="rchip" data-route-veh="${v ? v.unit_id : ""}" title="${r.vehicles} ТС: ${r.red} красн., ${r.yellow} жёлт., ${r.green} зел.">${lvlIcon(r.level)}${esc(name)}${r.red + r.yellow ? ` <span class="tag">${r.red + r.yellow}</span>` : ""}</span>`;
    }).join("") || "—"}</div>`;
    $("routes").querySelectorAll("[data-route-veh]").forEach((el) => { el.onclick = () => el.dataset.routeVeh && MT.track(+el.dataset.routeVeh); });
    $("events").innerHTML = `<h4>Журнал событий</h4>${MT.snap.events.map((e) =>
      `<div class="ev ${e.level}"><span class="tm">${e.time}</span><span class="tx">${esc(e.text)}</span></div>`).join("") || '<div class="ev"><span></span><span class="tx">—</span></div>'}`;
    const p = MT.snap.perf, s = MT.snap.status;
    const f = (v, u) => (v === null || v === undefined ? "—" : `${Math.round(v * 10) / 10} ${u}`);
    $("perf").innerHTML = `<h4>Производительность</h4><div class="perf-grid">
      <span>Пакетов NDTP / с</span><span>${p.packets_per_s}</span>
      <span>ML-инференс p95</span><span>${f(p.ml_p95_ms, "мс")}</span>
      <span>Пакет → прогноз p95</span><span>${f(p.e2e_p95_ms, "мс")}</span>
      <span>Цикл прогноза p95</span><span>${f(p.cycle_p95_ms, "мс")}</span>
      <span>Последний пакет</span><span>${s.last_packet_age_s === null ? "—" : f(s.last_packet_age_s, "с назад")}</span></div>`;
  }

  // ------------------------------------------------------------ детали ТС
  async function openDrawer(unit) {
    drawerId = unit;
    $("drawer").classList.remove("hidden");
    $("drawer-body").innerHTML = '<div class="empty">Загрузка…</div>';
    clearInterval(drawerTimer);
    await refreshDrawer();
    drawerTimer = setInterval(refreshDrawer, 3000);
  }
  MT.openDrawer = openDrawer;
  $("drawer-close").onclick = () => { drawerId = null; clearInterval(drawerTimer); $("drawer").classList.add("hidden"); MT.layers.path.clearLayers(); };
  $("drawer-track").onclick = () => { if (drawerId !== null) MT.track(MT.tracked === drawerId ? null : drawerId); };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("drawer-close").click(); });

  async function refreshDrawer() {
    if (drawerId === null) return;
    let d;
    try { d = await api(`/api/vehicles/${drawerId}`); } catch (e) { $("drawer-body").innerHTML = '<div class="empty">Нет данных по ТС</div>'; return; }
    const p = d.prediction || {};
    $("drawer-title").innerHTML = `${lvlIcon(d.level)} ${esc(vname(d))}`;
    $("drawer-track").textContent = MT.tracked === d.unit_id ? "Не отслеживать" : "Отслеживать";
    const seg = p.segment || {};
    const causes = (p.causes || []).map((c) => `<div class="cause"><b>${esc(c.title)}</b><span class="detail">${esc(c.detail)}</span><div class="reco">${esc(c.recommendation)}</div></div>`).join("");
    const feats = Object.entries(d.features || {}).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${v === null ? "—" : typeof v === "number" ? Math.round(v * 100) / 100 : esc(v)}</td></tr>`).join("");
    const eta = d.eta;
    const etaRows = eta && eta.stops.length ? eta.stops.map((s) => `<tr class="${s.skipped ? "skipped" : ""}"><td>${esc(s.address)}</td><td>${s.plan}</td><td><b>${s.eta}</b> <span class="delay-sub">±${Math.round(s.sigma_s / 6) / 10} мин</span></td><td class="num ${delayCls(s.delay_s)}">${fmtDelay(s.delay_s)}</td></tr>`).join("") : "";
    const top = $("drawer-body").scrollTop;
    $("drawer-body").innerHTML = `
      ${eta && eta.mode === "dead_reckoning" ? `<div class="banner" style="border-radius:8px">Нет связи с ТС ${Math.round((d.age_s || 0) / 60)} мин — положение и ETA рассчитаны по последней точке, темпу (×${eta.pace}) и графику</div>` : ""}
      <div class="kv">
        <div><div class="k">Прогноз ML (10–15 мин)</div><div class="v">${fmtDelay(d.predicted_delay_s)}</div></div>
        <div><div class="k">P(опоздание &gt;2 мин)</div><div class="v">${pct(d.p_late)}</div></div>
        <div><div class="k">Текущее отклонение</div><div class="v">${fmtDelay(d.cur_dev_s)}</div></div>
        <div><div class="k">Ср. скорость на сегменте</div><div class="v">${seg.avg_speed_kmh != null ? Math.round(seg.avg_speed_kmh) + " км/ч" : "—"}</div></div>
        <div><div class="k">Плановая скорость</div><div class="v">${seg.plan_speed_kmh != null ? Math.round(seg.plan_speed_kmh) + " км/ч" : "—"}</div></div>
        <div><div class="k">Время простоя</div><div class="v">${p.dwell_s != null ? Math.round(p.dwell_s) + " с" : "—"}</div></div>
        <div><div class="k">Возраст телеметрии</div><div class="v">${p.telemetry_age_s != null ? Math.round(p.telemetry_age_s) + " с" : "—"}</div></div>
        <div><div class="k">Темп к графику</div><div class="v">${eta ? "×" + eta.pace : "—"}</div></div>
        <div><div class="k">Источник прогноза</div><div class="v">${p.source === "model" ? "ML-модель" : p.source === "fallback" ? "baseline" : "—"}</div></div>
      </div>
      ${etaRows ? `<div><h5>Прибытие на ближайшие остановки (ETA)</h5><table class="tbl"><tr><th>Остановка</th><th>План</th><th>Прогноз</th><th class="num">Откл.</th></tr>${etaRows}</table></div>` : ""}
      ${causes ? `<div><h5>Причины и рекомендации</h5>${causes}</div>` : ""}
      ${shapBlock(p.explanation, d.predicted_delay_s)}
      ${d.scheduled ? `<div><h5>Моделирование (what-if)</h5><div class="quick">
        <button class="btn" data-act="whatif" data-type="breakdown" data-unit="${d.unit_id}">Поломка</button>
        <button class="btn" data-act="whatif" data-type="skip_stops" data-unit="${d.unit_id}">Пропуск остановок</button>
        <button class="btn" data-act="whatif" data-type="reserve_bus" data-unit="${d.unit_id}">Выпуск резерва</button>
        <button class="btn" data-act="whatif" data-type="detour" data-unit="${d.unit_id}">Объезд</button>
        <button class="btn" data-act="whatif" data-type="signal_priority" data-unit="${d.unit_id}">Светофорный приоритет</button>
        <button class="btn" data-act="whatif" data-type="domino" data-unit="${d.unit_id}">Эффект домино</button></div></div>` : ""}
      <div class="chart"><h5>Отклонение и прогноз, с</h5>${trailChart(d.trail)}</div>
      <div><h5>Факт прибытия (детектор по GPS)</h5>${arrivalsTable(d.arrivals)}</div>
      ${feats ? `<details><summary>Признаки модели (${Object.keys(d.features).length})</summary><table class="tbl">${feats}</table></details>` : ""}
      <div class="delay-sub">Пакетов: ${d.stats.packets} · прибытий обнаружено: ${d.stats.arrivals_detected}, пропущено детектором: ${d.stats.arrivals_skipped}</div>`;
    $("drawer-body").scrollTop = top;
    bindChartHover();
    MT.layers.path.clearLayers();
    if (d.upcoming && d.upcoming.length > 1) {
      L.polyline(d.upcoming, { color: cssVar("--route-casing"), weight: 9, opacity: 0.8 }).addTo(MT.layers.path);
      L.polyline(d.upcoming, { color: cssVar("--series-1"), weight: 5, dashArray: "8 6", opacity: 1 }).addTo(MT.layers.path);
    }
  }

  function arrivalsTable(rows) {
    if (!rows || !rows.length) return '<div class="delay-sub">Пока нет</div>';
    return `<table class="tbl"><tr><th>Остановка</th><th>План</th><th>Факт</th><th class="num">Откл.</th></tr>${rows.slice().reverse().map((a) =>
      `<tr><td>${esc(a.stop)}</td><td>${a.plan}</td><td>${a.fact}</td><td class="num ${delayCls(a.delay_s)}">${fmtDelay(a.delay_s)}</td></tr>`).join("")}</table>`;
  }

  // Линейный график: 2 серии (прогноз / текущее отклонение) + порог опоздания, общая ось Y.
  let chartData = null;
  function trailChart(trail) {
    if (!trail || trail.length < 2) { chartData = null; return '<div class="delay-sub">Накапливается история прогнозов…</div>'; }
    const W = 500, H = 190, L0 = 44, R0 = 8, T0 = 10, B0 = 22;
    const ys = trail.flatMap((t) => [t.cur_dev_s, t.predicted_delay_s]).concat([0, 120]);
    let lo = Math.min(...ys), hi = Math.max(...ys);
    const pad = (hi - lo) * 0.1 || 30; lo -= pad; hi += pad;
    const x = (i) => L0 + (i / (trail.length - 1)) * (W - L0 - R0);
    const y = (v) => T0 + (1 - (v - lo) / (hi - lo)) * (H - T0 - B0);
    const path = (key) => trail.map((t, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(t[key]).toFixed(1)}`).join("");
    const step = Math.max(30, Math.ceil((hi - lo) / 4 / 30) * 30);
    let grid = "";
    for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
      grid += `<line x1="${L0}" x2="${W - R0}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)" stroke-width="1"/>
        <text x="${L0 - 6}" y="${y(v) + 4}" text-anchor="end" font-size="10" fill="var(--muted)">${v}</text>`;
    }
    const xl = [0, Math.floor((trail.length - 1) / 2), trail.length - 1].map((i) =>
      `<text x="${x(i)}" y="${H - 6}" text-anchor="${i === 0 ? "start" : i === trail.length - 1 ? "end" : "middle"}" font-size="10" fill="var(--muted)">${trail[i].t}</text>`).join("");
    chartData = { trail, x, y, W, L0, R0 };
    return `<div class="legend"><span><i style="background:var(--series-1)"></i>Прогноз на T+10–15 мин</span>
        <span><i style="background:var(--series-2)"></i>Текущее отклонение</span>
        <span><i style="border-top:1px dashed var(--muted);height:0"></i>порог опоздания +120 с</span></div>
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" id="trail-svg" role="img" aria-label="График отклонения и прогноза">
        ${grid}
        <line x1="${L0}" x2="${W - R0}" y1="${y(120)}" y2="${y(120)}" stroke="var(--muted)" stroke-dasharray="4 4"/>
        <line x1="${L0}" x2="${W - R0}" y1="${y(0)}" y2="${y(0)}" stroke="var(--axis)"/>
        <path d="${path("cur_dev_s")}" fill="none" stroke="var(--series-2)" stroke-width="2" stroke-linejoin="round"/>
        <path d="${path("predicted_delay_s")}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round"/>
        <line id="xh" y1="${T0}" y2="${H - B0}" stroke="var(--muted)" visibility="hidden"/>
        <circle id="xh-a" r="4" fill="var(--series-1)" stroke="var(--surface)" stroke-width="2" visibility="hidden"/>
        <circle id="xh-b" r="4" fill="var(--series-2)" stroke="var(--surface)" stroke-width="2" visibility="hidden"/>
        ${xl}
        <rect x="${L0}" y="0" width="${W - L0 - R0}" height="${H}" fill="transparent"/>
      </svg>`;
  }
  function bindChartHover() {
    const svg = document.getElementById("trail-svg");
    if (!svg || !chartData) return;
    const tip = $("tooltip");
    const { trail, x, y } = chartData;
    svg.addEventListener("mousemove", (e) => {
      const r = svg.getBoundingClientRect();
      const px = ((e.clientX - r.left) / r.width) * chartData.W;
      let i = Math.round(((px - chartData.L0) / (chartData.W - chartData.L0 - chartData.R0)) * (trail.length - 1));
      i = Math.max(0, Math.min(trail.length - 1, i));
      const t = trail[i];
      [["#xh-a", "predicted_delay_s"], ["#xh-b", "cur_dev_s"]].forEach(([id, key]) => {
        const c = svg.querySelector(id);
        c.setAttribute("cx", x(i)); c.setAttribute("cy", y(t[key])); c.setAttribute("visibility", "visible");
      });
      const xh = svg.querySelector("#xh");
      xh.setAttribute("x1", x(i)); xh.setAttribute("x2", x(i)); xh.setAttribute("visibility", "visible");
      tip.innerHTML = `<b>${t.t}</b><br>Прогноз: ${fmtDelay(t.predicted_delay_s)}<br>Текущее: ${fmtDelay(t.cur_dev_s)}<br>P(опозд.): ${pct(t.p_late)}`;
      tip.style.left = `${e.clientX + 14}px`; tip.style.top = `${e.clientY + 10}px`;
      tip.classList.remove("hidden");
    });
    svg.addEventListener("mouseleave", () => {
      tip.classList.add("hidden");
      ["#xh", "#xh-a", "#xh-b"].forEach((s) => svg.querySelector(s).setAttribute("visibility", "hidden"));
    });
  }

  // ------------------------------------------------------------ транспорт
  let fitted = false;
  function render(s) {
    if (!s || s.type !== "snapshot") return;
    MT.snap = s;
    if (!fitted && MT.map) {
      const pts = s.vehicles.filter((v) => v.lat !== null && v.scheduled).map((v) => [v.lat, v.lon]);
      if (pts.length >= 2 && MT.tracked === null) { MT.map.fitBounds(pts, { padding: [40, 40], maxZoom: 13 }); fitted = true; }
    }
    renderHeader();
    renderMap();
    renderSide();
    renderBottom();
    MT.emit("snapshot", s);
  }

  let wsBackoff = 1000, lastMsg = 0;
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => { wsBackoff = 1000; };
    ws.onmessage = (e) => { lastMsg = Date.now(); try { render(JSON.parse(e.data)); } catch (err) { console.error(err); } };
    ws.onclose = () => { setTimeout(connect, wsBackoff); wsBackoff = Math.min(10000, wsBackoff * 2); };
    ws.onerror = () => ws.close();
  }
  setInterval(async () => {
    if (Date.now() - lastMsg < 5000) return;
    try { const st = await api("/api/state"); render(st); lastMsg = Date.now() - 3000; return; } catch (e) { /* */ }
    const mode = $("mode");
    mode.className = "mode degraded";
    mode.querySelector(".mode-text").textContent = "Нет связи с сервером";
    $("banner").textContent = "Нет связи с Backend — показано последнее полученное состояние";
    $("banner").classList.remove("hidden");
  }, 2500);

  // ------------------------------------------------------------ старт
  (async function init() {
    try { MT.region = await api("/api/region"); } catch (e) {
      MT.region = { name: "Москва и МО", center: [55.7558, 37.6176], zoom: 11, min_zoom: 8, max_zoom: 18, bounds: [[54.2, 35.1], [56.99, 40.25]],
        default_provider: "osm", provider_defs: { osm: { name: "OpenStreetMap", url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png", crs: "EPSG3857", max_zoom: 19, attribution: "&copy; OpenStreetMap" } } };
    }
    $("region-name").textContent = `${MT.region.name} · прогноз на 10–15 мин`;
    initProviders();
    buildMap();
    loadNetwork();
    connect();
    refreshFeeder();
    const m = location.hash.match(/track=(\d+)/);
    if (m) setTimeout(() => MT.track(+m[1]), 2500);
  })();
})();
