"""MacBook Price Watcher India - cloud price checker.

Runs on a schedule (GitHub Actions). For every retailer URL in data/config.json it:
  1. fetches the product page,
  2. extracts a price (JSON-LD -> meta tags -> retailer-specific patterns),
  3. VERIFIES it (not blocked, spec matches M5 / 13" / 24GB / 1TB, in stock, sane range),
  4. appends the check to data/history.json,
  5. raises alerts (price drop vs previous verified price; reached/below target)
     into data/alerts.json and pushes them to the iPhone via ntfy.

Only VERIFIED prices ever trigger alerts. Standard library only - no pip install needed.

Env:
  NTFY_TOPIC   secret topic name (required for push; without it alerts are only logged)
  NTFY_SERVER  default https://ntfy.sh
  NTFY_TOKEN   optional access token for a protected topic
  TEST_PUSH    "true" -> send one test notification and exit
"""
from __future__ import annotations

import gzip
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG_PATH = DATA / "config.json"
HISTORY_PATH = DATA / "history.json"
ALERTS_PATH = DATA / "alerts.json"

RETAILERS = {
    "amazon": {"name": "Amazon India", "domains": ("amazon.in", "amzn.in")},
    "flipkart": {"name": "Flipkart", "domains": ("flipkart.com", "dl.flipkart.com")},
    "croma": {"name": "Croma", "domains": ("croma.com",)},
    "vijaysales": {"name": "Vijay Sales", "domains": ("vijaysales.com",)},
    "reliance": {"name": "Reliance Digital", "domains": ("reliancedigital.in",)},
    "imagine": {"name": "Imagine Online", "domains": ("imagineonline.store",)},
}

# Apple India part numbers for MacBook Air 13" M5 / 24GB / 1TB (any of these in a listing confirms the spec;
# the 15" equivalents, e.g. MDVN4HN/A, are deliberately absent). Overridable via config "model_numbers".
MODEL_NUMBERS = ["MDHG4HN/A", "MDHK4HN/A", "MDH94HN/A", "MDHD4HN/A"]  # Midnight, Sky Blue, Silver, Starlight

PRICE_MIN, PRICE_MAX = 100_000, 400_000   # sanity band for this SKU (INR)
MAX_HISTORY = 5000
MAX_ALERTS = 300
FAILURE_ALERT_AFTER = 4                    # consecutive unverified checks -> one "checker needs attention" push

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

BLOCK_MARKERS = (
    "captcha", "robot check", "are you a human", "access denied",
    "request blocked", "unusual traffic", "pardon our interruption",
    "something went wrong. please try again", "ddos protection",
)
OOS_MARKERS = (
    "currently unavailable", "out of stock", "sold out", "notify me when available",
    "coming soon", "temporarily unavailable",
)


# --------------------------------------------------------------------------- io

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def fetch(url: str, timeout: int = 30) -> tuple[int, str, str]:
    """Return (status, final_url, text). Status 0 = network error."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
        "Accept-Language": "en-IN,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "no-cache",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status, final = r.read(), r.status, r.geturl()
            enc = (r.headers.get("Content-Encoding") or "").lower()
    except urllib.error.HTTPError as e:
        raw, status, final = e.read() or b"", e.code, url
        enc = (e.headers.get("Content-Encoding") or "").lower() if e.headers else ""
    except Exception as e:  # DNS, TLS, timeout
        return 0, url, f"{type(e).__name__}: {e}"
    try:
        if enc == "gzip":
            raw = gzip.decompress(raw)
        elif enc == "deflate":
            raw = zlib.decompress(raw)
    except Exception:
        pass
    return status, final, raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------- extraction

def parse_price(value) -> float | None:
    """'₹1,89,900.00' / '189900' / 189900.0 -> 189900.0"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = html.unescape(str(value))
    s = re.sub(r"[^\d.]", "", s.replace(",", ""))
    if not s or s.count(".") > 1:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _walk_jsonld(node):
    if isinstance(node, list):
        for x in node:
            yield from _walk_jsonld(x)
    elif isinstance(node, dict):
        yield node
        for k in ("@graph", "mainEntity", "itemListElement"):
            if k in node:
                yield from _walk_jsonld(node[k])


def jsonld_products(page: str) -> list[dict]:
    out = []
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                         page, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        for node in _walk_jsonld(data):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Product" in types:
                out.append(node)
    return out


def price_from_jsonld(page: str) -> tuple[float | None, str | None, str | None, str]:
    """Return (price, title, availability, sku/mpn) from the first Product with an offer price."""
    for prod in jsonld_products(page):
        offers = prod.get("offers")
        offers = offers if isinstance(offers, list) else [offers]
        for off in offers:
            if not isinstance(off, dict):
                continue
            p = parse_price(off.get("price") or off.get("lowPrice"))
            if p is None and isinstance(off.get("priceSpecification"), dict):
                p = parse_price(off["priceSpecification"].get("price"))
            if p:
                sku = " ".join(str(prod.get(k) or "") for k in ("sku", "mpn", "model"))
                return p, prod.get("name"), str(off.get("availability") or ""), sku
    return None, None, None, ""


def meta(page: str, key: str) -> str | None:
    for pat in (rf'<meta[^>]+(?:property|name|itemprop)=["\']{re.escape(key)}["\'][^>]*content=["\']([^"\']+)',
                rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name|itemprop)=["\']{re.escape(key)}["\']'):
        m = re.search(pat, page, re.I)
        if m:
            return html.unescape(m.group(1)).strip()
    return None


def page_title(page: str) -> str | None:
    m = re.search(r'<span[^>]+id=["\']productTitle["\'][^>]*>(.*?)</span>', page, re.S | re.I)
    if m:
        return re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()
    t = meta(page, "og:title")
    if t:
        return t
    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(m.group(1))).strip() if m else None


# Retailer-specific fallbacks: ordered (label, regex) - group 1 is the price.
RETAILER_PATTERNS = {
    "amazon": [
        ("amazon:attach-price", r'id=["\']attach-base-product-price["\'][^>]*value=["\']([\d.,]+)'),
        ("amazon:priceAmount", r'"priceAmount"\s*:\s*([\d.]+)'),
        ("amazon:corePrice", r'(?:corePriceDisplay_desktop_feature_div|corePrice_feature_div|apex_desktop).{0,4000}?'
                             r'class=["\']a-price-whole["\'][^>]*>([\d,]+)'),
    ],
    "flipkart": [
        ("flipkart:sellingPrice", r'"finalPrice"\s*:\s*\{[^}]*?"value"\s*:\s*([\d.]+)'),
        ("flipkart:priceClass", r'class=["\'](?:Nx9bqj[^"\']*|_30jeq3[^"\']*)["\'][^>]*>\s*(?:₹|&#8377;)?\s*([\d,]+)'),
    ],
    "croma": [
        ("croma:sellingPrice", r'"sellingPrice(?:Value)?"\s*:\s*"?(?:₹)?([\d,.]+)'),
        ("croma:amount", r'class=["\']amount["\'][^>]*>\s*(?:₹|&#8377;)?\s*([\d,]+)'),
    ],
    "vijaysales": [
        ("vijaysales:price", r'"(?:special_price|final_price|sellingPrice)"\s*:\s*"?([\d,.]+)'),
    ],
    "reliance": [
        ("reliance:selling_price", r'"selling_price"\s*:\s*"?([\d,.]+)'),
        ("reliance:effective", r'"(?:effective|offer)_price"\s*:\s*\{?[^}]{0,80}?"?value"?\s*:\s*"?([\d,.]+)'),
    ],
}


def croma_api_price(url: str) -> float | None:
    """Croma renders price client-side; its public pricing API is the reliable source."""
    m = re.search(r"/p/(\d+)", url)
    if not m:
        return None
    status, _, text = fetch(f"https://api.croma.com/pricing-services/v1/price?productList={m.group(1)}")
    if status != 200:
        return None
    hit = re.search(r'"sellingPrice(?:Value)?"\s*:\s*"?(?:₹)?([\d,.]+)', text)
    return parse_price(hit.group(1)) if hit else None


def extract(retailer: str, url: str, page: str) -> dict:
    """Return {price, title, availability, source}. Never raises."""
    price, title, avail, sku = price_from_jsonld(page)
    source = "json-ld" if price else None
    if not price:
        p = parse_price(meta(page, "product:price:amount") or meta(page, "og:price:amount") or meta(page, "price"))
        if p:
            price, source = p, "meta"
    if not price:
        for label, pat in RETAILER_PATTERNS.get(retailer, []):
            m = re.search(pat, page, re.S | re.I)
            if m and (p := parse_price(m.group(1))) and PRICE_MIN <= p <= PRICE_MAX:
                price, source = p, label
                break
    if not price and retailer == "croma":
        p = croma_api_price(url)
        if p:
            price, source = p, "croma:api"
    return {
        "price": price,
        "title": title or page_title(page),
        "availability": avail or meta(page, "product:availability") or "",
        "source": source,
        "sku": sku,
    }


# -------------------------------------------------------------- verification

def spec_problems(text: str, models=MODEL_NUMBERS) -> list[str]:
    """Which of the required spec tokens are missing from title/URL/SKU text.
    A known part number (MDHG4HN/A …) confirms the exact config on its own."""
    compact = re.sub(r"[^a-z0-9]", "", text.lower())
    if any(re.sub(r"[^a-z0-9]", "", m.lower()) in compact for m in models):
        return []
    t = text.lower().replace("-", " ")
    missing = []
    if not re.search(r"\bm5\b", t):
        missing.append("M5")
    if not re.search(r"\b24\s*gb\b|\b24gb\b|\b24 gb unified", t):
        missing.append("24GB")
    if not re.search(r"\b1\s*tb\b|\b1tb\b|\b1024\s*gb\b", t):
        missing.append("1TB")
    if not re.search(r"\b13(\.\d)?\b|\b34\.\d+\s*cm", t):
        missing.append('13"')
    if not re.search(r"macbook\s*air", t):
        missing.append("MacBook Air")
    return missing


def verify(retailer: str, url: str, status: int, page: str, ex: dict, models=MODEL_NUMBERS) -> tuple[str, str]:
    """Return (status_code, note). status_code 'verified' is the only alertable one."""
    low = page[:300_000].lower()
    if status == 0:
        return "error", page[:160]
    if status in (403, 429, 503) or (any(m in low for m in BLOCK_MARKERS) and not ex["price"]):
        return "blocked", f"HTTP {status}: retailer blocked the cloud checker"
    if status >= 400:
        return "error", f"HTTP {status}"
    if not ex["price"]:
        return "no_price", "Page loaded but no price found"
    if not (PRICE_MIN <= ex["price"] <= PRICE_MAX):
        return "suspicious", f"Price ₹{ex['price']:,.0f} outside sane range"
    missing = spec_problems(f"{ex['title'] or ''} {ex.get('sku') or ''} {url}", models)
    if missing:
        return "spec_mismatch", "Listing doesn't confirm: " + ", ".join(missing)
    avail = (ex["availability"] or "").lower()
    if "outofstock" in avail.replace(" ", "") or "soldout" in avail.replace(" ", ""):
        return "out_of_stock", "Listed as out of stock"
    if not avail and any(m in low for m in OOS_MARKERS) and ex["source"] not in ("json-ld",):
        # no structured availability: fall back to visible text, but only flag explicit markers near the buy box
        buybox = re.search(r'id=["\'](?:availability|outOfStock)["\'].{0,600}', page, re.S | re.I)
        if buybox and any(m in buybox.group(0).lower() for m in OOS_MARKERS):
            return "out_of_stock", "Listed as unavailable"
    return "verified", f"via {ex['source']}"


# -------------------------------------------------------------------- alerts

def fmt_inr(x: float) -> str:
    """Indian digit grouping: 185000 -> ₹1,85,000"""
    n = int(round(x))
    s = str(n)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        s = f"{head},{tail}"
    return f"₹{s}"


def push(title: str, message: str, url: str | None, priority: int = 4, tags=None) -> bool:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print(f"  [push skipped - NTFY_TOPIC not set] {title}: {message}")
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    body = {"topic": topic, "title": title, "message": message, "priority": priority,
            "tags": tags or []}
    if url:
        body["click"] = url
        body["actions"] = [{"action": "view", "label": "Open listing", "url": url}]
    headers = {"Content-Type": "application/json"}
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
    req = urllib.request.Request(server, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = 200 <= r.status < 300
    except Exception as e:
        print(f"  push failed: {e}")
        return False
    print(f"  pushed: {title}")
    return ok


def build_alerts(rec: dict, prev_verified: dict | None, target: float, name: str) -> list[dict]:
    """Alerts for one VERIFIED check, given the retailer's previous verified check."""
    if rec["status"] != "verified":
        return []
    price, prev = rec["price"], (prev_verified or {}).get("price")
    out = []
    dropped = prev is not None and price < prev
    crossed = price <= target and (prev is None or prev > target)
    if crossed:
        msg = f"{name}: {fmt_inr(price)} — at/below your {fmt_inr(target)} target"
        if dropped:
            msg += f" (down {fmt_inr(prev - price)} from {fmt_inr(prev)})"
        out.append({"type": "target", "title": "🎯 Target price reached", "message": msg, "priority": 5})
    elif dropped:
        msg = f"{name}: {fmt_inr(prev)} → {fmt_inr(price)} (−{fmt_inr(prev - price)})"
        if price <= target:
            msg += " · still below target"
        else:
            msg += f" · {fmt_inr(price - target)} above target"
        out.append({"type": "drop", "title": "📉 Price drop", "message": msg, "priority": 4})
    for a in out:
        a.update(retailer=rec["retailer"], price=price, previous=prev, url=rec["url"], ts=rec["ts"])
    return out


# ---------------------------------------------------------------------- main

def run() -> int:
    if os.environ.get("TEST_PUSH", "").lower() == "true":
        ok = push("✅ MacBook Price Watcher", "Test notification — your iPhone is wired up.", None, 3, ["white_check_mark"])
        return 0 if ok else 1

    config = load_json(CONFIG_PATH, {})
    target = float(config.get("target_price", 185000))
    models = config.get("model_numbers") or MODEL_NUMBERS
    urls = {k: (v or "").strip() for k, v in (config.get("urls") or {}).items()}
    history = load_json(HISTORY_PATH, {"last_run": None, "checks": []})
    alerts = load_json(ALERTS_PATH, [])
    checks = history["checks"]

    active = [(rid, u) for rid, u in urls.items() if rid in RETAILERS and u]
    print(f"Checking {len(active)} retailer(s); target {fmt_inr(target)}")
    for i, (rid, url) in enumerate(active):
        name = RETAILERS[rid]["name"]
        if i:
            time.sleep(random.uniform(3, 8))   # be polite; also looks less bot-like
        status, final, page = fetch(url)
        ex = extract(rid, final, page) if status else {"price": None, "title": None, "availability": "", "source": None}
        code, note = verify(rid, url, status, page, ex, models)
        rec = {
            "ts": now_iso(), "retailer": rid, "url": url, "status": code,
            "verified": code == "verified",
            "price": ex["price"] if code in ("verified", "out_of_stock", "spec_mismatch") else None,
            "title": (ex["title"] or "")[:200], "note": note,
        }
        mine = [c for c in checks if c["retailer"] == rid]
        prev_verified = next((c for c in reversed(mine) if c.get("verified")), None)
        print(f"- {name}: {code} {fmt_inr(rec['price']) if rec['price'] else ''} ({note})")

        new = build_alerts(rec, prev_verified, target, name)
        for a in new:
            push(a["title"], a["message"], url, a["priority"], ["moneybag"] if a["type"] == "target" else ["chart_with_downwards_trend"])
            alerts.append(a)

        # one heads-up when a retailer has been unverifiable for a while (only after it worked before, or from start)
        streak = 0
        for c in reversed(mine + [rec]):
            if c.get("verified"):
                break
            streak += 1
        if streak == FAILURE_ALERT_AFTER:
            msg = f"{name}: last {streak} checks unverified ({note}). Check the URL in Settings."
            push("⚠️ Checker needs attention", msg, url, 2, ["warning"])
            alerts.append({"type": "health", "title": "Checker needs attention", "message": msg,
                           "retailer": rid, "url": url, "ts": rec["ts"]})
        checks.append(rec)

    history["last_run"] = now_iso()
    history["checks"] = checks[-MAX_HISTORY:]
    save_json(HISTORY_PATH, history)
    save_json(ALERTS_PATH, alerts[-MAX_ALERTS:])
    return 0


if __name__ == "__main__":
    sys.exit(run())
