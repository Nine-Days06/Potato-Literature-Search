import unittest
from unittest.mock import patch, Mock
import io
import tarfile
import tempfile
import csv
from pathlib import Path

from downloader.pdf_downloader import (
    normalize_pmc_asset_url,
    normalize_pmc_id,
    fetch_oa_links,
    load_cached_oa_links,
    download_pdf_file,
    download_txt_from_tgz,
    download_pdf_from_tgz,
    download_with_fallback,
    export_oa_links_csv,
)


class TestPdfDownloaderUrlNormalize(unittest.TestCase):
    def test_convert_ftp_to_https_and_deprecated_prefix(self):
        old_url = "ftp://ftp.ncbi.nlm.nih.gov/pub/pmc/oa_pdf/bb/cc/jmdh-8-091.PMC4334330.pdf"
        expected = "https://ftp.ncbi.nlm.nih.gov/pub/pmc/deprecated/oa_pdf/bb/cc/jmdh-8-091.PMC4334330.pdf"
        self.assertEqual(normalize_pmc_asset_url(old_url), expected)

    def test_keep_already_migrated_url(self):
        url = "https://ftp.ncbi.nlm.nih.gov/pub/pmc/deprecated/oa_package/bb/cc/PMC4334330.tar.gz"
        self.assertEqual(normalize_pmc_asset_url(url), url)


class TestPmcIdNormalize(unittest.TestCase):
    def test_numeric_id_should_add_prefix(self):
        self.assertEqual(normalize_pmc_id("4334330"), "PMC4334330")

    def test_prefixed_id_should_uppercase(self):
        self.assertEqual(normalize_pmc_id("pmc4334330"), "PMC4334330")

    def test_invalid_id_should_return_none(self):
        self.assertIsNone(normalize_pmc_id("PMCABC"))
        self.assertIsNone(normalize_pmc_id(""))


class TestFetchOaLinks(unittest.TestCase):
    @patch("downloader.pdf_downloader.time.sleep")
    @patch("downloader.pdf_downloader.requests.get")
    def test_should_fetch_batch_and_parse_pdf_tgz(self, mock_get, _mock_sleep):
        def build_resp(xml_text: str):
            resp = Mock()
            resp.content = xml_text.encode("utf-8")
            resp.raise_for_status = Mock()
            return resp

        def fake_get(_url, params=None, timeout=45):
            self.assertEqual(timeout, 45)
            self.assertIn(("id", "PMC4334330"), params)
            self.assertIn(("id", "PMCXXXX"), params)
            return build_resp(
                "<OA><records><record id='PMC4334330'>"
                "<link format='pdf' href='ftp://ftp.ncbi.nlm.nih.gov/pub/pmc/oa_pdf/a/b/test.PMC4334330.pdf' />"
                "<link format='tgz' href='ftp://ftp.ncbi.nlm.nih.gov/pub/pmc/oa_package/a/b/PMC4334330.tar.gz' />"
                "</record></records>"
                "<error code='idDoesNotExist'>PMCXXXX</error></OA>"
            )

        mock_get.side_effect = fake_get

        result = fetch_oa_links(["PMC4334330", "PMCXXXX"])

        self.assertEqual(
            result["PMC4334330"]["pdf"],
            "https://ftp.ncbi.nlm.nih.gov/pub/pmc/deprecated/oa_pdf/a/b/test.PMC4334330.pdf",
        )
        self.assertEqual(
            result["PMC4334330"]["tgz"],
            "https://ftp.ncbi.nlm.nih.gov/pub/pmc/deprecated/oa_package/a/b/PMC4334330.tar.gz",
        )
        self.assertNotIn("PMCXXXX", result)
        self.assertEqual(mock_get.call_count, 2)

    @patch("downloader.pdf_downloader.time.sleep")
    @patch("downloader.pdf_downloader.requests.get")
    def test_should_retry_single_query_for_unresolved_ids(self, mock_get, _mock_sleep):
        def build_resp(xml_text: str):
            resp = Mock()
            resp.content = xml_text.encode("utf-8")
            resp.raise_for_status = Mock()
            return resp

        def fake_get(_url, params=None, timeout=45):
            if isinstance(params, list):
                return build_resp(
                    "<OA><records><record id='PMC1'>"
                    "<link format='pdf' href='ftp://ftp.ncbi.nlm.nih.gov/pub/pmc/oa_pdf/a/b/1.pdf' />"
                    "</record></records></OA>"
                )

            if params == {"id": "PMC2"}:
                return build_resp(
                    "<OA><records><record id='PMC2'>"
                    "<link format='tgz' href='ftp://ftp.ncbi.nlm.nih.gov/pub/pmc/oa_package/a/b/2.tar.gz' />"
                    "</record></records></OA>"
                )

            return build_resp("<OA></OA>")

        mock_get.side_effect = fake_get

        result = fetch_oa_links(["PMC1", "PMC2"])

        self.assertIn("PMC1", result)
        self.assertIn("PMC2", result)
        self.assertEqual(mock_get.call_count, 2)

    @patch("downloader.pdf_downloader.requests.get")
    def test_should_reuse_cached_links_without_remote_fetch(self, mock_get):
        result = fetch_oa_links(
            ["PMC1"],
            cached_links={"PMC1": {"pdf": "https://example.org/1.pdf"}},
        )

        self.assertEqual(result["PMC1"]["pdf"], "https://example.org/1.pdf")
        mock_get.assert_not_called()


class TestLoadCachedOaLinks(unittest.TestCase):
    def test_should_load_latest_cached_links_by_pmc_id(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            old_file = out_dir / "oa_download_links_20260101_010101.csv"
            new_file = out_dir / "oa_download_links_20260102_010101.csv"

            old_file.write_text(
                "pmid,pmc_id,label,pdf_url,tgz_url\n"
                "111,PMC1,高相关,https://old.example/1.pdf,\n",
                encoding="utf-8",
            )
            new_file.write_text(
                "pmid,pmc_id,label,pdf_url,tgz_url\n"
                "111,PMC1,高相关,https://new.example/1.pdf,\n"
                "222,PMC2,中相关,,https://new.example/2.tgz\n",
                encoding="utf-8",
            )

            cached = load_cached_oa_links(["PMC1", "PMC2", "PMC3"], out_dir=out_dir)

        self.assertEqual(cached["PMC1"]["pdf"], "https://new.example/1.pdf")
        self.assertEqual(cached["PMC2"]["tgz"], "https://new.example/2.tgz")
        self.assertNotIn("PMC3", cached)


class TestDownloadPdfFromTgz(unittest.TestCase):
    @patch("downloader.pdf_downloader.subprocess.run")
    def test_should_extract_pdf_file_from_tgz(self, mock_run):
        pdf_payload = b"%PDF-1.4\n" + (b"A" * 3000)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="paper.pdf")
            info.size = len(pdf_payload)
            tar.addfile(info, io.BytesIO(pdf_payload))

        tgz_bytes = buf.getvalue()

        def fake_run(cmd, capture_output=True, text=True, timeout=300, check=False):
            out_dir = Path(cmd[cmd.index("-d") + 1])
            out_name = cmd[cmd.index("-o") + 1]
            out_path = out_dir / out_name
            out_path.write_bytes(tgz_bytes)
            return Mock(returncode=0, stderr="", stdout="")

        mock_run.side_effect = fake_run

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "out.pdf"
            ok = download_pdf_from_tgz("https://example.org/a.tgz", dest)

            self.assertTrue(ok)
            self.assertTrue(dest.exists())
            self.assertGreater(dest.stat().st_size, 1024)


class TestDownloadTxtFromTgz(unittest.TestCase):
    @patch("downloader.pdf_downloader.subprocess.run")
    def test_should_extract_text_from_xml_when_no_txt_member(self, mock_run):
        paragraph = " ".join(["Potato disease resistance is important for breeding and genomic analysis."] * 8)
        xml = f"<article><body><p>{paragraph}</p></body></article>"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            content = xml.encode("utf-8")
            info = tarfile.TarInfo(name="article.nxml")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))

        tgz_bytes = buf.getvalue()

        def fake_run(cmd, capture_output=True, text=True, timeout=300, check=False):
            out_dir = Path(cmd[cmd.index("-d") + 1])
            out_name = cmd[cmd.index("-o") + 1]
            out_path = out_dir / out_name
            out_path.write_bytes(tgz_bytes)
            return Mock(returncode=0, stderr="", stdout="")

        mock_run.side_effect = fake_run

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "out.txt"
            ok = download_txt_from_tgz("https://example.org/a.tgz", dest)

            self.assertTrue(ok)
            self.assertTrue(dest.exists())
            self.assertIn("Potato disease resistance", dest.read_text(encoding="utf-8"))


class TestDownloadWithFallback(unittest.TestCase):
    @patch("downloader.pdf_downloader.download_pdf_from_tgz")
    @patch("downloader.pdf_downloader.download_txt_from_tgz")
    @patch("downloader.pdf_downloader.download_pdf_file")
    def test_should_try_txt_first_when_prefer_txt(self, mock_pdf, mock_txt, mock_pdf_from_tgz):
        mock_pdf.return_value = True
        mock_txt.return_value = True
        mock_pdf_from_tgz.return_value = True

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            result = download_with_fallback(
                links={"pdf": "https://example.org/a.pdf", "tgz": "https://example.org/a.tgz"},
                pdf_path=base / "a.pdf",
                txt_path=base / "a.txt",
                prefer_format="txt",
            )

        self.assertEqual(result, "txt")
        mock_txt.assert_called_once()
        mock_pdf.assert_not_called()
        mock_pdf_from_tgz.assert_not_called()

    @patch("downloader.pdf_downloader.download_pdf_from_tgz")
    @patch("downloader.pdf_downloader.download_pdf_file")
    def test_should_fallback_to_extract_pdf_from_tgz_when_pdf_link_missing(self, mock_pdf, mock_pdf_from_tgz):
        mock_pdf.return_value = False
        mock_pdf_from_tgz.return_value = True

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            result = download_with_fallback(
                links={"tgz": "https://example.org/a.tgz"},
                pdf_path=base / "a.pdf",
                txt_path=base / "a.txt",
                prefer_format="pdf",
            )

        self.assertEqual(result, "pdf")
        mock_pdf.assert_not_called()
        mock_pdf_from_tgz.assert_called_once()


class TestAria2Download(unittest.TestCase):
    @patch("downloader.pdf_downloader.subprocess.run")
    def test_should_download_pdf_file_via_aria2c(self, mock_run):
        pdf_payload = b"%PDF-1.4\n" + (b"B" * 3000)

        def fake_run(cmd, capture_output=True, text=True, timeout=300, check=False):
            out_dir = Path(cmd[cmd.index("-d") + 1])
            out_name = cmd[cmd.index("-o") + 1]
            out_path = out_dir / out_name
            out_path.write_bytes(pdf_payload)
            return Mock(returncode=0, stderr="", stdout="")

        mock_run.side_effect = fake_run

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "paper.pdf"
            ok = download_pdf_file("https://example.org/paper.pdf", dest)

            self.assertTrue(ok)
            self.assertTrue(dest.exists())
            self.assertGreater(dest.stat().st_size, 1024)

        self.assertTrue(mock_run.called)

    @patch("downloader.pdf_downloader.subprocess.run", side_effect=FileNotFoundError())
    def test_should_fail_when_aria2c_not_installed(self, _mock_run):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "paper.pdf"
            ok = download_pdf_file("https://example.org/paper.pdf", dest)

        self.assertFalse(ok)


class TestExportOaLinksCsv(unittest.TestCase):
    def test_should_export_required_columns_and_rows(self):
        oa_links = {
            "PMC1": {"pdf": "https://example.org/1.pdf"},
            "PMC2": {"tgz": "https://example.org/2.tgz"},
        }
        pmc_to_info = {
            "PMC1": {"pmid": "111", "label": "高相关"},
            "PMC2": {"pmid": "222", "label": "中相关"},
        }

        with tempfile.TemporaryDirectory() as td:
            out_path = export_oa_links_csv(oa_links, pmc_to_info, Path(td))
            self.assertTrue(out_path.exists())

            with open(out_path, "r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["pmid"], "111")
        self.assertEqual(rows[0]["pmc_id"], "PMC1")
        self.assertEqual(rows[0]["label"], "高相关")
        self.assertEqual(rows[0]["pdf_url"], "https://example.org/1.pdf")
        self.assertEqual(rows[1]["tgz_url"], "https://example.org/2.tgz")


if __name__ == "__main__":
    unittest.main()
