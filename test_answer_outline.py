# -*- coding: utf-8 -*-
"""答题框架（answer_outline.json）的数据完整性与取用回归测试。

这份数据是**给考生看的**：它把 must_hit（评委的判分要点）转成答题动作，
填上「考生只看到考点名和最终判定、中间没有支架」这段空白。

它必须守三条：
1. 数据完整——30 条试点覆盖 6 专业各 5 条，每条 3~4 步且都有「别这么说」；
2. 不泄题——框架句与同考点的 must_hit 互不为子串，否则等于把答案提前念出来；
3. 取用安全——按（专业, 考点原文）精确匹配，未知考点返回 None，文件坏了也不抛异常。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import knowledge


class TestAnswerOutlineData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = knowledge.load_answer_outlines()
        cls.rubrics = knowledge.load_rubrics()

    def test_pilot_covers_thirty_entries(self):
        total = sum(len(entries) for entries in self.data.values())
        self.assertEqual(total, 30)
        self.assertEqual(len(self.data), len(knowledge.MAJORS))

    def test_every_major_has_five(self):
        for major in knowledge.MAJORS:
            with self.subTest(major=major):
                self.assertEqual(len(self.data.get(major, {})), 5)

    def test_entry_shape(self):
        for major, entries in self.data.items():
            for topic, item in entries.items():
                with self.subTest(major=major, topic=topic):
                    steps = item.get("outline") or []
                    self.assertGreaterEqual(len(steps), 3)
                    self.assertLessEqual(len(steps), 4)
                    for step in steps:
                        self.assertTrue(str(step).strip())
                    self.assertTrue(str(item.get("avoid") or "").strip())

    def test_every_topic_exists_in_its_major(self):
        for major, entries in self.data.items():
            bucket = self.rubrics.get(major) or {}
            for topic in entries:
                with self.subTest(major=major, topic=topic):
                    self.assertIn(topic, bucket)

    def test_levels_span_at_least_two_tiers(self):
        """每专业 5 条里至少跨两档，否则框架只覆盖同一种难度。"""
        for major, entries in self.data.items():
            bucket = self.rubrics.get(major) or {}
            levels = {bucket[t]["level"] for t in entries if t in bucket}
            with self.subTest(major=major):
                self.assertGreaterEqual(len(levels), 2, f"{major} 只覆盖了 {levels}")

    def test_no_outline_line_leaks_a_must_hit(self):
        """框架句不能与判分要点互为子串——那就是泄题。"""
        for major, entries in self.data.items():
            bucket = self.rubrics.get(major) or {}
            for topic, item in entries.items():
                entry = bucket.get(topic) or {}
                for step in item.get("outline") or []:
                    for must in entry.get("must_hit") or []:
                        with self.subTest(major=major, topic=topic):
                            self.assertNotIn(step, must, f"{topic}：框架句「{step}」出现在判分要点里")
                            self.assertNotIn(must, step, f"{topic}：判分要点「{must}」被写进框架句")


class TestAnswerOutlineLookup(unittest.TestCase):
    def test_known_topic_returns_steps_and_avoid(self):
        outline = knowledge.answer_outline(
            "计算机科学与技术", "红黑树的性质与旋转调整过程"
        )
        self.assertIsNotNone(outline)
        self.assertGreaterEqual(len(outline["outline"]), 3)
        self.assertIn("红黑树", outline["avoid"])

    def test_unknown_topic_returns_none(self):
        self.assertIsNone(knowledge.answer_outline("计算机科学与技术", "不存在的考点"))

    def test_unknown_major_returns_none(self):
        self.assertIsNone(knowledge.answer_outline("不存在专业", "红黑树的性质与旋转调整过程"))

    def test_empty_inputs_return_none(self):
        self.assertIsNone(knowledge.answer_outline("", ""))
        self.assertIsNone(knowledge.answer_outline("计算机科学与技术", "   "))

    def test_lookup_is_exact_not_fuzzy(self):
        """只认逐字一致：截短的、改写过的都不该命中。"""
        self.assertIsNone(knowledge.answer_outline("计算机科学与技术", "红黑树"))
        self.assertIsNone(
            knowledge.answer_outline("计算机科学与技术", "红黑树性质与旋转调整过程")
        )


class TestAnswerOutlineDegrades(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        with mock.patch.object(
            knowledge, "ANSWER_OUTLINE_FILE", Path("no_such_outline_file.json")
        ):
            self.assertEqual(knowledge.load_answer_outlines(), {})

    def test_corrupt_file_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            with mock.patch.object(knowledge, "ANSWER_OUTLINE_FILE", broken):
                self.assertEqual(knowledge.load_answer_outlines(), {})

    def test_non_dict_top_level_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            weird = Path(tmp) / "list.json"
            weird.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
            with mock.patch.object(knowledge, "ANSWER_OUTLINE_FILE", weird):
                self.assertEqual(knowledge.load_answer_outlines(), {})

    def test_lookup_returns_none_when_library_missing(self):
        with mock.patch.object(
            knowledge, "ANSWER_OUTLINE_FILE", Path("no_such_outline_file.json")
        ):
            self.assertIsNone(
                knowledge.answer_outline("计算机科学与技术", "红黑树的性质与旋转调整过程")
            )


if __name__ == "__main__":
    unittest.main()
