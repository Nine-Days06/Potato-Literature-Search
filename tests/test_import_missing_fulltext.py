"""外部 DOI 命名 PDF 回填测试，不触碰真实目录。"""

import csv
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from downloader import fulltext_library
from scripts import import_missing_fulltext as importer


class TestImportMissingFulltext(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source = root / "downloads"
        self.source.mkdir()
        self.pdfs = root / "pdfs"
        self.pdfs.mkdir()
        self.manifest = root / "manifest.csv"
        rows = [
            ("1", "10.1016/a(b)", "ready"),
            ("2", "10.1002/c", "ready"),
            ("3", "10.1002/d", "ready"),
            ("4", "10.1002/e", "ready"),
            ("5", "10.1002/f", "duplicate_doi"),
        ]
        with self.manifest.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["pmid", "normalized_doi", "batch_status"])
            writer.writerows(rows)

        valid = b"%PDF-1.4" + b"A" * 20000
        (self.source / "10.1016_a(b).pdf").write_bytes(valid)
        (self.source / "10.1002_c.pdf").write_bytes(valid)
        (self.source / "10.1002_d.pdf").write_bytes(b"<html>login</html>" + b"A" * 20000)
        (self.pdfs / "4.txt").write_text("already here", encoding="utf-8")
        (self.source / "10.1002_unrelated.pdf").write_bytes(valid)

        self.addCleanup(patch.stopall)
        patch.object(fulltext_library, "PDF_DIR", self.pdfs).start()
        patch.object(importer, "PDF_DIR", self.pdfs).start()

    def test_plan_classifies_every_row(self):
        rows = importer.plan_import(self.manifest, self.source)
        self.assertEqual([r["action"] for r in rows], ["ready", "ready", "bad_pdf", "exists"])
        self.assertEqual([r["pmid"] for r in rows], ["1", "2", "3", "4"])
        self.assertEqual(importer.unmatched_sources(self.source, rows), ["10.1002_unrelated.pdf"])

    def test_dry_run_writes_nothing(self):
        before = sorted(p.name for p in self.pdfs.iterdir())
        counts = importer.run_import(self.manifest, self.source, apply=False)
        self.assertEqual(dict(counts), {"ready": 2, "bad_pdf": 1, "exists": 1})
        self.assertEqual(sorted(p.name for p in self.pdfs.iterdir()), before)

    def test_apply_copies_only_valid_and_never_overwrites(self):
        counts = importer.run_import(self.manifest, self.source, apply=True)
        self.assertEqual(dict(counts), {"ready": 2, "bad_pdf": 1, "exists": 1})
        self.assertEqual(sorted(p.name for p in self.pdfs.iterdir()),
                         ["1.pdf", "2.pdf", "4.txt"])
        self.assertEqual((self.pdfs / "1.pdf").read_bytes()[:4], b"%PDF")
        self.assertEqual((self.pdfs / "4.txt").read_text(encoding="utf-8"), "already here")
        self.assertTrue((self.source / "10.1016_a(b).pdf").exists())

    def test_stem_matches_target_naming(self):
        self.assertEqual(importer.doi_to_stem("10.1016/s0022-2836(05)80360-2"),
                         "10.1016_s0022-2836(05)80360-2")
        self.assertEqual(
            importer.doi_to_stem("10.1002/(SICI)1097-0258(19970815)16:15<1705::AID-SIM609>3.0.CO;2-Y"),
            "10.1002_(SICI)1097-0258(19970815)16_15_1705_AID-SIM609_3.0.CO;2-Y")
        # tidy 先把连续 _ / - 折成 " - "，所以回填只能正向算名字，不能反解析。
        self.assertEqual(importer.doi_to_stem("10.1234/a__b"), "10.1234_a - b")
        self.assertEqual(importer.doi_to_stem("10.1234/x", max_bytes=8), "10.1234")


if __name__ == "__main__":
    unittest.main()
