"""Merge the freshly downloaded Miss Glam day files into GCS and rebuild the monthly sell-out files.

    python publish_miss_glam.py --daily out/miss_glam                 # draft only
    python publish_miss_glam.py --daily out/miss_glam --production    # + production copy

For every month the download touched:
  1. sales_parquet/raw/minimarket/miss_glam/daily_raw/YYYYMM.zip  - the portal's own day files, one per
     store x day. Downloaded days REPLACE the same names in the zip, so a later edit at the portal overwrites
     the old value; days not in this run are untouched.
  2. the month is rebuilt in full from that zip (sold rows only, Value IDR = Grand Total) and written to
     the draft  .../miss_glam/sell_out_store/YYYYMM_Sell Out Miss Glam.csv
     and, with --production, to  sales_sell out_minimarket/miss_glam/sell_out/ (what build_parquets reads).
Stock: the newest day of each store holds today's stock (the portal column is "stock now"), saved as
  .../miss_glam/stock/<snapshot date>_Miss Glam Stock.csv  and, with --production, appended to the month file
  sales_sell out_minimarket/miss_glam/stock/YYYYMM_Stock Miss Glam.csv (re-running the same day replaces it).
A month with no existing zip is only accepted for the current month - a missing history zip is an error
(otherwise a partial month would overwrite a complete one).
"""
import argparse
import io
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from google.cloud import storage

sys.path.insert(0, str(Path(__file__).parent / "sources"))
import gcs_paths  # noqa: E402
import miss_glam_parse as mg  # noqa: E402

PROD_ROOT = "sales_sell out_minimarket/miss_glam"


def csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")


def put(bucket, name: str, data: bytes, ctype: str, dry: bool) -> None:
    if dry:
        print(f"  [dry-run] would write gs://{bucket.name}/{name}  ({len(data):,} bytes)")
        return
    bucket.blob(name).upload_from_string(data, content_type=ctype)
    print(f"  wrote gs://{bucket.name}/{name}  ({len(data):,} bytes)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily", type=Path, required=True)
    ap.add_argument("--production", action="store_true", help="also write sales_sell out_minimarket/miss_glam/")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    files = sorted(a.daily.glob("*/*.csv"))
    if not files:
        sys.exit("No downloaded files - refusing to publish")
    bucket = storage.Client().bucket(gcs_paths.BUCKET)
    root = f"{gcs_paths.ROOT}/miss_glam"
    today = datetime.now(ZoneInfo("Asia/Jakarta")).date()
    this_month = today.strftime("%Y%m")

    by_month: dict = {}
    for f in files:
        by_month.setdefault(f.stem[:7].replace("-", ""), []).append(f)

    for ym, fs in sorted(by_month.items()):
        print(f"--- {ym}: {len(fs)} downloaded day file(s) ---")
        zblob = bucket.blob(f"{root}/daily_raw/{ym}.zip")
        if zblob.exists():
            entries = mg.read_zip(zblob.download_as_bytes())
        elif ym == this_month:
            entries = {}
        else:
            sys.exit(f"REFUSING: daily_raw/{ym}.zip is missing for a past month")
        before = set(entries)
        changed = 0
        for f in fs:
            key = f"{f.parent.name}/{f.name}"
            data = f.read_bytes()
            if entries.get(key) != data:
                changed += 1
            entries[key] = data
        assert before <= set(entries)
        print(f"  zip: {len(before)} -> {len(entries)} entries, {changed} new or changed")
        put(bucket, f"{root}/daily_raw/{ym}.zip", mg.write_zip(entries), "application/zip", a.dry_run)

        frame = mg.month_frame(entries)
        name = f"{ym}_Sell Out Miss Glam.csv"
        old = bucket.get_blob(f"{root}/sell_out_store/{name}")
        if old is not None:
            old_dates = set(pd.read_csv(io.BytesIO(old.download_as_bytes()), usecols=["Date"])["Date"])
            lost = old_dates - set(frame["Date"])
            if lost:
                sys.exit(f"REFUSING: {ym} would lose {len(lost)} day(s) of sales, e.g. {sorted(lost)[:3]}")
        print(f"  month: {len(frame):,} rows, {frame['Date'].nunique()} days, "
              f"{frame['Value IDR'].sum():,.0f} IDR, {frame['Store ID'].nunique()} stores")
        data = csv_bytes(frame)
        put(bucket, f"{root}/sell_out_store/{name}", data, "text/csv", a.dry_run)
        if a.production:
            put(bucket, f"{PROD_ROOT}/sell_out/{name}", data, "text/csv", a.dry_run)

    # ---- stock snapshot: newest downloaded day of every store ----
    newest = max(f.stem for f in files)
    snap = today.isoformat()
    latest = [f for f in files if f.stem == newest]
    stock = pd.concat([mg.stock_rows(f.read_bytes(), f.parent.name, snap) for f in latest], ignore_index=True)
    print(f"--- stock snapshot {snap} (from {newest} files): {len(stock):,} rows, "
          f"{stock['Store ID'].nunique()} stores, {stock['Stok'].sum():,.0f} units ---")
    put(bucket, f"{root}/stock/{snap}_Miss Glam Stock.csv", csv_bytes(stock), "text/csv", a.dry_run)
    if a.production:
        mname = f"{this_month}_Stock Miss Glam.csv"
        blob = bucket.get_blob(f"{PROD_ROOT}/stock/{mname}")
        if blob is not None:
            prev = pd.read_csv(io.BytesIO(blob.download_as_bytes()), dtype={"Store ID": str, "SKU": str, "Barcode": str})
            stock = pd.concat([prev[prev["Snapshot Date"] != snap], stock], ignore_index=True)
        stock = stock.sort_values(["Snapshot Date", "Store ID", "SKU"], kind="stable")
        put(bucket, f"{PROD_ROOT}/stock/{mname}", csv_bytes(stock), "text/csv", a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
