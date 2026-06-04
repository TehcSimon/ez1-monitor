/* EZ1 Monitor — UI translations */

const TRANSLATIONS = {
  de: {
    app: {
      subtitleLoading: "— wird geladen —",
    },
    status: {
      connecting: "verbinde …",
      online: "online",
      standby: "standby",
      error: "Fehler",
      noData: "keine Daten",
      connectionError: "Verbindungsfehler",
    },
    hero: {
      label: "Aktuelle Leistung",
      pv1: "PV 1",
      pv2: "PV 2",
      energyToday: "kWh heute",
      maxPower: "/ {max} W",
      peakToday: "Spitzenwert heute",
    },
    chart: {
      todayTitle: "Tagesverlauf",
      historyTitle: "Verlauf",
      rangeWeek: "Woche",
      rangeMonth: "Monat",
      rangeYear: "Jahr",
      granDaily: "Tage",
      granMonthly: "Monate",
      tooltipPower: "Leistung",
      tooltipKwh: "kWh",
    },
    stats: {
      today: "Heute",
      week: "Diese Woche",
      month: "Dieser Monat",
      year: "Dieses Jahr",
      yesterdayUntilNow: "gestern bis jetzt",
      yesterdayFull: "gestern gesamt",
      lastWeekUntilNow: "letzte Woche bis jetzt",
      lastWeekFull: "letzte Woche gesamt",
      lastMonthUntilProgress: "letzter Monat bis Stichtag",
      lastMonthFull: "letzter Monat gesamt",
      lastYearYtd: "Vorjahr bis heute",
      fullMonth: "gesamt",
    },
    lifetime: {
      totalGeneration: "Gesamterzeugung",
      sinceStart: "seit Inbetriebnahme",
      co2Avoided: "CO₂ gespart",
      co2BasedOn: "basierend auf {g} g/kWh",
      moneySaved: "Ersparnis",
      moneyBasedOn: "bei {price}",
    },
    footer: {
      updateEvery: "Update alle {s} s",
      lastReading: "letzte Messung",
    },
  },

  en: {
    app: {
      subtitleLoading: "— loading —",
    },
    status: {
      connecting: "connecting …",
      online: "online",
      standby: "standby",
      error: "error",
      noData: "no data",
      connectionError: "connection error",
    },
    hero: {
      label: "Current Power",
      pv1: "PV 1",
      pv2: "PV 2",
      energyToday: "kWh today",
      maxPower: "/ {max} W",
      peakToday: "Peak today",
    },
    chart: {
      todayTitle: "Today's Curve",
      historyTitle: "History",
      rangeWeek: "Week",
      rangeMonth: "Month",
      rangeYear: "Year",
      granDaily: "Daily",
      granMonthly: "Monthly",
      tooltipPower: "Power",
      tooltipKwh: "kWh",
    },
    stats: {
      today: "Today",
      week: "This Week",
      month: "This Month",
      year: "This Year",
      yesterdayUntilNow: "yesterday until now",
      yesterdayFull: "yesterday total",
      lastWeekUntilNow: "last week until now",
      lastWeekFull: "last week total",
      lastMonthUntilProgress: "last month until same day",
      lastMonthFull: "last month total",
      lastYearYtd: "last year YTD",
      fullMonth: "full month",
    },
    lifetime: {
      totalGeneration: "Total Generation",
      sinceStart: "since commissioning",
      co2Avoided: "CO₂ Avoided",
      co2BasedOn: "based on {g} g/kWh",
      moneySaved: "Money Saved",
      moneyBasedOn: "at {price}",
    },
    footer: {
      updateEvery: "Update every {s} s",
      lastReading: "last reading",
    },
  },
};

function t(lang, key, vars = {}) {
  const dict = TRANSLATIONS[lang] || TRANSLATIONS.en;
  const value = key.split(".").reduce(
    (acc, k) => (acc && acc[k] !== undefined ? acc[k] : null),
    dict
  );
  let str = value == null ? key : String(value);
  for (const [k, v] of Object.entries(vars)) {
    str = str.replace(new RegExp(`\\{${k}\\}`, "g"), v);
  }
  return str;
}

function applyTranslations(lang) {
  document.documentElement.lang = lang;
  document.querySelectorAll("[data-i18n]").forEach(el => {
    const key = el.getAttribute("data-i18n");
    el.textContent = t(lang, key);
  });
}

window.i18n = { t, applyTranslations, TRANSLATIONS };
