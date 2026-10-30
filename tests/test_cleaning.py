"""Run with:  python -m unittest discover -s tests -v   (no pytest needed)"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import clean_data as c  # noqa: E402


class TestParsers(unittest.TestCase):
    def test_money_formats(self):
        cases = {
            "£51.77": (51.77, "GBP"), "Â£51.77": (51.77, "GBP"),
            "51.77 GBP": (51.77, "GBP"), "USD 65.54": (65.54, "USD"),
            "$1,234.50": (1234.50, "USD"), "€60,20": (60.20, "EUR"),
            "60,20 EUR": (60.20, "EUR"), "₹6,382.45": (6382.45, "INR"),
            "12": (12.0, "GBP"), "-5.00": (-5.0, "GBP"),
            "1.234.567": (1234567.0, "GBP"),
        }
        for raw, (amt, cur) in cases.items():
            a, k = c.parse_money(raw)
            self.assertAlmostEqual(a, amt, places=2, msg=raw)
            self.assertEqual(k, cur, raw)
        self.assertTrue(np.isnan(c.parse_money("free")[0]))
        self.assertTrue(np.isnan(c.parse_money(pd.NA)[0]))

    def test_dates(self):
        s = pd.Series(["2026-10-05", "05/10/2026", "Oct 05, 2026", "05-Oct-26",
                       "20261005", "31/02/2026", "not a date", pd.NA], dtype="string")
        out = c.parse_dates(s, c.DEFAULT_CONFIG["date_formats"])
        self.assertTrue((out.iloc[:5] == pd.Timestamp("2026-10-05")).all())
        self.assertTrue(out.iloc[5:].isna().all())

    def test_rating(self):
        for raw, want in [("3", 3), ("Three", 3), ("three", 3), ("3 stars", 3),
                          ("3/5", 3), ("3.0", 3), ("5", 5)]:
            self.assertEqual(c.parse_rating(raw), want, raw)
        for raw in ["0", "6", "six", "excellent", pd.NA]:
            self.assertTrue(np.isnan(c.parse_rating(raw)), raw)

    def test_availability(self):
        self.assertEqual(c.parse_availability("In stock (22 available)"), (True, 22))
        self.assertEqual(c.parse_availability("IN STOCK"), (True, pd.NA))
        self.assertEqual(c.parse_availability("Out of stock"), (False, 0))
        self.assertTrue(pd.isna(c.parse_availability("???")[0]))

    def test_description_repair(self):
        full = "It is a truth universally acknowledged that a single man in possession of a good fortune must be in want of a wife. " * 2
        messy = full[:80] + " " + full + " ...more"
        fixed = c.clean_description(messy)
        self.assertFalse(fixed.endswith("...more"))
        self.assertTrue(fixed.startswith(full[:60]))
        self.assertLess(len(fixed), len(messy))

    def test_canonical_spelling(self):
        s = pd.Series(["Fiction", "FICTION", "fiction", "Fiction", "POETRY", pd.NA], dtype="string")
        out = c.canonical_spelling(s)
        self.assertEqual(list(out.dropna()), ["Fiction"] * 4 + ["Poetry"])


class TestSchema(unittest.TestCase):
    def _valid_frame(self):
        df = pd.DataFrame([{
            "upc": "a897fe39b1053632", "title": "T", "category": "Poetry",
            "price_gbp": 51.77, "original_price": 51.77, "original_currency": "GBP",
            "rating": 3, "in_stock": True, "stock_quantity": 22, "num_reviews": 0,
            "description": "d", "product_type": "Books",
            "product_url": "https://x.com/a", "image_url": None, "source_page": None,
            "scraped_date": pd.Timestamp("2026-10-05"),
        }])
        return c.enforce_types(df)

    def test_valid_passes(self):
        c.validate_schema(self._valid_frame())

    def test_violations_raise(self):
        df = self._valid_frame()
        df.loc[0, "rating"] = 9
        with self.assertRaises(c.SchemaError):
            c.validate_schema(df)
        df = self._valid_frame()
        df["title"] = pd.array([pd.NA], dtype="string")
        with self.assertRaises(c.SchemaError):
            c.validate_schema(df)
        df = pd.concat([self._valid_frame()] * 2, ignore_index=True)
        with self.assertRaises(c.SchemaError):
            c.validate_schema(df)


class TestEndToEnd(unittest.TestCase):
    """Dirty data generated from the real dataset must be recovered exactly."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = Path(cls.tmp.name)
        subprocess.run([sys.executable, str(ROOT / "generate_messy_data.py"),
                        "--source", str(ROOT / "data/source/books.csv"),
                        "--config", str(ROOT / "config.json"),
                        "--out", str(t / "messy.csv")], check=True, capture_output=True)
        report = c.run_pipeline(t / "messy.csv", t / "out", c.load_config(ROOT / "config.json"))
        cls.report, cls.clean = report, report["_clean_df"]
        cls.src = pd.read_csv(ROOT / "data/source/books.csv", dtype=str,
                              keep_default_na=False, encoding="utf-8-sig").set_index("upc")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_row_accounting(self):
        r = self.report["run"]
        self.assertEqual(r["rows_in"], 1100)
        self.assertEqual(r["duplicates_removed"], 100)
        self.assertEqual(r["rows_out"] + r["rows_rejected"], 1000)

    def test_keys_unique_and_real(self):
        self.assertTrue(self.clean["upc"].is_unique)
        self.assertTrue(self.clean["product_url"].is_unique)
        self.assertTrue(self.clean["upc"].isin(self.src.index).all())

    def test_values_recovered_exactly(self):
        j = self.clean.set_index("upc").join(self.src, rsuffix="_src")
        self.assertTrue((j["title"] == j["title_src"]).all())
        self.assertLessEqual((j["price_gbp"] - j["price_gbp_src"].astype(float)).abs().max(), 0.02)
        r = j[j["rating"].notna()]
        self.assertTrue((r["rating"].astype(int) == r["rating_src"].astype(int)).all())
        k = j[j["category"] != "Unknown"]
        self.assertTrue((k["category"] == k["category_src"]).all())

    def test_no_dirty_artifacts_left(self):
        self.assertFalse(self.clean["description"].str.endswith("...more").any())
        self.assertFalse(self.clean["title"].str.contains(r"^\s|\s$").any())
        self.assertTrue(self.clean["price_gbp"].between(0.01, 1000).all())

    def test_output_files(self):
        out = Path(self.tmp.name) / "out"
        for f in ["cleaned_books.csv", "cleaned_books.json", "rejected_records.csv",
                  "quality_report.json", "quality_report.md"]:
            self.assertTrue((out / f).exists(), f)


if __name__ == "__main__":
    unittest.main(verbosity=2)
