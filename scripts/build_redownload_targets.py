"""从体检表重建两份交接清单：重下目标 + 接收方清理。

    python scripts/build_redownload_targets.py
    python scripts/build_redownload_targets.py --cutoff "2026-11-03 22:22"

`redownload_targets.csv` 只收「重下可能修好」的篇目：无 DOI、扫描正文
（`scanned_page*`，OCR 才有文本）、`non_english`（原文就是外文）全部排除。
`recipient_cleanup.csv` 再按文件创建时间过滤出已发给接收方的那部分。
"""

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from export_fulltext_review import latest, load_rows

from config.settings import DB_PATH, OUTPUT_DIR, PDF_DIR, SUBDIR_FAILURE
from downloader.fulltext_library import load_records, logger

PRIORITY = {
    "broken": 0, "front_page": 0, "supplement": 0,
    "placeholder": 1, "unparsed": 1,
    "no_text": 2,
    "mismatch": 3,
    "too_short": 4,
}
# 重下修不了的判定：扫描件有正文只是没文本层；外文原文本来就不是英文；
# weak 是标题覆盖率 0.6-0.85 的启发式结果（LLM 复核 330/334 判 MATCH），交人工不重下
SKIP_VERDICTS = {"ok", "weak", "non_english"}
SKIP_REASONS = {"scanned_page_watermark", "scanned_page", "scanned_only", "text_non_latin"}
TARGET_FIELDS = ["priority", "verdict", "pmid", "doi", "title", "reason", "current_file", "doi_status"]
CLEANUP_FIELDS = ["pmid", "doi", "title", "action", "verdict", "reason", "filename", "file_ctime"]
ACTION = {"mismatch": "replace", "too_short": "replace"}
EXCLUDED = Path(OUTPUT_DIR) / "cache" / "fulltext_excluded.json"
EXCLUDED_ROOT = Path(PDF_DIR) / "_excluded_notices"
REPLACED = Path(PDF_DIR) / "_replaced_backup"


def action_for(verdict: str) -> str:
    return ACTION.get(verdict, "delete")


def doi_flags() -> dict[str, str]:
    """check_doi_metadata.py 的报告，只含可疑项；没出现即视为正常。"""
    reports = sorted((Path(OUTPUT_DIR) / SUBDIR_FAILURE).glob("doi_metadata_*.csv"))
    if not reports:
        return {}
    with open(reports[-1], encoding="utf-8-sig") as fh:
        return {
            r["pmid"]: "title_mismatch" if r["status"] == "ok" else r["status"]
            for r in csv.DictReader(fh)
        }


def select_targets(rows: list[dict], records: dict[str, dict], flags: dict[str, str] | None = None) -> list[dict]:
    flags = flags or {}
    targets = []
    for row in rows:
        verdict = row["verdict"]
        if verdict in SKIP_VERDICTS or row.get("reason") in SKIP_REASONS:
            continue
        record = records.get(row["pmid"], {})
        if not record.get("doi"):
            continue
        targets.append({
            "priority": PRIORITY.get(verdict, 9),
            "verdict": verdict,
            "pmid": row["pmid"],
            "doi": record["doi"],
            "title": record.get("title", ""),
            "reason": row.get("reason", ""),
            "current_file": row["file"],
            "doi_status": flags.get(row["pmid"], "ok"),
        })
    targets.sort(key=lambda t: (t["priority"], t["pmid"]))
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, help="体检表（默认最新非 mineru 表）")
    parser.add_argument("--cutoff", default="2026-11-03 22:22", help="已发送文件的创建时间上限")
    args = parser.parse_args()

    main_csv = args.csv or latest("fulltext_integrity_*.csv", exclude="mineru")
    # 与核对目录同口径：unparsed / no_text 以 MinerU 复核结果为准
    rows = list(load_rows(main_csv, latest("fulltext_integrity_mineru_final_*.csv")).values())
    # 已被 exclude_notice_fulltext.py 移出全文库的，不再进重下队列
    rows = [r for r in rows if (Path(PDF_DIR) / r["file"]).exists()]
    records = load_records(DB_PATH)
    targets = select_targets(rows, records, doi_flags())

    handoff = Path(OUTPUT_DIR) / "handoff"
    handoff.mkdir(parents=True, exist_ok=True)
    with open(handoff / "redownload_targets.csv", "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=TARGET_FIELDS)
        writer.writeheader()
        writer.writerows(targets)

    cutoff = datetime.strptime(args.cutoff, "%Y-%m-%d %H:%M")
    cleanup = []
    for t in targets:
        ctime = datetime.fromtimestamp((Path(PDF_DIR) / t["current_file"]).stat().st_ctime)
        if ctime >= cutoff:
            continue
        cleanup.append({
            "pmid": t["pmid"], "doi": t["doi"], "title": t["title"],
            "action": action_for(t["verdict"]), "verdict": t["verdict"],
            "reason": t["reason"], "filename": t["current_file"],
            "file_ctime": ctime.strftime("%Y-%m-%d %H:%M"),
        })

    # 撤稿/更正移出的文件同样要通知接收方删（它们不在重下队列里）
    if EXCLUDED.exists():
        for pmid, item in json.loads(EXCLUDED.read_text(encoding="utf-8")).items():
            path = next(EXCLUDED_ROOT.rglob(item["file"]), None)
            if path is None:
                continue
            ctime = datetime.fromtimestamp(path.stat().st_ctime)
            if ctime >= cutoff:
                continue
            record = records.get(pmid, {})
            cleanup.append({
                "pmid": pmid, "doi": record.get("doi", ""), "title": record.get("title", ""),
                "action": "delete", "verdict": "excluded", "reason": item["reason"],
                "filename": item["file"], "file_ctime": ctime.strftime("%Y-%m-%d %H:%M"),
            })

    # 我们替换覆盖过的：备份目录里有旧版、库里已经是新版
    if REPLACED.exists():
        for old in REPLACED.iterdir():
            if not (Path(PDF_DIR) / old.name).exists():
                continue
            ctime = datetime.fromtimestamp(old.stat().st_ctime)
            if ctime >= cutoff:
                continue
            record = records.get(old.stem, {})
            cleanup.append({
                "pmid": old.stem, "doi": record.get("doi", ""), "title": record.get("title", ""),
                "action": "replace", "verdict": "replaced", "reason": "fulltext_replaced",
                "filename": old.name, "file_ctime": ctime.strftime("%Y-%m-%d %H:%M"),
            })
    with open(handoff / "recipient_cleanup.csv", "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CLEANUP_FIELDS)
        writer.writeheader()
        writer.writerows(cleanup)

    logger.info(f"体检表 {main_csv.name}")
    for verdict, count in sorted(
        ((v, sum(1 for t in targets if t["verdict"] == v)) for v in {t["verdict"] for t in targets}),
        key=lambda kv: PRIORITY.get(kv[0], 9),
    ):
        logger.info(f"  重下 {verdict:<12}{count:>4}")
    logger.info(f"重下目标 {len(targets)} 篇 -> {handoff / 'redownload_targets.csv'}")
    logger.info(f"接收方清理 {len(cleanup)} 篇（{args.cutoff} 前创建）-> {handoff / 'recipient_cleanup.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
