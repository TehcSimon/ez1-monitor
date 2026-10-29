"""Capture the README screenshots from a running ez1-monitor instance.

    python shoot.py http://app:8080 /out

Runs in the official Playwright image (see compose.yml). The browser clock
is aligned with the monitor's pinned clock, and every shot waits for live
data, fonts and the Chart.js entry animation, so the images show the
dashboard the way a user sees it a few seconds after opening it. Output is
WebP: a fraction of the PNG size at no visible loss.
"""
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://app:8080"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/out")

DESKTOP = {"width": 1360, "height": 900}
# Phone width, but tall enough to show the hero card and today's full curve.
PHONE = {"width": 390, "height": 1120}


def backend_now() -> datetime:
    """The monitor's clock, read off its latest reading (a few seconds old)."""
    with urllib.request.urlopen(f"{BASE}/api/live") as r:
        latest = json.load(r)["latest"]
    return datetime.fromtimestamp(latest["timestamp"] + 5, tz=timezone.utc)


def open_dashboard(browser, theme, viewport, mobile=False):
    ctx = browser.new_context(
        viewport=viewport, device_scale_factor=2, color_scheme=theme,
        locale="en-US", timezone_id="Europe/Berlin",
        is_mobile=mobile, has_touch=mobile,
    )
    ctx.clock.install(time=backend_now())
    # Explicit theme override, exactly like clicking the toggle would set it.
    ctx.add_init_script(
        f"try {{ localStorage.setItem('ez1-theme', '{theme}') }} catch (e) {{}}"
    )
    page = ctx.new_page()
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_function(
        "['current-power', 'stat-today', 'lifetime-kwh', 'hof-day-value']"
        ".every(id => document.getElementById(id).textContent.trim() !== '—')"
    )
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(2500)
    return ctx, page


def save(png: bytes, name: str) -> None:
    """Write the capture as WebP (the PNG never touches the output dir)."""
    path = OUT / f"{name}.webp"
    from io import BytesIO
    Image.open(BytesIO(png)).save(path, "WEBP", quality=88, method=6)
    print(f"{path.name}: {path.stat().st_size // 1024} KB")


def shoot_region(page, name, *selectors, pad=0):
    """Capture the box around several elements. Measured in document
    coordinates and taken from the full page, so it works regardless of
    scroll position and for regions taller than the viewport."""
    clip = page.evaluate(
        """([sels, pad]) => {
            const rs = sels.map(s => document.querySelector(s).getBoundingClientRect());
            const y = window.scrollY;
            const x0 = Math.max(0, Math.min(...rs.map(r => r.left)) - pad);
            const x1 = Math.min(document.documentElement.clientWidth,
                                Math.max(...rs.map(r => r.right)) + pad);
            const y0 = Math.max(0, Math.min(...rs.map(r => r.top)) + y - pad);
            const y1 = Math.max(...rs.map(r => r.bottom)) + y + pad;
            return { x: x0, y: y0, width: x1 - x0, height: y1 - y0 };
        }""",
        [list(selectors), pad],
    )
    save(page.screenshot(full_page=True, clip=clip), name)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()

        for theme in ("dark", "light"):
            ctx, page = open_dashboard(browser, theme, DESKTOP)
            # Hero: top bar, live power, today's curve and the four stat cards.
            shoot_region(page, f"dashboard-{theme}", ".topbar", ".comparison-grid", pad=24)
            ctx.close()

            ctx, page = open_dashboard(browser, theme, PHONE, mobile=True)
            save(page.screenshot(), f"mobile-{theme}")
            ctx.close()

        ctx, page = open_dashboard(browser, "dark", DESKTOP)
        save(page.screenshot(full_page=True), "full-dark")
        # Hall of Fame + lifetime cards (energy, CO₂, money, payback).
        shoot_region(page, "records-dark", ".hof-section", ".lifetime-grid", pad=24)
        # Best-month tile → drill-down into the record month, with the
        # comparison pills (previous month, last year, running month).
        page.click("#hof-month")
        page.wait_for_selector("#history-summary", state="visible")
        page.wait_for_timeout(2000)
        save(page.locator(".history-card").screenshot(), "drilldown-dark")

        # Year view, weekly bars — the densest readable history overview.
        page.click("#history-summary-back")
        page.click(".range-tab[data-range='year']")
        page.click(".gran-tab[data-gran='weekly']")
        page.wait_for_timeout(2000)
        save(page.locator(".history-card").screenshot(), "year-weekly-dark")
        ctx.close()

        browser.close()


if __name__ == "__main__":
    main()
