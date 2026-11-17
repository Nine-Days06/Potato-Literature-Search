"""按体检结论清理全文：LLM 判 MATCH 的保留，其余删除并记入 fulltext_excluded。

默认 dry-run，列清单不删文件：

    python scripts/prune_bad_fulltext.py
    python scripts/prune_bad_fulltext.py --apply

合并三份结果：体检 CSV、MinerU 复核 CSV（覆盖 unparsed/no_text）、
所有 fulltext_llm_check_*.csv（同一 PMID 取最新一份）。weak 只有 LLM=MATCH 才留；
non_english 保留（外语原文仍可用，只影响自动抽取）。
"""

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import OUTPUT_DIR, PDF_DIR, SUBDIR_FAILURE


def _latest(pattern: str) -> Path | None:
    files = sorted((Path(OUTPUT_DIR) / SUBDIR_FAILURE).glob(pattern))
    return files[-1] if files else None


def _load(path: Path) -> list[dict]:
    return list(csv.DictReader(open(path, encoding="utf-8-sig")))


KEEP_VERDICTS = {"ok", "non_english"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--integrity", type=Path)
    parser.add_argument("--mineru", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    rows = _load(args.integrity or _latest("fulltext_integrity_2*.csv"))
    mineru = {r["pmid"]: r["verdict"] for r in _load(args.mineru or _latest("fulltext_integrity_mineru_*.csv"))}

    llm: dict[str, str] = {}
    for path in sorted((Path(OUTPUT_DIR) / SUBDIR_FAILURE).glob("fulltext_llm_check_*.csv")):
        for r in _load(path):
            if r.get("identity") and r["identity"] != "ERROR":
                llm[r["pmid"]] = r["identity"]

    verdict = {r["pmid"]: mineru.get(r["pmid"], r["verdict"]) for r in rows}
    keep = {p for p, v in verdict.items()
            if v in KEEP_VERDICTS or (v == "weak" and llm.get(p) == "MATCH")}
    drop = [r for r in rows if r["pmid"] not in keep]

    print(f"体检 {len(rows)} 篇，保留 {len(keep)}，删除 {len(drop)}")
    for reason in sorted({v for p, v in verdict.items() if p not in keep}):
        print(f"  {reason}: {sum(1 for r in drop if verdict[r['pmid']] == reason)}")
    for row in drop[:10]:
        print("   ", row["pmid"], verdict[row["pmid"]], row["file"])
    if not args.apply:
        print("dry-run，未删除（加 --apply 执行）")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = Path(OUTPUT_DIR) / SUBDIR_FAILURE / f"fulltext_deleted_{stamp}.csv"
    excluded_path = Path(OUTPUT_DIR) / "cache" / "fulltext_excluded.json"
    excluded = json.loads(excluded_path.read_text(encoding="utf-8")) if excluded_path.exists() else {}
    written = 0
    with open(log_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=["pmid", "file", "verdict", "reason", "size_kb"])
        writer.writeheader()
        for row in drop:
            path = Path(PDF_DIR) / row["file"]
            if not path.exists():
                continue
            size_kb = round(path.stat().st_size / 1024, 1)
            path.unlink()
            written += 1
            writer.writerow({"pmid": row["pmid"], "file": row["file"],
                             "verdict": verdict[row["pmid"]], "reason": row["reason"],
                             "size_kb": size_kb})
            excluded[row["pmid"]] = {
                "pmid": row["pmid"], "file": row["file"],
                "reason": f"integrity_{verdict[row['pmid']]}",
                "title": "", "types": "", "moved_at": datetime.now().isoformat(timespec="seconds"),
            }
    excluded_path.write_text(json.dumps(excluded, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已删除 {written} 篇，清单 {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
