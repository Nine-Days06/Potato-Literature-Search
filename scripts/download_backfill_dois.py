"""为 doi_backfill 补录 DOI 的相关文献抓全文（OpenAlex → OA 直链）。

只处理 doi_backfill 里有记录、且本地还没有全文的 pmid；每条结果记 channel，
便于后续统计「全文从哪条途径来」。
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import DB_PATH
from downloader.fulltext_library import existing_fulltext_path

CACHE = ROOT / "data/output/cache"
PDF_DIR = ROOT / "data/pdfs_relevant"
STATE = CACHE / "backfill_download_state.json"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")


def valid(buf: bytes) -> bool:
    return buf[:5] == b"%PDF-" and b"%%EOF" in buf[-2048:]


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT b.pmid, b.doi FROM doi_backfill b JOIN articles a ON a.pmid=b.pmid "
        "ORDER BY CAST(b.pmid AS INTEGER)"
    ).fetchall()
    conn.close()

    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    s = requests.Session()
    s.headers["User-Agent"] = "PotatoLit/1.0 (mailto:noreply@example.org)"
    stats = {"total": len(rows), "ok": 0, "no_oa": 0, "fail": 0, "skip": 0}

    for pmid, doi in rows:
        if existing_fulltext_path(pmid):
            stats["skip"] += 1
            continue
        # removed_repository：仓储稿被人工删除，重跑时不要再抓回来
        if state.get(pmid, {}).get("status") in ("downloaded", "no_oa", "removed_repository"):
            stats["skip"] += 1
            continue
        urls = []
        try:
            r = s.get(f"https://api.openalex.org/works/doi:{doi}", timeout=25)
            if r.status_code == 200:
                j = r.json()
                for loc in [j.get("best_oa_location") or {}] + (j.get("locations") or []):
                    u = loc.get("pdf_url")
                    if u and u not in urls:
                        urls.append(u)
        except Exception:
            pass

        saved = None
        for u in urls[:3]:
            try:
                # 部分 OA 站回 octet-stream/html，不看 content-type，只看字节
                d = s.get(u, timeout=(10, 60),
                          headers={"User-Agent": BROWSER_UA, "Referer": u.rsplit("/", 2)[0] + "/"})
                if d.status_code == 200 and valid(d.content):
                    (PDF_DIR / f"{pmid}.pdf").write_bytes(d.content)
                    saved = {"url": u, "bytes": len(d.content)}
                    break
            except Exception:
                continue
        if saved:
            state[pmid] = {"status": "downloaded", "channel": "openalex_oa", **saved, "doi": doi}
            stats["ok"] += 1
        elif urls:
            state[pmid] = {"status": "fail", "channel": "openalex_oa", "urls": urls[:3], "doi": doi}
            stats["fail"] += 1
        else:
            state[pmid] = {"status": "no_oa", "channel": "none", "doi": doi}
            stats["no_oa"] += 1
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        time.sleep(0.5)
    print(stats)


if __name__ == "__main__":
    main()
