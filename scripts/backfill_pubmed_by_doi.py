"""按 DOI 反查补录 PubMed 已收录、库里没有的文献。

来源是 potato-ai.top 覆盖盘点里「不在库、但 PubMed 有」的那批（暂存清单 in_pubmed=Y）。
复用现有下载/解析/硬过滤，不新增入库逻辑；这批属于站点文献，跳过 LLM 验证，
直接写一条 RELEVANT 标记（reason 里注明来源），否则导不出、也下不到全文。

用法：
    python scripts/backfill_pubmed_by_doi.py                 # dry-run，只报告
    python scripts/backfill_pubmed_by_doi.py --apply
"""

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cleaner.hard_filter import run_incremental_hard_filter
from config.settings import DB_PATH, RAW_XML_DIR
from downloader.pubmed_downloader import (
    _download_single_batch,
    _next_batch_index,
)
from parser.xml_parser import parse_xml_file
from scripts.import_site_literature import latest_harvest
from utils.db import get_conn, init_db
from utils import now_iso

MARK_REASON = "potato_ai_site:skip_llm"
def pending_pmids(csv_path: Path, db_path: Path) -> list[str]:
    """暂存清单里 PubMed 命中的 PMID，去掉库里已有的。"""
    with csv_path.open(encoding="utf-8-sig") as fh:
        found = [r["pmid"] for r in csv.DictReader(fh) if r["in_pubmed"] == "Y" and r["pmid"]]
    with get_conn(db_path) as conn:
        existing = {r[0] for r in conn.execute("SELECT pmid FROM articles")}
    return sorted({p for p in found if p not in existing})


def mark_relevant(pmids: list[str], db_path: Path) -> None:
    stamp = now_iso()
    with get_conn(db_path) as conn:
        # 摘要为空的会被硬过滤转 needs_review；站点文献按约定直接入库，统一改回通过
        conn.executemany(
            "INSERT OR REPLACE INTO filter_log (pmid, stage, reason, filtered_at) "
            "VALUES (?, 'hard_filter_pass', 'potato_ai_import', ?)",
            [(pmid, stamp) for pmid in pmids],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO llm_validation "
            "(pmid, llm_verdict, reason, validated_at, human_review, research_field) "
            "VALUES (?, 'RELEVANT', ?, ?, NULL, '其他')",
            [(pmid, MARK_REASON, stamp) for pmid in pmids],
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="", help="暂存清单，省略用最新一份")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    csv_path = Path(args.csv) if args.csv else latest_harvest()
    pmids = pending_pmids(csv_path, Path(DB_PATH))
    print(f"PubMed 反查命中但库里没有：{len(pmids)} 篇")
    if not pmids or not args.apply:
        print("dry-run，未写库" if pmids else "无需补录")
        return

    init_db(Path(DB_PATH))
    out_dir = Path(RAW_XML_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    imported = 0
    for i in range(0, len(pmids), 200):
        chunk = pmids[i:i + 200]
        batch = _download_single_batch(
            _next_batch_index(out_dir), chunk, out_dir, 200, 1
        )
        if not batch:
            continue
        imported += parse_xml_file(batch, db_path=Path(DB_PATH))[0]

    run_incremental_hard_filter(Path(DB_PATH))
    mark_relevant(pmids, Path(DB_PATH))
    print(f"入库 {imported} 篇，已标记 RELEVANT（{MARK_REASON}）")


if __name__ == "__main__":
    main()
