/* Интерактивный таймлайн: история состояний (кадры Backend) и прогноз положения ТС (ETA-движок).
   Полоса показывает число ТС с высоким риском / вниманием по времени; перетаскивание — просмотр карты
   на выбранный момент; «LIVE» — возврат к живой картине. */
(() => {
  "use strict";
  const MT = window.MT;
  const $ = (id) => document.getElementById(id);
  let data = null;
  let frames = [];          // [{T, t, v, future}]
  let scrubT = null;
  const track = $("tl-track"), svg = $("tl-svg"), cursor = $("tl-cursor");

  async function refresh() {
    if (MT.scrubbing) return;           // во время просмотра не сдвигаем шкалу
    try { data = await MT.api("/api/timeline?past_min=120&future_min=30&step_s=60"); } catch (e) { return; }
    frames = [...data.past, ...data.future];
    draw();
  }

  function range() {
    if (!frames.length) return [0, 1];
    return [frames[0].T, frames[frames.length - 1].T];
  }

  function draw() {
    if (!frames.length) { svg.innerHTML = ""; $("tl-info").textContent = "Накапливается история…"; return; }
    const W = track.clientWidth, H = track.clientHeight;
    const [t0, t1] = range();
    const x = (T) => ((T - t0) / Math.max(1, t1 - t0)) * W;
    const maxN = Math.max(3, ...frames.map((f) => f.v.filter((v) => v[3] === "red" || v[3] === "yellow").length));
    const bw = Math.max(2, W / frames.length - 1);
    let bars = "";
    frames.forEach((f) => {
      const red = f.v.filter((v) => v[3] === "red").length;
      const yel = f.v.filter((v) => v[3] === "yellow").length;
      const hr = (red / maxN) * (H - 14), hy = (yel / maxN) * (H - 14);
      const op = f.future ? 0.45 : 0.95;
      const xx = x(f.T) - bw / 2;
      if (hy) bars += `<rect x="${xx}" y="${H - 12 - hy - hr}" width="${bw}" height="${hy}" fill="var(--warning)" opacity="${op}"/>`;
      if (hr) bars += `<rect x="${xx}" y="${H - 12 - hr}" width="${bw}" height="${hr}" fill="var(--critical)" opacity="${op}"/>`;
      if (f.mode && f.mode !== "online") bars += `<rect x="${xx}" y="${H - 12}" width="${bw}" height="3" fill="var(--muted)"/>`;
    });
    const nowX = x(data.now_T);
    let ticks = "";
    const step = 1800;
    for (let T = Math.ceil(t0 / step) * step; T <= t1; T += step) {
      const d = new Date((T + 3 * 3600) * 1000);
      const lbl = `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
      ticks += `<line x1="${x(T)}" x2="${x(T)}" y1="${H - 12}" y2="${H - 8}" stroke="var(--axis)"/><text x="${x(T)}" y="${H - 1}" font-size="9" text-anchor="middle" fill="var(--muted)">${lbl}</text>`;
    }
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.innerHTML = `<rect x="${nowX}" y="0" width="${Math.max(0, W - nowX)}" height="${H - 12}" fill="var(--surface-2)"/>
      <line x1="0" x2="${W}" y1="${H - 12}" y2="${H - 12}" stroke="var(--axis)"/>
      ${bars}${ticks}
      <line x1="${nowX}" x2="${nowX}" y1="0" y2="${H - 12}" stroke="var(--critical)" stroke-width="2"/>
      <text x="${nowX + 4}" y="10" font-size="10" font-weight="700" fill="var(--critical-ink)">сейчас</text>
      <text x="${W - 4}" y="10" font-size="10" text-anchor="end" fill="var(--muted)">прогноз →</text>
      <text x="4" y="10" font-size="10" fill="var(--muted)">← история</text>`;
    if (!MT.scrubbing) {
      $("tl-info").textContent = `История ${Math.round((data.now_T - frames[0].T) / 60)} мин · прогноз ${data.future.length ? Math.round((frames[frames.length - 1].T - data.now_T) / 60) : 0} мин · перетащите для просмотра`;
    }
  }

  function scrubAt(clientX) {
    if (!frames.length) return;
    const r = track.getBoundingClientRect();
    const [t0, t1] = range();
    const T = t0 + ((clientX - r.left) / r.width) * (t1 - t0);
    let best = frames[0];
    frames.forEach((f) => { if (Math.abs(f.T - T) < Math.abs(best.T - T)) best = f; });
    scrubT = best.T;
    MT.scrubbing = true;
    MT.renderFrame(best);
    cursor.style.display = "block";
    cursor.style.left = `${((best.T - t0) / Math.max(1, t1 - t0)) * r.width}px`;
    const dt = Math.round((best.T - data.now_T) / 60);
    const txt = best.future ? `Прогноз на ${best.t} (через ${dt} мин) — положение по ETA` : `История: ${best.t} (${-dt} мин назад)`;
    $("scrub-banner").textContent = txt + " · нажмите LIVE для возврата";
    $("scrub-banner").classList.remove("hidden");
    const red = best.v.filter((v) => v[3] === "red").length, yel = best.v.filter((v) => v[3] === "yellow").length;
    $("tl-info").textContent = `${best.t}: высокий риск ${red}, внимание ${yel}`;
    $("tl-live").classList.remove("active");
  }

  function goLive() {
    MT.scrubbing = false;
    scrubT = null;
    cursor.style.display = "none";
    $("scrub-banner").classList.add("hidden");
    $("tl-live").classList.add("active");
    MT.renderLive();
    refresh();
  }

  let dragging = false;
  track.addEventListener("mousedown", (e) => { dragging = true; scrubAt(e.clientX); });
  window.addEventListener("mousemove", (e) => { if (dragging) scrubAt(e.clientX); });
  window.addEventListener("mouseup", () => { dragging = false; });
  track.addEventListener("touchstart", (e) => scrubAt(e.touches[0].clientX), { passive: true });
  track.addEventListener("touchmove", (e) => scrubAt(e.touches[0].clientX), { passive: true });
  $("tl-live").addEventListener("click", goLive);
  document.addEventListener("keydown", (e) => {
    if (!MT.scrubbing || !["ArrowLeft", "ArrowRight"].includes(e.key) || e.target.tagName === "INPUT") return;
    const i = frames.findIndex((f) => f.T === scrubT);
    const j = Math.max(0, Math.min(frames.length - 1, i + (e.key === "ArrowRight" ? 1 : -1)));
    const r = track.getBoundingClientRect();
    const [t0, t1] = range();
    scrubAt(r.left + ((frames[j].T - t0) / (t1 - t0)) * r.width);
    e.preventDefault();
  });
  window.addEventListener("resize", draw);
  $("tl-live").classList.add("active");
  setInterval(refresh, 10000);
  setTimeout(refresh, 3000);
})();
