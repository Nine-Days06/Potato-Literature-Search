"""把 ACS 手工下载的 PDF（文件名 = DOI 后缀）回填为 <pmid>.pdf。

默认 dry-run；加 --apply 才写入 data/pdfs_relevant：
    python scripts/import_acs_pdfs.py --source "D:\\桌面\\pubs.acs" --apply
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import DB_PATH, PDF_DIR
from downloader.pdf_downloader import _existing_fulltext_path
from downloader.fulltext_library import relevant_pmids
from export_missing_fulltext import normalize_doi

DOI_PREFIX = "10.1021/"
MIN_VALID_BYTES = 10 * 1024
_COPY_SUFFIX = re.compile(r"\s*\(\d+\)$")
# 现代 ACS 文件名丢掉期刊段（jf4c07881 → acs.jafc.4c07881），只能按尾部 token 认。
_DOI_TAIL = re.compile(r"(\d+c\d+|c\d+)$")


def load_doi_index(db_path: Path) -> tuple[dict[str, str], list[tuple[str, str]]]:
    relevant = relevant_pmids(db_path)
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        rows = [
            (str(pmid), (normalize_doi(doi) or "").lower())
            for pmid, doi in conn.execute("SELECT pmid, doi FROM articles WHERE doi LIKE '10.1021/%'")
            if doi and str(pmid) in relevant
        ]
    exact = {doi: pmid for pmid, doi in rows if doi}
    return exact, rows


def match_pmid(path: Path, exact: dict[str, str], rows: list[tuple[str, str]]) -> str | None:
    stem = _COPY_SUFFIX.sub("", path.stem).lower()
    direct = normalize_doi(f"{DOI_PREFIX}{stem}") or ""
    if direct in exact:
        return exact[direct]
    token = _DOI_TAIL.search(stem)
    if not token:
        return None
    hits = [pmid for pmid, doi in rows if doi.endswith(token.group(1))]
    return hits[0] if len(hits) == 1 else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    exact, rows = load_doi_index(DB_PATH)
    files = sorted(args.source.glob("*.pdf"))
    stats: Counter[str] = Counter()
    copied_pmids: list[str] = []
    for path in files:
        if path.stat().st_size < MIN_VALID_BYTES or path.open("rb").read(5) != b"%PDF-":
            stats["bad_pdf"] += 1
            continue
        pmid = match_pmid(path, exact, rows)
        if not pmid:
            stats["no_match"] += 1
            print(f"未匹配 {path.name}")
            continue
        target = PDF_DIR / f"{pmid}.pdf"
        if _existing_fulltext_path(pmid) is not None:
            stats["exists"] += 1
            continue
        stats["ready"] += 1
        copied_pmids.append(f"{pmid} <= {path.name}")
        if args.apply:
            part = target.with_suffix(".pdf.part")
            shutil.copy2(path, part)
            part.replace(target)

    for line in copied_pmids[:20]:
        print("  ", line)
    print(f"共 {len(files)} 个文件：{dict(stats)}")
    print("已写入" if args.apply else "dry-run，未写盘")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
