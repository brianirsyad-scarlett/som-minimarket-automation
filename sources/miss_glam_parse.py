"""Parse the Miss Glam M2 portal CSV ('Laporan Penjualan per Kriteria Produk', one store, one day).

The portal lists EVERY product for the store-day: about 90% are 'Tidak Terjual' (quantity 0). Sell out keeps only
the 'Terjual' rows. The 'Stok' column is the stock at the moment of export, NOT the stock on that date (checked
2026-10-21: identical on every historical date), so it is only meaningful when the file is downloaded for the
current day - the stock snapshot is taken from the newest day of each run.

Value IDR = 'Grand Total' (after discount).
"""
import io
import re
import zipfile

import pandas as pd

OUT_COLUMNS = ["Date", "Store ID", "Branch", "Store Type", "SKU", "Barcode", "Product", "Brand",
               "Value Qty", "Value IDR", "Gross IDR", "Discount IDR", "HPP IDR"]
STOCK_COLUMNS = ["Snapshot Date", "Store ID", "Branch", "Store Type", "SKU", "Barcode", "Product", "Stok"]


def store_type(name: str) -> str:
    n = name.upper()
    if "DELETED" in n:
        return "DELETED"
    if "ONLINE" in n:
        return "ONLINE"
    if "WAREHOUSE" in n:
        return "WAREHOUSE"
    if "DISTRIVERSA" in n or re.search(r"\bDC\b", n):
        return "DISTRIBUTOR"
    if "EVENT" in n:
        return "EVENT"
    if n.startswith("MISS GLAM") or n.startswith("MISSGLAM"):
        return "STORE"
    return "OTHER"  # e.g. Radysa, Maddie Beauty


def read_portal_csv(content: bytes) -> pd.DataFrame:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return pd.read_csv(io.BytesIO(content), sep=";", dtype=str, encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("portal CSV is neither UTF-8 nor cp1252")


def branch_names(df: pd.DataFrame, store_id: str) -> pd.Series:
    """Cabang per row; blanks take the file's own (most common) branch name, else 'Store <id>'."""
    b = df["Cabang"].str.strip()
    known = b.dropna()
    return b.fillna(known.mode().iloc[0] if not known.empty else f"Store {store_id}")


def parse_store_day(content: bytes, store_id: str, day: str) -> pd.DataFrame:
    """The sold rows of one store-day in OUT_COLUMNS (empty frame when nothing sold)."""
    df = read_portal_csv(content)
    df = df[df["Status"].str.strip() == "Terjual"]
    if df.empty:
        return pd.DataFrame(columns=OUT_COLUMNS)
    num = lambda c: pd.to_numeric(df[c], errors="coerce").fillna(0)
    branch = branch_names(df, store_id)
    return pd.DataFrame({
        "Date": day,
        "Store ID": store_id,
        "Branch": branch,
        "Store Type": branch.map(store_type),
        "SKU": df["SKU"].str.strip(),
        "Barcode": df["Barcode"].fillna("").str.strip(),
        "Product": df["Nama Produk"].str.strip(),
        "Brand": df["Brand"].str.strip().str.upper(),
        "Value Qty": num("QTY Terjual"),
        "Value IDR": num("Grand Total"),
        "Gross IDR": num("Total Harga"),
        "Discount IDR": num("Total Diskon"),
        "HPP IDR": num("Total HPP"),
    })[OUT_COLUMNS]


def stock_rows(content: bytes, store_id: str, snapshot: str) -> pd.DataFrame:
    """Every product's current stock for one store (all rows, sold or not)."""
    df = read_portal_csv(content)
    if df.empty:
        return pd.DataFrame(columns=STOCK_COLUMNS)
    branch = branch_names(df, store_id)
    return pd.DataFrame({
        "Snapshot Date": snapshot,
        "Store ID": store_id,
        "Branch": branch,
        "Store Type": branch.map(store_type),
        "SKU": df["SKU"].str.strip(),
        "Barcode": df["Barcode"].fillna("").str.strip(),
        "Product": df["Nama Produk"].str.strip(),
        "Stok": pd.to_numeric(df["Stok"], errors="coerce").fillna(0),
    })[STOCK_COLUMNS]


def month_frame(entries: dict) -> pd.DataFrame:
    """entries = {'<store id>/<YYYY-MM-DD>.csv': bytes} for ONE month -> all sold rows, sorted."""
    frames = []
    for name in sorted(entries):
        sid, fname = name.split("/")
        frames.append(parse_store_day(entries[name], sid, fname[:-4]))
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=OUT_COLUMNS)
    df = pd.concat(frames, ignore_index=True)
    return df.sort_values(["Date", "Store ID", "SKU"], kind="stable").reset_index(drop=True)


def read_zip(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {n: z.read(n) for n in z.namelist() if n.endswith(".csv")}


def write_zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name in sorted(entries):
            z.writestr(name, entries[name])
    return buf.getvalue()
