"""面试官情绪状态机（mood.py）的单元测试。

被测对象：`mood.py` 的 `initial` / `update` / `mood_of` / `describe` /
`effective_max_probes` / `summarize`。

测试原则（对应 DESIGN.md 第五节「情绪状态机」）：
    1. 全部离线、零依赖：mood.py 是纯函数（无随机、无全局、无 IO），
       所以这里不需要任何替身，直接调用即可——这正是它值得被测的理由；
    2. 断言"行为"而不是"实现"：重点验证状态迁移的边界（夹取上下限）、
       `mood_of` 的四种派生、情绪对追问上限的调制、以及严格模式下情绪不生效；
    3. 钉死"纯函数"这条契约：同输入必须同输出，且不修改传入的 state。

运行方式（项目根目录，零新增依赖）：
    py -3.12 -m unittest test_mood -v
"""

from __future__ import annotations

import unittest

import mood


def make_profile(**overrides) -> dict:
    """造一份最小可用的面试官配置（只放 mood 相关的键）。"""
    profile = {
        "max_probes": 3,
        "strict": False,
        "allow_reflect": True,
        "total_rounds": 5,
    }
    profile.update(overrides)
    return profile


class TestInitial(unittest.TestCase):
    """开局状态：夹取到合法区间，并带上 ask_streak 计数。"""

    def test_defaults(self):
        state = mood.initial()
        self.assertEqual(state["patience"], 2)
        self.assertEqual(state["energy"], 2)
        self.assertEqual(state["pressure"], 0)
        self.assertEqual(state["ask_streak"], 0)

    def test_values_are_clamped(self):
        state = mood.initial(patience=99, energy=-5, pressure=99)
        self.assertEqual(state["patience"], mood.PATIENCE_MAX)
        self.assertEqual(state["energy"], 0)
        self.assertEqual(state["pressure"], mood.PRESSURE_MAX)


class TestUpdateIsPure(unittest.TestCase):
    """纯函数契约：不修改入参、同输入同输出。"""

    def test_does_not_mutate_input(self):
        state = mood.initial()
        snapshot = dict(state)
        mood.update(state, answer="3000 QPS", cur_q=1)
        self.assertEqual(state, snapshot, "update 不得就地修改传入的 state")

    def test_same_input_same_output(self):
        kwargs = dict(
            answer="因为缓存命中率提升，P99 从 800ms 降到 120ms。",
            decision={"action": "ask", "gap": ""},
            elapsed_min=3.0,
            planned_rounds=5,
            cur_q=2,
        )
        first = mood.update(mood.initial(), **kwargs)
        second = mood.update(mood.initial(), **kwargs)
        self.assertEqual(first, second)


class TestUpdateSignals(unittest.TestCase):
    """逐条验证 update 的四组信号。"""

    def test_probe_on_quantification_costs_patience(self):
        state = mood.initial()
        after = mood.update(
            state, decision={"action": "probe", "gap": "缺量化结果"}, cur_q=1
        )
        self.assertEqual(after["patience"], state["patience"] - 1)

    def test_probe_on_other_gap_keeps_patience(self):
        state = mood.initial()
        after = mood.update(
            state, decision={"action": "probe", "gap": "缺反思"}, cur_q=1
        )
        self.assertEqual(after["patience"], state["patience"])

    def test_two_asks_in_a_row_restore_patience(self):
        # 耐心先压到 1，再连做两次 ask，应回到 2（ask_streak == 2 触发 +1）
        state = mood.initial(patience=1)
        state = mood.update(state, decision={"action": "ask"}, cur_q=1)
        self.assertEqual(state["patience"], 1, "第一次 ask 只累计 streak，不加耐心")
        state = mood.update(state, decision={"action": "ask"}, cur_q=2)
        self.assertEqual(state["patience"], 2)

    def test_probe_resets_ask_streak(self):
        state = mood.initial(patience=1)
        state = mood.update(state, decision={"action": "ask"}, cur_q=1)
        self.assertEqual(state["ask_streak"], 1)
        state = mood.update(state, decision={"action": "probe"}, cur_q=1)
        self.assertEqual(state["ask_streak"], 0, "追问会打断连续换题计数")

    def test_quantified_answer_raises_patience_and_energy(self):
        state = mood.initial(patience=1, energy=0)
        after = mood.update(state, answer="优化后 3000 QPS，耗时降到 120ms", cur_q=1)
        self.assertEqual(after["patience"], 2)
        self.assertEqual(after["energy"], 1)

    def test_vague_numbers_are_not_quantified(self):
        # 「大概 2019 年」「几百 QPS」按设计不算量化成果
        for vague in ("大概是 2019 年做的", "几百 QPS 吧", "差不多就行了"):
            with self.subTest(answer=vague):
                state = mood.initial(patience=1, energy=0)
                after = mood.update(state, answer=vague, cur_q=1)
                self.assertEqual(after["patience"], 1, f"{vague!r} 不该被当成量化")

    def test_unit_before_number_is_quantified(self):
        # 中文口语里「QPS 3000」也常见，必须收
        state = mood.initial(patience=1, energy=0)
        after = mood.update(state, answer="QPS 3000 左右", cur_q=1)
        self.assertEqual(after["patience"], 2)

    def test_long_causal_answer_raises_energy(self):
        text = "因为缓存命中率上不去，所以我把热点键单独拆了出来，导致整体延迟下降。" * 6
        self.assertGreater(len(text), 120, "前提：这段文本要足够长")
        state = mood.initial(energy=0)
        after = mood.update(state, answer=text, cur_q=1)
        self.assertEqual(after["energy"], 1)

    def test_short_causal_answer_does_not_raise_energy(self):
        state = mood.initial(energy=0)
        after = mood.update(state, answer="因为缓存命中率提升了。", cur_q=1)
        self.assertEqual(after["energy"], 0, "光有因果词、篇幅不够，不算讲逻辑")

    def test_time_pressure_rises_when_over_planned_duration(self):
        state = mood.initial()
        after = mood.update(state, elapsed_min=21.0, planned_rounds=5, cur_q=1)
        self.assertEqual(after["pressure"], 1, "超过 planned_rounds*4 分钟要升压")

    def test_time_pressure_rises_when_rounds_exhausted(self):
        state = mood.initial()
        after = mood.update(state, elapsed_min=1.0, planned_rounds=5, cur_q=5)
        self.assertEqual(after["pressure"], 1)

    def test_time_pressure_stays_within_plan(self):
        state = mood.initial()
        after = mood.update(state, elapsed_min=5.0, planned_rounds=5, cur_q=2)
        self.assertEqual(after["pressure"], 0)


class TestClamping(unittest.TestCase):
    """所有信号叠加后仍必须落在合法区间内。"""

    def test_patience_clamped_at_both_ends(self):
        low = mood.initial(patience=0)
        low = mood.update(
            low, decision={"action": "probe", "gap": "缺量化"}, cur_q=1
        )
        self.assertEqual(low["patience"], 0)

        high = mood.initial(patience=mood.PATIENCE_MAX)
        high = mood.update(high, answer="P99 120ms", cur_q=1)
        self.assertEqual(high["patience"], mood.PATIENCE_MAX)

    def test_energy_clamped_at_both_ends(self):
        high = mood.initial(energy=mood.ENERGY_MAX)
        high = mood.update(high, answer="P99 120ms", cur_q=1)
        self.assertEqual(high["energy"], mood.ENERGY_MAX)

    def test_pressure_clamped(self):
        state = mood.initial(pressure=mood.PRESSURE_MAX)
        state = mood.update(state, elapsed_min=99.0, planned_rounds=5, cur_q=5)
        self.assertEqual(state["pressure"], mood.PRESSURE_MAX)


class TestMoodOf(unittest.TestCase):
    """耐心 × 精力 → 四种对外表现。"""

    def test_four_quadrants(self):
        cases = [
            (3, 2, "投入"),
            (3, 0, "例行公事"),
            (0, 2, "急躁"),
            (0, 0, "冷淡"),
        ]
        for patience, energy, expected in cases:
            with self.subTest(patience=patience, energy=energy):
                state = mood.initial(patience=patience, energy=energy)
                self.assertEqual(mood.mood_of(state), expected)

    def test_boundaries_follow_documented_thresholds(self):
        # 耐心 >= 2 算有耐心；精力 >= 1 算有精力
        self.assertEqual(mood.mood_of(mood.initial(patience=2, energy=1)), "投入")
        self.assertEqual(mood.mood_of(mood.initial(patience=2, energy=0)), "例行公事")
        self.assertEqual(mood.mood_of(mood.initial(patience=1, energy=1)), "急躁")
        self.assertEqual(mood.mood_of(mood.initial(patience=1, energy=0)), "冷淡")

    def test_missing_keys_fall_back_to_max(self):
        # 空 dict 应回落到最宽松的状态，而不是抛异常
        self.assertEqual(mood.mood_of({}), "投入")


class TestDescribe(unittest.TestCase):
    """提示词段落：状态 + 态度 + 硬红线。"""

    def test_always_carries_the_red_line(self):
        for patience in (0, 3):
            with self.subTest(patience=patience):
                text = mood.describe(mood.initial(patience=patience))
                self.assertIn("禁止评价候选人的人格", text)

    def test_cold_mood_forbids_filler_acknowledgements(self):
        text = mood.describe(mood.initial(patience=0, energy=0))
        self.assertIn("当前状态：冷淡", text)
        self.assertIn("不要出现「嗯」「不错」", text)

    def test_impatient_mood_forbids_paraphrasing(self):
        text = mood.describe(mood.initial(patience=0, energy=2))
        self.assertIn("当前状态：急躁", text)
        self.assertIn("不要复述候选人的话", text)

    def test_high_pressure_adds_brevity_rule(self):
        text = mood.describe(mood.initial(pressure=1))
        self.assertIn("不超过 8 个字", text)

    def test_no_pressure_rule_when_calm(self):
        text = mood.describe(mood.initial(pressure=0))
        self.assertNotIn("不超过 8 个字", text)


class TestEffectiveMaxProbes(unittest.TestCase):
    """情绪调制追问上限；严格模式下必须锁死，不受情绪影响。"""

    def test_strict_mode_ignores_mood(self):
        profile = make_profile(strict=True, max_probes=2)
        for patience in range(0, mood.PATIENCE_MAX + 1):
            with self.subTest(patience=patience):
                state = mood.initial(patience=patience)
                self.assertEqual(
                    mood.effective_max_probes(profile, state),
                    2,
                    "严格模式追求每场节奏一致，追问上限不得随情绪浮动",
                )

    def test_human_mode_modulates_by_patience(self):
        profile = make_profile(strict=False, max_probes=3)
        impatient = mood.effective_max_probes(profile, mood.initial(patience=0))
        neutral = mood.effective_max_probes(profile, mood.initial(patience=1))
        patient = mood.effective_max_probes(profile, mood.initial(patience=3))
        self.assertLess(impatient, patient, "越不耐烦越早放弃这道题")
        self.assertEqual(neutral, 3, "耐心=1 时回到配置基线")
        self.assertGreater(patient, neutral)

    def test_result_is_clamped_to_one_through_four(self):
        low = make_profile(strict=False, max_probes=1)
        high = make_profile(strict=False, max_probes=10)
        for patience in range(0, mood.PATIENCE_MAX + 1):
            with self.subTest(patience=patience):
                state = mood.initial(patience=patience)
                self.assertGreaterEqual(mood.effective_max_probes(low, state), 1)
                self.assertLessEqual(mood.effective_max_probes(high, state), 4)

    def test_missing_max_probes_defaults(self):
        self.assertIsInstance(mood.effective_max_probes({}, mood.initial()), int)


class TestSummarize(unittest.TestCase):
    """轨迹摘要：给评估官和报告页用。"""

    def test_empty_trace(self):
        self.assertIn("没有记录到状态变化", mood.summarize([]))

    def test_reports_first_to_last(self):
        trace = [
            {"mood": "投入", "patience": 3, "energy": 2, "pressure": 0},
            {"mood": "冷淡", "patience": 0, "energy": 0, "pressure": 2},
        ]
        text = mood.summarize(trace, "压力型")
        self.assertIn("情绪模式：压力型", text)
        self.assertIn("「投入」走到「冷淡」", text)
        self.assertIn("耐心 3→0", text)
        self.assertIn("时间压力 0→2", text)

    def test_mode_label_is_optional(self):
        trace = [{"mood": "投入", "patience": 3, "energy": 2, "pressure": 0}]
        self.assertNotIn("情绪模式", mood.summarize(trace))


if __name__ == "__main__":
    unittest.main()
