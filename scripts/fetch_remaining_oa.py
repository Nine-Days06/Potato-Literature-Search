"""尝试仍缺文献的剩余合法通道：
  1. 重试 EuropePMC getPdf（对之前 429/500/404）。
  2. 直链下载：从 OpenAlex/builder_oa_targets_round2.json 中取非封锁主机的 pdf_url，用 plain requests 下载。

用法：
  python scripts/fetch_remaining_oa.py --limit 80    # 小批试跑
  python scripts/fetch_remaining_oa.py             # 全量重试+直链
"""

import argparse
import csv
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from downloader.pdf_downloader import PROXY, _existing_fulltext_path, _output_dir, PDF_DIR
from config.settings import SUBDIR_CACHE, SUBDIR_FAILURE

BLOCKED_HOSTS = frozenset([
    "sciencedirect.com", "wiley.com", "tandfonline.com", "cell.com",
    "apsnet.org", "pnas.org", "nature.com", "link.springer.com",
    "karger.com", "ingentaconnect.com", "ashpublications.org", "doi.org",
])
TIMEOUT = (30, 180)
MAX_WORKERS = 8
MIN_BYTES = 10 * 1024
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0"


def _progress_path():
    return _output_dir(SUBDIR_CACHE) / "remaining_oa_progress.json"


def load_targets(csv_path: Path) -> list:
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def plan_units(rows: list) -> list[dict]:
    done = set()
    p = _progress_path()
    if p.exists():
        done.update(json.load(open(p, encoding="utf-8")))
    for r in rows:
        pmid = r["pmid"].strip()
        if pmid in done or _existing_fulltext_path(pmid):
            done.add(pmid)
    units = []
    # 1) EPMC 失败项重试：取缺失清单中所有带 pmc_id 的行
    for row in rows:
        pmid = row.get("pmid", "").strip()
        if not pmid or pmid in done:
            continue
        pmcid = row.get("pmc_id", "").strip()
        if pmcid.startswith("PMC"):
            units.append({"pmid": pmid, "url": f"https://europepmc.org/api/getPdf?pmcid={pmcid}", "kind": "epmc_retry"})
    # 2) round2 目标中的非封锁直链
    tjson = Path("data/output/cache/browser_oa_targets_round2.json")
    if tjson.exists():
        targets = json.load(open(tjson, encoding="utf-8"))
        ids = set(u["pmid"] for u in units) | done
        for item in targets:
            pmid = item.get("pmid", "")
            if pmid in ids:
                continue
            for url in item.get("urls", []):
                host = urlparse(url).netloc.lower()
                if host in BLOCKED_HOSTS or ".pdf" not in url:
                    continue
                if _existing_fulltext_path(pmid):
                    ids.add(pmid)
                    break
                units.append({"pmid": pmid, "url": url, "kind": "direct"})
                break
    return units


def download_with_validation(session: requests.Session, url: str, pmid: str) -> tuple[str | None, str]:
    """GET 下载，校验 %PDF 与体积后原子落盘。"""
    try:
        resp = session.get(url, timeout=TIMEOUT, allow_redirects=True)
    except Exception as e:
        return None, f"http_err:{type(e).__name__}"
    if resp.status_code != 200:
        return None, f"http:{resp.status_code}"
    data = resp.content
    if not data.startswith(b"%PDF"):
        return None, "not_pdf"
    if len(data) < MIN_BYTES:
        return None, f"too_small:{len(data)}"
    path = Path(PDF_DIR) / f"{pmid}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(".part")
    part.write_bytes(data)
    part.replace(path)
    return "downloaded", f"{url}|{len(data)}"


def main() -> int:
    ap = argparse.ArgumentParser(description="补下载仍缺文献的 OA 全文")
    ap.add_argument("--csv", default=r"data/output/exports/full/missing_relevant_current.csv", help="基准 CSV")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    rows = load_targets(Path(args.csv))
    units = plan_units(rows)
    print(f"计划任务 {len(units)} 条", flush=True)
    if args.limit:
        units = units[:args.limit]
        print(f"限流至 {len(units)} 条", flush=True)

    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    if PROXY:
        session.proxies.update({"http": PROXY, "https": PROXY})

    progress: dict[str, dict] = {}
    lock = threading.Lock()
    stats = {"ok": 0, "fail": 0}
    failed = []

    def worker(unit: dict) -> None:
        pmid = unit["pmid"]
        ok, reason = download_with_validation(session, unit["url"], pmid)
        with lock:
            if ok:
                stats["ok"] += 1
                progress[pmid] = {"status": "ok", "kind": unit["kind"], "detail": reason}
                json.dump(progress, open(_progress_path(), "w", encoding="utf-8"), ensure_ascii=False)
            else:
                stats["fail"] += 1
                failed.append({"pmid": pmid, "reason": reason, "kind": unit["kind"]})

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(worker, u) for u in units]
        for i, fut in enumerate(as_completed(futures), 1):
            fut.result()
            if i % 50 == 0 or i == len(units):
                print(f"进度 {i}/{len(units)} ok={stats['ok']} fail={stats['fail']}", flush=True)

    failed_csv = _output_dir(SUBDIR_FAILURE) / f"remaining_oa_failed_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with open(failed_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["pmid", "reason", "kind"])
        w.writeheader()
        w.writerows(failed)
    print(f"成功 {stats['ok']}，失败 {stats['fail']}，失败清单 {failed_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
