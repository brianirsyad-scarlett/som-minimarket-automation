"""Guard for the Miss Glam sell-out job: say so when it stops updating.

Runs at the end of miss-glam-daily.yml. Checks, against GCS (the job runs at 02:45 WIB, so yesterday must be complete):
  * Miss Glam rows exist in Minimarket_Sales.parquet up to yesterday, with no hole in the last 14 days
  * yesterday has about as many stores selling as the recent days (a half-downloaded day shows up here)
  * today's stock snapshot was written (draft stock/<date>_Miss Glam Stock.csv) and covers most stores
  * the portal login still works - the download step itself fails loudly otherwise, this only reports it

Result: exit 1 (red run, GitHub emails the owner) AND one rolling issue "ALERT|miss-glam-guard" that updates
itself and closes when everything is fine (same mechanism as guard_minimarket.py).

    python guard_miss_glam.py            # check + alert
    python guard_miss_glam.py --dry-run  # check, print, change nothing
"""
import argparse
import io
import os
import sys
from datetime import datetime, timedelta

import pandas as pd
from google.cloud import storage

import gcs_paths
import guard_minimarket as gm

ALERT_TITLE = "ALERT|miss-glam-guard"
ACCOUNT = "MISS GLAM"
MIN_STOCK_STORES = 40


def check(today, problems):
    b = storage.Client().bucket(gcs_paths.BUCKET)
    blob = b.get_blob(gm.SALES)
    if blob is None:
        problems.append(f"**{gm.SALES} is missing.**")
        return
    df = pd.read_parquet(io.BytesIO(blob.download_as_bytes()), columns=["Date", "Branch", "Account"])
    df = df[df["Account"] == ACCOUNT]
    if df.empty:
        problems.append(f"**No {ACCOUNT} rows in Minimarket_Sales.parquet.** The nightly publish or the parquet rebuild has not run.")
        return
    df["Date"] = pd.to_datetime(df["Date"]).dt.date
    yesterday = today - timedelta(days=1)
    last = df["Date"].max()
    if last < yesterday:
        problems.append(f"**Miss Glam sales stop at {last}** (expected {yesterday}). The 02:45 job or its publish step failed - "
                        "check the 'Miss Glam Sell Out + Stock' runs.")
    per_day = df.groupby("Date")["Branch"].nunique()
    window = [d for d in pd.date_range(today - timedelta(days=14), yesterday).date]
    holes = [d for d in window if d not in per_day.index]
    if holes:
        problems.append(f"**No Miss Glam sales on {', '.join(str(d) for d in holes[:8])}.**")
    base = per_day[(per_day.index >= today - timedelta(days=14)) & (per_day.index < yesterday)]
    if yesterday in per_day.index and len(base) >= 5 and per_day[yesterday] < 0.7 * base.median():
        problems.append(f"**Only {per_day[yesterday]} stores sold on {yesterday}** (usual ~{int(base.median())}) - "
                        "a store download probably failed; the next run re-downloads the last 5 days.")
    snap = b.get_blob(f"{gcs_paths.ROOT}/miss_glam/stock/{today}_Miss Glam Stock.csv")
    if snap is None:
        problems.append(f"**No stock snapshot for {today}.**")
    else:
        stores = pd.read_csv(io.BytesIO(snap.download_as_bytes()), usecols=["Store ID"])["Store ID"].nunique()
        if stores < MIN_STOCK_STORES:
            problems.append(f"**Stock snapshot {today} covers only {stores} stores** (expected 40+).")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    today = datetime.now(gm.WIB).date()
    problems = []
    check(today, problems)
    if problems:
        print(f"--- miss-glam guard: {len(problems)} problem(s) ---")
        for p in problems:
            print("  " + p.replace("**", ""))
    else:
        print("--- miss-glam guard: all clear (sales to yesterday, no holes, stock snapshot written) ---")
    if os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPOSITORY"):
        run_url = ""
        if os.environ.get("GITHUB_RUN_ID"):
            run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                       f"{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}")
        gh = gm.GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
        gm.sync_alert(gh, problems, run_url, a.dry_run, title=ALERT_TITLE, who="Miss Glam guard")
    return 1 if (problems and not a.dry_run) else 0


if __name__ == "__main__":
    sys.exit(main())
