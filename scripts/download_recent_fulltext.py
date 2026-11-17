"""只补最近一批复核通过文献的全文，不重试全库。

    python scripts/download_recent_fulltext.py --since 2026-11-17T20:00:00
    python scripts/download_recent_fulltext.py      # 默认从最后一次验证前 1 小时起
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import DB_PATH
from downloader.pdf_downloader import run_pdf_download
from utils.db import get_conn


def _default_since() -> str:
    with get_conn(DB_PATH) as conn:
        latest = conn.execute("SELECT MAX(validated_at) FROM llm_validation").fetchone()[0]
    if not latest:
        return "1970-01-01T00:00:00"
    return (datetime.fromisoformat(latest) - timedelta(hours=1)).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default=None, help="只处理该时间（ISO）之后通过验证的 PMID")
    parser.add_argument("--pmids", default="", help="逗号分隔，只补这些 PMID（优先于 --since）")
    args = parser.parse_args()

    if args.pmids:
        pmids = {p.strip() for p in args.pmids.split(",") if p.strip()}
        print(f"指定 PMID {len(pmids)} 篇")
    else:
        since = args.since or _default_since()
        with get_conn(DB_PATH) as conn:
            rows = conn.execute(
                """
                SELECT pmid FROM llm_validation
                WHERE validated_at >= ?
                  AND (human_review = 'Y' OR (human_review IS NULL AND llm_verdict = 'RELEVANT'))
                """,
                (since,),
            ).fetchall()
        pmids = {r["pmid"] for r in rows}
        print(f"since={since} 复核通过 {len(pmids)} 篇")
    run_pdf_download(db_path=DB_PATH, pmids=pmids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
