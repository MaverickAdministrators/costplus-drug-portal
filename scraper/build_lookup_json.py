#!/usr/bin/env python3
"""
Build the site-facing drug price lookup file.

Reads scraper/output/costplusdrugs.db (produced by costplus_scraper.py) and
writes data/drug-prices.json -- a trimmed, compact JSON file the portal pages
fetch client-side. Only the columns the site needs are included; raw_json and
other audit-only columns stay in the database/CSV artifacts.

Usage
-----
    python3 build_lookup_json.py
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

SCRAPER_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRAPER_DIR)

DB_PATH = os.path.join(SCRAPER_DIR, "output", "costplusdrugs.db")
OUT_PATH = os.path.join(REPO_ROOT, "data", "drug-prices.json")

FIELDS = [
    "slug",
    "name",
    "brand_name",
    "brand_generic",
    "form",
    "strength",
    "package_size",
    "our_price",
    "retail_price_other_pharmacies",
    "default_quantity",
    "savings_amount",
    "savings_percent",
    "is_available",
    "url",
]


def main() -> int:
    if not os.path.exists(DB_PATH):
        print(f"ERROR: database not found at {DB_PATH}; run costplus_scraper.py first", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT {', '.join(FIELDS)}, scraped_at FROM medications "
            "ORDER BY name COLLATE NOCASE, slug"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("ERROR: medications table is empty; refusing to write an empty lookup file", file=sys.stderr)
        return 1

    medications = []
    for row in rows:
        med = {field: row[field] for field in FIELDS}
        med["is_available"] = bool(med["is_available"])
        medications.append(med)

    payload = {
        "source": "costplusdrugs.com",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scraped_at": rows[0]["scraped_at"],
        "count": len(medications),
        "medications": medications,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")

    size_kb = os.path.getsize(OUT_PATH) / 1024
    print(f"Wrote {len(medications)} medications to {OUT_PATH} ({size_kb:.0f} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
