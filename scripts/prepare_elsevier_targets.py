"""为缺失的 Elsevier 文献取 ScienceDirect PII（alternative-id），生成批次清单。

用法: python scripts/prepare_elsevier_targets.py --limit 100
输出: data/output/cache/elsevier_targets_batch<N>.csv  (pmid,doi,pii,journal,year)
"""
import argparse
import csv
import json
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SRC = BASE / "data/output/exports/full/missing_subscribed_20261101.csv"
CACHE = BASE / "data/output/cache/doi_pii_map.json"
UA = {"User-Agent": "potato-lit-search/1.0 (mailto:codex@example.com)"}


def fetch_pii(dois):
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    todo = [d for d in dois if d not in cache]
    for i in range(0, len(todo), 40):
        chunk = todo[i : i + 40]
        q = "filter=" + urllib.parse.quote(",".join("doi:" + d for d in chunk), safe="")
        url = f"https://api.crossref.org/works?{q}&rows=100&select=DOI,alternative-id"
        for attempt in range(5):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90) as r:
                    items = json.load(r)["message"]["items"]
                break
            except Exception as e:
                print("retry", i, type(e).__name__, e)
                time.sleep(3 * (attempt + 1))
        else:
            continue
        for it in items:
            alt = it.get("alternative-id") or []
            pii = next((a for a in alt if a.startswith("S") and a.isalnum() and len(a) > 10), "")
            cache[it["DOI"].lower()] = pii
        for d in chunk:
            cache.setdefault(d, "")
        CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        print(f"crossref {i + len(chunk)}/{len(todo)}", flush=True)
        time.sleep(0.4)
    return cache


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--batch", default="1")
    ap.add_argument("--interleave", action="store_true", help="各刊轮流取样，避免一批全是同一本刊")
    ap.add_argument("--per-journal", type=int, default=10, help="轮转取样时每刊上限")
    ap.add_argument("--skip-existing", action="store_true", help="跳过本地已有全文的 PMID")
    args = ap.parse_args()

    rows = [r for r in csv.DictReader(open(SRC, encoding="utf-8-sig"))
            if "Elsevier" in r["db"] and (r.get("doi") or "").strip()]
    if args.skip_existing:
        pdfs = BASE / "data/pdfs_relevant"
        rows = [r for r in rows if not (
            (pdfs / f"{r['pmid']}.pdf").exists() and (pdfs / f"{r['pmid']}.pdf").stat().st_size > 10240
        )]
    freq = Counter(r["journal"].strip() for r in rows)
    rows.sort(key=lambda r: (-freq[r["journal"].strip()], -int(str(r["year"])[:4] or 0), r["pmid"]))
    if args.interleave:
        buckets = {}
        for r in rows:
            buckets.setdefault(r["journal"].strip(), []).append(r)
        picked, taken = [], {}
        # 只轮转缺口最大的若干刊，否则第一轮就会被上百个小刊填满
        top_n = max(1, -(-args.limit // max(1, args.per_journal)))
        queues = sorted(buckets.items(), key=lambda kv: -len(kv[1]))[:top_n]
        while len(picked) < args.limit:
            progressed = False
            for journal, q in queues:
                if len(picked) >= args.limit:
                    break
                if q and taken.get(journal, 0) < args.per_journal:
                    picked.append(q.pop(0))
                    taken[journal] = taken.get(journal, 0) + 1
                    progressed = True
            if not progressed:
                break
    else:
        picked = rows[: args.limit]

    cache = fetch_pii([r["doi"].strip().lower() for r in picked])
    out = BASE / f"data/output/cache/elsevier_targets_batch{args.batch}.csv"
    payload = [
        {
            "pmid": r["pmid"],
            "doi": r["doi"],
            "pii": cache.get(r["doi"].strip().lower(), ""),
            "journal": r["journal"],
            "year": r["year"],
        }
        for r in picked
    ]
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pmid", "doi", "pii", "journal", "year"])
        for p in payload:
            w.writerow([p["pmid"], p["doi"], p["pii"], p["journal"], p["year"]])
    out.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"picked {len(picked)} -> {out}")
    print("with_pii", sum(1 for p in payload if p["pii"]))


if __name__ == "__main__":
    main()
