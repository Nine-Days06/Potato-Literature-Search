import re
import unittest
import tempfile
from pathlib import Path

from cleaner.hard_filter import run_hard_filter, run_incremental_hard_filter
from utils.db import init_db, get_conn

TEST_YEAR = 2020


def _add_article(conn, pmid, title=None, abstract="", year=TEST_YEAR,
                 language="eng", article_types="Journal Article"):
    """插入一篇文章。

    abstract 默认为空（2026-10 起 → 转人工审核，stage='needs_review'），
    需显式传入非空摘要才能进入正常通过/排除路径。
    title 传 None 时自动生成；传 "" / "   " 会**原样写入**（用于测试标题为空规则），
    所以这里必须用 `is None` 判断而不是 `or`——空串是合法测试输入。
    """
    if title is None:
        title = f"Potato research article number {pmid}"
    conn.execute(
        """INSERT OR IGNORE INTO articles
           (pmid, title, abstract, pub_year, journal, language, article_types)
           VALUES (?, ?, ?, ?, 'J Test', ?, ?)""",
        (pmid, title, abstract, year, language, article_types),
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
            # 真正新增、摘要为空 → 处理并转人工审核（不再排除）
            _add_article(conn, "104", abstract="")
            # 真正新增、标题为空 → 处理并排除
            _add_article(conn, "105", title="", abstract=_good_abstract())

        stats = run_incremental_hard_filter(self.db_path)

        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["filtered"], 1)
        self.assertEqual(stats["passed"], 1)
        self.assertEqual(stats["review"], 1)

        # 103 通过 → 写入通过标记
        rows103 = self._log_rows("103")
        self.assertEqual(len(rows103), 1)
        self.assertEqual(rows103[0]["stage"], "hard_filter_pass")
        # 104 摘要为空 → 写入转审标记（不排除、不进 LLM）
        rows104 = self._log_rows("104")
        self.assertEqual(len(rows104), 1)
        self.assertEqual(rows104[0]["stage"], "needs_review")
        self.assertEqual(rows104[0]["reason"], "abstract_empty")
        # 105 标题为空 → 写入排除标记
        rows105 = self._log_rows("105")
        self.assertEqual(len(rows105), 1)
        self.assertEqual(rows105[0]["stage"], "hard_filter")
        self.assertEqual(rows105[0]["reason"], "title_empty")
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

    def test_needs_review_is_not_reprocessed(self):
        """转审文献下次增量运行同样不再重复处理（needs_review 也算已处理）"""
        with get_conn(self.db_path) as conn:
            _add_article(conn, "201", abstract="")

        first = run_incremental_hard_filter(self.db_path)
        self.assertEqual(first["total"], 1)
        self.assertEqual(first["review"], 1)

        second = run_incremental_hard_filter(self.db_path)
        self.assertEqual(second["total"], 0)


class TestFullHardFilterPassMarkers(HardFilterTestBase):
    """全量硬过滤应写入通过标记，并在重跑时清理过期标记"""

    def test_full_filter_writes_pass_and_fail_markers(self):
        with get_conn(self.db_path) as conn:
            _add_article(conn, "300", abstract=_good_abstract())
            _add_article(conn, "301", abstract="")
            _add_article(conn, "302", title="", abstract=_good_abstract())

        stats = run_hard_filter(self.db_path)

        self.assertEqual(stats["passed"], 1)
        self.assertEqual(stats["filtered"], 1)
        self.assertEqual(stats["review"], 1)
        rows300 = self._log_rows("300")
        self.assertEqual(len(rows300), 1)
        self.assertEqual(rows300[0]["stage"], "hard_filter_pass")
        rows301 = self._log_rows("301")
        self.assertEqual(rows301[0]["stage"], "needs_review")
        rows302 = self._log_rows("302")
        self.assertEqual(rows302[0]["stage"], "hard_filter")
        self.assertEqual(rows302[0]["reason"], "title_empty")

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
        self.assertEqual(rows[0]["stage"], "needs_review")
        self.assertEqual(rows[0]["reason"], "abstract_empty")


class TestRuleEvaluationStats(HardFilterTestBase):
    """单行规则判定与原因统计输出的行为锁定（供 _evaluate_row / _log_reason_stats 抽取回归）"""

    def _seed_reason_stats_fixture(self, conn):
        """
        构造"原因统计"固定数据（下面 3 个测试共用）：
          - 501：摘要为空 → 转审规则命中 abstract_empty（stage='needs_review'）
          - 502 / 503：同期刊、同年份、标题仅大小写/首尾空白不同的重复对
            （find_duplicate_titles 按 LOWER(TRIM(title)) 归一化，视为重复）
            502 先插入 → 保留；503 后插入 → 标记 duplicate_title。
            503 摘要同样为空，用于锁定"重复标题优先于转审规则"。
        返回 (empty_pmid, kept_dup_pmid, dup_pmid)
        """
        dup_title_kept   = "  Potato tuber Duplication Study With A Long Title  "
        dup_title_second = "potato tuber duplication study with a long title"
        _add_article(conn, "501", abstract="")
        _add_article(conn, "502", title=dup_title_kept, abstract=_good_abstract())
        _add_article(conn, "503", title=dup_title_second, abstract="")
        return "501", "502", "503"

    def test_reason_counts_and_dedup_priority_wins(self):
        """重复标题先于转审规则命中；保留的重复伙伴不被重复计数"""
        with get_conn(self.db_path) as conn:
            empty_pmid, kept_pmid, dup_pmid = self._seed_reason_stats_fixture(conn)
            # 一篇完全合格的文献
            _add_article(conn, "504", abstract=_good_abstract())

        stats = run_hard_filter(self.db_path)

        # 503 摘要同样为空，但因重复标题优先 → 计入 duplicate_title 而非转审
        self.assertEqual(
            stats["reason_counts"],
            {"abstract_empty": 1, "duplicate_title": 1},
        )
        self.assertEqual(stats["total"], 4)
        self.assertEqual(stats["filtered"], 1)
        self.assertEqual(stats["review"], 1)
        # 保留 2 篇：先插入的重复伙伴 + 完全合格的一篇
        self.assertEqual(stats["passed"], 2)

        rows_empty = self._log_rows(empty_pmid)
        self.assertEqual(rows_empty[0]["stage"], "needs_review")
        self.assertEqual(rows_empty[0]["reason"], "abstract_empty")
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
            _add_article(conn, "503", title=dup_title_second, abstract="")
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
        """过滤原因统计：标题行 + 每条 '  {reason:<40} {cnt:>6} 篇' 格式的行 + 转审提示行"""
        with get_conn(self.db_path) as conn:
            self._seed_reason_stats_fixture(conn)

        with self.assertLogs("hard_filter", level="INFO") as cm:
            run_hard_filter(self.db_path)

        # assertLogs 的每行为 "INFO:hard_filter:<消息>"，去掉前缀后比对
        prefix = "INFO:hard_filter:"
        lines = [ln[len(prefix):] if ln.startswith(prefix) else ln for ln in cm.output]
        self.assertIn("过滤原因统计：", lines)
        stat_lines = lines[lines.index("过滤原因统计：") + 1:]
        # 末尾多一行转审提示（摘要为空不排除），先摘掉再校验统计行
        review_lines = [ln for ln in stat_lines if ln.startswith("其中摘要为空转人工审核")]
        stat_lines = [ln for ln in stat_lines if not ln.startswith("其中摘要为空转人工审核")]
        self.assertEqual(review_lines, ["其中摘要为空转人工审核 1 篇（未排除，不进 LLM 验证）"])
        self.assertEqual(len(stat_lines), 2)
        for line in stat_lines:
            self.assertIsNotNone(
                re.fullmatch(r"  \S+\s+\d+ 篇", line), f"统计行格式不符: {line!r}"
            )
        reasons = [line.split()[0] for line in stat_lines]
        self.assertEqual(sorted(reasons), ["abstract_empty", "duplicate_title"])
        dup_line = next(line for line in stat_lines if line.startswith("  duplicate_title"))
        self.assertIsNotNone(re.fullmatch(r"  duplicate_title\s+1 篇", dup_line))


class TestRelaxedRules(HardFilterTestBase):
    """2026-10 放宽后，语言/摘要长度/年份/文章类型/标题长度都不再是排除理由"""

    def test_non_english_passes(self):
        with get_conn(self.db_path) as conn:
            _add_article(conn, "600", abstract=_good_abstract(), language="chi")

        stats = run_hard_filter(self.db_path)

        self.assertEqual(stats["passed"], 1)
        self.assertEqual(stats["filtered"], 0)
        self.assertEqual(self._log_rows("600")[0]["stage"], "hard_filter_pass")

    def test_short_abstract_passes(self):
        with get_conn(self.db_path) as conn:
            _add_article(conn, "601", abstract="tiny")

        stats = run_hard_filter(self.db_path)

        self.assertEqual(stats["passed"], 1)
        self.assertEqual(self._log_rows("601")[0]["stage"], "hard_filter_pass")

    def test_out_of_range_year_passes(self):
        with get_conn(self.db_path) as conn:
            _add_article(conn, "602", abstract=_good_abstract(), year=1901)
            _add_article(conn, "603", abstract=_good_abstract(), year=2999)

        stats = run_hard_filter(self.db_path)

        self.assertEqual(stats["passed"], 2)
        self.assertEqual(stats["filtered"], 0)

    def test_blacklisted_article_type_passes(self):
        with get_conn(self.db_path) as conn:
            _add_article(conn, "604", abstract=_good_abstract(),
                         article_types="Retracted Publication; Review")
            _add_article(conn, "605", abstract=_good_abstract(),
                         article_types="Editorial")

        stats = run_hard_filter(self.db_path)

        self.assertEqual(stats["passed"], 2)
        self.assertEqual(stats["filtered"], 0)

    def test_short_title_passes(self):
        with get_conn(self.db_path) as conn:
            _add_article(conn, "606", title="Potato", abstract=_good_abstract())

        stats = run_hard_filter(self.db_path)

        self.assertEqual(stats["passed"], 1)
        self.assertEqual(self._log_rows("606")[0]["stage"], "hard_filter_pass")

    def test_only_empty_fields_are_excluded(self):
        """剩下一条排除规则（标题为空）+ 一条转审规则（摘要为空，空白串也算空）"""
        with get_conn(self.db_path) as conn:
            _add_article(conn, "607", title="", abstract=_good_abstract())
            _add_article(conn, "608", title="   ", abstract=_good_abstract())
            _add_article(conn, "609", abstract="")
            _add_article(conn, "610", abstract="   ")

        stats = run_hard_filter(self.db_path)

        self.assertEqual(stats["reason_counts"],
                         {"title_empty": 2, "abstract_empty": 2})
        self.assertEqual(stats["filtered"], 2)
        self.assertEqual(stats["review"], 2)
        self.assertEqual(stats["passed"], 0)

    def test_empty_abstract_goes_to_needs_review_not_filtered(self):
        """摘要为空不再丢弃：写 needs_review，不是 hard_filter"""
        with get_conn(self.db_path) as conn:
            _add_article(conn, "611", abstract="")

        stats = run_hard_filter(self.db_path)

        self.assertEqual(stats["review"], 1)
        self.assertEqual(stats["filtered"], 0)
        rows = self._log_rows("611")
        self.assertEqual(rows[0]["stage"], "needs_review")
        self.assertEqual(rows[0]["reason"], "abstract_empty")

    def test_empty_title_and_abstract_excluded_as_title_empty(self):
        """标题为空优先于摘要为空转审：一条记录只写一次，且算真排除"""
        with get_conn(self.db_path) as conn:
            _add_article(conn, "612", title="", abstract="")

        stats = run_hard_filter(self.db_path)

        self.assertEqual(stats["filtered"], 1)
        self.assertEqual(stats["review"], 0)
        rows = self._log_rows("612")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stage"], "hard_filter")
        self.assertEqual(rows[0]["reason"], "title_empty")


if __name__ == "__main__":
    unittest.main()
