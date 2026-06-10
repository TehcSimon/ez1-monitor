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
      avgDuringProduction: "Ø",
    },
    chart: {
      todayTitle: "Tagesverlauf",
      historyTitle: "Verlauf",
      rangeWeek: "Woche",
      rangeMonth: "Monat",
      rangeYear: "Jahr",
      rangeMultiyear: "Gesamt",
      granDaily: "Tage",
      granMonthly: "Monate",
      granYearly: "Jahre",
      tooltipPower: "Leistung",
      tooltipKwh: "kWh",
      previousDay: "Vorheriger Tag",
      nextDay: "Nächster Tag",
      openCalendar: "Kalender öffnen",
      todayButton: "Heute",
      noDataForDay: "Keine Daten für diesen Tag",
      clickForDayDetail: "Klick für Tagesverlauf",
    },
    stats: {
      today: "Heute",
      week: "Diese Woche",
      month: "Dieser Monat",
      year: "Dieses Jahr",
      yesterdayUntilNow: "Vergleichszeitraum gestern",
      yesterdayFull: "gestern gesamt",
      lastWeekUntilNow: "Vergleichszeitraum letzte Woche",
      lastWeekFull: "letzte Woche gesamt",
      lastMonthUntilProgress: "Vergleichszeitraum letzter Monat",
      lastMonthFull: "letzter Monat gesamt",
      lastYearYtd: "Vergleichszeitraum Vorjahr",
      lastYearFull: "Vorjahr gesamt",
      sameMonthLySamePeriod: "Vergleichszeitraum {month}",
      sameMonthLyTotal: "{month} gesamt",
      fullMonth: "gesamt",
      infoTodayTitle: "Wie wird verglichen?",
      infoToday: "Die heutige Produktion wird mit gestern bis zur gleichen Uhrzeit verglichen. So bekommst du einen fairen Trend statt eines schiefen Tageswerts am Morgen. „gestern gesamt“ zeigt, wie der Vortag insgesamt gelaufen ist.",
      infoWeek: "Vergleicht die laufende Woche mit der Vorwoche bis zum gleichen Wochentag und zur gleichen Uhrzeit (Mittwoch 14 Uhr → letzten Mittwoch 14 Uhr).",
      infoMonth: "Vergleicht den aktuellen Monat mit dem gleichen Tag im Vormonat. Wenn der Vormonat kürzer ist (z. B. Februar mit 28 Tagen), wird auf den letzten Tag des Vormonats begrenzt — kurze Monate können dann den Vergleich rechnerisch leicht „benachteiligen“.",
      infoYear: "Vergleicht das laufende Jahr mit dem gleichen Tag im Vorjahr. „Vorjahr gesamt“ zeigt zusätzlich das komplette Vorjahr als Anker.",
    },
    hof: {
      title: "Hall of Fame",
      bestDay: "Bester Tag",
      bestWeek: "Beste Woche",
      bestMonth: "Bester Monat",
      bestYear: "Bestes Jahr",
      newBadge: "NEU",
    },
    lifetime: {
      totalGeneration: "Gesamterzeugung",
      sinceStart: "seit Inbetriebnahme",
      co2Avoided: "CO₂ gespart",
      co2BasedOn: "basierend auf {g} g/kWh",
      // CO2 source variants
      co2Live: "Live ({zone}) · {g} g/kWh · {time} Uhr",
      co2Stale: "Letzter Wert ({zone}) · {g} g/kWh · vor {hours} h",
      co2Avg: "Mittelwert ({zone}) · {g} g/kWh · seit {count} Polls",
      co2Static: "Statisch · {g} g/kWh",
      co2GridMix: "Grid-Mix: {fossil}% fossil · {clean}% sauber",
      moneySaved: "Ersparnis",
      moneyBasedOn: "bei {price}",
    },
    footer: {
      updateEvery: "Update alle {s} s",
      lastReading: "letzte Messung",
    },
    theme: {
      switchToLight: "Zum hellen Theme wechseln",
      switchToDark: "Zum dunklen Theme wechseln",
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
      avgDuringProduction: "Ø",
    },
    chart: {
      todayTitle: "Today's Curve",
      historyTitle: "History",
      rangeWeek: "Week",
      rangeMonth: "Month",
      rangeYear: "Year",
      rangeMultiyear: "All-time",
      granDaily: "Daily",
      granMonthly: "Monthly",
      granYearly: "Yearly",
      tooltipPower: "Power",
      tooltipKwh: "kWh",
      previousDay: "Previous day",
      nextDay: "Next day",
      openCalendar: "Open calendar",
      todayButton: "Today",
      noDataForDay: "No data for this day",
      clickForDayDetail: "Click for day detail",
    },
    stats: {
      today: "Today",
      week: "This Week",
      month: "This Month",
      year: "This Year",
      yesterdayUntilNow: "same period yesterday",
      yesterdayFull: "yesterday total",
      lastWeekUntilNow: "same period last week",
      lastWeekFull: "last week total",
      lastMonthUntilProgress: "same period last month",
      lastMonthFull: "last month total",
      lastYearYtd: "same period last year",
      lastYearFull: "last year total",
      sameMonthLySamePeriod: "same period {month}",
      sameMonthLyTotal: "{month} total",
      fullMonth: "full month",
      infoTodayTitle: "How is this compared?",
      infoToday: "Today's production is compared to yesterday up to the same time. This gives you a fair trend instead of a skewed value in the morning. \"yesterday total\" shows how the previous day went overall.",
      infoWeek: "Compares the current week to the previous week up to the same weekday and time (Wednesday 2 PM → last Wednesday 2 PM).",
      infoMonth: "Compares the current month to the same day in the previous month. If the previous month was shorter (e.g. February with 28 days), it's clamped to the last day of that month — short months can put the comparison at a slight numerical disadvantage.",
      infoYear: "Compares the current year to the same day last year. \"last year total\" shows the entire previous year as an anchor.",
    },
    hof: {
      title: "Hall of Fame",
      bestDay: "Best Day",
      bestWeek: "Best Week",
      bestMonth: "Best Month",
      bestYear: "Best Year",
      newBadge: "NEW",
    },
    lifetime: {
      totalGeneration: "Total Generation",
      sinceStart: "since commissioning",
      co2Avoided: "CO₂ Avoided",
      co2BasedOn: "based on {g} g/kWh",
      // CO2 source variants
      co2Live: "Live ({zone}) · {g} g/kWh · {time}",
      co2Stale: "Last value ({zone}) · {g} g/kWh · {hours} h ago",
      co2Avg: "Average ({zone}) · {g} g/kWh · over {count} polls",
      co2Static: "Static · {g} g/kWh",
      co2GridMix: "Grid mix: {fossil}% fossil · {clean}% clean",
      moneySaved: "Money Saved",
      moneyBasedOn: "at {price}",
    },
    footer: {
      updateEvery: "Update every {s} s",
      lastReading: "last reading",
    },
    theme: {
      switchToLight: "Switch to light theme",
      switchToDark: "Switch to dark theme",
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
