"""用 MinerU 复核 pypdf 解析不了的全文（字体乱码、AES 加密、扫描件）。

读最新一份 fulltext_integrity_*.csv，挑出 unparsed / no_text 的文献，
批量交给 mineru-kit 解析前 2 页，再用同一套判据重新判定。只读，不删文件。

    python scripts/recheck_with_mineru.py --limit 5     # 先试 5 篇
    python scripts/recheck_with_mineru.py               # 全量 283 篇，约 15 分钟

必须用 --ocr-mode ocr：auto 模式对「有文本层但只有水印」的扫描件会读文本层就停，
实测 NEJM 那类文件会只抽到版权水印。

默认 --ocr-mode auto-split：unparsed 组（有文本层，只是字体/加密问题）走 auto，
no_text 组（扫描件）走 ocr。两组分别批处理，比全量 OCR 快一半以上。
"""

import argparse
import csv
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import DB_PATH, OUTPUT_DIR, PDF_DIR, SUBDIR_FAILURE
from downloader.fulltext_library import (
    INTEGRITY_FIELDS,
    judge_fulltext,
    load_records,
    logger,
)

DEFAULT_VERDICTS = ("unparsed", "no_text")


def _output_dir(subdir: str) -> Path:
    return Path(OUTPUT_DIR) / subdir


def latest_integrity_csv() -> Path:
    files = [
        p for p in _output_dir(SUBDIR_FAILURE).glob("fulltext_integrity_*.csv")
        if "mineru" not in p.name
    ]
    if not files:
        raise SystemExit("未找到 fulltext_integrity_*.csv，先跑 check_fulltext_integrity.py")
    return max(files, key=lambda p: p.stat().st_mtime)


def clean_markdown(text: str) -> str:
    """去掉 base64 图片和 HTML 标签，否则 OCR 页的 1.9MB base64 会污染字符统计。"""
    import re

    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return text


def stage_files(files: list[str], workdir: Path, source_dir: Path = Path(PDF_DIR)) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    for name in files:
        src = source_dir / name
        dest = workdir / name
        try:
            os.link(src, dest)
        except OSError:
            shutil.copy2(src, dest)


def run_mineru(workdir: Path, outdir: Path, pages: str, tier: str, ocr_mode: str) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    command = [
        shutil.which("mineru-kit") or "mineru-kit", "parse", str(workdir),
        "-o", str(outdir), "-p", pages, "--tier", tier, "--ocr-mode", ocr_mode,
    ]
    logger.info(f"MinerU: {' '.join(command)}")
    subprocess.run(command, check=True)


def mode_of(row: dict, requested: str) -> str:
    """auto-split 时按原判定选 OCR 模式；其余情况原样使用。"""
    if requested != "auto-split":
        return requested
    return "ocr" if row["verdict"] == "no_text" else "auto"


def recheck(rows: list[dict], source_dir: Path = Path(PDF_DIR), pages: str = "1-2",
            tier: str = "flash", ocr_mode: str = "auto-split",
            keep_workdir: bool = False) -> list[dict]:
    """对给定行重抽并重判，返回更新后的行；MinerU 未产出的行原样返回。"""
    if not rows:
        return []

    logger.info(f"待 MinerU 复核 {len(rows)} 篇")
    records = load_records(DB_PATH)
    workdir = Path(tempfile.mkdtemp(prefix="mineru_in_"))
    outdir = Path(tempfile.mkdtemp(prefix="mineru_out_"))
    started = time.time()

    try:
        groups: dict[str, list[dict]] = {}
        for row in rows:
            groups.setdefault(mode_of(row, ocr_mode), []).append(row)
        for mode, group in groups.items():
            logger.info(f"[{mode}] {len(group)} 篇")
            stage_files([r["file"] for r in group], workdir / mode, source_dir)
            run_mineru(workdir / mode, outdir / mode, pages, tier, mode)

        result = []
        missing = 0
        for row in rows:
            pmid = row["pmid"]
            md_path = next(outdir.rglob(f"{pmid}.md"), None)
            if md_path is None:
                missing += 1
                result.append(row)
                continue
            body = clean_markdown(md_path.read_text(encoding="utf-8", errors="replace"))
            record = records.get(pmid, {})
            judged = judge_fulltext(body, record, pages=0)
            updated = {field: row.get(field, "") for field in INTEGRITY_FIELDS}
            updated.update(judged)
            updated["pmid"], updated["file"] = pmid, row["file"]
            updated["suffix"], updated["size_kb"] = row.get("suffix", ""), row.get("size_kb", "")
            updated["dup_group"] = row.get("dup_group", "")
            result.append(updated)
    finally:
        if not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
            shutil.rmtree(outdir, ignore_errors=True)
        else:
            logger.info(f"中间文件保留在 {workdir} / {outdir}")

    before = Counter(r["verdict"] for r in rows)
    after = Counter(r["verdict"] for r in result)
    logger.info(f"复核完成 {len(result)} 篇，耗时 {time.time() - started:.0f}s")
    logger.info(f"  复核前: {dict(before)}")
    logger.info(f"  复核后: {dict(after)}")
    if missing:
        logger.info(f"  MinerU 未产出 {missing} 篇（保留原判定）")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, help="指定体检 CSV（默认取最新）")
    parser.add_argument("--verdicts", default=",".join(DEFAULT_VERDICTS))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--pages", default="1-2")
    parser.add_argument("--tier", default="flash")
    parser.add_argument("--ocr-mode", default="auto-split")
    parser.add_argument("--keep-workdir", action="store_true")
    args = parser.parse_args()

    source = args.csv or latest_integrity_csv()
    wanted = {v.strip() for v in args.verdicts.split(",") if v.strip()}
    with open(source, encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["verdict"] in wanted]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        logger.info(f"{source} 里没有 {wanted} 的条目")
        return 0

    logger.info(f"来源 {source}")
    result = recheck(rows, pages=args.pages, tier=args.tier,
                     ocr_mode=args.ocr_mode, keep_workdir=args.keep_workdir)

    out_csv = _output_dir(SUBDIR_FAILURE) / f"fulltext_integrity_mineru_{datetime.now():%Y%m%d_%H%M%S}.csv"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=INTEGRITY_FIELDS)
        writer.writeheader()
        writer.writerows(result)

    logger.info(f"结果: {out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
