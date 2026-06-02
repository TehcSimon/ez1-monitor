/* EZ1 Monitor frontend */

const REFRESH_MS = 10_000;   // poll API every 10 s
const HISTORY_REFRESH_MS = 60_000;
let installKwp = 1.0;
let maxPowerW = 800;
let currentRange = "month";
let todayChart, historyChart;

const fmt = {
  W:   v => (v == null ? "—" : Number(v).toFixed(0)),
  kWh: v => (v == null ? "—" : Number(v).toFixed(2)),
  pct: v => (v == null ? "—" : Number(v).toFixed(0)),
  dateDE: ts => new Date(ts * 1000).toLocaleDateString("de-DE", { weekday:"long", day:"2-digit", month:"long", year:"numeric" }),
  timeDE: ts => new Date(ts * 1000).toLocaleString("de-DE", { hour:"2-digit", minute:"2-digit", second:"2-digit" }),
};

const css = getComputedStyle(document.documentElement);
const COLORS = {
  accent: css.getPropertyValue("--accent").trim() || "#f59e0b",
  accentWarm: css.getPropertyValue("--accent-warm").trim() || "#fb923c",
  text: css.getPropertyValue("--text-primary").trim() || "#f4ede0",
  muted: css.getPropertyValue("--text-muted").trim() || "#6f6353",
  border: css.getPropertyValue("--border").trim() || "#2a241c",
};

// ---------- Chart defaults --------------------------------------------
Chart.defaults.color = COLORS.muted;
Chart.defaults.font.family = "'JetBrains Mono', monospace";
Chart.defaults.font.size = 11;
Chart.defaults.borderColor = COLORS.border;
Chart.defaults.scale.grid.color = COLORS.border;
Chart.defaults.scale.grid.tickColor = COLORS.border;

// ---------- Live data --------------------------------------------------
async function loadLive() {
  try {
    const res = await fetch("/api/live");
    const data = await res.json();

    if (data.config) {
      installKwp = data.config.install_kwp || 1.0;
      document.getElementById("footer-interval").textContent = data.config.poll_interval;
      document.getElementById("footer-inverter").textContent = data.config.inverter_ip;
    }
    if (data.device) {
      maxPowerW = data.device.max_power || 800;
      document.getElementById("device-subtitle").textContent =
        `${data.device.device_id || data.device.serial_number || "EZ1-M"} · max ${maxPowerW} W`;
    }

    const m = data.latest;
    if (!m) {
      setStatus(false, "keine Daten");
      return;
    }

    const totalW = (m.p1 || 0) + (m.p2 || 0);
    document.getElementById("current-power").textContent = fmt.W(totalW);
    document.getElementById("pv1-power").textContent = fmt.W(m.p1);
    document.getElementById("pv2-power").textContent = fmt.W(m.p2);
    document.getElementById("pv1-energy").textContent = fmt.kWh(m.e1);
    document.getElementById("pv2-energy").textContent = fmt.kWh(m.e2);

    const pct = Math.min(100, (totalW / maxPowerW) * 100);
    document.getElementById("power-bar").style.width = pct + "%";
    document.getElementById("power-pct").textContent = fmt.pct(pct) + " %";
    document.getElementById("power-max").textContent = "/ " + maxPowerW + " W";

    document.getElementById("today-date").textContent = fmt.dateDE(m.timestamp);
    document.getElementById("footer-last").textContent = fmt.timeDE(m.timestamp);

    const age = Date.now() / 1000 - m.timestamp;
    const isOnline = !!m.online && age < 300;
    setStatus(isOnline, isOnline ? "online" : `letzte Daten vor ${Math.round(age/60)} min`);
  } catch (e) {
    console.error("loadLive:", e);
    setStatus(false, "Verbindungsfehler");
  }
}

function setStatus(online, text) {
  const pill = document.getElementById("status-pill");
  pill.classList.toggle("online", online);
  pill.classList.toggle("offline", !online);
  document.getElementById("status-text").textContent = text;
}

// ---------- Stats ------------------------------------------------------
async function loadStats() {
  try {
    const res = await fetch("/api/stats");
    const s = await res.json();

    document.getElementById("stat-today").textContent = fmt.kWh(s.today_kwh);
    document.getElementById("stat-yesterday").textContent = fmt.kWh(s.yesterday_kwh);
    document.getElementById("stat-week").textContent = fmt.kWh(s.this_week_kwh);
    document.getElementById("stat-last-week").textContent = fmt.kWh(s.last_week_kwh);
    document.getElementById("stat-month").textContent = fmt.kWh(s.this_month_kwh);
    document.getElementById("stat-last-month").textContent = fmt.kWh(s.last_month_kwh);
    document.getElementById("stat-peak").textContent = fmt.W(s.peak_w_today);

    renderCompare("stat-today-compare", s.today_kwh, s.yesterday_kwh);
    renderCompare("stat-week-compare",  s.this_week_kwh,  s.last_week_kwh);
    renderCompare("stat-month-compare", s.this_month_kwh, s.last_month_kwh);

    document.getElementById("lifetime-kwh").textContent = fmt.kWh(s.total_kwh);
    document.getElementById("lifetime-co2").textContent = (s.co2_saved_kg || 0).toFixed(1);
    document.getElementById("lifetime-eur").textContent = (s.money_saved_eur || 0).toFixed(2);
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

// ---------- Charts -----------------------------------------------------
async function loadTodayChart() {
  try {
    const res = await fetch("/api/history?range=day");
    const data = await res.json();
    const points = (data.points || []).filter(p => p.online);
    const labels = points.map(p => p.timestamp * 1000);
    const series = points.map(p => (p.p1 || 0) + (p.p2 || 0));

    if (todayChart) todayChart.destroy();
    const ctx = document.getElementById("chart-today").getContext("2d");

    // Amber gradient
    const grad = ctx.createLinearGradient(0, 0, 0, 280);
    grad.addColorStop(0, COLORS.accent + "55");
    grad.addColorStop(1, COLORS.accent + "00");

    todayChart = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: "Leistung",
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
      options: chartOptionsTime("HH:mm"),
    });
  } catch (e) {
    console.error("loadTodayChart:", e);
  }
}

async function loadHistoryChart(range) {
  try {
    const res = await fetch(`/api/history?range=${range}`);
    const data = await res.json();

    // For historical view, show daily energy (e1+e2 max per bucket) as bars
    const isYear = range === "year";
    const isMonth = range === "month";
    const points = data.points || [];

    // Aggregate to daily kWh
    const byDay = new Map();
    for (const p of points) {
      if (!p.online) continue;
      const d = new Date(p.timestamp * 1000);
      const key = d.toISOString().slice(0,10);
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
          tooltip: {
            backgroundColor: "#000",
            titleFont: { family: "JetBrains Mono", weight: 500 },
            bodyFont:  { family: "JetBrains Mono" },
            borderColor: COLORS.border,
            borderWidth: 1,
            padding: 10,
            callbacks: {
              title: items => new Date(items[0].label).toLocaleDateString("de-DE", { weekday:"short", day:"2-digit", month:"short" }),
              label: item => ` ${item.parsed.y.toFixed(2)} kWh`,
            },
          },
        },
        scales: {
          x: {
            ticks: {
              maxRotation: 0,
              autoSkip: true,
              callback: function(val, idx) {
                const lbl = this.getLabelForValue(val);
                const d = new Date(lbl);
                if (isYear) return d.toLocaleDateString("de-DE", { month:"short" });
                return d.toLocaleDateString("de-DE", { day:"2-digit", month:"2-digit" });
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

function chartOptionsTime(timeFormat) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { intersect: false, mode: "index" },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: "#000",
        titleFont: { family: "JetBrains Mono", weight: 500 },
        bodyFont:  { family: "JetBrains Mono" },
        borderColor: COLORS.border,
        borderWidth: 1,
        padding: 10,
        callbacks: {
          title: items => new Date(items[0].parsed.x).toLocaleString("de-DE", { hour:"2-digit", minute:"2-digit" }),
          label: item => ` ${item.parsed.y.toFixed(0)} W`,
        },
      },
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

// ---------- Range tabs -------------------------------------------------
document.querySelectorAll(".range-tab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".range-tab").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    currentRange = btn.dataset.range;
    loadHistoryChart(currentRange);
  });
});

// ---------- Bootstrap --------------------------------------------------
async function init() {
  await loadLive();
  await loadStats();
  await loadTodayChart();
  await loadHistoryChart(currentRange);

  setInterval(loadLive, REFRESH_MS);
  setInterval(loadStats, HISTORY_REFRESH_MS);
  setInterval(loadTodayChart, HISTORY_REFRESH_MS);
  setInterval(() => loadHistoryChart(currentRange), HISTORY_REFRESH_MS * 5);
}

init();
