# DIEM yield — Step 1: look at the real pages

This first script is **reconnaissance, not the final scraper**. Claude has
never seen the actual HTML of these marketplace pages, so this script's only
job is to visit them, save what's really there (a screenshot, the full page
HTML, and the plain text), and report back. Once you send those files back,
the real price-extraction logic gets written against the real page
structure instead of guesswork.

## One-time setup (Mac)

1. Open the **Terminal** app: press `Cmd + Space`, type `Terminal`, hit Enter.
2. Check Python is installed:
   ```
   python3 --version
   ```
   If you see `Python 3.x.x`, you're good. If you see "command not found",
   install Python from https://www.python.org/downloads/ first, then
   re-open Terminal and try again.
3. Move into this folder (adjust the path to wherever you saved it — likely
   your Downloads folder):
   ```
   cd ~/Downloads/diem_yield_scraper
   ```
4. Install the two packages this script needs:
   ```
   pip3 install -r requirements.txt
   ```
5. Install the browser Playwright will drive (one-time download, ~200MB):
   ```
   playwright install chromium
   ```

## One-time setup (Windows)

1. Open **Command Prompt** or **PowerShell**.
2. Check Python is installed: `python --version`. If missing, install from
   https://www.python.org/downloads/ (check "Add Python to PATH" during
   install), then reopen the terminal.
3. `cd` into this folder, e.g. `cd C:\Users\YourName\Downloads\diem_yield_scraper`
4. `pip install -r requirements.txt`
5. `playwright install chromium`

## Running it

```
python3 inspect_venues.py
```
(On Windows, just `python inspect_venues.py`.)

It prints progress as it goes and finishes in under a minute. When done,
you'll have a new `output` folder next to the script containing:

- `antseed_<timestamp>.png` / `cheaptokens_<timestamp>.png` — full-page screenshots
- `antseed_<timestamp>.html` / `cheaptokens_<timestamp>.html` — raw page HTML
- `antseed_<timestamp>_text.txt` / `cheaptokens_<timestamp>_text.txt` — plain visible text
- `run_summary_<timestamp>.json` — DIEM's price at the time of the run

## What to send back

Zip the `output` folder (right-click it → **Compress**) and share it back,
or just open the two `*_text.txt` files and paste their contents into the
chat. That's what's needed to write the real scraper.

## If something goes wrong

- **"command not found: python3" / "pip3"** — Python isn't installed or
  isn't on your PATH. Reinstall from python.org and make sure the installer's
  "Add to PATH" option is checked.
- **The script prints "Could not fetch DIEM price"** — your network may be
  blocking the CoinGecko API, or DIEM's ID on CoinGecko might not be exactly
  `diem`. Check https://www.coingecko.com/en/coins/diem and share what the
  URL says.
- **"No obvious $ or % values" for a venue** — that site may require a
  connected wallet or login to see real prices, or it renders very slowly.
  Send the screenshot back either way; it'll show what's actually blocking it.
- **"Executable doesn't exist... chrome-headless-shell"** — Playwright's
  browser download didn't finish. Run `playwright install chromium` again
  (or just `playwright install` with no browser name) and re-run the script.
