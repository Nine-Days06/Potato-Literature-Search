import unittest
import json
import tempfile
import os
from pathlib import Path
from cleaner.llm_validator import (
    _extract_json,
    _build_per_article_prompt,
    _build_jsonl,
    _parse_batch_results,
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


if __name__ == "__main__":
    unittest.main()
