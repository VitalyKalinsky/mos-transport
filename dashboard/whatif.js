/* What-if: конструктор сценариев, расчёт «до/после», отображение на карте, применение к живому прогнозу. */
(() => {
  "use strict";
  const MT = window.MT;
  const $ = (id) => document.getElementById(id);
  const body = $("whatif-body");
  const TYPES = {
    breakdown: { title: "Поломка ТС", hint: "ТС встаёт на линии; каскад на следующие остановки и рейсы",
      params: [["minutes", "Длительность простоя, мин", 10, 1, 90]] },
    accident: { title: "ДТП на участке", hint: "Пропускная способность перегонов впереди ТС падает",
      params: [["minutes", "Длительность, мин", 20, 5, 120], ["capacity", "Пропускная способность, %", 35, 5, 95]] },
    blockage: { title: "Засор / затор", hint: "Снижение скорости на участке",
      params: [["minutes", "Длительность, мин", 20, 5, 120], ["capacity", "Пропускная способность, %", 60, 10, 95]] },
    traffic: { title: "Пробки и автобусная полоса", hint: "Балл пробок на перегонах впереди; выделенная полоса снимает до 70% влияния",
      params: [["jam", "Пробки, баллы 0–10", 6, 0, 10], ["segments", "Перегонов впереди", 8, 1, 30], ["bus_lane", "Есть автобусная полоса", false]] },
    signal_priority: { title: "Светофорное регулирование", hint: "Приоритет ОТ на светофорах сокращает время перегонов",
      params: [["gain", "Сокращение времени перегона, %", 15, 5, 40], ["segments", "Перегонов впереди", 10, 1, 30]] },
    skip_stops: { title: "Пропуск остановок", hint: "Экономия времени стоянок ценой необслуженных остановок",
      params: [["count", "Число остановок", 2, 1, 6]] },
    detour: { title: "Смена маршрута (объезд)", hint: "Закрытие участка, объезд строится по дорогам, закрытые остановки не обслуживаются",
      params: [["offset", "Закрытие начинается через N остановок", 0, 0, 10], ["closed_stops", "Закрыто остановок", 1, 1, 6], ["side_m", "Смещение объезда, м", 450, 150, 1500]] },
    reserve_bus: { title: "Выпуск резерва", hint: "Резерв выходит на линию по графику проблемного ТС; пассажиры распределяются",
      params: [["dispatch_min", "Выход на линию через, мин", 8, 2, 40]] },
    domino: { title: "Купирование эффекта домино", hint: "Остановить перенос опоздания на следующие рейсы",
      params: [["action", "Мера", "short_turn", [["short_turn", "Укороченный рейс"], ["skip", "Пропуск остановок"], ["reserve", "Резерв на следующий рейс"], ["hold", "Удержание следующего ТС"]]]] },
  };
  let list = [];            // накопленные сценарии
  let lastResult = null;
  const overlay = () => MT.layers.overlay;

  function vehicleOptions(sel) {
    const vs = (MT.snap ? MT.snap.vehicles : []).filter((v) => v.scheduled && v.eta_mode);
    return vs.map((v) => `<option value="${v.unit_id}" ${v.unit_id === sel ? "selected" : ""}>${MT.esc(MT.vname(v))} · ${MT.fmtDelay(v.predicted_delay_s)}</option>`).join("");
  }

  function paramInputs(type, vals = {}) {
    return TYPES[type].params.map(([k, label, def, a, b]) => {
      const v = vals[k] ?? def;
      if (typeof def === "boolean") return `<label class="chk"><input type="checkbox" data-p="${k}" ${v ? "checked" : ""}> ${label}</label>`;
      if (Array.isArray(a)) return `<label>${label}<select data-p="${k}">${a.map(([o, t]) => `<option value="${o}" ${o === v ? "selected" : ""}>${t}</option>`).join("")}</select></label>`;
      return `<label>${label}<input type="number" data-p="${k}" value="${v}" min="${a}" max="${b}"></label>`;
    }).join("");
  }

  function renderForm(prefill = {}) {
    const type = prefill.type || $("wi-type")?.value || "breakdown";
    const unit = prefill.unit_id ?? (+($("wi-unit")?.value) || MT.tracked);
    body.innerHTML = `
      <div class="form">
        <div class="section-title">Сценарий</div>
        <label>Воздействие<select id="wi-type">${Object.entries(TYPES).map(([k, t]) => `<option value="${k}" ${k === type ? "selected" : ""}>${t.title}</option>`).join("")}</select></label>
        <div class="delay-sub" id="wi-hint">${TYPES[type].hint}</div>
        <label>Транспортное средство<select id="wi-unit">${vehicleOptions(unit)}</select></label>
        <div class="grid2" id="wi-params">${paramInputs(type)}</div>
        <div class="card-actions">
          <button class="btn" id="wi-add">+ Добавить в комбинацию</button>
          <button class="btn primary" id="wi-run">Рассчитать</button>
        </div>
        <div class="sc-list" id="wi-list"></div>
      </div>
      <div id="wi-result"></div>
      <div id="wi-live"></div>`;
    $("wi-type").onchange = () => { $("wi-hint").textContent = TYPES[$("wi-type").value].hint; $("wi-params").innerHTML = paramInputs($("wi-type").value); };
    $("wi-add").onclick = () => { list.push(current()); renderList(); };
    $("wi-run").onclick = run;
    renderList();
    renderLive();
  }

  function current() {
    const type = $("wi-type").value;
    const params = { unit_id: +$("wi-unit").value };
    body.querySelectorAll("#wi-params [data-p]").forEach((el) => {
      let v = el.type === "checkbox" ? el.checked : el.tagName === "SELECT" ? el.value : +el.value;
      if (el.dataset.p === "capacity" || el.dataset.p === "gain") v = v / 100;
      params[el.dataset.p] = v;
    });
    return { type, params };
  }

  function renderList() {
    $("wi-list").innerHTML = list.map((s, i) => `<span class="sc-chip">${TYPES[s.type].title} · ТС ${s.params.unit_id}<button data-rm="${i}" aria-label="Убрать">✕</button></span>`).join("");
    $("wi-list").querySelectorAll("[data-rm]").forEach((b) => { b.onclick = () => { list.splice(+b.dataset.rm, 1); renderList(); }; });
  }

  async function run() {
    const scenarios = list.length ? list : [current()];
    $("wi-result").innerHTML = '<div class="empty">Расчёт…</div>';
    try {
      lastResult = await MT.api("/api/whatif/simulate", { method: "POST", body: JSON.stringify({ scenarios, horizon_stops: 12 }) });
      lastResult._scenarios = scenarios;
      renderResult(lastResult);
      drawShapes(lastResult.shapes);
    } catch (e) { $("wi-result").innerHTML = `<div class="effect bad">Ошибка: ${MT.esc(e.message)}</div>`; }
  }

  function renderResult(r) {
    const t = r.totals, e = r.effect;
    const cls = e.delay_min_saved > 0.5 ? "good" : e.delay_min_saved < -0.5 ? "bad" : "neutral";
    const maxD = Math.max(1, t.before.delay_min, t.after.delay_min);
    const vehBlocks = r.affected.slice(0, 6).map((a) => `
      <details ${a.unit_id === r._scenarios[0].params.unit_id ? "open" : ""}><summary><b>ТС ${a.tr_id}</b> ${a.route ? `<span class="tag route">${MT.esc(a.route)}</span>` : ""}
        · опоздание ${a.before.delay_min} → <b>${a.after.delay_min}</b> мин·ост · остановок &gt;2 мин: ${a.before.late_stops} → ${a.after.late_stops}
        ${a.before.later_trips_late || a.after.later_trips_late ? ` · следующих рейсов с опозданием: ${a.before.later_trips_late} → ${a.after.later_trips_late}` : ""}</summary>
        <table class="tbl"><tr><th>Остановка</th><th>План</th><th>До</th><th>После</th></tr>
        ${a.stops.slice(0, 14).map((s) => `<tr class="${s.skipped ? "skipped" : ""}"><td>${MT.esc(s.address)}</td><td>${s.plan}</td>
          <td class="${MT.delayCls(s.delay_before_s)}">${MT.fmtDelay(s.delay_before_s)}</td>
          <td class="${MT.delayCls(s.delay_after_s)}"><b>${s.skipped ? "пропуск" : MT.fmtDelay(s.delay_after_s)}</b></td></tr>`).join("")}</table>
      </details>`).join("");
    $("wi-result").innerHTML = `
      <div class="section-title">Результат на ${r.T}</div>
      <div class="effect ${cls}">${MT.esc(r.recommendation)}</div>
      <div class="bar-cmp"><span>Без мер</span><span class="bar b0" style="width:${(t.before.delay_min / maxD) * 100}%"></span><span>${t.before.delay_min} мин</span></div>
      <div class="bar-cmp"><span>Сценарий</span><span class="bar b1" style="width:${(t.after.delay_min / maxD) * 100}%"></span><span>${t.after.delay_min} мин</span></div>
      <div class="compare">
        <div><div class="k">Суммарное опоздание (мин·остановок)</div><div class="v">${t.before.delay_min} → ${t.after.delay_min}</div></div>
        <div><div class="k">Остановок с опозданием &gt; 2 мин</div><div class="v">${t.before.late_stops} → ${t.after.late_stops}</div></div>
      </div>
      ${r.notes.length ? `<div class="delay-sub">${r.notes.map(MT.esc).join("<br>")}</div>` : ""}
      ${vehBlocks || '<div class="delay-sub">Сценарий не затрагивает ТС в горизонте прогноза</div>'}
      <div class="form">
        <div class="grid2">
          <label>Действует, мин (время потока)<input type="number" id="wi-dur" value="30" min="5" max="240"></label>
          <label class="chk"><input type="checkbox" id="wi-inject" checked> Имитировать в потоке телематики (поломка/ДТП/засор)</label>
        </div>
        <div class="card-actions"><button class="btn primary" id="wi-apply">Применить к живому прогнозу</button>
          <button class="btn" id="wi-clear-shapes">Скрыть на карте</button></div>
      </div>`;
    $("wi-apply").onclick = apply;
    $("wi-clear-shapes").onclick = () => overlay().clearLayers();
  }

  async function apply() {
    const btn = $("wi-apply");
    btn.disabled = true;
    try {
      const res = await MT.api("/api/whatif/apply", { method: "POST", body: JSON.stringify({
        scenarios: lastResult._scenarios, horizon_stops: 12, duration_min: +$("wi-dur").value, inject_into_stream: $("wi-inject").checked }) });
      btn.textContent = `Применено до ${res.until}${res.stream_injection.length ? " · в потоке" : ""}`;
      list = []; renderList(); renderLive();
    } catch (e) { btn.textContent = `Ошибка: ${e.message}`; btn.disabled = false; }
  }

  function renderLive() {
    const live = (MT.snap && MT.snap.live_scenarios) || [];
    const el = $("wi-live");
    if (!el) return;
    el.innerHTML = live.length ? `<div class="section-title">Применённые сценарии</div>
      ${live.map((x) => `<div class="sc-chip">${MT.esc(x.title)} · до ${x.until}</div>`).join(" ")}
      <div class="card-actions" style="margin-top:6px"><button class="btn danger" id="wi-live-clear">Снять все</button></div>` : "";
    const b = $("wi-live-clear");
    if (b) b.onclick = async () => { await MT.api("/api/whatif/live", { method: "DELETE" }).catch(() => {}); overlay().clearLayers(); setTimeout(renderLive, 1200); };
  }

  function drawShapes(shapes) {
    const lay = overlay();
    lay.clearLayers();
    const col = { red: MT.cssVar("--critical"), yellow: MT.cssVar("--warning"), good: MT.cssVar("--good"), closed: "#111" };
    const bounds = [];
    shapes.forEach((s) => {
      if (s.coords && s.coords.length > 1) {
        bounds.push(...s.coords);
        if (s.kind === "detour") {
          L.polyline(s.coords, { color: MT.cssVar("--route-casing"), weight: 10, opacity: 0.9 }).addTo(lay);
          L.polyline(s.coords, { color: MT.cssVar("--series-1"), weight: 6, dashArray: "10 6" }).bindTooltip(MT.esc(s.label), { sticky: true }).addTo(lay);
        } else {
          L.polyline(s.coords, { color: col[s.severity] || col.red, weight: s.severity === "closed" ? 8 : 7, opacity: 0.9, dashArray: s.severity === "closed" ? "2 8" : null })
            .bindTooltip(MT.esc(s.label), { sticky: true }).addTo(lay);
        }
      } else if (s.lat) {
        bounds.push([s.lat, s.lon]);
        const sym = s.kind === "reserve" ? "R" : s.kind === "stop" ? "×" : "!";
        L.marker([s.lat, s.lon], { icon: L.divIcon({ className: "", iconSize: null,
          html: `<span class="route-label" style="background:${s.kind === "reserve" ? MT.cssVar("--good") : MT.cssVar("--critical")}">${sym}</span>` }) })
          .bindTooltip(MT.esc(s.label)).addTo(lay);
      }
    });
    if (bounds.length) MT.map.fitBounds(bounds, { padding: [60, 60], maxZoom: 15 });
  }

  MT.on("whatif-prefill", (p) => { renderForm(p); });
  MT.on("tab", (t) => { if (t === "whatif" && !body.innerHTML) renderForm(); });
  MT.on("snapshot", () => { if (!$("whatif-body").classList.contains("hidden")) renderLive(); });
})();
