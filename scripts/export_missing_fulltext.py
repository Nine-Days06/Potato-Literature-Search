"""本地缺失全文导出；只读数据库，不请求网络，不改全文目录。

DOI normalization is adapted from ZTPchemistry/scihub-downloader at
8fc00071a65e6fe3dca6ca7689eb79813d9ed906 (scihub_dl/doi.py).

MIT License
Copyright (c) 2026 Sci-Hub Downloader contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import argparse
import csv
import json
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import DB_PATH, OUTPUT_DIR, SUBDIR_EXPORT_FULL
from downloader.fulltext_library import existing_fulltext_path, relevant_pmids


MANIFEST_FIELDS = [
    "pmid", "doi", "normalized_doi", "title", "authors", "pub_year",
    "journal", "pmc_id", "batch_status", "batch_pmid", "existing_doi_pmid",
]


def normalize_doi(raw: str) -> str | None:
    """与目标 JSON 导入器使用相同的规则，保留配平括号和 SICI DOI。"""
    s = raw.strip().strip("<>")
    s = re.sub(r"^doi\s*[:：]\s*", "", s, flags=re.I).strip()
    s = re.sub(r"^(?:https?://)?(?:dx\.)?doi\.org/", "", s, flags=re.I).strip()
    s = re.sub(r"^doi\s*[:：]\s*", "", s, flags=re.I).strip()
    pairs = {")": "(", "]": "[", "}": "{", "＞": "＜"}
    while s:
        last = s[-1]
        if last in ".,;:!?'\"、，。；：）】":
            s = s[:-1]
        elif last in pairs and s.count(last) > s.count(pairs[last]):
            s = s[:-1]
        else:
            break
    return s.lower() if re.fullmatch(r"10\.\d{4,9}/\S+", s) else None


def collect_missing(db_path: Path) -> tuple[list[dict], list[dict]]:
    """相关文献口径；CSV 保留全部相关缺失 PMID，JSON 每个规范 DOI 仅保留一条。"""
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(
            "SELECT pmid, doi, title, authors, pub_year, journal, pmc_id "
            "FROM articles ORDER BY CAST(pmid AS INTEGER), pmid"
        )]
    relevant = relevant_pmids(db_path)
    rows = [row for row in rows if str(row["pmid"] or "") in relevant]

    existing_pmids = set()
    existing_dois = {}
    for row in rows:
        pmid = str(row["pmid"] or "")
        if not re.fullmatch(r"[0-9]+", pmid):
            raise ValueError(f"非法 PMID：{pmid!r}")
        row["pmid"] = pmid
        row["normalized_doi"] = normalize_doi(row["doi"] or "") or ""
        if existing_fulltext_path(pmid) is not None:
            existing_pmids.add(pmid)
            if row["normalized_doi"]:
                existing_dois.setdefault(row["normalized_doi"], pmid)

    batch = []
    manifest = []
    batch_pmids = {}
    for row in rows:
        if row["pmid"] in existing_pmids:
            continue
        doi = row["normalized_doi"]
        row["existing_doi_pmid"] = existing_dois.get(doi, "")
        if not (row["doi"] or "").strip():
            row["batch_status"] = "no_doi"
        elif not doi:
            row["batch_status"] = "invalid_doi"
        elif doi in batch_pmids:
            row["batch_status"] = "duplicate_doi"
        else:
            row["batch_status"] = "ready"
            batch_pmids[doi] = row["pmid"]
            batch.append({
                "doi": doi,
                "title": (row["title"] or "").strip(),
                "author": (row["authors"] or "").split("|", 1)[0].strip(),
                "year": str(row["pub_year"] or ""),
                "journal": (row["journal"] or "").strip(),
            })
        row["batch_pmid"] = batch_pmids.get(doi, "")
        manifest.append(row)
    return batch, manifest


def _output_dir(subdir: str) -> Path:
    path = Path(OUTPUT_DIR) / subdir
    path.mkdir(parents=True, exist_ok=True)
    return path


def export_missing_fulltext(db_path: Path | None = None) -> tuple[Path, Path, dict]:
    batch, manifest = collect_missing(Path(db_path or DB_PATH))
    out = _output_dir(SUBDIR_EXPORT_FULL)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    batch_path = out / f"missing_fulltext_batch_{stamp}.json"
    manifest_path = out / f"missing_fulltext_manifest_{stamp}.csv"
    # JSON 导入器不接受 BOM；CSV 使用 BOM 便于 Excel 打开。
    with batch_path.open("x", encoding="utf-8") as stream:
        json.dump(batch, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    with manifest_path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(manifest)

    counts = {status: sum(row["batch_status"] == status for row in manifest)
              for status in ("ready", "duplicate_doi", "no_doi", "invalid_doi")}
    counts["missing"] = len(manifest)
    counts["existing_doi"] = sum(bool(row["existing_doi_pmid"]) for row in manifest)
    return batch_path, manifest_path, counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args()
    try:
        batch_path, manifest_path, counts = export_missing_fulltext()
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"导出失败：{exc}", file=sys.stderr)
        return 1
    print(f"缺失全文 {counts['missing']} 篇；批量 DOI {counts['ready']} 个；"
          f"重复 DOI {counts['duplicate_doi']} 篇；无 DOI {counts['no_doi']} 篇；"
          f"无效 DOI {counts['invalid_doi']} 篇。")
    print(f"其中 {counts['existing_doi']} 篇的同 DOI 全文已在其他 PMID 下，详见清单。")
    print(f"批量 JSON：{batch_path}\n映射 CSV：{manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
