# Automated Data Cleaning & Schema Transformation

Pandas pipeline that turns a messy tabular dataset into production-ready,
schema-validated data and writes an automated data-quality report.

**Data:** the 1,000-book dataset scraped in Task 1 (`data/source/books.csv`).
`generate_messy_data.py` corrupts it in realistic, reproducible ways (seed 42) to
produce `data/raw/messy_books.csv` (1,100 rows), so the cleaner can be verified
against known ground truth.

## Quick start
```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python generate_messy_data.py        # (optional) rebuild the messy input
python clean_data.py                 # clean data/raw/messy_books.csv -> output/
python -m unittest discover -s tests -v
```
Clean your own file: `python clean_data.py --input my.csv --out my_output`
(exit code 0 = ok, 1 = schema violation, 2 = reject rate above `max_reject_rate`).

## What gets fixed

| Problem injected | How the pipeline handles it |
|---|---|
| Prices as `£51.77`, `51.77 GBP`, `$65.54`, `€60,20`, `₹6,382.45`, `Â£51.77`, `N/A` | `parse_money()` detects symbol/code and US/EU number formats -> `original_price`, `original_currency`, converted `price_gbp` |
| 5 date formats, impossible dates (`31/02/2026`), junk | explicit ordered formats (no guessing); invalid -> null; out-of-range -> null |
| Ratings `Three`, `3 stars`, `3/5`, `0`, `six` | normalised to Int 1-5; invalid -> null |
| Availability `in stock`, `In stock (22 available)`, `Out of stock` | `in_stock` (boolean) + `stock_quantity` |
| Whitespace, null tokens (`N/A`, `-`, `null`, ...) | trimmed, collapsed, mapped to real nulls |
| Category/case variants (`FICTION`, ` fiction `) | unified to the most frequent spelling |
| `...more` + duplicated text in descriptions | repaired |
| 60 exact + 40 near duplicates, some with missing UPC | UPC recovered from product URL; keeps the most complete, most recent record |
| Missing / invalid required fields | quarantined in `rejected_records.csv` with reasons |

### Null-handling policy
* **Filled with a documented default:** `category` -> `Unknown`, `num_reviews` -> `0`, `description` -> `No description available`
* **Left null (cannot be imputed honestly):** `rating`, `in_stock`, `stock_quantity`, `image_url`, `scraped_date`
* **Row rejected:** missing/invalid `title`, `price_gbp`, `upc`, `product_url`, or price outside £0.01-£1000

### Final schema (enforced, raises `SchemaError` on any violation)
`upc` string unique · `title` string · `category` string · `price_gbp` float >= 0.01 ·
`original_price` float · `original_currency` string · `rating` Int8 1-5 (nullable) ·
`in_stock` boolean (nullable) · `stock_quantity` Int64 (nullable) · `num_reviews` Int64 >= 0 ·
`description` string · `product_type` string · `product_url` string unique ·
`image_url` string (nullable) · `source_page` string · `scraped_date` datetime (nullable)

## Outputs (`output/`)
| File | Content |
|---|---|
| `cleaned_books.csv` / `.json` | final dataset |
| `rejected_records.csv` | quarantined rows + `reject_reasons` + original `source_row` |
| `quality_report.md` / `.json` | step-by-step log, rejections, null counts before/after, dtypes |
| `cleaning.log` | run log |

## Verified results (messy input -> clean output)
* 1,100 rows in -> 100 duplicates removed -> 59 quarantined -> **941 clean rows** (941 + 59 = the original 1,000)
* Against the original data: titles 100% identical, prices within £0.02 (FX rounding), ratings 100%, categories 100%
* All 13 unit/integration tests pass

## Assumptions & limitations
* FX rates in `config.json` are **static and illustrative** - use a dated rate table for real finance work.
* Slash dates are read as **dd/mm/yyyy**.
* A single-row category whose only spelling is ALL CAPS is title-cased (heuristic).
* A UPC can only be recovered when another record with the same URL carries it.
* Developed and tested with pandas 3.0 / Python 3.12; written to work with pandas >= 2.0.

## Project layout
```
clean_data.py            pipeline, parsers, schema, reporting
generate_messy_data.py   builds the dirty dataset from the clean one
config.json              rates, formats, bounds, defaults, thresholds
tests/test_cleaning.py   parser, schema and end-to-end tests
data/source|raw          clean source / messy input
output/                  generated results
```
