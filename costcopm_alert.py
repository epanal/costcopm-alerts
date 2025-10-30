#!/usr/bin/env python3
"""
Costco Precious Metals → Bluesky Alert (+ Screenshot optional)
Flow (local & CI):
  1) Launch Playwright browser and load Costco precious metals page.
  2) Capture Costco's live JSON API to api-sample.json.
  3) Parse it to count gold/silver and in-stock items.
  4) Post summary to Bluesky (with screenshot if available).

Env:
  CI=true/false
  BROWSER=webkit|firefox|chrome|chromium
  HEADLESS=true|false
  POST_STATUS_UPDATES=true|false            -> post when OOS
  ALWAYS_POST_WHEN_INCONCLUSIVE=true|false  -> post even if signal is inconclusive (no JSON)
  BSKY_HANDLE=you.bsky.social
  BSKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
"""

import os
import re
import sys
import json
import time
import urllib.parse
import requests
from requests.adapters import HTTPAdapter, Retry
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

IS_CI = str(os.getenv("CI", "")).lower() in {"1", "true", "yes", "on"}
USE_BROWSER = os.getenv("BROWSER", "webkit" if IS_CI else "firefox").lower()
HEADLESS = os.getenv("HEADLESS", "true").lower() in {"1", "true", "yes", "on"}

# Bluesky creds (required)
BSKY_HANDLE = os.getenv("BSKY_HANDLE")
BSKY_APP_PASSWORD = os.getenv("BSKY_APP_PASSWORD")
if not BSKY_HANDLE or not BSKY_APP_PASSWORD:
    builtins.print("ERROR: BSKY_HANDLE or BSKY_APP_PASSWORD missing in env/.env")
    sys.exit(1)

# Posting toggles
POST_STATUS_UPDATES = os.getenv("POST_STATUS_UPDATES", "false").lower() in {"1","true","yes","on"}
ALWAYS_POST_WHEN_INCONCLUSIVE = os.getenv("ALWAYS_POST_WHEN_INCONCLUSIVE", "false").lower() in {"1","true","yes","on"}

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

FUSION_BASE = "https://search.costco.com/api/apps/www_costco_com/query/www_costco_com_navigation"

def _requests_session():
    s = requests.Session()
    retries = Retry(
        total=3, backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.costco.com/precious-metals.html",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    return s

def build_fusion_url():
    """
    A conservative, generic query that returns the Precious Metals tiles.
    You can further add your loc=… string later if you want exact warehouse filters.
    """
    params = {
        "expoption": "lucidworks",
        "q": "*:*",
        "locale": "en-US",
        "start": "0",
        "expand": "false",
        "userLocation": "CA",  # or "US"
        # You can also pass the giant 'loc=' list you used earlier if desired.
        # "loc": "653-bd,848-bd,423-wh,..."
    }
    return f"{FUSION_BASE}?{urllib.parse.urlencode(params)}"

def fetch_fusion_via_context(context, save_path: str) -> bool:
    """
    Plan B (CI-safe): call Fusion through Playwright's request client so cookies/headers match the page.
    Returns True if it saved a valid payload.
    """
    try:
        # Build the same URL you used earlier; include userLocation and (optionally) loc
        url = build_fusion_url()  # reuse your existing helper

        # Some endpoints are picky about headers; these are safe & generic.
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.costco.com/precious-metals.html",
            "Origin": "https://www.costco.com",
            "X-Requested-With": "XMLHttpRequest",
        }

        # Use the browser context's request client (shares cookies/session)
        resp = context.request.get(url, headers=headers, timeout=15000)
        if not resp.ok:
            builtins.print(f"[ctx] Fusion GET {resp.status} for {url[:140]}")
            return False

        data = resp.json()
        if not (isinstance(data, dict) and "response" in data and "docs" in data["response"]):
            builtins.print("[ctx] JSON did not look like a Fusion search payload")
            return False

        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        builtins.print(f"[ctx] JSON captured via Playwright context → {save_path}")
        return True

    except Exception as e:
        builtins.print(f"[ctx] fetch failed: {e}")
        return False

def scrape_dom_summary(page) -> dict | None:
    """
    Crawl the rendered product grid to produce a summary like parse_api_json().
    Returns None if no grid appears in time.
    """
    try:
        # Give the SPA a fighting chance on CI
        for _ in range(6):
            try:
                # Wait for any of the common containers
                page.wait_for_selector(
                    "[data-automation='product-tile'], .product-tile, [data-automation='product-grid'] a",
                    timeout=2500,
                )
                break
            except Exception:
                # nudge: scroll and idle a bit
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
                page.wait_for_timeout(800)
        else:
            # never found a tile container
            return None

        # Make sure all lazy tiles have had a chance to attach
        try:
            page.evaluate("""
                () => new Promise(r => {
                    let y = 0, n=0;
                    const tick = () => {
                        window.scrollTo(0, y);
                        y += Math.max(300, innerHeight*0.9);
                        n += 1;
                        if (y >= document.body.scrollHeight || n > 12) return r();
                        setTimeout(tick, 120);
                    };
                    tick();
                })
            """)
        except Exception:
            pass
        page.wait_for_timeout(700)

        # Collect tiles (union of likely selectors)
        tiles = []
        for sel in ("[data-automation='product-tile']", ".product-tile", "[data-automation='product-grid'] a"):
            try:
                count = page.locator(sel).count()
                for i in range(count):
                    tiles.append((sel, i))
            except Exception:
                pass

        if not tiles:
            return None

        def detect_metal_from_text(t: str) -> str:
            s = t.lower()
            if "gold" in s:
                return "gold"
            if "silver" in s:
                return "silver"
            return "other"

        counts = {"gold": 0, "silver": 0, "other": 0}
        stock = {
            "gold": {"in_stock": 0, "out_of_stock": 0},
            "silver": {"in_stock": 0, "out_of_stock": 0},
            "other": {"in_stock": 0, "out_of_stock": 0},
        }

        seen = 0
        for sel, idx in tiles:
            loc = page.locator(sel).nth(idx)
            try:
                txt = (loc.inner_text(timeout=1000) or "").strip()
            except Exception:
                txt = ""
            if not txt:
                continue

            m = detect_metal_from_text(txt)
            counts[m] = counts.get(m, 0) + 1
            seen += 1

            # crude stock signal from tile text
            tlow = txt.lower()
            # many Costco tiles omit "Out of stock"; treat presence of price/“in stock” words as positive
            in_stock = ("in stock" in tlow) or ("$" in tlow) or ("add to cart" in tlow) or ("price" in tlow)
            if in_stock:
                stock[m]["in_stock"] += 1
            else:
                stock[m]["out_of_stock"] += 1

        if seen == 0:
            return None

        return {"numFound": seen, "counts": counts, "stock": stock}

    except Exception:
        return None
    
def fetch_fusion_via_page(page, save_path: str) -> bool:
    """
    Plan A+ (strongest): run fetch() inside the page context so the request
    uses the real browser, cookies, referrer, and CORS.
    Returns True if it saved a valid payload.
    """
    try:
        url = build_fusion_url()

        # Run inside browser; credentials=include sends cookies if any.
        js = """
        async (url) => {
          try {
            const resp = await fetch(url, { credentials: "include" });
            return { ok: resp.ok, status: resp.status, data: await resp.json() };
          } catch (e) {
            return { ok: false, status: 0, error: String(e) };
          }
        }
        """
        result = page.evaluate(js, url)
        if not result.get("ok"):
            builtins.print(f'[page] Fusion GET {result.get("status")} {result.get("error","")} for {url[:140]}')
            return False

        data = result.get("data", {})
        if not (isinstance(data, dict) and "response" in data and "docs" in data["response"]):
            builtins.print("[page] JSON did not look like a Fusion search payload")
            return False

        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        builtins.print(f"[page] JSON captured via in-page fetch → {save_path}")
        return True
    except Exception as e:
        builtins.print(f"[page] fetch failed: {e}")
        return False


def fetch_fusion_direct(save_path: str) -> bool:
    """
    Plan B: call Fusion directly. Returns True if it saved a valid payload.
    """
    try:
        sess = _requests_session()
        url = build_fusion_url()
        r = sess.get(url, timeout=12)
        if r.status_code != 200:
            builtins.print(f"[direct] Fusion GET {r.status_code} for {url[:140]}")
            return False
        data = r.json()
        # sanity check
        if not (isinstance(data, dict) and "response" in data and "docs" in data["response"]):
            builtins.print("[direct] JSON did not look like a Fusion search payload")
            return False
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        builtins.print(f"[direct] JSON captured via HTTP → {save_path}")
        return True
    except Exception as e:
        builtins.print(f"[direct] fetch failed: {e}")
        return False

# ------------------------------------------------------------------------------
# Debug env print
# ------------------------------------------------------------------------------
builtins.print(
    f"[env] CI={IS_CI} BROWSER={USE_BROWSER} HEADLESS={HEADLESS} "
    f"POST_STATUS_UPDATES={POST_STATUS_UPDATES} "
    f"ALWAYS_POST_WHEN_INCONCLUSIVE={ALWAYS_POST_WHEN_INCONCLUSIVE}"
)

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
    # Use form/purity/name to be robust
    forms = " ".join(doc.get("Precious_Metal_Form_attr") or []).lower()
    purity = " ".join(doc.get("Purity_attr") or []).lower()
    name   = (doc.get("item_product_name") or doc.get("name") or "").lower()
    hay = " ".join([forms, purity, name])
    if "gold" in hay:
        return "gold"
    if "silver" in hay:
        return "silver"
    return "other"


def _is_in_stock(doc: dict) -> bool:
    # Prefer explicit boolean; otherwise stockStatus/availability/deliveryStatus
    if "isItemInStock" in doc:
        try:
            return bool(doc["isItemInStock"])
        except Exception:
            pass
    status = (doc.get("item_location_stockStatus")
              or doc.get("item_location_availability")
              or doc.get("deliveryStatus")
              or "").strip().lower()
    return status in {"in stock", "instock", "available"}


def parse_api_json(path: str) -> dict | None:
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
    ts  = f"{hst.strftime('%I:%M %p %Z')} / {pt.strftime('%I:%M %p %Z')} / {et.strftime('%I:%M %p %Z')}"

    gold = summary["counts"].get("gold", 0)
    silver = summary["counts"].get("silver", 0)
    total = summary.get("numFound", gold + silver + summary["counts"].get("other", 0))
    g_in = summary["stock"]["gold"]["in_stock"]
    s_in = summary["stock"]["silver"]["in_stock"]

    status_line = "🚨 Costco Precious Metals Listed!" if (g_in > 0 or s_in > 0) else "Costco Precious Metals — status update"

    text = (
        f"{status_line}\n\n"
        f"🕓 {ts}\n"
        f"Items found: {total} | Gold: {gold} | Silver: {silver}\n"
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
    try:
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
            # webkit (default on CI)
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
                headers = res.headers or {}
                ct = headers.get("content-type", "")
                url = res.url
                if "application/json" not in ct:
                    return
                if not any(host in url for host in ("search.costco.com", "costco.com")):
                    return
                data = res.json()
                looks_like_search = (
                    isinstance(data, dict)
                    and ("response" in data)
                    and isinstance(data["response"], dict)
                    and "docs" in data["response"]
                )
                if looks_like_search:
                    with open(API_JSON_PATH, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2)
                    builtins.print(f"[api] JSON captured from {url[:160]}... -> {API_JSON_PATH}")
            except Exception:
                # swallow noisy JSON errors from unrelated endpoints
                pass

        page.on("response", _on_response)
        return browser, context, page

    except Exception as e:
        # Surface the actual launch error instead of returning None
        raise RuntimeError(f"Failed to launch {USE_BROWSER} (HEADLESS={HEADLESS}, CI={IS_CI}): {e}") from e

# ------------------------------------------------------------------------------
# Main flow
# ------------------------------------------------------------------------------
def check_stock():
    with sync_playwright() as p:
        builtins.print("Launching browser...")
        res = launch_browser(p)
        if not isinstance(res, tuple) or len(res) != 3:
            raise RuntimeError(
                "launch_browser() did not return (browser, context, page). "
                "Ensure it ends with `return browser, context, page`."
            )
        browser, context, page = res

        builtins.print("Loading Costco...")
        resp, last_err = None, None

        try:
            if IS_CI:
                # DOM ready is safer for CI (commit can be too early)
                resp = page.goto(URL, wait_until="domcontentloaded", timeout=30_000)
                # Wait up to 10s for Lucidworks JSON
                try:
                    page.wait_for_response(
                        lambda r: ("search.costco.com" in r.url)
                                  and ("application/json" in (r.headers or {}).get("content-type", "")),
                        timeout=15_000,
                    )
                except Exception:
                    builtins.print("[info] No Lucidworks JSON observed within 10s on CI")
            else:
                # Locally, progressively relax wait conditions
                for wait in ("load", "domcontentloaded", "networkidle"):
                    try:
                        resp = page.goto(URL, wait_until=wait, timeout=TIMEOUT)
                        break
                    except Exception as e:
                        last_err = e
                        builtins.print(f"[goto] {USE_BROWSER} failed ({wait}): {e}")
        except Exception as e:
            last_err = e
            builtins.print(f"[goto] navigation error: {e}")

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

        # Brief pause for XHRs/json to arrive; then screenshot and HTML dump
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

        # Nudge the SPA to fire requests (helps CI sometimes)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(800)
        except Exception:
            pass

        # If the browser hook still didn't capture JSON, try page fetch → context → direct
        if not os.path.exists(API_JSON_PATH):
            builtins.print("[info] No captured JSON yet; attempting Fusion fetch via in-page fetch…")
            ok_page = fetch_fusion_via_page(page, API_JSON_PATH)

            if not ok_page:
                builtins.print("[info] In-page fetch failed; attempting Fusion fetch via browser context…")
                ok_ctx = fetch_fusion_via_context(context, API_JSON_PATH)

                if not ok_ctx:
                    builtins.print("[info] Context fetch failed; attempting plain HTTP fetch…")
                    ok_direct = fetch_fusion_direct(API_JSON_PATH)
                    if ok_direct:
                        builtins.print("[info] Direct fetch succeeded")
                    else:
                        builtins.print("[info] Direct fetch did not return a valid payload")


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
                if POST_STATUS_UPDATES:
                    builtins.print("[info] Posting OOS status update to Bluesky")
                    post_to_bluesky(SCREENSHOT if os.path.exists(SCREENSHOT) else None, text=text)

        else:
            # No JSON captured? Try a robust DOM scrape before giving up.
            dom_summary = scrape_dom_summary(page)
            if dom_summary:
                builtins.print(
                    f"[dom-summary] Parsed {dom_summary['numFound']} tiles → "
                    f"gold={dom_summary['counts']['gold']} (in {dom_summary['stock']['gold']['in_stock']}), "
                    f"silver={dom_summary['counts']['silver']} (in {dom_summary['stock']['silver']['in_stock']})"
                )
                text = build_text_from_summary(dom_summary)
                g_in = dom_summary["stock"]["gold"]["in_stock"]
                s_in = dom_summary["stock"]["silver"]["in_stock"]
                if g_in > 0 or s_in > 0:
                    builtins.print("IN STOCK DETECTED! (DOM)")
                    post_to_bluesky(SCREENSHOT if os.path.exists(SCREENSHOT) else None, text=text)
                else:
                    builtins.print("Out of stock (DOM)")
                    if POST_STATUS_UPDATES:
                        builtins.print("[info] Posting OOS status update to Bluesky (DOM)")
                        post_to_bluesky(SCREENSHOT if os.path.exists(SCREENSHOT) else None, text=text)
            else:
                # Fall back to heuristics from earlier
                if tile_count > 0 or has_terms:
                    builtins.print("IN STOCK DETECTED! (heuristic)")
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
                    if POST_STATUS_UPDATES:
                        now = datetime.now()
                        hst = now.astimezone(ZoneInfo("Pacific/Honolulu"))
                        pt  = now.astimezone(ZoneInfo("America/Los_Angeles"))
                        et  = now.astimezone(ZoneInfo("America/New_York"))
                        ts  = f"{hst.strftime('%I:%M %p %Z')} / {pt.strftime('%I:%M %p %Z')} / {et.strftime('%I:%M %p %Z')}"
                        text = (
                            "Costco Precious Metals — status update\n\n"
                            f"🕓 {ts}\n"
                            "No items currently in stock.\n"
                            "https://www.costco.com/precious-metals.html\n\n"
                            "#Costco #Gold #Silver #CostcoPM"
                        )
                        post_to_bluesky(SCREENSHOT if os.path.exists(SCREENSHOT) else None, text=text)
                else:
                    builtins.print("Inconclusive")
                    if POST_STATUS_UPDATES and ALWAYS_POST_WHEN_INCONCLUSIVE:
                        now = datetime.now()
                        hst = now.astimezone(ZoneInfo("Pacific/Honolulu"))
                        pt  = now.astimezone(ZoneInfo("America/Los_Angeles"))
                        et  = now.astimezone(ZoneInfo("America/New_York"))
                        ts  = f"{hst.strftime('%I:%M %p %Z')} / {pt.strftime('%I:%M %p %Z')} / {et.strftime('%I:%M %p %Z')}"
                        text = (
                            "Costco Precious Metals — status update (signal inconclusive)\n\n"
                            f"🕓 {ts}\n"
                            "Unable to verify stock status from page payload. Monitoring continues.\n"
                            "https://www.costco.com/precious-metals.html\n\n"
                            "#Costco #Gold #Silver #CostcoPM"
                        )
                        post_to_bluesky(SCREENSHOT if os.path.exists(SCREENSHOT) else None, text=text)

        try: browser.close()
        except Exception:
            pass


# ------------------------------------------------------------------------------
if __name__ == "__main__":
    check_stock()
