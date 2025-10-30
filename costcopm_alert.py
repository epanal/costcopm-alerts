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
# Load .env file
load_dotenv()  # reads .env in the current directory

# Read credentials from environment
BSKY_HANDLE = os.getenv("BSKY_HANDLE")
BSKY_APP_PASSWORD = os.getenv("BSKY_APP_PASSWORD")

# Validate credentials
if not BSKY_HANDLE or not BSKY_APP_PASSWORD:
    print("ERROR: BSKY_HANDLE or BSKY_APP_PASSWORD missing in .env")
    sys.exit(1)

# ----------------------------------------------------------------------
URL = "https://www.costco.com/precious-metals.html"
SCREENSHOT = "costco.png"
TIMEOUT = 90_000

# ----------------------------------------------------------------------
# --- Facet helpers (URLs + Hashtags) ---
HASHTAG_RE = re.compile(r"(?<!\w)#([A-Za-z0-9_]+)")
URL_RE = re.compile(r"https?://[^\s\)\]\}>,]+")


def _byte_slice(text: str, start: int, end: int) -> models.AppBskyRichtextFacet.ByteSlice:
    """Convert char offsets to UTF-8 byte offsets (required by Bluesky facets)."""
    byte_start = len(text[:start].encode("utf-8"))
    byte_end = byte_start + len(text[start:end].encode("utf-8"))
    return models.AppBskyRichtextFacet.ByteSlice(byte_start=byte_start, byte_end=byte_end)


def build_facets(text: str):
    facets = []

    # Hashtags
    for m in HASHTAG_RE.finditer(text):
        tag = m.group(1)
        facets.append(
            models.AppBskyRichtextFacet.Main(
                features=[models.AppBskyRichtextFacet.Tag(tag=tag)],
                index=_byte_slice(text, m.start(), m.end()),
            )
        )

    # Links
    for m in URL_RE.finditer(text):
        url = m.group(0)
        facets.append(
            models.AppBskyRichtextFacet.Main(
                features=[models.AppBskyRichtextFacet.Link(uri=url)],
                index=_byte_slice(text, m.start(), m.end()),
            )
        )

    return facets


# ----------------------------------------------------------------------
def post_to_bluesky(image_path: str) -> None:
    """Uploads a screenshot and posts to Bluesky with hashtags and timestamp."""
    try:
        client = Client()
        client.login(BSKY_HANDLE, BSKY_APP_PASSWORD)

        # Upload image as blob
        with open(image_path, "rb") as f:
            upload = client.upload_blob(f.read())

        # Build image embed
        embed = models.AppBskyEmbedImages.Main(
            images=[
                models.AppBskyEmbedImages.Image(
                    image=upload.blob,
                    alt="Costco precious metals page showing gold/silver bars in stock",
                )
            ]
        )

        # Timestamp in HST + ET
        now_hst = datetime.now(ZoneInfo("Pacific/Honolulu"))
        now_et = datetime.now(ZoneInfo("America/New_York"))
        timestamp = f"{now_hst.strftime('%I:%M %p %Z')} / {now_et.strftime('%I:%M %p %Z')}"

        # Post body
        text = (
            f"🚨 Costco Precious Metals IN STOCK!\n\n"
            f"🕓 {timestamp}\n"
            f"https://www.costco.com/precious-metals.html\n\n"
            "#Costco #Gold #Silver #CostcoPM"
        )

        facets = build_facets(text)
        client.send_post(text=text, embed=embed, facets=facets if facets else None)
        print("Bluesky post sent!")

    except Exception as e:
        print(f"Bluesky post failed: {e}", file=sys.stderr)


# ----------------------------------------------------------------------
def normalize(s: str) -> str:
    return re.sub(r"[\W_]+", " ", s.lower()).strip()


def check_stock():
    """Check Costco precious metals page for in-stock items."""
    with sync_playwright() as p:
        try:
            print("Launching browser...")
            try:
                # Try Firefox first
                browser = p.firefox.launch(headless=True, args=["--no-sandbox"])
            except Exception:
                print("[warn] Firefox launch failed; falling back to Chromium.")
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])

            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:129.0) Gecko/20100101 Firefox/129.0",
            )
            page = context.new_page()

            # Optionally skip heavy assets (CSS/fonts/images)
            page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in ["font", "media"]
                else route.continue_(),
            )

            print("Loading Costco...")
            page.goto(URL, wait_until="domcontentloaded", timeout=TIMEOUT)
            page.wait_for_function("document.body && document.body.innerText.length > 200", timeout=60_000)

            print("Waiting for product grid or fallback text...")
            page.wait_for_load_state("networkidle", timeout=30_000)

            selectors = [
                '[data-automation="product-grid"]',
                '[data-automation="product-tile"]',
                ".product-tile",
                ".no-results",
                'text=/we\\s*re\\s*sorry/i',
            ]
            for sel in selectors:
                try:
                    page.wait_for_selector(sel, timeout=10_000)
                    break
                except Exception:
                    pass

            screenshot_path = os.path.abspath(SCREENSHOT)
            page.screenshot(path=SCREENSHOT, full_page=True)
            print(f"Screenshot saved: {screenshot_path}")

            title = page.title()
            body_text = page.inner_text("body")[:2000]
            html = page.content()

            # --- Bot protection check ---
            if (
                "access denied" in title.lower()
                or "reference #" in body_text.lower()
                or "request was blocked" in body_text.lower()
            ):
                print("[warn] Likely blocked by bot protection (Akamai/Edge). Title:", title)
                print("[debug] Body preview:", normalize(body_text)[:300])
                print("Inconclusive")
                return

            # --- Detect stock status ---
            norm = normalize(body_text)
            OOS_PHRASES = [
                "we were not able to find a match",
                "no results found",
                "did not match any products",
            ]
            is_oos = any(p in norm for p in OOS_PHRASES)

            # Product tiles
            product_tile_count = 0
            for sel in (
                '[data-automation="product-tile"]',
                ".product-tile",
                '[data-automation="product-grid"] a',
            ):
                try:
                    c = page.locator(sel).count()
                    if c and c > product_tile_count:
                        product_tile_count = c
                except Exception:
                    pass

            has_precious_terms = any(
                t in norm for t in ["gold bar", "gold bars", "silver bar", "silver bars", "precious metals"]
            )

            print(
                f"[debug] title='{title}' tiles={product_tile_count} oos={is_oos} keywords={has_precious_terms}"
            )

            if product_tile_count > 0 or has_precious_terms:
                print("IN STOCK DETECTED!")
                post_to_bluesky(SCREENSHOT)
            elif is_oos:
                print("Out of stock")
            else:
                print("Inconclusive")

            browser.close()

        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)


# ----------------------------------------------------------------------
if __name__ == "__main__":
    check_stock()
