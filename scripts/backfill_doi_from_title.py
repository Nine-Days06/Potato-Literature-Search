"""把 title_to_doi.py 反查到的 DOI 写回 articles.doi，并在 doi_backfill 留溯源记录。

默认 dry-run；加 --apply 才写库。只填空 DOI，不覆盖 PubMed 原生 DOI。
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import DB_PATH
from utils.db import init_db

CACHE = ROOT / "data/output/cache"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-sim", type=float, default=1.0)  # 只保留完全一致的标题
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    resolved = json.loads((CACHE / "nodoi_resolved.json").read_text(encoding="utf-8"))
    picks = {p: v for p, v in resolved.items()
             if v.get("doi") and (v.get("sim") or 0) >= args.min_sim}

    init_db(DB_PATH)  # 补 doi_backfill 表
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = {r[0]: r[1] for r in conn.execute("SELECT pmid, doi FROM articles")}
        todo = {p: v for p, v in picks.items() if p in rows and not (rows[p] or "").strip()}
        print(f"反查命中 {len(picks)}，可写入 {len(todo)}（其余已有 DOI 或不在库）")

        by_pub: dict[str, int] = {}
        for v in todo.values():
            key = v["doi"].split("/")[0]
            by_pub[key] = by_pub.get(key, 0) + 1
        for k, n in sorted(by_pub.items(), key=lambda x: -x[1]):
            print(f"  {n:3d}  {k}")

        if not args.apply:
            print("dry-run，未写库；加 --apply 执行")
            return

        ts = datetime.now().isoformat(timespec="seconds")
        backup = CACHE / f"doi_backfill_backup_{datetime.now():%Y%m%d_%H%M%S}.json"
        backup.write_text(json.dumps({p: rows[p] for p in todo}, ensure_ascii=False, indent=1),
                          encoding="utf-8")
        for p, v in todo.items():
            conn.execute(
                "INSERT OR REPLACE INTO doi_backfill"
                "(pmid, doi, sim, matched_title, matched_year, source, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (p, v["doi"], v.get("sim"), v.get("title"), v.get("year"),
                 "crossref:query.bibliographic", ts),
            )
            conn.execute("UPDATE articles SET doi=? WHERE pmid=? AND (doi IS NULL OR doi='')",
                         (v["doi"], p))
        conn.commit()
        print(f"已写入 {len(todo)} 条；备份 {backup}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
