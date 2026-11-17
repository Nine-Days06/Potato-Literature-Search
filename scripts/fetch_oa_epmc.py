"""EuropePMC 全文补下载：覆盖 PMC OA 开放集合之外的 inEPMC 文献。

两条取链通道（复用 downloader/pdf_downloader 的下载与链接缓存）：
  1. https://europepmc.org/api/getPdf?pmcid=<PMCID>        —— 直取原始 PDF 字节
  2. PMC S3 OA 链接（fetch_oa_links / load_cached_oa_links）

用法：
  python scripts/fetch_oa_epmc.py --csv <缺失清单.csv> --limit 40   # 小批试跑
  python scripts/fetch_oa_epmc.py --csv <缺失清单.csv>              # 全量

断点续跑靠进度文件 data/output/cache/epmc_oa_progress.json，删掉即重来。
"""

import argparse
import csv
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import SUBDIR_CACHE, SUBDIR_FAILURE  # noqa: E402
from downloader.pdf_downloader import (  # noqa: E402
    MIN_VALID_FILE_BYTES,
    PDF_DIR,
    PROXY,
    _existing_fulltext_path,
    _output_dir,
    _run_requests_download,
    fetch_oa_links,
    load_cached_oa_links,
)

EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EPMC_GETPDF = "https://europepmc.org/api/getPdf?pmcid="
BATCH = 200
MAX_WORKERS = 8
TIMEOUT = (20, 120)
MIN_PDF_BYTES = 10240
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def _output_dir_of(subdir: str) -> Path:
    # 走模块属性而非直接引用，保证测试 patch OUTPUT_DIR 仍然生效
    import downloader.pdf_downloader as pd

    return Path(pd.OUTPUT_DIR) / subdir


def _progress_path() -> Path:
    return _output_dir_of(SUBDIR_CACHE) / "epmc_oa_progress.json"


def load_rows(csv_path: Path) -> list[dict]:
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def epmc_query(session: requests.Session, pmids: list[str]) -> dict[str, dict]:
    """按 PMID 批量查 EuropePMC，返回 {pmid: {pmcid, inEPMC, isOpenAccess, urls}}。"""
    cond = " OR ".join(f"EXT_ID:{p}" for p in pmids)
    params = {
        "query": cond,
        "format": "json",
        "resultType": "core",
        "pageSize": str(BATCH),
        "abstract": "false",
    }
    out: dict[str, dict] = {}
    resp = session.get(EPMC_SEARCH, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    for item in resp.json().get("resultList", {}).get("result", []):
        if item.get("pmid"):
            out[str(item["pmid"])] = item
    return out


def save_pdf(resp: requests.Response, pmid: str) -> int | None:
    """校验 %PDF 头与体积后原子落盘，返回字节数（无效返回 None）。"""
    data = resp.content
    if not data.startswith(b"%PDF") or len(data) <= MIN_PDF_BYTES:
        return None
    dest = Path(PDF_DIR) / f"{pmid}.pdf"
    part = dest.with_suffix(".pdf.part")
    part.write_bytes(data)
    part.replace(dest)
    return len(data)


def try_pmcid(session: requests.Session, pmid: str, pmcid: str) -> tuple[str | None, str]:
    url = EPMC_GETPDF + pmcid
    try:
        resp = session.get(url, timeout=TIMEOUT)
    except Exception as e:  # noqa: BLE001
        return None, f"getPdf_error:{type(e).__name__}"
    if resp.status_code != 200:
        return None, f"getPdf_http:{resp.status_code}"
    size = save_pdf(resp, pmid)
    if size:
        return "epmc_getpdf", f"{url}|{size}"
    return None, "getPdf_not_pdf"


def try_oa_link(session: requests.Session, pmid: str, pdf_url: str) -> tuple[str | None, str]:
    dest = Path(PDF_DIR) / f"{pmid}.pdf"
    if not _run_requests_download(pdf_url, dest):
        return None, "oa_download_failed"
    try:
        with open(dest, "rb") as f:
            head = f.read(5)
    except OSError:
        return None, "oa_missing_file"
    if not head.startswith(b"%PDF"):
        dest.unlink(missing_ok=True)
        return None, "oa_not_pdf"
    return "pmc_oa", f"{pdf_url}|{dest.stat().st_size}"


def run(csv_path: Path, limit: int, dry: bool) -> int:
    rows = load_rows(csv_path)
    done = json.loads(_progress_path().read_text(encoding="utf-8")) if _progress_path().exists() else {}
    targets = []
    for row in rows:
        pmid = str(row.get("pmid", "")).strip()
        if not pmid or pmid in done:
            continue
        if _existing_fulltext_path(pmid):
            done[pmid] = {"status": "preexisting"}
            continue
        targets.append(row)
    if limit:
        targets = targets[:limit]

    print(f"待处理 {len(targets)} 篇（已有全文跳过 {sum(1 for v in done.values() if v.get('status') == 'preexisting')} 篇）", flush=True)
    if dry:
        return 0

    proxies = {"http": PROXY, "https": PROXY} if PROXY else None
    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    if proxies:
        session.proxies.update(proxies)

    # 1) 批量元数据：pmcid / inEPMC / isOpenAccess / fullTextUrl
    meta: dict[str, dict] = {}
    todo_ids = [str(r["pmid"]) for r in targets]
    for i in range(0, len(todo_ids), BATCH):
        chunk = todo_ids[i : i + BATCH]
        for attempt in range(3):
            try:
                meta.update(epmc_query(session, chunk))
                break
            except Exception as e:  # noqa: BLE001
                print(f"  元数据批次 {i // BATCH + 1} 第 {attempt + 1} 次失败: {e}", flush=True)
                time.sleep(2 * (attempt + 1))
        print(f"  元数据进度 {min(i + BATCH, len(todo_ids))}/{len(todo_ids)}", flush=True)

    # 2) 非 OA 但有 pmcid 的走 getPdf；OA 子集走仓库既有 PMC S3 链接
    pmc_ids = sorted({m.get("pmcid") for m in meta.values() if m.get("pmcid")})
    cached = load_cached_oa_links(pmc_ids)
    oa_map, _net_failed = fetch_oa_links(pmc_ids, cached_links=cached) if pmc_ids else ({}, [])

    work = []
    for row in targets:
        pmid = str(row["pmid"])
        item = meta.get(pmid, {})
        work.append(
            {
                "pmid": pmid,
                "pmcid": item.get("pmcid", "") or row.get("pmc_id", ""),
                "open": str(item.get("isOpenAccess", "N")).upper() == "Y",
                "epmc": str(item.get("inEPMC", "N")).upper() == "Y",
                "oa_pdf": oa_map.get(item.get("pmcid", ""), {}).get("pdf", ""),
            }
        )

    lock = threading.Lock()
    stats = {"downloaded": 0, "failed": 0}
    failures: list[dict] = []

    def handle(unit: dict) -> None:
        pmid = unit["pmid"]
        channels = []
        if unit["oa_pdf"]:
            channels.append(("oa", unit["oa_pdf"]))
        # inEPMC 标志不可靠：实测标 N 的记录 getPdf 仍返回真 PDF，只要有 PMCID 就试
        if unit["pmcid"]:
            channels.append(("getpdf", unit["pmcid"]))

        reason = "no_channel"
        for kind, ref in channels:
            with lock:
                time.sleep(0.12)  # 全局节流，避免打爆 EPMC
            if kind == "oa":
                got, detail = try_oa_link(session, pmid, ref)
            elif kind == "getpdf":
                got, detail = try_pmcid(session, pmid, ref)
            if got:
                with lock:
                    done[pmid] = {"status": "downloaded", "kind": got, "detail": detail}
                    stats["downloaded"] += 1
                    _progress_path().write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
                return
            reason = detail
        with lock:
            done[pmid] = {"status": "failed", "reason": reason}
            stats["failed"] += 1
            failures.append({"pmid": pmid, "pmc_id": unit["pmcid"], "reason": reason})

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(handle, u) for u in work]
        for n, fut in enumerate(as_completed(futures), 1):
            fut.result()
            if n % 50 == 0 or n == len(work):
                print(f"下载进度 {n}/{len(work)} 成功 {stats['downloaded']} 失败 {stats['failed']}", flush=True)
                _progress_path().write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")

    _progress_path().write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
    if failures:
        out_dir = _output_dir_of(SUBDIR_FAILURE)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        fail_csv = out_dir / f"epmc_oa_failed_{stamp}.csv"
        with open(fail_csv, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["pmid", "pmc_id", "reason"])
            writer.writeheader()
            writer.writerows(failures)
        print(f"失败清单: {fail_csv}", flush=True)
    print(f"完成：成功 {stats['downloaded']}，失败 {stats['failed']}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="EuropePMC/PMC OA 全文补下载")
    ap.add_argument("--csv", required=True, help="缺失全文清单 CSV（列 pmid,doi,pmc_id,title,journal,year,authors）")
    ap.add_argument("--limit", type=int, default=0, help="本次最多处理多少篇（小批试跑）")
    ap.add_argument("--dry", action="store_true", help="只打印将要处理的规模，不发请求")
    return run(Path(ap.parse_args().csv), ap.parse_args().limit or 0, ap.parse_args().dry)


if __name__ == "__main__":
    raise SystemExit(main())
