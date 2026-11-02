# -*- coding: utf-8 -*-
"""行为面参照库（rubric_behavior.json）的数据与取用回归测试。

行为面此前**每一场都必然零参照**：rubric_for 一律用 `load_rubrics()[major]`
查表，而行为面那 15 条考点不按专业分、从来不在任何专业桶里。结果是行为面
报告里的「考点级判定」整段静默消失，用户只看到报告少了半页。

修法是给行为面单独一个参照文件 + 一个 rubric_table 分派函数。这个文件守住
三件事：数据完整（15 条一一对应、都带 veto）、取用正确（行为面走行为面表、
技术面不受影响）、降级安全（文件缺失不抛异常）。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import knowledge


class TestBehaviorRubricData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rubrics = knowledge.load_behavior_rubrics()

    def test_every_behavior_topic_has_an_entry(self):
        self.assertEqual(len(self.rubrics), len(knowledge.BEHAVIOR_TOPICS))
        self.assertEqual(set(self.rubrics), set(knowledge.BEHAVIOR_TOPICS))

    def test_entries_have_the_same_shape_as_the_tech_library(self):
        # 下游（rubric_block / topic_verdict / aggregate_rubric_check）拿到的条目
        # 形状必须与技术面完全一致，否则要到处加分支。
        tech = knowledge.load_rubrics()["计算机科学与技术"]
        tech_keys = {key for entry in tech.values() for key in entry}
        for topic, entry in self.rubrics.items():
            self.assertIsInstance(entry, dict, topic)
            self.assertTrue(set(entry) <= tech_keys, f"{topic} 出现技术面没有的键")
            self.assertGreaterEqual(len(entry.get("must_hit") or []), 3, topic)
            self.assertTrue(entry.get("common_errors"), topic)

    def test_every_entry_has_a_veto_that_quotes_its_own_common_errors(self):
        # veto 判定在代码里做的是子串匹配：不是 common_errors 的原文片段就永远判不中。
        for topic, entry in self.rubrics.items():
            veto = knowledge._clean_list(entry.get("veto"))
            self.assertTrue(veto, f"{topic} 缺 veto")
            errors = [str(x) for x in (entry.get("common_errors") or [])]
            for line in veto:
                self.assertTrue(
                    any(line in err for err in errors),
                    f"{topic} 的 veto「{line}」不是 common_errors 的原文片段",
                )

    def test_no_entry_carries_a_dimension_field(self):
        # 与技术面一致：维度由 MAJOR_MODEL 的考点名对齐，不写在参照里。
        for topic, entry in self.rubrics.items():
            self.assertNotIn("dimension", entry, topic)


class TestRubricTableDispatch(unittest.TestCase):
    def _profile(self, interview_type: str) -> dict:
        return knowledge.build_profile(
            scene="应届生校招",
            major="计算机科学与技术",
            level="B",
            interview_type=interview_type,
            total_rounds=3,
            use_real=False,
            real_quota=0,
        )

    def test_behavior_interview_uses_the_behavior_table(self):
        table = knowledge.rubric_table(self._profile("行为面"))
        self.assertEqual(set(table), set(knowledge.BEHAVIOR_TOPICS))

    def test_tech_interview_still_uses_the_major_bucket(self):
        table = knowledge.rubric_table(self._profile("技术面"))
        self.assertIn("分布式链路追踪与故障根因定位", table)
        self.assertNotIn(knowledge.BEHAVIOR_TOPICS[0], table)

    def test_behavior_topics_are_not_in_any_major_bucket(self):
        # 这是独立文件的理由：并进 rubrics.json 里的任何一个专业桶都是错放。
        rubrics = knowledge.load_rubrics()
        for major, bucket in rubrics.items():
            for topic in knowledge.BEHAVIOR_TOPICS:
                self.assertNotIn(topic, bucket, f"{major} 桶里混进了行为面考点 {topic}")

    def test_unknown_major_degrades_to_empty(self):
        profile = self._profile("技术面")
        profile["major"] = "不存在的专业"
        self.assertEqual(knowledge.rubric_table(profile), {})


class TestBehaviorRubricWiring(unittest.TestCase):
    """行为面走通 rubric_for / rubric_block / rubric_for_topic 全链路。"""

    @classmethod
    def setUpClass(cls):
        cls.profile = knowledge.build_profile(
            scene="应届生校招",
            major="人工智能",
            level="B",
            interview_type="行为面",
            total_rounds=3,
            use_real=False,
            real_quota=0,
        )
        cls.asked = knowledge.behavior_topics("B")[:3]

    def test_rubric_for_returns_every_asked_behavior_topic(self):
        items = knowledge.rubric_for(self.profile, self.asked)
        self.assertEqual(len(items), 3)
        self.assertEqual([i["topic"] for i in items], self.asked)

    def test_rubric_block_no_longer_degrades(self):
        block = knowledge.rubric_block(self.profile, self.asked)
        self.assertNotIn("未提供考点参照", block)
        self.assertIn("必须命中的要点", block)
        self.assertIn("一票否决的误区", block)

    def test_rubric_for_topic_works_for_single_behavior_topic(self):
        item = knowledge.rubric_for_topic(self.profile, knowledge.BEHAVIOR_TOPICS[0])
        self.assertIsNotNone(item)
        self.assertTrue(item["must_hit"])

    def test_rubric_hint_block_works_for_the_interviewer(self):
        text = knowledge.rubric_hint_block(self.profile, knowledge.BEHAVIOR_TOPICS[0])
        self.assertIn("你正在问的考点", text)

    def test_aggregate_marks_behavior_rows_as_library(self):
        rows = [
            {"topic": self.asked[0], "must_hit": ["a"], "missed": [], "errors": []},
            {"topic": self.asked[1], "must_hit": [], "missed": ["x"], "errors": []},
        ]
        agg = knowledge.aggregate_rubric_check(self.profile, rows)
        self.assertEqual(agg["library"], 2)
        self.assertEqual(agg["generated"], 0)


class TestBehaviorRubricDegrades(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        with mock.patch.object(
            knowledge, "BEHAVIOR_RUBRIC_FILE", Path("no_such_file_anywhere.json")
        ):
            self.assertEqual(knowledge.load_behavior_rubrics(), {})

    def test_corrupt_file_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            with mock.patch.object(knowledge, "BEHAVIOR_RUBRIC_FILE", broken):
                self.assertEqual(knowledge.load_behavior_rubrics(), {})

    def test_non_dict_top_level_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            weird = Path(tmp) / "list.json"
            weird.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
            with mock.patch.object(knowledge, "BEHAVIOR_RUBRIC_FILE", weird):
                self.assertEqual(knowledge.load_behavior_rubrics(), {})

    def test_block_falls_back_when_library_missing(self):
        profile = knowledge.build_profile(
            scene="应届生校招",
            major="计算机科学与技术",
            level="B",
            interview_type="行为面",
            total_rounds=3,
            use_real=False,
            real_quota=0,
        )
        with mock.patch.object(knowledge, "load_behavior_rubrics", lambda: {}):
            self.assertEqual(knowledge.rubric_for(profile, knowledge.BEHAVIOR_TOPICS[:2]), [])
            self.assertIn(
                "未提供考点参照", knowledge.rubric_block(profile, knowledge.BEHAVIOR_TOPICS[:2])
            )


class TestBehaviorTiering(unittest.TestCase):
    """行为面按难度档分池（v17）：A 考复杂局面、C 考校园场景。"""

    @classmethod
    def setUpClass(cls):
        cls.tiers = knowledge.BEHAVIOR_TOPIC_TIERS

    def test_three_tiers_have_fifteen_topics_each(self):
        self.assertEqual(sorted(self.tiers), ["A", "B", "C"])
        for level, topics in self.tiers.items():
            self.assertEqual(len(topics), 15, f"{level} 档应 15 条")

    def test_tiers_do_not_overlap(self):
        sets = {level: set(topics) for level, topics in self.tiers.items()}
        self.assertEqual(len(sets["A"] & sets["B"]), 0)
        self.assertEqual(len(sets["A"] & sets["C"]), 0)
        self.assertEqual(len(sets["B"] & sets["C"]), 0)

    def test_union_matches_behavior_topics(self):
        self.assertEqual(len(knowledge.BEHAVIOR_TOPICS), 45)
        self.assertEqual(set(knowledge.BEHAVIOR_TOPICS), set().union(*self.tiers.values()))

    def test_every_rubric_level_matches_its_tier(self):
        rubrics = knowledge.load_behavior_rubrics()
        for level, topics in self.tiers.items():
            for topic in topics:
                self.assertEqual(rubrics[topic].get("level"), level, topic)

    def test_behavior_topics_returns_the_pool_of_that_level(self):
        for level in ("A", "B", "C"):
            self.assertEqual(knowledge.behavior_topics(level), list(self.tiers[level]))

    def test_unknown_level_falls_back_to_b(self):
        self.assertEqual(knowledge.behavior_topics("Z"), list(self.tiers["B"]))
        self.assertEqual(knowledge.behavior_topics(""), list(self.tiers["B"]))

    def test_behavior_topics_is_a_copy(self):
        pool = knowledge.behavior_topics("A")
        pool.clear()
        self.assertEqual(len(knowledge.behavior_topics("A")), 15)

    def test_profile_topics_follow_the_level(self):
        pools = {}
        for level in ("A", "B", "C"):
            profile = knowledge.build_profile(
                scene="应届生校招",
                major="计算机科学与技术",
                level=level,
                interview_type="行为面",
                total_rounds=5,
                use_real=False,
                real_quota=0,
            )
            self.assertEqual(len(profile["topics"]), 15)
            self.assertEqual(profile["topic_mix"], {})
            self.assertEqual(profile["topic_tiers"], {})
            pools[level] = frozenset(profile["topics"])
        self.assertEqual(len(set(pools.values())), 3, "三档不应拿到同一批题")


if __name__ == "__main__":
    unittest.main(verbosity=2)
