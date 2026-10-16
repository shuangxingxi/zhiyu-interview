"""评估官「只按本场问到的考点」注入参照的单元测试。

被测对象：`knowledge.rubric_for` / `knowledge.rubric_block` 的
`asked_topics` 过滤，以及 `agents.evaluate_interview` 是否真的把它传下去。

**这是在修一个实测发现的真实缺陷**：`rubric_for` 原先遍历 `profile["topics"]`
（该难度档的**全部**考点，A 档 17 条 / B、C 档各 15 条），而 `evaluate_interview`
**根本没有接收** `asked_topics`，所以无从知道本场问过哪些。实测后果：

    本场真正问到的参照考点：1 个
    rubric_check 条目：17

评估官把 16 个**没问到**的考点也判了，其中「红黑树」被判「遗漏 1 条」——
而这个考点本场压根没问。`_rubric_missed_block` 再把 missed 喂给教练，
于是教练建议考生去补一个从没被问到的考点，与 DESIGN
"本场没被问到的考点不计入遗漏"直接矛盾。

测试原则：用真件（真 rubrics.json、真 profile），只把模型那层替换掉。

运行方式（项目根目录，零新增依赖）：
    py -3.12 -m unittest test_evaluator_rubric -v
"""

from __future__ import annotations

import unittest
from unittest import mock

import agents
import knowledge

MAJOR = "计算机科学与技术"


def make_profile(level: str = "A", **overrides) -> dict:
    profile = knowledge.build_profile(
        "应届生校招", MAJOR, level, "技术面", 5, use_real=False, real_quota=0
    )
    profile["mood_init"] = {"patience": 2, "energy": 2, "pressure": 0}
    profile.update(overrides)
    return profile


class TestRubricForFiltering(unittest.TestCase):
    """按 asked_topics 过滤，而不是返回全量考点。"""

    def test_without_asked_topics_returns_whole_pool(self):
        # 保留旧行为：不掌握逐轮信息的调用方仍可用（并明确记录为"退化行为"）
        profile = make_profile("B")
        items = knowledge.rubric_for(profile)
        self.assertEqual(len(items), len(profile["topics"]))
        self.assertGreater(len(items), 7, "前提：B 档考点数远多于 7 条")

    def test_only_asked_topics_are_returned(self):
        profile = make_profile("A")
        asked = [profile["topics"][0]]
        items = knowledge.rubric_for(profile, asked)
        self.assertEqual(len(items), 1, "只问了一条，就该只注入一条")
        self.assertEqual(items[0]["topic"], asked[0])

    def test_fewer_than_the_whole_pool(self):
        # 这是本缺陷的核心断言：注入条数必须等于"问到的条数"，不是考点池大小
        profile = make_profile("A")
        asked = profile["topics"][:3]
        items = knowledge.rubric_for(profile, asked)
        self.assertEqual(len(items), 3)
        self.assertLess(len(items), len(profile["topics"]))

    def test_empty_asked_topics_returns_nothing(self):
        # 一场没记录到任何考点 → 宁可让评估官退回通用标准，
        # 也不要凭空塞给它一批没问过的参照
        profile = make_profile("A")
        self.assertEqual(knowledge.rubric_for(profile, []), [])

    def test_unknown_topic_in_asked_is_skipped(self):
        # 模型自拟的考点（不在 270 条内）会被安静跳过，不影响其它条目
        profile = make_profile("A")
        asked = [profile["topics"][0], "模型自己编的考点"]
        items = knowledge.rubric_for(profile, asked)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["topic"], profile["topics"][0])

    def test_duplicates_are_collapsed(self):
        profile = make_profile("A")
        t = profile["topics"][0]
        items = knowledge.rubric_for(profile, [t, t, f"  {t}  "])
        self.assertEqual(len(items), 1, "同一考点重复出现只算一条")

    def test_blank_entries_are_ignored(self):
        profile = make_profile("A")
        asked = ["", "   ", None, profile["topics"][1]]
        items = knowledge.rubric_for(profile, asked)
        self.assertEqual(len(items), 1)

    def test_does_not_mutate_input(self):
        profile = make_profile("A")
        asked = [profile["topics"][0]]
        snapshot = list(asked)
        knowledge.rubric_for(profile, asked)
        self.assertEqual(asked, snapshot)

    def test_tolerates_missing_rubrics(self):
        with mock.patch.object(knowledge, "load_rubrics", lambda: {}):
            self.assertEqual(knowledge.rubric_for(make_profile(), ["任意"]), [])


class TestRubricBlockFiltering(unittest.TestCase):
    """注入评估官的段落也必须只含问到的考点。"""

    def test_block_only_mentions_asked_topics(self):
        profile = make_profile("A")
        asked = [profile["topics"][0], profile["topics"][1]]
        block = knowledge.rubric_block(profile, asked)

        self.assertIn(asked[0], block)
        self.assertIn(asked[1], block)
        # 没问到的考点一个字都不该出现
        for topic in profile["topics"][2:]:
            self.assertNotIn(topic, block, f"没问到的考点不该出现：{topic}")

    def test_block_falls_back_when_nothing_asked(self):
        block = knowledge.rubric_block(make_profile("A"), [])
        self.assertIn("未提供考点参照", block)

    def test_block_is_much_shorter_than_whole_pool(self):
        profile = make_profile("A")
        full = knowledge.rubric_block(profile)
        one = knowledge.rubric_block(profile, [profile["topics"][0]])
        self.assertLess(len(one), len(full) / 2, "只问一条时提示词应显著变短")


class TestEvaluateInterviewPassesAskedTopics(unittest.TestCase):
    """端到端：evaluate_interview 必须把 asked_topics 传到 rubric_block。"""

    def _capture_system(self, profile, asked_topics):
        captured: dict[str, str] = {}

        def fake_chat_json(messages, api_key, model=None, **kwargs):
            captured["system"] = messages[0]["content"]
            return {"overall_score": 60, "dimensions": []}

        with mock.patch.object(agents, "chat_json", fake_chat_json):
            agents.evaluate_interview(
                [{"role": "user", "content": "我做过推荐系统"}],
                profile,
                "fake-key",
                asked_topics=asked_topics,
            )
        return captured["system"]

    def test_only_asked_topics_reach_the_evaluator(self):
        profile = make_profile("A")
        asked = [profile["topics"][0]]
        system = self._capture_system(profile, asked)

        self.assertIn(asked[0], system)
        for topic in profile["topics"][1:]:
            self.assertNotIn(
                topic, system,
                f"没问到的考点不该进评估官提示词：{topic}",
            )

    def test_backward_compatible_without_asked_topics(self):
        # 不传时退回旧行为（全量），保证既有调用方不炸
        profile = make_profile("A")
        system = self._capture_system(profile, None)
        self.assertIn(profile["topics"][0], system)

    def test_signature_accepts_asked_topics(self):
        import inspect

        params = inspect.signature(agents.evaluate_interview).parameters
        self.assertIn(
            "asked_topics", params,
            "evaluate_interview 必须接收 asked_topics，否则缺陷会复现",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
