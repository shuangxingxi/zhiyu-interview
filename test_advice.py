"""提升建议（`advice.py` + `agents.advisor_review`）的离线单元测试。

被测对象：维度规整与排序、短板识别、以及最要紧的那条——**模型不能自己发明维度**。

测试原则（与既有 8 套一致）：
    1. **不联网**：`advisor_review` 里的 `chat_json` 换成假模型，不花 token；
    2. **用真件**：真 `knowledge.build_profile`（真维度名）、真评估结果结构；
    3. **断言行为而不是实现**：重点钉住四件事——
       维度名不在本场就丢弃、每维度至多一条、没给建议的维度补占位而不是静默消失、
       以及建议官确实**不接收** mood_trace（它与教练的分工不允许重叠）。

运行方式（项目根目录，零新增依赖）：
    py -3.12 -m unittest test_advice -v
"""

from __future__ import annotations

import inspect
import unittest
from unittest import mock

import advice
import agents
import knowledge


def make_evaluation(**overrides) -> dict:
    evaluation = {
        "overall_score": 62,
        "adjusted_score": 70,
        "level": "待定",
        "attribution": "第 3 轮面试官追问偏紧，候选人有两次推翻了自己原本正确的结论。",
        "dimensions": [
            {"name": "编程基础与数据结构", "score": 58, "comment": "红黑树讲清了旋转，但没讲颜色约束。"},
            {"name": "系统与工程能力", "score": 66, "comment": "缓存讲了，没有量化。"},
            {"name": "项目与实习经历", "score": 81, "comment": "实习那段的职责边界讲得很清楚。"},
            {"name": "沟通表达", "score": 74, "comment": "结构清楚。"},
            {"name": "学习与潜力", "score": 77, "comment": "有反思。"},
        ],
        "highlights": ["实习经历的职责边界清楚"],
        "risks": ["量化结果缺失"],
        "summary": "基础尚可，缺量化表达。",
    }
    evaluation.update(overrides)
    return evaluation


def make_profile(**overrides) -> dict:
    profile = knowledge.build_profile(
        "应届生校招", "计算机科学与技术", "B", "技术面", 5, use_real=False, real_quota=0
    )
    profile.update(overrides)
    return profile


class FakeChat:
    """假模型：记录收到的 messages 并返回预设响应。"""

    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def __call__(self, messages, api_key, model=None, temperature=0.3, **kwargs):
        self.calls.append(
            {"messages": [dict(m) for m in messages], "temperature": temperature}
        )
        return self.response


# ---------------------------------------------------------------------------
# 维度规整 / 排序
# ---------------------------------------------------------------------------


class TestDimensionRows(unittest.TestCase):
    def test_keeps_valid_rows(self):
        rows = advice.dimension_rows(make_evaluation())
        self.assertEqual(len(rows), 5)

    def test_drops_non_dict_and_nameless(self):
        evaluation = {
            "dimensions": [
                "一个字符串而不是字典",
                {"name": "  ", "score": 50},
                {"score": 60},
                {"name": "有效维度", "score": 70},
            ]
        }
        rows = advice.dimension_rows(evaluation)
        self.assertEqual([r["name"] for r in rows], ["有效维度"])

    def test_tolerates_missing_evaluation(self):
        for value in (None, {}, {"dimensions": None}):
            with self.subTest(value=value):
                self.assertEqual(advice.dimension_rows(value), [])

    def test_score_is_clamped_and_bool_excluded(self):
        evaluation = {
            "dimensions": [
                {"name": "过低", "score": -20},
                {"name": "过高", "score": 500},
                {"name": "布尔", "score": True},
                {"name": "非数值", "score": "80"},
            ]
        }
        scores = {r["name"]: r["score"] for r in advice.dimension_rows(evaluation)}
        self.assertEqual(scores["过低"], 0)
        self.assertEqual(scores["过高"], 100)
        # True 在 Python 里也是 int，必须显式排除——与 history_store 同一条约定
        self.assertIsNone(scores["布尔"])
        self.assertIsNone(scores["非数值"])


class TestWeakestDimensions(unittest.TestCase):
    def test_lowest_first(self):
        names = advice.weakest_dimensions(make_evaluation(), limit=2)
        self.assertEqual(names, ["编程基础与数据结构", "系统与工程能力"])

    def test_dimensions_without_score_do_not_rank(self):
        evaluation = {
            "dimensions": [
                {"name": "没分", "score": None},
                {"name": "有分", "score": 90},
            ]
        }
        # 不知道分，就不该假装它最弱
        self.assertEqual(advice.weakest_dimensions(evaluation), ["有分"])

    def test_empty_when_no_scores_at_all(self):
        self.assertEqual(advice.weakest_dimensions({"dimensions": []}), [])
        self.assertEqual(advice.weakest_dimensions(None), [])


class TestAdviceBlock(unittest.TestCase):
    def setUp(self):
        self.block = advice.advice_block(make_evaluation())

    def test_lowest_dimension_appears_first(self):
        # 报告里最该先补的那条必须排在第一行
        lines = [l for l in self.block.splitlines() if l.startswith("- ")]
        self.assertIn("编程基础与数据结构", lines[0])

    def test_marks_weak_dimensions(self):
        self.assertIn("短板", self.block)

    def test_carries_comments_as_evidence(self):
        self.assertIn("红黑树讲清了旋转", self.block)

    def test_includes_highlights_and_risks(self):
        self.assertIn("实习经历的职责边界清楚", self.block)
        self.assertIn("量化结果缺失", self.block)

    def test_degrades_without_dimensions(self):
        block = advice.advice_block({})
        self.assertIn("没有维度分", block)


# ---------------------------------------------------------------------------
# 收敛：这是本模块最要紧的一组断言
# ---------------------------------------------------------------------------


class TestNormalize(unittest.TestCase):
    def setUp(self):
        self.evaluation = make_evaluation()

    def test_keeps_advice_for_real_dimensions(self):
        data = {
            "advice": [
                {
                    "dimension": "编程基础与数据结构",
                    "gap": "缺知识：红黑树颜色约束说不清",
                    "evidence": "第 2 轮答『红黑树主要靠旋转平衡』",
                    "actions": ["把红黑树五条性质做成卡片"],
                    "self_check": "能不看资料说出插入后最多旋转几次吗",
                }
            ]
        }
        result = advice.normalize(data, self.evaluation)
        kept = [a for a in result["advice"] if not a["missing"]]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["dimension"], "编程基础与数据结构")
        self.assertTrue(kept[0]["has_evidence"])

    def test_invented_dimension_is_dropped(self):
        # 模型不能自己发明维度，也不能把别的专业的维度塞进来
        data = {
            "advice": [
                {"dimension": "算法与建模能力", "gap": "缺知识：X", "actions": ["A"]},
                {"dimension": "编程基础与数据结构", "gap": "缺表达：Y", "actions": ["B"]},
            ]
        }
        result = advice.normalize(data, self.evaluation)
        names = [a["dimension"] for a in result["advice"] if not a["missing"]]
        self.assertEqual(names, ["编程基础与数据结构"])
        self.assertEqual(result["dropped"], 1)

    def test_duplicate_dimension_keeps_first_only(self):
        data = {
            "advice": [
                {"dimension": "沟通表达", "gap": "第一条", "actions": ["A"]},
                {"dimension": "沟通表达", "gap": "第二条", "actions": ["B"]},
            ]
        }
        result = advice.normalize(data, self.evaluation)
        kept = [a for a in result["advice"] if a["dimension"] == "沟通表达"][0]
        self.assertEqual(kept["gap"], "第一条")

    def test_missing_dimensions_get_placeholder_not_dropped(self):
        # 刻意不静默丢弃：维度是从评分页抄过来的，缺一条会让人以为只是漏渲染
        result = advice.normalize({"advice": []}, self.evaluation)
        self.assertEqual(len(result["advice"]), len(self.evaluation["dimensions"]))
        self.assertTrue(all(item["missing"] for item in result["advice"]))

    def test_output_order_follows_evaluation(self):
        data = {
            "advice": [
                {"dimension": "学习与潜力", "gap": "G", "actions": ["A"]},
                {"dimension": "编程基础与数据结构", "gap": "G", "actions": ["A"]},
            ]
        }
        result = advice.normalize(data, self.evaluation)
        self.assertEqual(
            [a["dimension"] for a in result["advice"]],
            [d["name"] for d in self.evaluation["dimensions"]],
        )

    def test_score_is_carried_from_evaluation_not_model(self):
        data = {
            "advice": [
                {"dimension": "沟通表达", "score": 5, "gap": "G", "actions": ["A"]}
            ]
        }
        result = advice.normalize(data, self.evaluation)
        kept = [a for a in result["advice"] if a["dimension"] == "沟通表达"][0]
        self.assertEqual(kept["score"], 74, "分数只该采信评估官那一份")

    def test_entry_without_gap_and_actions_is_dropped(self):
        result = advice.normalize(
            {"advice": [{"dimension": "沟通表达", "gap": "", "actions": []}]},
            self.evaluation,
        )
        self.assertTrue(all(item["missing"] for item in result["advice"]))

    def test_no_evidence_is_counted_not_fatal(self):
        # 「缺量化结果」这类建议本就不针对某一句原话，不该被丢弃，但要如实计数
        data = {
            "advice": [
                {"dimension": "系统与工程能力", "gap": "缺量化", "actions": ["A"]}
            ]
        }
        result = advice.normalize(data, self.evaluation)
        self.assertEqual(result["evidence_missing"], 1)

    def test_actions_are_limited(self):
        data = {
            "advice": [
                {
                    "dimension": "沟通表达",
                    "gap": "G",
                    "actions": ["A1", "A2", "A3", "A4", "A5"],
                }
            ]
        }
        result = advice.normalize(data, self.evaluation)
        kept = [a for a in result["advice"] if a["dimension"] == "沟通表达"][0]
        self.assertEqual(len(kept["actions"]), advice.MAX_ACTIONS)

    def test_priority_and_quick_wins_are_cleaned(self):
        result = advice.normalize(
            {
                "priority": ["先补量化", "  ", "先补量化", "再练表达"],
                "quick_wins": ["把『负责』换成『独立实现』"],
                "next_setup": "下一场开 B 档技术面。",
            },
            self.evaluation,
        )
        self.assertEqual(result["priority"], ["先补量化", "再练表达"])
        self.assertEqual(len(result["quick_wins"]), 1)
        self.assertIn("B 档", result["next_setup"])

    def test_tolerates_garbage_input(self):
        for value in (None, {}, {"advice": "不是列表"}, {"advice": [1, 2, None]}):
            with self.subTest(value=value):
                result = advice.normalize(value, self.evaluation)
                self.assertEqual(len(result["advice"]), len(self.evaluation["dimensions"]))

    def test_falls_back_to_model_dimensions_without_evaluation(self):
        # 评估官没给维度（降级情形）：无从校验，采信模型自己报的名字
        result = advice.normalize(
            {"advice": [{"dimension": "自定义维度", "gap": "G", "actions": ["A"]}]}, None
        )
        self.assertEqual([a["dimension"] for a in result["advice"]], ["自定义维度"])

    def test_does_not_mutate_input(self):
        data = {"advice": [{"dimension": "沟通表达", "gap": "G", "actions": ["A"]}]}
        snapshot = [dict(item) for item in data["advice"]]
        advice.normalize(data, self.evaluation)
        self.assertEqual(data["advice"], snapshot)


# ---------------------------------------------------------------------------
# 端到端：advisor_review
# ---------------------------------------------------------------------------


class TestAdvisorReview(unittest.TestCase):
    def _run(self, profile, evaluation, response):
        fake = FakeChat(response)
        with mock.patch.object(agents, "chat_json", fake):
            result = agents.advisor_review(
                [{"role": "user", "content": "我做过推荐系统"}],
                evaluation,
                profile,
                "fake-key",
            )
        return result, fake

    def test_returns_normalized_structure(self):
        profile = make_profile()
        result, _ = self._run(
            profile,
            make_evaluation(),
            {"advice": [{"dimension": profile["dimensions"][0], "gap": "G", "actions": ["A"]}]},
        )
        self.assertIn("advice", result)
        self.assertEqual(len(result["advice"]), len(profile["dimensions"]))

    def test_prompt_carries_dimension_names(self):
        profile = make_profile()
        _, fake = self._run(profile, make_evaluation(), {"advice": []})
        system = fake.calls[0]["messages"][0]["content"]
        for name in profile["dimensions"]:
            self.assertIn(name, system, f"维度名必须进提示词：{name}")

    def test_prompt_carries_evaluation_evidence(self):
        profile = make_profile()
        _, fake = self._run(profile, make_evaluation(), {"advice": []})
        system = fake.calls[0]["messages"][0]["content"]
        self.assertIn("红黑树讲清了旋转", system, "评委评语要作为依据进提示词")
        self.assertIn("量化结果缺失", system, "评委指出的风险要进提示词")

    def test_prompt_forbids_vague_advice(self):
        profile = make_profile()
        _, fake = self._run(profile, make_evaluation(), {"advice": []})
        system = fake.calls[0]["messages"][0]["content"]
        self.assertIn("多学习", system, "提示词要举出要避免的空话例子")
        self.assertIn("可交付物", system)

    def test_does_not_receive_mood_trace(self):
        # 与教练的分工不允许重叠：压力稳定性由教练的 resilience 覆盖
        params = inspect.signature(agents.advisor_review).parameters
        self.assertNotIn("mood_trace", params)

    def test_signature_accepts_history_evaluation_profile(self):
        params = inspect.signature(agents.advisor_review).parameters
        for name in ("history", "evaluation", "profile", "api_key"):
            self.assertIn(name, params)

    def test_behavior_interview_gets_star_guidance(self):
        profile = make_profile(interview_type="行为面")
        _, fake = self._run(profile, make_evaluation(), {"advice": []})
        system = fake.calls[0]["messages"][0]["content"]
        self.assertIn("Action", system)

    def test_technical_interview_gets_tech_guidance(self):
        profile = make_profile(interview_type="技术面")
        _, fake = self._run(profile, make_evaluation(), {"advice": []})
        system = fake.calls[0]["messages"][0]["content"]
        self.assertIn("原理", system)

    def test_transcript_reaches_the_model(self):
        profile = make_profile()
        _, fake = self._run(profile, make_evaluation(), {"advice": []})
        user = fake.calls[0]["messages"][-1]["content"]
        self.assertIn("我做过推荐系统", user)
        self.assertIn("候选人", user)


if __name__ == "__main__":
    unittest.main(verbosity=2)
