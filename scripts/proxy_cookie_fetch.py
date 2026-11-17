"""走 HZAU 电子资源代理 + 会话 Cookie 直下订阅期刊 PDF。

浏览器里 PDF 是内联渲染、不落盘；带门户 Cookie 的 HTTP 请求一次拿全字节。
Cookie 由 CDP Network.getCookies 导出，存 data/output/cache/springer_gw_cookie.txt（data/ 已忽略）。
用法：python scripts/proxy_cookie_fetch.py --vendor tandf [--limit N]
"""
import argparse
import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import DB_PATH
from downloader.fulltext_library import existing_fulltext_path, relevant_pmids

CACHE = ROOT / "data/output/cache"
PDF_DIR = ROOT / "data/pdfs_relevant"
GW = "http://bcyc.hzau.edu.cn"

# vendor -> (DOI 前缀, PDF 地址前缀, 后缀)
VENDORS = {
    "springer": ("10.1007", f"{GW}/s/com/springer/link/G.https/content/pdf/", ".pdf"),
    "tandf": ("10.1080", f"{GW}/s/com/tandfonline/www/G.https/doi/pdf/", ""),
    "asm": ("10.1128", f"{GW}/s/org/asm/journals/G.https/doi/pdf/", "?download=true"),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor", choices=sorted(VENDORS), required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=2.0)
    args = ap.parse_args()

    prefix, base, suffix = VENDORS[args.vendor]
    cookie = (CACHE / "springer_gw_cookie.txt").read_text(encoding="utf-8").strip()
    state_path = CACHE / f"proxy_progress_{args.vendor}.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

    rel = relevant_pmids(DB_PATH)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        rows = conn.execute("SELECT pmid, doi FROM articles ORDER BY CAST(pmid AS INTEGER)").fetchall()
    targets = [(str(p), (d or "").strip()) for p, d in rows
               if str(p) in rel and (d or "").strip().lower().startswith(prefix)
               and not existing_fulltext_path(str(p))]

    s = requests.Session()
    s.headers.update({
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36",
        "Referer": f"{GW}/",
    })

    stats = {"total": len(targets), "ok": 0, "no_access": 0, "fail": 0}
    done = 0
    for pmid, doi in targets:
        if args.limit and done >= args.limit:
            break
        done += 1
        try:
            r = s.get(base + quote(doi, safe="/") + suffix, timeout=(15, 60), stream=True)
            ctype = (r.headers.get("Content-Type") or "").lower()
            if r.status_code != 200 or "pdf" not in ctype:
                state[pmid] = {"status": "no_access", "code": r.status_code,
                               "ctype": ctype.split(";")[0], "doi": doi}
                stats["no_access"] += 1
            else:
                buf = b"".join(r.iter_content(65536))
                if buf[:5] == b"%PDF-" and b"%%EOF" in buf[-2048:]:
                    (PDF_DIR / f"{pmid}.pdf").write_bytes(buf)
                    state[pmid] = {"status": "downloaded", "bytes": len(buf), "doi": doi}
                    stats["ok"] += 1
                else:
                    state[pmid] = {"status": "invalid", "bytes": len(buf), "doi": doi}
                    stats["fail"] += 1
        except Exception as e:  # 单篇失败不该中断整批
            state[pmid] = {"status": "error", "error": str(e)[:120], "doi": doi}
            stats["fail"] += 1
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        time.sleep(args.delay)
    print(stats)


if __name__ == "__main__":
    main()
