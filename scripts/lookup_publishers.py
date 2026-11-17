"""按 DOI 批量查 Crossref publisher，为缺失全文清单标注出版社归属。

用法: python scripts/lookup_publishers.py [--csv 缺失清单]
输出: data/output/cache/doi_publisher_map.json  {doi_lower: publisher}
"""
import argparse
import csv
import json
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent.parent
MISSING_CSV = BASE / "data/output/exports/full/missing_relevant_remaining_20261101.csv"
OUT = BASE / "data/output/cache/doi_publisher_map.json"
UA = {"User-Agent": "potato-lit-search/1.0 (mailto:codex@example.com)"}
BATCH = 40


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(MISSING_CSV))
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.csv, encoding="utf-8-sig")))
    dois = sorted({r["doi"].strip().lower() for r in rows if r.get("doi", "").strip()})
    print(f"rows={len(rows)} with_doi={len(dois)}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    done = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    todo = [d for d in dois if d not in done]

    s = requests.Session()
    for i in range(0, len(todo), BATCH):
        chunk = todo[i : i + BATCH]
        url = "https://api.crossref.org/works"
        params = {
            "filter": ",".join("doi:" + d for d in chunk),
            "rows": 100,
            "select": "DOI,publisher",
        }
        for attempt in range(5):
            r = s.get(url, params=params, headers=UA, timeout=60)
            if r.status_code == 200:
                break
            time.sleep(2 * (attempt + 1))
        if r.status_code != 200:
            print(f"batch {i} FAILED {r.status_code} {r.text[:120]}")
            continue
        for it in r.json()["message"]["items"]:
            done[it["DOI"].lower()] = it.get("publisher", "")
        for d in chunk:
            done.setdefault(d, "")
        print(f"batch {i}-{i + len(chunk)}: total {len(done)}", flush=True)
        OUT.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
        time.sleep(0.4)

    OUT.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
    hit = sum(1 for v in done.values() if v)
    print(f"resolved {hit}/{len(done)}")


if __name__ == "__main__":
    main()
