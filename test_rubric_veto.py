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
        total = sum(len(by_topic) for by_topic in self.rubrics.values())
        marked = sum(len(topics) for topics in annotated.values())
        # 覆盖率上界不是 100%：一部分考点的 common_errors 写的是"没答到/做得不好"，
        # 没有"他说了一句确定错话"这种条目——那种不该标一票否决（见 .tmp/veto_apply2.py）。
        self.assertGreaterEqual(marked / total, 0.8, "veto 覆盖率跌破 80%")
        for major, by_topic in self.rubrics.items():
            topics = annotated[major]
            self.assertTrue(topics, f"{major} 还没有标 veto 的考点")
            # 电子信息/自动化的 C 档考点里，仪器操作、测量方法、接线规范占相当比例，
            # 那些误区是"操作不严谨"而非"概念说错"，按判定标准不标——所以专业下限比总下限低。
            self.assertGreaterEqual(
                len(topics) / len(by_topic), 0.70, f"{major} 的 veto 覆盖率偏低"
            )

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
                        self.assertLessEqual(len(veto), 60)
                        self.assertTrue(
                            any(veto in err for err in errors),
                            "veto 必须是该考点 common_errors 的原文片段",
                        )


class TestVetoRoundTrip(unittest.TestCase):
    """判定链路：真实数据里的 veto 能渲染、命中即判不通过。"""

    MAJOR = "计算机科学与技术"
    TOPIC = "事务隔离级别与 MVCC 的实现原理"
    # 必须挑一条**没有** veto 的考点：自我介绍属于表达类，误区都是"说得不好"
    # 而不是"说错了概念"，按判定标准不标一票否决。
    PLAIN_TOPIC = "自我介绍的结构与重点"

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


class TestUnaskedRows(unittest.TestCase):
    """没问到的考点不能算成「一条要点都没答到」。"""

    def profile(self):
        return {"major": "计算机科学与技术"}

    def test_new_style_placeholder_is_unknown(self):
        row = {
            "topic": "事务隔离级别与 MVCC 的实现原理",
            "must_hit": [],
            "missed": ["本场未问到"],
            "errors": [],
            "comment": "本场未考察该考点",
        }
        agg = knowledge.aggregate_rubric_check(self.profile(), [row])
        self.assertEqual(agg["topics"][0]["verdict"], knowledge.VERDICT_UNKNOWN)
        self.assertEqual(agg["counts"][knowledge.VERDICT_FAIL], 0)
        self.assertEqual(agg["weak"], [])

    def test_legacy_placeholder_is_unknown(self):
        row = {
            "topic": "事务隔离级别与 MVCC 的实现原理",
            "must_hit": [],
            "missed": ["理解 MVCC 快照读与当前读的区别"],
            "errors": [],
            "comment": "本场未问到该考点。",
        }
        agg = knowledge.aggregate_rubric_check(self.profile(), [row])
        self.assertEqual(agg["topics"][0]["verdict"], knowledge.VERDICT_UNKNOWN)

    def test_asked_but_unanswered_stays_fail(self):
        row = {
            "topic": "事务隔离级别与 MVCC 的实现原理",
            "must_hit": [],
            "missed": ["理解 MVCC 快照读与当前读的区别"],
            "errors": [],
            "comment": "候选人没有回答",
        }
        agg = knowledge.aggregate_rubric_check(self.profile(), [row])
        self.assertEqual(agg["topics"][0]["verdict"], knowledge.VERDICT_FAIL)


if __name__ == "__main__":
    unittest.main()
