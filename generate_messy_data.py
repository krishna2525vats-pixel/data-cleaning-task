#!/usr/bin/env python3
"""
Creates a realistically DIRTY dataset from the clean scraped books.csv so the
cleaning pipeline can be demonstrated and verified against known ground truth.

Injected problems (seeded -> reproducible):
  * prices in mixed currencies/formats ('£51.77', '51.77 GBP', '$65.54', '€60,20',
    '₹6,382.45', mojibake 'Â£', 'N/A', negative and absurd values)
  * scrape dates in 5 formats + impossible dates (31/02/2026) + nulls
  * ratings as words / '3 stars' / '3/5' / invalid values
  * inconsistent availability text, casing and whitespace; null tokens
  * missing UPCs, titles, categories, descriptions, review counts
  * exact duplicate rows and near-duplicates (UPC upper-cased, older date)
"""
import argparse
import json
import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

COLUMNS = ["title", "price", "rating", "availability", "product_url", "image_url",
           "source_page", "upc", "category", "product_type", "num_reviews",
           "description", "scraped_date"]
NULLS = ["", "N/A", "null", "-", "NaN", "None"]
WORDS = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five"}


def eu(amount):
    return f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_price(gbp, rng, rates):
    r = rng.random()
    if r < 0.003:
        return "-5.00"
    if r < 0.006:
        return "£99999.00"
    if r < 0.021:
        return rng.choice(NULLS)
    r = rng.random()
    cur = "GBP" if r < 0.70 else "USD" if r < 0.82 else "EUR" if r < 0.94 else "INR"
    amt = round(gbp / rates[cur], 2)
    if cur == "GBP":
        return rng.choice(["£{:.2f}", "{:.2f} GBP", "GBP {:.2f}", "Â£{:.2f}", "{:.2f}", "£ {:.2f}"]).format(amt)
    if cur == "USD":
        return rng.choice(["${:,.2f}", "{:,.2f} USD", "USD {:,.2f}"]).format(amt)
    if cur == "EUR":
        return rng.choice(["€" + eu(amt), eu(amt) + " EUR", eu(amt) + " €"])
    return rng.choice(["₹{:,.2f}", "{:,.2f} INR", "INR {:,.2f}"]).format(amt)


def fmt_date(d, rng):
    r = rng.random()
    if r < 0.02:
        return rng.choice(["not a date", "31/02/2026", "2026-13-45", "TBD"])
    if r < 0.04:
        return rng.choice(NULLS)
    f = rng.choices(["%Y-%m-%d", "%d/%m/%Y", "%b %d, %Y", "%d-%b-%y", "%Y%m%d"],
                    weights=[40, 25, 15, 10, 10])[0]
    return d.strftime(f)


def fmt_rating(n, rng):
    r = rng.random()
    if r < 0.02:
        return rng.choice(NULLS)
    if r < 0.03:
        return rng.choice(["0", "6", "six", "excellent"])
    return rng.choice([str(n), str(n), f"{n}.0", WORDS[n], WORDS[n].lower(),
                       f"{n} stars", f"{n}/5"])


def fmt_avail(rng):
    r = rng.random()
    if r < 0.015:
        return "Out of stock"
    if r < 0.025:
        return rng.choice(NULLS)
    return rng.choice(["In stock", "in stock", "IN STOCK", "In stock ",
                       f"In stock ({rng.randint(1, 30)} available)", "Available"])


def messy_text(v, rng, p_null=0.0, p_pad=0.0, p_case=0.0):
    if rng.random() < p_null:
        return rng.choice(NULLS)
    if rng.random() < p_pad:
        v = rng.choice(["  ", " ", "   "]) + v + rng.choice(["  ", " ", "\t"])
    if rng.random() < p_case:
        v = rng.choice([v.upper(), v.lower()])
    return v


def make_row(r, rng, rates, scraped, variant=False):
    num = r["num_reviews"]
    return {
        "title": messy_text(r["title"], rng, p_null=0 if variant else 0.005, p_pad=0.10),
        "price": fmt_price(float(r["price_gbp"]), rng, rates),
        "rating": fmt_rating(int(r["rating"]), rng),
        "availability": fmt_avail(rng),
        "product_url": r["product_url"],
        "image_url": messy_text(r["image_url"], rng, p_null=0.01),
        "source_page": r["source_page"],
        "upc": (r["upc"].upper() if variant else
                messy_text(r["upc"], rng, p_null=0.03, p_pad=0.03)),
        "category": messy_text(r["category"], rng, p_null=0.03, p_pad=0.08, p_case=0.16),
        "product_type": messy_text(r["product_type"], rng, p_pad=0.05),
        "num_reviews": rng.choice(NULLS) if rng.random() < 0.05 else rng.choice([num, f"{num}.0"]),
        "description": rng.choice(NULLS) if (variant or rng.random() < 0.04) else r["description"],
        "scraped_date": fmt_date(scraped, rng),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="data/source/books.csv")
    ap.add_argument("--out", default="data/raw/messy_books.csv")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--exact-dups", type=int, default=60)
    ap.add_argument("--near-dups", type=int, default=40)
    a = ap.parse_args()

    rng = random.Random(a.seed)
    rates = json.loads(Path(a.config).read_text())["currency_rates_to_gbp"]
    src = pd.read_csv(a.source, dtype=str, keep_default_na=False, encoding="utf-8-sig")

    start = date(2026, 10, 1)
    rows = []
    for _, r in src.iterrows():
        rows.append(make_row(r, rng, rates, start + timedelta(days=rng.randint(0, 29))))
    exact = [dict(x) for x in rng.sample(rows, a.exact_dups)]
    near = []
    near_idx = rng.sample(range(len(src)), a.near_dups)
    for i in near_idx:
        near.append(make_row(src.iloc[i], rng, rates,
                             start - timedelta(days=rng.randint(3, 10)), variant=True))
    # blank the UPC on 10 originals that have a near-duplicate: recoverable via URL
    for i in near_idx[:10]:
        rows[i]["upc"] = rng.choice(NULLS)
    allrows = rows + exact + near
    rng.shuffle(allrows)

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(allrows, columns=COLUMNS).to_csv(out, index=False, encoding="utf-8")
    print(f"Wrote {len(allrows)} messy rows -> {out} "
          f"({len(rows)} base + {len(exact)} exact dups + {len(near)} near dups)")


if __name__ == "__main__":
    main()
