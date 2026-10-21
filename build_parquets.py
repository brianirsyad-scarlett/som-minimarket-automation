"""Rebuild the two combined minimarket parquets from GCS - no laptop, no Excel.

    python build_parquets.py --sales            # after any sell-out publish
    python build_parquets.py --market-share     # after the market-share download
    python build_parquets.py --sales --market-share --dry-run

sales -> gs://bucket_som/sales_parquet/Minimarket_Sales.parquet
    Every csv under PRODUCTION sales_sell out_minimarket/<chain>/sell_out/,
    through the laptop converter's own process_files()
    (sources/minimarket_sell_out_converter.py, copied verbatim), then exactly
    its main(): concat, drop rows missing Date/Branch/Category, cast.

market share -> gs://bucket_som/sales_parquet/Minimarket_Market_Share.parquet
    The months in the draft (prev + current, refreshed nightly) are rebuilt
    from the raw portal files with the per-chain converters' logic; every other
    month is kept from the published parquet (the history has no raw files).
    Fixes over the laptop chain, all found on 2026-09-24:
      - Alfa "WOMEN PARFUME EDT & EXTRAIT" -> "WOMEN PARFUME & EDT", the name
        every month since 2025-01 uses.
      - Indomaret dates are real dates. The laptop converter wrote them as text
        dd/mm/yyyy, which the combiner read month-first (01/08 -> 8 January).
      - Refuses to publish duplicate (Account, Date, Category, Brand) rows.

Each publish refuses to replace the parquet with one that lost a month or an
account, so a broken download can never wipe history.
"""

import argparse
import io
import os
import re
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

import openpyxl
import pandas as pd
from google.cloud import storage

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "sources"))
import gcs_paths  # noqa: E402
import minimarket_sell_out_converter as sell_out_conv  # noqa: E402

SALES_DEST = "sales_parquet/Minimarket_Sales.parquet"
MS_DEST = "sales_parquet/Minimarket_Market_Share.parquet"
PROD_ROOT = "sales_sell out_minimarket"
ACCOUNTS = {"indomaret": "INDOMARET", "alfamart": "ALFAMART", "alfamidi": "ALFAMIDI"}
MS_COLUMNS = ["Date", "Category", "Brand", "Market Share", "PLU", "Account"]
CATEGORY_FIX = {"WOMEN PARFUME EDT & EXTRAIT": "WOMEN PARFUME & EDT"}
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def month_key(d) -> str:
    return pd.Timestamp(d).strftime("%Y-%m")


def guard(old: pd.DataFrame, new: pd.DataFrame, what: str) -> None:
    """New must still hold every (Account, month) the published file had."""
    def keys(df):
        return set(zip(df["Account"], pd.to_datetime(df["Date"]).dt.strftime("%Y-%m")))
    lost = sorted(keys(old) - keys(new))
    if lost:
        raise SystemExit(f"REFUSING to publish {what}: it would lose {len(lost)} account-month(s), e.g. {lost[:6]}")


def publish(bucket, df: pd.DataFrame, dest: str, dry: bool) -> None:
    if dry:
        print(f"  [dry-run] would write gs://{bucket.name}/{dest}  ({len(df):,} rows)")
        return
    buf = io.BytesIO()
    df.to_parquet(buf, index=False, engine="pyarrow")
    bucket.blob(dest).upload_from_string(buf.getvalue(), content_type="application/octet-stream")
    print(f"  published gs://{bucket.name}/{dest}  ({len(df):,} rows)")


def describe(df: pd.DataFrame) -> str:
    d = pd.to_datetime(df["Date"])
    return ", ".join(f"{a} {n:,} rows {lo:%Y-%m-%d}..{hi:%Y-%m-%d}"
                     for a, (n, lo, hi) in df.assign(_d=d).groupby("Account")["_d"]
                     .agg(["size", "min", "max"]).iterrows())


# ---------------------------------------------------------------- sales -----

def miss_glam_rows(bucket):
    """Miss Glam BRANCHES from production miss_glam/sell_out/ (monthly files written by publish_miss_glam.py).

    Only Store Type == STORE (the user's choice, 2026-10-21): ONLINE, DISTRIBUTOR, WAREHOUSE, EVENT, OTHER and
    DELETED stay in the monthly files but are not in Minimarket_Sales. No Category (the portal has none; the user
    chose to skip it). The parquet keeps exactly the columns the other chains have."""
    frames = []
    for b in bucket.list_blobs(prefix=f"{PROD_ROOT}/miss_glam/sell_out/"):
        if b.name.lower().endswith(".csv"):
            frames.append(pd.read_csv(io.BytesIO(b.download_as_bytes()), dtype=str, encoding="utf-8-sig"))
    if not frames:
        print("  miss_glam: no production files yet - not included")
        return None
    d = pd.concat(frames, ignore_index=True)
    skipped = d[d["Store Type"] != "STORE"]
    d = d[d["Store Type"] == "STORE"]
    print(f"  miss_glam: leaving out {len(skipped):,} non-branch row(s) "
          f"({', '.join(f'{k} {v:,}' for k, v in skipped['Store Type'].value_counts().items()) or 'none'})")
    out = pd.DataFrame({
        "Date": pd.to_datetime(d["Date"]).dt.date,
        "Branch": d["Branch"],
        "Brand": d["Brand"],
        "Category": None,
        "Product": d["Product"],
        "Value IDR": d["Value IDR"].astype(float),
        "Value Qty": d["Value Qty"].astype(float),
        "Account": "MISS GLAM",
    })
    dupes = out.duplicated(["Date", "Branch", "Product"]).sum()
    print(f"  miss_glam: {len(frames)} production file(s), {len(out):,} rows"
          + (f", {dupes} repeated (Date, Branch, Product) - kept (different SKUs can share a name)" if dupes else ""))
    return out


def build_sales(bucket, dry: bool) -> None:
    print("--- Minimarket_Sales.parquet (from production sell_out/) ---")
    parts = []
    with tempfile.TemporaryDirectory() as tmp:
        for chain, account in ACCOUNTS.items():
            folder = Path(tmp) / chain
            folder.mkdir()
            n = 0
            for b in bucket.list_blobs(prefix=f"{PROD_ROOT}/{chain}/sell_out/"):
                name = b.name.rsplit("/", 1)[-1]
                if name.lower().endswith(".csv"):
                    b.download_to_filename(str(folder / name))
                    n += 1
            print(f"  {chain}: {n} production file(s)")
            if n == 0:
                raise SystemExit(f"REFUSING: no {chain} production sell_out files found")
            parts.append(sell_out_conv.process_files(str(folder), account))
    # --- identical to minimarket_sell_out_converter.main() ---
    combined = pd.concat(parts, ignore_index=True)
    combined = combined.dropna(subset=["Date", "Branch", "Category"])
    combined["Date"] = pd.to_datetime(combined["Date"]).dt.date
    combined["Value Qty"] = combined["Value Qty"].astype(float)
    combined["Value IDR"] = combined["Value IDR"].astype(float)
    miss_glam = miss_glam_rows(bucket)
    if miss_glam is not None:
        combined = pd.concat([combined, miss_glam], ignore_index=True)
    print(f"  built: {describe(combined)}")
    old = bucket.get_blob(SALES_DEST)
    if old is not None:
        guard(pd.read_parquet(io.BytesIO(old.download_as_bytes())), combined, "Minimarket_Sales")
    publish(bucket, combined, SALES_DEST, dry)


# --------------------------------------------------------- market share -----

def find_header_row(df_raw):
    for i, val in enumerate(df_raw.iloc[:, 0]):
        if pd.notna(val) and str(val).strip().upper() == "NO":
            return i
    return None


def parse_alfa(content: bytes, filename: str):
    """Alfamart/Alfamidi raw_converter.py, one file. Date/Category come from the
    filename; Brand = column B, PLU = D, Market Share = H under the 'NO' row."""
    m = re.search(r"_Actual_([A-Za-z]{3})-(\d{4})_(?:MTD_)?(.+?)_BRANCH", filename, re.IGNORECASE)
    if not m or m.group(1).title() not in MONTHS:
        print(f"    skipped {filename}: name does not carry month/category")
        return None
    month = date(int(m.group(2)), MONTHS[m.group(1).title()], 1)
    category = re.sub(r"^MTD_", "", m.group(3), flags=re.IGNORECASE).strip("_")
    df_raw = pd.read_excel(io.BytesIO(content), sheet_name=0, header=None)
    hdr = find_header_row(df_raw)
    if hdr is None:
        print(f"    skipped {filename}: no 'NO' header row")
        return None
    df = pd.read_excel(io.BytesIO(content), sheet_name=0, header=hdr)
    if df.shape[1] <= 7:
        print(f"    skipped {filename}: lacks required columns (no data this month)")
        return None
    out = pd.DataFrame({"Date": month, "Category": category, "Brand": df.iloc[:, 1],
                        "Market Share": df.iloc[:, 7], "PLU": df.iloc[:, 3]})
    out = out.dropna(subset=["Brand"])
    return out[out["Brand"].astype(str).str.strip() != ""]


def parse_indomaret(content: bytes, filename: str):
    """Indomaret 1_market_share_converter.py, one file: month and category from
    cell A1, table from row 3 (Brand = A, PLU = C, Market Share = D)."""
    a1 = str(openpyxl.load_workbook(io.BytesIO(content), data_only=True).active["A1"].value or "")
    mm, cm = re.search(r"month_id is (.+)", a1), re.search(r"cat_nm is (.+)", a1)
    if not mm:
        print(f"    skipped {filename}: A1 has no month_id")
        return None
    month = datetime.strptime(mm.group(1).strip(), "%d %B %Y").date().replace(day=1)
    df = pd.read_excel(io.BytesIO(content), skiprows=2).dropna(how="all")
    return pd.DataFrame({"Date": month, "Category": cm.group(1).strip() if cm else None,
                         "Brand": df.iloc[:, 0], "Market Share": df.iloc[:, 3], "PLU": df.iloc[:, 2]})


def excel_round_trip(df: pd.DataFrame) -> pd.DataFrame:
    """The laptop chain writes each month to .xlsx and the combiner reads it back;
    do the same so values/types come out identical (e.g. whole numbers)."""
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    buf.seek(0)
    return pd.read_excel(buf, sheet_name=0, header=0)


def to_fraction(df: pd.DataFrame, chain: str) -> pd.DataFrame:
    """History stores market share as a fraction (0.269, a category sums to ~1).
    Since about 2026-08 the Alfamart/Alfamidi portals send percent (26.9, sums to
    ~100), which made those months x100 and Indomaret look tiny beside them.
    Scale any month+category that sums past 5 back to a fraction."""
    ms = pd.to_numeric(df["Market Share"], errors="coerce")
    tot = ms.groupby([df["Date"], df["Category"]]).transform("sum")
    pct = tot > 5
    if pct.any():
        print(f"  {chain}: {int(pct.sum())} rows arrived as percent - divided by 100")
    return df.assign(**{"Market Share": (ms.where(~pct, ms / 100)).round(6)})


def build_market_share(bucket, dry: bool) -> None:
    print("--- Minimarket_Market_Share.parquet (history kept, draft months rebuilt) ---")
    fresh = []
    for chain, account in ACCOUNTS.items():
        rows = []
        for b in bucket.list_blobs(prefix=gcs_paths.prefix(chain, "market_share")):
            name = b.name.rsplit("/", 1)[-1]
            if not name.lower().endswith(".xlsx"):
                continue
            if chain == "indomaret":
                if "data" not in name.lower():
                    continue
                part = parse_indomaret(b.download_as_bytes(), name)
            else:
                if "Market Share by Category by Month by Branch_" not in name:
                    continue
                part = parse_alfa(b.download_as_bytes(), name)
            if part is not None and len(part):
                rows.append(part)
        if not rows:
            print(f"  {chain}: no raw files in the draft - keeping its published months")
            continue
        df = excel_round_trip(pd.concat(rows, ignore_index=True))
        df = to_fraction(df, chain)
        df["Account"] = account
        fresh.append(df)
        print(f"  {chain}: {len(df):,} rows rebuilt for {sorted(df['Date'].map(month_key).unique())}")

    blob = bucket.get_blob(MS_DEST)
    old = pd.read_parquet(io.BytesIO(blob.download_as_bytes())) if blob else pd.DataFrame(columns=MS_COLUMNS)
    if not fresh:
        print("  nothing rebuilt - published file left as it is")
        return
    new = pd.concat(fresh, ignore_index=True)
    # --- minimarket_market_share_converter.main() transforms ---
    new["Brand"] = new["Brand"].astype(str).str.strip().str.upper()
    new["Category"] = new["Category"].astype(str).str.strip().str.upper().replace(CATEGORY_FIX)
    new["Account"] = new["Account"].astype(str).str.upper()
    new["Market Share"] = pd.to_numeric(new["Market Share"], errors="coerce")
    new["PLU"] = pd.to_numeric(new["PLU"], errors="coerce").astype(float)
    new["Date"] = pd.to_datetime(new["Date"], errors="coerce").dt.date
    new = new[MS_COLUMNS]

    replaced = set(zip(new["Account"], new["Date"].map(month_key)))
    keep = ~pd.Series(list(zip(old["Account"], pd.to_datetime(old["Date"]).dt.strftime("%Y-%m"))),
                      index=old.index).isin(replaced)
    combined = pd.concat([old[keep][MS_COLUMNS], new], ignore_index=True)
    combined = combined.sort_values(["Account", "Date", "Category", "Brand"], kind="stable").reset_index(drop=True)

    dupes = combined.duplicated(["Account", "Date", "Category", "Brand"]).sum()
    if dupes:
        raise SystemExit(f"REFUSING to publish market share: {dupes} duplicate (Account, Date, Category, Brand) rows")
    if combined["Date"].isna().any():
        raise SystemExit("REFUSING to publish market share: rows without a Date")
    guard(old, combined, "Minimarket_Market_Share")
    print(f"  replaced {len(replaced)} account-month(s), kept {int(keep.sum()):,} history rows")
    print(f"  built: {describe(combined)}")
    publish(bucket, combined, MS_DEST, dry)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sales", action="store_true")
    ap.add_argument("--market-share", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not (a.sales or a.market_share):
        ap.error("pick --sales and/or --market-share")
    bucket = storage.Client().bucket(gcs_paths.BUCKET)
    if a.sales:
        build_sales(bucket, a.dry_run)
    if a.market_share:
        build_market_share(bucket, a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
