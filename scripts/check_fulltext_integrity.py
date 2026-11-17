"""全文库完整性体检：文件是否损坏、内容是否完整、是否对应这个 PMID。

只读。默认把结果写成 CSV，不改任何文件：

    python scripts/check_fulltext_integrity.py --limit 200   # 先抽样看误报
    python scripts/check_fulltext_integrity.py               # 全库体检

加 --apply 才把 broken / placeholder / front_page 移到
data/pdfs_relevant/_quarantine/；文件消失后 --step pdf / pdf-retry 会自动重抓。
mismatch / too_short / no_text / weak / unparsed 一律只写 CSV，交人工判断。

unparsed / no_text 默认自动交 MinerU 重抽前 2 页再判一次（扫描件走 ocr、
其余走 auto），最终 CSV 里写的是重判后的结论；--no-mineru 可跳过。
"""

import argparse
import csv
import hashlib
import random
import shutil
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import DB_PATH, OUTPUT_DIR, PDF_DIR, SUBDIR_FAILURE
from downloader.fulltext_library import (
    HARD_FAIL_VERDICTS,
    INTEGRITY_FIELDS,
    _PMID_FILE_RE,
    inspect_fulltext,
    load_records,
    logger,
)

_RECORDS: dict[str, dict] = {}


def _output_dir(subdir: str) -> Path:
    return Path(OUTPUT_DIR) / subdir


def _init_worker(records: dict[str, dict]) -> None:
    global _RECORDS
    _RECORDS = records


def _inspect_worker(path_str: str) -> dict:
    path = Path(path_str)
    return inspect_fulltext(path, _RECORDS.get(path.stem))


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_duplicates(files: list[Path]) -> dict[str, str]:
    """只在文件大小相同的组内算 MD5（全库 52GB 但实际只读几百个文件）。"""
    by_size: dict[int, list[Path]] = defaultdict(list)
    for path in files:
        by_size[path.stat().st_size].append(path)

    groups: dict[str, str] = {}
    for same_size in by_size.values():
        if len(same_size) < 2:
            continue
        by_hash: dict[str, list[Path]] = defaultdict(list)
        for path in same_size:
            by_hash[_md5(path)].append(path)
        for same in by_hash.values():
            if len(same) > 1:
                members = ",".join(sorted(p.stem for p in same))
                for path in same:
                    groups[path.stem] = members
    return groups


def collect_files(directory: Path = Path(PDF_DIR), numeric_only: bool = True) -> list[Path]:
    return sorted(
        p for p in directory.iterdir()
        if p.is_file() and (not numeric_only or _PMID_FILE_RE.match(p.name))
        and p.suffix.lower() in (".pdf", ".xml", ".txt")
    )


def doi_records() -> dict[str, dict]:
    """DOI 命名目录（data/pdfs_nonpubmed）用 DOI 归一化后的文件名做身份比对。"""
    from scripts.import_missing_fulltext import doi_to_stem

    records = load_records()
    return {doi_to_stem(r["doi"]): r for r in records.values() if r["doi"]}


def write_csv(rows: list[dict]) -> Path:
    out_dir = _output_dir(SUBDIR_FAILURE)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"fulltext_integrity_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=INTEGRITY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return csv_path


def quarantine(rows: list[dict]) -> int:
    dest = Path(PDF_DIR) / "_quarantine"
    dest.mkdir(parents=True, exist_ok=True)
    moved = 0
    for row in rows:
        if row["verdict"] not in HARD_FAIL_VERDICTS:
            continue
        src = Path(PDF_DIR) / row["file"]
        if src.exists():
            shutil.move(str(src), str(dest / row["file"]))
            moved += 1
    return moved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="只抽检 N 篇（固定种子，可复现）")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--apply", action="store_true", help="隔离硬失败文件（默认只写 CSV）")
    parser.add_argument("--no-dup-scan", action="store_true", help="跳过同内容文件扫描")
    parser.add_argument("--dir", default=str(PDF_DIR), help="体检目录（默认相关全文库）")
    parser.add_argument("--pmids", default="", help="逗号分隔，只体检这些 PMID")
    parser.add_argument("--no-mineru", action="store_true", help="跳过 unparsed/no_text 的 MinerU 重判")
    args = parser.parse_args()

    directory = Path(args.dir)
    numeric_only = directory.resolve() == Path(PDF_DIR).resolve()
    files = collect_files(directory, numeric_only=numeric_only)
    if args.pmids:
        wanted = {p.strip() for p in args.pmids.split(",") if p.strip()}
        files = [p for p in files if p.stem in wanted]
    if args.limit and args.limit < len(files):
        files = random.Random(0).sample(files, args.limit)
    logger.info(f"待体检 {len(files)} 篇，全文库 {directory}")

    duplicates = {} if args.no_dup_scan else scan_duplicates(files)
    if duplicates:
        logger.info(f"发现 {len(set(duplicates.values()))} 组同内容文件（仅标记，不判定对错）")

    records = doi_records() if not numeric_only else load_records(DB_PATH)
    start = time.time()
    rows: list[dict] = []
    with ProcessPoolExecutor(
        max_workers=args.workers,
        initializer=_init_worker,
        initargs=(records,),
    ) as pool:
        for done, row in enumerate(pool.map(_inspect_worker, map(str, files), chunksize=20), 1):
            row["dup_group"] = duplicates.get(row["pmid"], "")
            rows.append(row)
            if done % 500 == 0:
                logger.info(f"进度 {done}/{len(files)}，{time.time() - start:.0f}s")

    if not args.no_mineru:
        retry = [row for row in rows if row["verdict"] in ("unparsed", "no_text")]
        if retry:
            try:
                from scripts.recheck_with_mineru import recheck

                logger.info(f"{len(retry)} 篇 unparsed/no_text 交 MinerU 重抽")
                fixed = {row["pmid"]: row for row in recheck(retry, source_dir=directory)}
                rows = [fixed.get(row["pmid"], row) for row in rows]
            except Exception as exc:
                logger.warning(f"MinerU 重判失败，保留原判定：{exc}")

    csv_path = write_csv(rows)
    counts = Counter(row["verdict"] for row in rows)
    logger.info(f"体检完成 {len(rows)} 篇，耗时 {time.time() - start:.0f}s：")
    for verdict, count in counts.most_common():
        logger.info(f"  {verdict:<12}{count:>7} 篇")
    logger.info(f"结果: {csv_path}")

    if args.apply:
        moved = quarantine(rows)
        logger.info(f"已隔离 {moved} 篇到 {Path(PDF_DIR) / '_quarantine'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
