# -*- coding: utf-8 -*-
"""rubrics.json 的 veto 数据与判定链路回归测试。

veto 的语义是"误区里最致命的那几条"：踩中即该考点判为不通过。
数据侧约束：veto 必须摘自考点的 common_errors 原文，不能是另写的一句话——
否则提示词里会出现"评估官手里没有的误区"，而且评估官原样照抄时也对不上。
"""

import unittest

import knowledge


class TestVetoData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rubrics = knowledge.load_rubrics()

    def test_all_topics_still_load(self):
        topics = [topic for by_topic in self.rubrics.values() for topic in by_topic.values()]
        expected = sum(
            len(level_topics)
            for major in knowledge.MAJOR_MODEL.values()
            for level_topics in major["topics"].values()
        )
        self.assertEqual(len(topics), expected)
        self.assertEqual(len(self.rubrics), 6)

    def test_veto_batch_covers_every_major(self):
        annotated = {
            major: [topic for topic, entry in by_topic.items() if entry.get("veto")]
            for major, by_topic in self.rubrics.items()
        }
        self.assertGreaterEqual(
            sum(len(topics) for topics in annotated.values()), 72
        )
        for major, topics in annotated.items():
            self.assertTrue(topics, f"{major} 还没有标 veto 的考点")

    def test_every_veto_is_a_short_verbatim_fragment(self):
        for major, by_topic in self.rubrics.items():
            for topic, entry in by_topic.items():
                vetoes = entry.get("veto") or []
                if not vetoes:
                    continue
                errors = [str(err) for err in entry.get("common_errors") or []]
                for veto in vetoes:
                    with self.subTest(major=major, topic=topic, veto=veto):
                        self.assertIsInstance(veto, str)
                        self.assertGreaterEqual(len(veto), 6)
                        self.assertLessEqual(len(veto), 40)
                        self.assertTrue(
                            any(veto in err for err in errors),
                            "veto 必须是该考点 common_errors 的原文片段",
                        )


class TestVetoRoundTrip(unittest.TestCase):
    """判定链路：真实数据里的 veto 能渲染、命中即判不通过。"""

    MAJOR = "计算机科学与技术"
    TOPIC = "事务隔离级别与 MVCC 的实现原理"
    PLAIN_TOPIC = "红黑树的性质与旋转调整过程"

    def _profile(self):
        return {"major": self.MAJOR}

    def test_block_renders_veto_only_for_annotated_topic(self):
        marked = knowledge.rubric_block(self._profile(), [self.TOPIC])
        plain = knowledge.rubric_block(self._profile(), [self.PLAIN_TOPIC])
        self.assertIn("一票否决", marked)
        self.assertIn("把 MVCC 说成加锁实现", marked)
        self.assertNotIn("一票否决", plain, "没标 veto 的考点不该多出这一行")

    def test_verdict_fails_when_evaluator_reports_veto_hit(self):
        entry = knowledge.rubric_for_topic(self._profile(), self.TOPIC)
        self.assertIsNotNone(entry)
        row = {
            "topic": self.TOPIC,
            "must_hit": entry.get("must_hit") or [],
            "missed": [],
            "errors": ["把 MVCC 说成加锁实现"],
        }
        verdict, reason = knowledge.topic_verdict(row, entry.get("veto"))
        self.assertEqual(verdict, knowledge.VERDICT_FAIL)
        self.assertIn("一票否决", reason)

    def test_aggregate_reads_veto_from_real_rubrics(self):
        row = {
            "topic": self.TOPIC,
            "must_hit": [],
            "missed": [],
            "errors": ["把 MVCC 说成加锁实现"],
        }
        agg = knowledge.aggregate_rubric_check(self._profile(), [row])
        self.assertEqual(agg["topics"][0]["verdict"], knowledge.VERDICT_FAIL)
        self.assertIn("一票否决", agg["topics"][0]["reason"])

    def test_non_veto_error_does_not_claim_veto(self):
        row = {
            "topic": self.PLAIN_TOPIC,
            "must_hit": [],
            "missed": [],
            "errors": ["一句和 veto 无关的概念错误"],
        }
        agg = knowledge.aggregate_rubric_check(self._profile(), [row])
        self.assertEqual(agg["topics"][0]["verdict"], knowledge.VERDICT_FAIL)
        self.assertNotIn("一票否决", agg["topics"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
