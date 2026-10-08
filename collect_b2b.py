"""Download the B2B report links that Power Automate filed as GitHub issues.

    Portal emails a report link -> Outlook (lmbg.co.id)
      -> Power Automate "Create an issue", title "B2B|<brand>|<subject>|<received>",
         body = the email body
      -> this script (scheduled): download each link, upload it to the draft
         GCS prefix, close the issue.

    python collect_b2b.py                  # drain every open B2B issue
    python collect_b2b.py --expect-today   # also FAIL if a brand sent nothing today

--expect-today is for the last slot of the day. Without it, a broken flow
(no issues at all) looks exactly like a quiet mailbox: "nothing to collect",
green run, no data. That silent-success trap is how collection went dark on
the laptop from 2026-09-21 with every status reading "ok".

Links are signed Google Storage URLs valid ~24 h and need no login, so the
download is a plain GET. They are never printed: this repo's logs and issues
must not carry a working link to sales data.
"""

import argparse
import base64
import hashlib
import html
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import requests
from google.cloud import storage

import gcs_paths

API = "https://api.github.com"
TITLE_PREFIX = "B2B|"
BRAND_MARKERS = {"alfamart": "b2b-np@smtp.sat.co.id", "alfamidi": "b2b_midi@smtp.sat.co.id"}
URL_RE = re.compile(r"""https?://[^\s"'<>]+""")
WIB = timezone(timedelta(hours=7))
# The portal has no data for the last ~2 days of a period when it is first requested, and the
# same period is re-requested every day under the SAME file name. A period that ended up to
# this many days ago is therefore downloaded again and replaces the stored file if it changed
# (2026-10-08: September stopped at the 28th for Alfamart/Alfamidi because the 30 Sep snapshot
# was never replaced). Older periods are final.
REFRESH_DAYS = 14


def refreshable(filename: str) -> bool:
    m = re.search(r"_sd_(\d{4}-\d\d-\d\d)", filename)
    if not m:
        return False
    end = datetime.fromisoformat(m.group(1)).date()
    return (datetime.now(WIB).date() - end).days <= REFRESH_DAYS


def unwrap_safelinks(url: str) -> str:
    """Recover the real URL from a Microsoft Defender Safe Links wrapper.

    Ported from Automation/Mail-ReportLinks/report_links.py, where it is proven:
    parse_qs percent-decodes exactly once, which is right - the inner URL is
    double-encoded, and a second decode would corrupt the %2F / %40 inside the
    signed URL's credential and break the signature.
    """
    host = (urlparse(url).hostname or "").lower()
    if not host.endswith("safelinks.protection.outlook.com"):
        return url
    return parse_qs(urlparse(url).query).get("url", [None])[0] or url


def report_link(body: str):
    text = html.unescape(body or "")
    # Outlook keeps the true target in originalsrc="..." - prefer it over the wrapper.
    candidates = re.findall(r'originalsrc="([^"]+)"', text) + URL_RE.findall(text)
    for c in candidates:
        u = unwrap_safelinks(c)
        p = urlparse(u)
        if (p.hostname or "").endswith("storage.googleapis.com") and p.path.lower().endswith(".csv"):
            return u
    return None


def route(filename: str):
    # Same markers as the laptop's imap_b2b_download.route_subfolder(), plus stock.
    if "by_branch_Stok" in filename:
        return "stock"
    if "by_branch_Selling_Out" in filename:
        return "sell_out_branch"
    if "_All_Store" in filename:
        return "sell_out_store"
    return None


def brand_of(issue):
    parts = (issue.get("title") or "").split("|")
    if len(parts) > 1 and parts[1].strip().lower() in BRAND_MARKERS:
        return parts[1].strip().lower()
    body = issue.get("body") or ""
    return next((b for b, mark in BRAND_MARKERS.items() if mark in body), None)


class GitHub:
    def __init__(self, repo: str, token: str):
        self.repo = repo
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}",
                               "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28"})

    def call(self, method, path, **kw):
        r = self.s.request(method, f"{API}/repos/{self.repo}{path}", timeout=60, **kw)
        r.raise_for_status()
        return r.json() if r.content else None

    def issues(self, **params):
        page = 1
        while True:
            batch = self.call("GET", "/issues", params={"per_page": 100, "page": page, **params})
            if not batch:
                return
            for i in batch:
                if "pull_request" not in i and (i.get("title") or "").startswith(TITLE_PREFIX):
                    yield i
            page += 1

    def close(self, number: int, comment: str, completed: bool):
        self.call("POST", f"/issues/{number}/comments", json={"body": comment})
        self.call("PATCH", f"/issues/{number}",
                  json={"state": "closed", "state_reason": "completed" if completed else "not_planned"})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--expect-today", action="store_true",
                    help="fail if a brand has no report email at all today (last daily slot)")
    a = ap.parse_args()

    gh = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    bucket = storage.Client().bucket(gcs_paths.BUCKET)

    saved = already = 0
    problems = []
    seen_files = set()
    # Materialise first: closing issues while paging through "open" would shift pages.
    for issue in list(gh.issues(state="open")):
        n = issue["number"]
        brand = brand_of(issue)
        url = report_link(issue.get("body"))
        if not brand or not url:
            why = "unknown brand" if not brand else "no report link found"
            gh.close(n, f"Not collected: {why}.", completed=False)
            problems.append(f"#{n}: {why}")
            continue

        filename = url.split("?")[0].split("/")[-1]   # identical to the laptop's naming
        report = route(filename)
        if not report:
            gh.close(n, f"Not collected: `{filename}` is neither by-branch nor by-store.", completed=False)
            problems.append(f"#{n}: unroutable {filename}")
            continue

        blob = bucket.blob(gcs_paths.prefix(brand, report) + filename)
        # Keyed on the full path, never the bare filename: Alfamart and Alfamidi
        # name their exports identically, so a filename key made the second
        # brand's report look "already there" and closed it uncollected.
        exists = blob.exists()
        if blob.name in seen_files or (exists and not refreshable(filename)):
            # Periods that ended long ago are re-requested every day and never change.
            # (Issues are listed newest first, so the first copy seen in a run is the freshest.)
            already += 1
            gh.close(n, f"Already in `gs://{gcs_paths.BUCKET}/{blob.name}`.", completed=True)
            continue

        r = requests.get(url, timeout=300)
        if r.status_code in (400, 401, 403):
            gh.close(n, f"Link expired or refused (HTTP {r.status_code}) before collection.", completed=False)
            problems.append(f"#{n}: {filename} link expired (HTTP {r.status_code})")
            continue
        if r.status_code != 200 or not r.content or r.content.lstrip()[:1] == b"<":
            problems.append(f"#{n}: {filename} bad download (HTTP {r.status_code}, {len(r.content)} bytes)")
            continue   # left OPEN: the next slot retries it while the link is still alive

        if exists:
            blob.reload()
            if blob.md5_hash == base64.b64encode(hashlib.md5(r.content).digest()).decode():
                seen_files.add(blob.name)
                already += 1
                gh.close(n, f"Already in `gs://{gcs_paths.BUCKET}/{blob.name}` (same content).", completed=True)
                continue
            print(f"  refreshed {brand}/{report}/{filename}: newer data than the stored copy")
        blob.upload_from_string(r.content, content_type="text/csv")
        seen_files.add(blob.name)
        saved += 1
        print(f"  saved {brand}/{report}/{filename}  ({len(r.content):,} bytes)")
        gh.close(n, f"Saved to `gs://{gcs_paths.BUCKET}/{blob.name}` ({len(r.content):,} bytes).", completed=True)

    print(f"\n--- {saved} saved, {already} already there, {len(problems)} problem(s) ---")
    for p in problems:
        print(f"  PROBLEM {p}")

    if a.expect_today:
        start = datetime.now(WIB).replace(hour=0, minute=0, second=0, microsecond=0)
        today_brands = {brand_of(i) for i in gh.issues(state="all", since=start.isoformat())
                        if datetime.fromisoformat(i["created_at"].replace("Z", "+00:00")) >= start}
        for b in BRAND_MARKERS:
            if b not in today_brands:
                msg = f"no {b} report email reached GitHub today - check the Power Automate flow"
                problems.append(msg)
                print(f"  PROBLEM {msg}")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
