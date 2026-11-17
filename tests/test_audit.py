import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.audit import record


class AuditRecordTest(unittest.TestCase):
    def test_record_writes_jsonl_with_file_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "12345.pdf"
            pdf.write_bytes(b"x" * 2048)
            with patch("utils.audit.OUTPUT_DIR", Path(tmp)):
                record("delete", "12345", pdf, verdict="mismatch", reason="integrity_mismatch")
            lines = (Path(tmp) / "logs" / "fulltext_audit.jsonl").read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(row["op"], "delete")
        self.assertEqual(row["pmid"], "12345")
        self.assertEqual(row["file"], "12345.pdf")
        self.assertEqual(row["md5"], hashlib.md5(b"x" * 2048).hexdigest())
        self.assertEqual(row["size_kb"], 2.0)
        self.assertEqual(row["verdict"], "mismatch")
        self.assertTrue(row["ts"])

    def test_record_appends_and_drops_none_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("utils.audit.OUTPUT_DIR", Path(tmp)):
                record("download", "1", doi="10.1/x", url=None)
                record("download", "2", doi="10.2/y")
            lines = (Path(tmp) / "logs" / "fulltext_audit.jsonl").read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(lines), 2)
        first, second = (json.loads(line) for line in lines)
        self.assertNotIn("url", first)
        self.assertNotIn("file", first)
        self.assertEqual(second["doi"], "10.2/y")

    def test_missing_path_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("utils.audit.OUTPUT_DIR", Path(tmp)):
                record("delete", "404", Path(tmp) / "404.pdf")
            row = json.loads(
                (Path(tmp) / "logs" / "fulltext_audit.jsonl").read_text(encoding="utf-8").strip()
            )
        self.assertEqual(row["pmid"], "404")
        self.assertNotIn("md5", row)


if __name__ == "__main__":
    unittest.main()
