import unittest
from unittest.mock import Mock, patch

import requests

from config.settings import SEARCH_YEAR_MIN, SEARCH_YEAR_MAX
from downloader.pubmed_downloader import fetch_pmid_list, _get, _post


class TestPubmedDownloaderDateRange(unittest.TestCase):
    @patch("downloader.pubmed_downloader.time.sleep")
    @patch("downloader.pubmed_downloader._safe_json")
    @patch("downloader.pubmed_downloader._get")
    def test_esearch_should_include_2020_to_now_date_range(self, mock_get, mock_safe_json, _mock_sleep):
        mock_safe_json.return_value = {
            "esearchresult": {
                "count": "2",
                "webenv": "test_webenv",
                "querykey": "1",
            }
        }

        esearch_resp = Mock()
        efetch_resp = Mock()
        efetch_resp.text = "1\n2\n"
        mock_get.side_effect = [esearch_resp, efetch_resp]

        pmids = fetch_pmid_list("potato")

        self.assertEqual(pmids, ["1", "2"])
        first_call_args, _ = mock_get.call_args_list[0]
        params = first_call_args[1]
        self.assertEqual(params["datetype"], "pdat")
        self.assertEqual(params["mindate"], str(SEARCH_YEAR_MIN))
        self.assertEqual(params["maxdate"], str(SEARCH_YEAR_MAX))


def _resp(status_code: int) -> Mock:
    """构造一个最小可用的 Response mock（raise_for_status 为空操作）"""
    r = Mock()
    r.status_code = status_code
    r.raise_for_status = Mock()
    return r


class TestRequestRetry(unittest.TestCase):
    """_get / _post 共用 _request_with_retry 的重试语义特征化测试"""

    @patch("downloader.pubmed_downloader.time.sleep")
    @patch("downloader.pubmed_downloader.requests.post")
    @patch("downloader.pubmed_downloader.requests.get")
    def test_get_success_calls_requests_get_once(self, mock_get, mock_post, mock_sleep):
        """_get 成功：用 params= 调 requests.get 一次，timeout=60，不调 requests.post"""
        ok = _resp(200)
        mock_get.return_value = ok
        params = {"db": "pubmed", "id": "1,2"}

        result = _get("https://example.test/esearch.fcgi", params)

        self.assertIs(result, ok)
        mock_get.assert_called_once_with("https://example.test/esearch.fcgi", params=params, timeout=60)
        mock_post.assert_not_called()
        mock_sleep.assert_not_called()

    @patch("downloader.pubmed_downloader.time.sleep")
    @patch("downloader.pubmed_downloader.requests.post")
    @patch("downloader.pubmed_downloader.requests.get")
    def test_post_success_calls_requests_post_with_data(self, mock_get, mock_post, mock_sleep):
        """_post 成功：用 data= 调 requests.post 一次，timeout=60，不调 requests.get"""
        ok = _resp(200)
        mock_post.return_value = ok
        params = {"db": "pubmed", "term": "potato"}

        result = _post("https://example.test/esearch.fcgi", params)

        self.assertIs(result, ok)
        mock_post.assert_called_once_with("https://example.test/esearch.fcgi", data=params, timeout=60)
        mock_get.assert_not_called()
        mock_sleep.assert_not_called()

    @patch("downloader.pubmed_downloader.time.sleep")
    @patch("downloader.pubmed_downloader.requests.get")
    def test_get_retries_after_429_then_succeeds(self, mock_get, mock_sleep):
        """429 一次后 200：退避 2**1=2 秒后重试，返回 200 响应"""
        ok = _resp(200)
        mock_get.side_effect = [_resp(429), ok]

        result = _get("https://example.test/efetch.fcgi", {"id": "1"})

        self.assertIs(result, ok)
        self.assertEqual(mock_get.call_count, 2)
        mock_sleep.assert_called_once_with(2)

    @patch("downloader.pubmed_downloader.time.sleep")
    @patch("downloader.pubmed_downloader.requests.get")
    def test_get_all_attempts_429_returns_none_implicitly(self, mock_get, mock_sleep):
        """全部尝试都 429：循环耗尽后隐式返回 None（不抛异常），退避 2/4/8 秒"""
        mock_get.side_effect = [_resp(429), _resp(429), _resp(429)]

        result = _get("https://example.test/efetch.fcgi", {"id": "1"}, retries=3)

        self.assertIsNone(result)
        self.assertEqual(mock_get.call_count, 3)
        self.assertEqual([c.args[0] for c in mock_sleep.call_args_list], [2, 4, 8])

    @patch("downloader.pubmed_downloader.time.sleep")
    @patch("downloader.pubmed_downloader.requests.get")
    def test_get_retries_after_request_exception_then_succeeds(self, mock_get, mock_sleep):
        """RequestException 一次后 200：退避 2**1=2 秒后重试，返回 200 响应"""
        ok = _resp(200)
        mock_get.side_effect = [requests.RequestException("boom"), ok]

        result = _get("https://example.test/efetch.fcgi", {"id": "1"})

        self.assertIs(result, ok)
        self.assertEqual(mock_get.call_count, 2)
        mock_sleep.assert_called_once_with(2)

    @patch("downloader.pubmed_downloader.time.sleep")
    @patch("downloader.pubmed_downloader.requests.get")
    def test_get_reraises_request_exception_on_final_attempt(self, mock_get, mock_sleep):
        """最后一次尝试仍抛 RequestException：向上抛出，只退避一次"""
        mock_get.side_effect = [requests.RequestException("boom"), requests.RequestException("boom")]

        with self.assertRaises(requests.RequestException):
            _get("https://example.test/efetch.fcgi", {"id": "1"}, retries=2)

        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(mock_sleep.call_count, 1)
        mock_sleep.assert_called_once_with(2)

    @patch("downloader.pubmed_downloader.time.sleep")
    @patch("downloader.pubmed_downloader.requests.get")
    def test_get_honors_retries_argument_of_one(self, mock_get, mock_sleep):
        """retries=1：429 时只尝试一次即结束，隐式返回 None"""
        mock_get.side_effect = [_resp(429)]

        result = _get("https://example.test/efetch.fcgi", {"id": "1"}, retries=1)

        self.assertIsNone(result)
        self.assertEqual(mock_get.call_count, 1)
        mock_sleep.assert_called_once_with(2)


if __name__ == "__main__":
    unittest.main()
