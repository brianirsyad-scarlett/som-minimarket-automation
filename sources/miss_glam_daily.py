"""Download the Miss Glam M2 sales CSV for every store and the newest few days.

    python sources/miss_glam_daily.py --out out/miss_glam --look-back 5

Writes <out>/<store id>/<YYYY-MM-DD>.csv exactly as the portal exports it (comma-free ';' CSV).
The portal allows ONE session per user: this logs in once and everything shares that session, and a login
anywhere else (a browser, the laptop script) logs this run out - the session is re-created automatically.

Secrets (env): GLAMINDO_USERNAME, GLAMINDO_PASSWORD. Optional GLAMINDO_BASE_URL.
Exit code 1 when any store-day could not be downloaded; everything that did download is still written.
"""
import argparse
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

EXPORT = ("/laporanPrincipal/exportCsvLaporanPenjualanPerkriteriaProduk?dateStart={d}&dateEnd={d}"
          "&toko={s}&filter_brand=&filterby=&kategori=&subkategori=&subkategori2=&delimiter=;")


class Portal:
    def __init__(self, base, user, pw):
        self.base, self.user, self.pw = base.rstrip("/"), user, pw
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "Mozilla/5.0"
        self.lock = threading.Lock()
        self.login_at = 0.0
        self.report_page = ""

    def login(self, seen=None):
        with self.lock:
            if seen is not None and self.login_at > seen:  # another thread already re-logged in
                return
            for attempt in range(5):  # the portal sometimes rejects a valid login once
                try:
                    self.s.cookies.clear()
                    self.s.get(self.base + "/", timeout=60)
                    self.s.post(self.base + "/login/auth", timeout=60,
                                data={"loc": "", "username": self.user, "password": self.pw})
                    page = self.s.get(self.base + "/laporanPrincipal", timeout=60)
                    if "dateStart" in page.text:
                        self.login_at, self.report_page = time.time(), page.text
                        return
                except requests.RequestException as e:
                    print(f"login attempt {attempt + 1}: {type(e).__name__}", flush=True)
                time.sleep(15 * (attempt + 1))
            sys.exit("Login failed 5 times - check GLAMINDO_USERNAME / GLAMINDO_PASSWORD "
                     "(the portal may also have expired the password)")

    def stores(self):
        sel = re.search(r'<select class="select2" id="toko">([\s\S]*?)</select>', self.report_page).group(1)
        return [(v, n.strip()) for v, n in re.findall(r'<option value="(\d+)">([^<]*)', sel)]

    def has_access(self, store):
        r = self.s.post(self.base + "/laporan/cekUserAccess", data={"idToko": store}, timeout=60)
        return r.text.strip() == "1"

    def fetch(self, store, day):
        url = self.base + EXPORT.format(d=day, s=store)
        for attempt in range(6):
            stamp = time.time()
            try:
                r = self.s.get(url, timeout=120)
            except requests.RequestException:
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 200 and "text/csv" in r.headers.get("Content-Type", ""):
                return r.content
            if r.status_code == 200:  # HTML instead of CSV = session expired
                self.login(seen=stamp)
            else:
                time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"giving up on store {store} {day}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--look-back", type=int, default=5, help="days to (re)download, ending today")
    ap.add_argument("--stores", help="comma separated store ids (default: every store you can access)")
    ap.add_argument("--workers", type=int, default=2)
    a = ap.parse_args()

    portal = Portal(os.environ.get("GLAMINDO_BASE_URL", "https://m2.glamindo.com"),
                    os.environ["GLAMINDO_USERNAME"], os.environ["GLAMINDO_PASSWORD"])
    portal.login()
    wanted = set(a.stores.split(",")) if a.stores else None
    stores = []
    for sid, name in portal.stores():
        if wanted and sid not in wanted:
            continue
        if portal.has_access(sid):
            stores.append((sid, name))
        else:
            print(f"skip {sid} {name}: no access")
    if not stores:
        sys.exit("No store is accessible - refusing to continue")
    print(f"{len(stores)} stores")

    today = datetime.now(ZoneInfo("Asia/Jakarta")).date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(a.look_back)]
    failed = []

    def job(args):
        sid, day = args
        try:
            body = portal.fetch(sid, day)
        except RuntimeError as e:
            failed.append(str(e))
            print(e, flush=True)
            return
        d = a.out / sid
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{day}.csv").write_bytes(body)

    with ThreadPoolExecutor(a.workers) as ex:
        list(ex.map(job, [(sid, day) for day in days for sid, _ in stores]))
    n = sum(1 for _ in a.out.glob("*/*.csv"))
    print(f"downloaded {n} store-day file(s) for {days[-1]}..{days[0]}; {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
