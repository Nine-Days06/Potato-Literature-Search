import logging
import os
import tempfile
import time
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path

from utils.logger import LOG_BACKUPS, LOG_MAX_BYTES, LOG_RETENTION_DAYS, get_logger


class LoggerTest(unittest.TestCase):
    def tearDown(self):
        logger = logging.getLogger("unittest_logger")
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)

    def test_file_handler_rotates_and_old_logs_purged(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            stale = log_dir / "old_20200101.log"
            stale.write_text("old", encoding="utf-8")
            old_time = time.time() - (LOG_RETENTION_DAYS + 1) * 86400
            os.utime(stale, (old_time, old_time))
            fresh = log_dir / "keep.log"
            fresh.write_text("keep", encoding="utf-8")

            logger = get_logger("unittest_logger", log_dir=log_dir)
            logger.debug("hello")

            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
            handler = next(h for h in logger.handlers if isinstance(h, RotatingFileHandler))
            self.assertEqual(handler.maxBytes, LOG_MAX_BYTES)
            self.assertEqual(handler.backupCount, LOG_BACKUPS)

            handler.flush()
            written = next(p for p in log_dir.glob("unittest_logger_*.log"))
            self.assertIn("hello", written.read_text(encoding="utf-8"))

            for h in list(logger.handlers):
                h.close()
                logger.removeHandler(h)


if __name__ == "__main__":
    unittest.main()
