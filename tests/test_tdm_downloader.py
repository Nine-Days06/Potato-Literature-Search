"""TDM 下载器测试。

覆盖三类风险：
1. DOI 归渠道 —— 归错渠道会用错端点，白跑一遍限流配额；
2. 计划生成 —— 只读库、不发请求，只纳入复核通过的相关文献，并排除本地已有全文；
3. 确认闸门 —— 没 --yes 时绝不能发任何下载请求（用户明确要求下载前确认）。
"""
import csv
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from downloader import tdm_downloader as tdm
from utils.db import (
    CREATE_ARTICLES_SQL, CREATE_FILTER_LOG_SQL, CREATE_LLM_VALIDATION_SQL,
)


def _init_db(path: Path):
    import sqlite3
    conn = sqlite3.connect(path)
    try:
        conn.execute(CREATE_ARTICLES_SQL)
        conn.execute(CREATE_FILTER_LOG_SQL)
        conn.execute(CREATE_LLM_VALIDATION_SQL)
        conn.commit()
    finally:
        conn.close()


def _add(conn, pmid, doi=None, verdict="RELEVANT", human=None,
         title="t", journal="j", pub_year=2020, pmc_id=None):
    conn.execute(
        "INSERT INTO articles (pmid, title, journal, pub_year, doi, pmc_id) "
        "VALUES (?,?,?,?,?,?)",
        (pmid, title, journal, pub_year, doi, pmc_id),
    )
    conn.execute(
        "INSERT INTO llm_validation (pmid, llm_verdict, human_review) VALUES (?,?,?)",
        (pmid, verdict, human),
    )


class TestClassifyDoi(unittest.TestCase):
    def test_wiley_prefixes(self):
        for doi in ("10.1111/j.1365-3059.2010.02335.x",
                    "10.1002/ptr.1234", "10.1046/j.1365-3059.2001.00563.x"):
            self.assertEqual(tdm.classify_doi(doi), tdm.WILEY, doi)

    def test_elsevier_prefixes(self):
        for doi in ("10.1016/j.talanta.2004.11.006",
                    "10.1006/viro.1993.1234", "10.1074/jbc.M123.456"):
            self.assertEqual(tdm.classify_doi(doi), tdm.ELSEVIER, doi)

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(tdm.classify_doi("  10.1111/ABC  "), tdm.WILEY)

    def test_springer_prefixes(self):
        for doi in ("10.1007/s00122-001-0001-1", "10.1038/nature12373",
                    "10.1023/A:1001234567890", "10.1134/S1234567801010001"):
            self.assertEqual(tdm.classify_doi(doi), tdm.SPRINGER, doi)

    def test_other_publishers_return_none(self):
        for doi in ("10.1094/PHYTO.2000.90.3.220",
                    "10.1093/jxb/erx001", "10.3389/fpls.2020.00001"):
            self.assertIsNone(tdm.classify_doi(doi), doi)

    def test_empty_and_malformed_return_none(self):
        for doi in (None, "", "   ", "not-a-doi", "10.1111"):
            self.assertIsNone(tdm.classify_doi(doi), repr(doi))

    def test_prefix_boundary_not_confused(self):
        # 10.11110 不是 10.1111，前缀必须精确匹配
        self.assertIsNone(tdm.classify_doi("10.11110/xyz.2020.1"))


class TestBuildTdmPlan(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = self.root / "test.db"
        self.pdf_dir = self.root / "pdfs"
        self.pdf_dir.mkdir()
        _init_db(self.db)
        self._pdf_patch = patch.object(tdm, "PDF_DIR", self.pdf_dir)
        self._pdf_patch.start()

    def tearDown(self):
        self._pdf_patch.stop()
        self._tmp.cleanup()

    def _plan(self):
        return tdm.build_tdm_plan(self.db)

    def test_supported_channels_included(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        _add(conn, "1", doi="10.1111/a.1")
        _add(conn, "2", doi="10.1016/b.2")
        _add(conn, "3", doi="10.1007/s00122-001-0001-1")
        _add(conn, "4", doi="10.1094/PHYTO.2000.90.3.220")  # APS，无批量通道
        conn.commit()
        conn.close()

        plan = self._plan()
        self.assertEqual([p["pmid"] for p in plan], ["1", "2", "3"])
        self.assertEqual(plan[0]["publisher"], tdm.WILEY)
        self.assertEqual(plan[1]["publisher"], tdm.ELSEVIER)
        self.assertEqual(plan[2]["publisher"], tdm.SPRINGER)

    def test_not_relevant_excluded(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        _add(conn, "1", doi="10.1111/a.1", verdict="NOT_RELEVANT")
        conn.commit()
        conn.close()
        self.assertEqual([p["pmid"] for p in self._plan()], [])

    def test_human_review_overrides_model_verdict(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        _add(conn, "1", doi="10.1111/a.1", verdict="NOT_RELEVANT", human="Y")
        _add(conn, "2", doi="10.1111/b.2", verdict="RELEVANT", human="N")
        conn.commit()
        conn.close()
        self.assertEqual([p["pmid"] for p in self._plan()], ["1"])

    def test_existing_fulltext_skipped(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        _add(conn, "1", doi="10.1111/a.1")
        _add(conn, "2", doi="10.1111/b.2")
        _add(conn, "3", doi="10.1016/c.3")
        conn.commit()
        conn.close()

        (self.pdf_dir / "1.pdf").write_bytes(b"%PDF-1.4\n" + b"A" * 4000)
        (self.pdf_dir / "3.xml").write_bytes(b"<xocs:doc>" + b"A" * 4000)

        plan = self._plan()
        self.assertEqual([p["pmid"] for p in plan], ["2"])

    def test_tiny_existing_file_is_not_trusted(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        _add(conn, "1", doi="10.1111/a.1")
        conn.commit()
        conn.close()
        (self.pdf_dir / "1.pdf").write_bytes(b"x")  # 空壳/错误页残留
        self.assertEqual([p["pmid"] for p in self._plan()], ["1"])

    def test_missing_doi_excluded(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        _add(conn, "1", doi=None)
        _add(conn, "2", doi="")
        _add(conn, "3", doi="10.1111/c.3")
        conn.commit()
        conn.close()
        self.assertEqual([p["pmid"] for p in self._plan()], ["3"])


class TestPlanExportAndDescribe(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._out_patch = patch.object(tdm, "OUTPUT_DIR", self.root / "output")
        self._out_patch.start()

    def tearDown(self):
        self._out_patch.stop()
        self._tmp.cleanup()

    def test_plan_csv_written_to_tdm_subdir(self):
        plan = [{"pmid": "1", "doi": "10.1111/a.1", "publisher": tdm.WILEY,
                 "title": "T", "journal": "J", "pub_year": 2020, "pmc_id": ""}]
        path = tdm.export_tdm_plan(plan)
        self.assertTrue(path.exists())
        self.assertEqual(path.parent, self.root / "output" / "tdm")
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]["pmid"], "1")
        self.assertEqual(list(rows[0].keys()), tdm.PLAN_FIELDS)

    def test_empty_plan_writes_nothing(self):
        self.assertIsNone(tdm.export_tdm_plan([]))

    def test_latest_plan_returns_newest(self):
        plan = [{"pmid": "1", "doi": "10.1111/a.1", "publisher": tdm.WILEY,
                 "title": "", "journal": "", "pub_year": 2020, "pmc_id": ""}]
        first = tdm.export_tdm_plan(plan)
        first.rename(first.with_name("tdm_plan_20200101_000000.csv"))
        second = tdm.export_tdm_plan(plan)
        self.assertEqual(tdm.latest_tdm_plan(), second)

    def test_latest_plan_none_when_absent(self):
        self.assertIsNone(tdm.latest_tdm_plan())

    def test_describe_reports_missing_token(self):
        plan = [{"pmid": "1", "doi": "10.1111/a.1", "publisher": tdm.WILEY}]
        with patch.object(tdm, "WILEY_TDM_TOKEN", None):
            text = tdm.describe_plan(plan)
        self.assertIn("未配置 WILEY_TDM_TOKEN", text)

    def test_describe_estimates_elapsed(self):
        plan = [{"pmid": str(i), "doi": f"10.1111/a.{i}", "publisher": tdm.WILEY}
                for i in range(60)]
        with patch.object(tdm, "WILEY_TDM_TOKEN", "tok"), \
             patch.object(tdm, "WILEY_TDM_INTERVAL", 10.0):
            text = tdm.describe_plan(plan)
        self.assertIn("60 篇", text)
        self.assertIn("10 分钟", text)

    def test_describe_warns_elsevier_entitlement(self):
        plan = [{"pmid": "1", "doi": "10.1016/a.1", "publisher": tdm.ELSEVIER}]
        with patch.object(tdm, "ELSEVIER_API_KEY", "key"), \
             patch.object(tdm, "ELSEVIER_INSTTOKEN", None):
            text = tdm.describe_plan(plan)
        self.assertIn("只会返回摘要", text)

    def test_describe_empty_plan(self):
        self.assertIn("没有需要走 TDM", tdm.describe_plan([]))


class TestDownloadGate(unittest.TestCase):
    """没 --yes 时绝不能发下载请求（用户要求下载前确认）"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = self.root / "test.db"
        _init_db(self.db)
        import sqlite3
        conn = sqlite3.connect(self.db)
        _add(conn, "1", doi="10.1111/a.1")
        conn.commit()
        conn.close()
        self._patches = [
            patch.object(tdm, "PDF_DIR", self.root / "pdfs"),
            patch.object(tdm, "OUTPUT_DIR", self.root / "output"),
            patch.object(tdm, "WILEY_TDM_TOKEN", "11111111-1111-1111-1111-111111111111"),
        ]
        for p in self._patches:
            p.start()
        (self.root / "pdfs").mkdir()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _patched_sleep(self):
        """只 patch time.sleep：整体 patch tdm.time 会让 time.time() 变 MagicMock，
        耗时格式化直接抛 TypeError。"""
        return patch.object(tdm.time, "sleep")

    def test_no_confirm_does_not_touch_network(self):
        with patch.object(tdm, "download_one") as mocked:
            result = tdm.run_tdm_download(self.db, confirm=False)
        mocked.assert_not_called()
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["planned"], 1)
        self.assertEqual(result["success"], 0)

    def test_confirm_downloads(self):
        # 目标路径故意放在 PDF_DIR 之外：build_tdm_plan 会把 PDF_DIR 里已有文件
        # 当作「已下载」跳过，放在里面会让计划为空、测不到下载分支
        dest = self.root / "other" / "1.pdf"
        dest.parent.mkdir()
        dest.write_bytes(b"%PDF-1.4\n" + b"A" * 4000)
        with patch.object(tdm, "download_one", return_value=(True, "ok", dest)), \
             self._patched_sleep():
            result = tdm.run_tdm_download(self.db, confirm=True)
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["success"], 1)

    def test_reported_success_without_file_counts_as_failure(self):
        """渠道报成功但文件不在，不能算成功（否则统计虚高、失败清单漏项）"""
        dest = self.root / "other" / "1.pdf"  # 故意不创建
        with patch.object(tdm, "download_one", return_value=(True, "ok", dest)), \
             self._patched_sleep():
            result = tdm.run_tdm_download(self.db, confirm=True)
        self.assertEqual(result["success"], 0)
        self.assertEqual(result["failed"], 1)

    def test_missing_token_skips_channel(self):
        with patch.object(tdm, "WILEY_TDM_TOKEN", None), \
             patch.object(tdm, "download_one") as mocked:
            result = tdm.run_tdm_download(self.db, confirm=True)
        mocked.assert_not_called()
        self.assertEqual(result["planned"], 0)
        self.assertEqual(result["success"], 0)

    def test_limit_caps_plan(self):
        with patch.object(tdm, "download_one") as mocked, self._patched_sleep():
            result = tdm.run_tdm_download(self.db, confirm=True, limit=0)
        mocked.assert_not_called()
        self.assertEqual(result["total"], 0)


class TestDownloadWiley(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._token = patch.object(
            tdm, "WILEY_TDM_TOKEN", "11111111-1111-1111-1111-111111111111")
        self._sleep = patch.object(tdm.time, "sleep")
        self._token.start()
        self.fake_sleep = self._sleep.start()

    def tearDown(self):
        self._sleep.stop()
        self._token.stop()
        self._tmp.cleanup()

    def _resp(self, status=200, body=b"%PDF-1.4\n" + b"A" * 4000):
        resp = Mock()
        resp.status_code = status
        resp.iter_content = lambda chunk_size=65536: [body]
        return resp

    def test_success_saves_pdf(self):
        dest = self.root / "1.pdf"
        with patch.object(tdm, "_request", return_value=self._resp()):
            ok, status = tdm.download_wiley("10.1111/a.1", dest)
        self.assertTrue(ok)
        self.assertEqual(status, "ok")
        self.assertGreater(dest.stat().st_size, 2048)

    def test_403_is_access_denied_without_retry(self):
        dest = self.root / "1.pdf"
        with patch.object(tdm, "_request", return_value=self._resp(403)) as req:
            ok, status = tdm.download_wiley("10.1111/a.1", dest)
        self.assertFalse(ok)
        self.assertEqual(status, "access_denied")
        self.assertEqual(req.call_count, 1)

    def test_404_is_unknown_doi(self):
        with patch.object(tdm, "_request", return_value=self._resp(404)):
            ok, status = tdm.download_wiley("10.1111/a.1", self.root / "1.pdf")
        self.assertEqual((ok, status), (False, "unknown_doi"))

    def test_429_backs_off_then_gives_up(self):
        dest = self.root / "1.pdf"
        with patch.object(tdm, "_request", return_value=self._resp(429)) as req:
            ok, status = tdm.download_wiley("10.1111/a.1", dest)
        self.assertFalse(ok)
        self.assertEqual(status, "rate_limited")
        self.assertEqual(req.call_count, tdm.MAX_ATTEMPTS)
        self.assertTrue(self.fake_sleep.called)

    def test_oversized_response_rejected(self):
        dest = self.root / "1.pdf"
        big = b"%PDF-1.4\n" + b"A" * 5000
        with patch.object(tdm, "_request", return_value=self._resp(body=big)), \
             patch.object(tdm, "TDM_MAX_BYTES", 3000):
            ok, status = tdm.download_wiley("10.1111/a.1", dest)
        self.assertFalse(ok)
        self.assertIn("save_error", status)
        self.assertFalse(dest.exists())
        self.assertFalse(dest.with_name(dest.name + ".part").exists())

    def test_tiny_response_rejected(self):
        dest = self.root / "1.pdf"
        with patch.object(tdm, "_request", return_value=self._resp(body=b"err")):
            ok, status = tdm.download_wiley("10.1111/a.1", dest)
        self.assertFalse(ok)
        self.assertIn("save_error", status)

    def test_missing_token_short_circuits(self):
        with patch.object(tdm, "WILEY_TDM_TOKEN", None), \
             patch.object(tdm, "_request") as req:
            ok, status = tdm.download_wiley("10.1111/a.1", self.root / "1.pdf")
        self.assertEqual((ok, status), (False, "missing_token"))
        req.assert_not_called()

    def test_token_header_is_sent(self):
        dest = self.root / "1.pdf"
        with patch.object(tdm, "_request", return_value=self._resp()) as req:
            tdm.download_wiley("10.1111/a.1", dest)
        headers = req.call_args[0][1]
        self.assertEqual(headers[tdm.WILEY_TOKEN_HEADER], tdm.WILEY_TDM_TOKEN)


class TestDownloadSpringer(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._sleep = patch.object(tdm.time, "sleep")
        self._sleep.start()

    def tearDown(self):
        self._sleep.stop()
        self._tmp.cleanup()

    def _resp(self, status=200, body=b"%PDF-1.4\n" + b"A" * 4000, json_body=None):
        resp = Mock()
        resp.status_code = status
        resp.content = body
        resp.iter_content = Mock(return_value=iter([body]))
        resp.json = Mock(return_value=json_body or {})
        return resp

    def test_crossref_link_saved_as_pdf(self):
        meta = {"message": {"link": [{
            "URL": "https://link.springer.com/content/pdf/x.pdf",
            "content-type": "application/pdf",
            "intended-application": "text-mining",
        }]}}
        dest = self.root / "1.pdf"
        with patch.object(tdm, "_request", side_effect=[
                self._resp(json_body=meta), self._resp()]) as req:
            ok, status = tdm.download_springer("10.1007/x", dest)
        self.assertEqual((ok, status), (True, "ok"))
        self.assertEqual(req.call_args_list[1][0][0],
                         "https://link.springer.com/content/pdf/x.pdf")
        self.assertTrue(dest.read_bytes().startswith(b"%PDF"))

    def test_missing_crossref_link(self):
        with patch.object(tdm, "_request",
                          return_value=self._resp(json_body={"message": {}})):
            ok, status = tdm.download_springer("10.1007/x", self.root / "1.pdf")
        self.assertEqual((ok, status), (False, "no_link"))

    def test_crossref_unreachable_is_not_no_link(self):
        with patch.object(tdm, "_request",
                          side_effect=tdm.requests.ConnectionError("blocked")):
            ok, status = tdm.download_springer("10.1007/x", self.root / "1.pdf")
        self.assertFalse(ok)
        self.assertTrue(status.startswith("crossref_error"))

    def test_truncated_body_is_retried(self):
        meta = {"message": {"link": [{
            "URL": "https://link.springer.com/content/pdf/x.pdf",
            "content-type": "application/pdf",
            "intended-application": "text-mining",
        }]}}
        class Broken:
            status_code = 200

            def iter_content(self, chunk_size):
                raise tdm.requests.exceptions.ChunkedEncodingError("cut")

        broken = Broken()
        dest = self.root / "1.pdf"
        with patch.object(tdm, "_request", side_effect=[
                self._resp(json_body=meta), broken, self._resp()]):
            ok, status = tdm.download_springer("10.1007/x", dest)
        self.assertEqual((ok, status), (True, "ok"))

    def test_stalled_stream_times_out(self):
        meta = {"message": {"link": [{
            "URL": "https://link.springer.com/content/pdf/x.pdf",
            "content-type": "application/pdf",
            "intended-application": "text-mining",
        }]}}
        stalled = Mock()
        stalled.status_code = 200
        stalled.closed = False

        def close():
            stalled.closed = True

        def chunks(chunk_size):
            while not stalled.closed:
                time.sleep(0.01)
            raise tdm.requests.ConnectionError("closed by watchdog")

        stalled.close = close
        stalled.iter_content = chunks
        dest = self.root / "1.pdf"
        with patch.object(tdm, "_request", side_effect=[self._resp(json_body=meta)] + [stalled] * 3), \
                patch.object(tdm, "STREAM_DEADLINE", 0.05):
            ok, status = tdm.download_springer("10.1007/x", dest)
        self.assertFalse(ok)
        self.assertTrue(status.startswith("network_error"))
        self.assertFalse(dest.exists())

    def test_html_login_page_is_not_saved(self):
        meta = {"message": {"link": [{
            "URL": "https://link.springer.com/content/pdf/x.pdf",
            "content-type": "application/pdf",
            "intended-application": "text-mining",
        }]}}
        dest = self.root / "1.pdf"
        with patch.object(tdm, "_request", side_effect=[
                self._resp(json_body=meta), self._resp(body=b"<html>login</html>")]):
            ok, status = tdm.download_springer("10.1007/x", dest)
        self.assertEqual((ok, status), (False, "not_pdf"))
        self.assertFalse(dest.exists())


class TestDownloadElsevier(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._p1 = patch.object(tdm, "ELSEVIER_API_KEY", "key")
        self._p2 = patch.object(tdm, "time")
        self._p1.start()
        self.fake_time = self._p2.start()
        self.fake_time.sleep = Mock()

    def tearDown(self):
        self._p2.stop()
        self._p1.stop()
        self._tmp.cleanup()

    def _resp(self, status=200, body=b"<xocs:doc><ce:section/></xocs:doc>"):
        resp = Mock()
        resp.status_code = status
        resp.content = body
        return resp

    def test_fulltext_saved_as_xml(self):
        dest = self.root / "1.xml"
        with patch.object(tdm, "_request", return_value=self._resp()):
            ok, status = tdm.download_elsevier("10.1016/a.1", dest)
        self.assertEqual((ok, status), (True, "ok"))
        self.assertTrue(dest.exists())

    def test_coredata_only_means_no_entitlement(self):
        """有 key 无内容权限时 Elsevier 仍回 200，只有摘要没有正文"""
        body = b"<xocs:doc><coredata><dc:title>T</dc:title></coredata></xocs:doc>"
        dest = self.root / "1.xml"
        with patch.object(tdm, "_request", return_value=self._resp(body=body)):
            ok, status = tdm.download_elsevier("10.1016/a.1", dest)
        self.assertEqual((ok, status), (False, "no_entitlement"))
        self.assertFalse(dest.exists())

    def test_insttoken_header_when_configured(self):
        dest = self.root / "1.xml"
        with patch.object(tdm, "ELSEVIER_INSTTOKEN", "inst"), \
             patch.object(tdm, "_request", return_value=self._resp()) as req:
            tdm.download_elsevier("10.1016/a.1", dest)
        headers = req.call_args[0][1]
        self.assertEqual(headers["X-ELS-Insttoken"], "inst")
        self.assertEqual(headers["X-ELS-APIKey"], "key")

    def test_no_insttoken_header_when_absent(self):
        dest = self.root / "1.xml"
        with patch.object(tdm, "ELSEVIER_INSTTOKEN", None), \
             patch.object(tdm, "_request", return_value=self._resp()) as req:
            tdm.download_elsevier("10.1016/a.1", dest)
        self.assertNotIn("X-ELS-Insttoken", req.call_args[0][1])

    def test_missing_key_short_circuits(self):
        with patch.object(tdm, "ELSEVIER_API_KEY", None), \
             patch.object(tdm, "_request") as req:
            ok, status = tdm.download_elsevier("10.1016/a.1", self.root / "1.xml")
        self.assertEqual((ok, status), (False, "missing_key"))
        req.assert_not_called()


class TestDownloadOneDispatch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._p = patch.object(tdm, "PDF_DIR", self.root)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def test_wiley_targets_pdf(self):
        item = {"pmid": "1", "doi": "10.1111/a.1", "publisher": tdm.WILEY}
        with patch.object(tdm, "download_wiley", return_value=(True, "ok")) as fn:
            ok, status, dest = tdm.download_one(item)
        self.assertEqual(dest, self.root / "1.pdf")
        fn.assert_called_once()

    def test_elsevier_targets_xml(self):
        item = {"pmid": "1", "doi": "10.1016/a.1", "publisher": tdm.ELSEVIER}
        with patch.object(tdm, "download_elsevier", return_value=(True, "ok")) as fn:
            ok, status, dest = tdm.download_one(item)
        self.assertEqual(dest, self.root / "1.xml")

    def test_springer_targets_pdf(self):
        item = {"pmid": "1", "doi": "10.1007/a.1", "publisher": tdm.SPRINGER}
        with patch.object(tdm, "download_springer", return_value=(True, "ok")) as fn:
            ok, status, dest = tdm.download_one(item)
        self.assertEqual(dest, self.root / "1.pdf")
        fn.assert_called_once()

    def test_unknown_channel_rejected(self):
        item = {"pmid": "1", "doi": "10.1094/x", "publisher": "aps"}
        ok, status, _ = tdm.download_one(item)
        self.assertEqual((ok, status), (False, "unsupported_channel"))


class TestFulltextLookup(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._p = patch.object(tdm, "PDF_DIR", self.root)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def test_finds_any_supported_suffix(self):
        for suffix in (".pdf", ".xml", ".txt"):
            (self.root / f"9{suffix}").write_bytes(b"x" * 4000)
            found = tdm.local_fulltext_path("9")
            self.assertEqual(found, self.root / f"9{suffix}")
            (self.root / f"9{suffix}").unlink()

    def test_missing_returns_none(self):
        self.assertIsNone(tdm.local_fulltext_path("nope"))


class TestFailureExport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._p = patch.object(tdm, "OUTPUT_DIR", self.root)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def test_writes_to_failures_subdir(self):
        path = tdm.export_tdm_failures([
            {"pmid": "1", "doi": "10.1111/a.1", "publisher": "wiley",
             "status": "access_denied", "detail": "t"},
        ])
        self.assertEqual(path.parent, self.root / "failures")
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]["status"], "access_denied")

    def test_empty_writes_nothing(self):
        self.assertIsNone(tdm.export_tdm_failures([]))
