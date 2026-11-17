import unittest
import json
import csv
import sqlite3
import tempfile
import os
from pathlib import Path
from cleaner.llm_validator import (
    _extract_json,
    _build_per_article_prompt,
    _build_jsonl,
    _parse_batch_results,
    _build_log_rows,
    _count_verdicts,
    _normalize_verdict,
    _normalize_research_field,
    _export_raw_csv,
    _export_filtered_csv,
    export_incremental_raw_csv,
    export_incremental_filtered_csv,
    export_by_research_field,
    _field_slug,
    RAW_EXPORT_FIELDS,
    FILTERED_EXPORT_FIELDS,
    FIELD_EXPORT_INDEX_FIELDS,
    REVIEW_PENDING_FIELDS,
    NEEDS_REVIEW_VERDICT,
)
from config.settings import RESEARCH_FIELDS, DEFAULT_RESEARCH_FIELD
from utils.db import init_db, get_conn


class TestResearchFieldSettings(unittest.TestCase):
    """研究方向枚举的封闭性约束（LLM 只能二选一，不得自造标签）"""

    def test_enum_is_closed_and_unique(self):
        self.assertTrue(RESEARCH_FIELDS)
        self.assertEqual(len(RESEARCH_FIELDS), len(set(RESEARCH_FIELDS)))

    def test_default_is_member(self):
        self.assertIn(DEFAULT_RESEARCH_FIELD, RESEARCH_FIELDS)

    def test_prompt_lists_every_field(self):
        """系统提示词必须把全部枚举标签交给模型，否则分类不可控"""
        from cleaner.llm_validator import SYSTEM_PROMPT
        for field in RESEARCH_FIELDS:
            self.assertIn(field, SYSTEM_PROMPT)
        prompt = _build_per_article_prompt("1", "t", "a")
        self.assertIn("research_field", prompt)


class TestPromptRules(unittest.TestCase):
    """V12：只剔除事实性错误，其余一律收录（导师审阅口径）"""

    def setUp(self):
        from cleaner.llm_validator import SYSTEM_PROMPT
        self.prompt = SYSTEM_PROMPT

    def test_core_question_is_relatedness(self):
        self.assertIn("Solanum tuberosum", self.prompt)
        self.assertIn("这篇文献是否与马铃薯有关", self.prompt)

    def test_warns_sweet_potato_is_not_potato(self):
        """甘薯混淆是实测中最顽固的错误，负例规则必须保留"""
        self.assertIn("Ipomoea batatas", self.prompt)
        self.assertIn("sweet potato", self.prompt.lower())

    def test_material_and_background_usage_kept(self):
        """食品/材料/背景提及全部保留：V12 的核心变化"""
        self.assertIn("配料、原料、饲料、底盘", self.prompt)
        self.assertIn("物种列表、对比组、通用方法、背景", self.prompt)

    def test_old_exclusion_blocks_removed(self):
        """「顺带提及」与食品排除块与新口径冲突，不得回退"""
        self.assertNotIn("顺带提及", self.prompt)
        self.assertNotIn("食品科学 / 非农学用途的排除", self.prompt)
        self.assertNotIn("去掉马铃薯", self.prompt)

    def test_has_self_check_step(self):
        self.assertIn("自检", self.prompt)
        self.assertIn("Q1", self.prompt)
        self.assertIn("Q2", self.prompt)
        self.assertIn("不确定时判 RELEVANT", self.prompt)

    def test_no_few_shot_examples(self):
        self.assertNotIn("示例1", self.prompt)
        self.assertNotIn("示例 1", self.prompt)

    def test_plant_protection_exemption(self):
        """病原物研究即使寄主写烟草/番茄也算相关"""
        self.assertIn("Nicotiana benthamiana", self.prompt)
        self.assertIn("Phytophthora infestans", self.prompt)
        self.assertIn("Leptinotarsa decemlineata", self.prompt)

    def test_prompt_lists_every_field_with_hint(self):
        from config.settings import RESEARCH_FIELD_HINTS
        for field in RESEARCH_FIELDS:
            self.assertIn(f"- {field}：{RESEARCH_FIELD_HINTS[field]}", self.prompt)


class TestExtractJsonMultiArray(unittest.TestCase):

    def test_normal_single_array(self):
        text = '[{"pmid":"1","verdict":"RELEVANT"}]'
        result = _extract_json(text)
        self.assertEqual(len(result), 1)

    def test_glm_multi_array_with_newlines(self):
        text = (
            '[{"pmid":"1","verdict":"RELEVANT","reason":"a"}],\n'
            '[{"pmid":"2","verdict":"RELEVANT","reason":"b"}]'
        )
        result = _extract_json(text, fix_glm_multi_array=True)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["pmid"], "1")
        self.assertEqual(result[1]["pmid"], "2")

    def test_glm_multi_array_with_crlf(self):
        text = (
            '[{"pmid":"1","verdict":"RELEVANT"}],\r\n'
            '[{"pmid":"2","verdict":"RELEVANT"}]'
        )
        result = _extract_json(text, fix_glm_multi_array=True)
        self.assertEqual(len(result), 2)

    def test_glm_multi_array_with_spaces(self):
        text = (
            '[{"pmid":"1","verdict":"RELEVANT"}],   \n'
            '[{"pmid":"2","verdict":"RELEVANT"}]'
        )
        result = _extract_json(text, fix_glm_multi_array=True)
        self.assertEqual(len(result), 2)

    def test_fix_disabled_for_deepseek(self):
        text = (
            '[{"pmid":"1","verdict":"RELEVANT"}],\n'
            '[{"pmid":"2","verdict":"RELEVANT"}]'
        )
        result = _extract_json(text, fix_glm_multi_array=False)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["pmid"], "1")

    def test_glm_single_record_with_trailing_comma(self):
        text = '[{"pmid":"1","verdict":"RELEVANT","reason":"a"}],\n'
        result = _extract_json(text, fix_glm_multi_array=True)
        self.assertEqual(len(result), 1)

    def test_glm_trailing_comma_before_close_bracket(self):
        text = (
            '[\n'
            '{"pmid":"1","verdict":"RELEVANT","reason":"a"},\n'
            '{"pmid":"2","verdict":"NOT_RELEVANT","reason":"b"},\n'
            ']'
        )
        result = _extract_json(text, fix_glm_multi_array=True)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["pmid"], "1")
        self.assertEqual(result[1]["pmid"], "2")


class TestBuildPerArticlePrompt(unittest.TestCase):

    def test_prompt_includes_pmid(self):
        prompt = _build_per_article_prompt("12345", "Test Title", "Test Abstract")
        self.assertIn("12345", prompt)
        self.assertIn("Test Title", prompt)
        self.assertIn("Test Abstract", prompt)

    def test_prompt_includes_output_format(self):
        prompt = _build_per_article_prompt("99999", "T", "A")
        self.assertIn('"pmid":', prompt)
        self.assertIn('"verdict":', prompt)
        self.assertIn('"reason":', prompt)
        self.assertIn('"research_field":', prompt)

    def test_prompt_handles_empty_fields(self):
        prompt = _build_per_article_prompt("1", "", "")
        self.assertIn("PMID: 1", prompt)
        self.assertNotIn("None", prompt)


class TestBuildJsonl(unittest.TestCase):

    def setUp(self):
        from config.settings import OUTPUT_DIR, LLM_MAX_TOKENS
        self.original_output_dir = OUTPUT_DIR
        self.original_max_tokens = LLM_MAX_TOKENS
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _mock_rows(self, count=2):
        return [
            {"pmid": f"pmid_{i}", "title": f"Title {i}", "abstract": f"Abstract {i}"}
            for i in range(count)
        ]

    def test_jsonl_line_count(self):
        import cleaner.llm_validator as mod
        prev_out = mod.Path(mod.OUTPUT_DIR)
        mod.OUTPUT_DIR = self.temp_dir
        try:
            rows = self._mock_rows(3)
            path = mod._build_jsonl(rows)
            with open(path, "r", encoding="utf-8") as f:
                lines = f.read().strip().split("\n")
            self.assertEqual(len(lines), 3)
        finally:
            mod.OUTPUT_DIR = prev_out

    def test_jsonl_keys(self):
        rows = [
            {"pmid": "TEST1", "title": "T", "abstract": "A"},
        ]
        # Use config to control output path
        from cleaner.llm_validator import OUTPUT_DIR as mod_out_dir
        from cleaner.llm_validator import _build_jsonl as do_build

        test_dir = Path(self.temp_dir)
        with tempfile.TemporaryDirectory() as td:
            # monkey-patch OUTPUT_DIR
            import cleaner.llm_validator as mod
            orig = mod.OUTPUT_DIR
            mod.OUTPUT_DIR = td
            try:
                path = do_build(rows)
                with open(path, "r", encoding="utf-8") as f:
                    line_data = json.loads(f.read().split("\n")[0])
                self.assertEqual(line_data["custom_id"], "TEST1")
                self.assertEqual(line_data["method"], "POST")
                self.assertEqual(line_data["url"], "/v4/chat/completions")
                self.assertIn("model", line_data["body"])
                self.assertIn("messages", line_data["body"])
            finally:
                mod.OUTPUT_DIR = orig

    def test_jsonl_body_messages(self):
        rows = self._mock_rows(1)
        import cleaner.llm_validator as lm
        orig = lm.OUTPUT_DIR
        lm.OUTPUT_DIR = self.temp_dir
        try:
            path = lm._build_jsonl(rows)
            with open(path, "r", encoding="utf-8") as f:
                line_data = json.loads(f.read().split("\n")[0])
            msgs = line_data["body"]["messages"]
            self.assertEqual(msgs[0]["role"], "system")
            self.assertEqual(msgs[1]["role"], "user")
        finally:
            lm.OUTPUT_DIR = orig


class TestParseBatchResults(unittest.TestCase):
    """解析 batch 结果 JSONL 并写库。

    注意：`_parse_batch_results` 会真实写库，因此必须注入临时库
    （`db_path` 参数），否则测试数据会污染生产库。
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_db(self.db_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_result_jsonl(self, lines: list[dict]) -> str:
        path = os.path.join(self.temp_dir, "test_results.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for item in lines:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        return path

    def test_parse_success_result(self):
        content = (
            '{"pmid": "TEST001", "verdict": "RELEVANT", '
            '"reason": "contains potato gene"}'
        )
        result_line = {
            "custom_id": "TEST001",
            "response": {
                "status_code": 200,
                "body": {
                    "choices": [{
                        "message": {"content": content}
                    }]
                }
            }
        }
        path = self._make_result_jsonl([result_line])
        from cleaner.llm_validator import _parse_batch_results as parser
        success, failed = parser(path, self.db_path)
        self.assertEqual(success, 1)
        self.assertEqual(len(failed), 0)

    def test_parse_markdown_wrapped_result(self):
        """模拟 glm-4-flash 返回 ```json ... ``` 包裹的真实场景"""
        raw_content = "```json\n{\"pmid\": \"18944394\", \"verdict\": \"RELEVANT\", \"reason\": \"涉及马铃薯与病虫害相互作用\"}\n```"
        result_line = {
            "custom_id": "18944394",
            "response": {
                "status_code": 200,
                "body": {
                    "choices": [{
                        "message": {"content": raw_content}
                    }]
                }
            }
        }
        path = self._make_result_jsonl([result_line])
        from cleaner.llm_validator import _parse_batch_results as parser
        success, failed = parser(path, self.db_path)
        self.assertEqual(success, 1, "markdown 包裹的单 JSON 对象应能成功解析")
        self.assertEqual(len(failed), 0)

    def test_parse_failed_status_code(self):
        result_line = {
            "custom_id": "FAIL01",
            "response": {"status_code": 500, "body": {}}
        }
        path = self._make_result_jsonl([result_line])
        from cleaner.llm_validator import _parse_batch_results as parser
        success, failed = parser(path, self.db_path)
        self.assertEqual(success, 0)
        self.assertEqual(len(failed), 1)

    def test_parse_invalid_json_line(self):
        path = os.path.join(self.temp_dir, "invalid.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("this is not json\n")
        from cleaner.llm_validator import _parse_batch_results as parser
        success, failed = parser(path, self.db_path)
        self.assertEqual(success, 0)

    def test_parse_mixed_results(self):
        ok_content = '{"pmid":"OK1","verdict":"RELEVANT","reason":"good"}'
        ok = {
            "custom_id": "OK1",
            "response": {
                "status_code": 200,
                "body": {"choices": [{"message": {"content": ok_content}}]}
            }
        }
        err = {
            "custom_id": "ERR1",
            "response": {"status_code": 500, "body": {}}
        }
        path = self._make_result_jsonl([ok, err])
        from cleaner.llm_validator import _parse_batch_results as parser
        success, failed = parser(path, self.db_path)
        self.assertEqual(success, 1)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0], "ERR1")

    def test_parse_unrecognized_verdict_counts_as_failed(self):
        """verdict 缺失或非标准 → 判失败重试，不入库 UNKNOWN"""
        for content in (
            '{"pmid":"X1","reason":"no verdict"}',
            '{"pmid":"X2","verdict":"maybe","reason":"bad verdict"}',
        ):
            item = {
                "custom_id": content.split('"pmid":"')[1].split('"')[0],
                "response": {
                    "status_code": 200,
                    "body": {"choices": [{"message": {"content": content}}]}
                }
            }
            path = self._make_result_jsonl([item])
            from cleaner.llm_validator import _parse_batch_results as parser
            success, failed = parser(path, self.db_path)
            self.assertEqual(success, 0, content)
            self.assertEqual(len(failed), 1, content)


class TestBuildLogRows(unittest.TestCase):
    """回归测试：SQL 查询不再返回 label 列，log_rows 必须为 5 元组（末位为 research_field）"""

    def _make_sql_rows(self, pmids: list[str]):
        """模拟同步验证的查询结果（仅 pmid/title/abstract 三列）"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (pmid TEXT, title TEXT, abstract TEXT)")
        for p in pmids:
            conn.execute("INSERT INTO t VALUES (?, 't', 'a')", (p,))
        rows = conn.execute("SELECT pmid, title, abstract FROM t").fetchall()
        conn.close()
        return rows

    def test_rows_without_label_column(self):
        batch = self._make_sql_rows(["1", "2"])
        pmid_to_result = {
            "1": {"pmid": "1", "verdict": "RELEVANT", "reason": "r1",
                  "research_field": "遗传育种"},
            "2": {"pmid": "2", "verdict": "NOT_RELEVANT", "reason": "r2",
                  "research_field": "遗传育种"},
        }
        log_rows, failed = _build_log_rows(batch, pmid_to_result, set(), "now")
        self.assertEqual(len(log_rows), 2)
        self.assertEqual(failed, [])
        for row in log_rows:
            self.assertEqual(len(row), 5)
        self.assertEqual(log_rows[0], ("1", "RELEVANT", "r1", "now", "遗传育种"))
        # NOT_RELEVANT 不分类：即使模型给了方向也清空
        self.assertEqual(log_rows[1], ("2", "NOT_RELEVANT", "r2", "now", ""))

    def test_failed_pmids_are_collected(self):
        batch = self._make_sql_rows(["1", "2", "3"])
        pmid_to_result = {"1": {"pmid": "1", "verdict": "RELEVANT"}}
        log_rows, failed = _build_log_rows(
            batch, pmid_to_result, {"2"}, "now"
        )
        self.assertEqual(len(log_rows), 1)
        self.assertEqual([r["pmid"] for r in failed], ["2", "3"])

    def test_missing_verdict_goes_to_failed(self):
        """verdict 缺失（截断残缺对象）→ 判失败重试，不入库 UNKNOWN"""
        batch = self._make_sql_rows(["1"])
        pmid_to_result = {"1": {"pmid": "1", "reason": "only reason"}}
        log_rows, failed = _build_log_rows(batch, pmid_to_result, set(), "now")
        self.assertEqual(log_rows, [])
        self.assertEqual([r["pmid"] for r in failed], ["1"])

    def test_invalid_verdict_goes_to_failed(self):
        batch = self._make_sql_rows(["1"])
        pmid_to_result = {"1": {"pmid": "1", "verdict": "maybe"}}
        log_rows, failed = _build_log_rows(batch, pmid_to_result, set(), "now")
        self.assertEqual(log_rows, [])
        self.assertEqual([r["pmid"] for r in failed], ["1"])

    def test_lowercase_verdict_normalized(self):
        batch = self._make_sql_rows(["1"])
        pmid_to_result = {"1": {"pmid": "1", "verdict": "relevant"}}
        log_rows, _ = _build_log_rows(batch, pmid_to_result, set(), "now")
        self.assertEqual(log_rows[0][1], "RELEVANT")


class TestNormalizeVerdict(unittest.TestCase):

    def test_standard_values(self):
        self.assertEqual(_normalize_verdict("RELEVANT"), "RELEVANT")
        self.assertEqual(_normalize_verdict("NOT_RELEVANT"), "NOT_RELEVANT")

    def test_case_insensitive(self):
        self.assertEqual(_normalize_verdict("relevant"), "RELEVANT")
        self.assertEqual(_normalize_verdict("Not Relevant"), "NOT_RELEVANT")

    def test_variants(self):
        self.assertEqual(_normalize_verdict("Irrelevant"), "NOT_RELEVANT")
        self.assertEqual(_normalize_verdict("not_relevant"), "NOT_RELEVANT")

    def test_unrecognized_returns_none(self):
        self.assertIsNone(_normalize_verdict("maybe"))
        self.assertIsNone(_normalize_verdict(""))
        self.assertIsNone(_normalize_verdict(None))


class TestNormalizeResearchField(unittest.TestCase):
    """方向归一化：NOT_RELEVANT 不分类；越界/缺失/非字符串回退 DEFAULT_RESEARCH_FIELD"""

    def test_not_relevant_never_classified(self):
        self.assertEqual(_normalize_research_field("遗传育种", "NOT_RELEVANT"), "")
        self.assertEqual(_normalize_research_field("", "NOT_RELEVANT"), "")
        self.assertEqual(_normalize_research_field(None, "NOT_RELEVANT"), "")

    def test_valid_field_kept(self):
        for field in RESEARCH_FIELDS:
            self.assertEqual(_normalize_research_field(field, "RELEVANT"), field)

    def test_whitespace_stripped(self):
        self.assertEqual(_normalize_research_field("  栽培生理 \n", "RELEVANT"), "栽培生理")

    def test_out_of_enum_falls_back(self):
        # 模型自造标签不应导致整篇回退重试，收敛到兜底值
        self.assertEqual(
            _normalize_research_field("植物病理学", "RELEVANT"),
            DEFAULT_RESEARCH_FIELD,
        )
        self.assertEqual(
            _normalize_research_field("分子生物学与遗传学", "RELEVANT"),
            DEFAULT_RESEARCH_FIELD,
        )

    def test_missing_or_wrong_type_falls_back(self):
        self.assertEqual(_normalize_research_field(None, "RELEVANT"), DEFAULT_RESEARCH_FIELD)
        self.assertEqual(_normalize_research_field("", "RELEVANT"), DEFAULT_RESEARCH_FIELD)
        self.assertEqual(_normalize_research_field(["遗传育种"], "RELEVANT"), DEFAULT_RESEARCH_FIELD)
        self.assertEqual(_normalize_research_field(123, "RELEVANT"), DEFAULT_RESEARCH_FIELD)


class TestCountVerdicts(unittest.TestCase):
    """回归测试：log_rows 为 5 元组，统计解包不能按 4 元组"""

    def test_counts_five_tuple_rows(self):
        rows = [
            ("1", "RELEVANT", "r1", "now", "遗传育种"),
            ("2", "NOT_RELEVANT", "r2", "now", ""),
            ("3", "RELEVANT", "r3", "now", "栽培生理"),
        ]
        self.assertEqual(
            _count_verdicts(rows),
            {"RELEVANT": 2, "NOT_RELEVANT": 1},
        )

    def test_empty(self):
        self.assertEqual(_count_verdicts([]), {})


class TestExportRawCsv(unittest.TestCase):
    """_export_raw_csv：导出与 llm_filtered 相同筛选条件、仅含原始信息的 CSV"""

    RAW_FIELDS = [
        "pmid", "title", "abstract", "keywords", "mesh_terms",
        "pub_year", "pub_month", "journal", "journal_abbr", "doi",
        "pmc_id", "article_types", "authors", "affiliation",
        "language",
    ]

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test.db"
        init_db(self.db_path)
        self._seed()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _seed(self):
        articles = [
            ("A", "Title A", "Abstract A", "2021", "Mar", "Plant J",
             "PMC1", "batch1.xml"),
            ("B", "Title B", "Abstract B", "2021", "Apr", "Potato Res",
             "PMC2", "batch1.xml"),
            ("C", "Title C", "Abstract C", "2021", "May", "Plant J",
             "PMC3", "batch1.xml"),
            ("D", "Title D", "Abstract D", "2021", "Jun", "Plant J",
             "PMC4", "batch1.xml"),
            ("E", "Title E", "Abstract E", "2021", "Jul", "Plant J",
             "PMC5", "batch1.xml"),
        ]
        with get_conn(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO articles "
                "(pmid, title, abstract, pub_year, pub_month, journal, "
                "pmc_id, raw_xml_file) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                articles,
            )
            conn.executemany(
                "INSERT INTO llm_validation (pmid, llm_verdict, human_review) "
                "VALUES (?, ?, ?)",
                [
                    ("A", "RELEVANT", "Y"),        # 人工通过 → 应导出
                    ("B", "RELEVANT", None),       # LLM 判相关未复核 → 应导出
                    ("C", "RELEVANT", "N"),        # 人工驳回 → 不导出
                    ("D", "NOT_RELEVANT", None),   # LLM 判不相关 → 不导出
                ],
            )
            # E 无 llm_validation 记录 → 不导出

    def _export(self):
        import cleaner.llm_validator as mod
        orig = mod.OUTPUT_DIR
        mod.OUTPUT_DIR = self.temp_dir
        try:
            return mod._export_raw_csv(db_path=self.db_path)
        finally:
            mod.OUTPUT_DIR = orig

    def test_export_includes_only_filtered_articles(self):
        path = self._export()
        self.assertIsNotNone(path)
        with open(path, "r", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(sorted(r["pmid"] for r in rows), ["A", "B"])

    def test_export_columns_are_raw_only(self):
        path = self._export()
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            rows = list(reader)
        self.assertEqual(fieldnames, self.RAW_FIELDS)
        for col in ("llm_verdict", "llm_reason", "human_review", "raw_xml_file"):
            self.assertNotIn(col, fieldnames)
        self.assertEqual(len(rows), 2)

    def test_export_preserves_raw_values(self):
        path = self._export()
        with open(path, "r", encoding="utf-8-sig") as f:
            rows = {r["pmid"]: r for r in csv.DictReader(f)}
        a = rows["A"]
        self.assertEqual(a["title"], "Title A")
        self.assertEqual(a["journal"], "Plant J")
        self.assertEqual(a["pub_month"], "Mar")

    def test_export_filename_and_bom(self):
        path = self._export()
        self.assertTrue(path.name.startswith("articles_raw_"))
        self.assertTrue(path.name.endswith(".csv"))
        with open(path, "rb") as f:
            self.assertTrue(f.read(3).startswith(b"\xef\xbb\xbf"))

    def test_export_empty_returns_none(self):
        with get_conn(self.db_path) as conn:
            conn.execute("DELETE FROM llm_validation")
        path = self._export()
        self.assertIsNone(path)


class TestIncrementalRawCsv(unittest.TestCase):
    """
    特征化测试（D3 重构前置）：钉住 export_incremental_raw_csv 的既有行为。

    三个易被"顺手优化"破坏的契约：
    1. 文件名后缀 ``_since_{since.replace(':', '-').replace('T', '_')}``；
    2. 时间谓词是严格 ``>``（等于 since 的行必须排除）；
    3. ORDER BY v.validated_at 与 since_timestamp **无关**，无 since 时依然排序。
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test.db"
        init_db(self.db_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _seed(self, validations):
        """validations: [(pmid, validated_at, llm_verdict, human_review), ...]"""
        with get_conn(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO articles "
                "(pmid, title, abstract, pub_year, pub_month, journal, "
                "pmc_id, raw_xml_file) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (pmid, f"Title {pmid}", f"Abstract {pmid}",
                     "2026", "Jan", "Plant J", f"PMC{pmid}", "batch1.xml")
                    for pmid, *_ in validations
                ],
            )
            conn.executemany(
                "INSERT INTO llm_validation "
                "(pmid, llm_verdict, reason, validated_at, human_review) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (pmid, verdict, f"reason {pmid}", validated_at, review)
                    for pmid, validated_at, verdict, review in validations
                ],
            )

    def _export(self, since):
        import cleaner.llm_validator as mod
        orig = mod.OUTPUT_DIR
        mod.OUTPUT_DIR = self.temp_dir
        try:
            return mod.export_incremental_raw_csv(since, db_path=self.db_path)
        finally:
            mod.OUTPUT_DIR = orig

    def _read(self, path):
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            return reader.fieldnames, list(reader)

    def test_incremental_export_filters_by_validated_at_and_orders_ascending(self):
        # 严格递增的 validated_at，但按 DESC 顺序插入 —— 若无 ORDER BY，
        # 结果会是插入顺序（倒序），测试即可捕捉到顺序回归。
        self._seed([
            ("1", "2026-03-01T00:00:00", "RELEVANT", "Y"),
            ("2", "2026-02-01T00:00:00", "RELEVANT", "Y"),
            ("3", "2026-01-01T00:00:00", "RELEVANT", "Y"),
        ])
        path = self._export("2026-01-15T00:00:00")
        self.assertIsNotNone(path)

        # (a) 文件名后缀变换契约
        self.assertTrue(
            path.name.startswith("articles_raw_incremental_since_2026-01-15_00-00-00_"),
            path.name,
        )

        fieldnames, rows = self._read(path)
        # (b) 仅 validated_at > since 的 2 行；2026-01-01 必须被排除
        self.assertEqual([r["pmid"] for r in rows], ["2", "1"])
        self.assertNotIn("3", [r["pmid"] for r in rows])
        # (c) 升序
        self.assertEqual([r["pmid"] for r in rows], ["2", "1"])
        # (d) 表头 == RAW_EXPORT_FIELDS
        self.assertEqual(fieldnames, RAW_EXPORT_FIELDS)

    def test_incremental_export_boundary_row_at_since_is_excluded(self):
        """谓词是严格 `>`：validated_at 恰等于 since 的行不得导出"""
        self._seed([
            ("1", "2026-01-15T00:00:00", "RELEVANT", "Y"),
        ])
        path = self._export("2026-01-15T00:00:00")
        self.assertIsNone(path, "validated_at == since 应被排除，因此无数据可导出")

    def test_incremental_export_without_since_has_no_filename_suffix_but_still_orders(self):
        """since=None：无 `_since_` 后缀、导出全部、**仍然**按 validated_at 升序"""
        self._seed([
            ("1", "2026-03-01T00:00:00", "RELEVANT", "Y"),
            ("2", "2026-02-01T00:00:00", "RELEVANT", "Y"),
            ("3", "2026-01-01T00:00:00", "RELEVANT", "Y"),
        ])
        path = self._export(None)
        self.assertIsNotNone(path)
        self.assertNotIn("_since_", path.name, path.name)

        _, rows = self._read(path)
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["pmid"] for r in rows], ["3", "2", "1"])

    def test_filtered_export_uses_its_own_column_set(self):
        """_export_filtered_csv 的列集/筛选与 raw 导出不同，勿混用"""
        import cleaner.llm_validator as mod
        self._seed([
            ("1", "2026-03-01T00:00:00", "NOT_RELEVANT", "Y"),       # 人工通过 → 导出
            ("2", "2026-02-01T00:00:00", "RELEVANT", None),            # LLM 相关未复核 → 导出
            ("3", "2026-01-01T00:00:00", "RELEVANT", "N"),             # 人工驳回 → 不导出
            ("4", "2025-12-01T00:00:00", "NOT_RELEVANT", None),        # LLM 不相关 → 不导出
        ])

        orig_db, orig_out = mod.DB_PATH, mod.OUTPUT_DIR
        mod.DB_PATH, mod.OUTPUT_DIR = self.db_path, self.temp_dir
        try:
            path = mod._export_filtered_csv()
        finally:
            mod.DB_PATH, mod.OUTPUT_DIR = orig_db, orig_out

        self.assertIsNotNone(path)
        self.assertTrue(path.name.startswith("llm_filtered_"), path.name)

        fieldnames, rows = self._read(path)
        self.assertEqual(sorted(r["pmid"] for r in rows), ["1", "2"])
        self.assertEqual(
            fieldnames[-4:],
            ["llm_verdict", "llm_reason", "research_field", "human_review"],
        )
        self.assertNotIn("reason", fieldnames)
        # 过滤导出不含 raw 专属列
        for col in ("pub_month", "journal_abbr"):
            self.assertNotIn(col, fieldnames)
        # reason → llm_reason 改名生效
        by_pmid = {r["pmid"]: r for r in rows}
        self.assertEqual(by_pmid["1"]["llm_reason"], "reason 1")

    def test_should_warn_when_since_predates_all_validations(self):
        """时间戳早于全部验证时间时导出=全量，必须显式告警而非静默"""
        self._seed([
            ("1", "2026-08-08T00:00:00", "RELEVANT", "Y"),
            ("2", "2026-08-08T00:00:00", "RELEVANT", "Y"),
        ])
        with self.assertLogs("llm_validator", level="WARNING") as logs:
            path = self._export("2025-08-01T00:00:00")

        self.assertIsNotNone(path)
        _, rows = self._read(path)
        self.assertEqual(len(rows), 2, "时间戳过早时确实会导出全部")
        warned = [ln for ln in logs.output if "并非真正的增量导出" in ln]
        self.assertTrue(warned, f"应告警时间戳过早，实际日志: {logs.output}")

    def test_should_not_warn_when_since_narrows_results(self):
        """真正的增量导出（条数小于总数）不应误报"""
        self._seed([
            ("1", "2026-08-08T00:00:00", "RELEVANT", "Y"),
            ("2", "2026-08-08T00:00:00", "RELEVANT", "Y"),
            ("3", "2026-10-01T00:00:00", "RELEVANT", "Y"),
        ])
        import cleaner.llm_validator as mod
        orig = mod.OUTPUT_DIR
        mod.OUTPUT_DIR = self.temp_dir
        try:
            with self.assertLogs("llm_validator", level="INFO") as logs:
                path = mod.export_incremental_raw_csv(
                    "2026-09-01T00:00:00", db_path=self.db_path)
        finally:
            mod.OUTPUT_DIR = orig

        self.assertIsNotNone(path)
        _, rows = self._read(path)
        self.assertEqual([r["pmid"] for r in rows], ["3"])
        self.assertFalse([ln for ln in logs.output if "并非真正的增量导出" in ln],
                         "条数已收窄，不应告警")


class TestIncrementalFilteredCsv(unittest.TestCase):
    """export_incremental_filtered_csv：与 raw 增量同筛选/同时段，但带研究方向列"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test.db"
        init_db(self.db_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _seed(self):
        with get_conn(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO articles "
                "(pmid, title, abstract, pub_year, pub_month, journal, "
                "pmc_id, raw_xml_file) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("1", "Title 1", "Abstract 1", "2026", "Jan", "Plant J",
                     "PMC1", "b.xml"),
                    ("2", "Title 2", "Abstract 2", "2026", "Jan", "Plant J",
                     "PMC2", "b.xml"),
                    ("3", "Title 3", "Abstract 3", "2026", "Jan", "Plant J",
                     "PMC3", "b.xml"),
                ],
            )
            conn.executemany(
                "INSERT INTO llm_validation "
                "(pmid, llm_verdict, reason, validated_at, research_field, "
                "human_review) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("1", "RELEVANT", "r1", "2026-01-01T00:00:00", "遗传育种", None),
                    ("2", "RELEVANT", "r2", "2026-09-01T00:00:00", "植物保护", None),
                    ("3", "NOT_RELEVANT", "r3", "2026-09-02T00:00:00", "", None),
                ],
            )

    def _export(self, since):
        import cleaner.llm_validator as mod
        orig = mod.OUTPUT_DIR
        mod.OUTPUT_DIR = self.temp_dir
        try:
            return mod.export_incremental_filtered_csv(since, db_path=self.db_path)
        finally:
            mod.OUTPUT_DIR = orig

    def test_columns_carry_research_field(self):
        self._seed()
        path = self._export("2026-08-01T00:00:00")
        self.assertIsNotNone(path)
        self.assertTrue(path.name.startswith("llm_filtered_incremental"), path.name)
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            self.assertEqual(reader.fieldnames, FILTERED_EXPORT_FIELDS)
            self.assertEqual(reader.fieldnames[-4:],
                             ["llm_verdict", "llm_reason", "research_field",
                              "human_review"])
            rows = list(reader)
        self.assertEqual([r["pmid"] for r in rows], ["2"])
        self.assertEqual(rows[0]["research_field"], "植物保护")
        self.assertEqual(rows[0]["llm_reason"], "r2")

    def test_filters_out_not_relevant_and_old_timestamps(self):
        self._seed()
        # since 早于全部记录 → 只受复核谓词过滤，NOT_RELEVANT 仍应被排除
        path = self._export("2025-01-01T00:00:00")
        with open(path, "r", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(sorted(r["pmid"] for r in rows), ["1", "2"])

    def test_no_rows_returns_none(self):
        self._seed()
        self.assertIsNone(self._export("2027-01-01T00:00:00"))


class TestExportByResearchField(unittest.TestCase):
    """export_by_research_field：一方向一文件 + 索引，与 filtered 同谓词同列"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test.db"
        init_db(self.db_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _seed(self):
        with get_conn(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO articles "
                "(pmid, title, abstract, pub_year, journal, raw_xml_file) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("1", "T1", "A1", "2026", "Plant J", "b.xml"),
                    ("2", "T2", "A2", "2026", "Plant J", "b.xml"),
                    ("3", "T3", "A3", "2026", "Plant J", "b.xml"),
                    ("4", "T4", "A4", "2026", "Plant J", "b.xml"),
                ],
            )
            conn.executemany(
                "INSERT INTO llm_validation "
                "(pmid, llm_verdict, reason, validated_at, research_field, "
                "human_review) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("1", "RELEVANT", "r1", "2026-01-01T00:00:00", "植物保护", None),
                    ("2", "RELEVANT", "r2", "2026-01-02T00:00:00", "植物保护", None),
                    ("3", "RELEVANT", "r3", "2026-01-03T00:00:00", "遗传育种", None),
                    # 被复核谓词排除，不应出现在任何方向文件里
                    ("4", "NOT_RELEVANT", "r4", "2026-01-04T00:00:00", "", None),
                ],
            )

    def _export(self, **kw):
        import cleaner.llm_validator as mod
        orig = mod.OUTPUT_DIR
        mod.OUTPUT_DIR = self.temp_dir
        try:
            return mod.export_by_research_field(db_path=self.db_path, **kw)
        finally:
            mod.OUTPUT_DIR = orig

    def _read(self, path):
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            return reader.fieldnames, list(reader)

    def _full_dir(self):
        """全量导出的落地目录（OUTPUT_DIR/exports/full）

        索引在导出根目录，方向文件在 by_field/<方向>/ 子目录里，
        测试不能只扫 temp_dir 根目录。
        """
        return Path(self.temp_dir) / "exports" / "full"

    def _field_dir(self, field):
        return self._full_dir() / "by_field" / field

    def test_one_dir_per_field_with_two_files(self):
        """每个方向一个目录，目录内含「含 LLM 信息」与「raw 格式」两份"""
        self._seed()
        paths = self._export()
        self.assertEqual(len(paths), 4)  # 2 个方向 × 2 份
        for field, expected in (("植物保护", 2), ("遗传育种", 1)):
            fdir = self._field_dir(field)
            self.assertTrue(fdir.is_dir(), fdir)
            llm_files = sorted(fdir.glob("llm_filtered_by_field_*.csv"))
            raw_files = sorted(fdir.glob("articles_raw_by_field_*.csv"))
            self.assertEqual(len(llm_files), 1, fdir)
            self.assertEqual(len(raw_files), 1, fdir)
            fieldnames, rows = self._read(llm_files[0])
            self.assertEqual(fieldnames, FILTERED_EXPORT_FIELDS)
            self.assertEqual(len(rows), expected)
            raw_fieldnames, raw_rows = self._read(raw_files[0])
            self.assertEqual(raw_fieldnames, RAW_EXPORT_FIELDS)
            self.assertEqual(len(raw_rows), expected)
            self.assertEqual({r["pmid"] for r in raw_rows},
                             {r["pmid"] for r in rows})

    def test_raw_variant_has_no_llm_columns(self):
        """raw 版必须与 articles_raw_*.csv 同列，不含任何 LLM/复核信息"""
        self._seed()
        self._export()
        raw_files = sorted(self._full_dir().glob("by_field/*/articles_raw_by_field_*.csv"))
        self.assertTrue(raw_files)
        for p in raw_files:
            fieldnames, _ = self._read(p)
            self.assertEqual(fieldnames, RAW_EXPORT_FIELDS)
            for col in ("llm_verdict", "llm_reason", "research_field",
                        "human_review", "reason"):
                self.assertNotIn(col, fieldnames, p.name)

    def test_not_relevant_never_exported(self):
        self._seed()
        paths = self._export()
        pmids = set()
        for p in paths:
            if not p.name.startswith("llm_filtered"):
                continue
            _, rows = self._read(p)
            pmids |= {r["pmid"] for r in rows}
        self.assertEqual(pmids, {"1", "2", "3"})

    def test_index_lists_every_field_with_counts(self):
        self._seed()
        self._export()
        index_files = sorted(self._full_dir().glob(
            "llm_filtered_by_field_index_*.csv"))
        self.assertEqual(len(index_files), 1)
        fieldnames, rows = self._read(index_files[0])
        self.assertEqual(fieldnames, FIELD_EXPORT_INDEX_FIELDS)
        by_field = {r["research_field"]: int(r["count"]) for r in rows}
        self.assertEqual(by_field, {"植物保护": 2, "遗传育种": 1})
        self.assertEqual(sum(by_field.values()), 3)
        # 索引里的目录与两份文件名都必须真实存在
        for r in rows:
            self.assertTrue((self._full_dir() / r["dir"]).is_dir(), r["dir"])
            for key in ("llm_file", "raw_file"):
                self.assertTrue(
                    (self._full_dir() / r["dir"] / r[key]).exists(), r[key])

    def test_fields_follow_research_fields_declaration_order(self):
        """方向顺序取 RESEARCH_FIELDS 声明顺序，而非字典插入顺序"""
        self._seed()
        self._export()
        index = sorted(self._full_dir().glob(
            "llm_filtered_by_field_index_*.csv"))[0]
        _, rows = self._read(index)
        order = [r["research_field"] for r in rows]
        self.assertEqual(order, ["遗传育种", "植物保护"])

    def test_missing_field_falls_back_to_default(self):
        """历史脏数据 direction 为空 → 归入兜底方向，不静默丢弃"""
        self._seed()
        with get_conn(self.db_path) as conn:
            conn.execute("UPDATE llm_validation SET research_field='' "
                         "WHERE pmid='3'")
        paths = self._export()
        llm_paths = [p for p in paths if p.name.startswith("llm_filtered")]
        _, rows = self._read([p for p in llm_paths
                              if DEFAULT_RESEARCH_FIELD in p.name][0])
        self.assertEqual([r["pmid"] for r in rows], ["3"])

    def test_incremental_prefix_and_since_filter(self):
        self._seed()
        # 谓词是严格大于（> since），故用 01-01 12:00 把 pmid 2 圈进来
        paths = self._export(since_timestamp="2026-01-01T12:00:00")
        self.assertTrue(paths)
        for p in paths:
            expected = ("llm_filtered_incremental_since_"
                        if p.name.startswith("llm_filtered")
                        else "articles_raw_incremental_since_")
            self.assertTrue(p.name.startswith(expected), p.name)
        pmids = set()
        for p in paths:
            if not p.name.startswith("llm_filtered"):
                continue
            _, rows = self._read(p)
            pmids |= {r["pmid"] for r in rows}
        self.assertEqual(pmids, {"2", "3"})

    def test_no_rows_returns_empty_list(self):
        with get_conn(self.db_path) as conn:
            conn.execute("INSERT INTO articles (pmid, title, abstract) "
                         "VALUES ('9', 'T', 'A')")
        self.assertEqual(self._export(), [])


class TestFieldSlug(unittest.TestCase):
    """方向标签转文件名：中文原样保留，只清理路径分隔符"""

    def test_chinese_label_kept(self):
        self.assertEqual(_field_slug("植物保护"), "植物保护")

    def test_path_separators_replaced(self):
        self.assertEqual(_field_slug("a/b\\c"), "a_b_c")

    def test_blank_falls_back_to_default(self):
        self.assertEqual(_field_slug(""), DEFAULT_RESEARCH_FIELD)
        self.assertEqual(_field_slug("   "), DEFAULT_RESEARCH_FIELD)


class TestReviewPendingCsv(unittest.TestCase):
    """待复核清单：摘要为空转人工（needs_review）的文献必须出现在清单里

    这类文献没有 llm_validation 行（跳过 LLM），若清单只 JOIN llm_validation，
    人工就永远看不到它们——规则改了但人拿不到活干。
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test.db"
        init_db(self.db_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _seed(self):
        with get_conn(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO articles (pmid, title, abstract, pub_year, journal, "
                "article_types) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("1", "LLM 已判相关", "Abstract 1", 2020, "Plant J", "Review"),
                    ("2", "LLM 已判不相关", "Abstract 2", 2020, "Plant J",
                     "Journal Article"),
                    ("3", "无摘要待人工", "", 1953, "Agron J", "Journal Article"),
                ],
            )
            conn.executemany(
                "INSERT INTO llm_validation (pmid, llm_verdict, reason, "
                "validated_at, research_field, human_review) VALUES (?,?,?,?,?,?)",
                [
                    ("1", "RELEVANT", "r1", "2026-01-01T00:00:00", "遗传育种", None),
                    ("2", "NOT_RELEVANT", "r2", "2026-01-01T00:00:00", "", None),
                ],
            )
            conn.execute(
                "INSERT INTO filter_log (pmid, stage, reason, filtered_at) "
                "VALUES ('3', 'needs_review', 'abstract_empty', "
                "'2026-01-01T00:00:00')"
            )

    def _export(self):
        import cleaner.llm_validator as mod
        orig = mod.OUTPUT_DIR
        mod.OUTPUT_DIR = self.temp_dir
        try:
            return mod._export_review_csv(self.db_path)
        finally:
            mod.OUTPUT_DIR = orig

    def test_manual_rows_are_included(self):
        self._seed()
        path = self._export()
        self.assertIsNotNone(path)
        self.assertTrue(path.name.startswith("llm_review_pending_"), path.name)

        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            rows = list(reader)

        # 补的三列必须在，人工无摘要时只能靠它们判断
        for col in ("pub_year", "journal", "article_types"):
            self.assertIn(col, fieldnames)
        self.assertEqual(fieldnames, REVIEW_PENDING_FIELDS)

        self.assertEqual(sorted(r["pmid"] for r in rows), ["1", "2", "3"])
        manual = next(r for r in rows if r["pmid"] == "3")
        # 转审行必须显式标注，不能伪装成模型判过
        self.assertEqual(manual["llm_verdict"], NEEDS_REVIEW_VERDICT)
        self.assertEqual(manual["reason"], "abstract_empty")
        self.assertEqual(manual["pub_year"], "1953")
        self.assertEqual(manual["journal"], "Agron J")
        self.assertEqual(manual["research_field"], "")

    def test_already_reviewed_articles_are_excluded(self):
        self._seed()
        with get_conn(self.db_path) as conn:
            conn.execute("UPDATE llm_validation SET human_review='Y' WHERE pmid='1'")
            conn.execute("UPDATE llm_validation SET human_review='N' WHERE pmid='2'")
        path = self._export()
        with open(path, "r", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([r["pmid"] for r in rows], ["3"])

    def test_manual_row_dropped_once_validated(self):
        """转审文献一旦有了 llm_validation 行（人工判完），不再作为转审项重复出现"""
        self._seed()
        with get_conn(self.db_path) as conn:
            conn.execute(
                "INSERT INTO llm_validation (pmid, llm_verdict, reason, "
                "validated_at, research_field, human_review) "
                "VALUES ('3', 'RELEVANT', '人工', '2026-02-01T00:00:00', '栽培生理', NULL)"
            )
        path = self._export()
        with open(path, "r", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        three = [r for r in rows if r["pmid"] == "3"]
        self.assertEqual(len(three), 1)
        self.assertEqual(three[0]["llm_verdict"], "RELEVANT")
        self.assertEqual(three[0]["research_field"], "栽培生理")


class TestImportHumanReviewManualRows(unittest.TestCase):
    """人工复核导入：摘要为空的转审文献没有 llm_validation 行，判 Y 必须建行

    否则 _REVIEW_PASS_PREDICATE 匹配不到它，人工放行也导不出去。
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test.db"
        init_db(self.db_path)
        with get_conn(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO articles (pmid, title, abstract, pub_year, journal) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    ("1", "T1", "Abstract 1", 2020, "J"),
                    ("2", "T2", "", 1953, "J"),
                    ("3", "T3", "", 1955, "J"),
                ],
            )
            conn.execute(
                "INSERT INTO llm_validation (pmid, llm_verdict, reason, "
                "validated_at, research_field, human_review) "
                "VALUES ('1', 'NOT_RELEVANT', 'r1', '2026-01-01T00:00:00', '', NULL)"
            )
            conn.executemany(
                "INSERT INTO filter_log (pmid, stage, reason, filtered_at) "
                "VALUES (?, 'needs_review', 'abstract_empty', '2026-01-01T00:00:00')",
                [("2",), ("3",)],
            )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_review_csv(self, rows):
        path = Path(self.temp_dir) / "review.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=REVIEW_PENDING_FIELDS)
            writer.writeheader()
            for r in rows:
                writer.writerow(r)
        return path

    def _import(self, csv_path):
        import cleaner.llm_validator as mod
        orig_db, orig_out = mod.DB_PATH, mod.OUTPUT_DIR
        mod.DB_PATH, mod.OUTPUT_DIR = self.db_path, self.temp_dir
        try:
            return mod.import_human_review(str(csv_path))
        finally:
            mod.DB_PATH, mod.OUTPUT_DIR = orig_db, orig_out

    def _row(self, pmid, review, verdict, field=""):
        return {"pmid": pmid, "title": "T", "abstract": "", "pub_year": "1953",
                "journal": "J", "article_types": "Journal Article",
                "llm_verdict": verdict, "reason": "abstract_empty",
                "research_field": field, "human_review": review}

    def test_manual_yes_creates_validation_row(self):
        csv_path = self._write_review_csv([self._row("2", "Y", "NEEDS_REVIEW")])
        stats = self._import(csv_path)

        self.assertEqual(stats["passed"], 1)
        self.assertEqual(stats["manual_inserted"], 1)

        with get_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM llm_validation WHERE pmid='2'"
            ).fetchone()
        self.assertEqual(row["llm_verdict"], "RELEVANT")
        self.assertEqual(row["human_review"], "Y")
        # 未给方向 → 兜底默认值（不判失败）
        self.assertEqual(row["research_field"], DEFAULT_RESEARCH_FIELD)

    def test_manual_no_creates_not_relevant_row(self):
        csv_path = self._write_review_csv([self._row("3", "N", "NEEDS_REVIEW")])
        stats = self._import(csv_path)

        self.assertEqual(stats["rejected"], 1)
        self.assertEqual(stats["manual_inserted"], 1)

        with get_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM llm_validation WHERE pmid='3'"
            ).fetchone()
        self.assertEqual(row["llm_verdict"], "NOT_RELEVANT")
        self.assertEqual(row["human_review"], "N")
        # NOT_RELEVANT 不带方向
        self.assertEqual(row["research_field"], "")

    def test_manual_row_keeps_given_research_field(self):
        csv_path = self._write_review_csv(
            [self._row("2", "Y", "NEEDS_REVIEW", field="植物保护")]
        )
        self._import(csv_path)
        with get_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT research_field FROM llm_validation WHERE pmid='2'"
            ).fetchone()
        self.assertEqual(row["research_field"], "植物保护")

    def test_llm_rows_still_use_update_path(self):
        """已有 llm_validation 行的记录仍走 UPDATE，不新建重复行"""
        csv_path = self._write_review_csv([self._row("1", "Y", "NOT_RELEVANT")])
        stats = self._import(csv_path)

        self.assertEqual(stats["manual_inserted"], 0)
        with get_conn(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM llm_validation WHERE pmid='1'"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["human_review"], "Y")
        # UPDATE 路径不改 llm_verdict
        self.assertEqual(rows[0]["llm_verdict"], "NOT_RELEVANT")

    def test_manual_yes_becomes_exportable(self):
        """人工放行的转审文献必须能进最终导出（复核谓词链路打通）"""
        csv_path = self._write_review_csv([self._row("2", "Y", "NEEDS_REVIEW")])
        self._import(csv_path)

        import cleaner.llm_validator as mod
        orig_db, orig_out = mod.DB_PATH, mod.OUTPUT_DIR
        mod.DB_PATH, mod.OUTPUT_DIR = self.db_path, self.temp_dir
        try:
            path = mod._export_filtered_csv()
        finally:
            mod.DB_PATH, mod.OUTPUT_DIR = orig_db, orig_out

        self.assertIsNotNone(path)
        with open(path, "r", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertIn("2", [r["pmid"] for r in rows])


if __name__ == "__main__":
    unittest.main()
