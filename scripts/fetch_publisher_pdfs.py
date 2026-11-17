"""用浏览器会话批量抓取订阅出版社的 PDF（绕开脚本指纹拦截）。

先手动登录一次（校园网 / WebVPN 均可），之后脚本复用同一浏览器配置目录：

    python scripts/fetch_publisher_pdfs.py --login            # 打开窗口，登录后回车
    python scripts/fetch_publisher_pdfs.py --limit 20         # 小批试跑
    python scripts/fetch_publisher_pdfs.py --publishers Wiley,ACS

命中失败时逐个换候选 URL；下载文件要求 %PDF 头，落点 data/pdfs_relevant/<pmid>.pdf。
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import sys
import time
from collections import Counter
from contextlib import closing
from pathlib import Path
from urllib.parse import urljoin

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import DB_PATH, OUTPUT_DIR, PDF_DIR, SUBDIR_CACHE  # noqa: E402
from downloader.fulltext_library import relevant_pmids  # noqa: E402

CACHE_DIR = Path(OUTPUT_DIR) / SUBDIR_CACHE
PROFILE_DIR = CACHE_DIR / "browser_profile"
EDGE_PROFILE_DIR = CACHE_DIR / "edge_profile"
CHECKPOINT = CACHE_DIR / "publisher_fetch_progress.json"
META = re.compile(
    r'<meta[^>]+(?:name|property)=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)', re.I)

DIRECT = {
    "Wiley": "https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}",
    "ACS": "https://pubs.acs.org/doi/pdf/{doi}",
    "ASM": "https://journals.asm.org/doi/pdf/{doi}",
    "Springer": "https://link.springer.com/content/pdf/{doi}.pdf",
    "T&F": "https://www.tandfonline.com/doi/pdf/{doi}",
    "APS": "https://apsjournals.apsnet.org/doi/pdf/{doi}",
}

PREFIX = {
    "10.1111": "Wiley", "10.1002": "Wiley", "10.1016": "Elsevier", "10.1007": "Springer",
    "10.1093": "OUP", "10.1021": "ACS", "10.1128": "ASM", "10.1099": "MicrobioSoc",
    "10.1080": "T&F", "10.1094": "APS", "10.1039": "RSC", "10.1017": "Cambridge",
}


def publisher_of(doi: str) -> str:
    return PREFIX.get(".".join(doi.lower().split("/")[0].split(".")[:2]), "Other")


def missing_rows(db_path: Path) -> list[dict]:
    pdfs = Path(PDF_DIR)
    names = {f.name for f in pdfs.iterdir()} if pdfs.exists() else set()
    relevant = relevant_pmids(db_path)
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        rows = [{"pmid": p, "doi": d} for p, d in
                conn.execute("SELECT pmid, doi FROM articles WHERE doi IS NOT NULL AND doi != ''")]
    rows = [r for r in rows if r["pmid"] in relevant]
    return [r for r in rows
            if not any(f"{r['pmid']}{s}" in names for s in (".pdf", ".xml", ".txt"))]


def _load_checkpoint() -> dict[str, str]:
    if CHECKPOINT.exists():
        return json.loads(CHECKPOINT.read_text(encoding="utf-8"))
    return {}


def _save_checkpoint(done: dict[str, str]) -> None:
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    tmp = CHECKPOINT.with_suffix(".tmp")
    tmp.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CHECKPOINT)


def _grab(page, url: str) -> bytes | None:
    """访问 PDF 地址：优先接下载事件，其次读响应体。"""
    from playwright.sync_api import TimeoutError as PWTimeout

    try:
        with page.expect_download(timeout=25000) as info:
            page.goto(url, wait_until="commit", timeout=45000)
        with open(info.value.path(), "rb") as stream:
            return stream.read()
    except PWTimeout:
        pass
    except Exception:  # noqa: BLE001 - 下载事件失败时退回响应体
        pass
    resp = page.goto(url, wait_until="load", timeout=45000)
    if resp is not None and resp.status == 200:
        try:
            return resp.body()
        except Exception:  # noqa: BLE001
            return None
    return None


def _save(data: bytes, pmid: str) -> None:
    dest = Path(PDF_DIR) / f"{pmid}.pdf"
    part = dest.with_suffix(".pdf.part")
    part.write_bytes(data)
    part.replace(dest)


def _meta_links(html: str, base: str) -> str | None:
    match = META.search(html)
    return urljoin(base, match.group(1)) if match else None


def _http_pdf(session: requests.Session, url: str) -> bytes | None:
    try:
        resp = session.get(url, timeout=45, allow_redirects=True)
    except requests.RequestException:
        return None
    return resp.content if resp.ok and resp.content[:4] == b"%PDF" else None


def fetch_one(page, row: dict, session: requests.Session) -> str:
    """先走 B（带 cookie 的直连，快），全部失败再走 A（浏览器下载）。"""
    doi, pmid = row["doi"], row["pmid"]
    pub = publisher_of(doi)
    candidates: list[str] = []
    if pub in DIRECT:
        candidates.append(DIRECT[pub].format(doi=doi))

    landed = False
    try:
        resp = session.get(f"https://doi.org/{doi}", timeout=45, allow_redirects=True)
        if resp.ok and "html" in resp.headers.get("content-type", ""):
            landed = True
            link = _meta_links(resp.text, resp.url)
            if link:
                candidates.append(link)
    except requests.RequestException:
        pass

    for url in dict.fromkeys(candidates):
        data = _http_pdf(session, url)
        if data:
            _save(data, pmid)
            return "http"

    if not landed:
        resp = page.goto(f"https://doi.org/{doi}", wait_until="domcontentloaded", timeout=45000)
        if resp is not None and resp.status == 200:
            link = _meta_links(page.content(), page.url)
            if link:
                candidates.append(link)

    for url in dict.fromkeys(candidates):
        data = _grab(page, url)
        if data and data[:4] == b"%PDF":
            _save(data, pmid)
            return "browser"
    return "failed"


def build_session(ctx) -> requests.Session:
    """把浏览器 cookie 注入 requests，尝试走 B 路线的快速下载。"""
    session = requests.Session()
    session.headers["User-Agent"] = ctx.pages[0].evaluate("navigator.userAgent") if ctx.pages else ""
    for cookie in ctx.cookies():
        session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"],
                            path=cookie.get("path", "/"))
    return session


def main() -> int:
    parser = argparse.ArgumentParser(description="浏览器会话批量抓取出版社 PDF")
    parser.add_argument("--login", action="store_true", help="打开浏览器手动登录后回车")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--publishers", default="", help="逗号分隔，如 Wiley,ACS")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--attach", default="",
                        help="连接已开的 Edge/Chrome，如 http://127.0.0.1:9222（保留登录态与扩展）")
    parser.add_argument("--edge", action="store_true", help="用本机 Edge 内核启动")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    profile_dir = EDGE_PROFILE_DIR if args.edge else PROFILE_DIR
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        attached = bool(args.attach)
        if attached:
            browser = pw.chromium.connect_over_cdp(args.attach)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        else:
            ctx = pw.chromium.launch_persistent_context(
                str(profile_dir), headless=args.headless, channel="msedge" if args.edge else None,
                accept_downloads=True, args=["--disable-blink-features=AutomationControlled"],
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if args.login:
            print("请在打开的窗口里访问图书馆或 WebVPN 并完成登录。", flush=True)
            input("请在浏览器中登录，完成后按回车继续...")
            if not attached:
                ctx.close()
            print("登录态已保存到", profile_dir)
            return 0

        rows = missing_rows(Path(DB_PATH))
        if args.publishers:
            wanted = {p.strip() for p in args.publishers.split(",") if p.strip()}
            rows = [r for r in rows if publisher_of(r["doi"]) in wanted]
        done = _load_checkpoint()
        rows = [r for r in rows if done.get(r["pmid"]) != "downloaded"]
        if args.limit:
            rows = rows[: args.limit]
        print(f"待抓取 {len(rows)} 篇", flush=True)

        session = build_session(ctx)
        counters = Counter()
        for index, row in enumerate(rows, 1):
            try:
                status = fetch_one(page, row, session)
            except Exception as exc:  # noqa: BLE001 - 单篇异常不中断整批
                status = "error"
                print(f"  {row['pmid']} 异常: {type(exc).__name__}", flush=True)
            done[row["pmid"]] = status
            counters[status] += 1
            if index % 20 == 0:
                _save_checkpoint(done)
                print(f"  {index}/{len(rows)}  {dict(counters)}", flush=True)
            time.sleep(random.uniform(1.5, 3.0))
        _save_checkpoint(done)
        print("结果：", dict(counters))
        if not attached:
            ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
