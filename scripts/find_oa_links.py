"""用 OpenAlex 为缺失全文的 DOI 查免费 OA 副本，导出清单，可选直接下载。

只读数据库；不改 articles/llm_validation。下载落点沿用相关全文库约定 data/pdfs_relevant/<pmid>.pdf。

    python scripts/find_oa_links.py                 # 只查链接，导出 CSV
    python scripts/find_oa_links.py --download      # 查完后立即下载
    python scripts/find_oa_links.py --from-csv <csv> --download  # 只下载已有清单
    python scripts/find_oa_links.py --from-csv <csv> --resolve-landing
        # 对只有落地页的 OA 记录（仓储站）解析出 PDF 直链，产出新清单
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import DB_PATH, OUTPUT_DIR, PDF_DIR, PROXY, SUBDIR_EXPORT_FULL  # noqa: E402
from downloader.fulltext_library import existing_fulltext_path  # noqa: E402
from downloader.pdf_downloader import download_pdf_file  # noqa: E402
from export_missing_fulltext import collect_missing  # noqa: E402

MAILTO = "potato-lit@proton.me"
API = "https://api.openalex.org/works/doi:"
# 这些落地页域名下没有可直接抓的 PDF：出版社门户或需要人工/浏览器验证的页面。
SKIP_LANDING_HOSTS = ("doi.org", "dx.doi.org", "pubmed.ncbi.nlm.nih.gov",
                      "pmc.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov")
CITATION_PDF = re.compile(
    r'<meta[^>]+(?:name|property)=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)', re.I)
PDF_HREF = re.compile(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', re.I)


def resolve_pdf_url(html: str, base_url: str) -> str | None:
    match = CITATION_PDF.search(html) or PDF_HREF.search(html)
    return urljoin(base_url, match.group(1)) if match else None


def resolve_landing(rows: list[dict], workers: int = 6) -> Counter:
    """给只有 landing_url 的仓储记录补 PDF 直链，原地改 rows。"""
    counters = Counter()

    def work(row: dict) -> None:
        landing = row.get("landing_url") or ""
        host = urlparse(landing).netloc
        if not landing or any(h in host for h in SKIP_LANDING_HOSTS):
            counters["skipped"] += 1
            return
        try:
            resp = _session().get(landing, timeout=30)
            if resp.status_code != 200:
                counters["http_error"] += 1
                return
            url = resolve_pdf_url(resp.text, landing)
        except requests.RequestException:
            counters["http_error"] += 1
            return
        if url:
            row["pdf_urls"] = url
            counters["resolved"] += 1
        else:
            counters["no_pdf_found"] += 1

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, rows))
    return counters
FIELDS = [
    "pmid", "doi", "title", "pub_year", "journal", "pmc_id",
    "oa_status", "host_type", "license", "pdf_urls", "landing_url",
]
_LOCAL = threading.local()


def _session() -> requests.Session:
    sess = getattr(_LOCAL, "sess", None)
    if sess is None:
        sess = requests.Session()
        sess.headers["User-Agent"] = f"potato-lit/1.0 (mailto:{MAILTO})"
        if PROXY:
            sess.proxies = {"http": PROXY, "https": PROXY}
        _LOCAL.sess = sess
    return sess


def extract_candidates(payload: dict) -> tuple[str, list[str], str, str]:
    """返回 (oa_status, pdf_url 列表, host_type, landing_url)。"""
    best = payload.get("best_oa_location") or {}
    urls: list[str] = []
    for loc in [best, *(payload.get("locations") or [])]:
        url = (loc or {}).get("pdf_url")
        if url and url not in urls:
            urls.append(url)
    return (
        (payload.get("open_access") or {}).get("oa_status") or "closed",
        urls,
        best.get("host_type") or "",
        best.get("landing_page_url") or "",
    )


def lookup(doi: str, retries: int = 3) -> dict | None:
    for attempt in range(retries):
        try:
            resp = _session().get(API + doi, params={"mailto": MAILTO}, timeout=30)
        except requests.RequestException:
            time.sleep(1 + attempt)
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 * (attempt + 1))
            continue
        return None
    return None


def _output_dir(subdir: str) -> Path:
    path = Path(OUTPUT_DIR) / subdir
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_links(db_path: Path | None = None, workers: int = 6) -> tuple[Path, Counter]:
    _batch, manifest = collect_missing(db_path or DB_PATH)
    # 清单行才有 pmid/title 等元数据；batch 只有外部工具需要的五个字段。
    batch = [row for row in manifest if row["batch_status"] == "ready"]
    counters = Counter()
    rows: list[dict] = []

    def work(row: dict) -> dict:
        doi = row["normalized_doi"] or row["doi"]
        payload = lookup(doi)
        if payload is None:
            return {"doi": doi, "pmid": row["pmid"], "oa_status": "lookup_failed",
                    "pdf_urls": "", "host_type": "", "license": "", "landing_url": ""}
        status, urls, host, landing = extract_candidates(payload)
        return {
            "doi": doi, "pmid": row["pmid"], "oa_status": status,
            "pdf_urls": ";".join(urls), "host_type": host,
            "license": (payload.get("best_oa_location") or {}).get("license") or "",
            "landing_url": landing,
        }

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, row): row for row in batch}
        for done, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            src = futures[fut]
            rows.append({
                "pmid": res["pmid"], "doi": res["doi"],
                "title": (src["title"] or "").strip(),
                "pub_year": str(src["pub_year"] or ""),
                "journal": (src["journal"] or "").strip(),
                "pmc_id": src["pmc_id"] or "",
                "oa_status": res["oa_status"], "host_type": res["host_type"],
                "license": res["license"], "pdf_urls": res["pdf_urls"],
                "landing_url": res["landing_url"],
            })
            counters[res["oa_status"]] += 1
            counters["has_pdf_url" if res["pdf_urls"] else "no_pdf_url"] += 1
            if done % 200 == 0:
                print(f"  已查 {done}/{len(batch)}", flush=True)

    rows.sort(key=lambda r: int(r["pmid"]))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = _output_dir(SUBDIR_EXPORT_FULL) / f"oa_links_{stamp}.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path, counters


def _is_pdf(path: Path) -> bool:
    try:
        with open(path, "rb") as stream:
            return stream.read(4) == b"%PDF"
    except OSError:
        return False


def download_rows(rows: list[dict], workers: int = 4) -> Counter:
    counters = Counter()

    def work(row: dict) -> str:
        pmid = row["pmid"]
        if existing_fulltext_path(pmid) is not None:
            return "exists"
        for url in [u for u in (row.get("pdf_urls") or "").split(";") if u]:
            dest = Path(PDF_DIR) / f"{pmid}.pdf"
            try:
                if download_pdf_file(url, dest) and _is_pdf(dest):
                    return "downloaded"
            except Exception:  # noqa: BLE001 - 单个 URL 失败要换下一个
                pass
            # 出版社常回 200 + 登录页 HTML，体积过 `download_pdf_file` 的门槛但不是 PDF。
            if dest.exists() and not _is_pdf(dest):
                dest.unlink(missing_ok=True)
        return "failed"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for done, fut in enumerate(as_completed([pool.submit(work, r) for r in rows]), 1):
            counters[fut.result()] += 1
            if done % 100 == 0:
                print(f"  已处理 {done}/{len(rows)}", flush=True)
    return counters


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenAlex 免费 OA 链接查询与下载")
    parser.add_argument("--download", action="store_true", help="查完后立即下载")
    parser.add_argument("--from-csv", type=Path, help="跳过查询，直接下载该清单")
    parser.add_argument("--resolve-landing", action="store_true",
                        help="对只有落地页的仓储记录解析 PDF 直链（配合 --from-csv）")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    if args.from_csv:
        with open(args.from_csv, encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        if args.resolve_landing:
            targets = [r for r in rows if not r.get("pdf_urls") and r.get("landing_url")]
            print(f"落地页待解析 {len(targets)} 篇")
            print("解析结果：", dict(resolve_landing(targets, workers=args.workers)))
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            out = _output_dir(SUBDIR_EXPORT_FULL) / f"oa_links_resolved_{stamp}.csv"
            with open(out, "w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows(rows)
            print(f"清单：{out}")
        todo = [r for r in rows if r.get("pdf_urls")]
        if args.download and todo:
            print(f"开始下载 {len(todo)} 篇")
            print("下载结果：", dict(download_rows(todo, workers=min(args.workers, 4))))
        return 0

    path, counters = find_links(workers=args.workers)
    print(f"清单：{path}")
    print("统计：", dict(counters))
    if args.download:
        with open(path, encoding="utf-8-sig") as stream:
            rows = [r for r in csv.DictReader(stream) if r.get("pdf_urls")]
        print(f"开始下载 {len(rows)} 篇")
        print("下载结果：", dict(download_rows(rows, workers=4)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
