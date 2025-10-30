#!/usr/bin/env python3
"""
Costco Precious Metals → Bluesky Alert (CI-safe, no external fetch hacks)

Flow:
  1) Launch Playwright and open the Precious Metals page.
  2) Capture JSON via network hook (fast path).
  3) If not captured, mine the HAR (works even when API is 401 via normal fetch).
  4) If still missing, scrape DOM tiles.
  5) Post summary to Bluesky (OOS/inconclusive posting is configurable).

Env:
  CI=true/false
  BROWSER=webkit|firefox|chrome|chromium     (defaults: webkit on CI, firefox locally)
  HEADLESS=true|false                        (defaults: true)
  POST_STATUS_UPDATES=true|false             (post even when OOS)
  ALWAYS_POST_WHEN_INCONCLUSIVE=true|false   (post even when signal is inconclusive)
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
HAR_PATH = "run.har"
SCREENSHOT = "costco.png"
TIMEOUT = 90_000  # ms

OOS_PATTERNS = [
    "we were not able to find a match",
    "no results found",
    "did not match any products",
]
IN_STOCK_TERMS = ["gold bar", "gold bars", "silver bar", "silver bars", "precious metals"]

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
    forms = " ".join(doc.get("Precious_Metal_Form_attr") or []).lower()
    purity = " ".join(doc.get("Purity_attr") or []).lower()
    name   = (doc.get("item_product_name") or doc.get("name") or "").lower()
    hay = " ".join([forms, purity, name])
    if "gold" in hay: return "gold"
    if "silver" in hay: return "silver"
    return "other"


def _is_in_stock(doc: dict) -> bool:
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

    return {"numFound": num_found, "counts": counts, "stock": stock}

# ------------------------------------------------------------------------------
# HAR miner (CI-reliable)
# ------------------------------------------------------------------------------
def extract_api_from_har(har_path: str, out_path: str) -> bool:
    """
    Scan HAR for a Costco/Lucidworks JSON payload with response.docs; write to out_path.
    Returns True if found.
    """
    try:
        if not os.path.exists(har_path):
            return False
        with open(har_path, "r", encoding="utf-8") as f:
            har = json.load(f)

        entries = har.get("log", {}).get("entries", [])
        best = None

        for e in entries:
            res = e.get("response", {})
            req = e.get("request", {})
            url = req.get("url", "")
            # content-type header
            cth = ""
            for h in res.get("headers", []):
                if h.get("name", "").lower() == "content-type":
                    cth = h.get("value", "")
                    break

            if "application/json" not in cth:
                continue
            if not any(host in url for host in ("search.costco.com", "www.costco.com", "costco.com")):
                continue

            text = res.get("content", {}).get("text", "")
            if not text:
                continue
            try:
                data = json.loads(text)
            except Exception:
                continue

            if isinstance(data, dict) and "response" in data and isinstance(data["response"], dict) and "docs" in data["response"]:
                docs = data["response"].get("docs") or []
                score = len(docs)
                if not best or score > best[0]:
                    best = (score, data)

        if best:
            _, data = best
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            builtins.print(f"[har] JSON extracted from HAR → {out_path}")
            return True

        return False
    except Exception as e:
        builtins.print(f"[har] parse error: {e}")
        return False

def force_load_images_and_deblur(page) -> None:
    """Force eager-load of lazy images, strip blur/skeleton styles, and wait for all images to render."""
    try:
        # Make all images eager, swap data-src/srcset into src, and nuke common skeleton/blur styles.
        page.evaluate("""
        () => {
          // 1) Kill common skeleton/blur classes & inline filters
          const killSelectors = [
            '.skeleton', '.Skeleton', '.shimmer', '.placeholder', '[class*="skeleton"]',
            '[class*="Shimmer"]', '[style*="filter: blur("]', '[style*="backdrop-filter"]'
          ];
          for (const sel of killSelectors) {
            document.querySelectorAll(sel).forEach(el => {
              el.style.filter = 'none';
              el.style.backdropFilter = 'none';
              el.style.animation = 'none';
              el.style.opacity = '1';
            });
          }

          // 2) Force-load <img> elements
          const imgs = Array.from(document.images || []);
          for (const img of imgs) {
            try {
              img.loading = 'eager';
              img.decoding = 'sync';
              // If site uses data-src / data-srcset, upgrade them
              const dsrc = img.getAttribute('data-src') || img.getAttribute('data-original') || img.getAttribute('data-lazy');
              const dsrcset = img.getAttribute('data-srcset');
              if (dsrcset && !img.srcset) img.srcset = dsrcset;
              if (dsrc && img.src !== dsrc) img.src = dsrc;
              // Remove CSS blur on the image itself
              img.style.filter = 'none';
              img.style.opacity = '1';
            } catch(e) {}
          }

          // 3) Trigger lazy observers (scroll up/down a bit)
          const nudge = () => {
            window.scrollBy(0, Math.max(200, innerHeight * 0.8));
            window.scrollBy(0, -Math.max(150, innerHeight * 0.6));
          };
          for (let i=0;i<4;i++) nudge();
        }
        """)

        # Progressive scroll to trigger any remaining lazy assets
        page.evaluate("""
            () => new Promise(resolve => {
              let y = 0, steps = 0;
              const step = () => {
                window.scrollTo(0, y);
                y += Math.max(300, innerHeight * 0.9);
                steps++;
                if (y >= document.body.scrollHeight || steps > 20) return resolve();
                setTimeout(step, 120);
              };
              step();
            })
        """)
        page.wait_for_timeout(800)

        # Wait until all images are actually decoded (renders un-greyed)
        page.wait_for_function("""
            () => {
              const imgs = Array.from(document.images || []);
              return imgs.length === 0 || imgs.every(i => i.complete && i.naturalWidth > 0);
            }
        """, timeout=7000)

        # One more idle pause
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass

        # Back to top for clean capture
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(250)
    except Exception as e:
        builtins.print(f"[warn] force_load_images_and_deblur failed: {e}")

def take_best_screenshot(page, path: str, *, min_bytes: int = 200_000) -> None:
    """Try to capture the product grid area first; fallback to full page, with retries."""
    try:
        # Ensure the page is visually ready
        try:
            page.wait_for_selector(
                "[data-automation='product-grid'], [data-automation='product-tile'], .product-tile",
                timeout=10_000
            )
        except Exception:
            pass

        force_load_images_and_deblur(page)

        # Prefer a tight crop of the grid if possible (less header/footer noise)
        grid = None
        for sel in ("[data-automation='product-grid']", ".product-grid", "[data-automation='product-tile']"):
            try:
                if page.locator(sel).count() > 0:
                    grid = page.locator(sel).first
                    break
            except Exception:
                pass

        if grid is not None:
            try:
                grid.screenshot(path=path)
            except Exception as e:
                builtins.print(f"[warn] grid screenshot failed: {e}")

        # If no grid shot saved or it's suspiciously small, take full-page
        need_full = True
        try:
            if os.path.exists(path) and os.path.getsize(path) >= min_bytes:
                need_full = False
        except Exception:
            pass

        if need_full:
            page.screenshot(path=path, full_page=True)
            # Retry once if tiny (late lazy-loaders)
            try:
                if os.path.getsize(path) < min_bytes:
                    page.wait_for_timeout(1500)
                    page.screenshot(path=path, full_page=True)
            except Exception:
                pass
    except Exception as e:
        builtins.print(f"[warn] take_best_screenshot failed: {e}")
        try:
            page.screenshot(path=path, full_page=True)
        except Exception:
            pass

# ------------------------------------------------------------------------------
# DOM scrape fallback
# ------------------------------------------------------------------------------
def scrape_dom_summary(page) -> dict | None:
    """Produce a summary from rendered tiles (last resort)."""
    try:
        # try a few cycles: wait, scroll, idle
        for _ in range(6):
            try:
                page.wait_for_selector(
                    "[data-automation='product-tile'], .product-tile, [data-automation='product-grid'] a",
                    timeout=2500,
                )
                break
            except Exception:
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
                page.wait_for_timeout(800)
        else:
            return None

        # Gather tiles
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
            if "gold" in s: return "gold"
            if "silver" in s: return "silver"
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

            tlow = txt.lower()
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

# ------------------------------------------------------------------------------
# Text builder + Bluesky
# ------------------------------------------------------------------------------
def build_text_from_summary(summary: dict) -> str:
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
# Browser launcher (records HAR)
# ------------------------------------------------------------------------------
def launch_browser(p):
    try:
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
            browser = p.webkit.launch(headless=HEADLESS, args=[])
            ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15")

        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=ua,
            ignore_https_errors=True,
            locale="en-US",
            timezone_id="America/Los_Angeles",
            # HAR recording for CI
            record_har_path=HAR_PATH,
            record_har_omit_content=False,
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

        # Response hook (fast path)
        def _on_response(res):
            try:
                ct = (res.headers or {}).get("content-type", "")
                url = res.url
                if "application/json" not in ct:
                    return
                if not any(host in url for host in ("search.costco.com", "costco.com")):
                    return
                data = res.json()
                if isinstance(data, dict) and "response" in data and "docs" in data["response"]:
                    with open(API_JSON_PATH, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2)
                    builtins.print(f"[api] JSON captured from {url[:160]}... -> {API_JSON_PATH}")
            except Exception:
                pass

        page.on("response", _on_response)
        return browser, context, page

    except Exception as e:
        raise RuntimeError(f"Failed to launch {USE_BROWSER} (HEADLESS={HEADLESS}, CI={IS_CI}): {e}") from e

## Screenshot
def take_fully_loaded_screenshot(page, path: str, *, min_bytes: int = 200_000) -> None:
    """Scrolls to trigger lazy-loading, waits for images to load, then captures a full-page screenshot."""
    try:
        # 1) Wait for something meaningful on the page
        try:
            page.wait_for_selector("[data-automation='product-grid'], [data-automation='product-tile'], .product-tile", timeout=10_000)
        except Exception:
            pass

        # 2) Nudge network to settle
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        # 3) Progressive scroll to trigger lazy images
        page.evaluate("""
            () => new Promise(resolve => {
              let y = 0, steps = 0;
              const step = () => {
                window.scrollTo(0, y);
                y += Math.max(300, innerHeight * 0.9);
                steps++;
                if (y >= document.body.scrollHeight || steps > 20) return resolve();
                setTimeout(step, 120);
              };
              step();
            })
        """)
        page.wait_for_timeout(700)

        # 4) Wait for images to finish loading
        page.wait_for_function("""
            () => Array.from(document.images || []).every(img => img.complete && img.naturalWidth > 0)
        """, timeout=7000)

        # 5) One more idle wait
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass

        # 6) Return to top for a nicer screenshot
        try:
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(250)
        except Exception:
            pass

        # 7) Capture full-page
        page.screenshot(path=path, full_page=True)

        # 8) If it looks suspiciously small, retry once after another settle
        try:
            import os
            if os.path.exists(path) and os.path.getsize(path) < min_bytes:
                page.wait_for_timeout(1500)
                page.screenshot(path=path, full_page=True)
        except Exception:
            pass

    except Exception as e:
        builtins.print(f"[warn] take_fully_loaded_screenshot failed: {e}")
        # Fallback—still try to save *something*
        try:
            page.screenshot(path=path, full_page=True)
        except Exception:
            pass

# ------------------------------------------------------------------------------
# Main flow
# ------------------------------------------------------------------------------
def check_stock():
    with sync_playwright() as p:
        builtins.print("Launching browser...")
        res = launch_browser(p)
        if not isinstance(res, tuple) or len(res) != 3:
            raise RuntimeError("launch_browser() did not return (browser, context, page).")
        browser, context, page = res

        builtins.print("Loading Costco...")
        resp, last_err = None, None

        try:
            if IS_CI:
                resp = page.goto(URL, wait_until="domcontentloaded", timeout=30_000)
                try:
                    page.wait_for_response(
                        lambda r: (("search.costco.com" in r.url) or ("costco.com" in r.url))
                                  and ("application/json" in (r.headers or {}).get("content-type", "")),
                        timeout=20_000,
                    )
                except Exception:
                    builtins.print("[info] No Lucidworks JSON observed within 10s on CI")
            else:
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

        # Cookie banner
        try:
            page.locator("#onetrust-accept-btn-handler, button:has-text('Accept All Cookies')").first.click(timeout=2500)
            builtins.print("[info] Cookie banner accepted")
        except Exception:
            pass

        # Brief settle
        try:
            page.wait_for_timeout(2500)
        except Exception:
            pass

        # Artifacts
        if not page.is_closed():
            try:
                take_best_screenshot(page, SCREENSHOT)
                builtins.print(f"Screenshot saved: {os.path.abspath(SCREENSHOT)}")
            except Exception as e:
                builtins.print(f"[warn] screenshot failed: {e}")
            try:
                with open("page.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                builtins.print("[debug] HTML dumped to page.html")
            except Exception as e:
                builtins.print(f"[warn] html dump failed: {e}")


        # Let late XHRs land, then mine HAR if needed
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        if not os.path.exists(API_JSON_PATH):
            if extract_api_from_har(HAR_PATH, API_JSON_PATH):
                builtins.print("[info] HAR mining succeeded")

        # Parse JSON if we have it
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

        # Heuristics / quick signals
        try:
            body_preview = page.inner_text("body")[:2000]
        except Exception:
            body_preview = ""
        low = (page.title().lower() + " " + body_preview.lower())

        if any(x in low for x in ("access denied", "request was blocked", "reference #", "problem loading page")):
            builtins.print("[warn] Possibly blocked/consent wall. See artifacts.")
            builtins.print("Inconclusive")
            try: browser.close()
            except Exception: pass
            return

        # Fallback: DOM tile count (for posting heuristics if needed)
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

        # ---- Decide & post ----
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
            # No JSON captured? Try DOM scrape before giving up.
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
                # Heuristic fallback
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
