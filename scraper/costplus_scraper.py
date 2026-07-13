#!/usr/bin/env python3
"""
Cost Plus Drugs medication directory collector.

Collects the entire medication directory from costplusdrugs.com, including
the "Retail Price at Other Pharmacies" comparison figure for each medication,
and writes the results into a SQLite database that is fully rebuilt
(overwritten) on every run.

How it works
-------------
Cost Plus Drugs' product pages are server-rendered by Next.js. Each page's
initial HTML already contains the full product JSON payload (price,
retail-price comparison, strength, package size, sibling dose variants,
etc.) inside a React Server Component "flight" script tag -- no JavaScript
execution or headless browser is required. This script:

  1. Downloads sitemap.xml and extracts every /medications/<slug>/ URL
     (skipping the /medications/categories/* taxonomy pages).
  2. Fetches each medication page with a pool of worker threads.
  3. Extracts the embedded `productDetails` JSON blob from the raw HTML.
  4. Writes every medication into a fresh SQLite database file, then
     atomically swaps it in to replace last week's database.

Usage
-----
    python3 costplus_scraper.py

Output (written into ./output/)
------
    costplusdrugs.db      -- the live database (always the latest run)
    costplusdrugs.csv      -- CSV export of the `medications` table
    last_run.json          -- run metadata (timestamp, counts, failures)
"""

import concurrent.futures
import csv
import json
import os
import re
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "https://www.costplusdrugs.com"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(WORKSPACE_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

DB_PATH = os.path.join(OUTPUT_DIR, "costplusdrugs.db")
DB_TMP_PATH = os.path.join(OUTPUT_DIR, "costplusdrugs.db.building")
CSV_PATH = os.path.join(OUTPUT_DIR, "costplusdrugs.csv")
LAST_RUN_PATH = os.path.join(OUTPUT_DIR, "last_run.json")
LOG_PATH = os.path.join(OUTPUT_DIR, "scraper.log")

MAX_WORKERS = 12          # concurrent page fetches -- kept moderate to be polite
REQUEST_TIMEOUT = 25      # seconds
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

MEDICATION_URL_RE = re.compile(
    r"https://www\.costplusdrugs\.com/medications/(?!categories/)[^/\s<]+/?"
)


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------------------
# Step 1: discover all medication URLs from the sitemap
# ---------------------------------------------------------------------------

def fetch_medication_urls(session: requests.Session) -> list[str]:
    resp = session.get(SITEMAP_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    urls = sorted(set(MEDICATION_URL_RE.findall(resp.text)))
    normalized = []
    for u in urls:
        if not u.endswith("/"):
            u += "/"
        normalized.append(u)
    return sorted(set(normalized))


# ---------------------------------------------------------------------------
# Step 2: fetch + parse a single medication page
# ---------------------------------------------------------------------------

PRODUCT_MARKER = '\\"productDetails\\":{'


def _unescape_js_string(fragment: str) -> str:
    """Undo the JS-string escaping wrapping the embedded JSON payload."""
    return (
        fragment.replace("\\\\", "\x00BSLASH\x00")
        .replace('\\"', '"')
        .replace("\x00BSLASH\x00", "\\")
    )


def extract_product_json(html: str) -> dict | None:
    """Pull the `productDetails` JSON object out of a Cost Plus Drugs page."""
    idx = html.find(PRODUCT_MARKER)
    if idx == -1:
        return None
    window = html[idx + len('\\"productDetails\\":'):]
    unescaped = _unescape_js_string(window)
    decoder = json.JSONDecoder()
    try:
        obj, _end = decoder.raw_decode(unescaped)
    except json.JSONDecodeError:
        return None
    return obj


def fetch_page(session: requests.Session, url: str) -> tuple[str, dict | None, str | None]:
    """Returns (url, parsed_product_dict_or_None, error_message_or_None)."""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 404:
                return url, None, "404 not found"
            resp.raise_for_status()
            product = extract_product_json(resp.text)
            if product is None:
                return url, None, "product JSON not found in page"
            return url, product, None
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    return url, None, last_err


# ---------------------------------------------------------------------------
# Step 3: shape a parsed product dict into flat row(s)
# ---------------------------------------------------------------------------

def slug_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def _find_matching_variant(url: str, product: dict) -> dict:
    """The top-level `productDetails` object represents the whole product
    family (e.g. "Atorvastatin"); dose/package-specific fields like
    strength, package size, and SKU only live on the individual variant
    that matches the page's own URL slug. Find that variant so each row
    represents exactly the dose shown on its page."""
    page_slug = slug_from_url(url)
    for variant in product.get("variants") or []:
        vmeta = variant.get("metafields") or {}
        if (vmeta.get("slug") or "").lower() == page_slug.lower():
            return variant
    variants = product.get("variants") or []
    return variants[0] if variants else {}


DEFAULT_PILL_QTY = 30
DEFAULT_NONPILL_QTY = 1


def build_medication_row(url: str, product: dict) -> dict:
    product_metafields = product.get("metafields") or {}
    matching_variant = _find_matching_variant(url, product)
    variant_metafields = matching_variant.get("metafields") or {}
    metafields = {**product_metafields, **variant_metafields}

    collections = product.get("collections") or []
    category_names = ", ".join(c.get("name", "") for c in collections if c.get("name"))
    category_slugs = ", ".join(c.get("slug", "") for c in collections if c.get("slug"))

    our_price = matching_variant.get("priceCalculation")
    if our_price is None:
        our_price = product.get("priceCalculation")

    retail_price_per_unit = _to_float(metafields.get("retailPricePerUnit"))
    pill_non_pill = (metafields.get("pillNonPill") or "").strip().lower()
    default_qty = DEFAULT_PILL_QTY if pill_non_pill == "pill" else DEFAULT_NONPILL_QTY

    retail_price = None
    if retail_price_per_unit is not None:
        retail_price = round(retail_price_per_unit * default_qty, 2)
    elif isinstance(product.get("retailPrice"), (int, float)):
        retail_price = product.get("retailPrice")

    savings_amount = None
    savings_percent = None
    if isinstance(our_price, (int, float)) and isinstance(retail_price, (int, float)) and retail_price > 0:
        savings_amount = round(retail_price - our_price, 2)
        savings_percent = round((savings_amount / retail_price) * 100, 2)

    return {
        "slug": slug_from_url(url),
        "url": url,
        "name": product.get("name"),
        "brand_name": metafields.get("brandName"),
        "brand_generic": metafields.get("brandGeneric") or metafields.get("brandGenric"),
        "form": metafields.get("form") or metafields.get("pillNonPill"),
        "strength": metafields.get("strength"),
        "package_size": metafields.get("package_size"),
        "sku": metafields.get("sku"),
        "our_price": our_price,
        "retail_price_other_pharmacies": retail_price,
        "retail_price_per_unit": retail_price_per_unit,
        "default_quantity": default_qty,
        "savings_amount": savings_amount,
        "savings_percent": savings_percent,
        "is_available": bool(matching_variant.get("isActive", product.get("isAvailable"))),
        "is_available_for_pharmacy_pickup": bool(product.get("isAvailableForPharmacyPickup")),
        "insurance_eligible": metafields.get("insuranceEligible"),
        "stock_status": metafields.get("stockStatus"),
        "categories": category_names,
        "category_slugs": category_slugs,
        "raw_json": json.dumps(product, ensure_ascii=False),
    }


def build_sibling_variant_rows(url: str, product: dict) -> list[dict]:
    parent_slug = slug_from_url(url)
    rows = []
    for variant in product.get("variants") or []:
        vmeta = variant.get("metafields") or {}
        rows.append(
            {
                "parent_slug": parent_slug,
                "variant_slug": vmeta.get("slug"),
                "variant_sku": variant.get("sku"),
                "strength": vmeta.get("strength"),
                "package_size": vmeta.get("package_size"),
                "our_price": variant.get("priceCalculation"),
                "retail_price_per_unit": _to_float(vmeta.get("retailPricePerUnit")),
            }
        )
    return rows


def _to_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Step 4: write everything into a fresh SQLite database
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE medications (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug                            TEXT UNIQUE NOT NULL,
    url                             TEXT NOT NULL,
    name                            TEXT,
    brand_name                      TEXT,
    brand_generic                   TEXT,
    form                            TEXT,
    strength                        TEXT,
    package_size                    TEXT,
    sku                             TEXT,
    our_price                       REAL,
    retail_price_other_pharmacies   REAL,
    retail_price_per_unit           REAL,
    default_quantity                INTEGER,
    savings_amount                  REAL,
    savings_percent                 REAL,
    is_available                    INTEGER,
    is_available_for_pharmacy_pickup INTEGER,
    insurance_eligible               TEXT,
    stock_status                    TEXT,
    categories                      TEXT,
    category_slugs                  TEXT,
    raw_json                        TEXT,
    scraped_at                      TEXT NOT NULL
);
CREATE INDEX idx_medications_name ON medications(name);
CREATE INDEX idx_medications_slug ON medications(slug);
CREATE INDEX idx_medications_category_slugs ON medications(category_slugs);

CREATE TABLE medication_sibling_variants (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_slug           TEXT NOT NULL,
    variant_slug          TEXT,
    variant_sku           TEXT,
    strength              TEXT,
    package_size          TEXT,
    our_price             REAL,
    retail_price_per_unit REAL,
    scraped_at             TEXT NOT NULL
);
CREATE INDEX idx_variants_parent_slug ON medication_sibling_variants(parent_slug);

CREATE TABLE run_meta (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_started_at     TEXT NOT NULL,
    run_finished_at    TEXT,
    total_urls         INTEGER,
    success_count      INTEGER,
    failure_count      INTEGER,
    source_sitemap     TEXT
);
"""


def write_database(rows: list[dict], variant_rows: list[dict], run_meta: dict) -> None:
    if os.path.exists(DB_TMP_PATH):
        os.remove(DB_TMP_PATH)

    conn = sqlite3.connect(DB_TMP_PATH)
    try:
        conn.executescript(SCHEMA_SQL)

        med_cols = [
            "slug", "url", "name", "brand_name", "brand_generic", "form", "strength",
            "package_size", "sku", "our_price", "retail_price_other_pharmacies",
            "retail_price_per_unit", "default_quantity", "savings_amount", "savings_percent", "is_available",
            "is_available_for_pharmacy_pickup", "insurance_eligible", "stock_status",
            "categories", "category_slugs", "raw_json", "scraped_at",
        ]
        placeholders = ", ".join("?" for _ in med_cols)
        conn.executemany(
            f"INSERT INTO medications ({', '.join(med_cols)}) VALUES ({placeholders})",
            [tuple(r.get(c) for c in med_cols) for r in rows],
        )

        var_cols = [
            "parent_slug", "variant_slug", "variant_sku", "strength", "package_size",
            "our_price", "retail_price_per_unit", "scraped_at",
        ]
        placeholders = ", ".join("?" for _ in var_cols)
        conn.executemany(
            f"INSERT INTO medication_sibling_variants ({', '.join(var_cols)}) VALUES ({placeholders})",
            [tuple(r.get(c) for c in var_cols) for r in variant_rows],
        )

        conn.execute(
            """INSERT INTO run_meta
               (run_started_at, run_finished_at, total_urls, success_count, failure_count, source_sitemap)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                run_meta["run_started_at"],
                run_meta["run_finished_at"],
                run_meta["total_urls"],
                run_meta["success_count"],
                run_meta["failure_count"],
                SITEMAP_URL,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    os.replace(DB_TMP_PATH, DB_PATH)


def write_csv(rows: list[dict]) -> None:
    if not rows:
        return
    cols = [c for c in rows[0].keys() if c != "raw_json"]
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c) for c in cols})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    run_started_at = datetime.now(timezone.utc).isoformat()
    log("Starting Cost Plus Drugs medication directory collection run")

    session = requests.Session()

    try:
        urls = fetch_medication_urls(session)
    except Exception as exc:  # noqa: BLE001
        log(f"FATAL: could not fetch/parse sitemap: {exc}")
        return 1

    log(f"Discovered {len(urls)} medication URLs from sitemap")

    rows: list[dict] = []
    variant_rows: list[dict] = []
    failures: list[dict] = []
    scraped_at = datetime.now(timezone.utc).isoformat()

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_page, session, url): url for url in urls}
        done = 0
        for future in concurrent.futures.as_completed(futures):
            url = futures[future]
            done += 1
            try:
                _, product, err = future.result()
            except Exception as exc:  # noqa: BLE001
                product, err = None, f"unhandled exception: {exc}"

            if product is None:
                failures.append({"url": url, "error": err})
            else:
                row = build_medication_row(url, product)
                row["scraped_at"] = scraped_at
                rows.append(row)
                for vr in build_sibling_variant_rows(url, product):
                    vr["scraped_at"] = scraped_at
                    variant_rows.append(vr)

            if done % 200 == 0 or done == len(urls):
                log(f"Progress: {done}/{len(urls)} pages processed "
                    f"({len(rows)} ok, {len(failures)} failed so far)")

    run_finished_at = datetime.now(timezone.utc).isoformat()

    run_meta = {
        "run_started_at": run_started_at,
        "run_finished_at": run_finished_at,
        "total_urls": len(urls),
        "success_count": len(rows),
        "failure_count": len(failures),
    }

    log(f"Writing database with {len(rows)} medications "
        f"and {len(variant_rows)} sibling-variant rows "
        f"({len(failures)} pages failed)")
    write_database(rows, variant_rows, run_meta)
    write_csv(rows)

    with open(LAST_RUN_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                **run_meta,
                "db_path": DB_PATH,
                "csv_path": CSV_PATH,
                "failures": failures[:200],
                "failure_count_total": len(failures),
            },
            f,
            indent=2,
        )

    log(f"Done. {len(rows)}/{len(urls)} medications saved to {DB_PATH}")
    if failures:
        log(f"{len(failures)} pages could not be parsed; see last_run.json for details")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log("FATAL: unhandled exception in main()")
        log(traceback.format_exc())
        sys.exit(1)