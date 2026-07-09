import unittest
from cleaner.llm_validator import _extract_json


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


if __name__ == "__main__":
    unittest.main()
