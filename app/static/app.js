/* EZ1 Monitor — dashboard frontend */

const REFRESH_LIVE_ACTIVE = 10_000;
const REFRESH_LIVE_IDLE   = 60_000;
const REFRESH_HIST_ACTIVE = 60_000;
const REFRESH_HIST_IDLE   = 300_000;

// Pre-detect language from navigator so the day picker can initialize
// with the right locale immediately — before the first /api/live response
// arrives. The backend's DEFAULT_LANG env (if set) overrides this once
// loadLive() runs, but for the common case (user's browser language matches
// the inverter location) this avoids the brief en → de flash on first load.
function detectInitialLang() {
  const supported = ["de", "en"];
  const browserLang = (navigator.language || "en").substring(0, 2).toLowerCase();
  return supported.includes(browserLang) ? browserLang : "en";
}

const initialLang = detectInitialLang();

const state = {
  lang: initialLang,
  locale: initialLang === "de" ? "de-DE" : "en-US",
  currency: "USD",
  pricePerKwh: 0.35,
  co2KgPerKwh: 0.38,
  installKwp: 1.0,
  maxPowerW: 800,
  currentRange: "month",
  yearGranularity: "daily",
  multiYearGranularity: "monthly",   // monthly | yearly
  statusState: "noData",
  pollInterval: 60,
  retentionDays: 730,
  // Day picker state: null = today (live), Date object = historical view
  viewedDay: null,
};

let todayChart, historyChart;
let liveTimer, statsTimer, todayTimer, historyTimer;
let dayPicker = null;  // flatpickr instance

const fmt = {
  power: v => (v == null ? "—" : Math.round(Number(v)).toString()),
  kwh:   v => (v == null ? "—" : Number(v).toFixed(2)),
  pct:   v => (v == null ? "—" : Math.round(Number(v)).toString()),
  date:  ts => new Date(ts * 1000).toLocaleDateString(state.locale, {
    weekday: "long", day: "2-digit", month: "long", year: "numeric"
  }),
  dateLong: d => d.toLocaleDateString(state.locale, {
    weekday: "long", day: "2-digit", month: "long", year: "numeric"
  }),
  time:  ts => new Date(ts * 1000).toLocaleTimeString(state.locale, {
    hour: "2-digit", minute: "2-digit", second: "2-digit"
  }),
  // HH:MM only — used for the CO2 card subtitle where seconds add noise
  // without any useful info (the CO2 source updates hourly anyway).
  timeShort: ts => new Date(ts * 1000).toLocaleTimeString(state.locale, {
    hour: "2-digit", minute: "2-digit"
  }),
  money: v => new Intl.NumberFormat(state.locale, {
    style: "currency",
    currency: state.currency,
    maximumFractionDigits: 2,
  }).format(v || 0),
  pricePerKwh: v => {
    if (state.lang === "de" && state.currency === "EUR") {
      const cents = v * 100;
      const display = Number.isInteger(cents)
        ? cents.toFixed(0)
        : cents.toFixed(2).replace(".", ",");
      return `${display} ct/kWh`;
    }
    const formatted = new Intl.NumberFormat(state.locale, {
      style: "currency",
      currency: state.currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 4,
    }).format(v);
    return `${formatted}/kWh`;
  },
  monthYear: (isoYearMonth) => {
    if (!isoYearMonth) return "—";
    const [y, m] = isoYearMonth.split("-").map(Number);
    return new Date(y, m - 1, 1).toLocaleDateString(state.locale, {
      month: "long", year: "numeric"
    });
  },
  shortMonthYear: (isoYearMonth) => {
    if (!isoYearMonth) return "—";
    const [y, m] = isoYearMonth.split("-").map(Number);
    return new Date(y, m - 1, 1).toLocaleDateString(state.locale, {
      month: "short", year: "2-digit"
    });
  },
  isoDay: (d) => {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  },
};

function localDateKey(date) {
  return fmt.isoDay(date);
}

function isToday(d) {
  if (!d) return false;
  const today = new Date();
  return d.getFullYear() === today.getFullYear()
      && d.getMonth() === today.getMonth()
      && d.getDate() === today.getDate();
}

// COLORS is populated from CSS variables. Re-call refreshChartColors() after
// a theme switch to pick up new values from the freshly-set CSS variables.
const COLORS = {};

function refreshChartColors() {
  const css = getComputedStyle(document.documentElement);
  COLORS.accent     = css.getPropertyValue("--accent").trim() || "#f59e0b";
  COLORS.accentWarm = css.getPropertyValue("--accent-warm").trim() || "#fb923c";
  COLORS.text       = css.getPropertyValue("--text-primary").trim() || "#f4ede0";
  COLORS.muted      = css.getPropertyValue("--text-muted").trim() || "#6f6353";
  COLORS.border     = css.getPropertyValue("--border").trim() || "#2a241c";
  COLORS.good       = css.getPropertyValue("--good").trim() || "#4ade80";
  COLORS.tooltipBg  = css.getPropertyValue("--tooltip-bg").trim() || "#000";
  Chart.defaults.color = COLORS.muted;
  Chart.defaults.font.family = "'JetBrains Mono', monospace";
  Chart.defaults.font.size = 11;
  Chart.defaults.borderColor = COLORS.border;
  Chart.defaults.scale.grid.color = COLORS.border;
  Chart.defaults.scale.grid.tickColor = COLORS.border;
}

// COLORS is populated from CSS variables. Re-call refreshChartColors() after
// any theme change to pick up new values. NOTE: do NOT call this at module
// load time — the explicit theme override (data-theme attribute) is set
// later by applyStoredTheme(), and reading CSS vars before that may pick
// up the wrong theme's colors. The first call happens at the end of the
// file, immediately after applyStoredTheme().


// --- Custom Chart.js plugin: dashed line + label at year boundary ---
const yearBoundaryPlugin = {
  id: "yearBoundary",
  afterDatasetsDraw(chart, args, options) {
    const boundaries = options.boundaries || [];
    if (!boundaries.length) return;
    const ctx = chart.ctx;
    const xAxis = chart.scales.x;
    const top = chart.chartArea.top;
    const bottom = chart.chartArea.bottom;
    ctx.save();
    boundaries.forEach(b => {
      const xPos = xAxis.getPixelForValue(b.label);
      if (xPos < chart.chartArea.left || xPos > chart.chartArea.right) return;
      ctx.strokeStyle = COLORS.accentWarm + "aa";
      ctx.lineWidth = 1;
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      ctx.moveTo(xPos, top);
      ctx.lineTo(xPos, bottom);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = COLORS.accentWarm;
      ctx.font = "bold 11px JetBrains Mono, monospace";
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.fillText(" " + b.year, xPos, top + 4);
    });
    ctx.restore();
  },
};


// --- Status pill -------------------------------------------------------

function applyStatus(statusState) {
  const pill = document.getElementById("status-pill");
  pill.classList.remove("online", "offline", "standby", "error", "noData");
  pill.classList.add(statusState);
  document.getElementById("status-text").textContent =
    window.i18n.t(state.lang, `status.${statusState}`);
}


// --- Live data ---------------------------------------------------------

async function loadLive() {
  try {
    const res = await fetch("/api/live");
    const data = await res.json();

    if (data.config) {
      state.lang = data.config.language || "en";
      state.locale = state.lang === "de" ? "de-DE" : "en-US";
      state.currency = data.config.currency || "USD";
      state.pricePerKwh = data.config.price_per_kwh || 0.35;
      state.co2KgPerKwh = data.config.co2_kg_per_kwh || 0.38;
      state.installKwp = data.config.install_kwp || 1.0;
      state.pollInterval = data.config.poll_interval || 60;
      state.retentionDays = data.config.retention_days || 730;

      window.i18n.applyTranslations(state.lang);
      updateDynamicLabels();
      ensureDayPicker();   // Re-init picker with new locale if needed
      updateDayPickerLabels();

      document.getElementById("footer-inverter").textContent =
        data.config.inverter_ip;
      const versionEl = document.getElementById("footer-version");
      if (versionEl && data.config.version) {
        versionEl.textContent = `v${data.config.version}`;
      }
    }

    if (data.device) {
      state.maxPowerW = data.device.max_power || 800;
      const deviceId = data.device.device_id || data.device.serial_number || "EZ1-M";
      document.getElementById("device-subtitle").textContent =
        `${deviceId} · max ${state.maxPowerW} W`;
    }

    // Carbon block (v1.4.0): live grid intensity from Electricity Maps or
    // fallback to static. Stored on state so updateDynamicLabels() can
    // render the CO2 card subtitles with full provenance.
    state.carbon = data.carbon || null;
    updateDynamicLabels();

    const newState = (data.status && data.status.state) || "noData";
    const stateChanged = newState !== state.statusState;
    state.statusState = newState;
    applyStatus(newState);

    const m = data.latest;
    if (m) {
      if (m.online) {
        const totalW = (m.p1 || 0) + (m.p2 || 0);
        document.getElementById("current-power").textContent = fmt.power(totalW);
        document.getElementById("pv1-power").textContent = fmt.power(m.p1);
        document.getElementById("pv2-power").textContent = fmt.power(m.p2);
        document.getElementById("pv1-energy").textContent = fmt.kwh(m.e1);
        document.getElementById("pv2-energy").textContent = fmt.kwh(m.e2);
        const pct = Math.min(100, (totalW / state.maxPowerW) * 100);
        document.getElementById("power-bar").style.width = pct + "%";
        document.getElementById("power-pct").textContent = fmt.pct(pct) + " %";
      } else if (newState === "standby" || newState === "error") {
        document.getElementById("current-power").textContent = "0";
        document.getElementById("pv1-power").textContent = "0";
        document.getElementById("pv2-power").textContent = "0";
        document.getElementById("power-bar").style.width = "0%";
        document.getElementById("power-pct").textContent = "0 %";
      }
      document.getElementById("power-max").textContent =
        window.i18n.t(state.lang, "hero.maxPower", { max: state.maxPowerW });
      if (m.timestamp) {
        document.getElementById("footer-last").textContent = fmt.time(m.timestamp);
      }
    }

    if (stateChanged) {
      scheduleTimers();
    }
  } catch (e) {
    console.error("loadLive:", e);
    applyStatus("error");
  }
}

function updateDynamicLabels() {
  renderCarbonSubtitles();

  const moneySub = document.getElementById("lifetime-money-sub");
  if (moneySub) {
    moneySub.textContent = window.i18n.t(state.lang, "lifetime.moneyBasedOn", {
      price: fmt.pricePerKwh(state.pricePerKwh),
    });
  }
  const footerUpdate = document.getElementById("footer-update-text");
  if (footerUpdate) {
    footerUpdate.textContent = window.i18n.t(state.lang, "footer.updateEvery", {
      s: state.pollInterval,
    });
  }
}


// --- CO2 card subtitles (variant B: two lines, all info at a glance) ---

function renderCarbonSubtitles() {
  // Two lines on the CO2 card:
  //   Line 1: source label + g/kWh + freshness ("Live (DE) · 117 g/kWh · 22:00 Uhr")
  //   Line 2: grid mix details ("Grid-Mix: 43% fossil · 57% sauber")
  // Line 2 is empty when fossilFuelPercentage isn't available (static mode
  // or older API responses). The CSS hides empty .lifetime-sub-secondary.
  const sub1 = document.getElementById("lifetime-co2-sub-1");
  const sub2 = document.getElementById("lifetime-co2-sub-2");
  if (!sub1 || !sub2) return;

  const c = state.carbon;
  // No carbon block yet (very first request before /api/live resolved) →
  // fall back to the v1.3.x behavior so the UI never shows "—"
  if (!c) {
    sub1.textContent = window.i18n.t(state.lang, "lifetime.co2BasedOn", {
      g: Math.round((state.co2KgPerKwh || 0.38) * 1000),
    });
    sub2.textContent = "";
    return;
  }

  const g = Math.round(c.g_per_kwh);
  const zone = c.country_code || c.configured_zone || "—";

  if (c.source === "live") {
    const time = c.datetime ? fmt.timeShort(Math.floor(new Date(c.datetime).getTime() / 1000)) : "";
    sub1.textContent = window.i18n.t(state.lang, "lifetime.co2Live", {
      zone, g, time,
    });
  } else if (c.source === "stale") {
    const hours = Math.round((c.age_seconds || 0) / 3600);
    sub1.textContent = window.i18n.t(state.lang, "lifetime.co2Stale", {
      zone, g, hours,
    });
  } else if (c.source === "avg") {
    sub1.textContent = window.i18n.t(state.lang, "lifetime.co2Avg", {
      zone, g, count: c.rolling_count || 0,
    });
  } else {
    // static
    sub1.textContent = window.i18n.t(state.lang, "lifetime.co2Static", { g });
  }

  // Grid mix line — only shown when we have fossil percentage
  if (typeof c.fossil_pct === "number") {
    const fossil = Math.round(c.fossil_pct);
    const clean = 100 - fossil;
    sub2.textContent = window.i18n.t(state.lang, "lifetime.co2GridMix", {
      fossil, clean,
    });
  } else {
    sub2.textContent = "";  // CSS :empty hides this line
  }
}


// --- Stats -------------------------------------------------------------

async function loadStats() {
  try {
    const res = await fetch("/api/stats");
    const s = await res.json();

    document.getElementById("stat-today").textContent      = fmt.kwh(s.today_kwh);
    document.getElementById("stat-yesterday-until-now").textContent = fmt.kwh(s.yesterday_until_now_kwh);
    document.getElementById("stat-yesterday-full").textContent      = fmt.kwh(s.yesterday_full_kwh);
    renderCompare("stat-today-compare", s.today_kwh, s.yesterday_until_now_kwh);

    document.getElementById("stat-week").textContent = fmt.kwh(s.this_week_kwh);
    document.getElementById("stat-last-week-until-now").textContent = fmt.kwh(s.last_week_until_now_kwh);
    document.getElementById("stat-last-week-full").textContent      = fmt.kwh(s.last_week_full_kwh);
    renderCompare("stat-week-compare", s.this_week_kwh, s.last_week_until_now_kwh);

    document.getElementById("stat-month").textContent = fmt.kwh(s.this_month_kwh);
    document.getElementById("stat-last-month-until-progress").textContent = fmt.kwh(s.last_month_until_progress_kwh);
    document.getElementById("stat-last-month-full").textContent           = fmt.kwh(s.last_month_full_kwh);
    renderCompare("stat-month-compare", s.this_month_kwh, s.last_month_until_progress_kwh);

    document.getElementById("stat-same-month-ly").textContent       = fmt.kwh(s.same_month_last_year_kwh);
    document.getElementById("stat-same-month-ly-total").textContent = fmt.kwh(s.same_month_last_year_total_kwh);
    document.getElementById("stat-same-month-ly-label").textContent = fmt.monthYear(s.same_month_last_year_iso);
    renderCompare("stat-same-month-ly-compare", s.this_month_kwh, s.same_month_last_year_kwh);
    const totalRow = document.getElementById("stat-same-month-ly-total-row");
    if (totalRow) {
      totalRow.style.display = (s.same_month_last_year_total_kwh > 0) ? "" : "none";
    }

    document.getElementById("stat-year").textContent = fmt.kwh(s.this_year_kwh);
    document.getElementById("stat-last-year-ytd").textContent = fmt.kwh(s.last_year_ytd_kwh);
    document.getElementById("stat-last-year-full").textContent = fmt.kwh(s.last_year_full_kwh);
    // Hide the "last year total" anchor row if we have no full-year data yet
    const lyFullRow = document.getElementById("stat-last-year-full-row");
    if (lyFullRow) {
      lyFullRow.style.display = (s.last_year_full_kwh > 0) ? "" : "none";
    }
    renderCompare("stat-year-compare", s.this_year_kwh, s.last_year_ytd_kwh);

    document.getElementById("hero-peak-value").textContent = fmt.power(s.peak_w_today);

    document.getElementById("lifetime-kwh").textContent = fmt.kwh(s.total_kwh);
    document.getElementById("lifetime-co2").textContent = (s.co2_saved_kg || 0).toFixed(1);
    document.getElementById("lifetime-money").textContent = fmt.money(s.money_saved);
  } catch (e) {
    console.error("loadStats:", e);
  }
}

function renderCompare(elementId, current, previous) {
  const el = document.getElementById(elementId);
  if (!el) return;
  if (!previous || previous === 0) {
    el.textContent = "";
    el.className = "stat-compare";
    return;
  }
  const delta = current - previous;
  const pct = (delta / previous) * 100;
  const sign = delta >= 0 ? "▲" : "▼";
  el.textContent = `${sign} ${Math.abs(pct).toFixed(0)} %`;
  el.className = "stat-compare " + (delta >= 0 ? "up" : "down");
}


// --- Day picker -------------------------------------------------------
//
// Architecture note (changed in v1.4.1):
//
// We previously used flatpickr's altInput: true feature, which generates a
// second visible <input> alongside the hidden real one. On iOS Safari this
// had a recurring quirk where setting input.value via JS didn't reliably
// paint inside flex/grid containers — the date would appear blank for up
// to 10 seconds until something else triggered a repaint.
//
// v1.4.1 takes full control: the visible element is now a <button> we
// render textContent into directly, and flatpickr binds to a hidden
// off-screen <input> for its internal Y-m-d state. Buttons repaint
// reliably on every browser, so the date appears instantly on every
// switch.

function getDayPickerFormat() {
  // Three-tier format depending on viewport width, gracefully degrading
  // from fully spelled-out names on desktop to compact abbreviations on
  // phones.
  const w = window.innerWidth || document.documentElement.clientWidth || 1200;
  if (state.lang === "de") {
    if (w <= 640)  return "D, d. M Y";   // "Fr, 05. Jun 2026"   — mobile
    if (w <= 1024) return "D, d. F Y";   // "Fr, 05. Juni 2026"  — tablet
    return "l, d. F Y";                  // "Freitag, 05. Juni 2026" — desktop
  }
  if (w <= 640)  return "D, M j, Y";
  if (w <= 1024) return "D, F j, Y";
  return "l, F j, Y";
}

function renderDayPickerDisplay(date) {
  // Single source of truth for what's shown in the visible button.
  // Called whenever the date changes (programmatically or via picker).
  const btn = document.getElementById("day-picker-display");
  if (!btn) return;
  const target = date || new Date();
  let text;
  try {
    // If flatpickr is loaded use its localized formatter — otherwise fall
    // back to the browser's locale-aware toLocaleDateString.
    if (typeof flatpickr !== "undefined" && flatpickr.formatDate) {
      const fmt = getDayPickerFormat();
      const locale = (dayPicker && dayPicker.l10n)
        ? dayPicker.l10n
        : flatpickr.l10ns.default;
      text = flatpickr.formatDate(target, fmt, locale);
    } else {
      text = target.toLocaleDateString(state.locale, {
        weekday: "short", day: "2-digit", month: "short", year: "numeric"
      });
    }
  } catch (e) {
    text = target.toLocaleDateString(state.locale);
  }
  // textContent paints reliably on iOS Safari (unlike input.value)
  btn.textContent = text;
  btn.setAttribute("aria-label", text);
}

function ensureDayPicker() {
  if (typeof flatpickr === "undefined") return;

  const input = document.getElementById("day-picker-input");
  const displayBtn = document.getElementById("day-picker-display");
  if (!input || !displayBtn) return;

  if (dayPicker) {
    dayPicker.destroy();
    dayPicker = null;
  }

  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const earliest = new Date(today);
  earliest.setDate(earliest.getDate() - state.retentionDays);

  const fpLocale = (state.lang === "de" && flatpickr.l10ns && flatpickr.l10ns.de)
    ? flatpickr.l10ns.de
    : "default";

  // No altInput — we render the display ourselves into displayBtn. The
  // bound input is hidden via CSS but still in the DOM so flatpickr can
  // position its popup relative to it.
  dayPicker = flatpickr(input, {
    locale: fpLocale,
    dateFormat: "Y-m-d",
    altInput: false,
    clickOpens: false,             // we open via displayBtn click
    maxDate: today,
    minDate: earliest,
    defaultDate: state.viewedDay || today,
    // Position the popup near the display button instead of the hidden input
    positionElement: displayBtn,
    onChange: function (selectedDates) {
      if (!selectedDates.length) return;
      const picked = selectedDates[0];
      setViewedDay(isToday(picked) ? null : picked);
    },
  });

  // Render the initial display
  renderDayPickerDisplay(state.viewedDay || today);

  // Re-bind the open-on-click handler. Using onclick (not addEventListener)
  // ensures we don't pile up listeners across multiple ensureDayPicker()
  // calls (e.g. on language change).
  displayBtn.onclick = (e) => {
    e.preventDefault();
    if (dayPicker) dayPicker.open();
  };
}

function applyResponsiveDayFormat() {
  // On viewport resize, just re-render the display text with the new
  // format. No flatpickr config change needed — getDayPickerFormat() is
  // re-evaluated on every renderDayPickerDisplay() call.
  renderDayPickerDisplay(state.viewedDay);
}

let _dayPickerResizeTimer = null;
window.addEventListener("resize", () => {
  if (_dayPickerResizeTimer) clearTimeout(_dayPickerResizeTimer);
  _dayPickerResizeTimer = setTimeout(applyResponsiveDayFormat, 200);
});
window.addEventListener("orientationchange", () => {
  setTimeout(applyResponsiveDayFormat, 300);
});

function updateDayPickerLabels() {
  const prevBtn = document.getElementById("day-prev");
  const nextBtn = document.getElementById("day-next");
  if (prevBtn) {
    const label = window.i18n.t(state.lang, "chart.previousDay");
    prevBtn.title = label;
    prevBtn.setAttribute("aria-label", label);
  }
  if (nextBtn) {
    const label = window.i18n.t(state.lang, "chart.nextDay");
    nextBtn.title = label;
    nextBtn.setAttribute("aria-label", label);
  }
}

function updateDayPickerButtons() {
  // Update the visibility/disabled state of day-picker controls without
  // triggering any data fetches. Call after state.viewedDay changes.
  const nextBtn = document.getElementById("day-next");
  if (nextBtn) {
    nextBtn.disabled = (state.viewedDay === null);
  }
  const prevBtn = document.getElementById("day-prev");
  if (prevBtn) {
    const earliest = new Date();
    earliest.setDate(earliest.getDate() - state.retentionDays);
    earliest.setHours(0, 0, 0, 0);
    const current = state.viewedDay || new Date();
    prevBtn.disabled = (current <= earliest);
  }
  const todayBtn = document.getElementById("day-today");
  if (todayBtn) {
    todayBtn.style.display = (state.viewedDay === null) ? "none" : "";
  }
}

function setViewedDay(date) {
  // date: null = today (live), Date = historical
  state.viewedDay = date;

  // Update picker internal state (silently — no onChange trigger)
  if (dayPicker) {
    const target = date || new Date();
    dayPicker.setDate(target, false);
  }

  // Render the visible display. With the v1.4.1 custom-button architecture
  // this is a simple textContent write — no iOS Safari quirks possible.
  renderDayPickerDisplay(date);

  updateDayPickerButtons();

  // Reload the chart for the new day
  loadTodayChart();

  // Reschedule timers: live refresh only for today
  scheduleTimers();

  // Re-render history chart to reflect the highlighted day (if applicable)
  if (historyChart) {
    loadHistoryChart(state.currentRange);
  }
}

function shiftViewedDay(deltaDays) {
  const base = state.viewedDay ? new Date(state.viewedDay) : new Date();
  base.setHours(0, 0, 0, 0);
  base.setDate(base.getDate() + deltaDays);

  const today = new Date();
  today.setHours(0, 0, 0, 0);
  if (base > today) {
    setViewedDay(null);  // clamp to today (live mode)
    return;
  }
  const earliest = new Date();
  earliest.setDate(earliest.getDate() - state.retentionDays);
  earliest.setHours(0, 0, 0, 0);
  if (base < earliest) return;  // already at limit

  setViewedDay(isToday(base) ? null : base);
}


// --- Today chart ------------------------------------------------------

async function loadTodayChart() {
  try {
    const dateParam = state.viewedDay
      ? `&date=${fmt.isoDay(state.viewedDay)}`
      : "";
    const res = await fetch(`/api/history?range=day${dateParam}`);
    const data = await res.json();
    const points = (data.points || []).filter(p => p.online);
    const labels = points.map(p => p.timestamp * 1000);
    const series = points.map(p => (p.p1 || 0) + (p.p2 || 0));

    // Empty-state overlay
    const empty = document.getElementById("chart-today-empty");
    if (empty) {
      empty.style.display = (points.length === 0) ? "" : "none";
    }

    // Update card title meta with chosen day (if not today)
    // (We re-use the meta slot via the day-picker, so nothing extra here)

    if (todayChart) todayChart.destroy();
    const ctx = document.getElementById("chart-today").getContext("2d");

    const grad = ctx.createLinearGradient(0, 0, 0, 280);
    grad.addColorStop(0, COLORS.accent + "55");
    grad.addColorStop(1, COLORS.accent + "00");

    todayChart = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: window.i18n.t(state.lang, "chart.tooltipPower"),
          data: series,
          borderColor: COLORS.accent,
          backgroundColor: grad,
          borderWidth: 2,
          fill: true,
          pointRadius: 0,
          pointHoverRadius: 4,
          pointHoverBackgroundColor: COLORS.accentWarm,
          tension: 0.3,
        }],
      },
      options: timeChartOptions("HH:mm"),
    });
  } catch (e) {
    console.error("loadTodayChart:", e);
  }
}


// --- History chart ----------------------------------------------------

async function loadHistoryChart(range) {
  try {
    const isYear = range === "year";
    const isMultiYear = range === "multiyear";

    // Multi-year view: pulls from monthly_aggregates table (survives
    // retention pruning). Same chart shape as the year-view monthly mode,
    // just spans every year that has data instead of the last 12 months.
    if (isMultiYear) {
      const url = state.multiYearGranularity === "yearly"
        ? "/api/history?range=multiyear&granularity=yearly"
        : "/api/history?range=multiyear&granularity=monthly";
      const res = await fetch(url);
      const data = await res.json();
      if (state.multiYearGranularity === "yearly") {
        renderYearlyHistory(data);
      } else {
        renderMultiYearMonthly(data);
      }
      const granTabs = document.getElementById("granularity-tabs");
      if (granTabs) granTabs.style.display = "";
      // Swap the tab labels: yearly vs monthly (instead of daily vs monthly)
      updateGranularityTabsForRange(range);
      return;
    }

    const useMonthly = isYear && state.yearGranularity === "monthly";
    const url = useMonthly
      ? "/api/history?range=year&granularity=monthly"
      : `/api/history?range=${range}`;

    const res = await fetch(url);
    const data = await res.json();

    if (useMonthly) {
      renderMonthlyHistory(data);
    } else {
      renderDailyHistory(data, isYear);
    }

    const granTabs = document.getElementById("granularity-tabs");
    if (granTabs) granTabs.style.display = isYear ? "" : "none";
    if (isYear) updateGranularityTabsForRange("year");
  } catch (e) {
    console.error("loadHistoryChart:", e);
  }
}

// Swap the granularity tab labels and their data-gran attributes depending
// on whether we're in "year" mode (daily/monthly) or "multiyear" (monthly/yearly).
function updateGranularityTabsForRange(range) {
  const tabs = document.querySelectorAll("#granularity-tabs .gran-tab");
  if (tabs.length !== 2) return;
  if (range === "multiyear") {
    tabs[0].dataset.gran = "monthly";
    tabs[0].textContent = window.i18n.t(state.lang, "chart.granMonthly");
    tabs[1].dataset.gran = "yearly";
    tabs[1].textContent = window.i18n.t(state.lang, "chart.granYearly");
    // Reflect current selection
    const active = state.multiYearGranularity || "monthly";
    tabs.forEach(t => t.classList.toggle("active", t.dataset.gran === active));
  } else {
    tabs[0].dataset.gran = "daily";
    tabs[0].textContent = window.i18n.t(state.lang, "chart.granDaily");
    tabs[1].dataset.gran = "monthly";
    tabs[1].textContent = window.i18n.t(state.lang, "chart.granMonthly");
    const active = state.yearGranularity || "daily";
    tabs.forEach(t => t.classList.toggle("active", t.dataset.gran === active));
  }
}

function renderMultiYearMonthly(data) {
  // Shape from API: { months: [{year, month, total_kwh, peak_w, days_with_data, ...}, ...] }
  // Convert to the same label format renderMonthlyHistory expects (YYYY-MM keys),
  // then delegate to it.
  const months = (data.months || []).map(m => ({
    month: `${m.year}-${String(m.month).padStart(2, "0")}`,
    kwh: m.total_kwh,
  }));
  if (months.length === 0) {
    // Edge case: no aggregates yet
    if (historyChart) { historyChart.destroy(); historyChart = null; }
    const ctx = document.getElementById("chart-history").getContext("2d");
    ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
    return;
  }
  renderMonthlyHistory({ months });
}

function renderYearlyHistory(data) {
  // Shape from API: { years: [{year, total_kwh, ...}, ...] }
  const years = data.years || [];
  const labels = years.map(y => String(y.year));
  const series = years.map(y => y.total_kwh);
  const thisYear = new Date().getFullYear();
  const backgroundColors = labels.map(l =>
    parseInt(l, 10) === thisYear ? COLORS.accent + "cc" : COLORS.accent + "55"
  );

  if (historyChart) historyChart.destroy();
  const ctx = document.getElementById("chart-history").getContext("2d");
  historyChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "kWh",
        data: series,
        backgroundColor: backgroundColors,
        borderColor: COLORS.accent,
        borderWidth: 1,
        borderRadius: 3,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: tooltipStyle({
          title: items => items[0].label,
          label: item => ` ${item.parsed.y.toFixed(2)} kWh`,
        }),
      },
      scales: {
        x: { ticks: { maxRotation: 0, autoSkip: false } },
        y: { beginAtZero: true, ticks: { callback: v => v + " kWh" } },
      },
    },
  });
}

function renderDailyHistory(data, isYear) {
  const points = data.points || [];
  const byDay = new Map();
  for (const p of points) {
    if (!p.online) continue;
    const d = new Date(p.timestamp * 1000);
    const key = localDateKey(d);
    const total = (p.e1 || 0) + (p.e2 || 0);
    if (!byDay.has(key) || byDay.get(key).max < total) {
      byDay.set(key, { ts: p.timestamp, max: total });
    }
  }
  const days = [...byDay.entries()].sort();
  const labels = days.map(([k]) => k);
  const series = days.map(([_, v]) => v.max);

  // Year-view dimming and boundaries
  const thisYear = new Date().getFullYear();
  const boundaries = [];
  let backgroundColors;
  let borderColors;
  let borderWidths;

  const viewedDayKey = state.viewedDay ? fmt.isoDay(state.viewedDay) : null;

  if (isYear && labels.length > 0) {
    backgroundColors = labels.map(label => {
      const y = parseInt(label.substring(0, 4), 10);
      return y === thisYear ? COLORS.accent + "cc" : COLORS.accent + "55";
    });
    let previousYear = null;
    labels.forEach(label => {
      const y = parseInt(label.substring(0, 4), 10);
      if (previousYear !== null && y !== previousYear) {
        boundaries.push({ label: label, year: y });
      }
      previousYear = y;
    });
  } else {
    backgroundColors = labels.map(() => COLORS.accent + "cc");
  }

  // Drill-down: highlight the day currently shown in the Today chart
  borderColors = labels.map(l => (l === viewedDayKey) ? COLORS.good : COLORS.accent);
  borderWidths = labels.map(l => (l === viewedDayKey) ? 2 : 1);

  if (historyChart) historyChart.destroy();
  const ctx = document.getElementById("chart-history").getContext("2d");

  historyChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "kWh",
        data: series,
        backgroundColor: backgroundColors,
        borderColor: borderColors,
        borderWidth: borderWidths,
        borderRadius: 3,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      onHover: (event, elements) => {
        event.native.target.style.cursor = elements.length > 0 ? "pointer" : "default";
      },
      onClick: (_event, elements) => {
        if (!elements.length) return;
        const idx = elements[0].index;
        const label = labels[idx];
        const [y, m, d] = label.split("-").map(Number);
        const picked = new Date(y, m - 1, d);
        setViewedDay(isToday(picked) ? null : picked);
        // Smooth scroll to today chart card
        const card = document.getElementById("today-chart-card");
        if (card) card.scrollIntoView({ behavior: "smooth", block: "start" });
      },
      plugins: {
        legend: { display: false },
        tooltip: tooltipStyle({
          title: items => new Date(items[0].label).toLocaleDateString(state.locale, {
            weekday: "short", day: "2-digit", month: "short", year: "numeric",
          }),
          label: item => ` ${item.parsed.y.toFixed(2)} kWh`,
          afterLabel: () => window.i18n.t(state.lang, "chart.clickForDayDetail"),
        }),
        yearBoundary: { boundaries },
      },
      scales: {
        x: {
          ticks: {
            maxRotation: 0,
            autoSkip: true,
            callback: function (val) {
              const lbl = this.getLabelForValue(val);
              const d = new Date(lbl);
              if (isYear) return d.toLocaleDateString(state.locale, { month: "short" });
              return d.toLocaleDateString(state.locale, { day: "2-digit", month: "2-digit" });
            },
          },
        },
        y: {
          beginAtZero: true,
          ticks: { callback: v => v + " kWh" },
        },
      },
    },
    plugins: [yearBoundaryPlugin],
  });
}

function renderMonthlyHistory(data) {
  const months = data.months || [];
  const labels = months.map(m => m.month);
  const series = months.map(m => m.kwh);

  const thisYear = new Date().getFullYear();
  const backgroundColors = labels.map(label => {
    const y = parseInt(label.substring(0, 4), 10);
    return y === thisYear ? COLORS.accent + "cc" : COLORS.accent + "55";
  });

  const boundaries = [];
  let previousYear = null;
  labels.forEach(label => {
    const y = parseInt(label.substring(0, 4), 10);
    if (previousYear !== null && y !== previousYear) {
      boundaries.push({ label: label, year: y });
    }
    previousYear = y;
  });

  if (historyChart) historyChart.destroy();
  const ctx = document.getElementById("chart-history").getContext("2d");

  historyChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "kWh",
        data: series,
        backgroundColor: backgroundColors,
        borderColor: COLORS.accent,
        borderWidth: 1,
        borderRadius: 3,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: tooltipStyle({
          title: items => fmt.monthYear(items[0].label),
          label: item => ` ${item.parsed.y.toFixed(2)} kWh`,
        }),
        yearBoundary: { boundaries },
      },
      scales: {
        x: {
          ticks: {
            maxRotation: 0,
            autoSkip: false,
            callback: function (val) {
              const lbl = this.getLabelForValue(val);
              return fmt.shortMonthYear(lbl);
            },
          },
        },
        y: {
          beginAtZero: true,
          ticks: { callback: v => v + " kWh" },
        },
      },
    },
    plugins: [yearBoundaryPlugin],
  });
}

function timeChartOptions(timeFormat) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { intersect: false, mode: "index" },
    plugins: {
      legend: { display: false },
      tooltip: tooltipStyle({
        title: items => new Date(items[0].parsed.x).toLocaleString(state.locale, {
          hour: "2-digit", minute: "2-digit",
        }),
        label: item => ` ${Math.round(item.parsed.y)} W`,
      }),
    },
    scales: {
      x: {
        type: "time",
        time: { displayFormats: { minute: timeFormat, hour: timeFormat } },
        ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 12 },
      },
      y: {
        beginAtZero: true,
        ticks: { callback: v => v + " W" },
      },
    },
  };
}

function tooltipStyle(callbacks) {
  return {
    backgroundColor: COLORS.tooltipBg,
    titleColor: COLORS.text,
    bodyColor: COLORS.text,
    titleFont: { family: "JetBrains Mono", weight: 500 },
    bodyFont: { family: "JetBrains Mono" },
    borderColor: COLORS.border,
    borderWidth: 1,
    padding: 10,
    callbacks,
  };
}


// --- Range, granularity, day-picker controls -------------------------

document.querySelectorAll(".range-tab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".range-tab").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    state.currentRange = btn.dataset.range;
    loadHistoryChart(state.currentRange);
  });
});

document.querySelectorAll(".gran-tab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".gran-tab").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    // Granularity tabs are reused between "year" (daily/monthly) and
    // "multiyear" (monthly/yearly) modes — store the value in the right
    // state slot depending on which range is active.
    if (state.currentRange === "multiyear") {
      state.multiYearGranularity = btn.dataset.gran;
      loadHistoryChart("multiyear");
    } else {
      state.yearGranularity = btn.dataset.gran;
      if (state.currentRange === "year") {
        loadHistoryChart("year");
      }
    }
  });
});

document.getElementById("day-prev")?.addEventListener("click", () => shiftViewedDay(-1));
document.getElementById("day-next")?.addEventListener("click", () => shiftViewedDay(+1));
document.getElementById("day-today")?.addEventListener("click", () => setViewedDay(null));


// --- Theme management (smart toggle: system default, click toggles light/dark) ---
//
// State model:
// - Initial:  localStorage empty → follows OS via prefers-color-scheme
// - Click:    sets explicit "light" or "dark" — whichever is the OPPOSITE
//             of what's currently being rendered
// - Icon:     shows the target of the next click. Currently rendered dark?
//             show a Sun (click → light). Currently light? show a Moon.
//
// There is intentionally no UI path back to "system" — that mode is only
// the silent default for first-time visitors. Clearing the override would
// require a manual localStorage.removeItem in dev tools, which is fine
// for the small audience that cares.

const THEME_KEY = "ez1-theme";

function getStoredOverride() {
  const stored = localStorage.getItem(THEME_KEY);
  return stored === "light" || stored === "dark" ? stored : null;
}

function getResolvedTheme() {
  // What's actually being rendered right now?
  const stored = getStoredOverride();
  if (stored) return stored;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyStoredTheme() {
  const stored = getStoredOverride();
  if (stored) {
    document.documentElement.setAttribute("data-theme", stored);
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
  updateThemeToggleIcon();
}

function updateThemeToggleIcon() {
  const resolved = getResolvedTheme();
  // Icon shows what you'll GET after clicking. In dark → show sun (light target).
  // In light → show moon (dark target).
  const targetIcon = resolved === "dark" ? "light" : "dark";
  document.querySelectorAll(".theme-icon").forEach(el => {
    el.style.display = (el.dataset.icon === targetIcon) ? "" : "none";
  });
  const btn = document.getElementById("theme-toggle");
  if (btn) {
    const labelKey = resolved === "dark" ? "theme.switchToLight" : "theme.switchToDark";
    const label = window.i18n.t(state.lang, labelKey);
    btn.title = label;
    btn.setAttribute("aria-label", label);
  }
}

function toggleTheme() {
  // Toggle to the opposite of what's currently rendered (whether that came
  // from system or an explicit override)
  const next = getResolvedTheme() === "dark" ? "light" : "dark";
  localStorage.setItem(THEME_KEY, next);
  applyStoredTheme();
  refreshChartColors();
  if (todayChart || historyChart) {
    loadTodayChart();
    loadHistoryChart(state.currentRange);
  }
}

// React to OS theme changes while user has no explicit override
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (!getStoredOverride()) {
    updateThemeToggleIcon();
    refreshChartColors();
    if (todayChart || historyChart) {
      loadTodayChart();
      loadHistoryChart(state.currentRange);
    }
  }
});

document.getElementById("theme-toggle")?.addEventListener("click", toggleTheme);

// Apply stored theme FIRST so the data-theme attribute is set on <html>,
// THEN refresh chart colors so Chart.defaults reads the correct theme's
// CSS variables (otherwise grid/border can render black in light theme
// on the very first pageload, before any user interaction).
applyStoredTheme();
refreshChartColors();


function scheduleTimers() {
  const active = state.statusState === "online";
  // Live refresh only when viewing today (otherwise data is historical = static)
  const liveOk = state.viewedDay === null;
  const liveInterval = active ? REFRESH_LIVE_ACTIVE : REFRESH_LIVE_IDLE;
  const histInterval = active ? REFRESH_HIST_ACTIVE : REFRESH_HIST_IDLE;

  if (liveTimer)    clearInterval(liveTimer);
  if (statsTimer)   clearInterval(statsTimer);
  if (todayTimer)   clearInterval(todayTimer);
  if (historyTimer) clearInterval(historyTimer);

  liveTimer    = setInterval(loadLive, liveInterval);
  statsTimer   = setInterval(loadStats, histInterval);
  if (liveOk) {
    todayTimer = setInterval(loadTodayChart, histInterval);
  }
  historyTimer = setInterval(() => loadHistoryChart(state.currentRange), histInterval * 5);
}


async function init() {
  // Translate static UI text and initialize the day picker BEFORE the first
  // backend roundtrip. Without this, the day picker input would stay empty
  // until /api/live returned. Both are re-applied inside loadLive() in case
  // the backend's DEFAULT_LANG env forces a different language than the
  // browser one we guessed.
  window.i18n.applyTranslations(state.lang);
  ensureDayPicker();
  updateDayPickerLabels();
  updateDayPickerButtons();   // UI-only, no fetches yet

  await loadLive();
  await loadStats();
  await loadTodayChart();
  await loadHistoryChart(state.currentRange);
  scheduleTimers();
}

init();
