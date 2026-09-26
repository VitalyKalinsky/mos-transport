/* Диспетчерский дашборд: WebSocket-поток снимков от Backend → карта, KPI, карточки инцидентов. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const LEVEL_TXT = { red: "Высокий риск", yellow: "Внимание", green: "В графике", gray: "Нет данных" };
  const LEVEL_ORDER = { red: 3, yellow: 2, green: 1, gray: 0 };

  let snap = null;
  let tab = "open";
  let levelFilter = null;
  let drawerId = null;
  let drawerTimer = null;
  const markers = new Map();
  const segLayers = new Map();

  // ------------------------------------------------------------ helpers
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  function fmtDelay(s) {
    if (s === null || s === undefined) return "—";
    const sign = s > 0 ? "+" : s < 0 ? "−" : "";
    const a = Math.abs(Math.round(s));
    const m = Math.floor(a / 60), sec = a % 60;
    return m ? `${sign}${m} мин ${String(sec).padStart(2, "0")} с` : `${sign}${sec} с`;
  }
  const pct = (p) => (p === null || p === undefined ? "—" : `${Math.round(p * 100)}%`);
  const lvlIcon = (l) => `<i class="lvl lvl-${l}" aria-hidden="true"></i>`;
  const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  function store(k, v) { try { v === undefined ? localStorage.getItem(k) : localStorage.setItem(k, v); } catch (e) { /* storage недоступен */ } }
  function load(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }

  // ------------------------------------------------------------ theme + map
  const map = L.map("map", { zoomControl: true, preferCanvas: false }).setView([55.76, 37.55], 11);
  let tiles = null;
  const netLayer = L.layerGroup().addTo(map);
  const stopLayer = L.layerGroup();
  const pathLayer = L.layerGroup().addTo(map);
  const vehLayer = L.layerGroup().addTo(map);
  function applyTheme(t) {
    document.documentElement.dataset.theme = t;
    store("theme", t);
    if (tiles) map.removeLayer(tiles);
    tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(map);
    tiles.bringToBack();
    recolorSegments();
  }
  applyTheme(load("theme") || "dark");
  $("theme-btn").onclick = () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");


  async function loadNetwork() {
    try {
      const r = await fetch("/api/network");
      if (!r.ok) throw new Error(r.status);
      const net = await r.json();
      const bounds = [];
      net.segments.forEach((s) => {
        const pl = L.polyline(s.coords, { weight: 2, opacity: 0.55, interactive: false });
        pl.addTo(netLayer);
        segLayers.set(s.id, pl);
        bounds.push(...s.coords);
      });
      net.stops.forEach((s) => {
        L.circleMarker([s.lat, s.lon], { radius: 2.5, weight: 1, fillOpacity: 0.9 })
          .bindTooltip(esc(s.address) || "Остановка", { direction: "top" }).addTo(stopLayer);
      });
      recolorSegments();
    } catch (e) {
      setTimeout(loadNetwork, 3000);
    }
  }
  map.on("zoomend", () => {
    if (map.getZoom() >= 14) stopLayer.addTo(map); else map.removeLayer(stopLayer);
  });

  function recolorSegments() {
    const base = cssVar("--series-1") || "#3987e5";
    const levels = (snap && snap.segments) || {};
    const colors = { red: cssVar("--critical"), yellow: cssVar("--warning") };
    segLayers.forEach((pl, id) => {
      const lvl = levels[id];
      if (lvl) { pl.setStyle({ color: colors[lvl], weight: lvl === "red" ? 6 : 4, opacity: 0.95 }); pl.bringToFront(); }
      else pl.setStyle({ color: base, weight: 2.5, opacity: 0.55 });
    });
    stopLayer.eachLayer((m) => m.setStyle({ color: base, fillColor: cssVar("--surface") }));
  }

  function vehIcon(v) {
    const cls = `${v.level}${v.stale ? " stale" : ""}`;
    const arrow = v.state === "moving" && !v.stale ? `<span class="veh-arrow" style="transform: translate(-50%,-100%) rotate(${v.heading}deg)"></span>` : "";
    const pulse = v.level === "red" && !v.stale ? '<span class="veh-pulse"></span>' : "";
    const label = v.level === "red" || v.level === "yellow" ? `<span class="veh-label">${fmtDelay(v.predicted_delay_s)}</span>` : "";
    return L.divIcon({ className: "veh-marker", iconSize: [22, 22], iconAnchor: [11, 11],
      html: `${pulse}<div class="veh-dot ${cls}">${arrow}</div>${label}` });
  }

  function vehTooltip(v) {
    const who = v.scheduled ? `ТС ${v.tr_id} · ${v.route_id}` : `Терминал ${v.unit_id} (без наряда)`;
    const lines = [`<b>${esc(who)}</b>`, `${lvlIcon(v.level)} ${LEVEL_TXT[v.level]}`];
    if (v.predicted_delay_s !== null) lines.push(`Прогноз: <b>${fmtDelay(v.predicted_delay_s)}</b> · P(опозд.) ${pct(v.p_late)}`);
    if (v.cur_dev_s !== null) lines.push(`Сейчас: ${fmtDelay(v.cur_dev_s)}`);
    if (v.cause) lines.push(`Причина: ${esc(v.cause)}`);
    lines.push(`Скорость ${v.speed ?? "—"} км/ч · данные ${v.last_seen ?? "—"}${v.stale ? " (устарели)" : ""}`);
    return lines.join("<br>");
  }

  function renderMap() {
    const seen = new Set();
    snap.vehicles.forEach((v) => {
      if (v.lat === null || v.lon === null) return;
      const hidden = levelFilter && !(levelFilter === "stale" ? v.stale || v.level === "gray" : v.level === levelFilter);
      const key = v.unit_id;
      seen.add(key);
      let m = markers.get(key);
      if (!m) {
        m = L.marker([v.lat, v.lon], { icon: vehIcon(v) }).addTo(vehLayer);
        m.bindTooltip("", { direction: "top", offset: [0, -10] });
        m.on("click", () => openDrawer(key));
        markers.set(key, m);
      }
      m.setLatLng([v.lat, v.lon]);
      const sig = `${v.level}|${v.stale}|${v.state}|${Math.round(v.heading / 10)}|${Math.round((v.predicted_delay_s || 0) / 5)}`;
      if (m._sig !== sig) { m.setIcon(vehIcon(v)); m._sig = sig; }
      m.setTooltipContent(vehTooltip(v));
      m.setZIndexOffset(LEVEL_ORDER[v.level] * 1000);
      m.setOpacity(hidden ? 0.15 : 1);
    });
    markers.forEach((m, k) => { if (!seen.has(k)) { vehLayer.removeLayer(m); markers.delete(k); } });
    recolorSegments();
  }

  // ------------------------------------------------------------ header/KPI
  function renderHeader() {
    const s = snap.status;
    $("clock").textContent = snap.data_time_local.split(" ")[1];
    $("clock-date").textContent = `${snap.data_time_local.split(" ")[0]} МСК · поток ×${s.clock_rate}`;
    const mode = $("mode");
    mode.className = `mode ${s.mode}`;
    mode.querySelector(".mode-text").textContent = s.mode === "online" ? "Онлайн" : s.mode === "waiting" ? "Ожидание потока" : "Деградация";
    const banner = $("banner");
    if (s.reasons.length && s.mode !== "online") {
      banner.textContent = s.reasons.join(" · ") + (s.link === "lost" ? ` (нет пакетов ${Math.round(s.last_packet_age_s)} с)` : "");
      banner.classList.remove("hidden");
    } else banner.classList.add("hidden");
    $("chip-link").className = `chip ${s.link === "online" ? "ok" : s.link === "lost" ? "bad" : ""}`;
    $("chip-link").title = `Поток NDTP: ${s.link}`;
    $("chip-ml").className = `chip ${s.ml_available ? "ok" : "bad"}`;
    $("chip-ml").title = s.ml_available ? "ML-сервис доступен" : `ML недоступен: ${s.ml_last_error || ""}`;

    const k = snap.kpi;
    $("k-red").textContent = k.red;
    $("k-yellow").textContent = k.yellow;
    $("k-green").textContent = k.green;
    $("k-gray").textContent = k.gray;
    $("k-gray-sub").textContent = `${k.stale} без связи · ${k.vehicles_total - k.scheduled} без наряда · ${k.no_target} вне рейса`;
    document.querySelector(".kpi-red").classList.toggle("hot", k.red > 0);
    const acc = snap.perf.accuracy;
    $("k-mae").textContent = acc.n ? `${acc.mae_model_s} с` : "—";
    $("k-mae-sub").textContent = acc.n ? `baseline ${acc.mae_baseline_s} с · n=${acc.n}` : "накапливается по факту прибытий";
    $("k-lat").textContent = snap.perf.e2e_p95_ms !== null ? `${Math.round(snap.perf.e2e_p95_ms)} мс` : "—";
    $("k-lat-sub").textContent = `пакет→прогноз p95 · ML p95 ${snap.perf.ml_p95_ms !== null ? Math.round(snap.perf.ml_p95_ms) + " мс" : "—"}`;
    document.querySelectorAll(".kpi[data-filter]").forEach((b) => b.classList.toggle("active", b.dataset.filter === levelFilter));
    $("map-filter").innerHTML = levelFilter ? `<span class="chip" id="clear-filter">Фильтр: ${esc(LEVEL_TXT[levelFilter] || "нет связи")} ✕</span>` : "";
  }
  document.querySelectorAll(".kpi[data-filter]").forEach((b) => b.addEventListener("click", () => {
    levelFilter = levelFilter === b.dataset.filter ? null : b.dataset.filter;
    if (snap) { renderHeader(); renderMap(); renderSide(); }
  }));
  $("map-filter").addEventListener("click", () => { levelFilter = null; if (snap) { renderHeader(); renderMap(); renderSide(); } });

  // ------------------------------------------------------------ side panel
  function incidentCard(i, resolved = false) {
    const c = i.causes[0] || {};
    const more = i.causes.slice(1).map((x) => `<span class="tag">${esc(x.title)}</span>`).join(" ");
    return `<article class="card ${i.level}${i.acknowledged ? " acked" : ""}" data-unit="${i.unit_id}">
      <div class="card-top">
        <span class="badge ${resolved ? "green" : i.level}">${lvlIcon(resolved ? "green" : i.level)}${resolved ? "Закрыт" : LEVEL_TXT[i.level]}</span>
        <span class="card-veh">ТС ${i.tr_id} · ${esc(i.route_id || "")}</span>
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
      <div class="reco">${esc(c.recommendation || "")}</div>
      <div class="card-actions">
        <button class="btn" data-act="show" data-unit="${i.unit_id}">На карте</button>
        <button class="btn" data-act="detail" data-unit="${i.unit_id}">Подробнее</button>
        ${i.acknowledged ? '<span class="tag">принят в работу</span>' : `<button class="btn primary" data-act="ack" data-id="${esc(i.id)}">Принять</button>`}
      </div>`}
    </article>`;
  }

  function vehicleRow(v) {
    const who = v.scheduled ? `ТС ${v.tr_id} · ${v.route_id}` : `Терминал ${v.unit_id}`;
    const sub = v.scheduled ? (v.no_target ? "нет рейса в горизонте 10–15 мин" : `${esc(v.cause || "")}${v.stale ? " · данные устарели" : ""}`) : "без наряда (нет расписания)";
    return `<div class="vrow" data-act="detail" data-unit="${v.unit_id}">${lvlIcon(v.level)}
      <div><div class="t">${esc(who)}</div><div class="s">${sub}</div></div>
      <div class="v">${v.predicted_delay_s !== null ? fmtDelay(v.predicted_delay_s) : "—"}</div></div>`;
  }

  function renderSide() {
    const body = $("side-body");
    const top = body.scrollTop;
    $("inc-count").textContent = snap.kpi.incidents_unacked;
    let html = "";
    if (tab === "open") {
      const list = snap.incidents.filter((i) => !levelFilter || levelFilter === "stale" || i.level === levelFilter);
      html = list.length ? list.map((i) => incidentCard(i)).join("") :
        `<div class="empty">${lvlIcon("green")} Нет ТС с риском отклонения в горизонте 10–15 минут</div>`;
    } else if (tab === "vehicles") {
      const list = snap.vehicles.filter((v) => !levelFilter || (levelFilter === "stale" ? v.stale || v.level === "gray" : v.level === levelFilter));
      html = list.map(vehicleRow).join("") || '<div class="empty">Нет ТС</div>';
    } else {
      html = snap.resolved.length ? snap.resolved.map((i) => incidentCard(i, true)).join("") : '<div class="empty">Закрытых инцидентов пока нет</div>';
    }
    body.innerHTML = html;
    body.scrollTop = top;
  }
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
    tab = t.dataset.tab;
    if (snap) renderSide();
  }));
  $("side-body").addEventListener("click", async (e) => {
    const el = e.target.closest("[data-act]");
    if (!el) return;
    const unit = Number(el.dataset.unit);
    if (el.dataset.act === "ack") {
      el.disabled = true;
      await fetch(`/api/incidents/${encodeURIComponent(el.dataset.id)}/ack`, { method: "POST" }).catch(() => {});
    } else if (el.dataset.act === "show") {
      const m = markers.get(unit);
      if (m) { map.flyTo(m.getLatLng(), 15, { duration: 0.6 }); m.openTooltip(); }
    } else if (el.dataset.act === "detail") {
      openDrawer(unit);
    }
  });

  // ------------------------------------------------------------ bottom
  function renderBottom() {
    $("routes").innerHTML = `<h4>Риск по маршрутам</h4><div class="route-chips">${snap.routes.map((r) =>
      `<span class="rchip" title="${r.vehicles} ТС: ${r.red} красн., ${r.yellow} жёлт., ${r.green} зел.">${lvlIcon(r.level)}${esc(r.id)}${r.red + r.yellow ? ` <span class="tag">${r.red + r.yellow}</span>` : ""}</span>`).join("") || "—"}</div>`;
    $("events").innerHTML = `<h4>Журнал событий</h4>${snap.events.map((e) =>
      `<div class="ev ${e.level}"><span class="tm">${e.time}</span><span class="tx">${esc(e.text)}</span></div>`).join("") || '<div class="ev"><span></span><span class="tx">—</span></div>'}`;
    const p = snap.perf, s = snap.status;
    const f = (v, u) => (v === null || v === undefined ? "—" : `${Math.round(v * 10) / 10} ${u}`);
    $("perf").innerHTML = `<h4>Производительность</h4><div class="perf-grid">
      <span>Пакетов NDTP / с</span><span>${p.packets_per_s}</span>
      <span>ML-инференс p95</span><span>${f(p.ml_p95_ms, "мс")}</span>
      <span>Пакет → прогноз p95</span><span>${f(p.e2e_p95_ms, "мс")}</span>
      <span>Цикл прогноза p95</span><span>${f(p.cycle_p95_ms, "мс")}</span>
      <span>Последний пакет</span><span>${s.last_packet_age_s === null ? "—" : f(s.last_packet_age_s, "с назад")}</span></div>`;
  }

  // ------------------------------------------------------------ drawer
  async function openDrawer(unit) {
    drawerId = unit;
    $("drawer").classList.remove("hidden");
    $("drawer-body").innerHTML = '<div class="empty">Загрузка…</div>';
    clearInterval(drawerTimer);
    await refreshDrawer();
    drawerTimer = setInterval(refreshDrawer, 3000);
  }
  $("drawer-close").onclick = () => { drawerId = null; clearInterval(drawerTimer); $("drawer").classList.add("hidden"); pathLayer.clearLayers(); };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("drawer-close").click(); });

  async function refreshDrawer() {
    if (drawerId === null) return;
    let d;
    try { const r = await fetch(`/api/vehicles/${drawerId}`); if (!r.ok) throw 0; d = await r.json(); }
    catch (e) { $("drawer-body").innerHTML = '<div class="empty">Нет данных по ТС</div>'; return; }
    const p = d.prediction || {};
    $("drawer-title").innerHTML = `${lvlIcon(d.level)} ${d.scheduled ? `ТС ${d.tr_id} · ${esc(d.route_id)}` : `Терминал ${d.unit_id}`}`;
    const seg = p.segment || {};
    const causes = (p.causes || []).map((c) => `<div class="cause"><b>${esc(c.title)}</b><span class="detail">${esc(c.detail)}</span><div class="reco">${esc(c.recommendation)}</div></div>`).join("");
    const feats = Object.entries(d.features || {}).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${v === null ? "—" : typeof v === "number" ? Math.round(v * 100) / 100 : esc(v)}</td></tr>`).join("");
    $("drawer-body").innerHTML = `
      <div class="kv">
        <div><div class="k">Прогноз опоздания</div><div class="v">${fmtDelay(d.predicted_delay_s)}</div></div>
        <div><div class="k">P(опоздание &gt;2 мин)</div><div class="v">${pct(d.p_late)}</div></div>
        <div><div class="k">Текущее отклонение</div><div class="v">${fmtDelay(d.cur_dev_s)}</div></div>
        <div><div class="k">Ср. скорость на сегменте</div><div class="v">${seg.avg_speed_kmh != null ? Math.round(seg.avg_speed_kmh) + " км/ч" : "—"}</div></div>
        <div><div class="k">Плановая скорость</div><div class="v">${seg.plan_speed_kmh != null ? Math.round(seg.plan_speed_kmh) + " км/ч" : "—"}</div></div>
        <div><div class="k">Время простоя</div><div class="v">${p.dwell_s != null ? Math.round(p.dwell_s) + " с" : "—"}</div></div>
        <div><div class="k">Возраст телеметрии</div><div class="v">${p.telemetry_age_s != null ? Math.round(p.telemetry_age_s) + " с" : "—"}</div></div>
        <div><div class="k">Скорость сейчас</div><div class="v">${d.speed ?? "—"} км/ч</div></div>
        <div><div class="k">Источник прогноза</div><div class="v">${p.source === "model" ? "ML-модель" : p.source === "fallback" ? "baseline" : "—"}</div></div>
      </div>
      ${p.target_address ? `<div class="row"><span class="k">Цель</span><span><b>${esc(p.target_address)}</b> (план ${esc(d.target_plan || "")})</span></div>` : ""}
      ${seg.from || seg.to ? `<div class="row"><span class="k">Участок</span><span>${esc((seg.from || {}).address || "—")} → ${esc((seg.to || {}).address || "—")}</span></div>` : ""}
      ${causes ? `<div><h5>Причины и рекомендации</h5>${causes}</div>` : ""}
      <div class="chart"><h5>Отклонение и прогноз, с</h5>${trailChart(d.trail)}</div>
      <div><h5>Факт прибытия (детектор по GPS)</h5>${arrivalsTable(d.arrivals)}</div>
      ${feats ? `<details><summary>Признаки модели (${Object.keys(d.features).length})</summary><table class="tbl">${feats}</table></details>` : ""}
      <div class="delay-sub">Пакетов: ${d.stats.packets} · прибытий обнаружено: ${d.stats.arrivals_detected}, пропущено детектором: ${d.stats.arrivals_skipped}</div>`;
    bindChartHover();
    pathLayer.clearLayers();
    if (d.upcoming && d.upcoming.length > 1) {
      L.polyline(d.upcoming, { color: cssVar("--series-1"), weight: 4, dashArray: "6 6", opacity: 0.9 }).addTo(pathLayer);
    }
  }

  function arrivalsTable(rows) {
    if (!rows || !rows.length) return '<div class="delay-sub">Пока нет</div>';
    return `<table class="tbl"><tr><th>Остановка</th><th>План</th><th>Факт</th><th class="num">Откл.</th></tr>${rows.slice().reverse().map((a) =>
      `<tr><td>${esc(a.stop)}</td><td>${a.plan}</td><td>${a.fact}</td><td class="num">${fmtDelay(a.delay_s)}</td></tr>`).join("")}</table>`;
  }

  // Линейный график: 2 серии (прогноз / текущее отклонение) + порог опоздания, общая ось Y.
  let chartData = null;
  function trailChart(trail) {
    if (!trail || trail.length < 2) { chartData = null; return '<div class="delay-sub">Накапливается история прогнозов…</div>'; }
    const W = 480, H = 190, L0 = 44, R0 = 8, T0 = 10, B0 = 22;
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
    chartData = { trail, x, y, W, H, L0, R0, T0, B0 };
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
        <rect x="${L0}" y="0" width="${W - L0 - R0}" height="${H}" fill="transparent" id="xh-hit"/>
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
      for (const [id, key] of [["xh-a", "predicted_delay_s"], ["xh-b", "cur_dev_s"]]) {
        const c = svg.querySelector(`#${id}`);
        c.setAttribute("cx", x(i)); c.setAttribute("cy", y(t[key])); c.setAttribute("visibility", "visible");
      }
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

  // ------------------------------------------------------------ transport
  let fitted = false;
  function render(s) {
    if (!s || s.type !== "snapshot") return;
    snap = s;
    if (!fitted) {
      const pts = s.vehicles.filter((v) => v.lat !== null && v.scheduled).map((v) => [v.lat, v.lon]);
      if (pts.length >= 2) { map.fitBounds(pts, { padding: [40, 40], maxZoom: 13 }); fitted = true; }
    }
    renderHeader();
    renderMap();
    renderSide();
    renderBottom();
  }

  let ws = null, wsBackoff = 1000, lastMsg = 0;
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => { wsBackoff = 1000; };
    ws.onmessage = (e) => { lastMsg = Date.now(); try { render(JSON.parse(e.data)); } catch (err) { console.error(err); } };
    ws.onclose = () => { setTimeout(connect, wsBackoff); wsBackoff = Math.min(10000, wsBackoff * 2); };
    ws.onerror = () => ws.close();
  }
  // Резерв: если WS молчит > 5 с — опрашиваем REST и показываем потерю связи с сервером
  setInterval(async () => {
    if (Date.now() - lastMsg < 5000) return;
    try {
      const r = await fetch("/api/state");
      if (r.ok) { render(await r.json()); lastMsg = Date.now() - 3000; return; }
    } catch (e) { /* сервер недоступен */ }
    const mode = $("mode");
    mode.className = "mode degraded";
    mode.querySelector(".mode-text").textContent = "Нет связи с сервером";
    $("banner").textContent = "Нет связи с Backend — показано последнее полученное состояние";
    $("banner").classList.remove("hidden");
  }, 2500);

  // ------------------------------------------------------------ demo panel
  async function demoStatus() {
    if (!$("demo").open) return;
    try {
      const s = await (await fetch("/feeder/status")).json();
      $("demo-status").textContent = `${s.running ? "▶" : "⏸"} ×${s.speed} · ${s.data_time_utc.slice(11, 19)} UTC · подключено ${s.connected_units}/${s.units}` +
        (s.outage_remaining_s ? ` · обрыв ещё ${Math.round(s.outage_remaining_s)} с` : "");
    } catch (e) { $("demo-status").textContent = "фидер недоступен"; }
  }
  setInterval(demoStatus, 2000);
  $("demo").addEventListener("toggle", demoStatus);
  $("demo").addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const url = b.dataset.demo ? `/feeder/${b.dataset.demo}` : `/feeder/seek?t=${encodeURIComponent(b.dataset.demoSeek)}`;
    await fetch(url, { method: "POST" }).catch(() => {});
    demoStatus();
  });

  loadNetwork();
  connect();
})();
