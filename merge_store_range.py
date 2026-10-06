"""Merge a re-pulled by-store date range (collected into the draft) into production.

The portal can be asked again for any date range (b2b-refire.yml). The collector
saves the answer in the draft, but production only ever receives the 3 newest rolling
periods, so an older fix never reached it. This copies the re-pulled rows over the
same dates in production, file by file:

  * a production file that overlaps [start, end]: its rows outside the range are kept,
    its rows inside the range are replaced by the re-pulled ones;
  * dates in the range that no production file covers get a new file (all-zero ones
    are skipped);
  * a production 'All_Category' file (Alfamart, whole months) is rebuilt from all the
    re-pulled categories - only when the file lies fully inside the range.

Every file it replaces is first copied to <chain>/store_backup/ in the draft prefix.
Streams line by line, so a 1 GB file is fine. Dry run by default.

    python merge_store_range.py --start 2026-03-01 --end 2026-03-31 [--brand alfamart] [--go]
"""
import argparse
import datetime as dt
import os
import re
import sys
import tempfile

from google.cloud import storage

import gcs_paths

storage.blob._DEFAULT_CHUNKSIZE = 8 * 1024 * 1024
MON = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
MEASURES = (("Value", "daily_sell_out_value"), ("Qty", "daily_sell_out_qty"))


def row_date(line):
    p = line.split("|", 1)[0].split("-")
    if len(p) == 3 and p[1].lower() in MON:
        return dt.date(2000 + int(p[2]), MON[p[1].lower()], int(p[0]))
    return None


def rx(measure):
    return re.compile(rf"Selling_Out_{measure}_(?:BRANCH_NASIONAL_All_Store_)?(.+?)_All_Item_"
                      rf"(?:NASIONAL_All_Store_)?O-0108_(\d{{4}}-\d\d-\d\d)_sd_(\d{{4}}-\d\d-\d\d)\.csv$")


def iso(s):
    return dt.date.fromisoformat(s)


def download(blob, tag):
    path = os.path.join(tempfile.gettempdir(), f"msr_{tag}.csv")
    blob.download_to_filename(path, timeout=1800)
    return path


def split_header(path):
    head = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for line in f:
            head.append(line)
            if line.startswith("periode|"):
                return head
    raise SystemExit(f"no 'periode|' header in {path}")


def rows(path, lo, hi):
    """data rows of path with lo <= date <= hi"""
    with open(path, encoding="utf-8-sig", newline="") as f:
        for line in f:
            d = row_date(line)
            if d and lo <= d <= hi:
                yield line


def value_sum(lines):
    t = 0.0
    for l in lines:
        try:
            t += float(l.rstrip().rsplit("|", 1)[-1] or 0)
        except ValueError:
            pass
    return t


def uncovered(lo, hi, spans):
    out, d = [], lo
    while d <= hi:
        if not any(a <= d <= z for a, z in spans):
            s = d
            while d + dt.timedelta(1) <= hi and not any(a <= d + dt.timedelta(1) <= z for a, z in spans):
                d += dt.timedelta(1)
            out.append((s, d))
        d += dt.timedelta(1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--brand", choices=["both", "alfamart", "alfamidi"], default="both")
    ap.add_argument("--go", action="store_true", help="write to production (default: dry run)")
    a = ap.parse_args()
    s, e = iso(a.start), iso(a.end)
    b = storage.Client().bucket("bucket_som")
    chains = ["alfamart", "alfamidi"] if a.brand == "both" else [a.brand]
    log = []

    def say(msg):
        print(msg, flush=True)
        log.append(msg)

    for chain in chains:
        dpre = gcs_paths.prefix(chain, "sell_out_store")
        for measure, psub in MEASURES:
            ppre = f"sales_sell out_minimarket/{chain}/{psub}/"
            R = rx(measure)
            draft = {}
            for x in b.list_blobs(prefix=dpre):
                m = R.search(x.name.replace("%26", "&"))
                if m and (iso(m.group(2)), iso(m.group(3))) == (s, e):
                    draft[m.group(1)] = x
            if not draft:
                say(f"{chain} {measure}: no re-pulled {s}..{e} in the draft - nothing to do")
                continue
            prod = []
            for x in b.list_blobs(prefix=ppre):
                m = R.search(x.name.replace("%26", "&"))
                if m and iso(m.group(2)) <= e and iso(m.group(3)) >= s:
                    prod.append((m.group(1), iso(m.group(2)), iso(m.group(3)), x))
            say(f"{chain} {measure}: {len(draft)} re-pulled categories, {len(prod)} overlapping production files")
            pulled = {}

            def get_pulled(cat):
                if cat not in pulled:
                    pulled[cat] = download(draft[cat], f"{chain}_{measure}_{len(pulled)}")
                return pulled[cat]

            def write_new(p_blob_name, header, lines_iter, gen):
                tmp = os.path.join(tempfile.gettempdir(), "msr_out.csv")
                n = 0
                with open(tmp, "w", encoding="utf-8", newline="") as f:
                    f.writelines(header)
                    for l in lines_iter:
                        f.write(l if l.endswith("\n") else l + "\n")
                        n += 1
                b.blob(p_blob_name).upload_from_filename(tmp, content_type="text/csv", timeout=3600,
                                                         if_generation_match=gen)
                os.remove(tmp)
                return n

            def backup(x):
                dest = f"sales_parquet/raw/minimarket/{chain}/store_backup/{x.name.rsplit('/', 1)[-1]}"
                if not b.blob(dest).exists():
                    b.copy_blob(x, b, dest)

            all_cat = [p for p in prod if p[0] == "All_Category"]
            per_cat = [p for p in prod if p[0] != "All_Category"]
            for _, pa, pz, x in all_cat:
                if pa >= s and pz <= e:
                    say(f"  {x.name.rsplit('/', 1)[-1][-60:]}: rebuild All_Category {pa}..{pz} from {len(draft)} categories")
                    if a.go:
                        head = split_header(download(x, "allcat_old"))
                        old_sum = value_sum(rows(os.path.join(tempfile.gettempdir(), "msr_allcat_old.csv"), pa, pz)) if measure == "Value" else 0
                        backup(x)
                        def gen():
                            for c in sorted(draft):
                                yield from rows(get_pulled(c), pa, pz)
                        n = write_new(x.name, head, gen(), x.generation)
                        say(f"    wrote {n:,} rows" + (f" (old value {old_sum:,.0f})" if old_sum else ""))
                else:
                    say(f"  SKIPPED All_Category file {pa}..{pz}: not fully inside {s}..{e}")
            if all_cat:
                continue   # the All_Category file covers every category
            for cat in sorted(draft):
                mine = [p for p in per_cat if p[0] == cat]
                spans = [(max(pa, s), min(pz, e)) for _, pa, pz, _ in mine]
                for _, pa, pz, x in mine:
                    lo, hi = max(pa, s), min(pz, e)
                    say(f"  {cat}: replace {lo}..{hi} in {x.name.rsplit('/', 1)[-1][-50:]}")
                    if a.go:
                        old = download(x, "old")
                        head = split_header(old)
                        keep = (l for l in rows(old, dt.date.min, dt.date.max) if not (lo <= row_date(l) <= hi))
                        old_sum = value_sum(rows(old, lo, hi)) if measure == "Value" else 0
                        backup(x)
                        new_iter = rows(get_pulled(cat), lo, hi)
                        def both():
                            yield from keep
                            yield from new_iter
                        n = write_new(x.name, head, both(), x.generation)
                        say(f"    wrote {n:,} rows" + (f" (replaced value {old_sum:,.0f})" if old_sum else ""))
                for lo, hi in uncovered(s, e, spans):
                    src = get_pulled(cat) if a.go else None
                    name = ppre + re.sub(r"\d{4}-\d\d-\d\d_sd_\d{4}-\d\d-\d\d", f"{lo}_sd_{hi}",
                                         draft[cat].name.rsplit("/", 1)[-1])
                    if a.go:
                        data = list(rows(src, lo, hi))
                        if not any(float(l.rstrip().rsplit("|", 1)[-1] or 0) for l in data):
                            say(f"  {cat}: {lo}..{hi} not in production and all zero - skipped")
                            continue
                        n = write_new(name, split_header(src), data, 0)
                        say(f"  {cat}: created {name.rsplit('/', 1)[-1][-60:]} ({n:,} rows)")
                    else:
                        say(f"  {cat}: {lo}..{hi} not in production - would create a new file")
    print("done" + ("" if a.go else " (dry run - add --go to write)"))


if __name__ == "__main__":
    sys.exit(main())
