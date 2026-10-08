"""Guard for the SAT / MIDI / IDM sell-out pipeline: say so loudly when data stops arriving.

Runs at the end of every collect (b2b-collect.yml). Failures it exists for:
  * report emails stop reaching GitHub (Power Automate flow broken: nothing new for 2026-10-07/08)
  * a finished month is missing its last days (the portal had no data when first asked: Sep 29-30)
  * the sales data stops moving forward, or has holes in the middle
  * report emails sit open because they cannot be downloaded

Result: exit code 1 (the run goes red and GitHub emails the owner) AND one rolling GitHub issue
"ALERT|minimarket-guard" that lists every problem, is updated while the problems change, gets a
comment when they are gone, and is closed. Issues starting with ALERT| are never read by the
collector (it only takes B2B|).

    python guard_minimarket.py            # check + alert
    python guard_minimarket.py --dry-run  # check, print, change nothing
"""
import argparse
import calendar
import io
import os
import sys
from datetime import datetime, timedelta, timezone, date

import pandas as pd
import requests
from google.cloud import storage

import gcs_paths

WIB = timezone(timedelta(hours=7))
ALERT_TITLE = "ALERT|minimarket-guard"
BRANDS = ("alfamart", "alfamidi")
EMAIL_MAX_AGE_H = 30        # a report email normally lands every morning, ~08:00 WIB
STALE_DAYS = 5              # the portals publish D-2..D-3; older than this means the feed is stuck
FINAL_AFTER_DAY = 4         # from the 4th, the previous month must be complete
STUCK_ISSUE_H = 48
SALES = "sales_parquet/Minimarket_Sales.parquet"


class GitHub:
    def __init__(self, repo, token):
        self.base = f"https://api.github.com/repos/{repo}"
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})

    def call(self, method, path, **kw):
        r = self.s.request(method, self.base + path, timeout=60, **kw)
        r.raise_for_status()
        return r.json() if r.content else None

    def issues(self, **params):
        page = 1
        while True:
            batch = self.call("GET", "/issues", params={"per_page": 100, "page": page, **params})
            if not batch:
                return
            for i in batch:
                if "pull_request" not in i:
                    yield i
            page += 1


def check_emails(gh, now, problems):
    """newest report email per brand, and report emails stuck open"""
    newest = {b: None for b in BRANDS}
    stuck = []
    for i in gh.issues(state="all", sort="created", direction="desc", per_page=100):
        title = i.get("title") or ""
        if not title.startswith("B2B|"):
            continue
        brand = title.split("|")[1] if "|" in title else ""
        created = datetime.fromisoformat(i["created_at"].replace("Z", "+00:00"))
        if brand in newest and newest[brand] is None:
            newest[brand] = created
        if i["state"] == "open" and now - created > timedelta(hours=STUCK_ISSUE_H):
            stuck.append(i["number"])
        if all(newest.values()) and created < now - timedelta(days=3):
            break
    for b, t in newest.items():
        if t is None or now - t > timedelta(hours=EMAIL_MAX_AGE_H):
            when = "never" if t is None else t.astimezone(WIB).strftime("%d %b %H:%M WIB")
            problems.append(f"**No {b} report email in GitHub since {when}.** First look in Outlook (Inbox\{b.title()}, "
                            f"Junk, Deleted): if the newest report email there is also old, the portal accepted the "
                            f"request but did not send the email - ask the portal / retry later; if newer emails are "
                            f"there, the Power Automate flow 'B2B {b.title()} to GitHub' stopped (turned on? connections?).")
    if stuck:
        problems.append(f"**{len(stuck)} report email(s) open for more than {STUCK_ISSUE_H} h** "
                        f"(link expired or download failing): #{', #'.join(map(str, stuck[:8]))}")


def check_data(today, problems):
    b = storage.Client().bucket(gcs_paths.BUCKET)
    blob = b.get_blob(SALES)
    if blob is None:
        problems.append(f"**{SALES} is missing.**")
        return
    df = pd.read_parquet(io.BytesIO(blob.download_as_bytes()), columns=["Date", "Account"])
    df["Date"] = pd.to_datetime(df["Date"]).dt.date
    first_of_month = today.replace(day=1)
    prev_end = first_of_month - timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    for acct, g in df.groupby("Account"):
        have = set(g["Date"])
        last = max(have)
        if (today - last).days > STALE_DAYS:
            problems.append(f"**{acct}: sales data stops at {last}** ({(today - last).days} days ago).")
        # holes in the last 45 days, up to the last date we have
        lo = max(today - timedelta(days=45), min(have))
        holes = [d for d in pd.date_range(lo, last).date if d not in have]
        # the previous month must be complete once it is a few days old (the portal fills in late)
        if today.day >= FINAL_AFTER_DAY:
            miss = [d for d in pd.date_range(prev_start, prev_end).date if d not in have]
            if miss:
                problems.append(f"**{acct}: {prev_start:%B %Y} is incomplete** - no sales on "
                                f"{', '.join(str(d) for d in miss[:10])}"
                                f"{' ...' if len(miss) > 10 else ''}. The month-end snapshot never refreshed.")
                holes = [d for d in holes if d not in miss]
        if holes:
            problems.append(f"**{acct}: gap in the sales data** - no sales on "
                            f"{', '.join(str(d) for d in holes[:10])}{' ...' if len(holes) > 10 else ''}.")


def sync_alert(gh, problems, run_url, dry):
    existing = next((i for i in gh.issues(state="open", per_page=100) if (i.get("title") or "") == ALERT_TITLE), None)
    stamp = datetime.now(WIB).strftime("%d %b %Y %H:%M WIB")
    if problems:
        body = (f"The minimarket guard found {len(problems)} problem(s) at {stamp}"
                f"{f' ({run_url})' if run_url else ''}:\n\n" + "\n".join(f"- {p}" for p in problems) +
                "\n\nThis issue updates itself and closes when everything is fine again.")
        if dry:
            return
        if existing is None:
            gh.call("POST", "/issues", json={"title": ALERT_TITLE, "body": body})
        elif (existing.get("body") or "").split(":\n\n", 1)[-1] != body.split(":\n\n", 1)[-1]:
            gh.call("PATCH", f"/issues/{existing['number']}", json={"body": body})
            gh.call("POST", f"/issues/{existing['number']}/comments", json={"body": f"Updated {stamp}."})
    elif existing is not None and not dry:
        gh.call("POST", f"/issues/{existing['number']}/comments", json={"body": f"All clear at {stamp}."})
        gh.call("PATCH", f"/issues/{existing['number']}", json={"state": "closed", "state_reason": "completed"})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    now = datetime.now(timezone.utc)
    today = datetime.now(WIB).date()
    gh = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    problems = []
    check_emails(gh, now, problems)
    check_data(today, problems)
    run_url = ""
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    if problems:
        print(f"--- guard: {len(problems)} problem(s) ---")
        for p in problems:
            print("  " + p.replace("**", ""))
    else:
        print("--- guard: all clear (report emails arriving, sales data current and complete) ---")
    sync_alert(gh, problems, run_url, a.dry_run)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
