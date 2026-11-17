"""相关全文库查询与清理测试。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from downloader import fulltext_library as lib
from utils.db import (
    CREATE_ARTICLES_SQL,
    CREATE_LLM_VALIDATION_SQL,
)


class TestRelevantFulltext(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.pdf_dir = root / "pdfs_relevant"
        self.pdf_dir.mkdir()
        self.db = root / "test.db"

        conn = sqlite3.connect(self.db)
        conn.execute(CREATE_ARTICLES_SQL)
        conn.execute(CREATE_LLM_VALIDATION_SQL)
        rows = [
            ("1", "RELEVANT", None),
            ("2", "NOT_RELEVANT", None),
            ("3", "NOT_RELEVANT", "Y"),
            ("4", "RELEVANT", "N"),
            ("5", "RELEVANT", None),
            ("x6", "RELEVANT", None),
        ]
        for pmid, verdict, human in rows:
            conn.execute("INSERT INTO articles (pmid) VALUES (?)", (pmid,))
            conn.execute(
                "INSERT INTO llm_validation (pmid, llm_verdict, human_review) "
                "VALUES (?,?,?)", (pmid, verdict, human),
            )
        # 站点补录的全文在 EXTERNAL_PDF_DIR，不能被 pdf/tdm 当成 PMID 文献去抓
        conn.execute("UPDATE articles SET source='potato_ai' WHERE pmid='x6'")
        conn.commit()
        conn.close()

        self._patch = patch.object(lib, "PDF_DIR", self.pdf_dir)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_relevant_pmids_uses_review_predicate(self):
        self.assertEqual(lib.relevant_pmids(self.db), {"1", "3", "5"})

    def test_existing_fulltext_path_checks_all_suffixes(self):
        (self.pdf_dir / "1.txt").write_text("full text", encoding="utf-8")
        self.assertEqual(lib.existing_fulltext_path("1").name, "1.txt")
        self.assertIsNone(lib.existing_fulltext_path("5"))

    def test_prune_removes_only_flipped_copies(self):
        for pmid in ("1", "2", "4"):
            (self.pdf_dir / f"{pmid}.pdf").write_bytes(b"%PDF")
        (self.pdf_dir / "notes.txt").write_text("keep", encoding="utf-8")

        stats = lib.prune_relevant_fulltext(self.db)

        self.assertEqual(sorted(p.name for p in self.pdf_dir.iterdir()),
                         ["1.pdf", "notes.txt"])
        self.assertEqual(stats, {"removed": 2, "relevant": 3})


if __name__ == "__main__":
    unittest.main()
