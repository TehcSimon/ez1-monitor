/* EZ1 Monitor — dashboard frontend */

const REFRESH_LIVE_ACTIVE = 10_000;
const REFRESH_LIVE_IDLE   = 60_000;
const REFRESH_HIST_ACTIVE = 60_000;
const REFRESH_HIST_IDLE   = 300_000;

const state = {
  lang: "en",
  locale: "en-US",
  currency: "USD",
  pricePerKwh: 0.35,
  co2KgPerKwh: 0.38,
  installKwp: 1.0,
  maxPowerW: 800,
  currentRange: "month",
  yearGranularity: "daily",   // "daily" or "monthly"
  statusState: "noData",
  pollInterval: 60,
};

let todayChart, historyChart;
let liveTimer, statsTimer, todayTimer, historyTimer;

const fmt = {
  power: v => (v == null ? "—" : Math.round(Number(v)).toString()),
  kwh:   v => (v == null ? "—" : Number(v).toFixed(2)),
  pct:   v => (v == null ? "—" : Math.round(Number(v)).toString()),
  date:  ts => new Date(ts * 1000).toLocaleDateString(state.locale, {
    weekday: "long", day: "2-digit", month: "long", year: "numeric"
  }),
  time:  ts => new Date(ts * 1000).toLocaleTimeString(state.locale, {
    hour: "2-digit", minute: "2-digit", second: "2-digit"
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
};

function localDateKey(date) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

const css = getComputedStyle(document.documentElement);
const COLORS = {
  accent: css.getPropertyValue("--accent").trim() || "#f59e0b",
  accentWarm: css.getPropertyValue("--accent-warm").trim() || "#fb923c",
  text: css.getPropertyValue("--text-primary").trim() || "#f4ede0",
  muted: css.getPropertyValue("--text-muted").trim() || "#6f6353",
  border: css.getPropertyValue("--border").trim() || "#2a241c",
};

Chart.defaults.color = COLORS.muted;
Chart.defaults.font.family = "'JetBrains Mono', monospace";
Chart.defaults.font.size = 11;
Chart.defaults.borderColor = COLORS.border;
Chart.defaults.scale.grid.color = COLORS.border;
Chart.defaults.scale.grid.tickColor = COLORS.border;


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
      // Get the X coordinate corresponding to the label
      const xPos = xAxis.getPixelForValue(b.label);
      if (xPos < chart.chartArea.left || xPos > chart.chartArea.right) return;
      // Dashed vertical line
      ctx.strokeStyle = COLORS.accentWarm + "aa";
      ctx.lineWidth = 1;
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      ctx.moveTo(xPos, top);
      ctx.lineTo(xPos, bottom);
      ctx.stroke();
      // Year label
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

      window.i18n.applyTranslations(state.lang);
      updateDynamicLabels();

      document.getElementById("footer-inverter").textContent =
        data.config.inverter_ip;
    }

    if (data.device) {
      state.maxPowerW = data.device.max_power || 800;
      const deviceId = data.device.device_id || data.device.serial_number || "EZ1-M";
      document.getElementById("device-subtitle").textContent =
        `${deviceId} · max ${state.maxPowerW} W`;
    }

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
        document.getElementById("today-date").textContent = fmt.date(m.timestamp);
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
  const co2Sub = document.getElementById("lifetime-co2-sub");
  if (co2Sub) {
    co2Sub.textContent = window.i18n.t(state.lang, "lifetime.co2BasedOn", {
      g: Math.round(state.co2KgPerKwh * 1000),
    });
  }
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


// --- Stats -------------------------------------------------------------

async function loadStats() {
  try {
    const res = await fetch("/api/stats");
    const s = await res.json();

    // Today
    document.getElementById("stat-today").textContent = fmt.kwh(s.today_kwh);
    document.getElementById("stat-yesterday-until-now").textContent = fmt.kwh(s.yesterday_until_now_kwh);
    document.getElementById("stat-yesterday-full").textContent = fmt.kwh(s.yesterday_full_kwh);
    renderCompare("stat-today-compare", s.today_kwh, s.yesterday_until_now_kwh);

    // Week
    document.getElementById("stat-week").textContent = fmt.kwh(s.this_week_kwh);
    document.getElementById("stat-last-week-until-now").textContent = fmt.kwh(s.last_week_until_now_kwh);
    document.getElementById("stat-last-week-full").textContent = fmt.kwh(s.last_week_full_kwh);
    renderCompare("stat-week-compare", s.this_week_kwh, s.last_week_until_now_kwh);

    // Month
    document.getElementById("stat-month").textContent = fmt.kwh(s.this_month_kwh);
    document.getElementById("stat-last-month-until-progress").textContent = fmt.kwh(s.last_month_until_progress_kwh);
    document.getElementById("stat-last-month-full").textContent = fmt.kwh(s.last_month_full_kwh);
    renderCompare("stat-month-compare", s.this_month_kwh, s.last_month_until_progress_kwh);

    // Year-over-year on month card
    document.getElementById("stat-same-month-ly").textContent = fmt.kwh(s.same_month_last_year_kwh);
    document.getElementById("stat-same-month-ly-total").textContent = fmt.kwh(s.same_month_last_year_total_kwh);
    document.getElementById("stat-same-month-ly-label").textContent = fmt.monthYear(s.same_month_last_year_iso);
    renderCompare("stat-same-month-ly-compare", s.this_month_kwh, s.same_month_last_year_kwh);
    const totalRow = document.getElementById("stat-same-month-ly-total-row");
    if (totalRow) {
      totalRow.style.display = (s.same_month_last_year_total_kwh > 0) ? "" : "none";
    }

    // Year
    document.getElementById("stat-year").textContent = fmt.kwh(s.this_year_kwh);
    document.getElementById("stat-last-year-ytd").textContent = fmt.kwh(s.last_year_ytd_kwh);
    renderCompare("stat-year-compare", s.this_year_kwh, s.last_year_ytd_kwh);

    // Hero
    document.getElementById("hero-peak-value").textContent = fmt.power(s.peak_w_today);

    // Lifetime
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


// --- Today chart ------------------------------------------------------

async function loadTodayChart() {
  try {
    const res = await fetch("/api/history?range=day");
    const data = await res.json();
    const points = (data.points || []).filter(p => p.online);
    const labels = points.map(p => p.timestamp * 1000);
    const series = points.map(p => (p.p1 || 0) + (p.p2 || 0));

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

    // Show/hide the granularity tabs based on current range
    const granTabs = document.getElementById("granularity-tabs");
    if (granTabs) granTabs.style.display = isYear ? "" : "none";
  } catch (e) {
    console.error("loadHistoryChart:", e);
  }
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

  // For the year view: dim previous-year bars and find year boundaries
  const thisYear = new Date().getFullYear();
  const boundaries = [];
  let backgroundColors;
  if (isYear && labels.length > 0) {
    backgroundColors = labels.map(label => {
      const y = parseInt(label.substring(0, 4), 10);
      return y === thisYear ? COLORS.accent + "cc" : COLORS.accent + "55";
    });
    // Find every Jan 1 within the visible range
    let previousYear = null;
    labels.forEach(label => {
      const y = parseInt(label.substring(0, 4), 10);
      if (previousYear !== null && y !== previousYear) {
        boundaries.push({ label: label, year: y });
      }
      previousYear = y;
    });
  } else {
    backgroundColors = COLORS.accent + "cc";
  }

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
          title: items => new Date(items[0].label).toLocaleDateString(state.locale, {
            weekday: "short", day: "2-digit", month: "short", year: "numeric",
          }),
          label: item => ` ${item.parsed.y.toFixed(2)} kWh`,
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
  const labels = months.map(m => m.month);  // "2026-06"
  const series = months.map(m => m.kwh);

  // Dim months from previous year
  const thisYear = new Date().getFullYear();
  const backgroundColors = labels.map(label => {
    const y = parseInt(label.substring(0, 4), 10);
    return y === thisYear ? COLORS.accent + "cc" : COLORS.accent + "55";
  });

  // Find year boundaries (Jan of current year, etc.)
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
    backgroundColor: "#000",
    titleFont: { family: "JetBrains Mono", weight: 500 },
    bodyFont: { family: "JetBrains Mono" },
    borderColor: COLORS.border,
    borderWidth: 1,
    padding: 10,
    callbacks,
  };
}


// --- Range + granularity tabs -----------------------------------------

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
    state.yearGranularity = btn.dataset.gran;
    if (state.currentRange === "year") {
      loadHistoryChart("year");
    }
  });
});


function scheduleTimers() {
  const active = state.statusState === "online";
  const liveInterval = active ? REFRESH_LIVE_ACTIVE : REFRESH_LIVE_IDLE;
  const histInterval = active ? REFRESH_HIST_ACTIVE : REFRESH_HIST_IDLE;

  if (liveTimer) clearInterval(liveTimer);
  if (statsTimer) clearInterval(statsTimer);
  if (todayTimer) clearInterval(todayTimer);
  if (historyTimer) clearInterval(historyTimer);

  liveTimer    = setInterval(loadLive, liveInterval);
  statsTimer   = setInterval(loadStats, histInterval);
  todayTimer   = setInterval(loadTodayChart, histInterval);
  historyTimer = setInterval(() => loadHistoryChart(state.currentRange), histInterval * 5);
}


async function init() {
  await loadLive();
  await loadStats();
  await loadTodayChart();
  await loadHistoryChart(state.currentRange);
  scheduleTimers();
}

init();
