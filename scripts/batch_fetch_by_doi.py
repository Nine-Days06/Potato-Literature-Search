"""校园网内批量下载全文：按 DOI 清单逐条抓 PDF，存成 <pmid>.pdf。

单文件、只依赖 requests。可在任何能访问出版方（校园 IP）的机器上运行：
    python batch_fetch_by_doi.py --input missing_relevant.csv --out pdfs
"""

from __future__ import annotations

import argparse
import csv
import re
import time
from pathlib import Path

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0"
)
MIN_PDF_BYTES = 10 * 1024

_META_PDF = re.compile(
    r'<meta[^>]+(?:name=["\']citation_pdf_url["\'][^>]*content=["\']([^"\']+)'
    r'|content=["\']([^"\']+)["\'][^>]*name=["\']citation_pdf_url)',
    re.I,
)
_HREF_PDF = re.compile(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', re.I)
_SD_PII = re.compile(r"/science/article/(?:pii|abs/pii)/([A-Z0-9]+)", re.I)


def existing(out: Path, pmid: str) -> Path | None:
    for suffix in (".pdf", ".xml", ".txt"):
        path = out / f"{pmid}{suffix}"
        if path.exists() and path.stat().st_size > MIN_PDF_BYTES:
            return path
    return None


def find_pdf_url(session: requests.Session, doi: str) -> tuple[str, str]:
    landing = session.get(f"https://doi.org/{doi}", timeout=40, allow_redirects=True)
    landing.raise_for_status()
    html = landing.text
    match = _META_PDF.search(html)
    if match:
        return (match.group(1) or match.group(2)), landing.url
    pii = _SD_PII.search(landing.url) or _SD_PII.search(html)
    if pii:
        return (
            f"https://www.sciencedirect.com/science/article/pii/{pii.group(1)}"
            "/pdfft?isDTMRedir=true&download=true",
            landing.url,
        )
    href = _HREF_PDF.search(html)
    if href:
        return requests.compat.urljoin(landing.url, href.group(1)), landing.url
    raise ValueError(f"no_pdf_link:{landing.url[:90]}")


def download(session: requests.Session, url: str, target: Path) -> int:
    with session.get(url, timeout=120, stream=True) as resp:
        resp.raise_for_status()
        first = next(resp.iter_content(5), b"")
        if not first.startswith(b"%PDF"):
            raise ValueError(f"not_pdf:{resp.headers.get('Content-Type', '')[:40]}")
        part = target.with_suffix(target.suffix + ".part")
        size = 0
        with part.open("wb") as f:
            f.write(first)
            size = len(first)
            for chunk in resp.iter_content(65536):
                f.write(chunk)
                size += len(chunk)
    if size < MIN_PDF_BYTES:
        part.unlink(missing_ok=True)
        raise ValueError(f"too_small:{size}")
    part.replace(target)
    return size


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="含 pmid,doi 两列的 CSV")
    parser.add_argument("--out", type=Path, default=Path("pdfs"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--sleep", type=float, default=1.0, help="每篇间隔秒数")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    with args.input.open(encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("doi", "").strip()]
    if args.limit:
        rows = rows[: args.limit]

    session = requests.Session()
    session.headers["User-Agent"] = UA
    session.trust_env = True  # 校园网直连时把系统代理关掉即可

    ok = skip = 0
    failed: list[tuple[str, str, str]] = []
    for i, row in enumerate(rows, 1):
        pmid, doi = str(row["pmid"]).strip(), row["doi"].strip()
        if existing(args.out, pmid):
            skip += 1
            continue
        try:
            pdf_url, landing_url = find_pdf_url(session, doi)
            # 带 referer 更像真实点击，降低被当成直链爬虫的概率。
            session.headers["Referer"] = landing_url
            size = download(session, pdf_url, args.out / f"{pmid}.pdf")
            ok += 1
            print(f"[{i}/{len(rows)}] OK {pmid} {size // 1024}KB", flush=True)
        except Exception as exc:  # noqa: BLE001 - 逐条记录，不中断批处理
            failed.append((pmid, doi, f"{type(exc).__name__}: {exc}"[:160]))
            print(f"[{i}/{len(rows)}] FAIL {pmid} {failed[-1][2][:70]}", flush=True)
        time.sleep(args.sleep)

    report = args.out.parent / "batch_fetch_failed.csv"
    with report.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["pmid", "doi", "reason"])
        writer.writerows(failed)
    print(f"完成：成功 {ok}，已存在 {skip}，失败 {len(failed)} -> {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
