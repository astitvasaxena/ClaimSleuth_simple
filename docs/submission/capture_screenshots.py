#!/usr/bin/env python3
"""Capture the report's dashboard screenshots (docs/screenshots/6_* to 9_*) from STORED runs.

How the 2026-11-20 images were made (nothing here runs an investigation or calls a model):
  1. Copy a runtime folder that holds the stored runs (runtime/runs, blackboard.sqlite3 and its key file) to a scratch
     folder, so the originals are never opened for writing.
  2. Start the dashboard from this repository in offline viewing mode on that copy, without services:
        CLAIMSLEUTH_RUNTIME_DIR=<copy> CLAIMSLEUTH_BLACKBOARD_DB=<copy>/blackboard.sqlite3 \
        <.venv python> -m claimsleuth.dashboard --mode offline --no-services --port 8119
     (no .env next to the code, so no API key is available; --no-services starts neither MCP nor CrewAI)
  3. Run this script with Playwright (any environment; it drives the installed Microsoft Edge):
        python docs/submission/capture_screenshots.py [out_dir]
It only opens #investigation?run=<RUN-ID> and screenshots page elements. It never clicks a review button.
Viewport 1024 px at device scale 2, so the text stays readable when a panel is printed 6.3 inches wide.
"""
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8119/"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "screenshots"
RESULT_RUN = "RUN-dash-e2a854a818"    # WC-2026-0411, offline test doubles, human review recorded on 2026-11-01
LIVE_RUN = "RUN-dash-bcdbabaf97"      # WC-2026-0412, live reference run on Gemini (docs/EVIDENCE.md)


def open_run(pg, run):
    pg.goto("about:blank")
    pg.goto(BASE + "#investigation?run=" + run)
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(2500)


def stack(paths, out, gap=24):
    ims = [Image.open(p).convert("RGB") for p in paths]
    canvas = Image.new("RGB", (max(i.width for i in ims), sum(i.height for i in ims) + gap * (len(ims) - 1)), "white")
    y = 0
    for i in ims:
        canvas.paste(i, (0, y))
        y += i.height + gap
    canvas.save(out)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = OUT / "_tmp_capture"
    tmp.mkdir(exist_ok=True)
    with sync_playwright() as p:
        br = p.chromium.launch(channel="msedge", headless=True)
        pg = br.new_page(viewport={"width": 1024, "height": 1000}, device_scale_factor=2)
        open_run(pg, RESULT_RUN)
        pg.locator("section.status-strip").screenshot(path=str(tmp / "strip.png"))
        pg.locator("#results-grid").screenshot(path=str(tmp / "banner.png"))
        stack([tmp / "strip.png", tmp / "banner.png"], OUT / f"6_result_0411_offline_{RESULT_RUN}.png")
        open_run(pg, LIVE_RUN)
        pg.locator("#report .block.integrity").screenshot(path=str(OUT / f"7_injection_0412_live_{LIVE_RUN}.png"))
        pg.locator("section.panel", has=pg.locator("#agents-body")).screenshot(
            path=str(OUT / f"8_agent_reports_0412_live_{LIVE_RUN}.png"))
        pg.locator("div.overview-grid").screenshot(path=str(OUT / f"9_overview_blackboard_0412_live_{LIVE_RUN}.png"))
        br.close()
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()


if __name__ == "__main__":
    main()
