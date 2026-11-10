# Upgrading EZ1 Monitor

Upgrading is the same for every release: pull the new image and recreate the
container. Your data lives in the `/data` volume and is picked up as is.

```bash
# Docker
docker pull ghcr.io/tehcsimon/ez1-monitor:latest
docker rm -f ez1-monitor   # then re-run your original `docker run` command

# Docker Compose with the published image (`image: ghcr.io/tehcsimon/ez1-monitor:latest`)
docker compose pull && docker compose up -d

# Docker Compose building from this repo (`build: .`, the default in docker-compose.yml)
git pull && docker compose up -d --build

# Unraid: Docker tab → "Check for Updates" → "apply update"
```

Database changes run automatically on the first start of a new version and
are idempotent. Only these releases do anything noticeable on first start:

| Release | What happens on first start |
|---|---|
| v1.13.0 | `ALTER TABLE` adds the temperature, grid and daily counter columns, the startup backfill fills in the counters (instant, the backfill runs on every start anyway) |
| v1.9.1 | Drops a redundant index on `measurements` (instant) |
| v1.6.1 | `ALTER TABLE` adds the electricity-price and firmware columns (instant) |
| v1.6.0 | Creates `daily_aggregates` and backfills it — 1–3 s, up to ~15 s on a full 2-year install on a Raspberry Pi |
| v1.4.0 | `ALTER TABLE` adds the CO₂ columns, then backfills the aggregate averages |
| v1.1.0 | **Manual step:** the container runs as UID 1000 — see [below](#from-v10x-to-v110) |

The rest of this file is the release history, newest first.

## From v1.15.2 to v1.15.3

No manual steps, no database changes.

- **Temperature graph with a scale.** Round values with dotted lines on the
  left. The temperature line in the header's device details is gone, since
  the graph shows the same value.
- **Play skips the night.** It starts half an hour before sunrise and stops
  half an hour after sunset, at the same pace as before.
- **House button replay** runs at full speed for most of the day and brakes
  later and harder towards the end.
- **Larger compass rose** in the sun path, on phones a little larger.

## From v1.15.1 to v1.15.2

No manual steps, no database changes.

- **Temperature graph in the header.** Below the status pill, the inverter's
  temperature over the last three hours, refreshed every minute, from the
  readings the monitor already stores. It turns light blue at 3 °C and below
  and red from 60 °C. With `WEATHER_ENABLED` the outside temperature runs
  along as a dashed line. On phones it gets its own row below the header.
  APsystems publishes no derating point for the EZ1, so 60 °C is a cautious
  mark: the datasheet allows up to 65 °C around the inverter.
- **Faster camera in the sun path.** Moves start fast and brake towards the
  end (1.3 s), and the house button's replay of the day does the same.
- New endpoint `GET /api/temperatures`.

## From v1.15.0 to v1.15.1

No manual steps, no database changes.

- **Light theme in sun amber.** The accent is amber like in the dark theme,
  surfaces stay cool white. Areas and bars use a brighter amber, so they stay
  yellow instead of turning tan. PV 1 is blue, PV 2 stays teal.
- **House button in the sun path.** It first moves back to the default view,
  then plays the day from sunrise, fast at first and slowing down towards now.
  Already in the default view and live, it does nothing.
- **Year view.** The daily bars lose their outline, which covered the fill.

## From v1.14.0 to v1.15.0

No manual steps, no database changes.

- **Weather in the sun path.** With `WEATHER_ENABLED` the scene draws each
  hour's weather between the house and the sun: clouds, rain, snow, a
  thunderstorm, fog over the map. A button next to the compass hides it.
- **Compass rose and default view.** The rose turns with the view; its letters
  look north, east, south or west. The house button moves slowly back to the
  default view, now flatter and a little further south, and plays the day.
- **Calmer figures.** Three tiles instead of four: what was expected so far,
  with the actual value below it, sun angle and clear-day expectation. Measured
  values are in the sun's colour, expected ones neutral, north is red.
- **API.** `/api/yield-curve` adds `weather_hours`, the WMO code of each hour of
  the day.

## From v1.13.0 to v1.14.0

No manual steps, no database changes.

- **Sun path.** A new card between the period cards and the history chart:
  expected vs. actual learned from your own days, and a projection of today's total.
  Works by itself once 7 days are recorded.
- **`SITE_ADDRESS` and `SITE_FLOORS` (optional).** With an address (or
  `lat,lon`) the card adds a rotatable sun path and a reduced map of your
  surroundings. The server looks the address up once at Nominatim and
  OpenStreetMap and caches the result in `/data/site-map.json`; without the
  variable nothing is sent anywhere. See [Sun path](README.md#sun-path).
- **`WEATHER_ENABLED` (optional).** Set to `true` next to `SITE_ADDRESS` for an
  hourly forecast from Open-Meteo: a better estimate of today from the start of
  the day, what is still to come in the bars and on the timeline, and an
  estimate for tomorrow. Only your coordinates, rounded to about 1 km, are
  sent. The Unraid template gains the variable for new installs; on an existing
  container add it by hand.

## From v1.12.1 to v1.13.0

No manual steps. The database migrates by itself on first start.

- **Temperature and grid values in the top bar.** Inverter temperature, grid
  voltage and frequency, read once a minute from the inverter's
  `getOutputDataDetail` endpoint and stored with every measurement, ready for a
  chart in a later release. They are hidden while the inverter sleeps and on
  firmware that doesn't report them. If a regular poll fails right after this
  request, the monitor leaves it alone for ten minutes, so firmware that
  doesn't cope well with it isn't disturbed.
- **The real AC limit.** The top bar and the power bar now use the limit in
  force (`getMaxPower`, re-read hourly). They used to show the highest value
  that can be set, for example 1800 W on an EZ1-D limited to 800 W. The limit
  shown corrects itself within a minute of starting the new version.
- **Lifetime figures survive a counter reset.** Some firmware wraps its
  lifetime counters to 0 near 540 kWh, and a replaced inverter starts over.
  The monitor now keeps each day's counter readings and adds a real fall back,
  so lifetime energy, money, CO₂ and payback stay put. A counter that never
  falls gives exactly the same figures as before, and short glitches in either
  direction are ignored.
- **`LIFETIME_OFFSET_KWH` (optional, default 0).** Adds a fixed amount to the
  lifetime total, negative to lower it, for a counter wrap from before the
  monitor was set up or a manual correction. Wraps the monitor sees are added
  back by itself, entering one of those would count it twice.
- **Total generation can show what the monitor recorded.** Click the card to
  switch between the inverter's lifetime counter and the sum of the history
  bars. Your browser remembers the choice. Money, CO₂ and payback stay on the
  inverter's counter. `/api/stats` gains `recorded_kwh` and `first_data_date`.
- **Chart tooltip on phones.** In the day chart it now shows only while you
  hold a finger on the chart, follows it, and disappears on release. A swipe
  across the chart scrolls the page. The other charts are unchanged.
- **PV tiles in the colors of their lines.** The PV 1 and PV 2 values match
  their lines in the day chart.
- **`INSTALL_KWP` removed.** It was only passed through `/api/live`
  (`config.install_kwp` is gone too). A variable still set on your container
  is simply ignored and can be deleted.

## From v1.12.0 to v1.12.1

No manual steps, no database migration.

- **The live power card sticks to live power.** The PV tiles no longer show
  each panel's yield for the day, which duplicated the table right below them.
  `pv1_kwh_today` and `pv2_kwh_today` in `/api/stats` and the Prometheus metrics
  are unchanged.

## From v1.11.3 to v1.12.0

No manual steps, no database migration.

- **Peak and average moved to the day curve, and now come per panel.** The
  curve's card shows peak (with its time), average and yield for PV 1, PV 2
  and in total, for whichever day is selected, and draws each panel as a thin
  line of its own, so shading stands out. The table's first column is the
  legend: click an entry to hide or show that line (remembered per browser).
- **The power bar's labels are visible.** The share of the AC limit and the
  limit itself, below the bar in the live power card, had been cut off by a
  layout bug since the first release.
- **A tariff change no longer moves what's already produced.** The part of
  the lifetime counter without a stamped price, usually production from before
  the monitor was set up, followed `PRICE_PER_KWH`, so the money and payback
  cards jumped when the tariff changed. It now stays at the oldest stamped
  price. If you have changed your tariff in the past, the figure settles once
  when you upgrade. After a change, the money card shows both prices
  ("avg €0.334 · now €0.28"). See
  [Changing your tariff](README.md#changing-your-tariff).
- **The projected break-even uses the current tariff.** It extrapolates your
  average production at what a kWh is worth today, instead of the average of
  everything saved so far, which kept promising the old price after a change.
- **One dropped poll is no outage.** A single failed request used to write an
  offline marker, which turned the status red and paused polling for up to
  five minutes. The inverter now counts as offline after three failed polls in
  a row, and until then polling continues at the normal interval. As a side
  effect, the status changes to standby at dusk two polls later than before
  (two minutes at the default interval).
- **Numbers follow the UI language**, including chart axes and tooltips:
  1.234,56 in German, 1,234.56 in English. Energy drops its decimals from
  1000 kWh on, and the lifetime cards move to two columns in windows narrower
  than 1240 px, so their values no longer wrap.
- **Fresh data when you come back.** Returning to the tab refreshes everything
  right away, instead of showing old values until the next slow tick.
- **`/health` reports the poller.** It now answers `503` when the poll loop has
  died or stopped completing its cycles, and it used to always say OK. An
  inverter asleep at night never fails it. Docker marks a stuck container as
  unhealthy; a Kubernetes liveness probe restarts it.
- **API changes.** `/api/stats` no longer has `peak_today_ts` and
  `avg_w_during_production`; those figures are now in the `figures` object of
  `/api/history?range=day`, per panel and for any day. `/api/stats` gains
  `avg_price_per_kwh`. `peak_w_today` and the Prometheus metrics are unchanged.

## From v1.11.2 to v1.11.3

No manual steps, no database migration — a bug-fix release.

- **A mid-production outage now stays "error".** Standby vs. error was
  decided by the average power of the five minutes before *now*. The poller
  writes no rows while the inverter is offline, so that window ran empty
  five minutes into an outage and the red alarm silently turned into the
  dimmed "standby" — on the dashboard and in `ez1_status` alike. The window
  is now anchored at the last online reading: dusk still winds down into
  standby exactly as before, a real outage stays an error until the
  inverter answers again.
- **The calendar popup no longer closes on its own.** The day picker was
  destroyed and rebuilt on every live refresh — every 10 s while the
  inverter is online — which closed an open calendar mid-selection. It is
  now only rebuilt when the language actually changes; its date bounds are
  refreshed whenever the calendar opens, and the date label rolls over at
  midnight together with the chart.
- **Browsers pick up a new release right away.** The cache-busting `?v=`
  query on all assets had been stuck at 1.9.0, so browsers — and installed
  PWAs in particular — could keep running an outdated `app.js`/`style.css`
  after an upgrade. The token is now filled in from the app version at
  startup, and the page itself is served with `Cache-Control: no-cache`.
- **No made-up year-over-year comparison in ISO week 53.** In a week 53
  without a week 53 the year before (the next one: 28 Dec 2026 – 3 Jan
  2027), the week card's YoY line and the drill-down's "vs. last year" pill
  compared against week 1 of the *same* year, labeled "Week 53 / 2025".
  Both are now hidden, like any other comparison without a counterpart.
- **Payback projection and break-even date account for pre-monitor
  production.** "Money saved" is valued from the inverter's lifetime
  counter, which also contains everything produced before the monitor was
  set up. The projected break-even date divided that total by the
  monitor's own, shorter history — too early by up to the whole
  pre-monitor period — and the break-even glow date came out too late.
  Both now base the rate and the crossover on the recorded history only.
- `/api/stats` no longer scans the whole `measurements` table three times
  per call: the "today" queries (peak, per-panel energy, production window)
  use an indexed timestamp range instead of `date()` on every row.
- Period anchors that don't exist (`?week=2026-W54`, `?month=2026-13`)
  return `422` instead of a `500`.

## From v1.11.1 to v1.11.2

No manual steps, no database migration — a one-fix release.

- **The day view's prev arrow now stops at the first recorded day.** Like
  the week/month navigation fixed in v1.11.1, the day picker's back arrow —
  and the calendar popup — could walk into empty days before the
  installation. The day endpoint now carries the same `first_data_date`
  bound, and the earliest selectable day is the **later** of it and the
  retention boundary, because the intraday curve reads raw measurements:
  on installs younger than `RETENTION_DAYS` the first recorded day binds,
  on older installs the retention window does, as before. A missing field
  (older backend) keeps the retention-only behaviour, and the arrow's
  disabled state now matches its click guard exactly (day-granular
  comparison).

## From v1.11.0 to v1.11.1

No manual steps, no database migration — a small bug-fix release.

- **The drill-down's prev arrow now stops at the first recorded period.**
  The anchored week/month navigation could step back past the beginning of
  the data into endless empty periods; the arrow now disables at the
  week/month containing the earliest recorded day — the mirror of the
  forward clamp at the running period. The anchored endpoints expose the
  bound as `first_data_date` (from the daily aggregates, so it reaches
  back beyond the raw-row retention window); a missing field, as with an
  older backend, simply keeps the old unbounded behaviour.
- **The running period's head figures now match its pills.** In the
  running week/month view the total and per-day average were read from
  the hourly-refreshed daily aggregates while the comparison pills used
  live raw measurements — so the head number could trail the pill
  percentages by up to an hour. Both now come from the same raw query.
  The two sources share identical per-day `MAX(e1+e2)` semantics, so
  completed days are unchanged and only today's staleness disappears.
  With a `RETENTION_DAYS` shorter than the running period the aggregates
  remain the source, since pruned raw rows would undercount.

## From v1.10.0 to v1.11.0

No manual steps, no database migration. A small feature release.

- **Arrow navigation in the period drill-down.** The anchored week/month
  view gains prev/next arrows around the period label (`‹ KW 26 / 2026 ›`),
  matching the day picker — step through adjacent weeks or months without
  going back to the overview first. The forward arrow stops at the
  currently running period. "Zurück zur Übersicht" moved into the pill
  row to make space.
- **Fair comparisons in the running period.** With the running week/month
  now one arrow-click away, its "vs. Vorwoche" / "vs. Vorjahr" pills no
  longer compare a few days against full reference totals (which read
  "−70 %" all week). They compare at equal clock progress — Tuesday 09:30
  against the reference's Tuesday 09:30, from raw measurements, exactly
  like the stat cards' "Vergleichszeitraum" — so today's partial
  production counts on both sides and the pills are never a day behind.
  Labeled `vs. Vorwoche (lfd.) +16.6 %`; the record-pace pill uses the
  same basis. Only when a reference predates the retention window (raw
  rows pruned) does the comparison fall back to completed days from the
  daily aggregates.
- Mobile polish: shorter pace-pill label (`vs. lfd. Woche`), the record
  pill leads with its subject (`lfd. Woche: Rekordkurs! +10 %`), the
  summary figures wrap as whole units instead of stranding a `· Bester
  Tag` on the next line, and the pill stays amber in the light theme
  instead of inheriting its blue accent.

## From v1.9.1 to v1.10.0

No manual steps, no database migration. A small feature release.

- **Record-pace comparison in the period drill-down.** Anchored views of a
  past week or month gain a third pill: **"vs. lfd. Woche/Monat (gleicher
  Stand)"** — the anchored period cut down to today's progress (same
  weekday resp. day-of-month) compared against the currently running
  period. Opened from the Hall of Fame this answers the actual question:
  is the running week on track to beat the record? A complete record week
  is never compared against a half-run current week — equal progress only.
  When the running period is actually beating the *all-time record's* pace,
  the pill flips into a celebratory amber **"Rekordkurs!"** state with a
  subtle glow (softer than the Hall-of-Fame record glow) and re-bases the
  percentage on the record — `Rekordkurs! +10 %` instead of an alarming red
  `−9 %`. The pill hides when the anchored period *is* the running one or
  the running period has no data yet.
- **Projected break-even date on the amortization card.** Once a full year
  of data exists (shorter histories are seasonally biased, so nothing is
  shown before that), the card shows the estimated month the installation
  pays for itself: linear extrapolation of the average savings rate over
  the whole history — `voraussichtlich amortisiert: März 2028`. The line
  disappears at break-even, where the existing glow takes over.

## From v1.9.0 to v1.9.1

No manual steps, no database migration. A bug-fix release.

- **The aggregate backfill no longer loses pre-retention history.** Months
  and days older than `RETENTION_DAYS` without a stored aggregate (imported
  databases, upgrades from pre-aggregate versions) were skipped at startup —
  and their raw rows then pruned for good ~60 s later. They are now
  aggregated from the remaining raw rows first; existing (frozen) aggregates
  stay untouched, exactly as before.
- **Year-view daily bars are now correct west of UTC.** The daily buckets of
  `/api/history?range=year` grouped by UTC day, so at negative UTC offsets
  each bar could show `max(day, next day)`. They now use the local calendar
  day like every other daily query.
- **Docs and templates pointed at a misspelled GitHub namespace**
  (`ThecSimon` → `TehcSimon`): badge, `docker pull` commands, clone URL and
  Unraid template links now match the real repository and GHCR image.
- Device info (serial, firmware, AC limit) is retried after every successful
  poll instead of only 10 attempts at startup — a container (re)started at
  night no longer shows placeholders until the next restart.
- `/metrics`: `ez1_pv1_today_kwh` / `ez1_pv2_today_kwh` are DB-derived like
  the dashboard's PV cards and roll over to 0 at local midnight instead of
  holding yesterday's counters overnight.
- Stat windows are half-open, so a measurement landing exactly on a window
  boundary (midnight) is no longer counted into two adjacent windows.
- A calm amber **"connection error"** pill when the dashboard can't reach
  its own backend, instead of the red inverter-error state.
- Electricity Maps polling retries with backoff (5 → 40 min) after a failed
  poll instead of silently waiting a full hour.
- Charts update in place instead of being rebuilt on every refresh (no more
  periodic flicker), and stale responses from rapid tab/day switches can no
  longer overwrite the newest view.
- Dropped a redundant SQLite index on `measurements` (the timestamp PRIMARY
  KEY already is the table's btree).

## From v1.8.x to v1.9.0

No manual steps, no database migration. A feature release — everything is
computed from the existing `daily_aggregates` table (which survives raw-row
pruning), so anchored views of old weeks/months work even after their raw
measurements are gone.

- **Weekly history view.** The Year range on the History card gains a third
  granularity, **Weekly** (≈52 ISO-week bars), alongside Daily and Monthly —
  the most readable density between 365 daily and 12 monthly bars. Rolling
  (last 52 weeks) and calendar (this year) both apply.
- **Drill into any week or month.** Click a weekly or monthly bar to open that
  exact period as daily bars, with a **summary line**: total, average per day,
  best day, and data-gated deltas vs. the previous period and vs. the same
  period last year. A "back" link returns to the overview; clicking a day
  drills further into its intraday curve.
- **Hall of Fame is now navigable.** Click the best-day / week / month tile to
  jump to that period (best-year opens the all-years overview). Tiles are
  keyboard-focusable buttons.
- **Harmonized stat cards.** The Week card gains a year-over-year line ("same
  ISO week last year") to match the Month card — both are now data-gated
  (hidden on young installs with nothing to compare). The Month card's "full
  month last year" line was removed; that figure is reachable via the new
  drill-down.

## From v1.7.0 to v1.8.0

No manual steps, no database migration. Feature release: realistic "money
saved" via `SELF_CONSUMPTION_PCT` + `FEED_IN_TARIFF` (defaults reproduce
the old behaviour), the amortization card via `INSTALL_COST` (with
break-even glow), a fixed/auto Y-axis toggle for the day chart, and two
new Prometheus metrics (`ez1_money_saved`, `ez1_amortization_percent`).
All three variables are described in the
[configuration table](README.md#configuration).

## From v1.6.3 to v1.7.0

No manual steps, no database migration. Feature release: rolling vs.
calendar mode for the Week / Month / Year charts (persisted toggle on the
History card), the device header now labels the inverter's output cap as
**AC limit**, plus day-picker icon and layout polish.

## From v1.6.2 to v1.6.3

No manual steps, no database migration. Bug-fix/polish release: per-panel
"today" kWh survives inverter standby (now DB-derived), shorter
Hall-of-Fame glow durations, a stacked labeled device header, and mobile
time-axis labels that no longer overlap.

## From v1.6.1 to v1.6.2

No manual steps, no database migration. Bug-fix/cleanup release: stat
windows bucket by local calendar day (fixes double-counting in far-east
timezones), the retention-boundary day stays frozen during backfill, the
month-rollover gap in the hourly refresh was closed, frontend date parsing
fixed for the Americas, and `PRICE_PER_KWH=0` / `RETENTION_DAYS=0` are
honored by the UI. All frontend assets (Chart.js, flatpickr, fonts) are
self-hosted since this release — the dashboard renders fully offline with
zero third-party requests (which also removed the Google-Fonts GDPR
issue).

## From v1.6.0 to v1.6.1

No manual steps. On first start the container runs idempotent `ALTER
TABLE` migrations (electricity-price columns, `firmware` on
`device_info`); existing rows keep `NULL` prices and fall back to the
current `PRICE_PER_KWH`, like the CO₂ handling of pre-v1.4 rows. Notable
fixes: long-term aggregates are no longer overwritten with partial values
at the retention boundary, daily aggregates group by local calendar date,
lifetime CO₂/money stay accurate after pruning, a Hall-of-Fame timer leak
and an iOS scroll artifact were fixed, `ez1_info` exports the firmware
under the correct label, and the image shrank by ~40 MB.

## From v1.5.x to v1.6.x

No manual steps. On first start the container creates the new
`daily_aggregates` table and backfills it from existing measurements
(typically 1-3 seconds, up to ~15 seconds on a fully populated 2-year
install on a Raspberry Pi). Subsequent starts are instant. The new
table powers the Hall of Fame card and the per-period "best day"
references on the Week/Month/Year stat cards.

## From v1.4.x to v1.5.x

No manual steps and no database migration. v1.5 is an internal cleanup
release — backend query consolidation, language-detection caching, and
the first round of unit tests. Behavior, API, and UI are unchanged from
v1.4.3.

## From v1.3.x to v1.4.x

No manual steps. On first start the container runs three idempotent
`ALTER TABLE` migrations to add CO₂ columns, then backfills the new
aggregate average columns from existing measurements. Old rows keep
`NULL` CO₂ factors — the lifetime CO₂ calc handles that automatically by
falling back to the static factor for the unmeasured portion.

## From v1.0.x to v1.1.0+

v1.1.0 changed the container user from root to UID 1000 (group 0). On
most platforms this is transparent; existing `/data` volumes need a
one-time permission fix:

```bash
sudo chown -R 1000:0 /path/to/ez1-data
sudo chmod -R g=u   /path/to/ez1-data
```
