"""逐条用 Crossref 核对「数据库 DOI 指向的文章标题」是否等于这条记录的标题。

PubMed 偶尔把 DOI 关联到同一期的另一篇文章，重下时拿这个 DOI 只会再下错，
所以重下前先跑一遍。只读，结果写 data/output/failures/doi_metadata_*.csv。

    python scripts/check_doi_metadata.py --limit 200   # 抽样
    python scripts/check_doi_metadata.py               # 全库，8 线程约 40 分钟

断点：命中/不匹配的结果写 data/output/cache/doi_metadata_check.json，重跑跳过。
"""

import argparse
import csv
import difflib
import json
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import DB_PATH, OUTPUT_DIR, SUBDIR_FAILURE
from downloader.fulltext_library import logger

CACHE = Path(OUTPUT_DIR) / "cache" / "doi_metadata_check.json"
UA = {"User-Agent": "potato-lit/1.0 (mailto:potato-lit@proton.me)"}
MISMATCH_BELOW = 0.6


def norm(text: str) -> str:
    # Crossref 标题带 <i>/<scp> 标签，<scp> 还会把词拆成 "O RGANIZATION"
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"[^a-z0-9]", "", text.lower())


def similarity(db_title: str, cr_title: str) -> float:
    return difflib.SequenceMatcher(None, norm(db_title), norm(cr_title)).ratio()


def categorize(db_title: str, cr_title: str) -> str:
    """把「标题对不上」拆成可解释的几类，只有 other 才是真可疑。"""
    db, cr = norm(db_title), norm(cr_title)
    if not cr:
        return "no_title"
    if cr in db or db in cr:
        return "truncated"  # PubMed 去掉副标题，Crossref 只存主标题
    if (db_title or "").strip().startswith("["):
        return "translated"  # 原文外文，PubMed 标题是英译
    main = norm(re.split(r":|\.\s", db_title or "", maxsplit=1)[0])
    if (main and main in cr) or difflib.SequenceMatcher(None, main, cr).ratio() >= 0.8:
        return "subtitle"
    return "other"


def lookup(session: requests.Session, doi: str) -> tuple[str, str]:
    """返回 (crossref 标题, 状态)。"""
    for attempt in range(3):
        try:
            r = session.get(f"https://api.crossref.org/works/{doi}", headers=UA, timeout=30)
        except Exception as exc:
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 404:
            return "", "not_in_crossref"
        if r.status_code == 200:
            return (r.json()["message"].get("title") or [""])[0], "ok"
        time.sleep(2 ** attempt)
    return "", "error"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT pmid, doi, title FROM articles WHERE doi IS NOT NULL AND doi<>''"
        ).fetchall()
    if args.limit:
        rows = rows[:args.limit]

    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    todo = [r for r in rows if r[0] not in cache]
    logger.info(f"待核对 DOI {len(todo)} 条（已缓存 {len(cache)}）")

    done = [0]
    lock = threading.Lock()
    local = threading.local()

    def work(row):
        pmid, doi, title = row
        if not hasattr(local, "session"):
            local.session = requests.Session()
        cr_title, status = lookup(local.session, doi)
        sim = round(similarity(title, cr_title), 3) if status == "ok" else 0.0
        with lock:
            cache[pmid] = {"doi": doi, "crossref_title": cr_title, "sim": sim, "status": status}
            done[0] += 1
            if done[0] % 500 == 0:
                CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
                logger.info(f"进度 {done[0]}/{len(todo)}")

    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(work, todo))
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    out_csv = Path(OUTPUT_DIR) / SUBDIR_FAILURE / f"doi_metadata_{datetime.now():%Y%m%d_%H%M%S}.csv"
    titles = {r[0]: r[2] for r in rows}
    flagged = []
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as fh:
        fields = ["pmid", "doi", "status", "category", "sim", "db_title", "crossref_title"]
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for pmid, item in cache.items():
            sim = similarity(titles.get(pmid, ""), item["crossref_title"])
            category = categorize(titles.get(pmid, ""), item["crossref_title"])
            if item["status"] == "ok" and sim >= MISMATCH_BELOW:
                continue
            if item["status"] == "ok" and category not in ("other", "no_title"):
                continue
            flagged.append(pmid)
            writer.writerow({
                "pmid": pmid, "db_title": titles.get(pmid, ""), **item,
                "sim": round(sim, 3), "category": category,
            })

    logger.info(f"核对 {len(cache)} 条，可疑 {len(flagged)} 条 -> {out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
