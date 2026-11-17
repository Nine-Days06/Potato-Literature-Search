"""按 OpenAlex 的 OA 直链（best_oa_location.pdf_url）为仍缺全文的文献补下载。

用法：
    python scripts/fetch_openalex_oa.py --csv data/output/exports/full/missing_relevant_remaining_20261101.csv

只做两件事：批量查 OpenAlex 拿合法 OA PDF 直链，然后校验 `%PDF` 头写进 data/pdfs_relevant/<pmid>.pdf。
订阅站（Elsevier/Wiley/APS…）在 OpenAlex 里没有 OA 直链，会被自然跳过。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import re
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))
from config import settings  # noqa: E402
from downloader.pdf_downloader import PDF_DIR, _existing_fulltext_path, _output_dir  # noqa: E402

API = "https://api.openalex.org/works"
MAILTO = "potato-lit@proton.me"
PAGE = 100           # 一次 filter 查询的 DOI 条数（OpenAlex 上限 200）
MAX_WORKERS = 10
TIMEOUT = (20, 120)
MIN_BYTES = 10240
PROXY = getattr(settings, "PROXY", "") or ""
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

BLOCKED_HOSTS = (
    "sciencedirect.com", "wiley.com", "onlinelibrary.wiley", "tandfonline.com",
    "cell.com", "apsnet.org", "apsjournals", "pnas.org", "nature.com",
    "link.springer", "springer.com", "karger.com", "ingentaconnect",
    "ashpublications", "doi.org",
)

_lock = threading.Lock()
_session_local = threading.local()


def _session() -> requests.Session:
    sess = getattr(_session_local, "sess", None)
    if sess is None:
        sess = requests.Session()
        sess.headers["User-Agent"] = UA
        if PROXY:
            sess.proxies = {"http": PROXY, "https": PROXY}
        _session_local.sess = sess
    return sess


def _key(doi: str) -> str:
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", doi.strip().lower())


def query_openalex(doies: list[str], retries: int = 3) -> dict[str, list[str]]:
    """返回 {doi: [pdf 直链, ...]}；查不到或无 OA 直链的 DOI 不出现在结果里。"""
    params = {
        "filter": "|".join("doi:" + d for d in doies),
        "select": "id,doi,best_oa_location,locations",
        "per-page": PAGE,
        "mailto": MAILTO,
    }
    resp = None
    for attempt in range(retries):
        try:
            got = _session().get(API, params=params, timeout=TIMEOUT)
            if got.status_code == 200:
                resp = got
                break
            time.sleep(2.0 * (attempt + 1))
        except requests.RequestException:
            time.sleep(2.0 * (attempt + 1))
    if resp is None:
        return {}
    out: dict[str, list[str]] = {}
    for work in resp.json().get("results", []):
        urls: list[str] = []
        for loc in [work.get("best_oa_location")] + (work.get("locations") or []):
            if not loc:
                continue
            for cand in (loc.get("pdf_url"), loc.get("landing_page_url")):
                if cand and ".pdf" in cand.lower() and cand not in urls:
                    urls.append(cand)
        if urls:
            doi = (work.get("doi") or "").replace("https://doi.org/", "")
            if doi:
                out[_key(doi)] = urls
    return out


def download(url: str, pmid: str) -> tuple[str, str]:
    if _existing_fulltext_path(pmid):
        return "preexisting", ""
    host = urlparse(url).netloc.lower()
    if any(b in host for b in BLOCKED_HOSTS):
        return "blocked_host", host
    try:
        resp = _session().get(url, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException as exc:
        return "request_error", type(exc).__name__
    body = resp.content
    if resp.status_code != 200:
        return "http_%d" % resp.status_code, ""
    if not body.startswith(b"%PDF") or len(body) <= MIN_BYTES:
        return "not_pdf", "bytes=%d ct=%s" % (len(body), resp.headers.get("Content-Type", ""))
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PDF_DIR / ("%s.pdf.part" % pmid)
    tmp.write_bytes(body)
    tmp.replace(PDF_DIR / ("%s.pdf" % pmid))
    return "ok", "%s|%d" % (host, len(body))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry", action="store_true", help="只查直链不下载")
    args = ap.parse_args()

    progress_path = ROOT / "data" / "output" / "cache" / "openalex_oa_progress.json"
    progress: dict[str, dict] = {}
    if progress_path.exists():
        progress = json.loads(progress_path.read_text(encoding="utf-8"))

    rows = [r for r in csv.DictReader(open(args.csv, encoding="utf-8-sig"))
            if (r.get("doi") or "").strip()]
    targets = [r for r in rows
               if _key(r["doi"]) not in progress and not _existing_fulltext_path(r["pmid"])]
    if args.limit:
        targets = targets[:args.limit]
    print("候选 %d 条（有 DOI 且仍缺全文）" % len(targets), flush=True)

    # 1) 批量拿 OA 直链；查过的 DOI 记进 progress，重跑时跳过
    links: dict[str, list[str]] = {}
    keys = [_key(r["doi"]) for r in targets]
    for i in range(0, len(keys), PAGE):
        batch = keys[i:i + PAGE]
        got = query_openalex(batch)
        links.update(got)
        with _lock:
            for b in batch:
                progress.setdefault(b, {"kind": "oa_pdf" if b in got else "no_oa_pdf"})
            progress_path.parent.mkdir(parents=True, exist_ok=True)
            progress_path.write_text(json.dumps(progress, ensure_ascii=False), encoding="utf-8")
        print("查询 %d/%d，累计有直链 %d" % (min(i + PAGE, len(keys)), len(keys), len(links)), flush=True)
        time.sleep(0.3)
    print("有 OA 直链的 DOI：%d" % len(links), flush=True)
    if args.dry:
        return 0

    # 2) 下载：同一 PMID 命中多个直链时，成功一个就停
    jobs = []
    seen = set()
    for row in targets:
        for url in links.get(_key(row["doi"]), []):
            if (row["pmid"], url) not in seen:
                seen.add((row["pmid"], url))
                jobs.append((row["pmid"], url))
    print("下载任务 %d 个（覆盖 PMID %d 个）" % (len(jobs), len({p for p, _ in jobs})), flush=True)

    done_pmids = set()
    stats: Counter = Counter()
    failures: list[tuple[str, str, str]] = []

    def work(job):
        pmid, url = job
        if pmid in done_pmids or _existing_fulltext_path(pmid):
            return pmid, "preexisting", ""
        return (pmid,) + download(url, pmid)

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for pmid, st, detail in pool.map(work, jobs):
            stats[st] += 1
            if st == "ok":
                done_pmids.add(pmid)
            elif st not in ("preexisting", "blocked_host"):
                failures.append((pmid, st, detail))
            n = sum(stats.values())
            if n % 100 == 0:
                print("进度 %d ok=%d" % (n, stats["ok"]), flush=True)

    if failures:
        out = _output_dir(getattr(settings, "SUBDIR_FAILURE")) / (
            "openalex_oa_failed_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M%S"))
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["pmid", "reason", "detail"])
            w.writerows(failures)
        print("失败清单 %s" % out, flush=True)
    print("新增成功 %d，明细 %s" % (stats["ok"], dict(stats)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
