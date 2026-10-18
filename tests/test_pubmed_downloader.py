import unittest
from datetime import date, timedelta
from unittest.mock import Mock, patch

import requests

from config.settings import SEARCH_YEAR_MIN, SEARCH_YEAR_MAX
from downloader.pubmed_downloader import (
    fetch_pmid_list, _get, _post,
    SAFE_LEAF_COUNT, _split_date_range, _esearch_count,
)


def _esearch_json(count: str, webenv: str = "test_webenv", querykey: str = "1") -> dict:
    return {"esearchresult": {"count": count, "webenv": webenv, "querykey": querykey}}


class TestPubmedDownloaderDateRange(unittest.TestCase):
    """安全命中（不触发递归细分）时，主 esearch 走 POST 且带正确日期参数"""

    @patch("downloader.pubmed_downloader.time.sleep")
    @patch("downloader.pubmed_downloader._safe_json")
    @patch("downloader.pubmed_downloader._get")
    @patch("downloader.pubmed_downloader._post")
    def test_esearch_should_include_full_date_range_via_post(self, mock_post, mock_get, mock_safe_json, _mock_sleep):
        # 全局年份范围被 5 年初分段；mock 探数返回 2、叶子取数返回 2，
        # 所有 esearch 都返回同一份小结果
        mock_safe_json.return_value = _esearch_json("2")
        esearch_resp = Mock()
        efetch_resp = Mock()
        efetch_resp.text = "1\n2\n"
        mock_post.return_value = esearch_resp
        mock_get.return_value = efetch_resp

        pmids = fetch_pmid_list("potato")

        self.assertEqual(pmids, ["1", "2"])
        first_args, _ = mock_post.call_args_list[0]
        params = first_args[1]
        self.assertEqual(params["db"], "pubmed")
        self.assertEqual(params["retmax"], 0)
        self.assertEqual(params["datetype"], "pdat")
        self.assertEqual(params["mindate"], f"{SEARCH_YEAR_MIN}-01-01")
        all_maxdates = [
            c[0][1]["maxdate"] for c in mock_post.call_args_list
        ]
        self.assertIn(f"{SEARCH_YEAR_MAX}-12-31", all_maxdates)


class TestAdaptiveSlicing(unittest.TestCase):
    """自适应切分：超限对半递归、单日降级、去重（技术报告 §8）"""

    def test_split_range_returns_exclusive_adjacent_halves(self):
        lo, hi = date(2024, 1, 1), date(2024, 12, 31)
        left, right = _split_date_range(lo, hi)
        self.assertEqual(left[0], date(2024, 1, 1))
        self.assertEqual(left[1], date(2024, 7, 1))
        self.assertEqual(right[0], date(2024, 7, 2))
        self.assertEqual(right[1], date(2024, 12, 31))
        self.assertEqual(left[1] + timedelta(days=1), right[0])

    def test_split_range_single_day_returns_none(self):
        d = date(2024, 5, 1)
        self.assertIsNone(_split_date_range(d, d))

    @patch("downloader.pubmed_downloader._fetch_page_by_date")
    @patch("downloader.pubmed_downloader._esearch_count")
    def test_over_limit_range_is_bisected(self, mock_count, mock_fetch):
        """命中 [12000, 6000, 6000]：根段超限对半拆，两个叶子各自取全"""
        mock_count.side_effect = [12000, 6000, 6000]
        mock_fetch.side_effect = [["1"], ["2"]]

        from downloader.pubmed_downloader import _collect_range
        pmids = _collect_range("q", date(2024, 1, 1), date(2024, 12, 31))

        self.assertEqual(pmids, ["1", "2"])
        self.assertEqual(mock_count.call_count, 3)
        # 两次取数调用的日期区间互斥相邻
        first_range = mock_fetch.call_args_list[0][0][1:]
        second_range = mock_fetch.call_args_list[1][0][1:]
        self.assertLess(first_range[1], second_range[0])

    @patch("downloader.pubmed_downloader._fetch_page_by_date")
    @patch("downloader.pubmed_downloader._esearch_count")
    def test_exactly_safe_count_is_leaf(self, mock_count, mock_fetch):
        """命中恰好 9500：不拆分，直接翻页取全"""
        mock_count.return_value = SAFE_LEAF_COUNT
        mock_fetch.return_value = ["1"]

        from downloader.pubmed_downloader import _collect_range
        pmids = _collect_range("q", date(2024, 1, 1), date(2024, 12, 31))

        self.assertEqual(pmids, ["1"])
        mock_count.assert_called_once()
        mock_fetch.assert_called_once()

    @patch("downloader.pubmed_downloader.logger")
    @patch("downloader.pubmed_downloader._fetch_page_by_date")
    @patch("downloader.pubmed_downloader._esearch_count")
    def test_single_day_over_limit_degrades_without_recursion(self, mock_count, mock_fetch, mock_logger):
        """递归到单日仍超限：降级直接取全 + 告警，不死循环"""
        mock_count.return_value = 99000
        mock_fetch.return_value = ["1"]

        from downloader.pubmed_downloader import _collect_range
        pmids = _collect_range("q", date(2024, 5, 1), date(2024, 5, 1))

        self.assertEqual(pmids, ["1"])
        self.assertEqual(mock_count.call_count, 1)   # 未继续递归探数
        mock_fetch.assert_called_once()
        mock_logger.warning.assert_called()

    @patch("downloader.pubmed_downloader.time.sleep")
    @patch("downloader.pubmed_downloader._safe_json")
    @patch("downloader.pubmed_downloader._get")
    @patch("downloader.pubmed_downloader._post")
    def test_slices_are_deduped_preserving_order(self, mock_post, mock_get, mock_safe_json, _mock_sleep):
        """不同切片返回重叠 PMID 时保序去重"""
        mock_safe_json.return_value = _esearch_json("1")
        efetch_resp = Mock()
        efetch_resp.text = "7\n"
        mock_get.return_value = efetch_resp

        pmids = fetch_pmid_list("potato", mindate=2024, maxdate=2024)

        self.assertEqual(pmids, ["7"])               # 跨切片重复仅保留一份


class TestEsearchCount(unittest.TestCase):
    """探数接口：retmax=0 且走 POST"""

    @patch("downloader.pubmed_downloader._rate_limiter")
    @patch("downloader.pubmed_downloader._safe_json")
    @patch("downloader.pubmed_downloader._post")
    def test_count_uses_post_and_retmax_zero(self, mock_post, mock_safe_json, _limiter):
        resp = Mock()
        mock_post.return_value = resp
        mock_safe_json.return_value = _esearch_json("123")

        count = _esearch_count("potato", "2024-01-01", "2024-12-31")

        self.assertEqual(count, 123)
        args, kwargs = mock_post.call_args
        params = args[1]
        self.assertEqual(params["retmax"], 0)
        self.assertEqual(params["mindate"], "2024-01-01")
        self.assertEqual(params["maxdate"], "2024-12-31")


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


class TestPmidListProvenance(unittest.TestCase):
    """pmid_list.json 记录的来源检索词必须与实际检索一致。

    否则 `--query` 自定义检索后，增量对比会把整批文献当成「移除+新增」。
    """

    def _run(self, **kwargs):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from downloader.pubmed_downloader import download_xml_batches

        with tempfile.TemporaryDirectory() as tmp:
            with patch("downloader.pubmed_downloader._download_single_batch",
                       return_value=None):
                download_xml_batches(["1", "2"], out_dir=Path(tmp), **kwargs)
            with open(Path(tmp) / "pmid_list.json", encoding="utf-8") as f:
                return json.load(f)

    def test_custom_query_is_recorded(self):
        data = self._run(query="potato CRISPR")
        self.assertEqual(data["query"], "potato CRISPR")
        self.assertEqual(data["pmids"], ["1", "2"])

    def test_omitted_query_falls_back_to_settings(self):
        from config.settings import PUBMED_QUERY
        self.assertEqual(self._run()["query"], PUBMED_QUERY)


class TestStaleBatchArchiving(unittest.TestCase):
    """检索词变化时必须归档旧批次，否则断点续传会把旧数据当新数据跳过。

    实测事故：换检索式后重下 105 个批次全被跳过，实际一篇都没更新。
    """

    def _prepare(self, tmp, old_query):
        import json
        from pathlib import Path
        root = Path(tmp)
        (root / "batch_00000.xml").write_text("<x/>", encoding="utf-8")
        (root / "batch_00001.xml").write_text("<x/>", encoding="utf-8")
        with open(root / "pmid_list.json", "w", encoding="utf-8") as f:
            json.dump({"query": old_query, "total": 0, "pmids": []}, f)
        return root

    def _run(self, root, **kwargs):
        from unittest.mock import patch
        from downloader.pubmed_downloader import download_xml_batches
        with patch("downloader.pubmed_downloader._download_single_batch",
                   return_value=None):
            download_xml_batches(["1"], out_dir=root, **kwargs)

    def test_query_change_archives_old_batches(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = self._prepare(tmp, "old query")
            self._run(root, query="new query")
            self.assertEqual(list(Path(root).glob("batch_*.xml")), [])
            archived = list(Path(root).glob("_archive_*/batch_*.xml"))
            self.assertEqual(len(archived), 2)

    def test_same_query_keeps_batches(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = self._prepare(tmp, "same query")
            self._run(root, query="same query")
            self.assertEqual(len(list(Path(root).glob("batch_*.xml"))), 2)
            self.assertEqual(list(Path(root).glob("_archive_*")), [])

    def test_first_run_without_pmid_list_archives_nothing(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "batch_00000.xml").write_text("<x/>", encoding="utf-8")
            self._run(root, query="any query")
            self.assertEqual(len(list(root.glob("batch_*.xml"))), 1)


class TestBatchIndexContinuation(unittest.TestCase):
    """增量下载必须续写批次编号。

    从 0 重新编号会撞上已有文件被断点续传跳过，新增文献静默丢失。
    """

    def test_next_index_is_max_plus_one(self):
        import tempfile
        from pathlib import Path
        from downloader.pubmed_downloader import _next_batch_index
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for idx in (0, 3, 17):
                (root / f"batch_{idx:05d}.xml").write_text("<x/>", encoding="utf-8")
            self.assertEqual(_next_batch_index(root), 18)

    def test_next_index_on_empty_dir_is_zero(self):
        import tempfile
        from pathlib import Path
        from downloader.pubmed_downloader import _next_batch_index
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_next_batch_index(Path(tmp)), 0)

    def test_download_writes_new_files_beyond_existing(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from downloader.pubmed_downloader import download_xml_batches
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "batch_00000.xml").write_text("<old/>", encoding="utf-8")
            written = []

            def fake(idx, chunk, out_dir, batch_size, total, seq=None):
                path = Path(out_dir) / f"batch_{idx:05d}.xml"
                path.write_text("<new/>", encoding="utf-8")
                written.append(path.name)
                return path

            with patch("downloader.pubmed_downloader._download_single_batch", fake):
                download_xml_batches(["1", "2"], out_dir=root, batch_size=1)

            self.assertEqual(sorted(written),
                             ["batch_00001.xml", "batch_00002.xml"])
            # 旧文件未被覆盖
            self.assertEqual((root / "batch_00000.xml").read_text(encoding="utf-8"),
                             "<old/>")


if __name__ == "__main__":
    unittest.main()
