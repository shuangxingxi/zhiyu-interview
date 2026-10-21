"""难度档「权重分布」的离线单元测试。

被测对象：`knowledge.LEVEL_TOPIC_MIX` / `topic_mix_quotas` / `mixed_topic_pool` /
`reference_for` / `topic_mix_block`，以及真题池的按档抽样。

钉住的核心主张（这是本模块存在的理由）：
    **难度档调的是三档题的权重，不是整场只问同一档。**
A 档里必须能出现中等题、C 档里必须能出现基础题；C 档不出高难题。
不钉住这一点，代码退化成「按档取池」也不会被任何测试发现。

运行方式（项目根目录，零新增依赖）：
    py -3.12 -m unittest test_topic_mix -v
"""

from __future__ import annotations

import random
import unittest
from collections import Counter

import knowledge as k

CS = "计算机科学与技术"


def _profile(level: str, total_rounds: int = 5, major: str = CS) -> dict:
    return k.build_profile(
        "应届生校招", major, level, "技术面", total_rounds, use_real=False
    )


class TestQuotas(unittest.TestCase):
    def test_quotas_always_sum_to_total(self):
        for level in k.LEVELS:
            for total in range(3, 11):
                quotas = k.topic_mix_quotas(level, total)
                self.assertEqual(sum(quotas.values()), total, f"{level}/{total}")
                self.assertEqual(set(quotas), set(k.LEVELS))

    def test_a_level_is_high_heavy_but_mixed(self):
        quotas = k.topic_mix_quotas("A", 5)
        self.assertGreater(quotas["A"], quotas["B"], "A 档应以高难题为主")
        self.assertGreater(quotas["B"], 0, "A 档必须掺中等题，不能整场都是高难题")
        self.assertGreaterEqual(quotas["C"], 0)

    def test_c_level_is_basic_heavy_and_has_no_high_tier(self):
        for total in (5, 8, 10):
            quotas = k.topic_mix_quotas("C", total)
            self.assertEqual(quotas["A"], 0, "C 档不出高难题")
            self.assertGreater(quotas["C"], quotas["B"], "C 档应以基础题为主")
            self.assertGreater(quotas["B"], 0, "C 档必须掺中等题")

    def test_b_level_is_balanced_in_the_middle(self):
        quotas = k.topic_mix_quotas("B", 5)
        self.assertEqual(max(quotas, key=quotas.get), "B")
        self.assertGreater(quotas["A"], 0)
        self.assertGreater(quotas["C"], 0)

    def test_unknown_level_falls_back_to_b(self):
        self.assertEqual(k.topic_mix_quotas("Z", 5), k.topic_mix_quotas("B", 5))

    def test_quotas_are_deterministic(self):
        self.assertEqual(k.topic_mix_quotas("A", 7), k.topic_mix_quotas("A", 7))


class TestMixedPool(unittest.TestCase):
    def test_pool_is_mixed_not_single_tier(self):
        """A 档池子里必须同时有三档——这是用户提的那条要求的直接断言。"""
        for level in k.LEVELS:
            profile = _profile(level)
            tiers = Counter(profile["topic_tiers"].values())
            self.assertGreaterEqual(
                len(tiers), 2, f"{level} 档的考点池只有一档：{dict(tiers)}"
            )

    def test_pool_composition_follows_the_mix(self):
        for _ in range(5):
            profile = _profile("A")
            tiers = Counter(profile["topic_tiers"].values())
            self.assertGreater(tiers["A"], tiers["B"], "A 档池应以高难题为主")
        for _ in range(5):
            profile = _profile("C")
            tiers = Counter(profile["topic_tiers"].values())
            self.assertEqual(tiers["A"], 0, "C 档池里不该有高难题")
            self.assertGreater(tiers["C"], tiers["B"])

    def test_pool_size_and_uniqueness(self):
        for total in (3, 5, 10):
            profile = _profile("B", total)
            self.assertEqual(len(profile["topics"]), k.topic_pool_size(total))
            self.assertEqual(len(set(profile["topics"])), len(profile["topics"]))
            self.assertGreaterEqual(len(profile["topics"]), k.MIN_TOPIC_POOL)

    def test_pool_topics_all_exist_in_the_library(self):
        profile = _profile("B")
        known = {
            topic
            for tier_topics in k.MAJOR_MODEL[CS]["topics"].values()
            for topic in tier_topics
        }
        for topic in profile["topics"]:
            self.assertIn(topic, known)

    def test_every_tier_label_matches_the_library(self):
        """topic_tiers 不能瞎标：标 A 的必须真在 A 档列表里。"""
        profile = _profile("B")
        for topic, tier in profile["topic_tiers"].items():
            self.assertIn(topic, k.MAJOR_MODEL[CS]["topics"][tier])
            self.assertEqual(k.topic_tier(CS, topic), tier)

    def test_behavior_interview_has_no_mix(self):
        profile = k.build_profile(
            "应届生校招", CS, "A", "行为面", 5, use_real=False
        )
        self.assertEqual(profile["topic_mix"], {})
        self.assertEqual(profile["topic_tiers"], {})
        self.assertEqual(k.topic_mix_block(profile), "")


class TestPromptRendering(unittest.TestCase):
    def test_reference_lines_carry_tier_labels(self):
        profile = _profile("A")
        text = k.reference_for(profile)
        for label in k.TIER_LABELS.values():
            self.assertIn(f"（{label}）", text)
        # 考点原文必须逐字保留：模型回写 topic 时要靠它查 rubric
        for topic in profile["topics"]:
            self.assertIn(topic, text)

    def test_mix_block_states_the_quotas_and_forbids_mentioning_them(self):
        profile = _profile("A", 5)
        block = k.topic_mix_block(profile)
        self.assertIn("高难 3 道", block)
        self.assertIn("中等 2 道", block)
        self.assertIn("不要说", block)

    def test_reference_without_tiers_is_a_plain_list(self):
        """JD 定向场没有档位信息，不能凭空编档位。"""
        profile = {"topics": ["甲考点", "乙考点"]}
        self.assertEqual(k.reference_for(profile), "- 甲考点\n- 乙考点")
        self.assertEqual(k.topic_mix_block(profile), "")


class TestRealQuestionSampling(unittest.TestCase):
    def test_builtin_pool_is_sampled_by_the_mix(self):
        """自建真题带 level：C 档场不该抽出高难题。"""
        for _ in range(10):
            profile = k.build_profile(
                "应届生校招", CS, "C", "技术面", 5, real_quota=3
            )
            levels = {str(item.get("level", "")) for item in profile["real_questions"]}
            self.assertNotIn("A", levels, f"C 档抽到了高难题：{levels}")

    def test_sampling_respects_the_quota(self):
        for quota in (0, 1, 2, 3):
            profile = k.build_profile(
                "应届生校招", CS, "B", "技术面", 5, real_quota=quota
            )
            self.assertLessEqual(len(profile["real_questions"]), quota)


if __name__ == "__main__":
    unittest.main()
