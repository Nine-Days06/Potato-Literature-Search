"""按 PMID 从中文期刊官网抓取免费全文，落盘 data/pdfs_relevant/<PMID>.pdf。

只处理 Magtech（仁和汇智）平台的刊物：DOI 直达文章页，或按英文标题分词检索后
逐条比对 citation_title[en]。付费/需登录的站点不在本脚本范围内。

用法：
    python scripts/fetch_zh_fulltext.py --journal 遗传 --limit 5
    python scripts/fetch_zh_fulltext.py                 # 全部登记的刊物
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DATA_DIR, PDF_DIR  # noqa: E402

CSV_PATH = DATA_DIR / "output" / "handoff" / "中文文献待下载_20261116.csv"
STATE_PATH = DATA_DIR / "output" / "cache" / "zh_cn_download_state.json"
MIN_PDF_BYTES = 10 * 1024

# PubMed 刊名 -> 期刊官网（Magtech 平台）
SITES = {
    "ying yong sheng tai xue bao": "https://www.cjae.net",
    "yi chuan": "https://www.chinagene.cn",
}

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    )
}

STOPWORDS = {
    "the", "of", "and", "on", "in", "for", "to", "a", "an", "with", "by",
    "from", "at", "as", "its", "their", "effect", "effects", "study", "studies",
    "analysis", "based", "using", "under", "between", "into", "during",
}

_META_PDF = re.compile(
    r'citation_pdf_url"[^>]*content="([^"]+)"', re.I
)
_META_EN_TITLE = re.compile(
    r'citation_title"\s+xml:lang="en"\s+content="([^"]*)"', re.I
)
_META_ZH_TITLE = re.compile(
    r'citation_title"\s+xml:lang="(?:zh|cn)"\s+content="([^"]*)"', re.I
)
_META_DATE = re.compile(r'citation_publication_date"[^>]*content="([^"]*)"', re.I)
_ARTICLE_ID = re.compile(
    r"(?:/CN/abstract/abstract(\d+)\.shtml|Abstract(\d+)|lsdy1\('PDF',\s*'(\d+)')"
)


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", text.lower()).strip()


def similar(pubmed_title: str, site_title: str) -> float:
    a, b = norm(pubmed_title), norm(site_title)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def tokens(title: str) -> list[str]:
    words = [w for w in norm(title).split() if len(w) >= 3 and w not in STOPWORDS]
    # 长词更有区分度，"nutilization" 这类出版物拼写差异留给相似度兜底
    return sorted(set(words), key=len, reverse=True)


class Fetcher:
    def __init__(self, sleep: float = 1.5):
        self.session = requests.Session()
        self.session.headers.update(UA)
        self.sleep = sleep

    def get(self, url: str, **kw) -> requests.Response:
        time.sleep(self.sleep)
        resp = self.session.get(url, timeout=45, **kw)
        resp.raise_for_status()
        return resp

    def post(self, url: str, data: dict) -> requests.Response:
        time.sleep(self.sleep)
        resp = self.session.post(url, data=data, timeout=45)
        resp.raise_for_status()
        return resp

    def meta_pdf(self, html: str) -> str | None:
        match = _META_PDF.search(html)
        return match.group(1) if match else None

    def by_doi(self, base: str, doi: str) -> str | None:
        resp = self.get(f"{base}/CN/{doi}")
        return self.meta_pdf(resp.text)

    def search_article_links(self, base: str, words: list[str]) -> list[str]:
        expr = " AND ".join(f"({w}[Title])" for w in words)
        sql = f"(({expr}) AND 1[Journal])"
        try:
            resp = self.post(f"{base}/CN/article/advancedSearchResult.do", {"searchSQL": sql})
        except requests.HTTPError:
            return []  # 组合过长时站点会 500，交给更短的词重试
        ids = {next(g for g in groups if g) for groups in _ARTICLE_ID.findall(resp.text)}
        return [f"/CN/abstract/abstract{i}.shtml" for i in sorted(ids, key=int)]

    def by_title(self, base: str, title: str, year: int) -> str | None:
        words = tokens(title)
        for pick in (words[:3], words[:2], words[:1]):
            best = (0.0, None)
            for link in self.search_article_links(base, pick)[:10]:
                try:
                    html = self.get(base + link).text
                except requests.RequestException:
                    continue
                en = _META_EN_TITLE.search(html)
                if not en:
                    continue
                score = similar(title, en.group(1))
                date = _META_DATE.search(html)
                site_year = date.group(1)[:4] if date and date.group(1)[:4].isdigit() else ""
                if site_year and year and abs(int(site_year) - year) > 1:
                    score -= 0.15
                if score > best[0]:
                    best = (score, html)
                if score >= 0.93:
                    return self.meta_pdf(html)
            if best[0] >= 0.88:
                return self.meta_pdf(best[1])
            if best[0] >= 0.75:
                return None  # 有近似但不确定，宁可留给人核
        return None

    def save(self, pdf_url: str, pmid: str) -> int:
        resp = self.get(pdf_url)
        content = resp.content
        if not content.startswith(b"%PDF"):
            self.explain_blocked(pdf_url)
            raise PermissionError("login_required")
        if len(content) < MIN_PDF_BYTES:
            raise ValueError(f"too_small:{len(content)}")
        target = PDF_DIR / f"{pmid}.pdf"
        part = target.with_suffix(".pdf.part")
        part.write_bytes(content)
        part.replace(target)
        return len(content)

    def explain_blocked(self, pdf_url: str) -> None:
        """PDF 链接返回 HTML 时，问 showArticleFile 到底是配额、购买还是登录。"""
        match = re.search(r"[?&]id=(\d+)", pdf_url)
        host = re.match(r"(https?://[^/]+)", pdf_url)
        if not match or not host:
            return
        try:
            html = self.post(
                f"{host.group(1)}/CN/article/showArticleFile.do",
                {"attachType": "PDF", "id": match.group(1), "json": "true"},
            ).text
        except requests.RequestException:
            return
        if "日下载量" in html:
            raise RuntimeError("daily_quota")
        if "购买" in html or "充值" in html:
            raise PermissionError("purchase_required")


def existing(pmid: str) -> Path | None:
    for suffix in (".pdf", ".xml", ".txt"):
        path = PDF_DIR / f"{pmid}{suffix}"
        if path.exists() and path.stat().st_size > MIN_PDF_BYTES:
            return path
    return None


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def journal_key(name: str) -> str:
    return re.sub(r"[\[\]]", "", name.split("=")[0]).strip().lower()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", help="只跑这个刊名（子串匹配，如 遗传）")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--sleep", type=float, default=1.5)
    args = parser.parse_args()

    rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8-sig")))
    state = load_state()
    fetcher = Fetcher(sleep=args.sleep)
    done = failed = skipped = 0
    quota: set[str] = set()

    for row in rows:
        pmid, journal = row["pmid"].strip(), journal_key(row["journal"])
        base = SITES.get(journal)
        if args.journal and args.journal.lower() not in journal:
            continue
        if not base:
            continue
        if existing(pmid):
            skipped += 1
            continue
        if state.get(pmid, {}).get("status") == "done":
            skipped += 1
            continue
        if base in quota:
            skipped += 1
            continue

        reason, size = "", 0
        try:
            pdf_url = None
            if row["doi"].strip():
                try:
                    pdf_url = fetcher.by_doi(base, row["doi"].strip())
                except requests.RequestException:
                    pdf_url = None
            if not pdf_url:
                pdf_url = fetcher.by_title(base, row["title"], int(row["year"]))
            if not pdf_url:
                reason = "no_pdf_link"
            else:
                size = fetcher.save(pdf_url, pmid)
                done += 1
                state[pmid] = {"status": "done", "file": f"{pmid}.pdf", "bytes": size}
                print(f"OK   {pmid} {size // 1024}KB {row['title'][:50]}", flush=True)
        except (PermissionError, RuntimeError) as exc:
            reason = str(exc)
        except Exception as exc:  # noqa: BLE001 - 逐条记录，不中断批处理
            reason = f"{type(exc).__name__}: {exc}"[:120]

        if reason:
            failed += 1
            state[pmid] = {"status": "failed", "reason": reason}
            print(f"FAIL {pmid} {reason[:70]} | {row['title'][:45]}", flush=True)
            if reason == "daily_quota":
                quota.add(base)
        save_state(state)
        if args.limit and done + failed >= args.limit:
            break

    save_state(state)
    print(f"完成：成功 {done}，跳过 {skipped}，失败 {failed} -> {STATE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
