"""简历优化（`resume_optimize.py` + `agents.resume_review`）的离线单元测试。

被测对象：结构诊断、量化密度、以及**最关键的一条**——模型给出的改写必须对得上
简历原文，对不上就丢弃。这是把"优化简历"和"帮你编简历"区分开的那道闸。

测试原则（与既有各套一致）：
    1. **不联网**：`resume_review` 里的 `chat_json` 换成假模型；
    2. **用真件**：真简历文本（含小节标题、量化句、非量化句）；
    3. **断言行为**：重点钉住"编造的改写会被丢弃"和"对不上原文的引用会被清掉"，
       以及降级路径（缺字段 / 结构异常 / 空简历）都不抛异常。

运行方式（项目根目录，零新增依赖）：
    py -3.12 -m unittest test_resume_optimize -v
"""

from __future__ import annotations

import unittest
from unittest import mock

import agents
import resume_optimize

# 一份带真实特征的简历：有小节、有量化句、也有纯年份行（不该被当成量化）
RESUME = """张三
电话 13800000000 邮箱 zhangsan@example.com

教育背景
某某大学 计算机科学与技术 本科 2021-2025

实习经历
2024.06-2024.09 某互联网公司 后端开发实习生
负责推荐系统的离线特征回填模块，用 Spark 聚合用户行为日志，QPS 峰值 3 万。
回填耗时从 4 小时降到 40 分钟。

项目经历
基于协同过滤的图书推荐系统（毕业设计）
参与开发，用 Python + Flask 实现。

技能
Java / SpringBoot / MySQL / Redis / Git
"""


class TestSectionDetection(unittest.TestCase):
    def test_detects_real_sections(self):
        present = resume_optimize.present_sections(RESUME)
        for name in ("联系方式", "教育背景", "实习/工作经历", "项目经历", "技能"):
            self.assertIn(name, present, f"应检出：{name}")

    def test_contact_detected_by_pattern_not_only_keyword(self):
        # 只写号码不写"电话"二字也要认出来
        text = "李四\n13800000000\n无小节标题的简历"
        self.assertIn("联系方式", resume_optimize.present_sections(text))

    def test_missing_essential_sections(self):
        self.assertIn("技能", resume_optimize.missing_sections("张三\n某某大学"))

    def test_experience_missing_only_when_neither_kind_present(self):
        # 实习与项目二者有其一即可，不要求两份都写
        only_project = "项目经历\n校园二手交易平台"
        self.assertNotIn("实习/项目经历", resume_optimize.missing_sections(only_project))
        self.assertIn(
            "实习/项目经历", resume_optimize.missing_sections("教育背景\n某某大学")
        )

    def test_does_not_report_missing_for_full_resume(self):
        self.assertEqual(resume_optimize.missing_sections(RESUME), [])


class TestQuantification(unittest.TestCase):
    def test_counts_metric_lines(self):
        quant = resume_optimize.quantification(RESUME)
        self.assertGreaterEqual(quant["quantified"], 2, "QPS 与耗时都是量化成果")
        self.assertGreater(quant["lines"], quant["quantified"])

    def test_bare_years_are_not_quantified(self):
        # 「2021-2025」不含单位，不该被算成量化成果 —— 否则每份简历都满分
        only_years = "教育背景\n某某大学 2021-2025\n实习 2023.06-2023.09"
        lines = resume_optimize.quantified_lines(only_years)
        self.assertEqual(lines, [], "纯年份行不该被当成量化结果")

    def test_ratio_is_safe_for_empty_input(self):
        quant = resume_optimize.quantification("")
        self.assertEqual(quant["lines"], 0)
        self.assertEqual(quant["ratio"], 0.0)

    def test_various_units_are_recognized(self):
        text = "提升 30%\n服务 12 万人\n耗时 200ms\n扩容 3 倍"
        self.assertEqual(len(resume_optimize.quantified_lines(text)), 4)


class TestIsGrounded(unittest.TestCase):
    """改写/引用能不能在原文里找到出处。"""

    def test_exact_quote_matches(self):
        self.assertTrue(
            resume_optimize.is_grounded("参与开发，用 Python + Flask 实现。", RESUME)
        )

    def test_whitespace_insensitive(self):
        # 模型经常把跨行的句子压成一行引用
        self.assertTrue(resume_optimize.is_grounded("负责推荐系统的离线特征回填模块", RESUME))

    def test_partial_quote_containing_a_long_original_fragment(self):
        # 模型只截半句、并跨行取一段，是正常改写手法，不该被当成编造
        self.assertTrue(
            resume_optimize.is_grounded(
                "负责推荐系统的离线特征回填模块，用 Spark 聚合日志。", RESUME
            )
        )

    def test_quote_with_added_narrative_is_rejected(self):
        # 掺进一段原文里没有的叙述：公共片段被摊薄到比例以下
        self.assertFalse(
            resume_optimize.is_grounded(
                "参与开发，用 Python + Flask 实现，并主导了三人团队的性能优化专项。",
                RESUME,
            )
        )

    def test_fabricated_quote_is_rejected(self):
        fake = "主导 3 人团队完成推荐系统重构，性能提升 40%"
        self.assertFalse(resume_optimize.is_grounded(fake, RESUME))

    def test_too_short_quote_has_no_checking_value(self):
        # 「提升」「项目」太容易在别处撞上，不算对得上
        self.assertFalse(resume_optimize.is_grounded("提升", RESUME))
        self.assertFalse(resume_optimize.is_grounded("", RESUME))
        self.assertFalse(resume_optimize.is_grounded(None, RESUME))


class TestIntroducedNumbers(unittest.TestCase):
    """「不许编造」这条红线在数字上的落点。

    注意这道闸的粒度：它只核对"这个数字**有没有在简历里出现过**"，
    不核对"这个数字是不是属于这一段经历"。所以 `40` 同时出现在
    「40 分钟」里时，「提升 40%」也会被放行——这是刻意的取舍（宁可放过、
    不误杀合法的数字搬迁），报告侧不宣称它能拦住所有编数字。
    """

    def test_number_absent_from_resume_is_flagged(self):
        # 简历里没有 35 这个数字（只有 3 万 / 4 小时 / 40 分钟）
        self.assertEqual(
            resume_optimize.introduced_numbers("性能提升 35%", RESUME), ["35"]
        )

    def test_number_present_in_resume_is_allowed(self):
        # 原文出现过的数字，换个位置用是允许的
        self.assertEqual(
            resume_optimize.introduced_numbers("把回填耗时从 4 小时降到 40 分钟", RESUME), []
        )

    def test_known_limitation_same_number_different_meaning(self):
        # 如实记录这道闸的盲区：40 出现在「40 分钟」里，于是「提升 40%」被放行
        self.assertEqual(resume_optimize.introduced_numbers("性能提升 40%", RESUME), [])

    def test_empty_improved_is_safe(self):
        self.assertEqual(resume_optimize.introduced_numbers("", RESUME), [])
        self.assertEqual(resume_optimize.introduced_numbers(None, RESUME), [])

    def test_dedupes_repeated_numbers(self):
        self.assertEqual(
            resume_optimize.introduced_numbers("提升 35%，又提升 35%", RESUME), ["35"]
        )


class TestNormalizeReport(unittest.TestCase):
    """收敛报告：本功能最要紧的一组断言。"""

    def test_fabricated_rewrite_is_dropped(self):
        data = {
            "rewrites": [
                {
                    "original": "主导 3 人团队完成推荐系统重构，性能提升 40%",
                    "improved": "主导 3 人团队完成推荐系统重构，性能提升 40%。",
                    "why": "量化",
                },
                {
                    "original": "参与开发，用 Python + Flask 实现。",
                    "improved": "独立实现基于协同过滤的图书推荐系统，用 Python + Flask 完成离线评测。",
                    "why": "把『参与』换成『独立实现』，职责边界清楚",
                },
            ]
        }
        result = resume_optimize.normalize_report(data, RESUME)
        self.assertEqual(len(result["rewrites"]), 1, "编造的那条必须被丢弃")
        self.assertEqual(result["dropped_rewrites"], 1)
        self.assertIn("参与开发", result["rewrites"][0]["original"])

    def test_ungrounded_evidence_is_cleared_and_counted(self):
        data = {
            "problems": [
                {
                    "severity": "high",
                    "category": "量化",
                    "problem": "量化成果太少",
                    "evidence": "服务过十亿用户",  # 简历里没有这句
                    "advice": "补一个真实数字",
                }
            ]
        }
        result = resume_optimize.normalize_report(data, RESUME)
        # 诊断本身成立（量化不足是事实），所以保留问题、只清掉可疑引用
        self.assertEqual(len(result["problems"]), 1)
        self.assertEqual(result["problems"][0]["evidence"], "")
        self.assertFalse(result["problems"][0]["evidence_grounded"])
        self.assertEqual(result["ungrounded_evidence"], 1)

    def test_rewrite_inventing_a_number_is_dropped(self):
        # 第二道闸：原句有出处、但改写凭空补了一个原文没有的数字，同样丢弃
        data = {
            "rewrites": [
                {
                    "original": "参与开发，用 Python + Flask 实现。",
                    "improved": "独立实现基于协同过滤的图书推荐系统，离线评测指标提升 35%。",
                    "why": "量化",
                }
            ]
        }
        result = resume_optimize.normalize_report(data, RESUME)
        self.assertEqual(result["rewrites"], [], "凭空补数字的改写必须丢弃")
        self.assertEqual(result["invented_numbers"], 1)

    def test_rewrite_reusing_an_existing_number_is_kept(self):
        data = {
            "rewrites": [
                {
                    "original": "负责推荐系统的离线特征回填模块，用 Spark 聚合用户行为日志，QPS 峰值 3 万。",
                    "improved": "独立负责推荐系统离线特征回填模块，用 Spark 聚合日志，支撑 QPS 峰值 3 万。",
                    "why": "把『负责』换成『独立负责』，职责边界清楚",
                }
            ]
        }
        result = resume_optimize.normalize_report(data, RESUME)
        self.assertEqual(len(result["rewrites"]), 1, "复用原文已有数字是允许的")
        self.assertEqual(result["invented_numbers"], 0)

    def test_grounded_evidence_is_kept(self):
        data = {
            "problems": [
                {
                    "severity": "medium",
                    "category": "量化",
                    "problem": "项目那段没有结果",
                    "evidence": "参与开发，用 Python + Flask 实现。",
                    "advice": "补一个真实数字",
                }
            ]
        }
        result = resume_optimize.normalize_report(data, RESUME)
        self.assertEqual(result["problems"][0]["evidence"], "参与开发，用 Python + Flask 实现。")
        self.assertEqual(result["ungrounded_evidence"], 0)

    def test_empty_evidence_is_allowed(self):
        # 「没看到量化成果」这类诊断本来就不针对某一句
        result = resume_optimize.normalize_report(
            {"problems": [{"problem": "没看到量化成果", "evidence": "", "advice": "补数字"}]},
            RESUME,
        )
        self.assertEqual(len(result["problems"]), 1)
        self.assertTrue(result["problems"][0]["evidence_grounded"])

    def test_severity_is_normalized(self):
        data = {
            "problems": [
                {"problem": "P1", "severity": "HIGH"},
                {"problem": "P2", "severity": "紧急"},
                {"problem": "P3", "severity": "low"},
            ]
        }
        result = resume_optimize.normalize_report(data, RESUME)
        severities = [p["severity"] for p in result["problems"]]
        self.assertEqual(severities, ["high", "medium", "low"], "非法严重度退化为 medium")

    def test_problem_without_text_is_dropped(self):
        result = resume_optimize.normalize_report(
            {"problems": [{"problem": "", "advice": "x"}, {"problem": "有效问题"}]}, RESUME
        )
        self.assertEqual([p["problem"] for p in result["problems"]], ["有效问题"])

    def test_rewrite_missing_fields_is_dropped(self):
        result = resume_optimize.normalize_report(
            {"rewrites": [{"original": "参与开发，用 Python + Flask 实现。"}, {}]}, RESUME
        )
        self.assertEqual(result["rewrites"], [])
        self.assertEqual(result["dropped_rewrites"], 2)

    def test_limits_are_enforced(self):
        data = {
            "problems": [{"problem": f"问题{i}"} for i in range(20)],
            "rewrites": [
                {"original": "参与开发，用 Python + Flask 实现。", "improved": f"改进{i}"}
                for i in range(20)
            ],
            "keywords": [f"kw{i}" for i in range(30)],
        }
        result = resume_optimize.normalize_report(data, RESUME)
        self.assertLessEqual(len(result["problems"]), resume_optimize.MAX_PROBLEMS)
        self.assertLessEqual(len(result["rewrites"]), resume_optimize.MAX_REWRITES)
        self.assertLessEqual(len(result["keywords"]), resume_optimize.MAX_KEYWORDS)

    def test_score_is_clamped_and_bool_excluded(self):
        self.assertEqual(resume_optimize.normalize_report({"score": 500}, RESUME)["score"], 100)
        self.assertIsNone(resume_optimize.normalize_report({"score": True}, RESUME)["score"])

    def test_local_stats_are_computed_not_taken_from_model(self):
        # 模型谎报小节情况也不采信
        data = {"sections": {"present": ["假的"], "missing": []}}
        result = resume_optimize.normalize_report(data, RESUME)
        self.assertIn("技能", result["sections"]["present"])
        self.assertNotIn("假的", result["sections"]["present"])

    def test_tolerates_garbage_input(self):
        for value in (None, {}, {"problems": "不是列表"}, {"rewrites": [1, 2]}):
            with self.subTest(value=value):
                result = resume_optimize.normalize_report(value, RESUME)
                self.assertEqual(result["rewrites"], [])
                self.assertIn("quantification", result)

    def test_clean_list_dedupes_and_drops_blanks(self):
        result = resume_optimize.normalize_report(
            {"missing": ["补项目", "  ", "补项目", "补技能"]}, RESUME
        )
        self.assertEqual(result["missing"], ["补项目", "补技能"])


class TestResumeReview(unittest.TestCase):
    """端到端：resume_review 的降级与提示词内容。"""

    def _run(self, resume_text, response, **kwargs):
        captured = {"messages": None}

        def fake_chat(messages, api_key, model=None, temperature=0.3, **rest):
            captured["messages"] = [dict(m) for m in messages]
            return response

        with mock.patch.object(agents, "chat_json", fake_chat):
            result = agents.resume_review(resume_text, "fake-key", **kwargs)
        return result, captured

    def test_empty_resume_returns_empty_without_calling_model(self):
        calls = []

        def fake_chat(*args, **kwargs):
            calls.append(1)
            return {}

        with mock.patch.object(agents, "chat_json", fake_chat):
            result = agents.resume_review("   ", "fake-key")
        self.assertEqual(result, {})
        self.assertEqual(calls, [], "空简历不该白花一次调用")

    def test_returns_normalized_structure(self):
        result, _ = self._run(RESUME, {"verdict": "还行", "score": 55})
        self.assertEqual(result["verdict"], "还行")
        self.assertEqual(result["score"], 55)

    def test_prompt_carries_resume_and_skeleton(self):
        _, captured = self._run(RESUME, {})
        system = captured["messages"][0]["content"]
        self.assertIn("参与开发", system, "简历原文必须进提示词")
        self.assertIn("量化密度", system, "本地结构统计要进提示词")

    def test_prompt_forbids_fabrication(self):
        _, captured = self._run(RESUME, {})
        system = captured["messages"][0]["content"]
        self.assertIn("不许编造", system)
        self.assertIn("严禁新增", system)
        # 缺数字时不许替他填，要引导他自己补
        self.assertIn("而不是替他填", system)

    def test_target_role_is_optional(self):
        _, captured = self._run(RESUME, {})
        system = captured["messages"][0]["content"]
        self.assertIn("未指定目标岗位", system, "不填目标岗位也要能跑")

    def test_target_role_and_major_reach_prompt(self):
        _, captured = self._run(
            RESUME, {}, target_role="后端开发实习生", major="计算机科学与技术"
        )
        system = captured["messages"][0]["content"]
        self.assertIn("后端开发实习生", system)
        self.assertIn("计算机科学与技术", system)

    def test_multiline_resume_survives_template(self):
        # 简历里带花括号或百分号也不能让 .format 崩掉
        weird = "项目经历\n用 f-string 写 {\\'a\\': 1}，命中率 30%"
        result, _ = self._run(weird, {"verdict": "ok"})
        self.assertEqual(result["verdict"], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
