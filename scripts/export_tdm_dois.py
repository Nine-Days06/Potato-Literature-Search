# -*- coding: utf-8 -*-
"""按出版社分组导出 TDM 申请所需的 DOI 清单。

用法：
    python scripts/export_tdm_dois.py

输出 `data/output/tdm/`：每个出版社一份 `tdm_dois_<出版社>.csv`（含 pmid / doi /
title / journal / pub_year / pmc_id），外加 `tdm_index.csv` 汇总篇数。

只把主要出版社单独成文件，其余合并进 `tdm_dois_others.csv`，避免产生上百个
只有 1-2 篇的碎文件。
"""
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

# 确保项目根目录在 Python 路径中
sys.path.insert(0, str(Path(__file__).parent.parent))

from cleaner.llm_validator import _REVIEW_PASS_PREDICATE
from config.settings import DB_PATH, OUTPUT_DIR, SUBDIR_TDM
from utils.db import get_conn

# DOI 前缀 → 出版社。只登记本项目需要单独申请的机构。
PUBLISHER_BY_PREFIX = {
    "10.1016": "Elsevier", "10.1006": "Elsevier", "10.1074": "Elsevier",
    "10.4315": "Elsevier", "10.1053": "Elsevier", "10.1054": "Elsevier",
    "10.1007": "SpringerNature", "10.1023": "SpringerNature",
    "10.1038": "SpringerNature", "10.1134": "SpringerNature",
    "10.1111": "Wiley", "10.1002": "Wiley", "10.1046": "Wiley",
    "10.2134": "Wiley", "10.1034": "Wiley",
    "10.1094": "APS",
    "10.1021": "ACS",
    "10.1093": "OUP", "10.1603": "OUP",
    "10.1080": "TaylorFrancis", "10.3109": "TaylorFrancis",
    "10.1099": "MicrobiologySociety",
    "10.1139": "CanadianSciencePublishing",
    "10.1039": "RSC",
    "10.1017": "CambridgeUP",
    "10.1271": "JSBBA",
    "10.1071": "CSIROPublishing",
    "10.1126": "AAAS",
    "10.3923": "ANSInet",
    "10.1177": "SAGE",
    "10.3390": "MDPI",
    "10.3389": "Frontiers",
    "10.1186": "BMC",
    "10.1371": "PLOS",
    "10.1155": "Hindawi",
    "10.1104": "ASPB",
    "10.1105": "ASPB",
    "10.1128": "ASM",
    "10.1073": "PNAS",
    "10.1534": "GSA",
    "10.7717": "PeerJ",
    "10.1590": "SciELO",
    "10.1159": "Karger",
}
MIN_STANDALONE = 20  # 少于这个篇数的出版社合并进 others


def publisher_of(doi: str) -> str:
    m = re.match(r"(10\.\d+)", (doi or "").strip().lower())
    if not m:
        return "unknown"
    return PUBLISHER_BY_PREFIX.get(m.group(1), "others")


sql = f"""
SELECT a.pmid, a.doi, a.title, a.journal, a.pub_year, a.pmc_id
FROM articles a JOIN llm_validation v ON a.pmid = v.pmid
WHERE {_REVIEW_PASS_PREDICATE}
  AND a.doi IS NOT NULL AND a.doi != ''
ORDER BY a.pmid
"""
with get_conn(DB_PATH) as conn:
    rows = [dict(r) for r in conn.execute(sql).fetchall()]

buckets = defaultdict(list)
for r in rows:
    buckets[publisher_of(r["doi"])].append(r)

out_dir = Path(OUTPUT_DIR) / SUBDIR_TDM
if out_dir.exists():
    for old in out_dir.glob("*.csv"):
        old.unlink()
out_dir.mkdir(parents=True, exist_ok=True)

merged_others = buckets.pop("others", [])
index_rows = []
for pub, items in buckets.items():
    if len(items) < MIN_STANDALONE:
        merged_others.extend(items)
        continue
    fname = f"tdm_dois_{pub}.csv"
    with open(out_dir / fname, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["pmid", "doi", "title", "journal", "pub_year", "pmc_id"])
        w.writeheader()
        w.writerows(items)
    index_rows.append({"publisher": pub, "count": len(items), "file": fname})

if merged_others:
    with open(out_dir / "tdm_dois_others.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["pmid", "doi", "title", "journal", "pub_year", "pmc_id"])
        w.writeheader()
        w.writerows(merged_others)
    index_rows.append({"publisher": "others", "count": len(merged_others), "file": "tdm_dois_others.csv"})

index_rows.sort(key=lambda r: -r["count"])
with open(out_dir / "tdm_index.csv", "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=["publisher", "count", "file"])
    w.writeheader()
    w.writerows(index_rows)

print(f"输出目录: {out_dir}  （{len(index_rows)} 个文件）")
print(f"{'出版社':<28}{'篇数':>8}")
for r in index_rows:
    print(f"{r['publisher']:<28}{r['count']:>8}")
print(f"{'合计':<28}{len(rows):>8}")
