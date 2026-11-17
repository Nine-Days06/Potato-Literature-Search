"""把当前缺失全文的文章导出成可点击的 HTML（DOI / PMC 链接）。"""

from __future__ import annotations

import csv
import html
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import DB_PATH, OUTPUT_DIR, SUBDIR_EXPORT_FULL
from export_missing_fulltext import collect_missing


def load_verdicts(db_path: Path) -> dict[str, tuple[str | None, str | None, str | None]]:
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        return {
            str(pmid): (verdict, human, field)
            for pmid, verdict, human, field in conn.execute(
                "SELECT pmid, llm_verdict, human_review, research_field FROM llm_validation"
            )
        }


def is_relevant(verdicts: dict, pmid: str) -> bool:
    verdict, human, _ = verdicts.get(pmid, (None, None, None))
    return human == "Y" or (human is None and verdict == "RELEVANT")


def build_html(rows: list[dict], source: str) -> str:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        groups[r["group"]].append(r)

    parts = [
        "<!doctype html><meta charset='utf-8'>",
        f"<title>待下载文献 {len(rows)} 篇</title>",
        "<style>body{font-family:system-ui;margin:2rem}li{margin:.3rem 0}"
        "h2{margin-top:1.6rem}small{color:#666}code{background:#f3f3f3;padding:0 .2rem}</style>",
        f"<h1>待下载文献 {len(rows)} 篇</h1><p><small>来源: {html.escape(source)}</small></p>",
    ]
    for prefix in sorted(groups, key=lambda p: -len(groups[p])):
        items = groups[prefix]
        parts.append(f"<h2>{html.escape(prefix)} <small>{len(items)} 篇</small></h2><ol>")
        for r in sorted(items, key=lambda x: x["year"] or ""):
            title = html.escape(r["title"] or "(无标题)")
            meta = (
                f"{html.escape(r['journal'] or '')} {html.escape(r['year'] or '')}"
                f" · PMID {html.escape(r['pmid'])}"
            )
            if r["link"]:
                title = f"<a href='{html.escape(r['link'])}' target='_blank'>{title}</a>"
            parts.append(f"<li>{title} <small>{meta}</small></li>")
        parts.append("</ol>")
    return "\n".join(parts)


def main() -> int:
    full_dir = OUTPUT_DIR / SUBDIR_EXPORT_FULL
    _, manifest = collect_missing(DB_PATH)
    verdicts = load_verdicts(DB_PATH)
    rows = []
    for r in manifest:
        doi = r["normalized_doi"]
        pmc = (r["pmc_id"] or "").strip()
        if doi:
            group, link = doi.split("/", 1)[0], f"https://doi.org/{doi}"
        elif pmc:
            group, link = "无 DOI（有 PMC）", f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc}/"
        else:
            group, link = "无 DOI（无链接）", ""
        rows.append(
            {
                "pmid": r["pmid"],
                "doi": doi,
                "pmc_id": pmc,
                "title": (r["title"] or "").strip(),
                "journal": (r["journal"] or "").strip(),
                "year": str(r["pub_year"] or ""),
                "author": (r["authors"] or "").split("|", 1)[0].strip(),
                "research_field": verdicts.get(r["pmid"], (None, None, ""))[2] or "",
                "relevant": "Y" if is_relevant(verdicts, r["pmid"]) else "N",
                "group": group,
                "link": link,
            }
        )
    stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
    csv_path = full_dir / f"missing_all_{stamp}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    out = full_dir / f"missing_links_{stamp}.html"
    out.write_text(build_html(rows, "potato_lit.db"), encoding="utf-8")
    print(f"缺失 {len(rows)} 篇（相关 {sum(1 for r in rows if r['relevant'] == 'Y')}）")
    print(f"  HTML -> {out}")
    print(f"  CSV  -> {csv_path}")
    for group, n in Counter(r["group"] for r in rows).most_common(12):
        print(f"  {group}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
