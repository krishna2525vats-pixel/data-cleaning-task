# Data Quality Report

*Generated 2026-10-30T14:40:25 from `data/raw/messy_books.csv`*

## Summary

| Metric | Value |
|---|---|
| Rows in | 1100 |
| Duplicates removed | 100 |
| Rows rejected (quarantined) | 59 (5.4%) |
| **Rows out (clean)** | **941** |

## Cleaning steps

| Step | Detail |
|---|---|
| ingest | Loaded 1100 rows x 13 columns from messy_books.csv |
| text | Trimmed/collapsed whitespace in 599 cells; null tokens -> NA |
| currency | Parsed prices; converted 304 non-GBP prices to GBP (static rates) |
| currency | 0 unparseable prices set to null |
| currency | 22 prices were already missing |
| dates | Parsed dates with 5 explicit formats; 17 unparseable -> null; 0 out-of-range -> null |
| rating | Normalised ratings to 1-5; 11 invalid -> null |
| availability | Availability -> in_stock (bool) + stock_quantity; 0 unrecognised -> null |
| numbers | num_reviews -> numeric; 0 invalid -> null |
| categories | category: unified 152 case/spelling variants |
| categories | product_type: unified 0 case/spelling variants |
| text | Repaired 966 descriptions (removed '...more' / duplicated lead-in) |
| dedupe | Recovered 10 missing UPCs via product URL |
| dedupe | Removed 100 duplicate records (same UPC/URL); kept the most complete and most recent version of each |
| nulls | Filled 23 nulls in 'category' with default 'Unknown' |
| nulls | Filled 57 nulls in 'num_reviews' with default 0 |
| nulls | Filled 36 nulls in 'description' with default 'No description available' |
| nulls | Left as null (cannot be imputed honestly): rating=39, in_stock=4, stock_quantity=808, image_url=9, scraped_date=35 |
| validation | Quarantined 59 invalid rows: {'invalid_upc': 29, 'missing_title': 6, 'missing_price': 21, 'price_out_of_range': 5} |
| schema | Schema validation passed for 941 rows x 16 columns |

## Rejections by reason

| Reason | Rows |
|---|---|
| invalid_upc | 29 |
| missing_title | 6 |
| missing_price | 21 |
| price_out_of_range | 5 |

## Null counts before / after

| Column | Before | After |
|---|---|---|
| upc | 42 | 0 |
| title | 7 | 0 |
| category | 25 | 0 |
| price_gbp | 0 | 0 |
| original_price | 0 | 0 |
| original_currency | 0 | 0 |
| rating | 30 | 39 |
| in_stock | 0 | 4 |
| stock_quantity | 0 | 760 |
| num_reviews | 63 | 0 |
| description | 78 | 0 |
| product_type | 0 | 0 |
| product_url | 0 | 0 |
| image_url | 10 | 9 |
| source_page | 0 | 0 |
| scraped_date | 22 | 32 |

## Final schema

| Column | dtype |
|---|---|
| upc | string |
| title | string |
| category | string |
| price_gbp | float64 |
| original_price | float64 |
| original_currency | string |
| rating | Int8 |
| in_stock | boolean |
| stock_quantity | Int64 |
| num_reviews | Int64 |
| description | string |
| product_type | string |
| product_url | string |
| image_url | string |
| source_page | string |
| scraped_date | datetime64[ns] |
