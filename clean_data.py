#!/usr/bin/env python3
"""
Automated Data Cleaning & Schema Transformation
===============================================
Turns a messy tabular dataset into production-ready, schema-validated data
using Pandas, and writes an automated data-quality report.

Pipeline (each step is logged with counts into the quality report)
  1.  Ingest everything as text (nothing is silently coerced by the CSV reader)
  2.  Standardise column names / apply aliases / add missing columns
  3.  Normalise text: trim, collapse whitespace, map null tokens ("N/A", "-") to NA
  4.  Currency: parse symbols/codes + EU/US number formats -> original_price,
      original_currency -> price_gbp (static rates from config)
  5.  Dates: multi-format parsing -> ISO-8601, impossible dates -> null
  6.  Categorical/numeric normalisation: rating words -> 1..5, availability ->
      boolean + quantity, review counts -> integers, consistent category spelling
  7.  Text repair: duplicated / truncated descriptions and "...more" artefacts
  8.  Missing UPCs recovered from product URL, then de-duplication by identity key
      (keeps the most complete, most recent record)
  9.  Null handling policy (fill defaults for optional fields)
 10.  Row-level validation -> invalid rows are quarantined with reasons
 11.  Type enforcement + rigorous schema validation (raises SchemaError)
 12.  Export: cleaned CSV/JSON, rejected records, quality_report.json/.md, log
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

LOG = logging.getLogger("cleaner")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "default_currency": "GBP",
    # Static, illustrative FX rates (1 unit of currency -> GBP). Replace with
    # a dated rate table for real financial use.
    "currency_rates_to_gbp": {"GBP": 1.0, "USD": 0.79, "EUR": 0.86, "INR": 0.0094},
    # Tried in order; dd/mm/yyyy is assumed for slash dates (documented choice).
    "date_formats": ["%Y-%m-%d", "%d/%m/%Y", "%b %d, %Y", "%d-%b-%y", "%Y%m%d"],
    "null_tokens": ["", "n/a", "na", "null", "none", "nan", "-", "--", "?"],
    "price_bounds_gbp": [0.01, 1000.0],
    "upc_regex": "^[0-9a-f]{16}$",
    "min_date": "2020-01-01",
    "fill_defaults": {
        "category": "Unknown",
        "num_reviews": 0,
        "description": "No description available",
    },
    "column_aliases": {
        "price_gbp": "price",
        "scraped_at": "scraped_date",
        "date_scraped": "scraped_date",
        "url": "product_url",
    },
    "max_reject_rate": 0.10,
}

EXPECTED_COLUMNS = [
    "title", "price", "rating", "availability", "product_url", "image_url",
    "source_page", "upc", "category", "product_type", "num_reviews",
    "description", "scraped_date",
]

FINAL_COLUMNS = [
    "upc", "title", "category", "price_gbp", "original_price",
    "original_currency", "rating", "in_stock", "stock_quantity", "num_reviews",
    "description", "product_type", "product_url", "image_url", "source_page",
    "scraped_date",
]

# Target schema. kind: string | float | int | bool | datetime
SCHEMA = {
    "upc":               dict(kind="string",   nullable=False, unique=True),
    "title":             dict(kind="string",   nullable=False),
    "category":          dict(kind="string",   nullable=False),
    "price_gbp":         dict(kind="float",    nullable=False, min=0.01),
    "original_price":    dict(kind="float",    nullable=False, min=0.0),
    "original_currency": dict(kind="string",   nullable=False),
    "rating":            dict(kind="int",      nullable=True,  min=1, max=5),
    "in_stock":          dict(kind="bool",     nullable=True),
    "stock_quantity":    dict(kind="int",      nullable=True,  min=0),
    "num_reviews":       dict(kind="int",      nullable=False, min=0),
    "description":       dict(kind="string",   nullable=False),
    "product_type":      dict(kind="string",   nullable=True),
    "product_url":       dict(kind="string",   nullable=False, unique=True),
    "image_url":         dict(kind="string",   nullable=True),
    "source_page":       dict(kind="string",   nullable=True),
    "scraped_date":      dict(kind="datetime", nullable=True),
}

CURRENCY_SYMBOLS = {"£": "GBP", "$": "USD", "€": "EUR", "₹": "INR"}
CURRENCY_CODE_RE = re.compile(r"\b(GBP|USD|EUR|INR)\b", re.I)
RATING_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


class SchemaError(Exception):
    """Raised when the final DataFrame violates the target schema."""


# --------------------------------------------------------------------------
# Quality log
# --------------------------------------------------------------------------
class QualityLog:
    def __init__(self):
        self.steps: list[dict] = []

    def add(self, step: str, message: str, count: int | None = None):
        entry = {"step": step, "message": message}
        if count is not None:
            entry["count"] = int(count)
        self.steps.append(entry)
        LOG.info("[%s] %s", step, message)


# --------------------------------------------------------------------------
# Pure parsing helpers (unit-tested)
# --------------------------------------------------------------------------
def parse_money(value, default_currency="GBP"):
    """Return (amount, currency) from messy price strings.

    Handles: '£51.77', 'Â£51.77' (mojibake), '51.77 GBP', 'USD 65.54',
    '€60,20' (decimal comma), '₹6,382.45' (thousands comma), '-5.00'.
    Unparseable -> (nan, currency).
    """
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return (np.nan, pd.NA)
    s = str(value).replace("Â", "").strip()
    cur = None
    for sym, code in CURRENCY_SYMBOLS.items():
        if sym in s:
            cur = code
    m = CURRENCY_CODE_RE.search(s)
    if m:
        cur = m.group(1).upper()
    cur = cur or default_currency

    num = re.sub(r"[^\d,.\-]", "", s)
    if not re.search(r"\d", num):
        return (np.nan, cur)
    negative = num.startswith("-")
    num = num.replace("-", "")

    if "," in num and "." in num:                      # last separator = decimal
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        if re.fullmatch(r"\d{1,3}(,\d{3})+", num):     # 1,234 / 1,234,567
            num = num.replace(",", "")
        else:                                          # 60,20 -> 60.20
            num = num.replace(",", ".")
    elif num.count(".") > 1:                           # 1.234.567
        num = num.replace(".", "")
    try:
        amount = float(num)
    except ValueError:
        return (np.nan, cur)
    return (-amount if negative else amount, cur)


def parse_dates(series: pd.Series, formats: list[str]) -> pd.Series:
    """Parse with an ordered list of explicit formats (no guessing)."""
    out = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    for fmt in formats:
        parsed = pd.to_datetime(series, format=fmt, errors="coerce")
        out = out.fillna(parsed.astype("datetime64[ns]"))
    return out


def parse_rating(value):
    """'Three' / '3' / '3 stars' / '3/5' -> 3 ; invalid or out of 1..5 -> nan."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return np.nan
    s = str(value).strip().lower()
    if s in RATING_WORDS:
        return float(RATING_WORDS[s])
    m = re.fullmatch(r"(\d+)(?:\.0+)?\s*(?:/\s*5|stars?)?", s)
    if m and 1 <= int(m.group(1)) <= 5:
        return float(m.group(1))
    m = re.match(r"(one|two|three|four|five)\b", s)
    if m:
        return float(RATING_WORDS[m.group(1)])
    return np.nan


def parse_availability(value):
    """-> (in_stock: True/False/NA, quantity: int/NA)"""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return (pd.NA, pd.NA)
    s = str(value).casefold()
    qty = re.search(r"(\d+)", s)
    qty = int(qty.group(1)) if qty else pd.NA
    if "out of stock" in s or "unavailable" in s or "sold out" in s:
        return (False, 0)
    if "in stock" in s or "available" in s:
        return (True, qty)
    return (pd.NA, pd.NA)


def clean_description(value):
    """Remove a trailing '...more' and a duplicated truncated lead-in."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return value
    t = re.sub(r"\s*\.\.\.\s*more\s*$", "", str(value)).strip()
    if len(t) > 120:
        idx = t.find(t[:60], 1)
        if idx > 0:                       # text starts again -> keep the full copy
            t = t[idx:].strip()
    return t


def canonical_spelling(series: pd.Series) -> pd.Series:
    """Map case/spacing variants to the most frequent spelling in the column."""
    key = series.str.casefold()
    frame = pd.DataFrame({"key": key, "val": series}).dropna()
    if frame.empty:
        return series
    best = frame.groupby("key")["val"].agg(lambda x: x.value_counts().idxmax())
    # A winning spelling that is ALL CAPS / all lowercase is almost certainly
    # mangled (e.g. a one-off category), so title-case it. Mixed case is kept.
    best = best.map(lambda v: v.title() if (v.isupper() or v.islower()) else v)
    return key.map(best).astype("string")


# --------------------------------------------------------------------------
# Pipeline steps
# --------------------------------------------------------------------------
def standardize_columns(df: pd.DataFrame, cfg: dict, q: QualityLog) -> pd.DataFrame:
    df = df.copy()
    df.columns = [re.sub(r"\W+", "_", c.strip().lower()).strip("_") for c in df.columns]
    aliases = {a: t for a, t in cfg["column_aliases"].items()
               if a in df.columns and t not in df.columns}
    if aliases:
        df = df.rename(columns=aliases)
        q.add("columns", f"Applied column aliases: {aliases}")
    missing = [c for c in EXPECTED_COLUMNS if c not in df.columns]
    for c in missing:
        df[c] = pd.NA
    if missing:
        q.add("columns", f"Added missing columns as all-null: {missing}", len(missing))
    extra = [c for c in df.columns if c not in EXPECTED_COLUMNS]
    if extra:
        df = df.drop(columns=extra)
        q.add("columns", f"Dropped unknown columns: {extra}", len(extra))
    return df


def normalize_text(df: pd.DataFrame, cols, null_tokens) -> int:
    tokens = {t.casefold() for t in null_tokens}
    changed_total = 0
    for c in cols:
        s = df[c].astype("string")
        before = s.copy()
        s = s.str.replace(r"\s+", " ", regex=True).str.strip()
        changed_total += int(((before != s) & before.notna()).sum())
        s = s.mask(s.str.casefold().isin(tokens), pd.NA)
        df[c] = s
    return changed_total


def completeness(df: pd.DataFrame) -> pd.Series:
    cols = ["title", "price_gbp", "rating", "in_stock", "category", "num_reviews",
            "description", "image_url", "scraped_date", "upc"]
    return df[cols].notna().sum(axis=1)


def deduplicate(df: pd.DataFrame, q: QualityLog) -> pd.DataFrame:
    url_key = df["product_url"].str.lower().str.rstrip("/")
    # recover missing UPC from another row with the same URL
    lookup = (pd.DataFrame({"k": url_key, "u": df["upc"]}).dropna()
              .drop_duplicates("k").set_index("k")["u"])
    recovered = df["upc"].isna() & url_key.map(lookup).notna()
    df["upc"] = df["upc"].fillna(url_key.map(lookup).astype("string"))
    q.add("dedupe", f"Recovered {int(recovered.sum())} missing UPCs via product URL",
          recovered.sum())

    key = df["upc"].fillna(url_key)
    work = df.assign(_key=key, _c=completeness(df))
    work = work.sort_values(["_c", "scraped_date"], ascending=[False, False],
                            na_position="last", kind="stable")
    dup = work.duplicated(subset="_key", keep="first") & work["_key"].notna()
    removed = work[dup]
    kept = work[~dup].sort_index().drop(columns=["_key", "_c"])
    q.add("dedupe",
          f"Removed {len(removed)} duplicate records (same UPC/URL); kept the most "
          f"complete and most recent version of each", len(removed))
    return kept


def build_rejections(df: pd.DataFrame, cfg: dict) -> pd.Series:
    lo, hi = cfg["price_bounds_gbp"]
    url_ok = df["product_url"].str.match(r"^https?://\S+$").fillna(False).astype(bool)
    upc_ok = df["upc"].str.fullmatch(cfg["upc_regex"]).fillna(False).astype(bool)
    checks = {
        "missing_title": df["title"].isna(),
        "missing_price": df["price_gbp"].isna(),
        "price_out_of_range": df["price_gbp"].notna() & ~df["price_gbp"].between(lo, hi),
        "invalid_upc": ~upc_ok,
        "invalid_product_url": ~url_ok,
        "duplicate_product_url": df["product_url"].notna()
                                 & df["product_url"].str.lower().duplicated(keep="first"),
        "negative_reviews": df["num_reviews"] < 0,
    }
    reasons = {i: [] for i in df.index}
    for name, mask in checks.items():
        mask = mask.fillna(False).astype(bool)
        for i in mask[mask].index:
            reasons[i].append(name)
    return pd.Series({i: ";".join(r) for i, r in reasons.items()}, dtype="object")


def enforce_types(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c, spec in SCHEMA.items():
        kind = spec["kind"]
        if kind == "string":
            df[c] = df[c].astype("string")
        elif kind == "float":
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
        elif kind == "int":
            df[c] = pd.to_numeric(df[c], errors="coerce").round().astype("Int64")
        elif kind == "bool":
            df[c] = df[c].astype("boolean")
        elif kind == "datetime":
            df[c] = pd.to_datetime(df[c], errors="coerce")
    df["rating"] = df["rating"].astype("Int8")
    df["price_gbp"] = df["price_gbp"].round(2)
    df["original_price"] = df["original_price"].round(2)
    return df[FINAL_COLUMNS]


def validate_schema(df: pd.DataFrame) -> None:
    """Strict final gate. Raises SchemaError listing every violation."""
    errors = []
    if list(df.columns) != FINAL_COLUMNS:
        errors.append(f"columns mismatch: {list(df.columns)}")
    kinds = {
        "string": pd.api.types.is_string_dtype,
        "float": pd.api.types.is_float_dtype,
        "int": pd.api.types.is_integer_dtype,
        "bool": pd.api.types.is_bool_dtype,
        "datetime": pd.api.types.is_datetime64_any_dtype,
    }
    for c, spec in SCHEMA.items():
        if c not in df.columns:
            continue
        s = df[c]
        if not kinds[spec["kind"]](s):
            errors.append(f"{c}: expected {spec['kind']}, got {s.dtype}")
            continue
        if not spec["nullable"] and s.isna().any():
            errors.append(f"{c}: {int(s.isna().sum())} nulls in non-nullable column")
        if spec.get("unique") and s.dropna().duplicated().any():
            errors.append(f"{c}: duplicate values in unique column")
        if "min" in spec and (s.dropna() < spec["min"]).any():
            errors.append(f"{c}: values below {spec['min']}")
        if "max" in spec and (s.dropna() > spec["max"]).any():
            errors.append(f"{c}: values above {spec['max']}")
    if errors:
        raise SchemaError("; ".join(errors))


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def run_pipeline(input_path: Path, out_dir: Path, cfg: dict) -> dict:
    q = QualityLog()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. ingest -------------------------------------------------------------
    raw = pd.read_csv(input_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    rows_in = len(raw)
    q.add("ingest", f"Loaded {rows_in} rows x {raw.shape[1]} columns from {input_path.name}", rows_in)

    # 2. columns ------------------------------------------------------------
    df = standardize_columns(raw, cfg, q)
    df["source_row"] = np.arange(len(df)) + 2          # CSV line number (header = 1)

    # 3. text normalisation -------------------------------------------------
    trimmed = normalize_text(df, EXPECTED_COLUMNS, cfg["null_tokens"])
    q.add("text", f"Trimmed/collapsed whitespace in {trimmed} cells; null tokens -> NA", trimmed)
    nulls_before = df[EXPECTED_COLUMNS].isna().sum()

    empty = df[EXPECTED_COLUMNS].isna().all(axis=1)
    if empty.any():
        df = df[~empty]
        q.add("text", f"Dropped {int(empty.sum())} completely empty rows", empty.sum())

    # 4. currency -----------------------------------------------------------
    parsed = df["price"].apply(lambda v: parse_money(v, cfg["default_currency"]))
    money = pd.DataFrame(parsed.tolist(), index=df.index,
                         columns=["original_price", "original_currency"])
    df["original_price"] = money["original_price"].astype("float64")
    df["original_currency"] = money["original_currency"].astype("string")
    bad_price = df["price"].notna() & df["original_price"].isna()
    rates = cfg["currency_rates_to_gbp"]
    df["price_gbp"] = (df["original_price"]
                       * df["original_currency"].map(rates).astype("float64")).round(2)
    foreign = (df["original_currency"].notna()
               & (df["original_currency"] != cfg["default_currency"]))
    q.add("currency", f"Parsed prices; converted {int(foreign.sum())} non-GBP prices to GBP "
                      f"(static rates)", foreign.sum())
    q.add("currency", f"{int(bad_price.sum())} unparseable prices set to null", bad_price.sum())
    q.add("currency", f"{int(df['price'].isna().sum())} prices were already missing",
          df["price"].isna().sum())

    # 5. dates --------------------------------------------------------------
    dates = parse_dates(df["scraped_date"], cfg["date_formats"])
    bad_dates = df["scraped_date"].notna() & dates.isna()
    today = pd.Timestamp.now().normalize() + pd.Timedelta(days=1)
    out_of_range = dates.notna() & ((dates < pd.Timestamp(cfg["min_date"])) | (dates > today))
    dates = dates.mask(out_of_range)
    df["scraped_date"] = dates
    q.add("dates", f"Parsed dates with {len(cfg['date_formats'])} explicit formats; "
                   f"{int(bad_dates.sum())} unparseable -> null; "
                   f"{int(out_of_range.sum())} out-of-range -> null",
          bad_dates.sum() + out_of_range.sum())

    # 6. ratings / availability / numbers / categories -----------------------
    rating = df["rating"].apply(parse_rating)
    bad_rating = df["rating"].notna() & rating.isna()
    df["rating"] = rating
    q.add("rating", f"Normalised ratings to 1-5; {int(bad_rating.sum())} invalid -> null",
          bad_rating.sum())

    avail = df["availability"].apply(parse_availability)
    df["in_stock"] = pd.Series([a[0] for a in avail], index=df.index, dtype="boolean")
    df["stock_quantity"] = pd.Series([a[1] for a in avail], index=df.index, dtype="Int64")
    unknown_av = df["availability"].notna() & df["in_stock"].isna()
    q.add("availability", f"Availability -> in_stock (bool) + stock_quantity; "
                          f"{int(unknown_av.sum())} unrecognised -> null", unknown_av.sum())

    reviews = pd.to_numeric(df["num_reviews"], errors="coerce")
    bad_rev = df["num_reviews"].notna() & reviews.isna()
    df["num_reviews"] = reviews
    q.add("numbers", f"num_reviews -> numeric; {int(bad_rev.sum())} invalid -> null", bad_rev.sum())

    for col in ("category", "product_type"):
        before = df[col].copy()
        df[col] = canonical_spelling(df[col])
        n = int((before.notna() & (before != df[col])).sum())
        q.add("categories", f"{col}: unified {n} case/spelling variants", n)

    # 7. text repair --------------------------------------------------------
    before = df["description"].copy()
    df["description"] = df["description"].apply(clean_description).astype("string")
    n = int((before.notna() & (before != df["description"])).sum())
    q.add("text", f"Repaired {n} descriptions (removed '...more' / duplicated lead-in)", n)
    df["upc"] = df["upc"].str.lower()

    # 8. de-duplication -----------------------------------------------------
    df = deduplicate(df, q)
    duplicates_removed = sum(s.get("count", 0) for s in q.steps
                             if s["step"] == "dedupe" and s["message"].startswith("Removed"))

    # 9. null policy --------------------------------------------------------
    for col, default in cfg["fill_defaults"].items():
        n = int(df[col].isna().sum())
        df[col] = df[col].fillna(default)
        q.add("nulls", f"Filled {n} nulls in '{col}' with default {default!r}", n)
    left = df[["rating", "in_stock", "stock_quantity", "image_url", "scraped_date"]].isna().sum()
    q.add("nulls", "Left as null (cannot be imputed honestly): "
                   + ", ".join(f"{k}={int(v)}" for k, v in left.items()))

    # 10. validation / quarantine ------------------------------------------
    reasons = build_rejections(df, cfg)
    bad = reasons != ""
    rejected = df[bad].assign(reject_reasons=reasons[bad])
    reject_counts = Counter(r for rs in reasons[bad] for r in rs.split(";"))
    df = df[~bad]
    q.add("validation", f"Quarantined {len(rejected)} invalid rows: {dict(reject_counts)}",
          len(rejected))

    # 11. types + schema ----------------------------------------------------
    clean = enforce_types(df).reset_index(drop=True)
    validate_schema(clean)
    q.add("schema", f"Schema validation passed for {len(clean)} rows x {clean.shape[1]} columns",
          len(clean))

    # 12. export --------------------------------------------------------------
    export = clean.copy()
    export["scraped_date"] = export["scraped_date"].dt.strftime("%Y-%m-%d")
    export.to_csv(out_dir / "cleaned_books.csv", index=False, encoding="utf-8-sig")
    export.to_json(out_dir / "cleaned_books.json", orient="records", indent=2,
                   force_ascii=False)
    rej = rejected.drop(columns=["price_gbp", "original_price", "original_currency",
                                 "in_stock", "stock_quantity"], errors="ignore").copy()
    rej["scraped_date"] = pd.to_datetime(rej["scraped_date"]).dt.strftime("%Y-%m-%d")
    rej.to_csv(out_dir / "rejected_records.csv", index=False, encoding="utf-8-sig")

    nulls_after = clean.isna().sum()
    report = {
        "run": {
            "input": str(input_path), "timestamp": datetime.now().isoformat(timespec="seconds"),
            "rows_in": rows_in, "rows_out": len(clean), "rows_rejected": len(rejected),
            "duplicates_removed": duplicates_removed,
            "reject_rate": round(len(rejected) / max(rows_in, 1), 4),
        },
        "steps": q.steps,
        "rejections_by_reason": dict(reject_counts),
        "nulls_before_after": {c: {"before": int(nulls_before.get(c, 0)),
                                   "after": int(nulls_after.get(c, 0))}
                               for c in clean.columns if c in nulls_after},
        "dtypes": {c: str(t) for c, t in clean.dtypes.items()},
        "profile": {
            "price_gbp": {k: round(float(v), 2) for k, v in clean["price_gbp"].describe().items()},
            "categories": int(clean["category"].nunique()),
            "date_range": [str(clean["scraped_date"].min().date()) if clean["scraped_date"].notna().any() else None,
                           str(clean["scraped_date"].max().date()) if clean["scraped_date"].notna().any() else None],
        },
    }
    write_reports(report, out_dir)
    report["_clean_df"] = clean
    return report


def write_reports(report: dict, out_dir: Path) -> None:
    with open(out_dir / "quality_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    r = report["run"]
    md = [
        "# Data Quality Report", "",
        f"*Generated {r['timestamp']} from `{r['input']}`*", "",
        "## Summary", "",
        "| Metric | Value |", "|---|---|",
        f"| Rows in | {r['rows_in']} |",
        f"| Duplicates removed | {r['duplicates_removed']} |",
        f"| Rows rejected (quarantined) | {r['rows_rejected']} ({r['reject_rate']:.1%}) |",
        f"| **Rows out (clean)** | **{r['rows_out']}** |", "",
        "## Cleaning steps", "", "| Step | Detail |", "|---|---|",
    ]
    md += [f"| {s['step']} | {s['message']} |" for s in report["steps"]]
    md += ["", "## Rejections by reason", ""]
    if report["rejections_by_reason"]:
        md += ["| Reason | Rows |", "|---|---|"]
        md += [f"| {k} | {v} |" for k, v in report["rejections_by_reason"].items()]
    else:
        md.append("None.")
    md += ["", "## Null counts before / after", "", "| Column | Before | After |", "|---|---|---|"]
    md += [f"| {c} | {v['before']} | {v['after']} |" for c, v in report["nulls_before_after"].items()]
    md += ["", "## Final schema", "", "| Column | dtype |", "|---|---|"]
    md += [f"| {c} | {t} |" for c, t in report["dtypes"].items()]
    (out_dir / "quality_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")


def load_config(path: Path | None) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path and path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    return cfg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Automated data cleaning & schema validation")
    ap.add_argument("--input", default="data/raw/messy_books.csv")
    ap.add_argument("--out", default="output")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(out / "cleaning.log", mode="w", encoding="utf-8")],
    )
    cfg = load_config(Path(args.config))
    try:
        report = run_pipeline(Path(args.input), out, cfg)
    except SchemaError as exc:
        LOG.error("SCHEMA VALIDATION FAILED: %s", exc)
        return 1
    rate = report["run"]["reject_rate"]
    if rate > cfg["max_reject_rate"]:
        LOG.error("Reject rate %.1f%% exceeds limit %.1f%%", rate * 100, cfg["max_reject_rate"] * 100)
        return 2
    LOG.info("Done: %d clean rows -> %s", report["run"]["rows_out"], out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
