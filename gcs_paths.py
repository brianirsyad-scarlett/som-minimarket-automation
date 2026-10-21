"""Every DRAFT GCS location this repo writes, in one place.

The draft (sales_parquet/raw/minimarket/<chain>/<report>/) holds the portal
files exactly as downloaded. Production is written only by the publish steps,
each of which documents its own paths:
  publish_production.py  Alfamart / Alfamidi -> sales_sell out_minimarket/<brand>/
  publish_indomaret.py   Indomaret           -> sales_sell out_minimarket/indomaret/
  publish_miss_glam.py   Miss Glam           -> sales_sell out_minimarket/miss_glam/ (sell_out/, stock/)
  build_parquets.py      sales_parquet/Minimarket_Sales.parquet, Minimarket_Market_Share.parquet
"""

BUCKET = "bucket_som"
ROOT = "sales_parquet/raw/minimarket"

CHAINS = ("alfamart", "alfamidi", "indomaret", "miss_glam")
REPORTS = ("market_share", "sell_out_branch", "sell_out_store", "stock", "daily_raw")


def prefix(chain: str, report: str) -> str:
    if chain not in CHAINS:
        raise ValueError(f"unknown chain {chain!r}")
    if report not in REPORTS:
        raise ValueError(f"unknown report {report!r}")
    return f"{ROOT}/{chain}/{report}/"
