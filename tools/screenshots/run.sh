#!/bin/sh
# Regenerate the README screenshots into docs/screenshots/ (see compose.yml).
#
# Always starts from a clean stack and tears it down again afterwards, even
# on failure: the demo database is rebuilt on every run, which must never
# happen underneath a monitor still running from an earlier, interrupted run.
set -e
cd "$(dirname "$0")/../.."
compose="docker compose -f tools/screenshots/compose.yml"
$compose down -v --remove-orphans >/dev/null 2>&1
trap '$compose down -v >/dev/null 2>&1' EXIT
$compose run --rm --build shoot
