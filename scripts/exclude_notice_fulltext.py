"""把撤稿原文、撤稿声明、以及原文不在库的孤立勘误移出全文库。

规则（2026-11-16 用户定）：
- `Retracted Publication`（被撤稿的原文）→ 移出
- `Retraction Notice`（撤稿声明）→ 移出，它指向的原文同时被撤
- `Corrected and Republished` → 保留新版；旧版和更正消息移出（旧版不在库时不动作）
- `Published Erratum` → 原文 PDF 也在库才保留，否则这份勘误单独存在没有意义

文件移到 `PDF_DIR/_excluded_notices/<原因>/`（不删除，可回滚），名单写
`data/output/cache/fulltext_excluded.json`，`relevant_pmids` 据此不再重抓。
默认 dry-run，`--apply` 才动文件。
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from config.settings import DB_PATH, OUTPUT_DIR, PDF_DIR
from downloader.fulltext_library import FULLTEXT_SUFFIXES, load_records, logger
from utils.db import get_conn

CACHE = Path(OUTPUT_DIR) / "cache" / "notice_update_to.json"
EXCLUDED = Path(OUTPUT_DIR) / "cache" / "fulltext_excluded.json"
DEST_ROOT = Path(PDF_DIR) / "_excluded_notices"


def existing_path(pmid: str) -> Path | None:
    return next((Path(PDF_DIR) / f"{pmid}{s}" for s in FULLTEXT_SUFFIXES
                 if (Path(PDF_DIR) / f"{pmid}{s}").exists()), None)


def reason_for(article_types: str, pmid: str, original: str) -> str:
    """返回移出原因；空串表示保留。`original` 是 erratum 指向的原文 PMID。"""
    types = article_types or ""
    if "Retracted Publication" in types:
        return "retracted_original"
    if "Retraction Notice" in types:
        return "retraction_notice"
    if "Published Erratum" in types and not (original and existing_path(original)):
        return "orphan_erratum"
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真正移动文件并写名单")
    parser.add_argument("--pmids", help="显式指定 PMID（逗号分隔），跳过规则判定")
    parser.add_argument("--reason", default="unrecoverable", help="配合 --pmids 的原因标签")
    args = parser.parse_args()

    up_to = {r["pmid"]: r for r in json.loads(CACHE.read_text(encoding="utf-8"))} if CACHE.exists() else {}
    records = load_records(DB_PATH)
    moves = []
    explicit = [p.strip() for p in (args.pmids or "").split(",") if p.strip()]
    for pmid, rec in records.items():
        path = existing_path(pmid)
        if not path:
            continue
        if explicit:
            if pmid not in explicit:
                continue
            reason = args.reason
        else:
            original = up_to.get(pmid, {}).get("original_pmid", "")
            reason = reason_for(rec.get("article_types", ""), pmid, original)
        if reason:
            moves.append({"pmid": pmid, "reason": reason, "file": path.name,
                          "types": rec.get("article_types", ""), "title": rec.get("title", "")})

    for m in moves:
        logger.info(f"  {m['reason']:<18}{m['pmid']}  {m['file']:<14}{m['title'][:60]}")
    logger.info(f"待移出 {len(moves)} 篇（{'执行' if args.apply else 'dry-run'}）")
    if not args.apply:
        return 0

    excluded = json.loads(EXCLUDED.read_text(encoding="utf-8")) if EXCLUDED.exists() else {}
    moved_at = datetime.now().isoformat(timespec="seconds")
    for m in moves:
        dest_dir = DEST_ROOT / m["reason"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        (Path(PDF_DIR) / m["file"]).replace(dest_dir / m["file"])
        excluded[m["pmid"]] = {**m, "moved_at": moved_at}
    EXCLUDED.write_text(json.dumps(excluded, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info(f"已移出 {len(moves)} 篇 -> {DEST_ROOT}；名单 {EXCLUDED.name} 共 {len(excluded)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
