"""Build the second-round OA targets for the built-in browser downloader."""

import csv
import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
PDF_DIR = ROOT / "data" / "pdfs_relevant"
CACHE_DIR = ROOT / "data" / "output" / "cache"
MISSING_CSV = ROOT / "data" / "output" / "exports" / "full" / "missing_relevant_remaining_20261101.csv"
CANDIDATES = CACHE_DIR / "missing_oa_candidates.json"
OLD_TARGETS = CACHE_DIR / "browser_oa_targets.json"
OLD_STATE = CACHE_DIR / "browser_oa_download_state.json"
OUTPUT = CACHE_DIR / "browser_oa_targets_round2.json"

MIN_FULLTEXT_BYTES = 10 * 1024
OA_PRIORITY = {"diamond": 0, "gold": 1, "hybrid": 2, "green": 3, "bronze": 4}


def has_fulltext(pmid: str) -> bool:
    for suffix in (".pdf", ".xml", ".txt"):
        path = PDF_DIR / f"{pmid}{suffix}"
        if path.exists() and path.stat().st_size > MIN_FULLTEXT_BYTES:
            return True
    return False


def is_usable_url(url: str) -> bool:
    if not url:
        return False
    host = urlparse(url).netloc.lower()
    if not host:
        return False
    if host.endswith(("pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov")) and "/pmc/articles/" not in url:
        return False
    return True


def url_score(url: str) -> int:
    lower = (url or "").lower()
    host = urlparse(lower).netloc
    if host.endswith(("pmc.ncbi.nlm.nih.gov", "europepmc.org")):
        return 0
    if ".pdf" in lower or "/pdf" in lower:
        return 1
    if "handle.net" in host or "eprint" in lower or "repository" in lower or "dspace" in lower:
        return 2
    return 3


def add_urls(item: dict, urls: list[str]) -> None:
    clean = []
    for url in urls:
        if is_usable_url(url) and url not in clean:
            clean.append(url)
    if clean:
        item["urls"] = sorted(clean, key=url_score)


def main() -> None:
    rows = {r["pmid"]: r for r in csv.DictReader(MISSING_CSV.open(encoding="utf-8-sig", newline=""))}
    candidates = {x["pmid"]: x for x in json.loads(CANDIDATES.read_text(encoding="utf-8"))}
    old_targets = {x["pmid"]: x for x in json.loads(OLD_TARGETS.read_text(encoding="utf-8"))}
    state = json.loads(OLD_STATE.read_text(encoding="utf-8"))
    failed = {k for k, v in state.items() if v.get("status") == "failed"}

    targets: dict[str, dict] = {}

    def ensure(pmid: str) -> dict:
        row = rows.get(pmid) or {}
        source = targets.setdefault(pmid, {
            "pmid": pmid,
            "doi": row.get("doi", ""),
            "pmc_id": row.get("pmc_id", ""),
            "title": row.get("title", ""),
            "year": row.get("year", ""),
            "journal": row.get("journal", ""),
            "urls": [],
            "source": [],
            "priority": 99,
            "oa_status": "",
        })
        return source

    for pmid, candidate in candidates.items():
        if pmid not in rows or pmid in state or not candidate.get("is_oa"):
            continue
        item = ensure(pmid)
        item["source"].append("openalex")
        item["oa_status"] = candidate.get("oa_status", "")
        add_urls(item, candidate.get("urls", []))
        if not item["urls"] and item["doi"]:
            add_urls(item, [f"https://doi.org/{item['doi']}"])
        item["priority"] = min(item["priority"], 1 + OA_PRIORITY.get(item["oa_status"], 9))

    for pmid in failed:
        if pmid not in rows or has_fulltext(pmid):
            continue
        item = ensure(pmid)
        item["source"].append("failed_retry")
        add_urls(item, (old_targets.get(pmid) or {}).get("urls", []))
        item["priority"] = min(item["priority"], 20)

    for pmid, row in rows.items():
        if has_fulltext(pmid) or not row.get("pmc_id"):
            continue
        item = ensure(pmid)
        item["source"].append("pmc")
        add_urls(item, [f"https://pmc.ncbi.nlm.nih.gov/articles/{row['pmc_id']}/"])
        item["priority"] = min(item["priority"], 0)

    # DOIs not already in OpenAlex candidate output still get one browser attempt.
    for pmid, row in rows.items():
        if has_fulltext(pmid) or pmid in targets or not row.get("doi"):
            continue
        item = ensure(pmid)
        item["source"].append("doi_lookup")
        add_urls(item, [f"https://doi.org/{row['doi']}"])
        item["priority"] = min(item["priority"], 30)

    for item in targets.values():
        item["source"] = sorted(set(item["source"]))
    result = sorted(targets.values(), key=lambda x: (x["priority"], x["pmid"]))
    result = [x for x in result if x["urls"] and not has_fulltext(x["pmid"])]

    OUTPUT.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    summary = {
        "output": str(OUTPUT),
        "targets": len(result),
        "sources": dict(Counter(src for x in result for src in x["source"])),
        "priority": dict(Counter(x["priority"] for x in result)),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
