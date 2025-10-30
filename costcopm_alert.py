#!/usr/bin/env python3
"""
Costco Precious Metals → Bluesky Alert + Screenshot
Credentials loaded from .env
"""

import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from atproto import Client, models

# ----------------------------------------------------------------------
load_dotenv()

IS_CI = bool(os.getenv("CI"))
USE_BROWSER = os.getenv("BROWSER", "webkit" if IS_CI else "firefox").lower()
HEADLESS = os.getenv("HEADLESS", "true").lower() in {"1", "true", "yes", "on"}

BSKY_HANDLE = os.getenv("BSKY_HANDLE")
BSKY_APP_PASSWORD = os.getenv("BSKY_APP_PASSWORD")
if not BSKY_HANDLE or not BSKY_APP_PASSWORD:
    print("ERROR: BSKY_HANDLE or BSKY_APP_PASSWORD missing in .env")
    sys.exit(1)

URL = "https://www.costco.com/precious-metals.html"
SCREENSHOT = "costco.png"
TIMEOUT = 90_000  # ms

# ----------------------------------------------------------------------
# Facets helpers (URLs + Hashtags)
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

# ----------------------------------------------------------------------
def post_to_bluesky(image_path: str) -> None:
    try:
        client = Client()
        client.login(BSKY_HANDLE, BSKY_APP_PASSWORD)

        with open(image_path, "rb") as f:
            upload = client.upload_blob(f.read())

        embed = models.AppBskyEmbedImages.Main(
            images=[models.AppBskyEmbedImages.Image(
                image=upload.blob,
                alt="Costco precious metals page showing gold/silver bars in stock",
            )]
        )

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
        facets = build_facets(text)

        client.send_post(text=text, embed=embed, facets=facets or None)
        print("Bluesky post sent!")
    except Exception as e:
        print(f"Bluesky post failed: {e}", file=sys.stderr)

# ----------------------------------------------------------------------
def launch_browser(p):
    """Launch the requested browser.
    Only pass --no-sandbox/--disable-dev-shm-usage for chromium/firefox on CI.
    Never pass them to webkit (it will crash)."""
    args = []

    if USE_BROWSER in ("chromium", "chrome"):
        # On CI, these flags are needed for Chromium; fine to include only here
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

    else:  # webkit
        # IMPORTANT: no --no-sandbox / --disable-dev-shm-usage for webkit
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

    return browser, context, page

# ----------------------------------------------------------------------
def check_stock():
    with sync_playwright() as p:
        try:
            print("Launching browser...")
            browser, context, page = launch_browser(p)

            print("Loading Costco...")
            resp, last_err = None, None

            if IS_CI:
                # On CI/CDN, don't wait for load events that may never fire
                try:
                    resp = page.goto(URL, wait_until="commit", timeout=30_000)
                except Exception as e:
                    last_err = e
                    print(f"[goto] commit failed on CI: {e}")
            else:
                # Local: try normal wait strategies
                for wait in ("load", "domcontentloaded", "networkidle"):
                    try:
                        resp = page.goto(URL, wait_until=wait, timeout=90_000)
                        break
                    except Exception as e:
                        last_err = e
                        print(f"[goto] {USE_BROWSER} failed ({wait}): {e}")

            if resp is None:
                print(f"[error] Page failed to initiate. Last error: {last_err}")
                print("Inconclusive")
                try: browser.close()
                except: pass
                return

            # After commit, give scripts time and try to find content
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass

            for sel in (
                '[data-automation="product-grid"]',
                '[data-automation="product-tile"]',
                '.product-tile',
                '.no-results'
            ):
                try:
                    page.wait_for_selector(sel, timeout=10_000)
                    print(f"[info] Found selector: {sel}")
                    break
                except Exception:
                    pass


            # Screenshot + DOM
            if not page.is_closed():
                screenshot_path = os.path.abspath(SCREENSHOT)
                try:
                    page.screenshot(path=SCREENSHOT, full_page=True)
                    print(f"Screenshot saved: {screenshot_path}")
                except Exception as e:
                    print(f"[warn] screenshot failed: {e}")
                try:
                    with open("page.html", "w", encoding="utf-8") as f:
                        f.write(page.content())
                    print("[debug] HTML dumped to page.html")
                except Exception as e:
                    print(f"[warn] html dump failed: {e}")

            # Diagnostics
            try:
                body_preview = page.inner_text("body")[:800]
            except Exception:
                body_preview = ""
            print("[debug] body preview:", body_preview.replace("\n", " ")[:300])

            tile_count = 0
            for sel in ('[data-automation="product-tile"]', '.product-tile', '[data-automation="product-grid"] a'):
                try:
                    c = page.locator(sel).count()
                    tile_count = max(tile_count, c or 0)
                except Exception:
                    pass
            print(f"[debug] tile_count={tile_count}")

            low = (page.title().lower() + " " + (body_preview or "").lower())
            if any(x in low for x in ("access denied", "request was blocked", "reference #", "problem loading page")):
                print("[warn] Possibly blocked/consent wall. See artifacts.")
                print("Inconclusive")
                try: browser.close()
                except Exception: pass
                return

            is_oos = any(p in low for p in [
                "we were not able to find a match",
                "no results found",
                "did not match any products",
            ])
            has_terms = any(t in low for t in ["gold bar", "gold bars", "silver bar", "silver bars", "precious metals"])

            if tile_count > 0 or has_terms:
                print("IN STOCK DETECTED!")
                post_to_bluesky(SCREENSHOT)
            elif is_oos:
                print("Out of stock")
            else:
                print("Inconclusive")

            try: browser.close()
            except Exception: pass

        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)

# ----------------------------------------------------------------------
if __name__ == "__main__":
    check_stock()
