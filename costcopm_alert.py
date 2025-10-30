#!/usr/bin/env python3
"""
Costco Precious Metals → Bluesky Alert + Screenshot
Credentials loaded from .env
"""

import os
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from atproto import Client
import sys
from datetime import datetime
from zoneinfo import ZoneInfo  # Python 3.9+

# ----------------------------------------------------------------------
# Load .env file
load_dotenv()  # <-- reads .env in the current directory

USE_BROWSER = os.getenv("BROWSER", "firefox" if not os.getenv("CI") else "chromium")


# Read credentials from environment
BSKY_HANDLE = os.getenv("BSKY_HANDLE")
BSKY_APP_PASSWORD = os.getenv("BSKY_APP_PASSWORD")

# Validate
if not BSKY_HANDLE or not BSKY_APP_PASSWORD:
    print("ERROR: BSKY_HANDLE or BSKY_APP_PASSWORD missing in .env")
    sys.exit(1)

# ----------------------------------------------------------------------
URL = "https://www.costco.com/precious-metals.html"
SCREENSHOT = "costco.png"
TIMEOUT = 90_000

IN_STOCK_TEXT = "Buy Gold Bars and Coins at Costco"
OOS_TEXT = "we're sorry. we were not able to find a match"

# ----------------------------------------------------------------------
from atproto import Client, models
import re

# --- Facets helpers (URLs + Hashtags) ---
HASHTAG_RE = re.compile(r'(?<!\w)#([A-Za-z0-9_]+)')
URL_RE = re.compile(r'https?://[^\s\)\]\}>,]+')  # avoid trailing ) ] } , >

def _byte_slice(text: str, start: int, end: int) -> models.AppBskyRichtextFacet.ByteSlice:
    # Bluesky facets use UTF-8 byte offsets, not codepoints
    byte_start = len(text[:start].encode('utf-8'))
    byte_end = byte_start + len(text[start:end].encode('utf-8'))
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

def post_to_bluesky(image_path: str) -> None:
    try:
        client = Client()
        client.login(BSKY_HANDLE, BSKY_APP_PASSWORD)

        # 1) Upload the image as a blob
        with open(image_path, "rb") as f:
            upload = client.upload_blob(f.read())

        # 2) Build an images embed
        embed = models.AppBskyEmbedImages.Main(
            images=[
                models.AppBskyEmbedImages.Image(
                    image=upload.blob,
                    alt="Costco precious metals page showing gold/silver bars in stock",
                )
            ]
        )

        # 3) Post text with clickable URL + hashtags (via facets)
        now = datetime.now()
        hst = now.astimezone(ZoneInfo("Pacific/Honolulu"))
        pt = now.astimezone(ZoneInfo("America/Los_Angeles"))
        et = now.astimezone(ZoneInfo("America/New_York"))

        timestamp = f"{hst.strftime('%I:%M %p %Z')} / {pt.strftime('%I:%M %p %Z')} / {et.strftime('%I:%M %p %Z')}"

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
def check_stock():
    with sync_playwright() as p:
        try:
            print("Launching browser...")
            if USE_BROWSER == "chromium":
                # CI: force HTTP/1.1 to bypass Cloudflare HTTP/2 block
                chromium_args = [
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-http2",          # THIS LINE FIXES ERR_HTTP2_PROTOCOL_ERROR
                ]
                if os.getenv("CI"):
                    chromium_args.append("--disable-gpu")
                browser = p.chromium.launch(headless=True, args=chromium_args)
                ua = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
            else:
                browser = p.firefox.launch(headless=True, args=["--no-sandbox"])
                ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:129.0) Gecko/20100101 Firefox/129.0"

            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=ua,
                ignore_https_errors=True,   # helps with occasional CDN/TLS quirks on CI
            )
            page = context.new_page()

            # Keep resource-thinning locally; allow full load on CI
            if not os.getenv("CI"):
                page.route("**/*", lambda route: route.abort()
                        if route.request.resource_type in ["stylesheet", "font", "media"]
                        else route.continue_())
            else:
                # On CI, do NOT block stylesheets/JS/images
                pass

            print("Loading Costco...")
            page.goto(URL, wait_until="domcontentloaded", timeout=TIMEOUT)
            page.wait_for_function("document.body && document.body.innerText.length > 200", timeout=60_000)

            print("Waiting for images and product grid to load...")
            page.wait_for_load_state("networkidle", timeout=30_000)
            try:
                page.wait_for_selector("img", state="visible", timeout=15_000)
            except:
                pass

            screenshot_path = os.path.abspath(SCREENSHOT)
            page.screenshot(path=SCREENSHOT, full_page=True)
            print(f"Screenshot saved: {screenshot_path}")

            html = page.content().lower()

            if IN_STOCK_TEXT.lower() in html:
                print("IN STOCK DETECTED!")
                post_to_bluesky(SCREENSHOT)
            elif OOS_TEXT.lower() in html:
                print("Out of stock")
            else:
                print("Inconclusive")

            browser.close()

        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)

# ----------------------------------------------------------------------
if __name__ == "__main__":
    check_stock()