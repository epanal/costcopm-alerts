#!/usr/bin/env python3
"""
Costco Precious Metals -> Bluesky Alert
- Loads https://www.costco.com/precious-metals.html
- Detects the word "Available" (case-insensitive), with optional keyword filters
- Optional TEST_OOS mode: triggers a dry-run post if "Out of Stock" is present (to verify pipeline)
- Posts to Bluesky using an app password when availability is detected

Setup:
  1) pip install -r requirements.txt
  2) playwright install webkit firefox chromium
  3) Create a .env file based on .env.example

Run:
  python3 costcopm_alert.py

Notes:
  - Be respectful of Costco's Terms of Use. Keep check frequency low.
"""
import os
import sys
import time
from contextlib import suppress

import requests
from requests.exceptions import RequestException
from dotenv import load_dotenv
from atproto import Client


URL = "https://www.costco.com/precious-metals.html"


def load_env() -> dict:
    """Load environment variables and return a config dict."""
    load_dotenv(override=True)
    cfg = {
        "BLSKY_HANDLE": os.getenv("BLSKY_HANDLE", "").strip(),
        "BLSKY_APP_PW": os.getenv("BLSKY_APP_PW", "").strip(),
        "ITEM_KEYWORDS": [k.strip().lower() for k in os.getenv("ITEM_KEYWORDS", "").split(",") if k.strip()],
        "USER_AGENT": os.getenv(
            "USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/119.0.0.0 Safari/537.36"
        ),
        "TIMEOUT_MS": int(os.getenv("TIMEOUT_MS", "20000")),
        "POST_PREFIX": os.getenv("POST_PREFIX", "ALERT:"),
        "DRY_RUN": os.getenv("DRY_RUN", "false").lower() in {"1", "true", "yes", "on"},
        "TEST_OOS": os.getenv("TEST_OOS", "false").lower() in {"1", "true", "yes", "on"},
        "TEST_PREFIX": os.getenv("TEST_PREFIX", "[TEST-OOS]"),
        "FORCE_REQUESTS": os.getenv("FORCE_REQUESTS", "false").lower() in {"1", "true", "yes", "on"},
    }
    if not cfg["BLSKY_HANDLE"] or not cfg["BLSKY_APP_PW"]:
        print("[error] Missing BLSKY_HANDLE or BLSKY_APP_PW in environment.", file=sys.stderr)
        sys.exit(2)
    return cfg


def fetch_page_text(user_agent: str, timeout_ms: int, force_requests: bool = False) -> str:
    """Resilient fetch: requests → webkit → firefox → chromium; returns empty string on failure."""
    def _requests_fetch() -> str:
        r = requests.get(
            URL,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.costco.com/",
                "Connection": "keep-alive",
            },
            timeout=20,
        )
        r.raise_for_status()
        return r.text or ""

    # Plain requests (HTTP/1.1) avoids some HTTP/2 headless flakiness
    try:
        return _requests_fetch()
    except RequestException as e:
        print(f"[warn] requests fetch failed: {e}", file=sys.stderr)
        if force_requests:
            return ""  # respect FORCE_REQUESTS

    # Playwright fallback across engines
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    except Exception as e:
        print(f"[warn] Playwright import failed: {e}", file=sys.stderr)
        return ""

    def try_engine(p, engine_name: str, attempts: int = 2) -> str | None:
        try:
            browser = getattr(p, engine_name).launch(headless=True)
        except Exception as e:
            print(f"[warn] {engine_name} launch failed: {e}", file=sys.stderr)
            return None
        try:
            for attempt in range(attempts):
                context = browser.new_context(
                    user_agent=user_agent,
                    viewport={"width": 1280, "height": 1800},
                    locale="en-US",
                    java_script_enabled=True,
                    bypass_csp=True,
                    extra_http_headers={
                        "Accept-Language": "en-US,en;q=0.9",
                        "Referer": "https://www.costco.com/",
                    },
                )
                page = context.new_page()
                try:
                    # More forgiving than "networkidle"
                    page.goto(URL, wait_until="load", timeout=timeout_ms)
                    time.sleep(1.5)  # small settle
                    try:
                        return page.inner_text("body")
                    except Exception:
                        return page.content() or ""
                except (PlaywrightTimeout, Exception) as e:
                    print(f"[warn] {engine_name} attempt {attempt+1} failed: {e}", file=sys.stderr)
                    time.sleep(1.0 + attempt)  # tiny backoff
                finally:
                    with suppress(Exception):
                        context.close()
        finally:
            with suppress(Exception):
                browser.close()
        return None

    with sync_playwright() as p:
        for engine in ("webkit", "firefox", "chromium"):
            text = try_engine(p, engine)
            if text:
                return text

    print("[warn] All engines failed; returning empty content.", file=sys.stderr)
    return ""


def contains_availability(text: str, keywords: list[str]) -> bool:
    """True if 'available' is found and (if provided) any keyword matches."""
    t = text.lower()
    if "available" not in t:
        return False
    if not keywords:
        return True
    return any(k in t for k in keywords)


def contains_out_of_stock(text: str) -> bool:
    """True if 'out of stock' substring is present (for TEST_OOS mode only)."""
    return "out of stock" in text.lower()


def post_to_bluesky(handle: str, app_pw: str, msg: str):
    """Post a message to Bluesky."""
    client = Client()
    client.login(handle, app_pw)
    client.send_post(msg)


def main():
    cfg = load_env()

    try:
        body_text = fetch_page_text(cfg["USER_AGENT"], cfg["TIMEOUT_MS"], cfg["FORCE_REQUESTS"])
    except Exception as e:
        print(f"[error] Failed to load page: {e}", file=sys.stderr)
        sys.exit(2)

    # --- TEST_OOS mode: verify pipeline even if fetch is empty (blocked/rate-limited)
    if cfg["TEST_OOS"]:
        oos = contains_out_of_stock(body_text)
        fetch_empty = not body_text or not body_text.strip()
        if oos or fetch_empty:
            reason = "found 'Out of Stock'" if oos else "fetch was empty (simulated test)"
            msg = f"""{cfg['TEST_PREFIX']} Costco page test: {reason}.
{URL}"""
            if cfg["DRY_RUN"]:
                print("[dry-run] Would post to Bluesky (TEST_OOS mode):\n", msg)
                sys.exit(0)
            try:
                post_to_bluesky(cfg["BLSKY_HANDLE"], cfg["BLSKY_APP_PW"], msg)
                print("[info] Posted test OOS alert to Bluesky.")
                sys.exit(0)
            except Exception as e:
                print(f"[error] Bluesky post failed (TEST_OOS mode): {e}", file=sys.stderr)
                sys.exit(3)
        else:
            print("[test] No 'Out of Stock' found; test not triggered.")
            # fall through to normal check

    # --- Normal availability check ---
    if contains_availability(body_text, cfg["ITEM_KEYWORDS"]):
        keywords_note = f" | filter: {','.join(cfg['ITEM_KEYWORDS'])}" if cfg["ITEM_KEYWORDS"] else ""
        msg = f"""{cfg['POST_PREFIX']} Costco precious metals page shows 'Available'{keywords_note}.
{URL}"""
        if cfg["DRY_RUN"]:
            print("[dry-run] Would post to Bluesky:\n", msg)
            sys.exit(0)
        try:
            post_to_bluesky(cfg["BLSKY_HANDLE"], cfg["BLSKY_APP_PW"], msg)
            print("[info] Posted to Bluesky.")
            sys.exit(0)
        except Exception as e:
            print(f"[error] Bluesky post failed: {e}", file=sys.stderr)
            sys.exit(3)
    else:
        print("[info] No 'Available' match (or keywords did not match).")
        sys.exit(1)


if __name__ == "__main__":
    main()
