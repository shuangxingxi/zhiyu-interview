"""面试计数器状态机（interview_state.py）的单元测试。

被测对象：`advance_counters` / `next_asked_topics` / `decision_row` /
`mood_trace_row`。

为什么值得测：这是 `app.advance()` 里风险最高的一段——三个计数器联动，
一旦错了，表现出来是**绕圈、超轮次、或该追问时不追问**，而且从界面上很难
一眼看出是哪一轮错的。逻辑本身与 Streamlit 无关，所以能离线直测。

测试原则：
    1. 全部离线、零依赖：纯函数，不需要任何替身；
    2. 断言"行为"而不是"实现"：重点覆盖四个动作各自对三个计数器的影响、
       以及 probe/reflect 会清零"连续非提问计数"这类容易写错的联动；
    3. 覆盖非法输入：模型偶发填出第五种动作时，不能推进任何计数。

运行方式（项目根目录，零新增依赖）：
    py -3.12 -m unittest test_interview_state -v
"""

from __future__ import annotations

import unittest

import interview_state as istate


class TestAdvanceCounters(unittest.TestCase):
    """四个动作各自如何推进三个计数器。"""

    def test_ask_advances_question_and_resets_the_rest(self):
        cur_q, probes, reflects = istate.advance_counters(3, 2, 1, "ask")
        self.assertEqual(cur_q, 4, "换题：主问题 +1")
        self.assertEqual(probes, 0, "换题：追问计数清零")
        self.assertEqual(reflects, 0, "换题：连续非提问计数清零")

    def test_probe_keeps_question_and_counts_up(self):
        cur_q, probes, reflects = istate.advance_counters(3, 1, 2, "probe")
        self.assertEqual(cur_q, 3, "追问不推进主问题")
        self.assertEqual(probes, 2, "追问：计数 +1")
        self.assertEqual(reflects, 0, "一旦重新提问，就不再是「连着没提问」")

    def test_reflect_only_counts_the_streak(self):
        cur_q, probes, reflects = istate.advance_counters(3, 1, 1, "reflect")
        self.assertEqual(cur_q, 3, "非提问回合不推进主问题")
        self.assertEqual(probes, 1, "非提问回合不算一次追问")
        self.assertEqual(reflects, 2, "连续非提问计数 +1")

    def test_end_changes_nothing(self):
        self.assertEqual(
            istate.advance_counters(5, 2, 1, "end"), (5, 2, 1), "收尾不推进任何计数"
        )

    def test_unknown_action_changes_nothing(self):
        # 模型偶发填出第五种取值：既不推进题目，也不计追问
        for bogus in ("", "ASK", "追问", None, "askk"):
            with self.subTest(action=bogus):
                self.assertEqual(istate.advance_counters(2, 1, 1, bogus), (2, 1, 1))

    def test_sequence_does_not_mutate_inputs(self):
        # 纯函数：同输入必得同输出
        first = istate.advance_counters(1, 0, 0, "probe")
        second = istate.advance_counters(1, 0, 0, "probe")
        self.assertEqual(first, second)

    def test_realistic_full_round_sequence(self):
        """走一遍真实的节奏：ask → probe → probe → reflect → ask。"""
        cur_q = probes = reflects = 0
        cur_q, probes, reflects = istate.advance_counters(cur_q, probes, reflects, "ask")
        self.assertEqual((cur_q, probes, reflects), (1, 0, 0))

        cur_q, probes, reflects = istate.advance_counters(cur_q, probes, reflects, "probe")
        self.assertEqual((cur_q, probes, reflects), (1, 1, 0))

        cur_q, probes, reflects = istate.advance_counters(cur_q, probes, reflects, "probe")
        self.assertEqual((cur_q, probes, reflects), (1, 2, 0))

        cur_q, probes, reflects = istate.advance_counters(cur_q, probes, reflects, "reflect")
        self.assertEqual((cur_q, probes, reflects), (1, 2, 1))

        cur_q, probes, reflects = istate.advance_counters(cur_q, probes, reflects, "ask")
        self.assertEqual(
            (cur_q, probes, reflects),
            (2, 0, 0),
            "换题后追问与连续非提问都必须归零，否则下一题会一上来就追问超限",
        )

    def test_reflect_streak_survives_consecutive_reflects(self):
        # 连续 reflect 的上限判定依赖这个计数真的能累加
        cur_q = probes = reflects = 0
        for _ in range(3):
            cur_q, probes, reflects = istate.advance_counters(
                cur_q, probes, reflects, "reflect"
            )
        self.assertEqual(reflects, 3)


class TestNextAskedTopics(unittest.TestCase):
    """考点去重：本场已考的不能再落一次。"""

    def test_appends_new_topic(self):
        topics, text = istate.next_asked_topics(["A"], "B")
        self.assertEqual(topics, ["A", "B"])
        self.assertEqual(text, "B")

    def test_does_not_duplicate(self):
        topics, text = istate.next_asked_topics(["A", "B"], "A")
        self.assertEqual(topics, ["A", "B"], "重复考点不该再追加一次")
        self.assertEqual(text, "A")

    def test_does_not_mutate_input(self):
        original = ["A"]
        istate.next_asked_topics(original, "B")
        self.assertEqual(original, ["A"], "必须返回新列表，不改入参")

    def test_blank_topic_is_ignored(self):
        for blank in (None, "", "   ", "\n"):
            with self.subTest(topic=blank):
                topics, text = istate.next_asked_topics(["A"], blank)
                self.assertEqual(topics, ["A"])
                self.assertEqual(text, "")

    def test_topic_is_stripped(self):
        topics, text = istate.next_asked_topics([], "  工程实践  ")
        self.assertEqual(topics, ["工程实践"], "考点要先去空白，否则去重会漏")
        self.assertEqual(text, "工程实践")

    def test_stripped_topic_matches_existing(self):
        # 「工程实践」与「 工程实践 」必须视为同一个考点
        topics, _ = istate.next_asked_topics(["工程实践"], " 工程实践 ")
        self.assertEqual(topics, ["工程实践"])

    def test_non_string_topic_is_coerced(self):
        topics, text = istate.next_asked_topics([], 123)
        self.assertEqual(topics, ["123"])
        self.assertEqual(text, "123")


class TestDecisionRow(unittest.TestCase):
    """报告「面试官决策轨迹」的一行。"""

    LABELS = {"ask": "换新题", "probe": "追问", "reflect": "反馈", "end": "收尾"}

    def test_maps_action_to_chinese_label(self):
        row = istate.decision_row(3, {"action": "probe"}, self.LABELS)
        self.assertEqual(row["动作"], "追问")
        self.assertEqual(row["主问题"], 3)

    def test_unknown_action_falls_back_to_raw(self):
        row = istate.decision_row(1, {"action": "weird"}, self.LABELS)
        self.assertEqual(row["动作"], "weird")

    def test_tool_trace_defaults_to_dash(self):
        row = istate.decision_row(1, {"action": "ask"}, self.LABELS)
        self.assertEqual(row["工具调用"], "—", "没调工具时显示占位符而不是空白")

    def test_tool_trace_is_shown_when_present(self):
        row = istate.decision_row(
            1, {"action": "ask", "tool_trace": "查阅简历「推荐系统」→ R2·实习经历"}, self.LABELS
        )
        self.assertIn("查阅简历", row["工具调用"])

    def test_all_columns_present(self):
        row = istate.decision_row(2, {"action": "ask", "topic": "工程实践"}, self.LABELS)
        self.assertEqual(
            list(row.keys()),
            ["主问题", "动作", "考点", "考察点", "工具调用", "面试官判断"],
        )


class TestMoodTraceRow(unittest.TestCase):
    """情绪状态轨迹：键名必须与 mood.summarize / agents.format_mood_trace 一致。"""

    def test_uses_english_keys(self):
        row = istate.mood_trace_row(
            2, {"patience": 3, "energy": 1, "pressure": 0}, "投入"
        )
        self.assertEqual(
            set(row.keys()), {"round", "mood", "patience", "energy", "pressure"}
        )

    def test_carries_state_values(self):
        row = istate.mood_trace_row(
            4, {"patience": 1, "energy": 0, "pressure": 2}, "急躁"
        )
        self.assertEqual(row["round"], 4)
        self.assertEqual(row["mood"], "急躁")
        self.assertEqual(row["patience"], 1)
        self.assertEqual(row["pressure"], 2)

    def test_missing_state_keys_default_to_zero(self):
        row = istate.mood_trace_row(1, {}, "投入")
        self.assertEqual(row["patience"], 0)
        self.assertEqual(row["energy"], 0)
        self.assertEqual(row["pressure"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
