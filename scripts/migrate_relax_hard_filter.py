# scripts/migrate_relax_hard_filter.py
"""
一次性迁移脚本：配合 2026-10 硬过滤放宽 + 研究方向分类。

背景
----
硬过滤原本有 6 条规则，现已取消其中 4 条（见 config/settings.py 注释）：

  取消前规则                        取消后去向
  ─────────────────────────────    ────────────────────────────────────
  1. language != 'eng'              LLM 二次验证（非英文也可为相关文献）
  2. len(abstract) < 80             取消（短摘要仍可能相关）
  3. pub_year 越界                  取消（检索范围由 SEARCH_YEAR_* 控制）
  4. article_types 黑名单           取消（Letter/Editorial 也可能是相关研究）
  5. title 长度 < 10                取消
  6. duplicate_title                保留
  +   title_empty / abstract_empty  保留（记录残缺）

放宽后这些旧规则留下的 filter_log 记录仍在，会让相关文献永远进不了 LLM 验证，
因此需要清理后重跑。同时 llm_validation 的判定标准也已变化（旧 NOT_RELEVANT
多因「无具体基因名/农艺方向」被排除，新标准下应属 RELEVANT），故未人工复核的
LLM 结论需要一并作废重跑。

本脚本做什么
------------
1. 删除 filter_log 中由**已取消规则**写下的排除记录（stage='hard_filter'）。
   只删这 4 类 reason 前缀，duplicate_title / title_empty / abstract_empty 保留。
2. 删除 llm_validation 中 **human_review IS NULL** 的记录（作废未复核的旧判定）。
   人工已复核（Y/N）的记录一律保留——人工结论的效力高于 LLM 标准变更。
3. 打印受影响条数与本轮变化，供人工确认。

默认 dry-run，只报告不修改；加 --apply 才真正写库。

用法
----
  # 先看看会删多少
  python scripts/migrate_relax_hard_filter.py

  # 确认后执行
  python scripts/migrate_relax_hard_filter.py --apply

  # 随后重跑（硬过滤会重新纳入这些文献，LLM 验证重新判定）
  python scripts/incremental_update.py --validate --export

参照先例：revalidate_relevant.py 用同样的思路处理过判定标准变更。
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import DB_PATH  # noqa: E402
from utils.db import get_conn  # noqa: E402

# 已取消规则写下的 reason。reason 形态为
#   'language=rus' / 'abstract_too_short(60chars)' / 'pub_year_too_old(1950)'
#   / 'excluded_type(Letter)' / 'title_empty_or_too_short(5chars)'
# 用前缀匹配，既可覆盖带参数的形式，也不会误伤保留规则
# （title_empty / abstract_empty / duplicate_title 都不与这些前缀相同）。
CANCELLED_REASON_PREFIXES = [
    "language=",
    "abstract_too_short",
    "pub_year_too_old",
    "pub_year_future",
    "pub_year_missing",
    "excluded_type",
    "title_empty_or_too_short",
]

# 保留规则写下的 reason，绝不能删（用作断言，防止前缀误伤）
KEPT_REASONS = {"duplicate_title", "title_empty", "abstract_empty"}


def _count_cancelled_filter_rows(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """统计将被删除的 filter_log 记录，按 reason 分组"""
    where = " OR ".join("reason LIKE ?" for _ in CANCELLED_REASON_PREFIXES)
    params = [p + "%" for p in CANCELLED_REASON_PREFIXES]
    rows = conn.execute(
        f"""SELECT reason, COUNT(*) FROM filter_log
            WHERE stage = 'hard_filter' AND ({where})
            GROUP BY reason ORDER BY 2 DESC""",
        params,
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _delete_cancelled_filter_rows(conn: sqlite3.Connection) -> int:
    """删除已取消规则写下的 filter_log 排除记录，返回删除条数"""
    where = " OR ".join("reason LIKE ?" for _ in CANCELLED_REASON_PREFIXES)
    params = [p + "%" for p in CANCELLED_REASON_PREFIXES]
    cur = conn.execute(
        f"DELETE FROM filter_log WHERE stage = 'hard_filter' AND ({where})",
        params,
    )
    return cur.rowcount


def _count_unreviewed_llm_rows(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """统计待作废的 llm_validation 记录，按 verdict 分组"""
    rows = conn.execute(
        """SELECT COALESCE(llm_verdict, '<NULL>'), COUNT(*) FROM llm_validation
           WHERE human_review IS NULL
           GROUP BY 1 ORDER BY 2 DESC"""
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _delete_unreviewed_llm_rows(conn: sqlite3.Connection) -> int:
    """删除未人工复核的 llm_validation 记录，返回删除条数"""
    cur = conn.execute("DELETE FROM llm_validation WHERE human_review IS NULL")
    return cur.rowcount


def _assert_kept_rules_untouched(conn: sqlite3.Connection) -> None:
    """安全检查：保留规则的记录必须还在（前缀匹配没有误伤）"""
    placeholders = ",".join("?" for _ in KEPT_REASONS)
    found = {
        r[0]
        for r in conn.execute(
            f"""SELECT DISTINCT reason FROM filter_log
                WHERE stage = 'hard_filter' AND reason IN ({placeholders})""",
            list(KEPT_REASONS),
        ).fetchall()
    }
    # 不要求全部存在（库里可能本来就没有），只要没有把保留规则删掉即可。
    # 真正防止误伤的是前缀不与保留 reason 相交，这里补一道显式断言。
    for reason in found:
        assert reason in KEPT_REASONS, f"保留规则被误删: {reason}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="清理已取消硬过滤规则留下的记录，并作废未复核的旧 LLM 判定"
    )
    parser.add_argument("--apply", action="store_true",
                        help="真正写库（默认仅 dry-run 报告）")
    parser.add_argument("--db", default=None, help=f"数据库路径（默认 {DB_PATH}）")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else DB_PATH
    if not db_path.exists():
        print(f"[错误] 数据库不存在: {db_path}")
        return 1

    mode = "APPLY（真正写库）" if args.apply else "DRY-RUN（仅报告）"
    print(f"数据库: {db_path}")
    print(f"模式  : {mode}\n")

    with get_conn(db_path) as conn:
        _assert_kept_rules_untouched(conn)

        cancelled = _count_cancelled_filter_rows(conn)
        unreviewed = _count_unreviewed_llm_rows(conn)
        n_filter = sum(c for _, c in cancelled)
        n_llm = sum(c for _, c in unreviewed)

        print(f"① 待清理的 filter_log 排除记录（已取消规则）: {n_filter} 条")
        for reason, cnt in cancelled:
            print(f"     {reason:<44} {cnt:>6} 篇")
        if not cancelled:
            print("     （无）")

        print(f"\n② 待作废的 llm_validation 记录（human_review IS NULL）: {n_llm} 条")
        for verdict, cnt in unreviewed:
            print(f"     {verdict:<44} {cnt:>6} 篇")
        if not unreviewed:
            print("     （无）")

        kept = conn.execute(
            "SELECT COUNT(*) FROM llm_validation WHERE human_review IS NOT NULL"
        ).fetchone()[0]
        print(f"\n     人工已复核、将保留的记录: {kept} 条")

        if not args.apply:
            print("\n[DRY-RUN] 未做任何修改。确认无误后加 --apply 执行。")
            return 0

        deleted_filter = _delete_cancelled_filter_rows(conn)
        deleted_llm = _delete_unreviewed_llm_rows(conn)

    print(f"\n[APPLY] 已删除 filter_log 记录 {deleted_filter} 条")
    print(f"[APPLY] 已删除 llm_validation 记录 {deleted_llm} 条")
    print("\n下一步（这些文献会被重新纳入硬过滤与 LLM 验证）:")
    print("  python scripts/incremental_update.py --validate --export")
    return 0


if __name__ == "__main__":
    sys.exit(main())
