"""把 potato-ai.top 文献清单里 PubMed 不收的条目直接入库（跳过 LLM 验证）。

输入是 `scripts/harvest_missing_metadata.py` 产出的暂存清单。站点侧已人工筛过，
按用户要求不再送 LLM：直接写 articles + filter_log(hard_filter_pass) +
llm_validation(RELEVANT, reason 标注来源)，保证能进导出和全文下载队列。

ID 方案：`x` + DOI 的 sha1 前 12 位。DOI 自带 `/`，直接当文件名会碎在目录上，
所以文件名走 `import_missing_fulltext.doi_to_stem` 的净化规则。

用法：
    python scripts/import_site_literature.py                 # dry-run
    python scripts/import_site_literature.py --apply
"""

import argparse
import csv
import hashlib
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import DB_PATH, OUTPUT_DIR
from utils import now_iso
from utils.db import get_conn, init_db

SOURCE = "potato_ai"
MARK_REASON = "potato_ai_site:skip_llm"


def external_id(doi: str) -> str:
    return "x" + hashlib.sha1(doi.strip().lower().encode()).hexdigest()[:12]


def latest_harvest() -> Path:
    files = sorted((Path(OUTPUT_DIR) / "handoff").glob("missing_external_metadata_*.csv"))
    if not files:
        raise SystemExit("找不到 missing_external_metadata_*.csv，先跑 harvest_missing_metadata.py")
    return files[-1]


def load_rows(csv_path: Path) -> list[dict]:
    with csv_path.open(encoding="utf-8-sig") as fh:
        rows = [r for r in csv.DictReader(fh) if r["in_pubmed"] == "N"]
    uniq, seen = [], set()
    for row in rows:
        doi = row["doi"].strip().lower()
        if doi and doi not in seen:
            seen.add(doi)
            uniq.append(row | {"doi": doi})
    return uniq


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    csv_path = Path(args.csv) if args.csv else latest_harvest()
    rows = load_rows(csv_path)
    db_path = Path(DB_PATH)
    with get_conn(db_path) as conn:
        existing_ids = {r[0] for r in conn.execute("SELECT pmid FROM articles")}
        existing_dois = {r[0].lower() for r in conn.execute(
            "SELECT doi FROM articles WHERE doi IS NOT NULL AND doi != ''")}
    todo = [r for r in rows
            if external_id(r["doi"]) not in existing_ids and r["doi"] not in existing_dois]
    print(f"暂存 {len(rows)}，已入库/重复跳过 {len(rows) - len(todo)}，待导入 {len(todo)}")
    if not args.apply:
        print("dry-run，未写库")
        return

    init_db(db_path)
    stamp = now_iso()
    with get_conn(db_path) as conn:
        for row in todo:
            pmid = external_id(row["doi"])
            conn.execute(
                "INSERT OR IGNORE INTO articles "
                "(pmid, title, abstract, keywords, mesh_terms, pub_year, pub_month, "
                " journal, journal_abbr, doi, pmc_id, article_types, authors, "
                " affiliation, language, raw_xml_file, source) "
                "VALUES (?, ?, ?, '', '', ?, '', ?, '', ?, '', '', '', '', '', NULL, ?)",
                (pmid, row["title"], row["abstract"],
                 int(row["pub_year"]) if row["pub_year"].isdigit() else None,
                 row["journal"], row["doi"], SOURCE),
            )
            conn.execute(
                "INSERT OR REPLACE INTO filter_log (pmid, stage, reason, filtered_at) "
                "VALUES (?, 'hard_filter_pass', 'potato_ai_import', ?)",
                (pmid, stamp),
            )
            conn.execute(
                "INSERT OR IGNORE INTO llm_validation "
                "(pmid, llm_verdict, reason, validated_at, human_review, research_field) "
                "VALUES (?, 'RELEVANT', ?, ?, NULL, '其他')",
                (pmid, MARK_REASON, stamp),
            )
    print(f"导入 {len(todo)} 篇（source={SOURCE}，reason={MARK_REASON}）")


if __name__ == "__main__":
    main()
