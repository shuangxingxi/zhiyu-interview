# -*- coding: utf-8 -*-
"""考点锚定判定（topic_anchored / unanchored_topics / unanchored_block）回归测试。

背景（缺陷 2）：面试官会自拟题，并把自拟题的考点名写进 `topic`。这个名字
多数时候不在参照库里，于是评估官拿不到参照、`rubric_check` 整段为空，报告
里的「考点级判定」静默消失。

更隐蔽的是**部分锚定**：本场既有库内考点、又有自拟题时，`rubric_block` 只
列出库内那几条，评估官会拿它们去套所有对话，把自拟题的表现硬判给库内考点。
报告上看不出任何异常——这比空报告更危险。

所以引入「锚定」这个概念，两个条件同时成立才算锚定：
    ① 在本场考点池里逐字出现（排除模型自拟）
    ② 参照库里确实有这一条（排除配置错误）
"""

import unittest

import knowledge


def make_profile(interview_type: str = "技术面", level: str = "B") -> dict:
    return knowledge.build_profile(
        scene="应届生校招",
        major="计算机科学与技术",
        level=level,
        interview_type=interview_type,
        total_rounds=3,
        use_real=False,
        real_quota=0,
    )


class TestTopicAnchored(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = make_profile()
        cls.pool = list(cls.profile["topics"])

    def test_pool_topic_is_anchored(self):
        for topic in self.pool:
            self.assertTrue(
                knowledge.topic_anchored(self.profile, topic),
                f"池内考点应判为锚定：{topic}",
            )

    def test_self_invented_topic_is_not_anchored(self):
        # 真实漂移样本（2026-10-23 那场逐字稿里模型自己写的三个名字）
        for topic in (
            "项目经历与个人贡献边界",
            "数据库系统原理",
            "计算机操作系统 - 进程管理",
        ):
            self.assertFalse(knowledge.topic_anchored(self.profile, topic), topic)

    def test_library_topic_outside_the_pool_is_not_anchored(self):
        # 库里确实有这题，但不在本场池里 → 模型不可能合法问到它。
        # 若判为锚定，等于承认「模型贴了一个本场根本没有的考点名」也能拿到参照。
        #
        # 不能硬编码某个考点名：难度档是混合抽池（B 档会抽到 A 档题），
        # 写死「秒杀系统的库存与流量设计」这类名称会随抽池结果时对时错。
        # 这里从库里现找一个不在池里的，用例才稳定。
        outside = self._a_library_topic_outside_the_pool()
        self.assertIsNotNone(outside, "库里应当存在不在本场池中的考点")
        self.assertFalse(knowledge.topic_anchored(self.profile, outside))

    def _a_library_topic_outside_the_pool(self) -> str | None:
        by_topic = knowledge.load_rubrics()["计算机科学与技术"]
        for topic in by_topic:
            if topic not in self.pool:
                return topic
        return None

    def test_fuzzy_lookalike_is_not_anchored(self):
        # 「索引」在库里能找到包含它的条目，但它不在池里 → 不锚定。
        # 这条守住的是"不做字符串近似归一"这个决定：靠猜映射会判出更隐蔽的错。
        self.assertFalse(knowledge.topic_anchored(self.profile, "索引"))

    def test_blank_and_none_are_not_anchored(self):
        self.assertFalse(knowledge.topic_anchored(self.profile, ""))
        self.assertFalse(knowledge.topic_anchored(self.profile, "   "))
        self.assertFalse(knowledge.topic_anchored(self.profile, None))

    def test_whitespace_is_stripped_before_matching(self):
        self.assertTrue(knowledge.topic_anchored(self.profile, f"  {self.pool[0]}  "))

    def test_behavior_topic_is_not_anchored_in_a_tech_session(self):
        self.assertFalse(
            knowledge.topic_anchored(self.profile, knowledge.BEHAVIOR_TOPICS[0])
        )

    def test_jd_override_pool_still_anchors(self):
        jd = dict(self.profile)
        jd["topics"] = self.pool[:2]
        jd["jd_topics"] = list(jd["topics"])
        self.assertEqual(
            [knowledge.topic_anchored(jd, t) for t in jd["topics"]], [True, True]
        )


class TestUnanchoredTopics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = make_profile()
        cls.pool = list(cls.profile["topics"])

    def test_returns_only_the_unanchored_ones(self):
        asked = [self.pool[0], "数据库系统原理", self.pool[1], "项目经历与个人贡献边界"]
        self.assertEqual(
            knowledge.unanchored_topics(self.profile, asked),
            ["数据库系统原理", "项目经历与个人贡献边界"],
        )

    def test_deduplicates_and_keeps_first_seen_order(self):
        asked = ["B 题", self.pool[0], "A 题", "B 题", "  A 题  ", ""]
        self.assertEqual(knowledge.unanchored_topics(self.profile, asked), ["B 题", "A 题"])

    def test_all_anchored_yields_empty(self):
        self.assertEqual(knowledge.unanchored_topics(self.profile, self.pool[:3]), [])

    def test_none_and_empty_are_safe(self):
        self.assertEqual(knowledge.unanchored_topics(self.profile, None), [])
        self.assertEqual(knowledge.unanchored_topics(self.profile, []), [])

    def test_behavior_session_anchors_all_of_its_topics(self):
        profile = make_profile("行为面")
        # 行为面按难度档分池（v17），取本档自己的池，不能用三档并集切片。
        self.assertEqual(
            knowledge.unanchored_topics(profile, knowledge.behavior_topics("B")[:3]), []
        )


class TestUnanchoredBlock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = make_profile()
        cls.pool = list(cls.profile["topics"])

    def test_block_lists_the_unanchored_topics(self):
        asked = [self.pool[0], "数据库系统原理"]
        block = knowledge.unanchored_block(self.profile, asked)
        self.assertIn("【没有参照的题】", block)
        self.assertIn("数据库系统原理", block)
        # 锚定的那条不该出现在"没有参照"清单里
        self.assertNotIn(f"- {self.pool[0]}", block)

    def test_block_is_empty_when_everything_is_anchored(self):
        self.assertEqual(knowledge.unanchored_block(self.profile, self.pool[:3]), "")

    def test_block_is_empty_when_nothing_was_asked(self):
        self.assertEqual(knowledge.unanchored_block(self.profile, None), "")
        self.assertEqual(knowledge.unanchored_block(self.profile, []), "")


class TestAggregateSourceLabels(unittest.TestCase):
    """aggregate 给每行标来源，供报告分区显示。"""

    @classmethod
    def setUpClass(cls):
        cls.profile = make_profile()
        cls.pool = list(cls.profile["topics"])
        cls.rows = [
            {"topic": cls.pool[0], "must_hit": ["a"], "missed": [], "errors": []},
            {"topic": "数据库系统原理", "must_hit": [], "missed": ["b"], "errors": []},
        ]

    def test_source_field_per_row(self):
        agg = knowledge.aggregate_rubric_check(self.profile, self.rows)
        self.assertEqual(
            [t["source"] for t in agg["topics"]], ["library", "generated"]
        )

    def test_counts_add_up(self):
        agg = knowledge.aggregate_rubric_check(self.profile, self.rows)
        self.assertEqual((agg["library"], agg["generated"]), (1, 1))
        self.assertEqual(agg["library"] + agg["generated"], agg["total"])

    def test_generated_rows_still_get_a_verdict(self):
        # 现拟参照同样要判——不因为"没库参照"就跳过。
        agg = knowledge.aggregate_rubric_check(self.profile, self.rows)
        gen = [t for t in agg["topics"] if t["source"] == "generated"][0]
        self.assertEqual(gen["verdict"], knowledge.VERDICT_FAIL)

    def test_generated_rows_cannot_trigger_veto(self):
        # veto 只认库条目的显式标记：现拟参照没有否决权。
        rows = [
            {
                "topic": "数据库系统原理",
                "must_hit": [],
                "missed": ["x"],
                "errors": ["把 MVCC 说成加锁实现"],
            }
        ]
        agg = knowledge.aggregate_rubric_check(self.profile, rows)
        self.assertNotIn("一票否决", agg["topics"][0]["reason"])

    def test_empty_rows_yield_zero_counts(self):
        agg = knowledge.aggregate_rubric_check(self.profile, [])
        self.assertEqual((agg["total"], agg["library"], agg["generated"]), (0, 0, 0))


class TestEvaluatorPromptContract(unittest.TestCase):
    """提示词里那几段现拟规则必须真实存在，否则块 4 等于没做。"""

    @classmethod
    def setUpClass(cls):
        import agents

        cls.tpl = agents.EVALUATOR_SCORE_SYSTEM

    def test_unanchored_block_placeholder_is_present(self):
        self.assertIn("{unanchored_block}", self.tpl)

    def test_generated_rule_forbids_veto(self):
        self.assertIn("现拟参照不许写「一票否决」", self.tpl)

    def test_generated_rule_requires_source_field(self):
        self.assertIn('"source": "library 或 generated"', self.tpl)

    def test_round_attribution_rule_is_present(self):
        self.assertIn("按轮次归属", self.tpl)

    def test_transcript_carries_topic_headers(self):
        import agents

        history = [
            {"role": "assistant", "content": "请讲讲 MVCC", "decision": {"topic": "MVCC"}},
            {"role": "user", "content": "好的"},
            {"role": "assistant", "content": "换个话题", "topic": "自拟题"},
            {"role": "user", "content": "嗯"},
        ]
        text = agents.format_transcript(history, tag_topics=True)
        self.assertIn("【考点：MVCC】面试官：请讲讲 MVCC", text)
        self.assertIn("【考点：自拟题】面试官：换个话题", text)
        self.assertIn("候选人：好的", text)

    def test_transcript_marks_missing_topic(self):
        import agents

        text = agents.format_transcript(
            [{"role": "assistant", "content": "开场白"}], tag_topics=True
        )
        self.assertIn("【考点：（未标注）】", text)

    def test_transcript_hides_topic_headers_by_default(self):
        # 教练 / 建议官拿到的记录不该出现内部标记：它们的提示词里没有解释过这个标记，
        # 模型轻则当噪音，重则把标签原文抄进给候选人看的建议里。
        import agents

        history = [
            {"role": "assistant", "content": "请讲讲 MVCC", "decision": {"topic": "MVCC"}},
            {"role": "user", "content": "好的"},
        ]
        text = agents.format_transcript(history)
        self.assertNotIn("【考点：", text)
        self.assertIn("面试官：请讲讲 MVCC", text)

    def test_only_the_evaluator_asks_for_topic_headers(self):
        # 钉住调用点：评估官必须带，教练与建议官必须不带。
        import pathlib
        import re

        src = (pathlib.Path(__file__).resolve().parent / "agents.py").read_text(encoding="utf-8")
        tagged = re.findall(r"format_transcript\([^)]*tag_topics=True", src)
        self.assertEqual(len(tagged), 1, f"应只有评估官带考点标签，实际 {len(tagged)} 处")
        self.assertIn("transcript = format_transcript(history, tag_topics=True)", src)


class TestUntopicedRowsAreDropped(unittest.TestCase):
    """format_transcript 给收尾轮标的占位符，不能变成一条假考点。

    实测来历：技术面那场最后一轮 action=end，被标成【考点：（未标注）】，
    评估官照着补了一行 {"topic": "（未标注）", "source": "generated"}，
    报告里就多出一条既没参照也没内容的判定。
    """

    def _normalize(self, rows):
        import agents

        return agents._normalize_rubric_check({"rubric_check": rows})["rubric_check"]

    def test_placeholder_topics_are_removed(self):
        rows = self._normalize(
            [
                {"topic": "内存泄漏的定位方法与工具体系", "source": "library"},
                {"topic": "（未标注）", "source": "generated"},
                {"topic": "—", "source": "generated"},
                {"topic": "  ", "source": "generated"},
                {"topic": "项目经历与个人贡献", "source": "generated"},
            ]
        )
        self.assertEqual(
            [r["topic"] for r in rows],
            ["内存泄漏的定位方法与工具体系", "项目经历与个人贡献"],
        )

    def test_clean_rows_are_untouched(self):
        import agents

        score = {"overall_score": 1, "rubric_check": [{"topic": "真实考点"}]}
        self.assertIs(agents._normalize_rubric_check(score), score)

    def test_missing_or_non_list_field_is_safe(self):
        import agents

        self.assertEqual(agents._normalize_rubric_check({"a": 1}), {"a": 1})
        self.assertEqual(
            agents._normalize_rubric_check({"rubric_check": "x"}), {"rubric_check": "x"}
        )

    def test_all_placeholders_yield_empty_list(self):
        self.assertEqual(self._normalize([{"topic": "（未标注）"}]), [])


class TestBlockOnlyListsAnchored(unittest.TestCase):
    """「部分锚定」是最危险的情形：评估官手里只有库参照，会拿它套所有对话。

    方案原文（块 4）要求：rubric_block 里只出现真正锚定的考点；自拟题另走
    unanchored_block，绝不混进参照清单里假装有依据。
    """

    MAJOR = "计算机科学与技术"

    def setUp(self):
        import knowledge

        self.knowledge = knowledge
        self.profile = knowledge.build_profile(
            scene="应届生校招",
            major=self.MAJOR,
            level="B",
            interview_type="技术面",
            total_rounds=5,
            use_real=False,
            real_quota=0,
        )

    def test_block_lists_only_anchored_topics(self):
        anchored = self.profile["topics"][0]
        mixed = [anchored, "项目经历与个人贡献边界", "数据库系统原理"]
        block = self.knowledge.rubric_block(self.profile, mixed)
        self.assertIn(f"考点：{anchored}", block)
        self.assertNotIn("项目经历与个人贡献边界", block)
        self.assertNotIn("数据库系统原理", block)

    def test_unanchored_block_names_them_instead(self):
        anchored = self.profile["topics"][0]
        mixed = [anchored, "数据库系统原理"]
        text = self.knowledge.unanchored_block(self.profile, mixed)
        self.assertIn("数据库系统原理", text)
        self.assertNotIn(anchored, text)

    def test_hint_block_is_empty_for_a_self_invented_topic(self):
        # 面试官提示词里的「当前考点参照」：自拟题没有参照，必须给空串，
        # 而不是硬塞一条别的考点的要点进去。
        self.assertEqual(self.knowledge.rubric_hint_block(self.profile, "数据库系统原理"), "")
        self.assertNotEqual(
            self.knowledge.rubric_hint_block(self.profile, self.profile["topics"][0]), ""
        )

    def test_unanchored_topics_equals_asked_minus_anchored(self):
        asked = [self.profile["topics"][0], "自拟甲", self.profile["topics"][1], "自拟乙"]
        self.assertEqual(
            self.knowledge.unanchored_topics(self.profile, asked), ["自拟甲", "自拟乙"]
        )


class TestMasteryIgnoresGenerated(unittest.TestCase):
    """边界③：现拟参照不能进 `jd_match.mastery`（"你在这个考点上的历史水平"）。"""

    TOPIC = "红黑树的性质与旋转调整过程"

    def _record(self, source: str) -> dict:
        return {
            "time": "2026-10-24T00:00:00",
            "profile": {"major": "计算机科学与技术", "level": "B"},
            "evaluation": {
                "rubric_check": [
                    {
                        "topic": self.TOPIC,
                        # 约定：`must_hit` 放的是**已命中**的要点（报告里那一列就叫「已命中」），
                        # 所以「一条都没答到」= must_hit 空 + missed 非空 → FAIL。
                        "must_hit": [],
                        "missed": ["a", "b"],
                        "errors": [],
                        "comment": "",
                        "source": source,
                    }
                ]
            },
        }

    def test_generated_row_is_skipped(self):
        import jd_match

        self.assertEqual(jd_match.mastery([self._record("generated")], "计算机科学与技术"), {})

    def test_library_row_still_counts(self):
        import jd_match

        stats = jd_match.mastery([self._record("library")], "计算机科学与技术")
        self.assertIn(self.TOPIC, stats)
        self.assertEqual(stats[self.TOPIC]["fail"], 1)

    def test_legacy_row_without_source_counts(self):
        # 旧记录没有 source 字段，必须按库参照处理，不能被当成现拟跳过。
        import jd_match

        record = self._record("library")
        del record["evaluation"]["rubric_check"][0]["source"]
        self.assertIn(self.TOPIC, jd_match.mastery([record], "计算机科学与技术"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
