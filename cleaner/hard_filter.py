# cleaner/hard_filter.py
"""
硬过滤模块
逐条检查数据库中的文献，对不符合条件的记录打上过滤标记（写入 filter_log 表）。
过滤规则（2026-10 放宽后仅剩两条机械性排除规则 + 一条转审规则）：
  1. 标题为空                          → 排除
  2. 疑似重复标题（同期刊同年份同标题） → 排除
  3. 摘要为空                          → **不排除，转人工审核**（stage='needs_review'）

摘要为空为什么不排除：这类文献多为 PubMed 早期（1940–1980 年代）无摘要字段的记录，
或 Letter/简讯，其中确有马铃薯相关文献。模型只看标题判定不可靠，因此跳过 LLM、
直接进人工复核队列，由人判断。

已取消的规则（不要加回；这些维度改由 LLM 二次验证 + 研究方向分类承担）：
  - 语言过滤（language != 'eng'）
  - 摘要最小长度（原 ABSTRACT_MIN_LEN=80）
  - 发表年份越界（原 PUB_YEAR_MIN / PUB_YEAR_MAX）
  - 文章类型黑名单（原 EXCLUDED_ARTICLE_TYPES）
  - 标题最小长度（原 < 10 字符）
历史数据中由这些规则写下的 filter_log 记录需用 scripts/migrate_relax_hard_filter.py 清理后重跑。

filter_log 记录约定（pmid 为主键，一篇文献至多一条）：
  - stage='hard_filter'       被硬过滤排除，reason 为排除原因
  - stage='hard_filter_pass'  通过硬过滤（含历史已验证文献回填）
  - stage='needs_review'      机械规则通过但摘要为空，转人工审核（不进 LLM 验证）
  - stage='query_removed'     因查询词/年份变更被移除
"""

import sqlite3
from pathlib import Path

from config.settings import (
    DB_PATH,
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

# 转人工审核：摘要为空但其他机械规则通过。
# 与 PASS_STAGE 区分开，才能让 LLM 验证跳过它们、复核清单单独挑出它们。
REVIEW_STAGE = "needs_review"
INSERT_REVIEW_SQL = """
INSERT OR REPLACE INTO filter_log (pmid, stage, reason, filtered_at)
VALUES (?, 'needs_review', ?, ?)
"""


# ── 单条规则函数 ──────────────────────────────────────────────

def check_abstract_empty(row: sqlite3.Row) -> str | None:
    """摘要为空返回原因描述，否则 None（长度阈值已取消）"""
    abstract = (row["abstract"] or "").strip()
    if not abstract:
        return "abstract_empty"
    return None


def check_title_empty(row: sqlite3.Row) -> str | None:
    """标题为空返回原因描述，否则 None（长度阈值已取消）"""
    title = (row["title"] or "").strip()
    if not title:
        return "title_empty"
    return None


# 排除规则：命中即 stage='hard_filter'，不再进入后续流程
RULE_FUNCS = [
    check_title_empty,
]

# 转审规则：命中不排除，写 stage='needs_review'，由人工判定
# （2026-10 起摘要为空不再丢弃；这些文献通常无摘要可给模型看，跳过 LLM）
REVIEW_FUNCS = [
    check_abstract_empty,
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

class _FilterAccumulator:
    """单轮硬过滤的累加器（全量 / 增量共用）

    - reason_counts   排除原因 → 篇数（含转审原因，便于日志里同时看到）
    - filtered_pmids  stage='hard_filter' 的 PMID
    - review_pmids    stage='needs_review' 的 PMID（摘要为空，转人工）
    - log_rows        待写入的排除记录
    - review_rows     待写入的转审记录
    """

    def __init__(self):
        self.reason_counts: dict[str, int] = {}
        self.filtered_pmids: set[str] = set()
        self.review_pmids: set[str] = set()
        self.log_rows: list[tuple] = []
        self.review_rows: list[tuple] = []


def _bump(reason_counts: dict, reason: str) -> None:
    """按 '(' 前缀归一后累加原因计数"""
    reason_key = reason.split("(")[0]
    reason_counts[reason_key] = reason_counts.get(reason_key, 0) + 1


def _evaluate_row(row, dup_pmids, acc: _FilterAccumulator, now) -> None:
    """单行硬过滤判定：重复标题优先，其后 RULE_FUNCS 排除，最后 REVIEW_FUNCS 转审。

    判定顺序保证"重复标题 > 标题空 > 摘要空转审"：
    一篇既是重复标题又摘要为空的文献算 duplicate_title（真排除），
    不会被转审队列重复收录。
    """
    pmid = row["pmid"]
    if pmid in dup_pmids:
        _bump(acc.reason_counts, "duplicate_title")
        acc.filtered_pmids.add(pmid)
        acc.log_rows.append((pmid, "duplicate_title", now))
        return

    for rule_fn in RULE_FUNCS:
        reason = rule_fn(row)
        if reason:
            _bump(acc.reason_counts, reason)
            acc.filtered_pmids.add(pmid)
            acc.log_rows.append((pmid, reason, now))
            return

    for rule_fn in REVIEW_FUNCS:
        reason = rule_fn(row)
        if reason:
            _bump(acc.reason_counts, reason)
            acc.review_pmids.add(pmid)
            acc.review_rows.append((pmid, reason, now))
            return


def _log_reason_stats(reason_counts: dict, review_pmids: set[str] | None = None) -> None:
    """输出过滤原因统计（两个入口共用）"""
    logger.info("过滤原因统计：")
    for reason, cnt in sorted(reason_counts.items(), key=lambda x: -x[1]):
        logger.info(f"  {reason:<40} {cnt:>6} 篇")
    if review_pmids:
        logger.info(f"其中摘要为空转人工审核 {len(review_pmids)} 篇（未排除，不进 LLM 验证）")


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

        acc = _FilterAccumulator()
        seen_pmids: set[str] = set()

        # 清理旧的过滤/通过/转审标记（仅限硬过滤阶段，query_removed 等其他标记保留）
        conn.execute(
            "DELETE FROM filter_log WHERE stage IN ('hard_filter', ?, ?)",
            (PASS_STAGE, REVIEW_STAGE),
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
                _evaluate_row(row, dup_pmids, acc, now)

            offset += page_size
            if offset % 20000 == 0:
                logger.info(f"  已扫描 {offset} / {total} ...")

        # 批量写入过滤日志
        conn.executemany(INSERT_LOG_SQL, acc.log_rows)
        conn.executemany(INSERT_REVIEW_SQL, acc.review_rows)

        # 通过的文献写入通过标记，供增量硬过滤识别已处理
        # 转审文献单独标记，不算"通过"（它们还没被判过相关性）
        pass_rows = [
            (pmid, "passed", now)
            for pmid in seen_pmids - acc.filtered_pmids - acc.review_pmids
        ]
        conn.executemany(INSERT_PASS_SQL, pass_rows)

    passed = total - len(acc.filtered_pmids) - len(acc.review_pmids)
    logger.info(
        f"硬过滤完成：保留 {passed} 篇，过滤 {len(acc.filtered_pmids)} 篇，"
        f"转人工审核 {len(acc.review_pmids)} 篇"
    )
    _log_reason_stats(acc.reason_counts, acc.review_pmids)

    return {
        "total": total,
        "filtered": len(acc.filtered_pmids),
        "passed": passed,
        "review": len(acc.review_pmids),
        "reason_counts": acc.reason_counts,
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
            "total": 0, "filtered": 0, "passed": 0, "review": 0,
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

    acc = _FilterAccumulator()

    for row in rows:
        _evaluate_row(row, new_dup_pmids, acc, now)

    # 批量写入过滤日志（追加模式，不删除旧记录）
    # 通过的文献写入通过标记，下次增量运行不再重复处理；
    # 摘要为空的写 needs_review，下次增量同样不再重复处理，但不进 LLM 验证。
    pass_rows = [
        (pmid, "passed", now)
        for pmid in new_pmids - acc.filtered_pmids - acc.review_pmids
    ]
    if acc.log_rows or acc.review_rows or pass_rows:
        with get_conn(db_path) as conn:
            if acc.log_rows:
                conn.executemany(INSERT_LOG_SQL, acc.log_rows)
            if acc.review_rows:
                conn.executemany(INSERT_REVIEW_SQL, acc.review_rows)
            if pass_rows:
                conn.executemany(INSERT_PASS_SQL, pass_rows)

    passed = len(rows) - len(acc.filtered_pmids) - len(acc.review_pmids)
    logger.info(
        f"增量硬过滤完成：保留 {passed} 篇，过滤 {len(acc.filtered_pmids)} 篇，"
        f"转人工审核 {len(acc.review_pmids)} 篇"
    )
    _log_reason_stats(acc.reason_counts, acc.review_pmids)

    return {
        "total": len(rows),
        "filtered": len(acc.filtered_pmids),
        "passed": passed,
        "review": len(acc.review_pmids),
        "reason_counts": acc.reason_counts,
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
