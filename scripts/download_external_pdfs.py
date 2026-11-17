"""下载站点补录里 PubMed 不收的文献 OA 全文，按 DOI 命名。

落点 `config.settings.EXTERNAL_PDF_DIR`（data/pdfs_nonpubmed/），与相关全文库
`data/pdfs_relevant/` 分开。已有同名文件且 ≥10KB 直接跳过（可重跑）。

用法：
    python scripts/download_external_pdfs.py                # 默认全部
    python scripts/download_external_pdfs.py --limit 20     # 先试 20 篇
"""

import argparse
import csv
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import EXTERNAL_PDF_DIR, OUTPUT_DIR, PROXY, SUBDIR_FAILURE
from scripts.import_missing_fulltext import doi_to_stem
from scripts.import_site_literature import latest_harvest, load_rows
from utils.audit import record

MIN_BYTES = 10 * 1024
UA = "Mozilla/5.0 (compatible; potato-lit/1.0)"
_lock = threading.Lock()


_MDPI = re.compile(r"mdpi\.com/\d+-\d+/(\d+)/\d+/(\d+)/pdf")


def candidates(url: str, journal: str) -> list[str]:
    """MDPI 主站被 Akamai bot manager 403，但它的 PDF CDN mdpi-res.com 不设防。"""
    match = _MDPI.search(url)
    if not match:
        return [url]
    slug = journal.strip().lower().replace(" ", "_")
    artno = int(match.group(2))
    stems = (f"{slug}-{int(match.group(1)):02d}-{artno:05d}",  # 卷号 <10 时 CDN 补零
             f"{slug}-{match.group(1)}-{artno:05d}")
    return [f"https://mdpi-res.com/d_attachment/{slug}/{stem}/article_deploy/{stem}.pdf"
            for stem in stems] + [url]


def _pdf_links(html: str, base: str) -> list[str]:
    return [urljoin(base, href) for href in
            re.findall(r'href="([^"]+?\.pdf[^"]*)"', html, re.I)][:2]


def fetch(urls: list[str], target: Path) -> tuple[str, str]:
    kwargs = {"timeout": 45, "headers": {"User-Agent": UA, "Referer": "https://scholar.google.com/"},
              "stream": True}
    if PROXY:
        kwargs["proxies"] = {"http": PROXY, "https": PROXY}
    part = target.with_suffix(".part")
    status = ""
    queue = list(urls)
    for _ in range(6):
        if not queue:
            break
        url = queue.pop(0)
        try:
            with requests.get(url, **kwargs) as resp:
                if resp.status_code != 200:
                    status = "http_%d" % resp.status_code
                    continue
                is_html = "html" in (resp.headers.get("content-type") or "")
                with part.open("wb") as fh:
                    for chunk in resp.iter_content(65536):
                        fh.write(chunk)
        except requests.RequestException as exc:
            status = type(exc).__name__
            continue
        if is_html:  # 机构库/出版社落地页：顺着页面里的 .pdf 链接再试一层
            status = "not_pdf"
            queue.extend(_pdf_links(part.read_bytes()[:400_000].decode("utf-8", "ignore"), url))
            continue
        if part.open("rb").read(4) != b"%PDF" or part.stat().st_size < MIN_BYTES:
            status = "not_pdf"
            continue
        part.replace(target)
        return "ok", str(target)
    if part.exists():
        part.unlink(missing_ok=True)
    return status or "failed", ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    rows = [r for r in load_rows(Path(args.csv) if args.csv else latest_harvest())
            if r["oa_pdf_url"]]
    if args.limit:
        rows = rows[: args.limit]
    out_dir = Path(EXTERNAL_PDF_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    todo = []
    for row in rows:
        target = out_dir / (doi_to_stem(row["doi"]) + ".pdf")
        if target.exists() and target.stat().st_size >= MIN_BYTES:
            continue
        todo.append((row, target))
    print(f"待下载 {len(todo)} / 有 OA 链接 {len(rows)}，落点 {out_dir}")

    failures = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch, candidates(row["oa_pdf_url"], row["journal"]), target)
                   for row, target in todo]
        for i, (future, (row, target)) in enumerate(zip(futures, todo), 1):
            status, _ = future.result()
            if status != "ok":
                failures.append((row["doi"], row["oa_pdf_url"], status))
            else:
                record("download", row.get("pmid", ""), target, doi=row["doi"],
                       source="external_oa", url=row["oa_pdf_url"])
            if i % 100 == 0:
                print(f"{i}/{len(todo)}", flush=True)

    fail_dir = Path(OUTPUT_DIR) / SUBDIR_FAILURE
    fail_dir.mkdir(parents=True, exist_ok=True)
    fail_csv = fail_dir / f"external_pdf_failed_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with fail_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["doi", "url", "status"])
        writer.writerows(failures)
    print(f"成功 {len(todo) - len(failures)}，失败 {len(failures)} → {fail_csv}")


if __name__ == "__main__":
    main()
