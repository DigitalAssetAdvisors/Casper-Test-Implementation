#!/usr/bin/env python3
"""
Step 1: reconnaissance, not the real scraper yet.

This script visits the DIEM credit-selling venues and DIEM's price source,
and saves exactly what it sees (a screenshot, the full HTML, and the plain
visible text) into an "output" folder next to this script. It does not try
to be clever about finding prices -- it just looks and reports back, so the
real price-extraction logic can be written against what these pages
*actually* contain instead of guesswork.

After running this once, send the files in the "output" folder back so the
next version of the script can be built to parse real prices automatically.

Usage:
    python3 inspect_venues.py
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("Missing dependency 'requests'. Run this first:\n  pip3 install -r requirements.txt")
    sys.exit(1)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print(
        "Missing dependency 'playwright'. Run this first:\n"
        "  pip3 install -r requirements.txt\n"
        "  playwright install chromium"
    )
    sys.exit(1)


OUTPUT_DIR = Path(__file__).parent / "output"

VENUES = {
    "antseed": "https://diem.antseed.com/",
    "cheaptokens": "https://cheaptokens.ai/",
}

# Loose pattern to spot anything that looks like a dollar amount or a
# percentage, just to give a quick preview in the terminal. This is NOT
# the real parser -- just a sanity check that prices are visible at all.
PRICE_LIKE = re.compile(r"\$\s?\d[\d,]*\.?\d*|\d+(?:\.\d+)?\s?%")


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def fetch_diem_price() -> dict | None:
    print("Fetching DIEM's spot price from CoinGecko...")
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "diem", "vs_currencies": "usd"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        price = data.get("diem", {}).get("usd")
        if price is None:
            print(f"  Got a response but no price in it: {data}")
            print(
                "  DIEM's CoinGecko ID might not be 'diem' -- check the URL on "
                "https://www.coingecko.com/en/coins/diem and tell Claude what it says."
            )
        else:
            print(f"  DIEM price: ${price}")
        return data
    except Exception as e:
        print(f"  Could not fetch DIEM price: {e}")
        return None


def inspect_venue(playwright, name: str, url: str) -> None:
    print(f"\nVisiting {name} ({url})...")
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1600})
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
    except Exception as e:
        print(f"  Page didn't fully settle ({e}) -- continuing anyway, it may still have content.")

    # Give any client-side widgets (price tickers, listings) a moment to render.
    time.sleep(3)

    ts = timestamp()
    screenshot_path = OUTPUT_DIR / f"{name}_{ts}.png"
    html_path = OUTPUT_DIR / f"{name}_{ts}.html"
    text_path = OUTPUT_DIR / f"{name}_{ts}_text.txt"

    page.screenshot(path=str(screenshot_path), full_page=True)
    html_path.write_text(page.content(), encoding="utf-8")

    visible_text = page.inner_text("body")
    text_path.write_text(visible_text, encoding="utf-8")

    print(f"  Saved screenshot:   {screenshot_path.name}")
    print(f"  Saved full HTML:    {html_path.name}")
    print(f"  Saved visible text: {text_path.name}")

    found = [m.group(0) for m in PRICE_LIKE.finditer(visible_text)]
    if found:
        preview = ", ".join(found[:15])
        print(f"  $ or % looking snippets found on page: {preview}")
    else:
        print(
            "  No obvious $ or % values in the visible text -- the page may "
            "require a login, a connected wallet, or more time to load."
        )

    browser.close()


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)

    print("=" * 60)
    print("DIEM yield venue reconnaissance")
    print("=" * 60)

    price_data = fetch_diem_price()

    with sync_playwright() as p:
        for name, url in VENUES.items():
            try:
                inspect_venue(p, name, url)
            except Exception as e:
                print(f"  FAILED on {name}: {e}")

    summary = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "diem_price_response": price_data,
    }
    (OUTPUT_DIR / f"run_summary_{timestamp()}.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    print("\nDone. Everything is saved in the 'output' folder next to this script.")
    print(
        "Next step: zip the 'output' folder and send it back (or open the "
        "*_text.txt files and paste their contents into the chat) so the real "
        "price-scraping logic can be written against what these pages actually contain."
    )


if __name__ == "__main__":
    main()
