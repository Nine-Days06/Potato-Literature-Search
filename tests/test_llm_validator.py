import unittest
import json
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
)


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

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

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
        success, failed = parser(path)
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
        success, failed = parser(path)
        self.assertEqual(success, 1, "markdown 包裹的单 JSON 对象应能成功解析")
        self.assertEqual(len(failed), 0)

    def test_parse_failed_status_code(self):
        result_line = {
            "custom_id": "FAIL01",
            "response": {"status_code": 500, "body": {}}
        }
        path = self._make_result_jsonl([result_line])
        from cleaner.llm_validator import _parse_batch_results as parser
        success, failed = parser(path)
        self.assertEqual(success, 0)
        self.assertEqual(len(failed), 1)

    def test_parse_invalid_json_line(self):
        path = os.path.join(self.temp_dir, "invalid.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("this is not json\n")
        from cleaner.llm_validator import _parse_batch_results as parser
        success, failed = parser(path)
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
        success, failed = parser(path)
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
            success, failed = parser(path)
            self.assertEqual(success, 0, content)
            self.assertEqual(len(failed), 1, content)


class TestBuildLogRows(unittest.TestCase):
    """回归测试：SQL 查询不再返回 label 列，log_rows 必须为 4 元组"""

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
            "1": {"pmid": "1", "verdict": "RELEVANT", "reason": "r1"},
            "2": {"pmid": "2", "verdict": "NOT_RELEVANT", "reason": "r2"},
        }
        log_rows, failed = _build_log_rows(batch, pmid_to_result, set(), "now")
        self.assertEqual(len(log_rows), 2)
        self.assertEqual(failed, [])
        for row in log_rows:
            self.assertEqual(len(row), 4)
        self.assertEqual(log_rows[0], ("1", "RELEVANT", "r1", "now"))

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


class TestCountVerdicts(unittest.TestCase):
    """回归测试：log_rows 为 4 元组，统计解包不能按 5 元组"""

    def test_counts_four_tuple_rows(self):
        rows = [
            ("1", "RELEVANT", "r1", "now"),
            ("2", "NOT_RELEVANT", "r2", "now"),
            ("3", "RELEVANT", "r3", "now"),
        ]
        self.assertEqual(
            _count_verdicts(rows),
            {"RELEVANT": 2, "NOT_RELEVANT": 1},
        )

    def test_empty(self):
        self.assertEqual(_count_verdicts([]), {})


if __name__ == "__main__":
    unittest.main()
