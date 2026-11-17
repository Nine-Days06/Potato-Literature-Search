"""站点补录（potato-ai.top）入库与标记的单元测试，不碰真实库。"""

import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import backfill_pubmed_by_doi, import_site_literature
from utils.db import init_db


def _row(doi: str, *, in_pubmed: str = "N", abstract: str = "x" * 100) -> dict:
    return {"doi": doi, "title": f"title {doi}", "pub_year": "2020",
            "journal": "Agronomy", "publisher": "", "cr_type": "journal-article",
            "abstract": abstract, "abstract_source": "openalex", "oa_pdf_url": "",
            "oa_status": "closed", "pmid": "", "in_pubmed": in_pubmed}


class TestExternalId(unittest.TestCase):
    def test_stable_and_short(self):
        one = import_site_literature.external_id("10.1016/J.LWT.2025.117858")
        two = import_site_literature.external_id("10.1016/j.lwt.2025.117858")
        self.assertEqual(one, two)          # 大小写归一
        self.assertEqual(len(one), 13)      # 'x' + 12 位
        self.assertNotIn("/", one)          # 不会破坏 <id>.pdf 命名


class TestLoadRows(unittest.TestCase):
    def test_skips_pubmed_and_dedups(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.csv"
            rows = [_row("10.1/a"), _row("10.1/a"), _row("10.1/b", in_pubmed="Y")]
            with path.open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            loaded = import_site_literature.load_rows(path)
        self.assertEqual([r["doi"] for r in loaded], ["10.1/a"])


class TestImportWrites(unittest.TestCase):
    def test_import_marks_pass_and_relevant(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            init_db(db)
            csv_path = Path(tmp) / "h.csv"
            with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(_row("10.1/a")))
                writer.writeheader()
                writer.writerow(_row("10.1/a", abstract=""))   # 无摘要也要入库
            with patch.object(import_site_literature, "DB_PATH", db), \
                    patch.object(import_site_literature, "init_db", init_db):
                with patch("sys.argv", ["x", "--csv", str(csv_path), "--apply"]):
                    import_site_literature.main()
            conn = sqlite3.connect(db)
            pmid, source = conn.execute("SELECT pmid, source FROM articles").fetchone()
            stage = conn.execute("SELECT stage FROM filter_log WHERE pmid=?", (pmid,)).fetchone()[0]
            verdict, field = conn.execute(
                "SELECT llm_verdict, research_field FROM llm_validation WHERE pmid=?",
                (pmid,)).fetchone()
            conn.close()
        self.assertEqual(source, "potato_ai")
        self.assertEqual(stage, "hard_filter_pass")
        self.assertEqual(verdict, "RELEVANT")
        self.assertEqual(field, "其他")


class TestMarkRelevant(unittest.TestCase):
    def test_overrides_needs_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            init_db(db)
            conn = sqlite3.connect(db)
            conn.execute("INSERT INTO articles (pmid, title) VALUES ('1', 't')")
            conn.execute("INSERT INTO filter_log (pmid, stage, reason) "
                         "VALUES ('1', 'needs_review', 'abstract_empty')")
            conn.commit()
            conn.close()
            backfill_pubmed_by_doi.mark_relevant(["1"], db)
            conn = sqlite3.connect(db)
            stage = conn.execute("SELECT stage FROM filter_log WHERE pmid='1'").fetchone()[0]
            verdict = conn.execute("SELECT llm_verdict FROM llm_validation").fetchone()[0]
            conn.close()
        self.assertEqual(stage, "hard_filter_pass")
        self.assertEqual(verdict, "RELEVANT")


if __name__ == "__main__":
    unittest.main()
