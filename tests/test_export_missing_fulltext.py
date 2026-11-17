"""本地缺失全文导出测试，不访问真实库或网络。"""

import csv
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from downloader import fulltext_library
from scripts import export_missing_fulltext as export
from utils.db import CREATE_LLM_VALIDATION_SQL


class TestMissingFulltextExport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.pdfs = root / "pdfs"
        self.pdfs.mkdir()
        self.db = root / "test.db"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE articles (pmid TEXT PRIMARY KEY, doi TEXT, "
                         "title TEXT, authors TEXT, pub_year INTEGER, journal TEXT, pmc_id TEXT, "
                         "source TEXT)")
            conn.execute(CREATE_LLM_VALIDATION_SQL)
            conn.executemany("INSERT INTO articles (pmid, doi, title, authors, pub_year, "
                             "journal, pmc_id) VALUES (?, ?, ?, ?, ?, ?, ?)", [
                ("1", "10.1234/LOCAL", "已有 PDF", None, None, None, None),
                ("2", "10.1234/xml", "已有 XML", None, None, None, None),
                ("3", "10.1234/txt", "已有 TXT", None, None, None, None),
                ("4", "DOI：https://doi.org/10.1234/A(B).", " 中文标题 ",
                 "First Author | Second Author", 2026, " Journal ", "PMC4"),
                ("5", "10.1234/a(b)", "重复 DOI", None, None, None, None),
                ("6", None, "无 DOI", None, None, None, None),
                ("7", "not-a-doi", "非法 DOI", None, None, None, None),
                ("8", "10.1234/local", "另一个 PMID 已有", None, None, None, None),
                ("9", "10.1234/partial", "仅下载临时文件", None, None, None, None),
                ("10", "10.1234/irrelevant", "不相关", None, None, None, None),
            ])
            conn.executemany(
                "INSERT INTO llm_validation (pmid, llm_verdict) VALUES (?, ?)",
                [(str(i), "RELEVANT") for i in range(1, 10)]
                + [("10", "NOT_RELEVANT")],
            )
        for name in ("1.pdf", "2.xml", "3.txt", "9.pdf.part"):
            (self.pdfs / name).write_bytes(b"existing")
        self.addCleanup(patch.stopall)
        patch.object(fulltext_library, "PDF_DIR", self.pdfs).start()
        patch.object(export, "OUTPUT_DIR", root / "output").start()

    def test_metadata_deduplication_and_three_suffixes(self):
        batch, manifest = export.collect_missing(self.db)
        self.assertEqual(len(manifest), 6)
        self.assertEqual([row["doi"] for row in batch],
                         ["10.1234/a(b)", "10.1234/local", "10.1234/partial"])
        self.assertEqual(batch[0], {"doi": "10.1234/a(b)", "title": "中文标题",
                                    "author": "First Author", "year": "2026",
                                    "journal": "Journal"})
        self.assertEqual(manifest[1]["batch_status"], "duplicate_doi")
        self.assertEqual(manifest[1]["batch_pmid"], "4")
        self.assertEqual(manifest[2]["batch_status"], "no_doi")
        self.assertEqual(manifest[3]["batch_status"], "invalid_doi")
        self.assertEqual(manifest[4]["existing_doi_pmid"], "1")
        self.assertNotIn("10", [row["pmid"] for row in manifest])
        self.assertEqual(batch[1]["year"], "")

    def test_encoding_output_and_read_only(self):
        original_db = self.db.read_bytes()
        originals = {p.name: p.read_bytes() for p in self.pdfs.iterdir()}
        batch_path, manifest_path, counts = export.export_missing_fulltext(self.db)
        self.assertFalse(batch_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertTrue(manifest_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertEqual(len(json.loads(batch_path.read_text(encoding="utf-8"))), 3)
        with manifest_path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 6)
        self.assertEqual(list(rows[0]), export.MANIFEST_FIELDS)
        self.assertEqual(counts, {"ready": 3, "duplicate_doi": 1, "no_doi": 1,
                                  "invalid_doi": 1, "missing": 6, "existing_doi": 1})
        next_batch, _, _ = export.export_missing_fulltext(self.db)
        self.assertNotEqual(batch_path, next_batch)
        self.assertTrue(batch_path.is_file())
        self.assertEqual(self.db.read_bytes(), original_db)
        self.assertEqual({p.name: p.read_bytes() for p in self.pdfs.iterdir()}, originals)

    def test_missing_database_is_not_created(self):
        missing_db = self.db.with_name("absent.db")
        with self.assertRaises(sqlite3.OperationalError):
            export.collect_missing(missing_db)
        self.assertFalse(missing_db.exists())

    def test_bad_pmid_fails_without_output(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO articles (pmid) VALUES ('../outside')")
            conn.execute(
                "INSERT INTO llm_validation (pmid, llm_verdict) "
                "VALUES ('../outside', 'RELEVANT')"
            )
        with self.assertRaisesRegex(ValueError, "非法 PMID"):
            export.export_missing_fulltext(self.db)
        self.assertFalse(Path(export.OUTPUT_DIR).exists())


class TestDOINormalization(unittest.TestCase):
    def test_supported_formats(self):
        examples = {
            "<http://dx.doi.org/10.1234/ABC>": "10.1234/abc",
            "doi: https://doi.org/10.1234/ABC": "10.1234/abc",
            "10.1016/S0022-2836(05)80360-2)": "10.1016/s0022-2836(05)80360-2",
            "10.1002/(SICI)1097-0258(19970815)16:15<1705::AID-SIM609>3.0.CO;2-Y":
                "10.1002/(sici)1097-0258(19970815)16:15<1705::aid-sim609>3.0.co;2-y",
            "10.1234/value]": "10.1234/value",
            "10.1234/[value]": "10.1234/[value]",
            "10.1234": None,
            "10.1234/with space": None,
            " ": None,
        }
        for raw, expected in examples.items():
            with self.subTest(raw=raw):
                self.assertEqual(export.normalize_doi(raw), expected)


if __name__ == "__main__":
    unittest.main()
