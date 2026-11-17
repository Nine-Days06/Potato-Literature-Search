"""把库里没有的 DOI 从 OpenAlex/Crossref 采成一张暂存清单（只读，不写库）。

用途：盘点"外部文献库有、我们没有"的缺口时，先拿到标题/年份/期刊/摘要/OA 直链，
再决定哪些能进 LLM 验证、哪些只能转人工。

用法：
    python scripts/harvest_missing_metadata.py --coverage data/output/handoff/potato_ai_literature_coverage.csv
    python scripts/harvest_missing_metadata.py --pmid-map tmp_doi_in_pubmed.jsonl --limit 50

输出：data/output/handoff/missing_external_metadata_<时间戳>.csv（utf-8-sig）
"""

import argparse
import csv
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import OUTPUT_DIR

MAILTO = "potato-lit@example.com"
OPENALEX = "https://api.openalex.org/works/doi:"
CROSSREF = "https://api.crossref.org/works/"
FIELDS = ["doi", "title", "pub_year", "journal", "publisher", "cr_type",
          "abstract", "abstract_source", "oa_pdf_url", "oa_status", "pmid", "in_pubmed"]

_lock = threading.Lock()
_last = [0.0]


def _throttle(interval: float) -> None:
    with _lock:
        wait = interval - (time.monotonic() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()


def _inverted_to_text(index: dict | None) -> str:
    if not index:
        return ""
    slots = []
    for word, positions in index.items():
        for pos in positions:
            slots.append((pos, word))
    return " ".join(word for _, word in sorted(slots))


def openalex(doi: str) -> dict:
    _throttle(0.12)
    try:
        resp = requests.get(
            OPENALEX + doi,
            params={"mailto": MAILTO},
            timeout=30,
        )
    except requests.RequestException:
        return {}
    if resp.status_code != 200:
        return {}
    rec = resp.json()
    best = rec.get("best_oa_location") or {}
    return {
        "title": rec.get("title") or (rec.get("display_name") or ""),
        "pub_year": rec.get("publication_year"),
        "journal": ((rec.get("primary_location") or {}).get("source") or {}).get("display_name", ""),
        "publisher": ((rec.get("primary_location") or {}).get("source") or {}).get("host_organization_name", ""),
        "abstract": _inverted_to_text(rec.get("abstract_inverted_index")),
        "oa_pdf_url": best.get("pdf_url") or rec.get("open_access", {}).get("oa_url") or "",
        "oa_status": rec.get("open_access", {}).get("oa_status") or "",
        "pmid_url": (rec.get("ids") or {}).get("pmid") or "",
    }


def crossref(doi: str) -> dict:
    _throttle(0.12)
    try:
        resp = requests.get(CROSSREF + doi, params={"mailto": MAILTO}, timeout=30)
    except requests.RequestException:
        return {}
    if resp.status_code != 200:
        return {}
    msg = resp.json().get("message") or {}
    year = (msg.get("issued", {}).get("date-parts") or [[None]])[0][0]
    return {
        "title": (msg.get("title") or [""])[0],
        "pub_year": year,
        "journal": (msg.get("container-title") or [""])[0],
        "publisher": msg.get("publisher") or "",
        "abstract": msg.get("abstract") or "",
        "cr_type": msg.get("type") or "",
    }


def harvest(record: dict, pubmed_pmids: dict) -> dict:
    doi = record["site_doi"].strip().lower()
    oa = openalex(doi)
    cr = {} if (oa.get("abstract") and oa.get("journal")) else crossref(doi)
    abstract = oa.get("abstract") or cr.get("abstract") or ""
    pmid = pubmed_pmids.get(doi, "")
    return {
        "doi": doi,
        "title": oa.get("title") or cr.get("title") or record["site_title"],
        "pub_year": oa.get("pub_year") or cr.get("pub_year") or "",
        "journal": oa.get("journal") or cr.get("journal") or "",
        "publisher": oa.get("publisher") or cr.get("publisher") or "",
        "cr_type": cr.get("cr_type", ""),
        "abstract_source": "openalex" if oa.get("abstract") else ("crossref" if cr.get("abstract") else ""),
        "oa_pdf_url": oa.get("oa_pdf_url", ""),
        "oa_status": oa.get("oa_status", ""),
        "pmid": pmid,
        "in_pubmed": "Y" if pmid else "N",
        "abstract": abstract,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--coverage", default="data/output/handoff/potato_ai_literature_coverage.csv")
    parser.add_argument("--pmid-map", default="tmp_doi_in_pubmed.jsonl")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    rows = [r for r in csv.DictReader(open(args.coverage, encoding="utf-8-sig")) if r["in_db"] == "N"]
    seen, uniq = set(), []
    for row in rows:
        key = row["site_doi"].strip().lower()
        if key and key not in seen:
            seen.add(key)
            uniq.append(row)

    pubmed_pmids = {}
    map_path = Path(args.pmid_map)
    if map_path.exists():
        for line in map_path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec.get("pmids"):
                pubmed_pmids[rec["doi"]] = rec["pmids"][0]
    if args.limit:
        uniq = uniq[: args.limit]
    print(f"待采 {len(uniq)}（PubMed 反查命中 {len(pubmed_pmids)}）", flush=True)

    out = Path(OUTPUT_DIR) / "handoff" / f"missing_external_metadata_{datetime.now():%Y%m%d_%H%M%S}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS + ["abstract_chars"])
        writer.writeheader()
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for i, item in enumerate(pool.map(lambda r: harvest(r, pubmed_pmids), uniq), 1):
                row = {k: item[k] for k in FIELDS}
                row["abstract_chars"] = len(item["abstract"])
                writer.writerow(row)
                if i % 200 == 0:
                    print(f"{i}/{len(uniq)}", flush=True)

    stats = {"total": len(uniq)}
    with out.open(encoding="utf-8-sig") as fh:
        data = list(csv.DictReader(fh))
    stats["with_abstract"] = sum(1 for r in data if int(r["abstract_chars"] or 0) >= 80)
    stats["in_pubmed"] = sum(1 for r in data if r["in_pubmed"] == "Y")
    stats["oa_pdf"] = sum(1 for r in data if r["oa_pdf_url"])
    print(json.dumps(stats, ensure_ascii=False))
    print(f"写入 {out}")


if __name__ == "__main__":
    main()
