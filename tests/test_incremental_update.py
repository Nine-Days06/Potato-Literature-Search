import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# scripts/ 不是包，需要把项目根加入 sys.path 才能导入
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.incremental_update as iu


class TestStageSelection(unittest.TestCase):
    """
    步骤选择必须与文档一致：

    - 无参数            → 1-3（下载/解析/硬过滤）
    - --export-since    → 仅导出（不跑 1-3）
    - --export          → 仅导出（不跑 1-3）
    - --validate        → 1-3 + 验证
    - --validate --export → 1-3 + 验证 + 导出

    回归背景：模式日志按 not (validate or export or export_since) 计算"默认步骤"，
    但实际执行只看 --skip-*，导致 --export-since 明明是"仅导出"却先跑了 1-3。
    """

    def _run(self, argv):
        """执行 main()，返回各阶段是否被调用的记录"""
        calls = {}

        def rec(name, ret=None):
            def inner(*a, **kw):
                calls[name] = True
                return ret
            return inner

        with patch.object(sys, "argv", ["incremental_update.py"] + argv), \
             patch.object(iu, "init_db"), \
             patch.object(iu, "run_incremental_download",
                          side_effect=rec("download", [])), \
             patch.object(iu, "run_parse", side_effect=rec("parse")), \
             patch.object(iu, "run_incremental_hard_filter", side_effect=rec("filter")), \
             patch.object(iu, "run_validation", side_effect=rec("validate")), \
             patch.object(iu, "export_incremental_raw_csv",
                          side_effect=rec("export", Path("out.csv"))), \
             patch.object(iu, "export_incremental_filtered_csv",
                          side_effect=rec("export_filtered", Path("out_filtered.csv"))), \
             patch.object(iu, "export_by_research_field",
                          side_effect=rec("export_by_field", [Path("out_植物保护.csv")])), \
             patch.object(iu, "get_removed_pmids", side_effect=rec("removed", set())), \
             patch.object(iu, "mark_removed_pmids"):
            iu.main()
        return calls

    def test_no_args_runs_default_steps_only(self):
        calls = self._run([])
        self.assertTrue(calls.get("download"))
        self.assertTrue(calls.get("parse"))
        self.assertTrue(calls.get("filter"))
        self.assertFalse(calls.get("validate"))
        self.assertFalse(calls.get("export"))

    def test_export_since_should_skip_all_three_default_steps(self):
        """仅导出：不应触发下载/解析/硬过滤"""
        calls = self._run(["--export-since", "2025-08-01T00:00:00"])
        self.assertNotIn("download", calls)
        self.assertNotIn("parse", calls)
        self.assertNotIn("filter", calls)
        self.assertTrue(calls.get("export"))

    def test_export_alone_should_skip_all_three_default_steps(self):
        """仅导出（本次运行新增）：同样不应触发 1-3"""
        calls = self._run(["--export"])
        self.assertNotIn("download", calls)
        self.assertNotIn("parse", calls)
        self.assertNotIn("filter", calls)
        self.assertTrue(calls.get("export"))

    def test_validate_should_run_default_steps_then_validate(self):
        """--validate 文档定义为"核心三步 + 验证"，故 1-3 仍需执行"""
        calls = self._run(["--validate"])
        self.assertTrue(calls.get("download"))
        self.assertTrue(calls.get("parse"))
        self.assertTrue(calls.get("filter"))
        self.assertTrue(calls.get("validate"))

    def test_validate_with_export_should_run_everything(self):
        """--validate --export 是文档里的"完整增量"，1-3 必须执行"""
        calls = self._run(["--validate", "--export"])
        self.assertTrue(calls.get("download"))
        self.assertTrue(calls.get("parse"))
        self.assertTrue(calls.get("filter"))
        self.assertTrue(calls.get("validate"))
        self.assertTrue(calls.get("export"))

    def test_explicit_skip_flags_still_honored(self):
        """显式 --skip-* 依旧生效"""
        calls = self._run(["--skip-download", "--skip-parse", "--validate"])
        self.assertNotIn("download", calls)
        self.assertNotIn("parse", calls)
        self.assertTrue(calls.get("filter"))
        self.assertTrue(calls.get("validate"))

    def test_export_since_passes_timestamp_through(self):
        """时间戳必须原样传给三个导出函数（raw / filtered / 按方向）"""
        seen = {}

        def fake_export(since=None, db_path=None):
            seen["since"] = since
            return Path("out.csv")

        def fake_export_filtered(since=None, db_path=None):
            seen["since_filtered"] = since
            return Path("out_filtered.csv")

        def fake_export_by_field(db_path=None, since_timestamp=None):
            seen["since_by_field"] = since_timestamp
            return [Path("out_植物保护.csv")]

        with patch.object(sys, "argv", ["incremental_update.py",
                                        "--export-since", "2025-08-01T00:00:00"]), \
             patch.object(iu, "init_db"), \
             patch.object(iu, "run_incremental_download", return_value=[]), \
             patch.object(iu, "run_parse"), \
             patch.object(iu, "run_incremental_hard_filter"), \
             patch.object(iu, "export_incremental_raw_csv", side_effect=fake_export), \
             patch.object(iu, "export_incremental_filtered_csv",
                          side_effect=fake_export_filtered), \
             patch.object(iu, "export_by_research_field",
                          side_effect=fake_export_by_field):
            iu.main()

        self.assertEqual(seen.get("since"), "2025-08-01T00:00:00")
        self.assertEqual(seen.get("since_filtered"), "2025-08-01T00:00:00")
        self.assertEqual(seen.get("since_by_field"), "2025-08-01T00:00:00")

    def test_export_emits_raw_filtered_and_by_field(self):
        """--export 必须同时产出 raw、带研究方向的 filtered、按方向拆分三份"""
        calls = self._run(["--export"])
        self.assertTrue(calls.get("export"))
        self.assertTrue(calls.get("export_filtered"))
        self.assertTrue(calls.get("export_by_field"))


if __name__ == "__main__":
    unittest.main()