"""把外部按 DOI 命名的 PDF 回填为 data/pdfs_relevant/<PMID>.pdf。

默认 dry-run；加 --apply 才复制。不覆盖已有全文、不移动或删除源文件。
文件名规则复刻 ZTPchemistry/scihub-downloader 的 naming.py + sanitize.py
（MIT，8fc00071a65e6fe3dca6ca7689eb79813d9ed906）。
"""

import argparse
import csv
import re
import shutil
import sys
import unicodedata
from collections import Counter
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import OUTPUT_DIR, PDF_DIR, SUBDIR_EXPORT_FULL
from downloader.fulltext_library import existing_fulltext_path
from utils.audit import record

# 目标下载器的合法 PDF 下限：首 4 字节 %PDF 且 ≥10KB。
MIN_VALID_BYTES = 10 * 1024
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_DUP_SEP = re.compile(r"(?:\s*[-_]\s*){2,}")
_EDGE_SEP = re.compile(r"^[\s\-_]+|[\s\-_]+$")
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def doi_to_stem(doi: str, max_bytes: int = 251) -> str:
    """等价于 build_filename(Paper(doi), "doi")：先整串 tidy，再 sanitize。"""
    s = _DUP_SEP.sub(" - ", doi)
    s = _EDGE_SEP.sub("", s)
    s = unicodedata.normalize("NFKC", re.sub(r"\s{2,}", " ", s).strip()).strip()
    s = _INVALID.sub("_", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"_+", "_", s).strip("._ ")
    if not s:
        return "untitled"
    if s.split(".")[0].upper() in _WIN_RESERVED:
        s = "_" + s
    data = s.encode("utf-8")
    if len(data) > max_bytes:
        s = data[:max_bytes].decode("utf-8", errors="ignore").rstrip("._ ")
    return s or "untitled"


def latest_manifest() -> Path:
    root = Path(OUTPUT_DIR) / SUBDIR_EXPORT_FULL
    found = sorted(root.glob("missing_fulltext_manifest_*.csv"))
    if not found:
        raise FileNotFoundError(f"{root} 下没有 missing_fulltext_manifest_*.csv")
    return found[-1]


def _read_manifest(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return [row for row in csv.DictReader(stream) if row.get("batch_status") == "ready"]


def _is_valid_pdf(path: Path) -> bool:
    with path.open("rb") as stream:
        return stream.read(4) == b"%PDF"


def plan_import(manifest_path: Path, source_dir: Path) -> list[dict]:
    """只读判定每条记录的归属，不做任何写入。"""
    rows = []
    for row in _read_manifest(manifest_path):
        pmid = row["pmid"]
        expected = source_dir / f"{doi_to_stem(row['normalized_doi'])}.pdf"
        if existing_fulltext_path(pmid) is not None:
            action = "exists"
        elif not expected.is_file():
            action = "not_found"
        elif expected.stat().st_size < MIN_VALID_BYTES or not _is_valid_pdf(expected):
            action = "bad_pdf"
        else:
            action = "ready"
        rows.append({"pmid": pmid, "doi": row["normalized_doi"],
                     "source": expected, "action": action})
    return rows


def unmatched_sources(source_dir: Path, rows: list[dict]) -> list[str]:
    expected = {row["source"].name.casefold() for row in rows}
    return sorted(p.name for p in source_dir.glob("*.pdf")
                  if p.name.casefold() not in expected)


def run_import(manifest_path: Path, source_dir: Path, apply: bool) -> Counter:
    rows = plan_import(manifest_path, source_dir)
    counts = Counter(row["action"] for row in rows)
    if apply:
        dest_dir = Path(PDF_DIR)
        dest_dir.mkdir(parents=True, exist_ok=True)
        for row in rows:
            if row["action"] != "ready":
                continue
            target = dest_dir / f"{row['pmid']}.pdf"
            tmp = dest_dir / f"{row['pmid']}.pdf.part"
            try:
                # 先写 .part 再原子改名：磁盘中途写满不会留下被当成全文的半截 PDF。
                with row["source"].open("rb") as src, tmp.open("wb") as stream:
                    shutil.copyfileobj(src, stream)
                if target.exists():
                    raise FileExistsError(target)
                tmp.replace(target)
                shutil.copystat(row["source"], target)
                record("import", row["pmid"], target, doi=row["doi"],
                       source="import_missing_fulltext")
            except FileExistsError:
                tmp.unlink(missing_ok=True)
                counts["exists"] += 1
                counts["ready"] -= 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=Path(r"D:\桌面\临时"))
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--apply", action="store_true", help="真正复制；默认只预览")
    args = parser.parse_args()
    manifest = args.manifest or latest_manifest()
    try:
        if not args.source.is_dir():
            raise NotADirectoryError(f"源目录不存在：{args.source}")
        rows = plan_import(manifest, args.source)
        counts = Counter(row["action"] for row in rows)
        print(f"清单：{manifest}")
        print(f"源目录：{args.source}（{len(rows)} 条待查）")
        print(f"命中可回填 {counts['ready']}；已存在 {counts['exists']}；"
              f"未找到 {counts['not_found']}；PDF 异常 {counts['bad_pdf']}")
        extra = unmatched_sources(args.source, rows)
        print(f"源目录中未匹配清单的 PDF：{len(extra)}")
        for row in [r for r in rows if r["action"] != "ready"][:5]:
            print(f"  [{row['action']}] {row['pmid']} {row['doi']} -> {row['source'].name}")
        if not args.apply:
            print("\n预览模式，未写入。确认后加 --apply。")
            return 0
        counts = run_import(manifest, args.source, apply=True)
        print(f"\n已复制 {counts['ready']} 篇到 {PDF_DIR}；跳过已存在 {counts['exists']} 篇。")
        return 0
    except (OSError, ValueError) as exc:
        print(f"回填失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
