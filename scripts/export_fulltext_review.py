"""把非 ok 的全文按判定分目录建硬链接，供人工核对。只建链接，不动原文件。

    python scripts/export_fulltext_review.py          # data/pdf_review/<verdict>/
    python scripts/export_fulltext_review.py --clean  # 先清空分类目录再建

默认合并最新一份体检表、MinerU 复核表和 LLM 独立复核表：`unparsed` / `no_text` 以
MinerU 结果为准，`weak` 以 LLM `MATCH` 为准，判 ok 的从核对清单里移除。
扫描正文（`reason` 以 `scanned_` 开头）单独归入 `scanned_needs_ocr/`，不算待核对。
每个分类目录内附 `_review.csv`。
"""

import argparse
import csv
import os
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import DATA_DIR, DB_PATH, OUTPUT_DIR, PDF_DIR, SUBDIR_FAILURE
from downloader.fulltext_library import load_records, logger

REVIEW_FIELDS = [
    "pmid", "file", "verdict", "reason", "title_cov", "ngram_cov",
    "chars", "db_title", "db_doi",
]
MERGED_VERDICTS = ("unparsed", "no_text")
SCANNED_PREFIX = "scanned_"
OCR_DIR = "scanned_needs_ocr"


def _failure_dir() -> Path:
    return Path(OUTPUT_DIR) / SUBDIR_FAILURE


def latest(pattern: str, exclude: str = "") -> Path | None:
    files = [p for p in _failure_dir().glob(pattern) if not exclude or exclude not in p.name]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def load_rows(
    main_csv: Path, mineru_csv: Path | None, llm_csv: Path | None = None
) -> dict[str, dict]:
    rows = {r["pmid"]: r for r in csv.DictReader(open(main_csv, encoding="utf-8-sig"))}
    if mineru_csv:
        for row in csv.DictReader(open(mineru_csv, encoding="utf-8-sig")):
            pmid = row["pmid"]
            if pmid in rows and rows[pmid]["verdict"] in MERGED_VERDICTS:
                rows[pmid] = row
    if llm_csv:
        for row in csv.DictReader(open(llm_csv, encoding="utf-8-sig")):
            pmid = row["pmid"]
            if pmid in rows and rows[pmid]["verdict"] == "weak" and row.get("identity") == "MATCH":
                rows[pmid] = {**rows[pmid], "verdict": "ok", "reason": "llm_match"}
    return rows


def link_or_copy(src: Path, dest: Path) -> None:
    if dest.exists():
        return
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, help="主体检 CSV（默认最新非 mineru 表）")
    parser.add_argument("--mineru-csv", type=Path, help="MinerU 复核表（默认最新）")
    parser.add_argument("--llm-csv", type=Path, help="LLM 复核表（默认最新）")
    parser.add_argument("--dest", type=Path, default=Path(DATA_DIR) / "pdf_review")
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()

    main_csv = args.csv or latest("fulltext_integrity_*.csv", exclude="mineru")
    if main_csv is None:
        raise SystemExit("未找到 fulltext_integrity_*.csv")
    mineru_csv = args.mineru_csv or latest("fulltext_integrity_mineru_final_*.csv")
    llm_csv = args.llm_csv or latest("fulltext_llm_check_*.csv")

    rows = load_rows(main_csv, mineru_csv, llm_csv)
    todo = [r for r in rows.values() if r["verdict"] != "ok"]
    records = load_records(DB_PATH)
    logger.info(
        f"主体检 {main_csv.name}，MinerU {mineru_csv.name if mineru_csv else '无'}，"
        f"LLM {llm_csv.name if llm_csv else '无'}，待核对 {len(todo)} 篇"
    )

    if args.clean and args.dest.exists():
        shutil.rmtree(args.dest)
    args.dest.mkdir(parents=True, exist_ok=True)

    grouped: dict[str, list[dict]] = {}
    missing = 0
    for row in todo:
        reason = row.get("reason", "")
        # 扫描正文是有效文件，只是没有文本层，单列一档等 OCR，不混进待核对
        verdict = OCR_DIR if reason.startswith(SCANNED_PREFIX) else row["verdict"]
        src = Path(PDF_DIR) / row["file"]
        if not src.exists():
            missing += 1
            continue
        group_dir = args.dest / verdict
        group_dir.mkdir(parents=True, exist_ok=True)
        link_or_copy(src, group_dir / row["file"])
        record = records.get(row["pmid"], {})
        grouped.setdefault(verdict, []).append({
            **{field: row.get(field, "") for field in REVIEW_FIELDS},
            "db_title": record.get("title", ""),
            "db_doi": record.get("doi", ""),
        })

    index = []
    for verdict, items in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        path = args.dest / verdict / "_review.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=REVIEW_FIELDS)
            writer.writeheader()
            writer.writerows(items)
        note = "扫描正文，文件有效，仅需 OCR 才能检索" if verdict == OCR_DIR else ""
        index.append({"verdict": verdict, "count": len(items), "dir": str(args.dest / verdict), "note": note})
        logger.info(f"  {verdict:<12}{len(items):>6} 篇 -> {args.dest / verdict}")

    with open(args.dest / "review_index.csv", "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=["verdict", "count", "dir", "note"])
        writer.writeheader()
        writer.writerows(index)

    if missing:
        logger.info(f"  {missing} 篇原文件不存在，未建链接")
    logger.info(f"索引: {args.dest / 'review_index.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
