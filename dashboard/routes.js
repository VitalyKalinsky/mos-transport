/* Реестр реальных маршрутов: импорт (OSM / GTFS / вручную), привязка ТС, показ на карте, ссылки интеграции. */
(() => {
  "use strict";
  const MT = window.MT;
  const $ = (id) => document.getElementById(id);
  const body = $("routes-body");
  let routes = [];
  let focusLayer = null;

  async function load() {
    try { routes = await MT.api("/api/routes"); } catch (e) { routes = []; }
    render();
  }

  function vehOptions() {
    return (MT.snap ? MT.snap.vehicles : []).filter((v) => v.scheduled)
      .map((v) => `<option value="${v.tr_id}">ТС ${v.tr_id}${v.route_ref ? " · " + MT.esc(v.route_ref) : ""}</option>`).join("");
  }

  function render() {
    const MODE = { bus: "Автобус", trolleybus: "Троллейбус", tram: "Трамвай", share_taxi: "Маршрутка" };
    body.innerHTML = `
      <div class="form">
        <div class="section-title">Добавить реальный маршрут</div>
        <div class="delay-sub">Источники: OpenStreetMap (маршруты Москвы с остановками и геометрией по дорогам), GTFS перевозчика
          или список остановок — геометрия строится по дорогам роутером.</div>
        <div class="grid2">
          <label>Номер маршрута (OSM)<input id="rt-ref" placeholder="м6, т25, 297…"></label>
          <label>Привязать ТС<select id="rt-veh"><option value="">— не привязывать —</option>${vehOptions()}</select></label>
        </div>
        <div class="card-actions"><button class="btn primary" id="rt-osm">Импорт из OpenStreetMap</button>
          <label class="btn" style="display:inline-flex;align-items:center;gap:6px">GTFS (zip)<input type="file" id="rt-gtfs" accept=".zip" hidden></label>
          <button class="btn" id="rt-manual-toggle">Вручную…</button></div>
        <div class="form hidden" id="rt-manual">
          <label>Номер<input id="rt-m-ref" placeholder="Э100"></label>
          <label>Остановки по порядку: «Название; широта; долгота» — по строке на остановку
            <textarea id="rt-m-stops" rows="5" style="font:inherit;background:var(--surface-2);color:var(--ink);border:1px solid var(--border);border-radius:8px;padding:6px"
              placeholder="Киевский вокзал; 55.7437; 37.5663&#10;Смоленская площадь; 55.7478; 37.5838"></textarea></label>
          <div class="card-actions"><button class="btn primary" id="rt-m-save">Построить по дорогам и сохранить</button></div>
        </div>
        <div id="rt-msg" class="delay-sub"></div>
      </div>
      <div class="section-title">Маршруты в реестре (${routes.length})</div>
      ${routes.length ? routes.map((r) => `
        <div class="card">
          <div class="card-top"><span class="tag route" style="background:${MT.esc(r.color || "")}">${MT.esc(r.ref || "—")}</span>
            <span class="card-veh">${MT.esc(r.name || "")}</span><span class="card-time">${MODE[r.mode] || r.mode} · ${MT.esc(r.source)}</span></div>
          <div class="delay-sub">${r.stops} ост. · ${r.length_km ?? "—"} км${r.from ? ` · ${MT.esc(r.from)} → ${MT.esc(r.to)}` : ""}</div>
          <div class="delay-sub">ТС: ${r.vehicles.length ? r.vehicles.map((v) => `<b>${v}</b>`).join(", ") : "не привязаны"}</div>
          <div class="card-actions">
            <button class="btn" data-show="${MT.esc(r.id)}">Показать</button>
            <select data-link="${MT.esc(r.id)}"><option value="">Привязать ТС…</option>${vehOptions()}</select>
            ${r.builtin ? "" : `<button class="btn" data-del="${MT.esc(r.id)}">Удалить</button>`}
          </div>
        </div>`).join("") : '<div class="empty">Реестр пуст — импортируйте маршрут по номеру</div>'}
      <div class="section-title">Интеграция с картографическими сервисами</div>
      <div class="delay-sub">Фиды в стандарте GTFS-Realtime (принимают Яндекс Карты, 2ГИС и др.) — ТС на их картах показываются
        по нашим прогнозам (ETA + ML):</div>
      <div class="card-actions">
        <a class="btn" href="/api/export/gtfs-rt/vehicle-positions?format=json" target="_blank">VehiclePositions</a>
        <a class="btn" href="/api/export/gtfs-rt/trip-updates?format=json" target="_blank">TripUpdates</a>
        <a class="btn" href="/api/export/gtfs-rt/alerts?format=json" target="_blank">Alerts</a>
        <a class="btn" href="/api/export/gtfs-static.zip">GTFS (zip)</a>
        <a class="btn" href="/api/export/vehicles.geojson" target="_blank">GeoJSON</a>
        <a class="btn" href="/integrations/" target="_blank">Примеры: Яндекс / 2ГИС</a>
      </div>`;
    const msg = (t, bad) => { $("rt-msg").innerHTML = bad ? `<span class="d-late">${MT.esc(t)}</span>` : MT.esc(t); };
    $("rt-osm").onclick = async () => {
      const ref = $("rt-ref").value.trim();
      if (!ref) return msg("Введите номер маршрута", true);
      msg("Запрос к OpenStreetMap (Overpass)…");
      try {
        const veh = $("rt-veh").value;
        const res = await MT.api("/api/routes/import/osm", { method: "POST", body: JSON.stringify({ ref, vehicles: veh ? [+veh] : [] }) });
        msg(`Импортировано направлений: ${res.length} (${res.map((r) => r.stops + " ост.").join(", ")})`);
        await load(); MT.reloadNetwork(); show(res[0].id);
      } catch (e) { msg(e.message, true); }
    };
    $("rt-gtfs").onchange = async (e) => {
      const f = e.target.files[0];
      if (!f) return;
      msg("Загрузка GTFS…");
      const fd = new FormData(); fd.append("file", f);
      try {
        const r = await fetch("/api/routes/import/gtfs", { method: "POST", body: fd });
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || r.status);
        msg(`Импортировано вариантов маршрутов: ${j.length}`);
        await load(); MT.reloadNetwork();
      } catch (err) { msg(err.message, true); }
    };
    $("rt-manual-toggle").onclick = () => $("rt-manual").classList.toggle("hidden");
    $("rt-m-save").onclick = async () => {
      const stops = $("rt-m-stops").value.split("\n").map((l) => l.split(";").map((x) => x.trim())).filter((p) => p.length >= 3)
        .map(([name, lat, lon]) => ({ name, lat: +lat, lon: +lon })).filter((s) => isFinite(s.lat) && isFinite(s.lon));
      if (stops.length < 2) return msg("Нужно минимум 2 остановки", true);
      msg("Построение геометрии по дорогам…");
      try {
        const r = await MT.api("/api/routes", { method: "POST", body: JSON.stringify({ ref: $("rt-m-ref").value || "Новый", stops }) });
        msg(`Сохранено: ${r.ref}, ${r.length_km} км`);
        await load(); MT.reloadNetwork(); show(r.id);
      } catch (e) { msg(e.message, true); }
    };
    body.querySelectorAll("[data-show]").forEach((b) => { b.onclick = () => show(b.dataset.show); });
    body.querySelectorAll("[data-del]").forEach((b) => { b.onclick = async () => {
      await MT.api(`/api/routes/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" }).catch(() => {});
      await load(); MT.reloadNetwork();
    }; });
    body.querySelectorAll("[data-link]").forEach((s) => { s.onchange = async () => {
      if (!s.value) return;
      const r = routes.find((x) => x.id === s.dataset.link);
      await MT.api(`/api/routes/${encodeURIComponent(r.id)}/vehicles`, { method: "PUT", body: JSON.stringify([...new Set([...r.vehicles, +s.value])]) }).catch(() => {});
      await load();
    }; });
  }

  async function show(id) {
    let r;
    try { r = await MT.api(`/api/routes/${encodeURIComponent(id)}`); } catch (e) { return; }
    if (focusLayer) MT.map.removeLayer(focusLayer);
    focusLayer = L.layerGroup().addTo(MT.map);
    L.polyline(r.geometry, { color: MT.cssVar("--route-casing"), weight: 10, opacity: 0.9 }).addTo(focusLayer);
    L.polyline(r.geometry, { color: r.color || MT.cssVar("--route"), weight: 6 }).bindTooltip(`${MT.esc(r.ref)} · ${MT.esc(r.name)}`, { sticky: true }).addTo(focusLayer);
    r.stops.forEach((s) => L.circleMarker([s.lat, s.lon], { radius: 5, color: r.color || MT.cssVar("--route"), weight: 2, fillColor: MT.cssVar("--surface"), fillOpacity: 1 })
      .bindTooltip(MT.esc(s.name || "Остановка")).addTo(focusLayer));
    MT.map.fitBounds(r.geometry, { padding: [40, 40] });
  }

  MT.on("route-focus", (id) => { MT.setTab("routes"); show(id); });
  MT.on("tab", (t) => { if (t === "routes") load(); else if (focusLayer) { MT.map.removeLayer(focusLayer); focusLayer = null; } });
})();
