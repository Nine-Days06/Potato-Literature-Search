# cleaner/relevance_scorer.py
"""
相关性评分模块
对通过硬过滤的文献进行两级评分：
  1. 关键词命中计数（快速，可解释）
  2. TF-IDF 向量相似度（可选，用于辅助排序）

评分逻辑：
  - 分别统计 gene_hits / function_hits / trait_hits
  - 三类都有命中 → has_all_three = 1
  - 总分 = gene_hits + function_hits + trait_hits（上限截断为 20）
  - label 分级：
      高相关 = has_all_three AND total_score >= HIGH_SCORE_THRESHOLD
      中相关 = has_all_three OR total_score >= MID_SCORE_THRESHOLD
      低相关 = 其余

最终结果写入 relevance_scores 表，并导出 CSV。
"""

import re
import csv
from datetime import datetime
from pathlib import Path

from config.settings import (
    DB_PATH, OUTPUT_DIR,
    GENE_TERMS, FUNCTION_TERMS, TRAIT_TERMS,
    HIGH_SCORE_THRESHOLD, MID_SCORE_THRESHOLD,
)
from utils.db import get_conn
from utils.logger import get_logger

logger = get_logger("relevance_scorer")

INSERT_SCORE_SQL = """
INSERT OR REPLACE INTO relevance_scores
  (pmid, gene_hits, function_hits, trait_hits,
   total_score, has_all_three, label)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""


# ── 预编译关键词正则（全词匹配，忽略大小写） ─────────────────

def _build_combined_pattern(terms: list[str]) -> re.Pattern:
    """将所有词条合并为单一正则，一次扫描完成全部匹配"""
    return re.compile(
        r"\b(?:" + "|".join(re.escape(t) for t in terms) + r")\b",
        re.IGNORECASE
    )


GENE_PATTERN     = _build_combined_pattern(GENE_TERMS)
FUNCTION_PATTERN = _build_combined_pattern(FUNCTION_TERMS)
TRAIT_PATTERN    = _build_combined_pattern(TRAIT_TERMS)


def _count_hits(text: str, pattern: re.Pattern) -> int:
    """返回命中的唯一词条数量（每个词条只计 1 次，不管出现多少次）"""
    return len(set(pattern.findall(text)))


# ── 单篇评分 ─────────────────────────────────────────────────

def score_record(row) -> tuple:
    """
    对单条 articles 记录评分。
    row 需包含 pmid / title / abstract / keywords / mesh_terms。
    返回 (pmid, gene_hits, function_hits, trait_hits, total, has_all_three, label)
    """
    pmid = row["pmid"]
    text = " ".join(filter(None, [
        row["title"]      or "",
        row["abstract"]   or "",
        row["keywords"]   or "",
        row["mesh_terms"] or "",
    ]))

    g  = _count_hits(text, GENE_PATTERN)
    f  = _count_hits(text, FUNCTION_PATTERN)
    tr = _count_hits(text, TRAIT_PATTERN)

    total         = min(g + f + tr, 20)   # 截断上限，避免极端值影响分析
    has_all_three = int(g > 0 and f > 0 and tr > 0)

    if has_all_three and total >= HIGH_SCORE_THRESHOLD:
        label = "高相关"
    elif has_all_three or total >= MID_SCORE_THRESHOLD:
        label = "中相关"
    else:
        label = "低相关"

    return (pmid, g, f, tr, total, has_all_three, label)


# ── 批量评分 ──────────────────────────────────────────────────

def run_relevance_scoring(
    db_path: Path = DB_PATH,
    page_size: int = 2000,
) -> dict:
    """
    对通过硬过滤的全部文献评分，结果写入 relevance_scores 表。
    使用单次 JOIN + 游标分页，避免两次全表扫描。
    """
    db_path = Path(db_path)
    logger.info("=" * 60)
    logger.info("阶段三-B：相关性评分")
    logger.info("=" * 60)

    query = """
        SELECT a.pmid, a.title, a.abstract, a.keywords, a.mesh_terms
        FROM articles a
        WHERE NOT EXISTS (
            SELECT 1 FROM filter_log f
            WHERE f.pmid = a.pmid AND f.stage = 'hard_filter'
        )
        ORDER BY a.pmid
        LIMIT ? OFFSET ?
    """

    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM relevance_scores")

        processed     = 0
        offset        = 0
        label_counts  = {"高相关": 0, "中相关": 0, "低相关": 0}

        while True:
            rows = conn.execute(query, (page_size, offset)).fetchall()
            if not rows:
                break

            score_rows = [score_record(row) for row in rows]
            conn.executemany(INSERT_SCORE_SQL, score_rows)

            for sr in score_rows:
                label_counts[sr[6]] += 1

            processed += len(rows)
            offset    += page_size

            if processed % 10000 == 0:
                logger.info(f"  评分进度: {processed}")

    total = processed
    logger.info(f"待评分文献：{total} 篇")

    logger.info("评分完成，分布：")
    for label, cnt in label_counts.items():
        pct = cnt / total * 100 if total else 0
        logger.info(f"  {label}: {cnt} 篇 ({pct:.1f}%)")

    return {"total": total, "label_counts": label_counts}


# ── 导出 CSV ──────────────────────────────────────────────────

def export_csv(db_path: Path = DB_PATH, out_dir: Path = OUTPUT_DIR) -> dict[str, Path]:
    """
    将高相关和中相关文献导出为 CSV 文件。
    每行包含完整字段 + 评分字段，便于后续 NER 工具直接读取。
    """
    db_path = Path(db_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    query = """
    SELECT
        a.pmid, a.title, a.abstract, a.keywords, a.mesh_terms,
        a.pub_year, a.journal, a.doi, a.pmc_id,
        a.article_types, a.authors, a.affiliation, a.language,
        s.gene_hits, s.function_hits, s.trait_hits,
        s.total_score, s.has_all_three, s.label
    FROM articles a
    JOIN relevance_scores s ON a.pmid = s.pmid
    WHERE s.label = ?
    ORDER BY s.total_score DESC
    """

    fieldnames = [
        "pmid", "title", "abstract", "keywords", "mesh_terms",
        "pub_year", "journal", "doi", "pmc_id",
        "article_types", "authors", "affiliation", "language",
        "gene_hits", "function_hits", "trait_hits",
        "total_score", "has_all_three", "label",
    ]

    output_paths: dict[str, Path] = {}

    with get_conn(db_path) as conn:
        for label in ("高相关", "中相关", "低相关"):
            safe_name  = {"高相关": "high", "中相关": "mid", "低相关": "low"}[label]
            out_path   = out_dir / f"{safe_name}_relevance.csv"

            rows = conn.execute(query, (label,)).fetchall()

            with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for row in rows:
                    writer.writerow(dict(row))

            logger.info(f"导出 {label} → {out_path}  ({len(rows)} 篇)")
            output_paths[label] = out_path

    return output_paths


# ── 生成清洗报告 ──────────────────────────────────────────────

def write_clean_report(
    hard_stats: dict,
    score_stats: dict,
    out_dir: Path = OUTPUT_DIR,
) -> Path:
    """
    将各阶段统计写为可读报告文本文件。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "clean_report.txt"
    lines = [
        "=" * 60,
        "马铃薯文献清洗报告",
        f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 60,
        "",
        "【阶段二 · 硬过滤】",
        f"  原始文献总数:  {hard_stats.get('total', 0):>8} 篇",
        f"  过滤后剩余:    {hard_stats.get('passed', 0):>8} 篇",
        f"  过滤掉:        {hard_stats.get('filtered', 0):>8} 篇",
        "",
        "  过滤原因明细：",
    ]
    for reason, cnt in sorted(
        hard_stats.get("reason_counts", {}).items(), key=lambda x: -x[1]
    ):
        lines.append(f"    {reason:<40} {cnt:>6} 篇")

    lines += [
        "",
        "【阶段三 · 相关性评分】",
        f"  参与评分文献:  {score_stats.get('total', 0):>8} 篇",
    ]
    for label, cnt in score_stats.get("label_counts", {}).items():
        total = score_stats.get("total", 1)
        lines.append(
            f"  {label}:          {cnt:>8} 篇  ({cnt/total*100:.1f}%)"
        )

    lines += ["", "=" * 60]

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logger.info(f"清洗报告已写入 {report_path}")
    return report_path
