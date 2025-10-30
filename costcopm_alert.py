#!/usr/bin/env python3
"""
Costco Precious Metals → Bluesky Alert (+ Screenshot optional)
Flow (local & CI):
  1) Launch Playwright browser and load Costco precious metals page.
  2) Capture Costco's live JSON API to api-sample.json.
  3) Parse it to count gold/silver and in-stock items.
  4) Post summary to Bluesky (with screenshot if available).

Env:
  CI=true/false                -> toggles minor stealth flags and default browser (webkit on CI)
  BROWSER=webkit|firefox|chrome|chromium
  HEADLESS=true|false
  BSKY_HANDLE=you.bsky.social
  BSKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
"""

import os
import re
import sys
import json
import builtins
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from atproto import Client, models

# ------------------------------------------------------------------------------
# Env / constants
# ------------------------------------------------------------------------------
load_dotenv()

IS_CI = bool(os.getenv("CI"))
USE_BROWSER = os.getenv("BROWSER", "webkit" if IS_CI else "firefox").lower()
HEADLESS = os.getenv("HEADLESS", "true").lower() in {"1", "true", "yes", "on"}

# Bluesky creds
BSKY_HANDLE = os.getenv("BSKY_HANDLE")
BSKY_APP_PASSWORD = os.getenv("BSKY_APP_PASSWORD")
if not BSKY_HANDLE or not BSKY_APP_PASSWORD:
    builtins.print("ERROR: BSKY_HANDLE or BSKY_APP_PASSWORD missing in .env")
    sys.exit(1)

URL = "https://www.costco.com/precious-metals.html"
API_JSON_PATH = "api-sample.json"
SCREENSHOT = "costco.png"
TIMEOUT = 90_000  # ms

# Text heuristics for status (fallback if JSON fails)
OOS_PATTERNS = [
    "we were not able to find a match",
    "no results found",
    "did not match any products",
]
IN_STOCK_TERMS = ["gold bar", "gold bars", "silver bar", "silver bars", "precious metals"]

# ------------------------------------------------------------------------------
# Bluesky facets (hashtags + links)
# ------------------------------------------------------------------------------
HASHTAG_RE = re.compile(r'(?<!\w)#([A-Za-z0-9_]+)')
URL_RE = re.compile(r'https?://[^\s\)\]\}>,]+')


def _byte_slice(text: str, start: int, end: int) -> models.AppBskyRichtextFacet.ByteSlice:
    bs = len(text[:start].encode("utf-8"))
    be = bs + len(text[start:end].encode("utf-8"))
    return models.AppBskyRichtextFacet.ByteSlice(byte_start=bs, byte_end=be)


def build_facets(text: str):
    facets = []
    for m in HASHTAG_RE.finditer(text):
        facets.append(
            models.AppBskyRichtextFacet.Main(
                features=[models.AppBskyRichtextFacet.Tag(tag=m.group(1))],
                index=_byte_slice(text, m.start(), m.end()),
            )
        )
    for m in URL_RE.finditer(text):
        facets.append(
            models.AppBskyRichtextFacet.Main(
                features=[models.AppBskyRichtextFacet.Link(uri=m.group(0))],
                index=_byte_slice(text, m.start(), m.end()),
            )
        )
    return facets

# ------------------------------------------------------------------------------
# JSON parsing (metal counts + in-stock)
# ------------------------------------------------------------------------------
def _detect_metal(doc: dict) -> str:
    forms = " ".join(doc.get("Precious_Metal_Form_attr") or []).lower()
    purity = " ".join(doc.get("Purity_attr") or []).lower()
    if "gold" in forms or "gold" in purity:
        return "gold"
    if "silver" in forms or "silver" in purity:
        return "silver"
    return "other"


def _is_in_stock(doc: dict) -> bool:
    if "isItemInStock" in doc:
        try:
            return bool(doc["isItemInStock"])
        except Exception:
            pass
    status = (doc.get("item_location_stockStatus") or doc.get("deliveryStatus") or "").lower()
    return status in {"in stock", "instock", "available"}


def parse_api_json(path: str) -> dict:
    """Return summary dict with numFound, counts by metal, and in-stock breakdown."""
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    resp = data.get("response", {})
    docs = resp.get("docs", [])
    num_found = int(resp.get("numFound") or len(docs))

    counts = {"gold": 0, "silver": 0, "other": 0}
    stock = {
        "gold": {"in_stock": 0, "out_of_stock": 0},
        "silver": {"in_stock": 0, "out_of_stock": 0},
        "other": {"in_stock": 0, "out_of_stock": 0},
    }

    for d in docs:
        m = _detect_metal(d)
        counts[m] = counts.get(m, 0) + 1
        if _is_in_stock(d):
            stock[m]["in_stock"] += 1
        else:
            stock[m]["out_of_stock"] += 1

    return {
        "numFound": num_found,
        "counts": counts,
        "stock": stock,
    }


def build_text_from_summary(summary: dict) -> str:
    """Compose the Bluesky message (with hashtags + URL) from a parsed summary."""
    now = datetime.now()
    hst = now.astimezone(ZoneInfo("Pacific/Honolulu"))
    pt  = now.astimezone(ZoneInfo("America/Los_Angeles"))
    et  = now.astimezone(ZoneInfo("America/New_York"))
    ts  = f"{hst.strftime('%I:%M %p %Z')} / {pt.strftime('%I:%M %Z')} / {et.strftime('%I:%M %Z')}"

    gold = summary["counts"].get("gold", 0)
    silver = summary["counts"].get("silver", 0)
    total = summary.get("numFound", gold + silver + summary["counts"].get("other", 0))
    g_in = summary["stock"]["gold"]["in_stock"]
    s_in = summary["stock"]["silver"]["in_stock"]

    status_line = "🚨 Costco Precious Metals IN STOCK!" if (g_in > 0 or s_in > 0) else "Costco Precious Metals — status update"

    text = (
        f"{status_line}\n\n"
        f"🕓 {ts}\n"
        f"Items found: {total} | Gold: {gold} | Silver: {silver}\n"
        f"In stock → Gold: {g_in} | Silver: {s_in}\n"
        "https://www.costco.com/precious-metals.html\n\n"
        "#Costco #Gold #Silver #CostcoPM"
    )
    return text

# ------------------------------------------------------------------------------
# Bluesky poster (supports text-only posts if screenshot is absent)
# ------------------------------------------------------------------------------
def post_to_bluesky(image_path: str | None, text: str) -> None:
    try:
        client = Client()
        client.login(BSKY_HANDLE, BSKY_APP_PASSWORD)

        embed = None
        if image_path and os.path.exists(image_path):
            with open(image_path, "rb") as f:
                upload = client.upload_blob(f.read())
            embed = models.AppBskyEmbedImages.Main(
                images=[models.AppBskyEmbedImages.Image(
                    image=upload.blob,
                    alt="Costco precious metals page showing gold/silver bars in stock",
                )]
            )

        facets = build_facets(text)
        client.send_post(text=text, embed=embed, facets=facets or None)
        builtins.print("Bluesky post sent!")
    except Exception as e:
        builtins.print(f"Bluesky post failed: {e}", file=sys.stderr)

# ------------------------------------------------------------------------------
# Browser launcher (regenerates api-sample.json via response hook)
# ------------------------------------------------------------------------------
def launch_browser(p):
    # Choose engine + UA
    args = []
    if USE_BROWSER in ("chromium", "chrome"):
        if IS_CI:
            args += ["--no-sandbox", "--disable-dev-shm-usage"]
        browser = (
            p.chromium.launch(channel="chrome", headless=HEADLESS, args=args)
            if USE_BROWSER == "chrome"
            else p.chromium.launch(headless=HEADLESS, args=args)
        )
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    elif USE_BROWSER == "firefox":
        if IS_CI:
            args += ["--no-sandbox", "--disable-dev-shm-usage"]
        browser = p.firefox.launch(headless=HEADLESS, args=args)
        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:129.0) Gecko/20100101 Firefox/129.0"
    else:
        # WebKit tends to be most reliable on CI for costco.com
        browser = p.webkit.launch(headless=HEADLESS, args=[])
        ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15")

    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent=ua,
        ignore_https_errors=True,
        locale="en-US",
        timezone_id="America/Los_Angeles",
    )
    page = context.new_page()

    # Safe console handlers
    def _console(msg):
        try:
            builtins.print(f"[console][{msg.type()}] {msg.text()}")
        except Exception as e:
            builtins.print(f"[console][error] {e!r}")

    def _pageerror(err):
        try:
            builtins.print(f"[pageerror] {err}")
        except Exception as e:
            builtins.print(f"[pageerror][error] {e!r}")

    page.on("console", _console)
    page.on("pageerror", _pageerror)

    # Headers + minor stealth tweaks on CI
    if IS_CI:
        context.set_extra_http_headers({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": "https://www.costco.com/",
        })
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:() => undefined});")
        page.add_init_script("Object.defineProperty(navigator,'plugins',{get:() => [1,2,3]});")
        page.add_init_script("Object.defineProperty(navigator,'languages',{get:() => ['en-US','en']});")

    # Capture the first relevant JSON to api-sample.json on every run
    def _on_response(res):
        try:
            ct = (res.headers or {}).get("content-type", "")
            url = res.url
            if "application/json" in ct and any(k in url for k in ["search", "product", "catalog", "browse"]):
                data = res.json()
                with open(API_JSON_PATH, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                builtins.print(f"[api] JSON captured from {url[:120]}... -> {API_JSON_PATH}")
        except Exception:
            pass

    page.on("response", _on_response)
    return browser, context, page

# ------------------------------------------------------------------------------
# Main flow
# ------------------------------------------------------------------------------
def check_stock():
    with sync_playwright() as p:
        browser = None
        try:
            builtins.print("Launching browser...")
            browser, context, page = launch_browser(p)

            builtins.print("Loading Costco...")
            resp, last_err = None, None

            if IS_CI:
                # On CI, keep it simple; we just need navigation to start to capture JSON
                try:
                    resp = page.goto(URL, wait_until="commit", timeout=30_000)
                except Exception as e:
                    last_err = e
                    builtins.print(f"[goto] commit failed on CI: {e}")
            else:
                # Locally, progressively relax wait conditions
                for wait in ("load", "domcontentloaded", "networkidle"):
                    try:
                        resp = page.goto(URL, wait_until=wait, timeout=TIMEOUT)
                        break
                    except Exception as e:
                        last_err = e
                        builtins.print(f"[goto] {USE_BROWSER} failed ({wait}): {e}")

            if resp is None:
                builtins.print(f"[error] Page failed to initiate. Last error: {last_err}")
                builtins.print("Inconclusive")
                try: browser.close()
                except Exception: pass
                return

            # Try cookie banner accept if present
            try:
                page.locator("#onetrust-accept-btn-handler, button:has-text('Accept All Cookies')").first.click(timeout=2500)
                builtins.print("[info] Cookie banner accepted")
            except Exception:
                pass

            # Wait briefly for XHRs/json to arrive; then screenshot and HTML dump
            try:
                page.wait_for_timeout(2500)
            except Exception:
                pass

            if not page.is_closed():
                try:
                    page.screenshot(path=SCREENSHOT, full_page=True)
                    builtins.print(f"Screenshot saved: {os.path.abspath(SCREENSHOT)}")
                except Exception as e:
                    builtins.print(f"[warn] screenshot failed: {e}")
                try:
                    with open("page.html", "w", encoding="utf-8") as f:
                        f.write(page.content())
                    builtins.print("[debug] HTML dumped to page.html")
                except Exception as e:
                    builtins.print(f"[warn] html dump failed: {e}")

            # Basic textual fallback signals (if JSON doesn't capture)
            try:
                body_preview = page.inner_text("body")[:2000]
            except Exception:
                body_preview = ""
            low = (page.title().lower() + " " + body_preview.lower())

            # Consent/blocked heuristic
            if any(x in low for x in ("access denied", "request was blocked", "reference #", "problem loading page")):
                builtins.print("[warn] Possibly blocked/consent wall. See artifacts.")
                builtins.print("Inconclusive")
                try: browser.close()
                except Exception: pass
                return

            # ---- JSON parse (freshly captured this run) ----
            summary = None
            if os.path.exists(API_JSON_PATH):
                try:
                    summary = parse_api_json(API_JSON_PATH)
                    if summary:
                        builtins.print(
                            f"[api-summary] Parsed {summary['numFound']} products → "
                            f"gold={summary['counts']['gold']} (in {summary['stock']['gold']['in_stock']}), "
                            f"silver={summary['counts']['silver']} (in {summary['stock']['silver']['in_stock']})"
                        )
                except Exception as e:
                    builtins.print(f"[warn] Failed to parse captured JSON: {e}")

            # Fallback check for product tiles (rarely needed if JSON captured)
            tile_count = 0
            for sel in ('[data-automation="product-tile"]', '.product-tile', '[data-automation="product-grid"] a'):
                try:
                    c = page.locator(sel).count()
                    tile_count = max(tile_count, c or 0)
                except Exception:
                    pass
            builtins.print(f"[debug] tile_count={tile_count}")

            is_oos = any(pat in low for pat in OOS_PATTERNS)
            has_terms = any(term in low for term in IN_STOCK_TERMS)

            # ---- Decide and post ----
            if summary:
                g_in = summary["stock"]["gold"]["in_stock"]
                s_in = summary["stock"]["silver"]["in_stock"]
                text = build_text_from_summary(summary)

                if g_in > 0 or s_in > 0:
                    builtins.print("IN STOCK DETECTED!")
                    post_to_bluesky(SCREENSHOT if os.path.exists(SCREENSHOT) else None, text=text)
                else:
                    builtins.print("Out of stock")
                    # Optional: you could still post a status update; currently we only log.
            else:
                # No JSON captured? Use weaker heuristics.
                if tile_count > 0 or has_terms:
                    builtins.print("IN STOCK DETECTED! (heuristic)")
                    # Build generic timestamp-only text if no summary exists
                    now = datetime.now()
                    hst = now.astimezone(ZoneInfo("Pacific/Honolulu"))
                    pt  = now.astimezone(ZoneInfo("America/Los_Angeles"))
                    et  = now.astimezone(ZoneInfo("America/New_York"))
                    ts  = f"{hst.strftime('%I:%M %p %Z')} / {pt.strftime('%I:%M %p %Z')} / {et.strftime('%I:%M %p %Z')}"
                    text = (
                        "🚨 Costco Precious Metals IN STOCK!\n\n"
                        f"🕓 {ts}\n"
                        "https://www.costco.com/precious-metals.html\n\n"
                        "#Costco #Gold #Silver #CostcoPM"
                    )
                    post_to_bluesky(SCREENSHOT if os.path.exists(SCREENSHOT) else None, text=text)
                elif is_oos:
                    builtins.print("Out of stock")
                else:
                    builtins.print("Inconclusive")

            try: browser.close()
            except Exception:
                pass

        except Exception as e:
            builtins.print(f"Error: {e}", file=sys.stderr)
            try:
                if browser:
                    browser.close()
            except Exception:
                pass


# ------------------------------------------------------------------------------
if __name__ == "__main__":
    check_stock()
