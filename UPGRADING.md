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
| v1.9.1 | Drops a redundant index on `measurements` (instant) |
| v1.6.1 | `ALTER TABLE` adds the electricity-price and firmware columns (instant) |
| v1.6.0 | Creates `daily_aggregates` and backfills it — 1–3 s, up to ~15 s on a full 2-year install on a Raspberry Pi |
| v1.4.0 | `ALTER TABLE` adds the CO₂ columns, then backfills the aggregate averages |
| v1.1.0 | **Manual step:** the container runs as UID 1000 — see [below](#from-v10x-to-v110) |

The rest of this file is the release history, newest first.

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
