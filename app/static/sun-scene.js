/* EZ1 Monitor — sun scene: a rotatable model of the day's sun path and yield
   around a reduced map of the house, plus target vs. actual, today's
   projection and, with WEATHER_ENABLED, the forecast. Follows the day shown
   in the day chart.

   Plain Canvas 2D, orthographic. World axes: x = east, y = north, z = up, in
   scene units (the dial has radius 10). Without SITE_ADDRESS there is no sky
   and no map: only the figures and the timeline remain. */
(() => {
  "use strict";

  const $ = id => document.getElementById(id);
  const card = $("sun-card");
  if (!card) return;

  const RAD = Math.PI / 180, DEG = 180 / Math.PI, TAU = Math.PI * 2;
  const BINS = 288, BIN_MINUTES = 5;
  // Bars and timeline stay linear but scale to the day: a fixed 800 W left
  // winter days nearly invisible. The expected peak is part of the scale, so a
  // dull day still looks small next to what could have been. The floor keeps
  // a few stray watts from filling the whole height.
  const MIN_SCALE_W = 150;
  const MAP_R = 7.6, DIAL_R = 10, CROWN_R = 8.95, CROWN_H = 3.2, BLADE_HALF = 1.15 * RAD, SKY_R = 12.6;
  const FLATTEN = 0.8;            // keeps the map calm; shadows still use the real height
  const SOLAR_HORIZON = -0.83;    // sunrise and sunset, degrees, with refraction
  const TARGET_MIN_W = 20;        // below this, actual / target says little
  const SLEEP_BINS = 3;           // slots without data until the inverter counts as asleep
  const NIGHT_END_MINUTE = 210;   // 03:30 on the clock: "night" turns into "before sunrise"
  // Behind the house as seen from the sun: from the sun's side, at this
  // elevation, its path would be edge-on. North here, south below the equator.
  const YAW_NORTH = 160 * RAD, YAW_SOUTH = -20 * RAD;
  const DEFAULT_PITCH = 42 * RAD, MIN_PITCH = 14 * RAD, MAX_PITCH = 72 * RAD;
  const PLAY_DURATION_MS = 26000;
  // WMO weather codes, each group by its highest code.
  const WEATHER_GROUPS = [
    [0, "clear"], [1, "mostlyClear"], [2, "partlyCloudy"], [3, "overcast"], [48, "fog"], [57, "drizzle"],
    [67, "rain"], [77, "snow"], [82, "showers"], [86, "snowShowers"], [99, "thunderstorm"],
  ];
  const REDUCED_MOTION = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const MONO = '"JetBrains Mono", ui-monospace, Menlo, monospace';
  const BODY = '"Inter", system-ui, sans-serif';

  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const mix = (a, b, t) => a + (b - a) * t;
  const smooth = (a, b, v) => { const t = clamp((v - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const scaled = (a, k) => [a[0] * k, a[1] * k, a[2] * k];
  const signedArea = pts => pts.reduce((s, a, i) => { const b = pts[(i + 1) % pts.length]; return s + a[0] * b[1] - b[0] * a[1]; }, 0) / 2;

  let lang = "en", locale = "en";
  const tr = (key, vars) => window.i18n.t(lang, key, vars);
  const num = (v, digits = 2) => v.toLocaleString(locale, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  const clock = ms => new Date(ms).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" });

  // ---- Theme: the canvas reads the same tokens as the page ----
  const T = {};
  const TOKENS = {
    card2: "--bg-card-2", line: "--border", line2: "--border-bright", ink: "--text-primary",
    ink2: "--text-secondary", muted: "--text-muted", accent: "--accent", pv1: "--pv1", pv2: "--pv2",
    sun: "--sun", dial: "--scene-dial", ring: "--scene-ring", ground: "--scene-ground",
    groundNight: "--scene-ground-night", road: "--scene-road", roof: "--scene-roof", home: "--scene-home",
    wallLit: "--scene-wall-lit", wallDark: "--scene-wall-dark", edge: "--scene-edge", label: "--scene-label",
  };
  function readTokens() {
    const css = getComputedStyle(document.documentElement);
    for (const [name, property] of Object.entries(TOKENS)) {
      const hex = css.getPropertyValue(property).trim();
      T[name] = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16));
    }
    T.shadeAlpha = parseFloat(css.getPropertyValue("--scene-shade-alpha"));
  }
  const color = (c, a = 1) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
  const blend = (a, b, t) => [mix(a[0], b[0], t), mix(a[1], b[1], t), mix(a[2], b[2], t)].map(Math.round);

  // ---- Sun: low-precision formula from the Astronomical Almanac ----
  function sunAt(ms) {
    const d = ms / 86400000 + 2440587.5 - 2451545;
    const g = (357.529 + 0.98560028 * d) * RAD;
    const q = 280.459 + 0.98564736 * d;
    const lambda = (q + 1.915 * Math.sin(g) + 0.02 * Math.sin(2 * g)) * RAD;
    const e = (23.439 - 0.00000036 * d) * RAD;
    const ra = Math.atan2(Math.cos(e) * Math.sin(lambda), Math.cos(lambda));
    const dec = Math.asin(Math.sin(e) * Math.sin(lambda));
    const h = (18.697374558 + 24.06570982441908 * d) * 15 * RAD + site.lon * RAD - ra;
    const lat = site.lat * RAD;
    const alt = Math.asin(Math.sin(lat) * Math.sin(dec) + Math.cos(lat) * Math.cos(dec) * Math.cos(h));
    const az = Math.atan2(-Math.sin(h) * Math.cos(dec), Math.cos(lat) * Math.sin(dec) - Math.sin(lat) * Math.cos(dec) * Math.cos(h));
    return { alt, az, dir: [Math.sin(az) * Math.cos(alt), Math.cos(az) * Math.cos(alt), Math.sin(alt)] };
  }

  // ---- Data ----
  let site = null;                // /api/site once ready, else null
  let solstices = { year: null, arcs: [] };
  let buildings = [], roads = [], streetNames = [], metre = 1;
  const state = { day: null, minute: 0, playing: false, follow: true };

  function applySite(payload) {
    site = payload.ready ? payload : null;
    solstices = { year: null, arcs: [] };
    if (!site) return;
    metre = MAP_R / site.radius;
    buildings = site.buildings.map(raw => {
      const f = raw.f.map(p => [p[0] * metre, p[1] * metre]);
      if (signedArea(f) < 0) f.reverse();
      const walls = f.map((a, i) => {
        const b = f[(i + 1) % f.length], len = Math.hypot(b[0] - a[0], b[1] - a[1]);
        return { a, b, len, mx: (a[0] + b[0]) / 2, my: (a[1] + b[1]) / 2, n: [(b[1] - a[1]) / (len || 1), -(b[0] - a[0]) / (len || 1), 0] };
      }).filter(w => w.len > 1e-3);
      const cx = f.reduce((s, p) => s + p[0], 0) / f.length, cy = f.reduce((s, p) => s + p[1], 0) / f.length;
      return { f, h: raw.h * metre * FLATTEN, castH: raw.h * metre, walls, cx, cy, home: !!raw.home, fade: smooth(MAP_R * 0.4, MAP_R, Math.hypot(cx, cy)) };
    });
    roads = site.roads.map(r => ({ w: r.w * metre, p: r.p.map(q => [q[0] * metre, q[1] * metre]) }));
    streetNames = site.labels.map(l => ({ text: l.t, path: l.p.map(q => [q[0] * metre, q[1] * metre]) }));
    cam.yaw = site.lat >= 0 ? YAW_NORTH : YAW_SOUTH;
  }

  function solsticeArcs(year) {
    if (solstices.year === year) return solstices.arcs;
    // Each sweep starts at local solar midnight, so the arc comes out in one piece.
    const start = day => Date.UTC(year, day[0], day[1]) - site.lon / 15 * 3600000;
    const arcs = [[[5, 21], tr("sun.solsticeJune")], [[11, 21], tr("sun.solsticeDecember")]].map(([day, label]) => {
      const t0 = start(day), pts = [];
      for (let m = 0; m <= 1440; m += 6) {
        const s = sunAt(t0 + m * 60000);
        if (s.dir[2] > 0) pts.push(scaled(s.dir, SKY_R));
      }
      return { pts, label, apex: pts[Math.floor(pts.length / 2)] };
    }).filter(arc => arc.pts.length > 1);
    solstices = { year, arcs };
    return arcs;
  }

  const valueAt = (a, x) => { const i = Math.floor(x); return mix(a[i], a[Math.min(i + 1, BINS - 1)], x - i); };
  function sample(d, m) {
    const x = clamp(m / BIN_MINUTES - 0.5, 0, BINS - 1.001);
    // A today still in progress has no power after its last slot with data:
    // the newest value holds for a moment, then the inverter has gone to sleep.
    const seen = Math.min(x, d.lastBin), awake = x <= d.lastBin + SLEEP_BINS;
    return {
      p1: awake ? valueAt(d.p1, seen) : 0, p2: awake ? valueAt(d.p2, seen) : 0, clear: valueAt(d.clear, seen),
      target: valueAt(d.target, x), forecast: d.forecast ? valueAt(d.forecast, x) : 0,
    };
  }
  const energy = (sum, m) => { const x = clamp(m / BIN_MINUTES, 0, BINS - 0.001), i = Math.floor(x); return mix(sum[i], sum[i + 1], x - i); };

  function nowMinute(d) { return (Date.now() - d.t0) / 60000; }

  function sunWindow(d) {
    let rise = null, set = null;
    for (let m = 1; m <= 1440; m++) {
      const a = sunAt(d.t0 + (m - 1) * 60000).alt * DEG, b = sunAt(d.t0 + m * 60000).alt * DEG;
      if (a < SOLAR_HORIZON && b >= SOLAR_HORIZON) rise = m;
      if (a >= SOLAR_HORIZON && b < SOLAR_HORIZON) set = m;
    }
    if (rise === null && set === null) {
      // Polar day or night
      const up = sunAt(d.t0 + 720 * 60000).alt > 0;
      return up ? { rise: 0, set: 1440 } : { rise: 720, set: 720 };
    }
    rise = rise ?? 0;
    set = set === null || set <= rise ? 1440 : set;
    return { rise, set };
  }

  function dataWindow(d) {
    const producing = b => d.act[b] > 5 || d.target[b] > 5;
    let first = 0, last = BINS - 1;
    while (first < BINS - 1 && !producing(first)) first++;
    while (last > 0 && !producing(last)) last--;
    return { rise: first * BIN_MINUTES, set: (last + 1) * BIN_MINUTES };
  }

  function addSky(d) {
    d.arc = [];
    for (let m = d.rise; m <= d.set; m += 4) {
      const s = sunAt(d.t0 + m * 60000);
      if (s.dir[2] > -0.015) d.arc.push({ m, p: scaled(s.dir, SKY_R) });
    }
    d.hours = [];
    for (let m = Math.ceil(d.rise) + 15; m < d.set - 15; m++) {
      const at = new Date(d.t0 + m * 60000);
      if (at.getMinutes() === 0 && at.getHours() % 2 === 0) {
        d.hours.push({ h: at.getHours(), m, p: scaled(sunAt(d.t0 + m * 60000).dir, SKY_R) });
      }
    }
    d.crown = [];
    d.azRise = d.azSet = null;
    if (d.set <= d.rise) return;
    // Unwrapped, because below the equator the azimuth runs backwards and through north.
    const table = [];
    for (let m = d.rise; m <= d.set; m += 2) {
      let az = sunAt(d.t0 + m * 60000).az * DEG;
      if (table.length) {
        const previous = table[table.length - 1][1];
        while (az - previous > 180) az -= 360;
        while (az - previous < -180) az += 360;
      }
      table.push([m, az]);
    }
    const first = table[0][1], end = table[table.length - 1][1], way = end >= first ? 1 : -1;
    d.azRise = first * RAD;
    d.azSet = end * RAD;
    for (let k = 1.5; k < Math.abs(end - first); k += 3) {
      const az = first + way * k;
      const i = table.findIndex(row => way * (row[1] - az) >= 0);
      if (i < 1) continue;
      const [ma, aa] = table[i - 1], [mb, ab] = table[i];
      const m = mix(ma, mb, (az - aa) / ((ab - aa) || 1));
      const v = sample(d, m);
      d.crown.push({ a: az * RAD, m, p1: v.p1, p2: v.p2, target: v.target, forecast: v.forecast });
    }
  }

  function prepare(raw) {
    const series = values => Float64Array.from({ length: BINS }, (_, b) => (values ? values[b] || 0 : 0));
    const p1 = series(raw.p1), p2 = series(raw.p2), target = series(raw.target);
    const forecast = raw.forecast ? series(raw.forecast) : null;
    const act = p1.map((v, b) => v + p2[b]);
    let lastBin = BINS - 1;
    if (raw.is_today) while (lastBin > 0 && act[lastBin] <= 0) lastBin--;

    const sumA = new Float64Array(BINS + 1), sumT = new Float64Array(BINS + 1);
    for (let b = 0; b < BINS; b++) {
      sumA[b + 1] = sumA[b] + act[b] / 12;
      sumT[b + 1] = sumT[b] + target[b] / 12;
    }
    // The day's total comes from the inverter's own counter, like the figures
    // above the day chart, so both show the same number. The power curve only
    // distributes it over the day.
    if (raw.kwh > 0 && sumA[BINS] > 0) {
      const k = raw.kwh * 1000 / sumA[BINS];
      for (let b = 0; b <= BINS; b++) sumA[b] *= k;
    }

    const ratio = Array.from(act, (v, b) => (target[b] > TARGET_MIN_W ? clamp(v / target[b], 0, 1.05) : null));
    let held = ratio.find(v => v !== null) ?? 1;
    for (let b = 0; b < BINS; b++) { if (ratio[b] === null) ratio[b] = held; else held = ratio[b]; }
    const peak = Math.max(...target) || 1;
    const clear = new Float64Array(BINS);
    for (let b = 0; b < BINS; b++) {
      const near = ratio.slice(Math.max(0, b - 3), b + 4);
      // Near sunrise and sunset the ratio says little about the sky.
      clear[b] = raw.target ? mix(1, near.reduce((s, v) => s + v, 0) / near.length, smooth(0.1, 0.3, target[b] / peak)) : 1;
    }

    const d = {
      ...raw, t0: raw.t0 * 1000, p1, p2, target, forecast, act, sumA, sumT, clear, lastBin,
      totalT: sumT[BINS], hasTarget: !!raw.target, hasData: sumA[BINS] > 0,
      scaleW: Math.max(MIN_SCALE_W, ...act, ...target, ...(forecast || [])),
      arc: [], hours: [], crown: [], azRise: null, azSet: null,
    };
    const span = site ? sunWindow(d) : dataWindow(d);
    d.rise = span.rise;
    d.set = span.set;
    const before = site ? 100 : 45, after = site ? 130 : 45;
    d.m0 = clamp(d.rise - before, 0, 1440);
    d.m1 = clamp(d.set + after, 0, 1440);
    if (site) addSky(d);
    return d;
  }

  // ---- Camera ----
  const view = $("sun-view");
  const canvas = $("sun-canvas");
  const pulse = $("sun-pulse");
  const ctx = canvas.getContext("2d");
  let W = 0, H = 0, S = 1, CX = 0, CY = 0;
  let cosY = 1, sinY = 0, cosP = 1, sinP = 0, V = [0, 0, 1];
  const cam = { yaw: YAW_NORTH, pitch: DEFAULT_PITCH, spin: 0, goal: null };
  function setCamera() {
    cosY = Math.cos(cam.yaw); sinY = Math.sin(cam.yaw);
    cosP = Math.cos(cam.pitch); sinP = Math.sin(cam.pitch);
    V = [-sinY * cosP, -cosY * cosP, sinP];
  }
  function P(x, y, z) {
    const xr = x * cosY - y * sinY, yr = x * sinY + y * cosY;
    return [CX + S * xr, CY - S * (z * cosP + yr * sinP), yr * cosP - z * sinP];
  }
  // Draws in ground coordinates, so street widths stay true in every direction.
  function onGround(draw) {
    ctx.save();
    ctx.transform(S * cosY, -S * sinP * sinY, -S * sinY, -S * sinP * cosY, CX, CY);
    draw();
    ctx.restore();
  }

  // ---- Light ----
  let SUN = [0, 0, 1], L = { e: 0, day: 0, clear: 1, shadow: 0 };
  function updateLight(sun, clearValue) {
    SUN = sun.dir;
    const e = sun.alt * DEG, clear = clamp(clearValue / 0.92, 0, 1);
    L = { e, clear, day: smooth(-4, 7, e), shadow: smooth(0.5, 9, e) * mix(0.2, 1, clear) };
  }

  // ---- Drawing helpers ----
  function trace(pts) {
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
    ctx.closePath();
  }
  function fillPoly(pts, fill) {
    trace(pts);
    ctx.fillStyle = fill;
    ctx.fill();
    ctx.strokeStyle = fill;   // closes the hairline seams between neighbouring faces
    ctx.lineWidth = 0.6;
    ctx.stroke();
  }
  function line(a, b, style, width) {
    ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]);
    ctx.strokeStyle = style; ctx.lineWidth = width; ctx.stroke();
  }
  function ringPath(r) { ctx.beginPath(); ctx.ellipse(CX, CY, S * r, S * r * sinP, 0, 0, TAU); }
  // Always counter-clockwise, so overlapping shapes merge under the nonzero rule and never darken twice.
  function addPoly(pts) {
    (signedArea(pts) < 0 ? pts.slice().reverse() : pts).forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
    ctx.closePath();
  }

  // Lies flat on the street, letter by letter along its bends, and always reads left to right.
  function paintStreetNames() {
    const px = 20, height = Math.max(0.36, 8.5 / S), k = height / px;
    ctx.font = `500 ${px}px ${BODY}`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = color(T.label, 0.9);
    for (const street of streetNames) {
      const first = street.path[0], end = street.path[street.path.length - 1];
      const forward = (end[0] - first[0]) * cosY - (end[1] - first[1]) * sinY >= 0;
      const path = forward ? street.path : street.path.slice().reverse();
      const dist = [0];
      for (let i = 1; i < path.length; i++) dist.push(dist[i - 1] + Math.hypot(path[i][0] - path[i - 1][0], path[i][1] - path[i - 1][1]));
      const total = dist[dist.length - 1];
      if (S < 16 && total < 30 * metre) continue;   // short names would run into the long ones on a phone
      const at = s => {
        let i = 1;
        while (i < path.length - 1 && dist[i] < s) i++;
        const t = clamp((s - dist[i - 1]) / ((dist[i] - dist[i - 1]) || 1), 0, 1);
        return [mix(path[i - 1][0], path[i][0], t), mix(path[i - 1][1], path[i][1], t)];
      };
      const chars = [...street.text], widths = chars.map(c => (ctx.measureText(c).width + 1) * k);
      const width = widths.reduce((s, w) => s + w, 0);
      if (width > total * 0.95) continue;
      let s = (total - width) / 2;
      chars.forEach((c, i) => {
        const mid = s + widths[i] / 2, q = at(mid), before = at(mid - 0.5), after = at(mid + 0.5);
        ctx.save();
        ctx.translate(q[0], q[1]);
        ctx.rotate(Math.atan2(after[1] - before[1], after[0] - before[0]));
        ctx.scale(k, -k);
        ctx.fillText(c, 0, 0);
        ctx.restore();
        s += widths[i];
      });
    }
  }

  // ---- Layers ----
  function paintGround() {
    ringPath(DIAL_R);
    ctx.fillStyle = color(T.dial);
    ctx.fill();
    ctx.strokeStyle = color(T.ring);
    ctx.lineWidth = 1;
    ctx.stroke();

    onGround(() => {
      ctx.beginPath();
      ctx.arc(0, 0, MAP_R, 0, TAU);
      ctx.fillStyle = color(blend(T.groundNight, T.ground, L.day));
      ctx.fill();
      ctx.clip();

      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      ctx.strokeStyle = color(T.road);
      for (const r of roads) {
        ctx.beginPath();
        r.p.forEach((q, i) => (i ? ctx.lineTo(q[0], q[1]) : ctx.moveTo(q[0], q[1])));
        ctx.lineWidth = r.w;
        ctx.stroke();
      }

      if (L.shadow > 0.02) {
        const s = Math.max(SUN[2], 0.07), ox = -SUN[0] / s, oy = -SUN[1] / s;
        ctx.beginPath();
        for (const b of buildings) {
          const dx = ox * b.castH, dy = oy * b.castH;
          addPoly(b.f);
          addPoly(b.f.map(p => [p[0] + dx, p[1] + dy]));
          for (const w of b.walls) addPoly([w.a, w.b, [w.b[0] + dx, w.b[1] + dy], [w.a[0] + dx, w.a[1] + dy]]);
        }
        ctx.fillStyle = `rgba(0,0,0,${T.shadeAlpha * L.shadow})`;
        ctx.fill();
      }
      paintStreetNames();
    });

    ringPath(MAP_R);
    ctx.strokeStyle = color(T.ring);
    ctx.lineWidth = 1;
    ctx.stroke();

    ctx.beginPath();
    for (let a = 0; a < 360; a += 5) {
      const s = Math.sin(a * RAD), c = Math.cos(a * RAD), inner = a % 30 ? 9.7 : 9.45;
      const p = P(s * inner, c * inner, 0), q = P(s * 9.95, c * 9.95, 0);
      ctx.moveTo(p[0], p[1]); ctx.lineTo(q[0], q[1]);
    }
    ctx.strokeStyle = color(T.muted);
    ctx.lineWidth = 1;
    ctx.stroke();

    if (L.e > -1) {
      const az = Math.atan2(SUN[0], SUN[1]);
      line(P(Math.sin(az) * MAP_R, Math.cos(az) * MAP_R, 0), P(Math.sin(az) * CROWN_R, Math.cos(az) * CROWN_R, 0), color(T.sun, 0.75 * L.day), 1.4);
    }
  }

  function strokeSide(points, far, style, width, dash) {
    ctx.beginPath();
    let open = false;
    for (let i = 0; i < points.length - 1; i++) {
      const a = P(points[i][0], points[i][1], points[i][2]), b = P(points[i + 1][0], points[i + 1][1], points[i + 1][2]);
      if (((a[2] + b[2]) / 2 > 0) === far) {
        if (!open) ctx.moveTo(a[0], a[1]);
        ctx.lineTo(b[0], b[1]);
        open = true;
      } else open = false;
    }
    ctx.strokeStyle = style;
    ctx.lineWidth = width;
    ctx.lineCap = "round";
    ctx.setLineDash(dash || []);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  function paintArcs(far, d, m) {
    ctx.textAlign = "center";
    ctx.textBaseline = "alphabetic";
    ctx.font = `400 ${S < 16 ? 9 : 10.5}px ${MONO}`;
    for (const arc of solsticeArcs(+d.date.slice(0, 4))) {
      strokeSide(arc.pts, far, color(T.line2), 1);
      const q = P(arc.apex[0], arc.apex[1], arc.apex[2]);
      if ((q[2] > 0) === far && S >= 16) {
        ctx.fillStyle = color(T.muted);
        ctx.fillText(arc.label, q[0], q[1] - 7);
      }
    }
    const past = [], future = [];
    for (const q of d.arc) (q.m <= m ? past : future).push(q.p);
    if (SUN[2] > 0) { const at = scaled(SUN, SKY_R); past.push(at); future.unshift(at); }
    strokeSide(future, far, color(T.ink2, 0.7), 1.5, [1, 6]);
    strokeSide(past, far, color(T.sun, far ? 1 : 0.6), 2.6);
    for (const h of d.hours) {
      const q = P(h.p[0], h.p[1], h.p[2]);
      if ((q[2] > 0) !== far) continue;
      ctx.fillStyle = h.m <= m ? color(T.sun) : color(T.ink2);
      ctx.beginPath(); ctx.arc(q[0], q[1], 2.4, 0, TAU); ctx.fill();
      ctx.fillStyle = color(T.ink2);
      ctx.fillText(h.h, q[0], q[1] - 8);
    }
  }

  function paintSun() {
    if (L.e < -1) return;
    const q = P(SUN[0] * SKY_R, SUN[1] * SKY_R, SUN[2] * SKY_R);
    const az = Math.atan2(SUN[0], SUN[1]);
    const foot = P(Math.sin(az) * CROWN_R, Math.cos(az) * CROWN_R, 0);
    const visible = smooth(-1, 1.5, L.e), power = mix(0.5, 1, L.clear) * visible;
    ctx.setLineDash([2, 4]);
    line(q, foot, color(T.sun, 0.4 * visible), 1);
    ctx.setLineDash([]);
    const r = Math.max(5, S * 0.34);
    // The radiating rings are a CSS animation on top of the canvas (see
    // placePulse), so the scene needn't be redrawn for them.
    if (REDUCED_MOTION) {
      ctx.beginPath(); ctx.arc(q[0], q[1], r * 1.9, 0, TAU);
      ctx.strokeStyle = color(T.sun, 0.45 * power);
      ctx.lineWidth = 1;
      ctx.stroke();
    }
    ctx.beginPath(); ctx.arc(q[0], q[1], r, 0, TAU);
    ctx.fillStyle = color(T.sun, power);
    ctx.fill();
    ctx.font = `500 ${S < 16 ? 10 : 11.5}px ${MONO}`;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillStyle = color(T.ink);
    ctx.fillText(`${Math.round(L.e)}°`, q[0] + r * 2.4, q[1]);
  }

  function placePulse() {
    pulse.hidden = L.e < -1;
    if (pulse.hidden) return;
    const q = P(SUN[0] * SKY_R, SUN[1] * SKY_R, SUN[2] * SKY_R);
    pulse.style.transform = `translate(${q[0]}px, ${q[1]}px)`;
    pulse.style.setProperty("--sun-r", `${Math.max(5, S * 0.34)}px`);
    pulse.style.opacity = mix(0.5, 1, L.clear) * smooth(-1, 1.5, L.e);   // fainter behind clouds
  }

  function wallTone(n, fade) {
    const lit = blend(T.wallDark, T.wallLit, Math.max(0, dot(n, SUN)) * L.shadow);
    return color(blend(lit, T.ground, fade * 0.55));
  }
  function paintBuilding(b) {
    const items = [];
    for (const w of b.walls) {
      if (dot(w.n, V) <= 1e-3) continue;
      items.push({
        d: P(w.mx, w.my, 0)[2],
        draw: () => fillPoly([P(w.a[0], w.a[1], 0), P(w.b[0], w.b[1], 0), P(w.b[0], w.b[1], b.h), P(w.a[0], w.a[1], b.h)], wallTone(w.n, b.fade)),
      });
    }
    items.sort((x, y) => y.d - x.d).forEach(it => it.draw());
    const roof = b.f.map(p => P(p[0], p[1], b.h));
    const tone = b.home ? T.home : blend(blend(T.roof, T.groundNight, (1 - L.day) * 0.3), T.ground, b.fade * 0.55);
    fillPoly(roof, color(tone));
    trace(roof);
    ctx.strokeStyle = color(T.edge, b.home ? 0.9 : 0.55);
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  function paintBlade(b, m, d) {
    const k = CROWN_H / d.scaleW;
    // Beyond the present a bar can only show the forecast, if there is one.
    const ahead = d.is_today && b.m > nowMinute(d);
    const h1 = b.p2 * k, h2 = (ahead ? b.forecast : b.p1 + b.p2) * k, ht = b.target * k;
    const a0 = b.a - BLADE_HALF, a1 = b.a + BLADE_HALF;
    const x0 = CROWN_R * Math.sin(a0), y0 = CROWN_R * Math.cos(a0), x1 = CROWN_R * Math.sin(a1), y1 = CROWN_R * Math.cos(a1);
    const past = b.m <= m;
    const band = (z0, z1, style) => {
      if (z1 - z0 < 0.015) return;
      trace([P(x0, y0, z0), P(x1, y1, z0), P(x1, y1, z1), P(x0, y0, z1)]);
      ctx.fillStyle = style;
      ctx.fill();
    };
    if (past) { band(0, h1, color(T.pv2)); band(h1, h2, color(T.pv1)); } else band(0, h2, color(T.muted, 0.4));
    if (ht > 0.02) line(P(x0, y0, ht), P(x1, y1, ht), color(T.ink, past ? 0.8 : 0.4), 1.1);
    if (Math.abs(b.m - m) < 6 && L.e > 0 && h2 > 0.02) line(P(x0, y0, h2), P(x1, y1, h2), color(T.ink), 2.2);
  }

  function paintObjects(d, m) {
    const items = buildings.map(b => ({ d: P(b.cx, b.cy, 0)[2], draw: () => paintBuilding(b) }));
    for (const b of d.crown) items.push({ d: P(CROWN_R * Math.sin(b.a), CROWN_R * Math.cos(b.a), 0)[2], draw: () => paintBlade(b, m, d) });
    items.sort((a, b) => b.d - a.d);
    for (const it of items) it.draw();
  }

  function paintLabels(d) {
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.font = `500 ${S < 16 ? 10 : 12}px ${MONO}`;
    const R = DIAL_R + 0.75;
    [["north", 0], ["east", 90], ["south", 180], ["west", 270]].forEach(([key, a]) => {
      const q = P(R * Math.sin(a * RAD), R * Math.cos(a * RAD), 0);
      ctx.fillStyle = key === "north" ? color(T.accent) : color(T.ink2);
      ctx.fillText(tr(`sun.${key}`), q[0], q[1]);
    });
    if (d.azRise === null) return;
    ctx.font = `400 ${S < 16 ? 9 : 10.5}px ${MONO}`;
    ctx.fillStyle = color(T.ink2);
    for (const [m, a] of [[d.rise, d.azRise], [d.set, d.azSet]]) {
      const q = P((R + 0.5) * Math.sin(a), (R + 0.5) * Math.cos(a), 0);
      ctx.fillText(clock(d.t0 + m * 60000), q[0], q[1]);
    }
  }

  // ---- Figures ----
  const shown = {};
  function put(id, value) { if (shown[id] !== value) { shown[id] = value; $(id).textContent = value; } }
  const timeInput = $("sun-time");
  const curve = $("sun-curve");
  const tl = curve.getContext("2d");
  let timelineDirty = true;

  function updateEstimate(d) {
    const kwh = `${num(d.kwh)} kWh`;
    if (!d.is_today) {
      put("sun-estimate-label", tr("sun.dayTotal"));
      put("sun-estimate", kwh);
    } else if (d.projection_kwh !== null && !d.day_done) {
      put("sun-estimate-label", tr("sun.todayExpected"));
      put("sun-estimate", `${num(d.projection_kwh)} kWh`);
    } else {
      put("sun-estimate-label", tr(d.day_done ? "sun.today" : "sun.todaySoFar"));
      put("sun-estimate", kwh);
    }
    put("sun-tomorrow", d.tomorrow_kwh === null ? "" : tr("sun.tomorrow", { kwh: num(d.tomorrow_kwh) }));
  }

  function weatherText(d) {
    const group = d.weather && WEATHER_GROUPS.find(([highest]) => d.weather.code <= highest);
    return group ? `${tr(`sun.weather.${group[1]}`)} · ${Math.round(d.weather.temp_c)} °C` : "";
  }

  function belowHorizonText(ms) {
    const at = new Date(ms), minute = at.getHours() * 60 + at.getMinutes();
    if (minute < NIGHT_END_MINUTE) return tr("sun.night");
    return tr(minute < 720 ? "sun.beforeRise" : "sun.down");
  }

  function updateFigures(d, m, v) {
    // Following the present, the clock keeps running beyond what the timeline shows.
    const ms = d.is_today && state.follow ? Date.now() : d.t0 + m * 60000;
    const sun = site ? sunAt(ms) : null;
    const watts = v.p1 + v.p2;
    put("sun-clock", clock(ms));
    put("sun-date", new Date(`${d.date}T12:00:00Z`).toLocaleDateString(locale, { timeZone: "UTC", day: "numeric", month: "long", year: "numeric" }));
    put("sun-power", String(Math.round(watts)));
    put("sun-pv1", `${tr("hero.pv1")} ${Math.round(v.p1)} W`);
    put("sun-pv2", `${tr("hero.pv2")} ${Math.round(v.p2)} W`);
    put("sun-sky", sun && sun.alt * DEG < SOLAR_HORIZON ? tr("sun.night")
      : d.hasTarget && v.target > TARGET_MIN_W ? tr("sun.ofTarget", { pct: Math.round(watts / v.target * 100) }) : "");
    // The sky of right now, so it doesn't belong to a replayed morning.
    put("sun-weather", m >= maxMinute(d) - BIN_MINUTES ? weatherText(d) : "");

    const sofar = energy(d.sumA, m), planned = energy(d.sumT, m);
    put("sun-f-now", `${num(sofar / 1000)} kWh`);
    put("sun-f-now-n", d.is_today ? tr("sun.upTo", { time: clock(ms) }) : tr("sun.wholeDay", { kwh: num(d.kwh) }));
    if (d.hasTarget) {
      put("sun-f-target", `${num(planned / 1000)} kWh`);
      put("sun-f-target-n", planned > 40 ? tr("sun.actualIs", { pct: Math.round(sofar / planned * 100) }) : tr("sun.tooEarly"));
      put("sun-f-full", `${num(d.totalT / 1000)} kWh`);
      put("sun-f-full-n", tr("sun.learnedFrom", { n: d.learned_days }));
    } else {
      const learning = tr("sun.learning", { n: d.learned_days, needed: d.needed_days });
      put("sun-f-target", "–");
      put("sun-f-target-n", learning);
      put("sun-f-full", "–");
      put("sun-f-full-n", learning);
    }
    if (sun) {
      const e = sun.alt * DEG;
      put("sun-f-sun", e > SOLAR_HORIZON ? tr("sun.elevation", { deg: Math.round(e) }) : belowHorizonText(ms));
      const az = (sun.az * DEG + 360) % 360;
      const name = tr("sun.directions").split(",")[Math.round(az / 45) % 8];
      put("sun-f-sun-n", `${name} · ${clock(d.t0 + d.rise * 60000)} – ${clock(d.t0 + d.set * 60000)}`);
    }
  }

  function drawTimeline() {
    const d = state.day, m = state.minute;
    const r = curve.getBoundingClientRect();
    const w = r.width, h = r.height, dpr = Math.min(devicePixelRatio || 1, 2);
    if (!w || !h) return;
    if (curve.width !== Math.round(w * dpr) || curve.height !== Math.round(h * dpr)) {
      curve.width = Math.round(w * dpr);
      curve.height = Math.round(h * dpr);
    }
    tl.setTransform(dpr, 0, 0, dpr, 0, 0);
    tl.clearRect(0, 0, w, h);
    const base = h - 20, top = 8;
    const X = x => (x - d.m0) / ((d.m1 - d.m0) || 1) * w, Y = p => base - (base - top) * p / d.scaleW;

    if (site) {
      tl.fillStyle = color(T.card2);
      tl.fillRect(0, 0, X(d.rise), base);
      tl.fillRect(X(d.set), 0, w - X(d.set), base);
    }
    tl.fillStyle = color(T.line);
    tl.fillRect(0, base, w, 1);

    const points = (values, lastBin) => {
      const out = [];
      for (let b = Math.floor(d.m0 / BIN_MINUTES); b < Math.min(BINS, Math.ceil(d.m1 / BIN_MINUTES), lastBin + 1); b++) {
        out.push([X(b * BIN_MINUTES + 2.5), Y(values[b])]);
      }
      return out;
    };
    const under = pts => {
      tl.beginPath();
      tl.moveTo(pts[0][0], base);
      for (const p of pts) tl.lineTo(p[0], p[1]);
      tl.lineTo(pts[pts.length - 1][0], base);
      tl.closePath();
    };
    const area = points(d.act, d.lastBin);
    if (area.length) {
      under(area);
      tl.fillStyle = color(T.muted, 0.3);
      tl.fill();
      tl.save();
      tl.beginPath(); tl.rect(0, 0, X(m), h); tl.clip();
      under(area);
      tl.fillStyle = color(T.sun, 0.75);
      tl.fill();
      tl.restore();
    }
    if (d.is_today && d.forecast) {
      const present = X(nowMinute(d)), ahead = points(d.forecast, BINS).filter(p => p[0] >= present);
      if (ahead.length) {
        under(ahead);
        tl.fillStyle = color(T.sun, 0.25);
        tl.fill();
      }
    }

    if (d.hasTarget) {
      tl.beginPath();
      points(d.target, BINS).forEach((p, i) => (i ? tl.lineTo(p[0], p[1]) : tl.moveTo(p[0], p[1])));
      tl.setLineDash([3, 3]);
      tl.strokeStyle = color(T.ink, 0.7);
      tl.lineWidth = 1;
      tl.stroke();
      tl.setLineDash([]);
    }

    tl.font = `400 10px ${MONO}`;
    tl.textAlign = "center";
    tl.textBaseline = "alphabetic";
    tl.fillStyle = color(T.ink2);
    const step = w < 520 ? 4 : 2;
    for (let minute = Math.ceil(d.m0); minute <= d.m1; minute++) {
      const at = new Date(d.t0 + minute * 60000);
      if (at.getMinutes() !== 0 || at.getHours() % step !== 0) continue;
      const x = X(minute);
      if (x > 14 && x < w - 14) tl.fillText(String(at.getHours()).padStart(2, "0"), x, h - 6);
    }

    const cx = X(m), v = sample(d, m);
    tl.fillStyle = color(T.ink);
    tl.fillRect(cx - 0.75, 0, 1.5, base);
    tl.beginPath(); tl.arc(cx, Y(v.p1 + v.p2), 3.5, 0, TAU); tl.fill();
  }

  function render() {
    const d = state.day;
    if (!d) return;
    const m = state.minute, v = sample(d, m);
    if (site && W) {
      setCamera();
      updateLight(sunAt(d.t0 + m * 60000), v.clear);
      ctx.clearRect(0, 0, W, H);
      ctx.lineJoin = "round";
      paintGround();
      const sunFar = P(SUN[0] * SKY_R, SUN[1] * SKY_R, SUN[2] * SKY_R)[2] > 0;
      paintArcs(true, d, m);
      if (sunFar) paintSun();
      paintObjects(d, m);
      paintArcs(false, d, m);
      if (!sunFar) paintSun();
      paintLabels(d);
      placePulse();
    }
    updateEstimate(d);
    updateFigures(d, m, v);
    if (timelineDirty) { drawTimeline(); timelineDirty = false; }
  }

  // ---- Frame loop: only runs while something has to be drawn ----
  let frame = null, lastFrame = null, dirty = false;
  function invalidate() {
    dirty = true;
    if (frame === null) frame = requestAnimationFrame(tick);
  }
  function tick(now) {
    frame = null;
    // Only frame timestamps are compared: one taken outside a frame can be
    // later than the next frame's, which made the step negative.
    const dt = lastFrame === null ? 16 : clamp(now - lastFrame, 0, 48);
    lastFrame = now;
    let moving = false;
    if (cam.goal !== null) {
      const diff = Math.atan2(Math.sin(cam.goal - cam.yaw), Math.cos(cam.goal - cam.yaw));
      const k = 1 - Math.pow(0.992, dt);
      cam.yaw += diff * k;
      cam.pitch += (DEFAULT_PITCH - cam.pitch) * k;
      if (Math.abs(diff) < 0.002 && Math.abs(DEFAULT_PITCH - cam.pitch) < 0.002) cam.goal = null;
      moving = true;
    } else if (!drag && Math.abs(cam.spin) > 2e-5) {
      cam.yaw += cam.spin * dt;
      cam.spin *= Math.pow(0.996, dt);
      moving = true;
    }
    if (state.playing && state.day) {
      const d = state.day;
      const end = maxMinute(d);
      const next = state.minute + dt * (d.m1 - d.m0) / PLAY_DURATION_MS;
      setMinute(next);
      // Once through. A replay of today ends at the present, so it goes live again.
      if (next >= end) { setPlaying(false); setFollow(d.is_today); }
      moving = true;
    }
    if (moving || dirty) {
      dirty = false;
      render();
    }
    // setMinute() may have asked for the next frame already.
    if (moving) frame ??= requestAnimationFrame(tick);
    else if (frame === null) lastFrame = null;
  }

  function maxMinute(d) { return d.is_today ? clamp(nowMinute(d), d.m0, d.m1) : d.m1; }
  function setMinute(m) {
    const d = state.day;
    state.minute = clamp(m, d.m0, maxMinute(d));
    timeInput.value = String(Math.round(state.minute));
    timelineDirty = true;
    invalidate();
  }
  function setFollow(on) {
    state.follow = on;
    $("sun-now").hidden = on || !state.day || !state.day.is_today;
  }
  function setPlaying(on) {
    state.playing = on;
    $("sun-icon-play").toggleAttribute("hidden", on);   // SVG elements have no hidden property
    $("sun-icon-pause").toggleAttribute("hidden", !on);
    $("sun-play").setAttribute("aria-label", tr(on ? "sun.pause" : "sun.play"));
    if (on) { setFollow(false); invalidate(); }
  }

  function applyDay(raw) {
    const previous = state.day;
    const d = prepare(raw);
    const sameDay = previous && previous.date === d.date;
    state.day = d;
    timeInput.min = String(Math.round(d.m0));
    timeInput.max = String(Math.round(d.m1));
    if (!sameDay) {
      setPlaying(false);
      setFollow(d.is_today);
      setMinute(d.is_today ? nowMinute(d) : d.set - 30);
    } else if (state.follow) {
      setMinute(nowMinute(d));
    } else {
      setMinute(state.minute);
    }
    timelineDirty = true;
    invalidate();
  }

  function applyLayout() {
    const hasSky = !!site;
    const visible = hasSky || (state.day && (state.day.hasData || state.day.hasTarget));
    card.hidden = !visible;
    card.classList.toggle("sun-flat", !hasSky);
    $("sun-osm").hidden = !(site && site.has_map);
    $("sun-meteo").hidden = !(state.day && (state.day.forecast || state.day.weather));
    $("sun-play").hidden = !hasSky;
    $("sun-reset").hidden = !hasSky;
    if (!hasSky) pulse.hidden = true;
  }

  function applyTexts() {
    canvas.setAttribute("aria-label", tr("sun.sceneLabel"));
    timeInput.setAttribute("aria-label", tr("sun.timeLabel"));
    $("sun-reset").setAttribute("aria-label", tr("sun.resetView"));
    $("sun-reset").title = tr("sun.resetView");
    $("sun-reset").textContent = tr("sun.north");
    $("sun-play").setAttribute("aria-label", tr(state.playing ? "sun.pause" : "sun.play"));
    solstices = { year: null, arcs: [] };
    for (const id of Object.keys(shown)) delete shown[id];
  }

  // ---- Input ----
  let drag = null;
  canvas.addEventListener("pointerdown", e => {
    canvas.setPointerCapture(e.pointerId);
    canvas.classList.add("grabbing");
    drag = { x: e.clientX, y: e.clientY, t: performance.now(), mouse: e.pointerType !== "touch" };
    cam.spin = 0;
    cam.goal = null;
  });
  canvas.addEventListener("pointermove", e => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y, now = performance.now();
    cam.yaw += dx * 0.0075;
    if (drag.mouse) cam.pitch = clamp(cam.pitch + dy * 0.005, MIN_PITCH, MAX_PITCH);
    cam.spin = REDUCED_MOTION ? 0 : mix(cam.spin, dx * 0.0075 / Math.max(8, now - drag.t), 0.4);
    drag.x = e.clientX; drag.y = e.clientY; drag.t = now;
    invalidate();
  });
  const endDrag = () => {
    if (drag && performance.now() - drag.t > 90) cam.spin = 0;
    drag = null;
    canvas.classList.remove("grabbing");
    invalidate();
  };
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", endDrag);
  canvas.addEventListener("keydown", e => {
    const move = { ArrowLeft: [-0.09, 0], ArrowRight: [0.09, 0], ArrowUp: [0, 0.05], ArrowDown: [0, -0.05] }[e.key];
    if (!move) return;
    e.preventDefault();
    cam.goal = null;
    cam.yaw += move[0];
    cam.pitch = clamp(cam.pitch + move[1], MIN_PITCH, MAX_PITCH);
    invalidate();
  });
  $("sun-reset").addEventListener("click", () => {
    cam.spin = 0;
    cam.goal = site && site.lat < 0 ? YAW_SOUTH : YAW_NORTH;
    invalidate();
  });

  const scrub = $("sun-scrub");
  let scrubbing = false;
  const seek = e => {
    const d = state.day;
    if (!d) return;
    const r = scrub.getBoundingClientRect();
    setFollow(false);
    setMinute(mix(d.m0, d.m1, clamp((e.clientX - r.left) / r.width, 0, 1)));
  };
  scrub.addEventListener("pointerdown", e => {
    scrub.setPointerCapture(e.pointerId);
    scrubbing = true;
    setPlaying(false);
    seek(e);
    timeInput.focus({ preventScroll: true });
  });
  scrub.addEventListener("pointermove", e => { if (scrubbing) seek(e); });
  scrub.addEventListener("pointerup", () => { scrubbing = false; });
  scrub.addEventListener("pointercancel", () => { scrubbing = false; });
  timeInput.addEventListener("input", () => { setFollow(false); setMinute(+timeInput.value); });
  $("sun-play").addEventListener("click", () => {
    const d = state.day;
    if (!d) return;
    if (!state.playing && state.minute >= maxMinute(d) - 1) setMinute(d.m0);
    setPlaying(!state.playing);
  });
  $("sun-now").addEventListener("click", () => {
    setPlaying(false);
    setFollow(true);
    setMinute(nowMinute(state.day));
  });

  function resize() {
    const r = view.getBoundingClientRect();
    W = r.width; H = r.height;
    if (W && H) {
      const dpr = Math.min(devicePixelRatio || 1, 2);
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      S = Math.min(W / 26.5, H / 21);
      CX = W / 2;
      CY = H * (H > W * 0.8 ? 0.62 : 0.67);   // a phone has the figures above the scene and can use its height
    }
    timelineDirty = true;
    invalidate();
  }
  const refreshTheme = () => { readTokens(); timelineDirty = true; invalidate(); };
  readTokens();
  new ResizeObserver(resize).observe(view);
  new ResizeObserver(() => { timelineDirty = true; invalidate(); }).observe(scrub);
  matchMedia("(prefers-color-scheme: light)").addEventListener("change", refreshTheme);
  new MutationObserver(refreshTheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  document.fonts?.ready.then(() => { timelineDirty = true; invalidate(); });
  // At night the day chart refreshes only every five minutes; the present doesn't wait for it.
  setInterval(() => { if (state.follow && state.day?.is_today) setMinute(nowMinute(state.day)); }, 20000);

  // ---- Loading: called with every refresh of the day chart ----
  let requestSeq = 0, sitePayload = null, textsFor = null;
  const fetchJson = url => fetch(url).then(res => res.json());

  async function load(day, nextLang, nextLocale) {
    lang = nextLang;
    locale = nextLocale;
    const seq = ++requestSeq;
    // The site only changes while the server is still resolving it.
    const askSite = !sitePayload || (sitePayload.configured && !(sitePayload.ready && sitePayload.has_map));
    try {
      const [siteAnswer, curveAnswer] = await Promise.all([
        askSite ? fetchJson("/api/site") : null,
        fetchJson(`/api/yield-curve${day ? `?date=${day}` : ""}`),
      ]);
      if (seq !== requestSeq) return;
      if (textsFor !== lang) { textsFor = lang; applyTexts(); }
      if (siteAnswer) {
        const changed = !sitePayload || sitePayload.ready !== siteAnswer.ready || sitePayload.has_map !== siteAnswer.has_map;
        sitePayload = siteAnswer;
        if (changed) { applySite(siteAnswer); state.day = null; }
      }
      applyDay(curveAnswer);
      applyLayout();
      resize();
    } catch (e) {
      console.error("SunScene.load:", e);
    }
  }

  window.SunScene = { load };
})();
