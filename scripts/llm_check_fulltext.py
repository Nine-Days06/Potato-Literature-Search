"""用 LLM 独立复核：数据库标题+摘要 vs pypdf 抽出的全文，判断是否对应、是否完整。

不依赖规则法的结论（不传 ngram/cov），避免模型为已有判断找理由。

    python scripts/llm_check_fulltext.py --limit 5      # 先验 5 篇
    python scripts/llm_check_fulltext.py                # weak 全量 334 篇
    python scripts/llm_check_fulltext.py --csv data/pdf_review/mismatch/_review.csv

默认走本地网关（group/ds4.1，无需 key）。结果写 fulltext_llm_check_*.csv，
中断重跑会跳过已完成的 PMID。
"""

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import DB_PATH, OUTPUT_DIR, PDF_DIR, SUBDIR_FAILURE
from downloader.fulltext_library import logger
from utils.db import get_conn

SYSTEM_PROMPT = """你是文献全文核对员。用户会给出一条数据库文献记录（标题、摘要、DOI、期刊、年份）和一份 PDF 提取出的全文文本。
判断这份 PDF 是否就是这条记录对应的文献，以及 PDF 内容是否完整。

规则：
1. 只依据给定文本判断，禁止用外部知识补全。必须从 PDF 文本中原样摘出一句证据（标题、DOI、作者、期刊或摘要中的连续句子）放进 evidence。
2. 标题可能被改写、翻译或属于预印本/不同版本。若摘要内容与 PDF 正文一致，判 MATCH，并在 reason 说明依据。
3. completeness 取值：
   FULL = 能看到摘要/引言、正文主体、结尾（参考文献或结论）；
   PARTIAL = 只有部分章节，明显缺头或缺尾；
   ABSTRACT_ONLY = 只有摘要、封面或版权页；
   UNKNOWN = 文本太少，无法判断。
4. 只输出一个 JSON 对象，不要 markdown、不要解释。

输出字段：
{"identity": "MATCH|MISMATCH|UNSURE", "completeness": "FULL|PARTIAL|ABSTRACT_ONLY|UNKNOWN", "evidence": "原文摘录", "reason": "一句话依据"}"""

OUT_FIELDS = [
    "pmid", "identity", "completeness", "evidence", "reason",
    "pdf_chars", "truncated", "model", "elapsed_s",
]


def _output_dir(subdir: str) -> Path:
    return Path(OUTPUT_DIR) / subdir


def extract_pdf_text(path: Path, max_chars: int) -> tuple[str, int, bool]:
    """返回 (文本, 原始字符数, 是否截断)。逐页容错：后面某页坏了也保留已抽到的部分。"""
    from pypdf import PdfReader

    reader = PdfReader(str(path), strict=False)
    parts = []
    index = 0
    while True:
        try:
            page = reader.pages[index]
            parts.append(page.extract_text() or "")
            index += 1
        except IndexError:
            break
        except Exception:  # noqa: BLE001 - 单页失败保留前面已抽到的文本
            break
    text = "\n".join(parts)
    # pypdf 偶尔抽出孤立代理字符，JSON/HTTP 编码会直接抛 UnicodeEncodeError。
    text = text.encode("utf-8", "ignore").decode("utf-8")
    total = len(text)
    if total <= max_chars:
        return text, total, False
    head = text[: max_chars // 2]
    tail = text[-(max_chars // 2):]
    return f"{head}\n\n[...中间省略...]\n\n{tail}", total, True


def build_prompt(record: dict, text: str) -> str:
    return f"""## 数据库记录
PMID: {record['pmid']}
标题: {record.get('title') or ''}
摘要: {record.get('abstract') or ''}
DOI: {record.get('doi') or ''}
期刊/年份: {record.get('journal') or ''} / {record.get('pub_year') or ''}

## PDF 提取文本
{text}

## 任务
判断这份 PDF 是否对应上面的数据库记录，以及内容是否完整。只输出 JSON。"""


def parse_json_object(text: str) -> dict | None:
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        s = s.rsplit("```", 1)[0] if "```" in s else s
    start = s.find("{")
    if start == -1:
        return None
    for end in range(len(s) - 1, start, -1):
        if s[end] != "}":
            continue
        try:
            obj = json.loads(s[start:end + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def call_llm(client, model: str, record: dict, text: str, retries: int = 3) -> tuple[dict | None, str]:
    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_prompt(record, text)},
                ],
                temperature=0,
                max_tokens=1200,
            )
            content = resp.choices[0].message.content or ""
            obj = parse_json_object(content)
            if obj:
                return obj, ""
            last_error = f"bad_json: {content[:120]}"
        except Exception as exc:  # noqa: BLE001 - 单篇失败不中断整批
            last_error = f"{type(exc).__name__}: {exc}"[:160]
            time.sleep(2 ** attempt)
    return None, last_error


def latest_llm_csv() -> Path | None:
    files = list(_output_dir(SUBDIR_FAILURE).glob("fulltext_llm_check_*.csv"))
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=Path("data/pdf_review/weak/_review.csv"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--pmids", default="", help="逗号分隔，只跑这些 PMID")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--model", default="group/ds4.1")
    parser.add_argument("--base-url", default="http://127.0.0.1:3425/v1")
    parser.add_argument("--max-chars", type=int, default=60000)
    parser.add_argument("--resume", action="store_true", help="跳过上一份结果里已有的 PMID")
    args = parser.parse_args()

    from openai import OpenAI

    rows = list(csv.DictReader(open(args.csv, encoding="utf-8-sig")))
    if args.pmids:
        wanted = {p.strip() for p in args.pmids.split(",") if p.strip()}
        rows = [r for r in rows if r["pmid"] in wanted]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        logger.info(f"{args.csv} 为空")
        return 0

    done: dict[str, dict] = {}
    previous = latest_llm_csv() if args.resume else None
    if previous:
        done = {
            r["pmid"]: r
            for r in csv.DictReader(open(previous, encoding="utf-8-sig"))
            if r["identity"] != "ERROR"
        }
        rows = [r for r in rows if r["pmid"] not in done]
        logger.info(f"续跑：跳过已完成的 {len(done)} 篇")

    with get_conn(DB_PATH) as conn:
        conn.row_factory = None
        db_rows = {
            str(pmid): {"pmid": str(pmid), "title": title, "abstract": abstract,
                        "doi": doi, "journal": journal, "pub_year": year}
            for pmid, title, abstract, doi, journal, year in conn.execute(
                "SELECT pmid, title, abstract, doi, journal, pub_year FROM articles"
            )
        }

    logger.info(f"待复核 {len(rows)} 篇，model={args.model}")
    started = time.time()
    client = OpenAI(base_url=args.base_url, api_key="local")
    result = list(done.values())
    failed = 0

    def work(row: dict) -> dict:
        pmid = row["pmid"]
        pdf_path = Path(PDF_DIR) / row["file"]
        record = db_rows.get(pmid, {"pmid": pmid})
        t0 = time.time()
        try:
            text, total, truncated = extract_pdf_text(pdf_path, args.max_chars)
        except Exception as exc:  # noqa: BLE001
            return {"pmid": pmid, "identity": "ERROR", "completeness": "UNKNOWN",
                    "evidence": "", "reason": f"extract_error: {type(exc).__name__}",
                    "pdf_chars": "", "truncated": "", "model": args.model,
                    "elapsed_s": round(time.time() - t0, 1)}
        obj, error = call_llm(client, args.model, record, text)
        if obj is None:
            return {"pmid": pmid, "identity": "ERROR", "completeness": "UNKNOWN",
                    "evidence": "", "reason": error, "pdf_chars": total,
                    "truncated": truncated, "model": args.model,
                    "elapsed_s": round(time.time() - t0, 1)}
        return {
            "pmid": pmid,
            "identity": str(obj.get("identity", "")).upper(),
            "completeness": str(obj.get("completeness", "")).upper(),
            "evidence": str(obj.get("evidence", ""))[:500],
            "reason": str(obj.get("reason", ""))[:300],
            "pdf_chars": total,
            "truncated": truncated,
            "model": args.model,
            "elapsed_s": round(time.time() - t0, 1),
        }

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, row): row for row in rows}
        for i, future in enumerate(as_completed(futures), 1):
            row = future.result()
            result.append(row)
            if row["identity"] == "ERROR":
                failed += 1
            if i % 20 == 0:
                logger.info(f"进度 {i}/{len(rows)}，{time.time() - started:.0f}s，失败 {failed}")

    out = _output_dir(SUBDIR_FAILURE) / f"fulltext_llm_check_{datetime.now():%Y%m%d_%H%M%S}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUT_FIELDS)
        writer.writeheader()
        writer.writerows(result)

    from collections import Counter
    logger.info(f"完成 {len(result)} 篇，耗时 {time.time() - started:.0f}s")
    logger.info(f"  identity: {dict(Counter(r['identity'] for r in result))}")
    logger.info(f"  completeness: {dict(Counter(r['completeness'] for r in result))}")
    logger.info(f"结果: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
