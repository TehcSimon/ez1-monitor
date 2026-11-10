/* EZ1 Monitor — temperature graph in the top bar: the inverter's temperature
   over the last hours as a running graph, refreshed every minute, and with
   WEATHER_ENABLED the outside temperature as a dashed line. The inverter's
   line turns light blue when it is near freezing and red when it is hot.

   APsystems publishes no derating point for the EZ1: the datasheet allows
   -40 to +65 °C around it, and its body, the heat sink, may reach 80 °C. */
(() => {
  "use strict";

  const $ = id => document.getElementById(id);
  const box = $("temp-spark");
  if (!box) return;
  const canvas = $("temp-spark-canvas");
  const ctx = canvas.getContext("2d");

  const COLD_C = 3, HOT_C = 60;
  const REFRESH_MS = 60000;
  const GAP_S = 300;          // a longer pause in the readings breaks the line
  const MIN_SPAN_C = 8;       // keeps a calm hour from looking like a storm
  const SCALE_W = 22;         // left column for the scale
  const STEPS_C = [2, 5, 10, 20];

  let data = null, lang = "en", locale = "en", timer = null;
  const tr = (key, vars) => window.i18n.t(lang, key, vars);
  const degrees = v => `${v.toLocaleString(locale, { maximumFractionDigits: 1 })} °C`;

  function tokens() {
    const css = getComputedStyle(document.documentElement);
    const read = name => css.getPropertyValue(name).trim();
    return { normal: read("--text-secondary"), cold: read("--cold"), hot: read("--bad"), outside: read("--text-muted"), line: read("--border") };
  }
  const tone = (t, v) => (v <= COLD_C ? t.cold : v >= HOT_C ? t.hot : t.normal);
  const alpha = (hex, a) => `${hex}${Math.round(a * 255).toString(16).padStart(2, "0")}`;

  function draw() {
    const inverter = data ? data.inverter : [], outside = (data && data.outside) || [];
    box.hidden = !inverter.length && !outside.length;
    if (box.hidden) return;
    const t = tokens();
    const latest = inverter.length ? inverter[inverter.length - 1] : null;
    // A last reading older than the gap means the inverter has gone to sleep.
    const awake = latest && data.now - latest[0] <= GAP_S;
    const inverterLabel = $("temp-spark-inverter");
    inverterLabel.textContent = awake ? tr("tempSpark.inverter", { value: degrees(latest[1]) }) : tr("tempSpark.asleep");
    inverterLabel.style.color = awake ? tone(t, latest[1]) : t.outside;
    const outsideNow = outside.length ? valueAt(outside, data.now) : null;
    $("temp-spark-outside").textContent = outsideNow === null ? "" : tr("tempSpark.outside", { value: degrees(Math.round(outsideNow)) });
    canvas.setAttribute("aria-label", tr("tempSpark.label", { hours: data.window_s / 3600 }));
    box.title = tr("tempSpark.label", { hours: data.window_s / 3600 });

    const r = canvas.getBoundingClientRect(), dpr = Math.min(devicePixelRatio || 1, 2);
    const w = r.width, h = r.height;
    if (!w || !h) return;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const start = data.now - data.window_s;
    // Only what is visible counts for the range: the outside forecast reaches
    // an hour beyond both ends, so its edges are read off the line.
    const values = [...inverter, ...outside.filter(p => p[0] >= start && p[0] <= data.now)].map(p => p[1]);
    if (outside.length) values.push(valueAt(outside, start), valueAt(outside, data.now));
    let lo = Math.min(...values), hi = Math.max(...values);
    if (hi - lo < MIN_SPAN_C) { const mid = (hi + lo) / 2; lo = mid - MIN_SPAN_C / 2; hi = mid + MIN_SPAN_C / 2; }
    const plot = w - SCALE_W;
    const X = ts => SCALE_W + (ts - start) / data.window_s * plot;
    const Y = v => 5 + (hi - v) / (hi - lo) * (h - 10);

    ctx.fillStyle = t.line;
    for (const x of [plot / 3, plot * 2 / 3]) ctx.fillRect(Math.round(SCALE_W + x), 0, 1, h);

    // The scale: up to three round values with a dotted line each. Where the
    // lines sit closer than a label is tall, only every other one is labelled.
    const fits = s => s * (h - 10) / (hi - lo) >= 6 && Math.floor(hi / s) - Math.ceil(lo / s) + 1 <= 3;
    const step = STEPS_C.find(fits) || 50;
    const every = step * (h - 10) / (hi - lo) < 11 ? 2 : 1;
    ctx.font = `9px ${getComputedStyle(box).fontFamily}`;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let v = Math.ceil(lo / step) * step, i = 0; v <= hi; v += step, i++) {
      const y = Math.round(Y(v)) + 0.5;
      ctx.fillStyle = t.outside;
      if (i % every === 0) ctx.fillText(`${v}°`.replace("-", "\u2212"), SCALE_W - 4, y);
      ctx.fillStyle = t.line;
      for (let x = SCALE_W; x < w; x += 4) ctx.fillRect(x, y - 0.5, 2, 1);
    }

    if (outside.length > 1) {
      ctx.save();
      ctx.beginPath(); ctx.rect(SCALE_W, 0, plot, h); ctx.clip();
      ctx.beginPath();
      outside.forEach((p, i) => (i ? ctx.lineTo(X(p[0]), Y(p[1])) : ctx.moveTo(X(p[0]), Y(p[1]))));
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = t.outside;
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.restore();
    }

    // The colour follows the height: a vertical gradient with hard steps at
    // the two thresholds colours line and area alike.
    const stops = (a) => {
      const g = ctx.createLinearGradient(0, Y(hi), 0, Y(lo));
      const at = v => Math.min(1, Math.max(0, (hi - v) / (hi - lo)));
      g.addColorStop(0, alpha(hi >= HOT_C ? t.hot : t.normal, a));
      g.addColorStop(at(HOT_C), alpha(hi >= HOT_C ? t.hot : t.normal, a));
      g.addColorStop(at(HOT_C), alpha(t.normal, a));
      g.addColorStop(at(COLD_C), alpha(t.normal, a));
      g.addColorStop(at(COLD_C), alpha(lo <= COLD_C ? t.cold : t.normal, a));
      g.addColorStop(1, alpha(lo <= COLD_C ? t.cold : t.normal, a));
      return g;
    };
    const runs = [];
    for (const p of inverter) {
      if (!runs.length || p[0] - runs[runs.length - 1].at(-1)[0] > GAP_S) runs.push([]);
      runs[runs.length - 1].push(p);
    }
    for (const run of runs) {
      ctx.beginPath();
      run.forEach((p, i) => (i ? ctx.lineTo(X(p[0]), Y(p[1])) : ctx.moveTo(X(p[0]), Y(p[1]))));
      ctx.lineTo(X(run.at(-1)[0]), h);
      ctx.lineTo(X(run[0][0]), h);
      ctx.closePath();
      ctx.fillStyle = stops(0.18);
      ctx.fill();
      ctx.beginPath();
      run.forEach((p, i) => (i ? ctx.lineTo(X(p[0]), Y(p[1])) : ctx.moveTo(X(p[0]), Y(p[1]))));
      ctx.strokeStyle = stops(1);
      ctx.lineWidth = 1.4;
      ctx.lineJoin = "round";
      ctx.stroke();
    }
  }

  function valueAt(points, ts) {
    if (ts <= points[0][0]) return points[0][1];
    for (let i = 1; i < points.length; i++) {
      const [ta, va] = points[i - 1], [tb, vb] = points[i];
      if (ts <= tb) return va + (vb - va) * (ts - ta) / (tb - ta);
    }
    return points[points.length - 1][1];
  }

  async function refresh() {
    try {
      data = await (await fetch("/api/temperatures")).json();
      draw();
    } catch (e) {
      console.error("TempSpark.refresh:", e);
    }
  }

  // Called with every live update; fetches on its own minute.
  function load(nextLang, nextLocale) {
    const changed = nextLang !== lang;
    lang = nextLang;
    locale = nextLocale;
    if (timer === null) {
      timer = setInterval(refresh, REFRESH_MS);
      refresh();
    } else if (changed) draw();
  }

  new ResizeObserver(() => data && draw()).observe(canvas);
  matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => data && draw());
  new MutationObserver(() => data && draw()).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  window.TempSpark = { load };
})();
