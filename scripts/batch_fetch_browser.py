"""回退方案：借已登录的 Edge 批量下载（Playwright + CDP）。

先在目标机器上启动带调试端口的 Edge（先完全关闭 Edge）：
    msedge.exe --remote-debugging-port=9222 --user-data-dir=D:\\vpn_profile ^
               --no-first-run --no-default-browser-check
在同一个窗口里登录 WebVPN，然后运行：
    python batch_fetch_browser.py --input missing_relevant.csv --out pdfs
"""

from __future__ import annotations

import argparse
import csv
import random
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright


def existing(out: Path, pmid: str) -> bool:
    return any((out / f"{pmid}{s}").exists() for s in (".pdf", ".xml", ".txt"))


PDF_LINK_SELECTORS = (
    "a[href*='article-pdf']",
    "a[href*='/doi/pdf/']",
    "a[href*='pdfft']",
    "a.pdf-download",
    "a[title*='PDF' i]",
)


def parse_routes(specs: list[str]) -> list[tuple[str, str, str]]:
    """DOI前缀=站点域名=代理前缀，用于走学校 WebVPN 这类 URL 改写网关。"""
    routes = []
    for spec in specs:
        doi_prefix, host, proxy = spec.split("=", 2)
        routes.append((doi_prefix.strip(), host.strip(), proxy.strip().rstrip("/")))
    return routes


def route_for(doi: str, routes: list[tuple[str, str, str]]):
    for doi_prefix, host, proxy in routes:
        if doi.startswith(doi_prefix):
            return host, proxy
    return None


def wait_challenge(page, seconds: int = 25) -> None:
    """Cloudflare 挑战页会先返回 HTML 再自己跳转，等它过去再抓链接。"""
    for _ in range(seconds):
        if "__cf_chl" not in page.url and "Just a moment" not in page.title():
            return
        page.wait_for_timeout(1000)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("pdfs"))
    parser.add_argument("--cdp", default="http://127.0.0.1:9222")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--sleep", type=float, default=2.0)
    parser.add_argument(
        "--route",
        action="append",
        default=[],
        metavar="DOI_PREFIX=HOST=PROXY_BASE",
        help="走 URL 改写网关，如 10.1021=pubs.acs.org=http://bcyc.hzau.edu.cn/s/org/acs/pubs/G.https",
    )
    parser.add_argument("--only-routed", action="store_true", help="跳过没有匹配 --route 的文献")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    routes = parse_routes(args.route)
    with args.input.open(encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("doi", "").strip()]
    if args.only_routed:
        rows = [r for r in rows if route_for(r["doi"].strip(), routes)]
    if args.limit:
        rows = rows[: args.limit]

    ok = skip = 0
    streak = 0
    failed: list[tuple[str, str, str]] = []
    try:
        p = sync_playwright().start()
        browser = p.chromium.connect_over_cdp(args.cdp)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
        for i, row in enumerate(rows, 1):
            pmid, doi = str(row["pmid"]).strip(), row["doi"].strip()
            if existing(args.out, pmid):
                skip += 1
                continue
            try:
                route = route_for(doi, routes)
                landing = f"{route[1]}/doi/{doi}" if route else f"https://doi.org/{doi}"
                page.goto(landing, wait_until="domcontentloaded", timeout=60000)
                if "/login" in page.url:
                    raise ValueError(f"need_login: {page.url}")
                wait_challenge(page)
                if "__cf_chl" in page.url:
                    raise ValueError("cloudflare_challenge")
                pdf_url = page.evaluate(
                    "() => document.querySelector('meta[name=\"citation_pdf_url\"]')?.content || null"
                )
                if pdf_url and route:
                    # 网关页面里的 PDF 链接仍是原站地址，改写成网关前缀才能带着会话下载。
                    pdf_url = pdf_url.replace(f"https://{route[0]}", route[1]).replace(
                        f"http://{route[0]}", route[1]
                    )
                target = args.out / f"{pmid}.pdf"
                if pdf_url:
                    with page.expect_download(timeout=90000) as info:
                        page.goto(pdf_url, wait_until="commit", timeout=90000)
                else:
                    # 没有 meta 时退而点页面上的 PDF 按钮（ACS / ScienceDirect 常见）。
                    with page.expect_download(timeout=90000) as info:
                        clicked = False
                        for selector in PDF_LINK_SELECTORS:
                            if page.locator(selector).count():
                                page.locator(selector).first.click()
                                clicked = True
                                break
                        if not clicked:
                            raise ValueError("no_pdf_link")
                info.value.save_as(str(target))
                ok += 1
                streak = 0
                print(f"[{i}/{len(rows)}] OK {pmid}", flush=True)
            except (PlaywrightTimeout, Exception) as exc:  # noqa: BLE001
                detail = f"{type(exc).__name__}: {exc}"[:160]
                try:
                    detail += f" | {page.url[:90]}"
                except Exception:  # noqa: BLE001
                    pass
                failed.append((pmid, doi, detail))
                streak += 1
                print(f"[{i}/{len(rows)}] FAIL {pmid} {detail[:110]}", flush=True)
                if streak >= 3:
                    # 连续失败多半是限流或人机验证，停下来别把整个出口 IP 拖黑。
                    print("连续 3 次失败，冷却 300 秒", flush=True)
                    time.sleep(300)
                    streak = 0
            # 抖动：固定间隔的请求节奏本身就是明显的机器人特征。
            time.sleep(args.sleep * random.uniform(1.0, 1.4))
    except RuntimeError as exc:
        # Playwright 同步 API 关闭时的已知噪音，不影响已完成的下载。
        if "event loop is already running" not in str(exc):
            raise

    report = args.out.parent / "batch_fetch_failed.csv"
    with report.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["pmid", "doi", "reason"])
        writer.writerows(failed)
    print(f"完成：成功 {ok}，已存在 {skip}，失败 {len(failed)} -> {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
