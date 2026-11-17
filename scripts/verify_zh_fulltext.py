"""核对中文文献全文与题名：抽取前 3 页文本，比英文题名词覆盖率。"""
import csv
import os
import re

from pypdf import PdfReader
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_IN = os.path.join(ROOT, "data/output/handoff/中文文献待下载_20261116.csv")
OUT = os.path.join(ROOT, "data/output/handoff/中文文献全文核对_20261117.csv")


def tokens(s):
    return set(re.findall(r"[a-z]{4,}", s.lower()))


def main():
    rows = list(csv.DictReader(open(CSV_IN, encoding="utf-8-sig")))
    try:
        cn = {x["pmid"]: x.get("title", "") for x in json.load(open(os.path.join(ROOT, "data/output/cache/zh_search_all.json"), encoding="utf-8"))}
    except Exception:
        cn = {}
    out = []
    for r in rows:
        path = os.path.join(ROOT, "data/pdfs_relevant", r["pmid"] + ".pdf")
        if not os.path.exists(path):
            continue
        try:
            reader = PdfReader(path)
            text = "".join((reader.pages[i].extract_text() or "") for i in range(min(3, len(reader.pages))))
            pages = len(reader.pages)
            err = ""
        except Exception as exc:  # 加密/损坏/超大流
            text, pages, err = "", 0, type(exc).__name__
        want = tokens(r["title"])
        have = tokens(text)
        cov = len(want & have) / max(1, len(want))
        ctitle = re.sub(r"\s", "", cn.get(r["pmid"], ""))
        zhcov = None
        if ctitle:
            body = re.sub(r"\s", "", text)
            zhcov = sum(1 for ch in set(ctitle) if ch in body) / len(set(ctitle))
        if err:
            verdict = "unparsed"
        elif len(text.strip()) < 50:
            verdict = "no_text"
        elif cov >= 0.6:
            verdict = "ok"
        elif zhcov is not None and zhcov >= 0.7:
            verdict = "ok_zh"
        elif zhcov is not None and zhcov < 0.4:
            verdict = "zh_mismatch?"
        elif cov >= 0.3:
            verdict = "weak"
        elif len(re.findall(r"[\u4e00-\u9fff]", text)) > 200:
            verdict = "zh_no_en_title"
        else:
            verdict = "mismatch?"
        out.append([r["pmid"], round(cov, 2), round(zhcov, 2) if zhcov is not None else "", pages, verdict, err, r["journal"][:30], r["title"][:70]])

    with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pmid", "cover", "zhcover", "pages", "verdict", "error", "journal", "title"])
        w.writerows(out)
    from collections import Counter
    print(Counter(x[3] for x in out))
    print(OUT, len(out))


if __name__ == "__main__":
    main()
