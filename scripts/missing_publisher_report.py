"""汇总缺失全文文献的出版社分布，并对照华中农大订阅清单标注可得性。

用法: python scripts/missing_publisher_report.py
输出: data/output/exports/full/missing_by_publisher_20261101.csv
"""
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
MISSING = BASE / "data/output/exports/full/missing_relevant_remaining_20261101.csv"
PUBMAP = BASE / "data/output/cache/doi_publisher_map.json"
OUT = BASE / "data/output/exports/full/missing_by_publisher_20261101.csv"
SUB_OUT = BASE / "data/output/exports/full/missing_subscribed_20261101.csv"

# 华中农大订阅清单 -> 归一化的出版社/机构关键词（小写子串匹配）
SUBSCRIBED = {
    "elsevier": "Elsevier ScienceDirect (含 Cell Press)",
    "pergamon": "Elsevier ScienceDirect",
    "academic press": "Elsevier ScienceDirect",
    "mosby": "Elsevier ScienceDirect",
    "churchill livingstone": "Elsevier ScienceDirect",
    "w.b. saunders": "Elsevier ScienceDirect",
    "saunders": "Elsevier ScienceDirect",
    "cell press": "Cell Press (Elsevier)",
    "excerpta medica": "Elsevier ScienceDirect",
    "curran": "Elsevier ScienceDirect(会议论文集)",
    "wiley": "Wiley Online Library",
    "john wiley": "Wiley Online Library",
    "springer": "Springer Nature Link",
    "humana": "Springer Nature Link (Humana 丛书)",
    "springer nature": "Springer Nature Link",
    "nature portfolio": "Nature / Springer Nature Link",
    "macmillan": "Nature / Springer Nature Link",
    "nature publishing group": "Nature / Springer Nature Link",
    "taylor & francis": "Taylor & Francis Journals",
    "taylor and francis": "Taylor & Francis Journals",
    "francis group": "Taylor & Francis Journals",
    "routledge": "Taylor & Francis Journals (Routledge)",
    "oxford university press": "Oxford Journals Online",
    "oxford univ": "Oxford Journals Online",
    "cambridge university press": "Cambridge Journals",
    "cambridge univ": "Cambridge Journals",
    "cold spring harbor": "未订阅(CSHL Press)",
    "american society for microbiology": "ASM Journals",
    "annual reviews": "Annual Reviews",
    "sage": "Sage",
    "royal society of chemistry": "RSC",
    "american chemical society": "ACS Journals",
    "american association for the advancement of science": "Science (AAAS)",
    "aaas": "Science (AAAS)",
    "national academy of sciences": "PNAS",
    "bentham": "Bentham Science",
    "emerald": "Emerald Journals",
    "de gruyter": "未订阅(De Gruyter 仅电子书)",
    "institute of electrical": "IEEE",
    "ieee": "IEEE",
    "project muse": "MUSE",
    "muse": "MUSE",
    "ovid": "未订阅(OVID 仅 CABI 电子书，LWW 刊未订)",
    "cabi": "未订阅(CABI 期刊未订)",
    "cabi publishing": "未订阅(CABI 期刊未订)",
    "ebsco": "EBSCO",
    "proquest": "ProQuest",
    "informa": "Taylor & Francis Journals (Informa)",
    "myjove": "JOVE",
    "royal society": "未订阅(Royal Society Journals)",
    "scientific societies": "未订阅(APS/ASA-CSSA-SSSA 学协会)",
    "american society of plant": "未订阅(ASPP)",
    "ashs": "未订阅(ASHS HortScience)",
}


def match_pub(name: str):
    n = (name or "").lower()
    for k, v in SUBSCRIBED.items():
        if k in n:
            return None if v.startswith("未订阅") else v
    return None


def main():
    pubmap = {k.lower(): v for k, v in json.loads(PUBMAP.read_text(encoding="utf-8")).items()}
    rows = list(csv.DictReader(open(MISSING, encoding="utf-8-sig")))

    # 期刊名 -> 出版社众数（用于无 DOI 的行）
    by_journal = defaultdict(Counter)
    for r in rows:
        d = (r.get("doi") or "").strip().lower()
        if d and pubmap.get(d):
            by_journal[r["journal"].strip()][pubmap[d]] += 1
    jpub = {j: c.most_common(1)[0][0] for j, c in by_journal.items()}

    stat = defaultdict(lambda: {"n": 0, "journals": Counter(), "years": Counter()})
    for r in rows:
        d = (r.get("doi") or "").strip().lower()
        pub = pubmap.get(d) or jpub.get(r["journal"].strip(), "") or "未知(无DOI/未解析)"
        s = stat[pub]
        s["n"] += 1
        s["journals"][r["journal"].strip()] += 1
        s["years"][str(r.get("year") or "")[:4]] += 1

    ordered = sorted(stat.items(), key=lambda kv: -kv[1]["n"])
    # 校园网可补的子集：保留原 7 列，可直接喂 scripts/batch_fetch_by_doi.py
    # 含无 DOI 的行（只能按期刊名归类），HTML 里用 PubMed 链接兜底
    sub_rows = [r for r in rows if match_pub(
        pubmap.get((r.get("doi") or "").strip().lower()) or jpub.get(r["journal"].strip(), "")
    )]
    with open(SUB_OUT, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) + ["db"])
        w.writeheader()
        for r in sub_rows:
            w.writerow({**r, "db": match_pub(pubmap.get(r["doi"].strip().lower()) or jpub.get(r["journal"].strip(), ""))})
    print(f"subscribed={len(sub_rows)} (with_doi={sum(1 for r in sub_rows if r['doi'].strip())}) -> {SUB_OUT}")

    with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["publisher", "missing", "subscribed_db", "top_journals"])
        for pub, s in ordered:
            w.writerow(
                [
                    pub,
                    s["n"],
                    match_pub(pub) or "",
                    "; ".join(f"{j}({n})" for j, n in s["journals"].most_common(5)),
                ]
            )
    print(f"total={len(rows)} publishers={len(ordered)} out={OUT}")
    print("\n=== TOP 40 出版社 ===")
    for pub, s in ordered[:40]:
        db = match_pub(pub) or "未订阅/待定"
        print(f"{s['n']:5d}  {pub[:45]:45s} {db}")
    sub = sum(s["n"] for p, s in stat.items() if match_pub(p))
    print(f"\n可经订阅渠道补齐: {sub} / {len(rows)}")
    print(f"无 DOI: {sum(1 for r in rows if not (r.get('doi') or '').strip())}")

    dbs = Counter()
    for pub, s in stat.items():
        dbs[match_pub(pub) or f"未订阅 · {pub}"] += s["n"]
    print("\n=== 按订阅库汇总 ===")
    for db, n in dbs.most_common(50):
        print(f"{n:5d}  {db}")
    print("\n=== 待定出版社的期刊构成 ===")
    for pub, s in ordered[:12]:
        if match_pub(pub):
            continue
        print(f"{s['n']:5d}  {pub[:40]:40s} " + "; ".join(f"{j}({n})" for j, n in s["journals"].most_common(4)))


if __name__ == "__main__":
    main()
