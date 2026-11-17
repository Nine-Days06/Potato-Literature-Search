"""全文库完整性体检的判定规则。"""

import tempfile
import unittest
from pathlib import Path

from downloader.fulltext_library import (
    _decode_glyph_names,
    _decode_decimal_glyphs,
    inspect_fulltext,
    judge_fulltext,
    normalize_title,
    title_coverage,
)
from scripts.build_redownload_targets import action_for, select_targets
from scripts.exclude_notice_fulltext import reason_for

TITLE = "Genetic diversity of potato late blight resistance in wild Solanum species"
BODY = " ".join([TITLE] * 40) + " " + " ".join(["introduction methods results"] * 200)
LOST_SPACE_TITLE = "Wounding induces the first enzyme of the shikimate pathway in Solanaceae"


class TestNormalize(unittest.TestCase):
    def test_normalize_and_coverage(self):
        self.assertEqual(normalize_title("Solanum tuberosum—L. (2019)"), "solanum tuberosum l 2019")
        self.assertEqual(title_coverage("", BODY), None)
        self.assertEqual(title_coverage(TITLE, TITLE), 1.0)
        self.assertLess(title_coverage("completely unrelated title about rice blast", BODY), 0.6)

    def test_hyphenated_linebreak_joined(self):
        self.assertEqual(normalize_title("po-\ntato"), "po tato")
        self.assertEqual(title_coverage("potato", "po-\ntato"), 1.0)

    def test_decimal_glyph_names_decoded(self):
        self.assertEqual(_decode_decimal_glyphs("/65/98/115/116/114/97/99/116"), "Abstract")
        self.assertEqual(_decode_decimal_glyphs("/G65/G66"), "AB")

    def test_glyph_names_decoded(self):
        self.assertEqual(_decode_glyph_names("/C65/C98/C99"), "Abc")
        self.assertEqual(_decode_glyph_names("/C650plain"), " plain")


class TestInspectTxt(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, text: str, suffix: str = ".txt") -> Path:
        path = self.dir / ("12345" + suffix)
        path.write_text(text, encoding="utf-8")
        return path

    def test_ok(self):
        row = inspect_fulltext(self.write(BODY), {"title": TITLE})
        self.assertEqual(row["verdict"], "ok")
        self.assertEqual(row["title_cov"], 1.0)

    def test_short_content(self):
        text = TITLE + " " + ("The effect of the treatment was small and the results "
                              "are in line with the findings of the earlier work " * 8)
        row = inspect_fulltext(self.write(text), {"title": TITLE})
        self.assertEqual(row["verdict"], "too_short")

    def test_short_notice_is_ok(self):
        text = "Erratum: Evaluation of the yield of abiotic stress treatments. " * 10
        row = judge_fulltext(text, {"title": TITLE, "article_types": "Published Erratum"}, pages=1)
        self.assertEqual(row["verdict"], "ok")
        self.assertEqual(row["reason"], "short_notice")

    def test_placeholder(self):
        text = "Reproduced with permission of the copyright owner.  Further reproduction prohibited."
        self.assertEqual(inspect_fulltext(self.write(text), {"title": TITLE})["verdict"], "placeholder")

    def test_scanned_watermark_is_no_text(self):
        text = "Reproduced with permission of the copyright owner.  Further reproduction prohibited."
        row = judge_fulltext(text, {"title": TITLE}, pages=1, has_scan=True)
        self.assertEqual(row["verdict"], "no_text")
        self.assertEqual(row["reason"], "scanned_page_watermark")

    def test_no_text_layer(self):
        self.assertEqual(inspect_fulltext(self.write("abc"), {"title": TITLE})["verdict"], "no_text")

    def test_title_mismatch(self):
        text = "A study about rice blast disease and nitrogen fertilizer. " * 80
        self.assertEqual(inspect_fulltext(self.write(text), {"title": TITLE})["verdict"], "mismatch")

    def test_missing_title_is_ok(self):
        self.assertEqual(inspect_fulltext(self.write(BODY), {})["verdict"], "ok")

    def test_placeholder_title_is_ok(self):
        row = inspect_fulltext(self.write(BODY), {"title": "[Not Available]."})
        self.assertEqual(row["verdict"], "ok")
        self.assertEqual(row["reason"], "no_db_title")

    def test_supplement_is_not_mismatch(self):
        text = "Supplemental data file for the article. " + \
               "The data and the results in the file are described in the text. " * 25
        row = inspect_fulltext(self.write(text), {"title": TITLE})
        self.assertEqual(row["verdict"], "supplement")

    def test_foreign_language_is_not_mismatch(self):
        text = ("Die Kartoffel und die Knollen des Bodens von aus bei mit nicht eine " * 10
                + "The English abstract is short and in the text. " * 5)
        row = inspect_fulltext(self.write(text), {"title": TITLE})
        self.assertEqual(row["verdict"], "non_english")

    def test_danish_is_not_mismatch(self):
        text = ("Flaming heart syndrome er en dansk artikel og den handler om falske minder "
                "af det samme som ikke er med i den tekst som er til " * 10
                + "The study and the results are in the text of the paper " * 5)
        row = inspect_fulltext(self.write(text), {"title": TITLE})
        self.assertEqual(row["verdict"], "non_english")

    def test_non_latin_is_not_unparsed(self):
        text = "Влияние удобрений на урожай картофеля и качество клубней " * 80
        row = inspect_fulltext(self.write(text), {"title": TITLE})
        self.assertEqual(row["verdict"], "non_english")
        self.assertEqual(row["reason"], "text_non_latin")

    def test_lost_word_spaces_is_ok(self):
        text = "WoundinginducesthefirstenzymeoftheshikimatepathwayinSolanaceae " + \
               "The of and in for to with are common words in the text. " * 40
        row = inspect_fulltext(self.write(text), {"title": LOST_SPACE_TITLE})
        self.assertEqual(row["verdict"], "ok")

    def test_short_title_ngram_match(self):
        text = "Viroidsandprionswerestudiedinthelaboratoryandtheresultsareinthetext " + \
               "The of and in for to with are common words in the text. " * 40
        row = inspect_fulltext(self.write(text), {"title": "Viroids and prions."})
        self.assertEqual(row["verdict"], "ok")
        self.assertEqual(row["reason"], "ngram_match")

    def test_publisher_front_page(self):
        text = ("This article was downloaded by: [University Library] On: 15 October 2014. "
                "Full Terms & Conditions of access and use can be found at the publisher site. " * 10
                + "The results of the study are described in the data and the text " * 20)
        row = inspect_fulltext(self.write(text), {"title": TITLE})
        self.assertEqual(row["verdict"], "front_page")


class TestInspectPdf(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_garbage_pdf_is_broken(self):
        path = self.dir / "12345.pdf"
        path.write_bytes(b"%PDF-1.4\n" + b"not a real pdf body " * 200)
        row = inspect_fulltext(path, {"title": TITLE})
        self.assertEqual(row["verdict"], "broken")
        self.assertIn("parse_error", row["reason"])

    def test_html_disguised_as_pdf_is_broken(self):
        path = self.dir / "12345.pdf"
        path.write_bytes(b"<html><body>login required</body></html>" * 100)
        row = inspect_fulltext(path, {"title": TITLE})
        self.assertEqual(row["verdict"], "broken")
        self.assertEqual(row["reason"], "not_pdf")

class TestRedownloadTargets(unittest.TestCase):

    def test_notice_exclusion_rules(self):
        self.assertEqual(reason_for("Journal Article|Retracted Publication", "1", ""), "retracted_original")
        self.assertEqual(reason_for("Retraction Notice", "2", "3"), "retraction_notice")
        self.assertEqual(reason_for("Published Erratum", "4", "999999999"), "orphan_erratum")
        self.assertEqual(reason_for("Published Erratum", "4", ""), "orphan_erratum")
        self.assertEqual(reason_for("Journal Article", "5", ""), "")

    def test_select_excludes_scanned_and_no_doi(self):
        rows = [
            {"pmid": "1", "file": "1.pdf", "verdict": "no_text", "reason": "scanned_page_watermark"},
            {"pmid": "2", "file": "2.pdf", "verdict": "no_text", "reason": "no_text_layer"},
            {"pmid": "3", "file": "3.pdf", "verdict": "broken", "reason": "parse_error: x"},
            {"pmid": "4", "file": "4.pdf", "verdict": "mismatch", "reason": "mismatch"},
            {"pmid": "5", "file": "5.pdf", "verdict": "non_english", "reason": "text_non_latin"},
            {"pmid": "6", "file": "6.pdf", "verdict": "no_text", "reason": "no_text_layer"},
            {"pmid": "7", "file": "7.pdf", "verdict": "weak", "reason": "title_partial"},
        ]
        records = {
            "1": {"doi": "a"}, "2": {"doi": "b"}, "3": {"doi": "c"},
            "4": {"doi": "d"}, "5": {"doi": ""}, "6": {}, "7": {"doi": "e"},
        }
        targets = select_targets(rows, records)
        self.assertEqual([t["pmid"] for t in targets], ["3", "2", "4"])
        self.assertEqual([t["priority"] for t in targets], [0, 2, 3])
        self.assertEqual(action_for("mismatch"), "replace")
        self.assertEqual(action_for("broken"), "delete")


if __name__ == "__main__":
    unittest.main()
