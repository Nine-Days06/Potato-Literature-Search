"""按标题反查没有 DOI 的相关文献（Crossref），只写缓存文件，不动库。

阈值保守：标题相似度 ≥0.92 且年份相差 ≤1（任一方缺年份时放宽），避免把同主题的
不同文章配错。结果：data/output/cache/nodoi_resolved.json
"""
import difflib
import json
import re
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import DB_PATH
from downloader.fulltext_library import existing_fulltext_path, relevant_pmids

CACHE = ROOT / "data/output/cache"
OUT = CACHE / "nodoi_resolved.json"


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).strip()


def year_of(item: dict) -> int:
    for key in ("issued", "published-print", "published-online"):
        parts = (item.get(key) or {}).get("date-parts") or [[None]]
        if parts and parts[0] and parts[0][0]:
            return int(parts[0][0])
    return 0


def main() -> None:
    rel = relevant_pmids(DB_PATH)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT pmid, title, journal, pub_year FROM articles ORDER BY CAST(pmid AS INTEGER)"
        ).fetchall()
    targets = [(str(p), t or "", j or "", int(y or 0)) for p, t, j, y in rows
               if str(p) in rel and not existing_fulltext_path(str(p))
               and (t or "").strip()]

    # 只保留库里确实没有 DOI 的
    with closing(sqlite3.connect(DB_PATH)) as conn:
        doi_map = dict(conn.execute("SELECT pmid, doi FROM articles").fetchall())
    targets = [x for x in targets if not (doi_map.get(x[0]) or "").strip()]

    resolved = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    s = requests.Session()
    stats = {"total": len(targets), "hit": 0, "miss": 0, "skip": 0}
    for n, (pmid, title, journal, year) in enumerate(targets, 1):
        if pmid in resolved:
            stats["skip"] += 1
            continue
        query = f"{title} {journal}".strip()
        try:
            r = s.get("https://api.crossref.org/works",
                      params={"query.bibliographic": query, "rows": 5}, timeout=30)
            items = r.json()["message"]["items"] if r.status_code == 200 else []
        except Exception:
            items = []
        best = None
        for it in items:
            cand = (it.get("title") or [""])[0]
            sim = difflib.SequenceMatcher(None, norm(title), norm(cand)).ratio()
            cy = year_of(it)
            if year and cy and abs(cy - year) > 1:
                continue
            if not best or sim > best["sim"]:
                best = {"sim": round(sim, 3), "doi": it.get("DOI", ""), "title": cand,
                        "year": cy, "container": (it.get("container-title") or [""])[0]}
        if best and best["sim"] >= 0.92 and best["doi"]:
            resolved[pmid] = best
            stats["hit"] += 1
        else:
            resolved[pmid] = {"sim": best["sim"] if best else 0, "doi": ""}
            stats["miss"] += 1
        if n % 50 == 0:
            OUT.write_text(json.dumps(resolved, ensure_ascii=False, indent=1), encoding="utf-8")
            print(n, stats, flush=True)
        time.sleep(0.3)
    OUT.write_text(json.dumps(resolved, ensure_ascii=False, indent=1), encoding="utf-8")
    print(stats)


if __name__ == "__main__":
    main()
