"""按文件系统实况重算「相关文献仍缺全文」清单与「本轮已补回」清单。

基准是冻结的目标清单（默认 missing_relevant_current.csv，5489 篇），
只用 downloader/pdf_downloader._existing_fulltext_path 判断本地是否已有全文，
不请求网络、不改数据库。

  python scripts/recount_missing_fulltext.py                 # 用默认基准
  python scripts/recount_missing_fulltext.py --base <csv>    # 指定别的基准
"""

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import SUBDIR_EXPORT_FULL  # noqa: E402
from downloader.pdf_downloader import _existing_fulltext_path, _output_dir  # noqa: E402

DEFAULT_BASE = "missing_relevant_current.csv"
FIELDS = ["pmid", "doi", "pmc_id", "title", "journal", "year", "authors"]


def main() -> int:
    ap = argparse.ArgumentParser(description="重算缺失全文清单")
    ap.add_argument("--base", default=DEFAULT_BASE, help="基准 CSV（含 pmid 列的冻结清单）")
    args = ap.parse_args()

    out_dir = _output_dir(SUBDIR_EXPORT_FULL)
    base_path = Path(args.base)
    if not base_path.is_absolute():
        base_path = out_dir / base_path

    with open(base_path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    stamp = datetime.now().strftime("%Y%m%d")
    missing, recovered = [], []
    for row in rows:
        pmid = str(row.get("pmid", "")).strip()
        if not pmid:
            continue
        path = _existing_fulltext_path(pmid)
        if path:
            recovered.append({**{k: row.get(k, "") for k in FIELDS}, "file": path.name, "bytes": path.stat().st_size})
        else:
            missing.append(row)

    missing_csv = out_dir / f"missing_relevant_remaining_{stamp}.csv"
    recovered_csv = out_dir / f"recovered_fulltext_{stamp}.csv"
    for path, data, fields in (
        (missing_csv, missing, FIELDS),
        (recovered_csv, recovered, FIELDS + ["file", "bytes"]),
    ):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(data)
        print(f"{path}: {len(data)} 行")

    print(f"基准 {len(rows)} 篇 → 仍缺 {len(missing)}，已补回 {len(recovered)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
