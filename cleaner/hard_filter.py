# cleaner/hard_filter.py
"""
硬过滤模块
逐条检查数据库中的文献，对不符合条件的记录打上过滤标记（写入 filter_log 表）。
过滤规则（任一满足即过滤）：
  1. 语言不是英文（language != 'eng'）
  2. 摘要为空或过短（< ABSTRACT_MIN_LEN 字符）
  3. 发表年份超出范围
  4. 文章类型属于排除列表（Letter / Comment / Correction 等）
  5. 标题为空
  6. 疑似重复标题（同期刊同年份完全相同的标题）

filter_log 记录约定（pmid 为主键，一篇文献至多一条）：
  - stage='hard_filter'       被硬过滤排除，reason 为排除原因
  - stage='hard_filter_pass'  通过硬过滤（含历史已验证文献回填）
  - stage='query_removed'     因查询词/年份变更被移除
"""

import sqlite3
from pathlib import Path

from config.settings import (
    DB_PATH,
    ABSTRACT_MIN_LEN,
    PUB_YEAR_MIN, PUB_YEAR_MAX,
    EXCLUDED_ARTICLE_TYPES,
    LOG_DIR,
)
from utils import now_iso
from utils.db import get_conn
from utils.logger import get_logger

logger = get_logger("hard_filter", log_dir=LOG_DIR)

# 写入过滤日志
INSERT_LOG_SQL = """
INSERT OR REPLACE INTO filter_log (pmid, stage, reason, filtered_at)
VALUES (?, 'hard_filter', ?, ?)
"""

# 通过标记：硬过滤通过的 PMID 也写入 filter_log（stage='hard_filter_pass'），
# 增量硬过滤据此识别"真正未处理过"的文献，避免每轮重复扫描全部历史文献
INSERT_PASS_SQL = """
INSERT OR REPLACE INTO filter_log (pmid, stage, reason, filtered_at)
VALUES (?, 'hard_filter_pass', ?, ?)
"""
PASS_STAGE = "hard_filter_pass"


# ── 单条规则函数 ──────────────────────────────────────────────

def check_language(row: sqlite3.Row) -> str | None:
    """非英文返回原因描述，否则 None"""
    lang = (row["language"] or "").lower().strip()
    # PubMed 英文记录标记为 'eng'；也接受空值（部分记录没有语言字段）
    if lang and lang != "eng":
        return f"language={lang}"
    return None


def check_abstract(row: sqlite3.Row) -> str | None:
    abstract = (row["abstract"] or "").strip()
    if not abstract:
        return "abstract_empty"
    if len(abstract) < ABSTRACT_MIN_LEN:
        return f"abstract_too_short({len(abstract)}chars)"
    return None


def check_year(row: sqlite3.Row) -> str | None:
    year = row["pub_year"]
    if year is None:
        return "pub_year_missing"
    if year < PUB_YEAR_MIN:
        return f"pub_year_too_old({year})"
    if year > PUB_YEAR_MAX:
        return f"pub_year_future({year})"
    return None


def check_article_type(row: sqlite3.Row) -> str | None:
    types = (row["article_types"] or "").lower()
    for excl in EXCLUDED_ARTICLE_TYPES:
        if excl.lower() in types:
            return f"excluded_type({excl})"
    return None


def check_title(row: sqlite3.Row) -> str | None:
    title = (row["title"] or "").strip()
    if not title or len(title) < 10:
        return "title_empty_or_too_short"
    return None


RULE_FUNCS = [
    check_language,
    check_abstract,
    check_year,
    check_article_type,
    check_title,
]


# ── 重复标题检测 ──────────────────────────────────────────────

def find_duplicate_titles(db_path: Path = DB_PATH, conn: sqlite3.Connection = None) -> set[str]:
    """
    找出同期刊、同年份、完全相同标题（小写规范化后）的重复 PMID。
    保留最小 PMID（最早收录），其余标记为重复。
    """
    query = """
    SELECT pmid, LOWER(TRIM(title)) AS norm_title, journal, pub_year
    FROM articles
    WHERE title IS NOT NULL AND title != ''
    """
    duplicates: set[str] = set()
    seen: dict[tuple, str] = {}     # (norm_title, journal, year) → first_pmid

    if conn is not None:
        rows = conn.execute(query).fetchall()
    else:
        with get_conn(db_path) as c:
            rows = c.execute(query).fetchall()

    for row in rows:
        key = (row["norm_title"], row["journal"] or "", row["pub_year"])
        if key in seen:
            duplicates.add(row["pmid"])
        else:
            seen[key] = row["pmid"]

    return duplicates


# ── 单行判定与统计输出（全量 / 增量共用）─────────────────────

def _evaluate_row(row, dup_pmids, reason_counts, filtered_pmids, log_rows, now) -> None:
    """单行硬过滤判定：重复标题优先，其后 RULE_FUNCS 任一命中即停。

    直接累加 reason_counts（按 '(' 前缀归一）、filtered_pmids、log_rows。
    """
    pmid = row["pmid"]
    if pmid in dup_pmids:
        reason = "duplicate_title"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        filtered_pmids.add(pmid)
        log_rows.append((pmid, reason, now))
        return

    for rule_fn in RULE_FUNCS:
        reason = rule_fn(row)
        if reason:
            reason_key = reason.split("(")[0]
            reason_counts[reason_key] = reason_counts.get(reason_key, 0) + 1
            filtered_pmids.add(pmid)
            log_rows.append((pmid, reason, now))
            break


def _log_reason_stats(reason_counts: dict) -> None:
    """输出过滤原因统计（两个入口共用）"""
    logger.info("过滤原因统计：")
    for reason, cnt in sorted(reason_counts.items(), key=lambda x: -x[1]):
        logger.info(f"  {reason:<40} {cnt:>6} 篇")


# ── 主流程 ────────────────────────────────────────────────────

def run_hard_filter(db_path: Path = DB_PATH) -> dict:
    """
    对 articles 表全量扫描，将不符合条件的记录写入 filter_log。
    返回统计字典。
    """
    db_path = Path(db_path)
    logger.info("=" * 60)
    logger.info("阶段三-A：硬过滤")
    logger.info("=" * 60)

    with get_conn(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        logger.info(f"articles 表共 {total} 条记录")

        # 先找重复标题（复用同一连接）
        logger.info("检测重复标题 ...")
        dup_pmids = find_duplicate_titles(db_path, conn=conn)
        logger.info(f"发现重复标题 {len(dup_pmids)} 篇")

        reason_counts: dict[str, int] = {}
        filtered_pmids: set[str] = set()
        seen_pmids: set[str] = set()
        log_rows: list[tuple] = []

        # 清理旧的过滤/通过标记（仅限硬过滤阶段，query_removed 等其他标记保留）
        conn.execute(
            "DELETE FROM filter_log WHERE stage IN ('hard_filter', ?)",
            (PASS_STAGE,),
        )

        # 分批读取（避免全量加载到内存）
        page_size = 5000
        offset    = 0
        now       = now_iso()

        while True:
            rows = conn.execute(
                "SELECT * FROM articles LIMIT ? OFFSET ?", (page_size, offset)
            ).fetchall()

            if not rows:
                break

            for row in rows:
                seen_pmids.add(row["pmid"])
                _evaluate_row(row, dup_pmids, reason_counts, filtered_pmids, log_rows, now)

            offset += page_size
            if offset % 20000 == 0:
                logger.info(f"  已扫描 {offset} / {total} ...")

        # 批量写入过滤日志
        conn.executemany(INSERT_LOG_SQL, log_rows)

        # 通过的文献写入通过标记，供增量硬过滤识别已处理
        pass_rows = [
            (pmid, "passed", now)
            for pmid in seen_pmids - filtered_pmids
        ]
        conn.executemany(INSERT_PASS_SQL, pass_rows)

    passed = total - len(filtered_pmids)
    logger.info(f"硬过滤完成：保留 {passed} 篇，过滤 {len(filtered_pmids)} 篇")
    _log_reason_stats(reason_counts)

    return {
        "total": total,
        "filtered": len(filtered_pmids),
        "passed": passed,
        "reason_counts": reason_counts,
    }


# ── 增量硬过滤 ────────────────────────────────────────────────

def run_incremental_hard_filter(db_path: Path = DB_PATH) -> dict:
    """
    增量硬过滤：仅对"从未处理过"的 PMID 执行过滤。
    不清空 filter_log，只处理增量部分。

    新文献判定（修复后的检测逻辑）：
      1. filter_log 中无任何记录（任何 stage 均视为已处理/已移除）
      2. 且未经过 LLM 验证（llm_validation 中不存在）
    历史上已通过硬过滤并完成 LLM 验证的文献会回填 hard_filter_pass 标记
    （reason='backfilled_validated'），此后不再重复扫描。
    重复标题检测仍需全量扫描（涉及旧文献），但只标记新 PMID。
    """
    db_path = Path(db_path)
    logger.info("=" * 60)
    logger.info("阶段三-A：增量硬过滤")
    logger.info("=" * 60)

    now = now_iso()

    # 回填：已通过 LLM 验证的文献必然通过过硬过滤（验证查询排除了被过滤项），
    # 为其补写通过标记，使"新文献"检测只留下真正未处理的 PMID
    with get_conn(db_path) as conn:
        backfilled = conn.execute("""
            INSERT INTO filter_log (pmid, stage, reason, filtered_at)
            SELECT v.pmid, ?, 'backfilled_validated', ?
            FROM llm_validation v
            WHERE NOT EXISTS (
                SELECT 1 FROM filter_log f WHERE f.pmid = v.pmid
            )
        """, (PASS_STAGE, now)).rowcount
    if backfilled:
        logger.info(f"回填历史已验证文献 {backfilled} 篇（标记为 {PASS_STAGE}）")

    # 找出真正未处理的 PMID：filter_log 无任何记录且未经过 LLM 验证
    with get_conn(db_path) as conn:
        rows = conn.execute("""
            SELECT * FROM articles a
            WHERE NOT EXISTS (
                SELECT 1 FROM filter_log f WHERE f.pmid = a.pmid
            )
            AND a.pmid NOT IN (SELECT pmid FROM llm_validation)
        """).fetchall()

    if not rows:
        logger.info("无新增 PMID 需要硬过滤")
        return {
            "total": 0, "filtered": 0, "passed": 0,
            "reason_counts": {}, "backfilled": backfilled,
        }

    logger.info(f"增量硬过滤: 待处理 {len(rows)} 篇新文献")

    # 先全量检测重复标题（需对比旧文献）
    with get_conn(db_path) as conn:
        dup_pmids = find_duplicate_titles(db_path, conn=conn)
    logger.info(f"全量重复标题检测: 共 {len(dup_pmids)} 篇重复")

    # 仅保留新增 PMID 中的重复项
    new_pmids = {row["pmid"] for row in rows}
    new_dup_pmids = dup_pmids & new_pmids
    logger.info(f"其中新增 PMID 重复: {len(new_dup_pmids)} 篇")

    reason_counts: dict[str, int] = {}
    filtered_pmids: set[str] = set()
    log_rows: list[tuple] = []

    for row in rows:
        _evaluate_row(row, new_dup_pmids, reason_counts, filtered_pmids, log_rows, now)

    # 批量写入过滤日志（追加模式，不删除旧记录）
    # 通过的文献写入通过标记，下次增量运行不再重复处理
    pass_rows = [(pmid, "passed", now) for pmid in new_pmids - filtered_pmids]
    if log_rows or pass_rows:
        with get_conn(db_path) as conn:
            if log_rows:
                conn.executemany(INSERT_LOG_SQL, log_rows)
            if pass_rows:
                conn.executemany(INSERT_PASS_SQL, pass_rows)

    passed = len(rows) - len(filtered_pmids)
    logger.info(f"增量硬过滤完成：保留 {passed} 篇，过滤 {len(filtered_pmids)} 篇")
    _log_reason_stats(reason_counts)

    return {
        "total": len(rows),
        "filtered": len(filtered_pmids),
        "passed": passed,
        "reason_counts": reason_counts,
        "backfilled": backfilled,
    }


def mark_removed_pmids(db_path: Path, removed_pmids: set[str], reason: str = "query_removed"):
    """
    标记因查询词/年份变化而不再匹配的 PMID。
    写入 filter_log，stage='query_removed'，不参与后续导出。
    """
    if not removed_pmids:
        logger.info("无需标记移除的 PMID")
        return

    db_path = Path(db_path)
    now = now_iso()

    with get_conn(db_path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO filter_log (pmid, stage, reason, filtered_at) VALUES (?, ?, ?, ?)",
            [(pmid, 'query_removed', reason, now) for pmid in removed_pmids]
        )

    logger.info(f"已标记 {len(removed_pmids)} 篇 PMID 为 '{reason}'")
