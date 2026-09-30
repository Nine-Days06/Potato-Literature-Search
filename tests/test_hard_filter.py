import re
import unittest
import tempfile
from pathlib import Path

from cleaner.hard_filter import run_hard_filter, run_incremental_hard_filter
from utils.db import init_db, get_conn
from config.settings import ABSTRACT_MIN_LEN, PUB_YEAR_MIN, PUB_YEAR_MAX

# 安全的测试年份：位于过滤范围内部
TEST_YEAR = max(PUB_YEAR_MIN + 1, min(2020, PUB_YEAR_MAX - 1))


def _add_article(conn, pmid, title=None, abstract="x", year=TEST_YEAR,
                 language="eng", article_types="Journal Article"):
    """插入一篇文章。abstract 默认无效（过短），需显式传入合格摘要才能通过硬过滤"""
    conn.execute(
        """INSERT OR IGNORE INTO articles
           (pmid, title, abstract, pub_year, journal, language, article_types)
           VALUES (?, ?, ?, ?, 'J Test', ?, ?)""",
        (pmid,
         title or f"Potato research article number {pmid}",
         abstract,
         year, language, article_types),
    )


def _good_abstract():
    return "This is a sufficiently long abstract about potato biology. " * 3


def _mark_validated(conn, pmid):
    conn.execute(
        """INSERT OR REPLACE INTO llm_validation
           (pmid, llm_verdict, reason, validated_at, human_review)
           VALUES (?, 'RELEVANT', 'test', '2026-01-01T00:00:00', NULL)""",
        (pmid,),
    )


def _mark_filter_log(conn, pmid, stage, reason="test"):
    conn.execute(
        "INSERT OR REPLACE INTO filter_log (pmid, stage, reason, filtered_at) "
        "VALUES (?, ?, ?, '2026-01-01T00:00:00')",
        (pmid, stage, reason),
    )


class HardFilterTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        init_db(self.db_path)

    def tearDown(self):
        self._tmp.cleanup()

    def _log_rows(self, pmid=None):
        with get_conn(self.db_path) as conn:
            if pmid:
                return conn.execute(
                    "SELECT * FROM filter_log WHERE pmid = ?", (pmid,)
                ).fetchall()
            return conn.execute("SELECT * FROM filter_log ORDER BY pmid").fetchall()


class TestIncrementalDetection(HardFilterTestBase):
    """增量硬过滤的"新文献"检测：只处理真正未处理过的 PMID"""

    def test_backfills_legacy_validated_articles(self):
        """历史已验证（通过硬过滤→LLM 验证）的文献应回填通过标记，不计入待处理"""
        with get_conn(self.db_path) as conn:
            _add_article(conn, "100", abstract=_good_abstract())
            _mark_validated(conn, "100")

        stats = run_incremental_hard_filter(self.db_path)

        self.assertEqual(stats.get("backfilled"), 1)
        self.assertEqual(stats["total"], 0)
        rows = self._log_rows("100")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stage"], "hard_filter_pass")
        self.assertEqual(rows[0]["reason"], "backfilled_validated")

    def test_only_truly_new_articles_are_processed(self):
        """候选 = 无 filter_log 记录（任何 stage）且未经过 LLM 验证的文献"""
        with get_conn(self.db_path) as conn:
            # 历史已验证 → 回填，不处理
            _add_article(conn, "100", abstract=_good_abstract())
            _mark_validated(conn, "100")
            # 曾被硬过滤排除 → 跳过
            _add_article(conn, "101", abstract="")
            _mark_filter_log(conn, "101", "hard_filter", "abstract_empty")
            # 曾被查询词移除 → 跳过（保留原标记）
            _add_article(conn, "102", abstract=_good_abstract())
            _mark_filter_log(conn, "102", "query_removed")
            # 真正新增、合格 → 处理并通过
            _add_article(conn, "103", abstract=_good_abstract())
            # 真正新增、不合格 → 处理并排除
            _add_article(conn, "104", abstract="")

        stats = run_incremental_hard_filter(self.db_path)

        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["filtered"], 1)
        self.assertEqual(stats["passed"], 1)

        # 103 通过 → 写入通过标记
        rows103 = self._log_rows("103")
        self.assertEqual(len(rows103), 1)
        self.assertEqual(rows103[0]["stage"], "hard_filter_pass")
        # 104 摘要为空 → 写入排除标记
        rows104 = self._log_rows("104")
        self.assertEqual(len(rows104), 1)
        self.assertEqual(rows104[0]["stage"], "hard_filter")
        self.assertEqual(rows104[0]["reason"], "abstract_empty")
        # 102 的 query_removed 标记未被覆盖
        rows102 = self._log_rows("102")
        self.assertEqual(len(rows102), 1)
        self.assertEqual(rows102[0]["stage"], "query_removed")

    def test_second_run_is_truly_incremental(self):
        """通过的文献下次运行不再重复处理（通过标记生效）"""
        with get_conn(self.db_path) as conn:
            _add_article(conn, "200", abstract=_good_abstract())

        first = run_incremental_hard_filter(self.db_path)
        self.assertEqual(first["total"], 1)

        second = run_incremental_hard_filter(self.db_path)
        self.assertEqual(second["total"], 0)


class TestFullHardFilterPassMarkers(HardFilterTestBase):
    """全量硬过滤应写入通过标记，并在重跑时清理过期标记"""

    def test_full_filter_writes_pass_and_fail_markers(self):
        with get_conn(self.db_path) as conn:
            _add_article(conn, "300", abstract=_good_abstract())
            _add_article(conn, "301", abstract="")

        stats = run_hard_filter(self.db_path)

        self.assertEqual(stats["passed"], 1)
        self.assertEqual(stats["filtered"], 1)
        rows300 = self._log_rows("300")
        self.assertEqual(len(rows300), 1)
        self.assertEqual(rows300[0]["stage"], "hard_filter_pass")
        rows301 = self._log_rows("301")
        self.assertEqual(rows301[0]["stage"], "hard_filter")

    def test_full_filter_removes_stale_pass_marker(self):
        """重跑全量过滤时，已失效的通过标记必须被清除"""
        with get_conn(self.db_path) as conn:
            _add_article(conn, "400", abstract=_good_abstract())
            _mark_filter_log(conn, "400", "hard_filter_pass", "old")

        # 先通过，再让文献失效
        run_hard_filter(self.db_path)
        with get_conn(self.db_path) as conn:
            conn.execute("UPDATE articles SET abstract='' WHERE pmid='400'")

        run_hard_filter(self.db_path)

        rows = self._log_rows("400")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stage"], "hard_filter")
        self.assertEqual(rows[0]["reason"], "abstract_empty")


class TestRuleEvaluationStats(HardFilterTestBase):
    """单行规则判定与原因统计输出的行为锁定（供 _evaluate_row / _log_reason_stats 抽取回归）"""

    def _seed_reason_stats_fixture(self, conn):
        """
        构造"原因统计"固定数据（下面 3 个测试共用）：
          - 501：摘要过短 → 规则命中 abstract_too_short(5chars)
          - 502 / 503：同期刊、同年份、标题仅大小写/首尾空白不同的重复对
            （find_duplicate_titles 按 LOWER(TRIM(title)) 归一化，视为重复）
            502 先插入 → 保留；503 后插入 → 标记 duplicate_title。
            503 摘要同样过短，用于锁定"重复标题优先于规则"。
        返回 (short_pmid, kept_dup_pmid, dup_pmid)
        """
        dup_title_kept   = "  Potato tuber Duplication Study With A Long Title  "
        dup_title_second = "potato tuber duplication study with a long title"
        _add_article(conn, "501", abstract="short")
        _add_article(conn, "502", title=dup_title_kept, abstract=_good_abstract())
        _add_article(conn, "503", title=dup_title_second, abstract="tiny")
        return "501", "502", "503"

    def test_reason_counts_keys_are_normalized_and_dedup_priority_wins(self):
        """统计键按 '(' 前缀归一；重复标题先于规则命中；保留的重复伙伴不被重复计数"""
        with get_conn(self.db_path) as conn:
            short_pmid, kept_pmid, dup_pmid = self._seed_reason_stats_fixture(conn)
            # 一篇完全合格的文献
            _add_article(conn, "504", abstract=_good_abstract())

        stats = run_hard_filter(self.db_path)

        # 503 摘要同样过短，但因重复标题优先 → 计入 duplicate_title 而非 abstract_too_short
        self.assertEqual(
            stats["reason_counts"],
            {"abstract_too_short": 1, "duplicate_title": 1},
        )
        self.assertEqual(stats["total"], 4)
        self.assertEqual(stats["filtered"], 2)
        # 保留 2 篇：先插入的重复伙伴 + 完全合格的一篇
        self.assertEqual(stats["passed"], 2)

        # filter_log 保留未归一的原始原因，统计键才做 '(' 归一
        rows_short = self._log_rows(short_pmid)
        self.assertEqual(rows_short[0]["stage"], "hard_filter")
        self.assertEqual(rows_short[0]["reason"], "abstract_too_short(5chars)")
        rows_dup = self._log_rows(dup_pmid)
        self.assertEqual(rows_dup[0]["stage"], "hard_filter")
        self.assertEqual(rows_dup[0]["reason"], "duplicate_title")
        # 先插入的重复伙伴保留通过，且不进入任何计数
        rows_kept = self._log_rows(kept_pmid)
        self.assertEqual(rows_kept[0]["stage"], "hard_filter_pass")

    def test_incremental_counts_only_new_duplicates(self):
        """增量路径只统计新增重复项：已标记的较早孪生不参与候选、不被重复计数"""
        with get_conn(self.db_path) as conn:
            dup_title_kept   = "  Potato tuber Duplication Study With A Long Title  "
            dup_title_second = "potato tuber duplication study with a long title"
            _add_article(conn, "502", title=dup_title_kept, abstract=_good_abstract())
            _add_article(conn, "503", title=dup_title_second, abstract="tiny")
            # 较早孪生已处理过 → 不是候选
            _mark_filter_log(conn, "502", "hard_filter")

        stats = run_incremental_hard_filter(self.db_path)

        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["filtered"], 1)
        self.assertEqual(stats["passed"], 0)
        self.assertEqual(stats["reason_counts"], {"duplicate_title": 1})

        # 新增孪生被标记为重复（而非其自身的摘要过短）
        rows_dup = self._log_rows("503")
        self.assertEqual(rows_dup[0]["stage"], "hard_filter")
        self.assertEqual(rows_dup[0]["reason"], "duplicate_title")
        # 预先标记的记录未被本轮覆盖，也未被计入本轮统计
        rows_kept = self._log_rows("502")
        self.assertEqual(len(rows_kept), 1)
        self.assertEqual(rows_kept[0]["stage"], "hard_filter")
        self.assertEqual(rows_kept[0]["reason"], "test")

    def test_reason_stats_are_logged(self):
        """过滤原因统计：标题行 + 每条 '  {reason:<40} {cnt:>6} 篇' 格式的行"""
        with get_conn(self.db_path) as conn:
            self._seed_reason_stats_fixture(conn)

        with self.assertLogs("hard_filter", level="INFO") as cm:
            run_hard_filter(self.db_path)

        # assertLogs 的每行为 "INFO:hard_filter:<消息>"，去掉前缀后比对
        prefix = "INFO:hard_filter:"
        lines = [ln[len(prefix):] if ln.startswith(prefix) else ln for ln in cm.output]
        self.assertIn("过滤原因统计：", lines)
        stat_lines = lines[lines.index("过滤原因统计：") + 1:]
        self.assertEqual(len(stat_lines), 2)
        for line in stat_lines:
            self.assertIsNotNone(
                re.fullmatch(r"  \S+\s+\d+ 篇", line), f"统计行格式不符: {line!r}"
            )
        reasons = [line.split()[0] for line in stat_lines]
        self.assertEqual(sorted(reasons), ["abstract_too_short", "duplicate_title"])
        dup_line = next(line for line in stat_lines if line.startswith("  duplicate_title"))
        self.assertIsNotNone(re.fullmatch(r"  duplicate_title\s+1 篇", dup_line))


if __name__ == "__main__":
    unittest.main()
