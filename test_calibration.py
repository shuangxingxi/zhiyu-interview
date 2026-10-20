"""结局回填与评分校准（calibration.py）的单元测试。

被测对象：`normalize_outcome` / `outcome_of` / `outcome_note` / `outcome_label`
与 `calibrate` 的对照结论。

为什么要单独测：校准输出的每一条结论都会直接被用户当成"我的分数准不准"来读。
结论算错比不算更糟——它会让人按错误方向调准备策略。所以这里的重点不是覆盖率，
而是把几条**容易算错**的边界钉死：

    1. 准确率的分母是"评分给了明确判断的场次"（`decided`），不是"有结局的场次"
       —— 落在两条线之间的灰区不该被算成对或错；
    2. `bool` 不是分数（`True` 在 Python 里也是 int，会被当成 1 分）；
    3. 判定用的是"两条线夹灰区"，不是单条线——60 分被拒既不算对也不算偏乐观；
    4. 样本不足时（`decided < 3`）必须直说样本不够，不能照常给结论。

运行方式（项目根目录，零新增依赖）：
    py -3.12 -m unittest test_calibration -v
"""

from __future__ import annotations

import unittest

import calibration


def make_record(overall=70, outcome=None, level="B", major="软件工程"):
    """造一场最小可用的记录：分数 + 结局 + 难度档。

    outcome 既支持字符串（早期 / 手改过的落盘形态），也支持 dict（当前写法），
    由调用方决定，方便覆盖 `outcome_of` 的两种分支。
    """
    record = {
        "overall_score": overall,
        "profile": {"level": level, "major": major},
    }
    if outcome is not None:
        record["outcome"] = outcome
    return record


class TestNormalizeOutcome(unittest.TestCase):
    """结局归一化：不认识的取值一律当"未回填"，绝不猜。"""

    def test_known_values_pass_through(self):
        for value in calibration.OUTCOMES:
            with self.subTest(value=value):
                self.assertEqual(calibration.normalize_outcome(value), value)

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(calibration.normalize_outcome("  被拒  "), "被拒")

    def test_unknown_becomes_unset(self):
        for bad in ("通过", "rejected", "被拒了", None, "", "   ", 123, True):
            with self.subTest(bad=bad):
                self.assertEqual(
                    calibration.normalize_outcome(bad),
                    calibration.OUTCOME_UNSET,
                    "乱码 / 手改过的历史文件不能把界面搞崩",
                )

    def test_outcomes_list_starts_with_unset(self):
        # 下拉框第一项必须是"未回填"，否则用户没法把已回填的场次改回去
        self.assertEqual(calibration.OUTCOMES[0], calibration.OUTCOME_UNSET)


class TestOutcomeOf(unittest.TestCase):
    """读取：兼容 dict 与纯字符串两种落盘形态。"""

    def test_dict_form(self):
        record = make_record(outcome={"value": "拿到 offer", "note": "面得很顺"})
        self.assertEqual(calibration.outcome_of(record), "拿到 offer")
        self.assertEqual(calibration.outcome_note(record), "面得很顺")

    def test_plain_string_form(self):
        # 早期 / 手改过的文件直接把结局存成字符串
        record = make_record(outcome="被拒")
        self.assertEqual(calibration.outcome_of(record), "被拒")
        self.assertEqual(calibration.outcome_note(record), "", "字符串形态没有备注可读")

    def test_missing_outcome_is_unset(self):
        self.assertEqual(calibration.outcome_of(make_record()), calibration.OUTCOME_UNSET)
        self.assertEqual(calibration.outcome_note(make_record()), "")

    def test_corrupt_shapes_are_unset(self):
        for bad in (None, "不是字典", 3, [], {"outcome": {"value": True}}):
            with self.subTest(bad=bad):
                self.assertEqual(calibration.outcome_of(bad), calibration.OUTCOME_UNSET)

    def test_dict_with_non_dict_outcome_value(self):
        # outcome 字段被写成了别的类型（比如数字），也应安全降级
        self.assertEqual(
            calibration.outcome_of({"outcome": 5}), calibration.OUTCOME_UNSET
        )

    def test_label_makes_unset_explicit(self):
        self.assertEqual(calibration.outcome_label(make_record()), "未回填")
        self.assertEqual(
            calibration.outcome_label(make_record(outcome="被拒")), "被拒"
        )

    def test_note_strips_whitespace(self):
        record = make_record(outcome={"value": "被拒", "note": "  结论下得太急  "})
        self.assertEqual(calibration.outcome_note(record), "结论下得太急")


class TestCalibrateCounts(unittest.TestCase):
    """计数与分类：两条线夹灰区的判定。"""

    def test_empty_and_none_input(self):
        for payload in (None, [], "不是列表"):
            with self.subTest(payload=payload):
                result = calibration.calibrate(payload)
                self.assertEqual(result["scored"], 0)
                self.assertEqual(result["labeled"], 0)
                self.assertEqual(result["samples"], 0)
                self.assertIsNone(result["accuracy"], "没有样本时准确率是 None，不是 0")
                self.assertIn("还没有回填过结局", result["notes"][0])

    def test_non_dict_items_are_filtered(self):
        weird = [None, "脏数据", 3, make_record(overall=80, outcome="拿到 offer")]
        self.assertEqual(calibration.calibrate(weird)["hits"], 1)

    def test_bool_score_is_not_a_score(self):
        self.assertEqual(calibration.calibrate([make_record(overall=True)])["scored"], 0)

    def test_high_score_with_pass_is_a_hit(self):
        result = calibration.calibrate([make_record(overall=80, outcome="拿到 offer")])
        self.assertEqual((result["hits"], result["misses"], result["unclear"]), (1, 0, 0))
        self.assertEqual(result["accuracy"], 1.0)

    def test_low_score_with_reject_is_a_hit(self):
        result = calibration.calibrate([make_record(overall=40, outcome="被拒")])
        self.assertEqual(result["hits"], 1)

    def test_low_score_with_pass_is_conservative(self):
        result = calibration.calibrate([make_record(overall=50, outcome="进入下一轮")])
        self.assertEqual((result["misses"], result["conservative"], result["optimistic"]), (1, 1, 0))

    def test_high_score_with_reject_is_optimistic(self):
        result = calibration.calibrate([make_record(overall=90, outcome="被拒")])
        self.assertEqual((result["misses"], result["optimistic"], result["conservative"]), (1, 1, 0))

    def test_grey_zone_is_unclear(self):
        # 60 分被拒：评分本身没给明确判断，既不算对也不算偏乐观
        result = calibration.calibrate([make_record(overall=60, outcome="被拒")])
        self.assertEqual(result["unclear"], 1)
        self.assertEqual(result["decided"], 0)
        self.assertIsNone(result["accuracy"])

    def test_boundary_scores_count_as_decided(self):
        # 75 恰好到高分线、55 恰好到低分线，边界要算进明确判断里
        records = [
            make_record(overall=75, outcome="拿到 offer"),  # >= HIGH 且通过 → 对
            make_record(overall=55, outcome="被拒"),        # <= LOW 且被拒 → 对
        ]
        result = calibration.calibrate(records)
        self.assertEqual(result["hits"], 2)
        self.assertEqual(result["decided"], 2)

    def test_accuracy_uses_decided_as_denominator(self):
        # 2 对 + 1 错 + 2 灰区：准确率分母应是 3 而不是 5
        records = [
            make_record(overall=80, outcome="拿到 offer"),
            make_record(overall=40, outcome="被拒"),
            make_record(overall=90, outcome="被拒"),
            make_record(overall=65, outcome="拿到 offer"),
            make_record(overall=60, outcome="被拒"),
        ]
        result = calibration.calibrate(records)
        self.assertEqual((result["hits"], result["misses"], result["unclear"]), (2, 1, 2))
        self.assertEqual(result["samples"], 5)
        self.assertEqual(result["decided"], 3)
        self.assertEqual(result["accuracy"], 0.667)

    def test_unlabeled_outcomes_are_counted_but_not_calibrated(self):
        records = [
            make_record(overall=80, outcome="还没结果"),
            make_record(overall=80, outcome="不适用"),
            make_record(overall=80),  # 未回填
        ]
        result = calibration.calibrate(records)
        self.assertEqual(result["scored"], 3)
        self.assertEqual(result["labeled"], 2, "未回填不算已回填")
        self.assertEqual(result["samples"], 0)
        self.assertEqual(
            result["outcome_counts"], {"还没结果": 1, "不适用": 1}
        )

    def test_by_level_tracks_scores_and_outcome_sides(self):
        records = [
            make_record(overall=40, outcome="被拒", level="B"),
            make_record(overall=45, outcome="被拒", level="B"),
            make_record(overall=90, outcome="拿到 offer", level="A"),
        ]
        result = calibration.calibrate(records)
        self.assertEqual(result["by_level"]["B"]["samples"], 2)
        self.assertEqual(result["by_level"]["B"]["negative"], 2)
        self.assertEqual(result["by_level"]["B"]["positive"], 0)
        self.assertEqual(result["by_level"]["B"]["avg_score"], 42.5)
        self.assertEqual(result["by_level"]["A"]["positive"], 1)

    def test_missing_level_falls_back(self):
        record = make_record(overall=80, outcome="拿到 offer")
        record["profile"] = {}
        result = calibration.calibrate([record])
        self.assertIn("未标难度", result["by_level"])


class TestCalibrateNotes(unittest.TestCase):
    """结论措辞：样本不够时必须直说，够了才给偏移判断。"""

    def test_no_label_at_all_says_never_backfilled(self):
        result = calibration.calibrate([make_record(overall=80), make_record(overall=70)])
        self.assertEqual(result["labeled"], 0)
        self.assertIn("还没有回填过结局", result["notes"][0])

    def test_labeled_but_not_decisive_is_not_reported_as_unfilled(self):
        """回填了「还没结果 / 不适用」时，不能反过来说"还没回填过"。"""
        records = [
            make_record(overall=80, outcome="还没结果"),
            make_record(overall=70, outcome="不适用"),
        ]
        result = calibration.calibrate(records)
        self.assertEqual(result["labeled"], 2)
        self.assertEqual(result["samples"], 0)
        self.assertNotIn("还没有回填过", result["notes"][0])
        self.assertIn("还不是明确结果", result["notes"][0])

    def test_too_few_decided_says_so(self):
        records = [
            make_record(overall=80, outcome="拿到 offer"),
            make_record(overall=90, outcome="被拒"),
            make_record(overall=65, outcome="拿到 offer"),  # 灰区
        ]
        result = calibration.calibrate(records)
        self.assertEqual(result["decided"], 2, "decided 不足 3 场")
        self.assertEqual(result["samples"], 3)
        self.assertIn("样本还太少", result["notes"][0])
        self.assertNotIn("%", result["notes"][0], "样本不够不该给准确率")

    def test_perfect_agreement_note(self):
        records = [
            make_record(overall=80, outcome="拿到 offer"),
            make_record(overall=85, outcome="进入下一轮"),
            make_record(overall=40, outcome="被拒"),
        ]
        result = calibration.calibrate(records)
        self.assertEqual(result["accuracy"], 1.0)
        self.assertTrue(
            any("完全一致" in note for note in result["notes"]),
            "全对时要明说没有偏移，而不是只报个百分比",
        )

    def test_optimistic_direction_note(self):
        records = [
            make_record(overall=90, outcome="被拒"),
            make_record(overall=80, outcome="被拒"),
            make_record(overall=40, outcome="拿到 offer"),  # 偏保守一场
        ]
        result = calibration.calibrate(records)
        self.assertEqual((result["optimistic"], result["conservative"]), (2, 1))
        self.assertTrue(any("偏乐观" in note for note in result["notes"]))

    def test_conservative_direction_note(self):
        records = [
            make_record(overall=40, outcome="拿到 offer"),
            make_record(overall=50, outcome="进入下一轮"),
            make_record(overall=90, outcome="被拒"),  # 偏乐观一场
        ]
        result = calibration.calibrate(records)
        self.assertEqual((result["optimistic"], result["conservative"]), (1, 2))
        self.assertTrue(any("偏保守" in note for note in result["notes"]))

    def test_balanced_offset_note(self):
        records = [
            make_record(overall=90, outcome="被拒"),        # 偏乐观
            make_record(overall=40, outcome="拿到 offer"),   # 偏保守
            make_record(overall=80, outcome="拿到 offer"),   # 对
        ]
        result = calibration.calibrate(records)
        self.assertEqual((result["optimistic"], result["conservative"]), (1, 1))
        self.assertTrue(any("没有单向偏移" in note for note in result["notes"]))

    def test_unclear_is_explained(self):
        records = [
            make_record(overall=80, outcome="拿到 offer"),
            make_record(overall=85, outcome="进入下一轮"),
            make_record(overall=40, outcome="被拒"),
            make_record(overall=60, outcome="被拒"),  # 灰区
        ]
        result = calibration.calibrate(records)
        self.assertEqual(result["unclear"], 1)
        self.assertTrue(any("不计入准确率" in note for note in result["notes"]))

    def test_level_notes_only_with_enough_samples(self):
        # 单场"全拒"不下结论——一场就总结是过度解读
        records = [make_record(overall=40, outcome="被拒", level="B")]
        result = calibration.calibrate(records)
        self.assertFalse(any("全部被拒" in note for note in result["notes"]))

    def test_all_rejected_level_note(self):
        records = [
            make_record(overall=40, outcome="被拒", level="B"),
            make_record(overall=50, outcome="被拒", level="B"),
            make_record(overall=80, outcome="拿到 offer", level="A"),
            make_record(overall=85, outcome="拿到 offer", level="A"),
        ]
        result = calibration.calibrate(records)
        self.assertTrue(any("B 档" in note and "全部被拒" in note for note in result["notes"]))

    def test_all_passed_level_note(self):
        records = [
            make_record(overall=80, outcome="拿到 offer", level="A"),
            make_record(overall=85, outcome="进入下一轮", level="A"),
            make_record(overall=40, outcome="被拒", level="B"),
            make_record(overall=45, outcome="被拒", level="B"),
        ]
        result = calibration.calibrate(records)
        self.assertTrue(any("A 档" in note and "全部通过" in note for note in result["notes"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
