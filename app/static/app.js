/* EZ1 Monitor — dashboard frontend */

const REFRESH_MS = 10_000;
const HISTORY_REFRESH_MS = 60_000;

// Runtime state (filled from /api/live config)
const state = {
  lang: "en",
  locale: "en-US",
  currency: "USD",
  pricePerKwh: 0.35,
  co2KgPerKwh: 0.38,
  installKwp: 1.0,
  maxPowerW: 800,
  currentRange: "month",
};

let todayChart, historyChart;

// --- Formatting helpers ------------------------------------------------
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
    // Display style varies per locale: "30 ct/kWh" feels native in DE,
    // currency-prefixed "$0.30/kWh" feels native in EN.
    if (state.lang === "de" && state.currency === "EUR") {
      return `${Math.round(v * 100)} ct/kWh`;
    }
    const formatted = new Intl.NumberFormat(state.locale, {
      style: "currency",
      currency: state.currency,
      minimumFractionDigits: 2,
    }).format(v);
    return `${formatted}/kWh`;
  },
};

// --- Chart defaults ----------------------------------------------------
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


// --- Live data ---------------------------------------------------------
async function loadLive() {
  try {
    const res = await fetch("/api/live");
    const data = await res.json();

    // First-time setup of runtime state from server config
    if (data.config) {
      state.lang = data.config.language || "en";
      state.locale = state.lang === "de" ? "de-DE" : "en-US";
      state.currency = data.config.currency || "USD";
      state.pricePerKwh = data.config.price_per_kwh || 0.35;
      state.co2KgPerKwh = data.config.co2_kg_per_kwh || 0.38;
      state.installKwp = data.config.install_kwp || 1.0;

      window.i18n.applyTranslations(state.lang);
      updateDynamicLabels();

      document.getElementById("footer-interval").textContent = data.config.poll_interval;
      document.getElementById("footer-inverter").textContent = data.config.inverter_ip;
    }

    if (data.device) {
      state.maxPowerW = data.device.max_power || 800;
      const deviceId = data.device.device_id || data.device.serial_number || "EZ1-M";
      document.getElementById("device-subtitle").textContent =
          `${deviceId} · max ${state.maxPowerW} W`;
    }

    const m = data.latest;
    if (!m) {
      setStatus(false, window.i18n.t(state.lang, "status.noData"));
      return;
    }

    const totalW = (m.p1 || 0) + (m.p2 || 0);
    document.getElementById("current-power").textContent = fmt.power(totalW);
    document.getElementById("pv1-power").textContent = fmt.power(m.p1);
    document.getElementById("pv2-power").textContent = fmt.power(m.p2);
    document.getElementById("pv1-energy").textContent = fmt.kwh(m.e1);
    document.getElementById("pv2-energy").textContent = fmt.kwh(m.e2);

    const pct = Math.min(100, (totalW / state.maxPowerW) * 100);
    document.getElementById("power-bar").style.width = pct + "%";
    document.getElementById("power-pct").textContent = fmt.pct(pct) + " %";
    document.getElementById("power-max").textContent =
        window.i18n.t(state.lang, "hero.maxPower", { max: state.maxPowerW });

    document.getElementById("today-date").textContent = fmt.date(m.timestamp);
    document.getElementById("footer-last").textContent = fmt.time(m.timestamp);

    const age = Date.now() / 1000 - m.timestamp;
    const isOnline = !!m.online && age < 300;
    if (isOnline) {
      setStatus(true, window.i18n.t(state.lang, "status.online"));
    } else {
      setStatus(false, window.i18n.t(state.lang, "status.staleData", { minutes: Math.round(age / 60) }));
    }
  } catch (e) {
    console.error("loadLive:", e);
    setStatus(false, window.i18n.t(state.lang, "status.connectionError"));
  }
}

function updateDynamicLabels() {
  // Subtitles that depend on env-configured values
  const co2Sub = document.getElementById("lifetime-co2-sub");
  if (co2Sub) co2Sub.textContent = window.i18n.t(state.lang, "lifetime.co2BasedOn", {
    g: Math.round(state.co2KgPerKwh * 1000),
  });

  const moneySub = document.getElementById("lifetime-money-sub");
  if (moneySub) moneySub.textContent = window.i18n.t(state.lang, "lifetime.moneyBasedOn", {
    price: fmt.pricePerKwh(state.pricePerKwh),
  });

  const throttle = document.getElementById("stat-throttle");
  if (throttle) throttle.textContent = window.i18n.t(state.lang, "stats.throttleMax", {
    max: state.maxPowerW,
  });

  const footerUpdate = document.getElementById("footer-update-text");
  if (footerUpdate) {
    const interval = document.getElementById("footer-interval").textContent;
    footerUpdate.innerHTML = window.i18n.t(state.lang, "footer.updateEvery", {
      s: `<span id="footer-interval">${interval}</span>`,
    });
  }
}

function setStatus(online, text) {
  const pill = document.getElementById("status-pill");
  pill.classList.toggle("online", online);
  pill.classList.toggle("offline", !online);
  document.getElementById("status-text").textContent = text;
}


// --- Stats -------------------------------------------------------------
async function loadStats() {
  try {
    const res = await fetch("/api/stats");
    const s = await res.json();

    document.getElementById("stat-today").textContent = fmt.kwh(s.today_kwh);
    document.getElementById("stat-yesterday").textContent = fmt.kwh(s.yesterday_kwh);
    document.getElementById("stat-week").textContent = fmt.kwh(s.this_week_kwh);
    document.getElementById("stat-last-week").textContent = fmt.kwh(s.last_week_kwh);
    document.getElementById("stat-month").textContent = fmt.kwh(s.this_month_kwh);
    document.getElementById("stat-last-month").textContent = fmt.kwh(s.last_month_kwh);
    document.getElementById("stat-peak").textContent = fmt.power(s.peak_w_today);

    renderCompare("stat-today-compare", s.today_kwh, s.yesterday_kwh);
    renderCompare("stat-week-compare",  s.this_week_kwh,  s.last_week_kwh);
    renderCompare("stat-month-compare", s.this_month_kwh, s.last_month_kwh);

    document.getElementById("lifetime-kwh").textContent = fmt.kwh(s.total_kwh);
    document.getElementById("lifetime-co2").textContent = (s.co2_saved_kg || 0).toFixed(1);
    document.getElementById("lifetime-money").textContent = fmt.money(s.money_saved);
  } catch (e) {
    console.error("loadStats:", e);
  }
}

function renderCompare(elementId, current, previous) {
  const el = document.getElementById(elementId);
  if (!previous || previous === 0) {
    el.textContent = "—";
    el.className = "stat-compare";
    return;
  }
  const delta = current - previous;
  const pct = (delta / previous) * 100;
  const sign = delta >= 0 ? "▲" : "▼";
  el.textContent = `${sign} ${Math.abs(pct).toFixed(0)} % (${delta >= 0 ? "+" : ""}${delta.toFixed(2)} kWh)`;
  el.className = "stat-compare " + (delta >= 0 ? "up" : "down");
}


// --- Charts ------------------------------------------------------------
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

async function loadHistoryChart(range) {
  try {
    const res = await fetch(`/api/history?range=${range}`);
    const data = await res.json();
    const isYear = range === "year";
    const points = data.points || [];

    // Aggregate to daily kWh (max(e1+e2) per day)
    const byDay = new Map();
    for (const p of points) {
      if (!p.online) continue;
      const d = new Date(p.timestamp * 1000);
      const key = d.toISOString().slice(0, 10);
      const total = (p.e1 || 0) + (p.e2 || 0);
      if (!byDay.has(key) || byDay.get(key).max < total) {
        byDay.set(key, { ts: p.timestamp, max: total });
      }
    }
    const days = [...byDay.entries()].sort();
    const labels = days.map(([k]) => k);
    const series = days.map(([_, v]) => v.max);

    if (historyChart) historyChart.destroy();
    const ctx = document.getElementById("chart-history").getContext("2d");

    historyChart = new Chart(ctx, {
      type: "bar",
      data: {
        labels,
        datasets: [{
          label: "kWh",
          data: series,
          backgroundColor: COLORS.accent + "cc",
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
              weekday: "short", day: "2-digit", month: "short",
            }),
            label: item => ` ${item.parsed.y.toFixed(2)} kWh`,
          }),
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
    });
  } catch (e) {
    console.error("loadHistoryChart:", e);
  }
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


// --- Range tabs --------------------------------------------------------
document.querySelectorAll(".range-tab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".range-tab").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    state.currentRange = btn.dataset.range;
    loadHistoryChart(state.currentRange);
  });
});


// --- Bootstrap ---------------------------------------------------------
async function init() {
  await loadLive();
  await loadStats();
  await loadTodayChart();
  await loadHistoryChart(state.currentRange);

  setInterval(loadLive, REFRESH_MS);
  setInterval(loadStats, HISTORY_REFRESH_MS);
  setInterval(loadTodayChart, HISTORY_REFRESH_MS);
  setInterval(() => loadHistoryChart(state.currentRange), HISTORY_REFRESH_MS * 5);
}

init();