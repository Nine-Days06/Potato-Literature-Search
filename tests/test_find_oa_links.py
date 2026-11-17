import csv
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import find_oa_links  # noqa: E402
import downloader.fulltext_library as fulltext_library  # noqa: E402


class TestExtractCandidates(unittest.TestCase):
    def test_collects_best_then_alternates(self):
        payload = {
            "open_access": {"oa_status": "green"},
            "best_oa_location": {"pdf_url": "https://a/p.pdf", "host_type": "repository",
                                 "landing_page_url": "https://a/x"},
            "locations": [{"pdf_url": "https://a/p.pdf"}, {"pdf_url": "https://b/p.pdf"}, {}],
        }
        status, urls, host, landing = find_oa_links.extract_candidates(payload)
        self.assertEqual(status, "green")
        self.assertEqual(urls, ["https://a/p.pdf", "https://b/p.pdf"])
        self.assertEqual(host, "repository")
        self.assertEqual(landing, "https://a/x")

    def test_closed_has_no_urls(self):
        self.assertEqual(find_oa_links.extract_candidates({}), ("closed", [], "", ""))


class TestFindLinks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.db = base / "t.db"
        self.pdf_dir = base / "pdfs"
        self.pdf_dir.mkdir()
        self.out = base / "output"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE articles (pmid TEXT, doi TEXT, title TEXT, authors TEXT,"
                         " pub_year TEXT, journal TEXT, pmc_id TEXT, source TEXT)")
            conn.execute(
                "CREATE TABLE llm_validation (pmid TEXT PRIMARY KEY, llm_verdict TEXT,"
                " human_review TEXT, reason TEXT, research_field TEXT,"
                " validated_at TEXT, model TEXT)"
            )
            conn.executemany("INSERT INTO articles (pmid, doi, title, authors, pub_year, "
                             "journal, pmc_id) VALUES (?,?,?,?,?,?,?)", [
                ("1", "10.1234/a", "A", "X", "2001", "J", ""),
                ("2", "10.1234/b", "B", "Y", "2002", "J", ""),
            ])
            conn.executemany(
                "INSERT INTO llm_validation (pmid, llm_verdict) VALUES (?, 'RELEVANT')",
                [("1",), ("2",)],
            )
            conn.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_manifest_and_counts(self):
        payloads = {
            "10.1234/a": {"open_access": {"oa_status": "gold"},
                       "best_oa_location": {"pdf_url": "https://x/a.pdf", "host_type": "publisher"}},
            "10.1234/b": {"open_access": {"oa_status": "closed"}},
        }
        with patch.object(find_oa_links, "lookup", side_effect=payloads.get), \
                patch.object(find_oa_links, "OUTPUT_DIR", self.out), \
                patch.object(fulltext_library, "PDF_DIR", self.pdf_dir):
            path, counters = find_oa_links.find_links(db_path=self.db, workers=1)
        self.assertEqual(counters["gold"], 1)
        self.assertEqual(counters["closed"], 1)
        rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
        self.assertEqual([r["pmid"] for r in rows], ["1", "2"])
        self.assertEqual(rows[0]["pdf_urls"], "https://x/a.pdf")
        self.assertEqual(rows[1]["pdf_urls"], "")


if __name__ == "__main__":
    unittest.main()
