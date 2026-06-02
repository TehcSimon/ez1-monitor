/* EZ1 Monitor — UI translations */

const TRANSLATIONS = {
    de: {
        app: {
            subtitleLoading: "— wird geladen —",
        },
        status: {
            connecting: "verbinde …",
            online: "online",
            offline: "offline",
            noData: "keine Daten",
            connectionError: "Verbindungsfehler",
            staleData: "letzte Daten vor {minutes} min",
        },
        hero: {
            label: "Aktuelle Leistung",
            pv1: "PV 1",
            pv2: "PV 2",
            energyToday: "kWh heute",
            maxPower: "/ {max} W",
        },
        chart: {
            todayTitle: "Tagesverlauf",
            historyTitle: "Verlauf",
            rangeWeek: "Woche",
            rangeMonth: "Monat",
            rangeYear: "Jahr",
            tooltipPower: "Leistung",
            tooltipKwh: "kWh",
        },
        stats: {
            today: "Heute",
            week: "Diese Woche",
            month: "Dieser Monat",
            peakToday: "Spitzenwert heute",
            yesterday: "gestern",
            lastWeek: "letzte Woche",
            lastMonth: "letzter Monat",
            throttleMax: "max. Drosselung: {max} W",
        },
        lifetime: {
            totalGeneration: "Gesamterzeugung",
            sinceStart: "seit Inbetriebnahme",
            co2Avoided: "CO₂ gespart",
            co2BasedOn: "basierend auf {g} g/kWh",
            moneySaved: "Ersparnis",
            moneyBasedOn: "bei {price}/kWh",
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
            offline: "offline",
            noData: "no data",
            connectionError: "connection error",
            staleData: "last data {minutes} min ago",
        },
        hero: {
            label: "Current Power",
            pv1: "PV 1",
            pv2: "PV 2",
            energyToday: "kWh today",
            maxPower: "/ {max} W",
        },
        chart: {
            todayTitle: "Today's Curve",
            historyTitle: "History",
            rangeWeek: "Week",
            rangeMonth: "Month",
            rangeYear: "Year",
            tooltipPower: "Power",
            tooltipKwh: "kWh",
        },
        stats: {
            today: "Today",
            week: "This Week",
            month: "This Month",
            peakToday: "Peak Today",
            yesterday: "yesterday",
            lastWeek: "last week",
            lastMonth: "last month",
            throttleMax: "max throttle: {max} W",
        },
        lifetime: {
            totalGeneration: "Total Generation",
            sinceStart: "since commissioning",
            co2Avoided: "CO₂ Avoided",
            co2BasedOn: "based on {g} g/kWh",
            moneySaved: "Money Saved",
            moneyBasedOn: "at {price}/kWh",
        },
        footer: {
            updateEvery: "Update every {s} s",
            lastReading: "last reading",
        },
    },
};

/**
 * Look up a translation string by dot-path, e.g. "stats.today".
 * Falls back to the key itself if not found.
 * Optional substitutions are passed as an object: { minutes: 5 }
 */
function t(lang, key, vars = {}) {
    const dict = TRANSLATIONS[lang] || TRANSLATIONS.en;
    const value = key.split(".").reduce((acc, k) => (acc && acc[k] !== undefined ? acc[k] : null), dict);
    let str = value == null ? key : String(value);
    for (const [k, v] of Object.entries(vars)) {
        str = str.replace(new RegExp(`\\{${k}\\}`, "g"), v);
    }
    return str;
}

/**
 * Walk the document and replace every [data-i18n="key"] element's textContent
 * with the localised string.
 */
function applyTranslations(lang) {
    document.documentElement.lang = lang;
    document.querySelectorAll("[data-i18n]").forEach(el => {
        const key = el.getAttribute("data-i18n");
        el.textContent = t(lang, key);
    });
}

window.i18n = { t, applyTranslations, TRANSLATIONS };