"""面试官侧「当前考点参照」注入（knowledge.rubric_for_topic / rubric_hint_block）的单元测试。

被测对象：`knowledge.rubric_for_topic` 与 `knowledge.rubric_hint_block`，
以及 `agents._current_topic`。

为什么值得测：这是 v10 补上的一个**口径缺口**——评估官一直拿得到
`must_hit` / `bonus` / `common_errors`，而面试官此前只拿得到考点**名字**。
于是"问的人"不知道"评的人"在看什么，DESIGN 说的"评分口径与提问口径同源"
在运行时并没有兑现。

这条链路上有三处容易出错，所以各钉一条断言：
    1. **取错条目** —— 检索键是（专业, 考点原文），换专业必须取不到；
    2. **泄露答案** —— 提示词必须明确禁止念要点/对答案，否则模型会照着清单念；
    3. **无参照时不该硬塞** —— 自拟题、行为面、参照库缺失时都要安静地留空。

测试原则：**用真件**。直接读工程里的 `rubrics.json`（真数据、真结构），
只把"模型"那一层排除在外——这个模块本来就不调模型。

运行方式（项目根目录，零新增依赖）：
    py -3.12 -m unittest test_rubric_hint -v
"""

from __future__ import annotations

import unittest
from unittest import mock

import agents
import knowledge


# 用真实参照库里的第一条：计算机科学与技术 / A 档 / 红黑树
REAL_MAJOR = "计算机科学与技术"
REAL_TOPIC = "红黑树的性质与旋转调整过程"


def make_profile(**overrides) -> dict:
    profile = {
        "major": REAL_MAJOR,
        "level": "A",
        "topics": [REAL_TOPIC],
        "dimensions": ["编程基础与数据结构"],
        "interview_type": "技术面",
        "total_rounds": 5,
        "max_probes": 3,
        "strict": False,
        "allow_reflect": True,
        "real_questions": [],
        "avoid_topics": [],
        "mood_style": "",
    }
    profile.update(overrides)
    return profile


class TestRubricForTopic(unittest.TestCase):
    """按考点原文取单条参照。"""

    def test_real_topic_hits(self):
        item = knowledge.rubric_for_topic(make_profile(), REAL_TOPIC)
        self.assertIsNotNone(item, "前提：这条考点确实在参照库里")
        self.assertEqual(item["topic"], REAL_TOPIC)
        self.assertIn("must_hit", item)

    def test_returns_none_for_unknown_topic(self):
        self.assertIsNone(
            knowledge.rubric_for_topic(make_profile(), "一个根本不存在的考点")
        )

    def test_returns_none_for_blank_topic(self):
        for blank in (None, "", "   ", "\n"):
            with self.subTest(topic=blank):
                self.assertIsNone(knowledge.rubric_for_topic(make_profile(), blank))

    def test_returns_none_for_unknown_major(self):
        # 检索键含专业，换专业必须取不到 —— 否则会把别的专业的要点喂给面试官
        profile = make_profile(major="不存在的专业")
        self.assertIsNone(knowledge.rubric_for_topic(profile, REAL_TOPIC))

    def test_topic_is_stripped(self):
        # 考点带空白也要能命中（去重与参照走的是同一份原文）
        item = knowledge.rubric_for_topic(make_profile(), f"  {REAL_TOPIC}  ")
        self.assertIsNotNone(item)

    def test_tolerates_missing_rubrics_file(self):
        # 参照库缺失时静默返回 None，绝不抛异常（与 load_rubrics 同款降级）
        with mock.patch.object(knowledge, "load_rubrics", lambda: {}):
            self.assertIsNone(knowledge.rubric_for_topic(make_profile(), REAL_TOPIC))


class TestRubricHintBlock(unittest.TestCase):
    """给面试官的单条参照提示。"""

    def setUp(self):
        self.profile = make_profile()
        self.block = knowledge.rubric_hint_block(self.profile, REAL_TOPIC)

    def test_empty_when_no_rubric(self):
        self.assertEqual(knowledge.rubric_hint_block(self.profile, "不存在"), "")

    def test_empty_for_blank_topic(self):
        self.assertEqual(knowledge.rubric_hint_block(self.profile, ""), "")

    def test_mentions_the_topic(self):
        self.assertIn(REAL_TOPIC, self.block)

    def test_carries_must_hit_points(self):
        item = knowledge.rubric_for_topic(self.profile, REAL_TOPIC)
        self.assertIn("必中要点", self.block)
        for point in item.get("must_hit") or []:
            self.assertIn(str(point), self.block, "必中要点必须逐条带上")

    def test_carries_common_errors_as_traps(self):
        item = knowledge.rubric_for_topic(self.profile, REAL_TOPIC)
        errors = item.get("common_errors") or []
        self.assertTrue(errors, "前提：这条考点有常见误区")
        self.assertIn("常见误区", self.block)
        # 关键能力增量：误区要转成"可以设的陷阱"，而不是只列出来
        self.assertIn("陷阱", self.block)
        for err in errors:
            self.assertIn(str(err), self.block)

    def test_bonus_is_optional_and_labelled(self):
        item = knowledge.rubric_for_topic(self.profile, REAL_TOPIC)
        if item.get("bonus"):
            self.assertIn("加分项", self.block)

    def test_forbids_reading_the_checklist_aloud(self):
        # 这是本功能最大的风险：模型拿着要点清单去念，或拿来对答案
        self.assertIn("严禁", self.block)
        self.assertIn("念", self.block)
        self.assertIn("标准答案", self.block)

    def test_allows_answers_outside_the_checklist(self):
        # 与评估官同一条原则：参照是底线不是天花板
        self.assertIn("清单之外", self.block)

    def test_directs_probing_at_what_is_missing(self):
        # 提示要指向"他没讲到的那一条"，而不是重复他已说过的
        self.assertIn("没讲到", self.block)

    def test_single_topic_only(self):
        # 逐轮注入，只该带当前这一条；带全场会稀释注意力
        profile = make_profile(
            topics=[REAL_TOPIC, "动态规划状态设计与最优子结构推导"]
        )
        block = knowledge.rubric_hint_block(profile, REAL_TOPIC)
        self.assertNotIn("动态规划状态设计与最优子结构推导", block)


class TestCurrentTopic(unittest.TestCase):
    """当前考点 = asked_topics 的最后一个（ask 落新题，probe/reflect 沿用）。"""

    def test_picks_last(self):
        self.assertEqual(
            agents._current_topic(["A", "B", "C"]), "C"
        )

    def test_empty_list(self):
        self.assertEqual(agents._current_topic([]), "")

    def test_none(self):
        self.assertEqual(agents._current_topic(None), "")

    def test_skips_blank_tail(self):
        # 模型偶尔把 topic 填成空白；不能因此丢掉"当前考点"
        self.assertEqual(agents._current_topic(["A", "  ", ""]), "A")

    def test_all_blank(self):
        self.assertEqual(agents._current_topic(["", "   "]), "")


class TestInjectionIntoInterviewerPrompt(unittest.TestCase):
    """端到端：参照要真的进到面试官的系统提示词里。"""

    def _system(self, profile, asked_topics):
        return agents._build_interviewer_system(
            profile, cur_q=1, probes_used=0, reflects_in_a_row=0,
            mood_state={}, asked_topics=asked_topics,
        )

    def test_hint_injected_for_current_topic(self):
        text = self._system(make_profile(), [REAL_TOPIC])
        self.assertIn("你正在问的考点", text)
        self.assertIn(REAL_TOPIC, text)

    def test_absent_when_topic_has_no_rubric(self):
        text = self._system(make_profile(), ["一个没有参照的自拟考点"])
        self.assertNotIn("你正在问的考点", text)

    def test_absent_on_opening_turn(self):
        # 开场还没有"当前考点"，不该硬塞
        text = self._system(make_profile(), [])
        self.assertNotIn("你正在问的考点", text)

    def test_still_builds_without_any_rubrics(self):
        # 参照库整个缺失时提示词仍要能正常拼出来（静默降级，面试不中断）
        with mock.patch.object(knowledge, "load_rubrics", lambda: {}):
            text = self._system(make_profile(), [REAL_TOPIC])
        self.assertIn("面试官", text)
        self.assertNotIn("你正在问的考点", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
