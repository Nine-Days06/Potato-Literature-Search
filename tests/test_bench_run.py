# tests/test_bench_run.py
"""
基准测试脚本的投票合并逻辑测试。

背景：实测 temperature=0 下，同一提示词连续两次运行的判定翻转率仍有
2.4–2.7%。如果直接拿单次运行结果比较版本，1–2 个百分点的差异完全可能是
随机噪声。因此 bench_run.py 改为每版本跑 N 遍再按多数票合并，本文件守住
这套合并逻辑不退化。
"""

import unittest

from bench_run import _merge_runs, _verdict_key


def _run(**kv):
    """构造一次运行的 results 字典：pmid -> 结果"""
    return {p: r for p, r in kv.items()}


def _res(verdict, field="", reason="r"):
    return {"verdict": verdict, "research_field": field, "reason": reason}


class TestVerdictKey(unittest.TestCase):
    def test_accepts_both_spellings(self):
        self.assertEqual(_verdict_key({"verdict": "RELEVANT"}), "REL")
        self.assertEqual(_verdict_key({"verdict": "NOT_RELEVANT"}), "NOT")

    def test_tolerates_case_and_separators(self):
        self.assertEqual(_verdict_key({"verdict": "not-relevant"}), "NOT")
        self.assertEqual(_verdict_key({"verdict": "Not_Relevant"}), "NOT")
        self.assertEqual(_verdict_key({"verdict": "relevant"}), "REL")

    def test_unknown_is_question_mark(self):
        self.assertEqual(_verdict_key({"verdict": "MAYBE"}), "?")
        self.assertEqual(_verdict_key({}), "?")
        self.assertEqual(_verdict_key(None), "?")


class TestMergeRuns(unittest.TestCase):
    def test_unanimous_wins(self):
        runs = [_run(p1=_res("RELEVANT")), _run(p1=_res("RELEVANT")),
                _run(p1=_res("RELEVANT"))]
        out = _merge_runs(runs, {"p1"})
        self.assertEqual(out["p1"]["verdict"], "RELEVANT")
        self.assertTrue(out["p1"]["_unanimous"])

    def test_majority_beats_single_outlier(self):
        """这是投票的全部意义：2:1 时少数派不能决定结果"""
        runs = [_run(p1=_res("NOT_RELEVANT")), _run(p1=_res("RELEVANT")),
                _run(p1=_res("RELEVANT"))]
        out = _merge_runs(runs, {"p1"})
        self.assertEqual(out["p1"]["verdict"], "RELEVANT")
        self.assertFalse(out["p1"]["_unanimous"])
        self.assertEqual(out["p1"]["_votes"], {"REL": 2, "NOT": 1})

    def test_missing_sample_in_some_runs_still_votes(self):
        """某次运行漏了这篇（限流失败）时，用其余次数投票，不该整篇丢弃"""
        runs = [_run(p1=_res("NOT_RELEVANT")), _run(), _run(p1=_res("NOT_RELEVANT"))]
        out = _merge_runs(runs, {"p1"})
        self.assertEqual(out["p1"]["verdict"], "NOT_RELEVANT")

    def test_sample_absent_from_every_run_is_dropped(self):
        out = _merge_runs([_run(), _run()], {"p1"})
        self.assertNotIn("p1", out)

    def test_hallucinated_pmid_is_dropped(self):
        """模型会返回不在基准集里的 pmid，必须按白名单丢弃"""
        runs = [_run(ghost=_res("RELEVANT"), p1=_res("RELEVANT"))]
        out = _merge_runs(runs, {"p1"})
        self.assertIn("p1", out)
        self.assertNotIn("ghost", out)

    def test_research_field_only_from_relevant_votes(self):
        """NOT_RELEVANT 的票不该贡献方向，否则会拿到空串或脏标签"""
        runs = [_run(p1=_res("NOT_RELEVANT", field="分子生物学")),
                _run(p1=_res("NOT_RELEVANT", field="")),
                _run(p1=_res("NOT_RELEVANT", field=""))]
        out = _merge_runs(runs, {"p1"})
        self.assertEqual(out["p1"]["verdict"], "NOT_RELEVANT")
        self.assertEqual(out["p1"]["research_field"], "")

    def test_research_field_takes_majority_among_relevant(self):
        runs = [_run(p1=_res("RELEVANT", field="植物保护")),
                _run(p1=_res("RELEVANT", field="植物保护")),
                _run(p1=_res("RELEVANT", field="栽培生理"))]
        out = _merge_runs(runs, {"p1"})
        self.assertEqual(out["p1"]["research_field"], "植物保护")

    def test_reason_comes_from_winning_side(self):
        runs = [_run(p1=_res("NOT_RELEVANT", reason="顺带提及")),
                _run(p1=_res("RELEVANT", reason="核心材料")),
                _run(p1=_res("RELEVANT", reason="核心材料"))]
        out = _merge_runs(runs, {"p1"})
        self.assertEqual(out["p1"]["reason"], "核心材料")

    def test_empty_input(self):
        self.assertEqual(_merge_runs([], {"p1"}), {})


if __name__ == "__main__":
    unittest.main()
