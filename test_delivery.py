# -*- coding: utf-8 -*-
"""表达指标（delivery.py）与考点命中率的回归测试。

这两块是报告里唯一不依赖模型算法的量化段落，所以要钉住三件事：
1. 没有计时数据时**不能编**（分母为 0 就返回 None，不返回 0 或无穷大）；
2. 填充词只认语气词，不把「然后 / 就是」这类正常连接词算成口癖；
3. 命中率的分母是「本场真的判过的要点」，未问到的考点不进分母。
"""

import unittest

import delivery
import knowledge


class TestSpokenChars(unittest.TestCase):
    def test_strips_whitespace_and_punctuation(self):
        self.assertEqual(delivery.spoken_chars("嗯，我觉得\n这个问题 很好。"), 10)

    def test_empty_and_none_are_zero(self):
        self.assertEqual(delivery.spoken_chars(""), 0)
        self.assertEqual(delivery.spoken_chars(None), 0)


class TestFillers(unittest.TestCase):
    def test_counts_hesitations(self):
        self.assertEqual(delivery.count_fillers("嗯，那个，就是说这个方案"), 3)

    def test_legit_connectives_are_not_fillers(self):
        # 然后 / 就是 / 其实 / 这个(单用) 都是合法中文，不该算口癖
        self.assertEqual(delivery.count_fillers("先做A，然后做B，其实就是取舍"), 0)

    def test_empty_is_zero(self):
        self.assertEqual(delivery.count_fillers(""), 0)


class TestAnswerMetrics(unittest.TestCase):
    def test_computes_chars_seconds_and_speed(self):
        metrics = delivery.answer_metrics("缓存一致性协议要求各核看到同一份数据", 10.0)
        self.assertEqual(metrics["chars"], 18)
        self.assertEqual(metrics["seconds"], 10.0)
        self.assertEqual(metrics["chars_per_second"], 1.8)

    def test_missing_timer_gives_none_not_zero(self):
        for bad in (None, "", 0, -3, "abc"):
            metrics = delivery.answer_metrics("有一段足够长的回答内容放在这里", bad)
            self.assertIsNone(metrics["seconds"])
            self.assertIsNone(metrics["chars_per_second"])


class TestSessionMetrics(unittest.TestCase):
    def _transcript(self):
        return [
            {"role": "assistant", "content": "请讲一下 MVCC"},
            {"role": "user", "content": "嗯，MVCC 靠版本链和 Read View 实现，嗯，避免加锁。", "elapsed": 10.0},
            {"role": "user", "content": "太短", "elapsed": 3.0},
            {"role": "user", "content": "这条长度够了但没有计时字段所以不该进统计", "elapsed": 0},
            {"role": "user", "content": "另一条正常回答：索引失效常见于函数、隐式转换与前缀模糊匹配。", "elapsed": 20.0},
        ]

    def test_only_timed_long_enough_answers_count(self):
        metrics = delivery.session_metrics(self._transcript())
        self.assertEqual(metrics["count"], 2)
        self.assertEqual(metrics["skipped"], 2)
        self.assertEqual([row["index"] for row in metrics["rows"]], [1, 2])

    def test_speed_is_total_chars_over_total_seconds(self):
        metrics = delivery.session_metrics(self._transcript())
        self.assertEqual(metrics["seconds"], 30.0)
        self.assertEqual(metrics["chars_per_second"], round(metrics["chars"] / 30.0, 2))

    def test_longest_and_shortest(self):
        metrics = delivery.session_metrics(self._transcript())
        self.assertGreater(metrics["longest"], metrics["shortest"])

    def test_empty_transcript_is_safe(self):
        for value in (None, [], [{"role": "assistant", "content": "x"}]):
            metrics = delivery.session_metrics(value)
            self.assertEqual(metrics["count"], 0)
            self.assertIsNone(metrics["chars_per_second"])
            self.assertEqual(delivery.summary_line(metrics), "")


class TestPace(unittest.TestCase):
    def test_targets_are_ordered(self):
        self.assertGreater(delivery.pace_target("A")[0], delivery.pace_target("C")[0])
        self.assertEqual(delivery.pace_target("未知"), delivery.PACE_TARGET["B"])

    def test_verdict(self):
        self.assertEqual(delivery.pace_verdict(1.5, "B"), "偏慢")
        self.assertEqual(delivery.pace_verdict(3.0, "B"), "合适")
        self.assertEqual(delivery.pace_verdict(9.0, "B"), "偏快")
        self.assertEqual(delivery.pace_verdict(None, "B"), "")

    def test_summary_line_mentions_fillers(self):
        metrics = delivery.session_metrics(
            [{"role": "user", "content": "嗯，这是一个足够长的回答，用来验证摘要里会提到填充词。", "elapsed": 8.0}]
        )
        line = delivery.summary_line(metrics, "B")
        self.assertIn("语速", line)
        self.assertIn("填充词", line)


class TestHitRate(unittest.TestCase):
    def _agg(self, rows):
        return knowledge.aggregate_rubric_check({"major": "计算机科学与技术"}, rows)

    def test_per_topic_rate(self):
        agg = self._agg(
            [{"topic": "A", "must_hit": ["1", "2", "3"], "missed": ["4", "5"], "errors": []}]
        )
        self.assertEqual(agg["topics"][0]["rate"], 0.6)
        self.assertEqual(agg["hit_rate"], 0.6)

    def test_unasked_topic_is_not_in_the_denominator(self):
        agg = self._agg(
            [
                {"topic": "A", "must_hit": ["1"], "missed": [], "errors": []},
                {"topic": "B", "must_hit": [], "missed": ["本场未问到"], "errors": []},
            ]
        )
        self.assertIsNone(agg["topics"][1]["rate"])
        self.assertEqual(agg["hit_rate"], 1.0)
        self.assertEqual(agg["planned_total"], 1)

    def test_all_zero_hit_gives_zero_rate(self):
        agg = self._agg([{"topic": "A", "must_hit": [], "missed": ["1", "2"], "errors": []}])
        self.assertEqual(agg["topics"][0]["rate"], 0.0)
        self.assertEqual(agg["hit_rate"], 0.0)

    def test_no_rows_gives_none_not_zero(self):
        agg = self._agg([])
        self.assertIsNone(agg["hit_rate"])
        self.assertEqual(agg["planned_total"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
