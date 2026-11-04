"""报告文本层单测 —— 只有拆出 report_text.py 之后才写得出来的那一批。

这批用例要钉住的是「导出件的结构契约」，而不是措辞：

1. 章节编号必须连续。报告各节是有开关的（没有教练复盘、没有决策轨迹都可能
   整节不出现），早先写死 `## 二、教练复盘` 就会出现「一、三、五」跳号，
   打印出来像缺页。现在按实际出现顺序发号，这里逐个数。
2. 会缺的字段不能变成 None/KeyError。评估官少给一个 dimensions、教练整段
   失败返回 {}，导出都必须照常出得来——报告是面试结束后唯一的产物，
   这时候崩掉等于整场白做。
3. 表格里的竖线要转义。候选人回答里出现 `|` 会让 Markdown 表格错列，
   所以单元格统一把 `|` 换成全角 `／`。
4. 口径要和页面一致：没有判定时如实写「本场未能生成考点级判定」，
   而不是让读者以为报告本来就没有这一节。

不联网、不调模型、不碰 history.json（结局记录由参数传入）。
"""

from __future__ import annotations

import unittest
from datetime import datetime

import knowledge
import report_text

# 用考点库里的真实考点，这样 aggregate_rubric_check 会走「查库命中」分支
# （source == "library"），而不是现场现拟那一支。
TOPIC_A = "TCP 拥塞控制算法的演进与取舍"  # 计算机科学与技术 · A 档
STAMP = datetime(2026, 1, 2, 3, 4)


def make_profile(**extra) -> dict:
    profile = {
        "major": "计算机科学与技术",
        "scene": "应届生校招",
        "level": "B",
        "level_label": "中型互联网 / 上市公司",
        "interview_type": "技术面",
        "total_rounds": 5,
        "mood_mode": "怀疑型",
    }
    profile.update(extra)
    return profile


def section_numbers(markdown: str) -> list[int]:
    """把 `## 三、xxx` 这类二级标题的编号取出来，转成 1/2/3 便于比较。

    只认中文数字一到十；认不出来就跳过（比如 `## 二、` 之外的自定义标题）。
    """
    numerals = report_text._NUMERALS  # noqa: SLF001 —— 测试与实现共用一个序号表
    found: list[int] = []
    for line in markdown.split("\n"):
        if not line.startswith("## "):
            continue
        head = line[3:].split("、")[0].strip()
        if head in numerals:
            found.append(numerals.index(head) + 1)
    return found


class TestPureHelpers(unittest.TestCase):
    """rate_text / tier_distribution_caption / answer_outline_lines。"""

    def test_rate_text_counts_both_sides(self):
        self.assertEqual(report_text.rate_text(["a", "b", "c"], ["d", "e"]), "3/5（60%）")

    def test_rate_text_without_comparable_points_is_dash(self):
        # 没问到 → 不能显示成「0/1（0%）」，那会把"没考"画成"全错"
        self.assertEqual(report_text.rate_text([], []), "—")
        self.assertEqual(report_text.rate_text(None, None), "—")

    def test_rate_text_ignores_blank_entries(self):
        self.assertEqual(report_text.rate_text(["a", "  ", ""], ["b"]), "1/2（50%）")

    def test_tier_caption_lists_actually_asked_topics(self):
        line = report_text.tier_distribution_caption(
            make_profile(), [{"topic": TOPIC_A}]
        )
        self.assertIn("本场实际档位分布", line)
        self.assertIn("高难 1 道", line)

    def test_tier_caption_is_silent_when_no_tier_known(self):
        # 查不到档位就不输出，不编一个「未知 N 道」出来凑数
        line = report_text.tier_distribution_caption(
            make_profile(), [{"topic": "这个考点并不在档位表里"}]
        )
        self.assertEqual(line, "")

    def test_tier_caption_needs_at_least_one_asked_topic(self):
        self.assertEqual(report_text.tier_distribution_caption(make_profile(), []), "")

    def test_answer_outline_returns_empty_for_unknown_topic(self):
        self.assertEqual(report_text.answer_outline_lines([{"topic": "不存在的考点"}], "计算机科学与技术"), [])

    def test_answer_outline_lists_steps_for_known_topic(self):
        lines = report_text.answer_outline_lines(
            [{"topic": "图的存储结构与遍历方式"}], "计算机科学与技术"
        )
        self.assertTrue(lines, "库里有的考点应当给得出框架")
        self.assertEqual(lines[0], "### 这类题该怎么答")
        self.assertTrue(any(line.startswith("1. ") for line in lines))


class TestReportMarkdownStructure(unittest.TestCase):
    """导出的第一件事是「结构正确」，措辞可以改，结构不能塌。"""

    def test_head_lines_carry_config_and_fixed_timestamp(self):
        md = report_text.report_markdown(
            profile=make_profile(), evaluation={"overall_score": 80}, now=STAMP
        )
        self.assertTrue(md.startswith("# 模拟面试复盘报告"))
        self.assertIn("- 生成时间：2026-01-02 03:04", md)
        self.assertIn("- 面试配置：应届生校招 · 计算机科学与技术 · B 级", md)
        self.assertIn("- 主问题数量：5", md)
        self.assertIn("- 面试官情绪模式：怀疑型", md)

    def test_section_numbers_stay_consecutive_when_blocks_are_missing(self):
        # 只有评分 + 文字稿：教练、建议、决策、情绪轨迹全都没有。
        # 早先的写法会得到「一、六」，现在必须是「一、二」。
        md = report_text.report_markdown(
            profile=make_profile(),
            evaluation={"overall_score": 80},
            history=[{"role": "assistant", "content": "开始吧"}],
            now=STAMP,
        )
        self.assertEqual(section_numbers(md), [1, 2])

    def test_all_six_sections_are_numbered_in_order(self):
        md = report_text.report_markdown(
            profile=make_profile(),
            evaluation={"overall_score": 80},
            coach={"encouragement": "继续练"},
            advice={"advice": [{"dimension": "深度", "gap": "缺例子"}]},
            decisions=[{"主问题": "Q1", "动作": "换新题"}],
            trace=[{"round": 1, "mood": "耐心", "patience": 80, "energy": 70, "pressure": 20}],
            history=[{"role": "user", "content": "我的回答"}],
            now=STAMP,
        )
        self.assertEqual(section_numbers(md), [1, 2, 3, 4, 5, 6])

    def test_empty_result_blocks_do_not_crash(self):
        # 评估官/教练/顾问整段失败返回 {}：只有配置与页脚，仍须出得来。
        # （面试配置本身一定存在，所以 profile 必须给；缺的是「结果」那些块。）
        md = report_text.report_markdown(profile=make_profile(), now=STAMP)
        self.assertIn("# 模拟面试复盘报告", md)
        self.assertIn("> 本报告由「知遇」AI 虚拟面试智能体自动生成。", md)
        self.assertEqual(section_numbers(md), [])

    def test_pipe_in_cell_is_escaped(self):
        md = report_text.report_markdown(
            profile=make_profile(),
            evaluation={
                "overall_score": 80,
                "dimensions": [{"name": "深度", "score": 7, "comment": "讲了 A|B 两条"}],
            },
            now=STAMP,
        )
        self.assertIn("讲了 A／B 两条", md)
        # 表格行里不能再出现裸竖线，否则会多出一列
        row = [ln for ln in md.split("\n") if "讲了 A" in ln][0]
        self.assertEqual(row.count("|"), 4)  # | 维度 | 得分 | 评语 |

    def test_usage_line_only_when_calls_happened(self):
        with_usage = report_text.report_markdown(
            profile=make_profile(),
            evaluation={"overall_score": 80},
            usage={"calls": 7, "prompt_tokens": 1200, "completion_tokens": 300},
            now=STAMP,
        )
        self.assertIn("本场共调用模型 7 次", with_usage)
        self.assertIn("输入 1200 tokens", with_usage)

        without = report_text.report_markdown(
            profile=make_profile(), evaluation={"overall_score": 80}, now=STAMP
        )
        self.assertNotIn("本场共调用模型", without)

    def test_outcome_record_is_written_when_backfilled(self):
        md = report_text.report_markdown(
            profile=make_profile(),
            evaluation={"overall_score": 80},
            outcome_record={"outcome": {"value": "拿到 offer", "note": "口头 offer"}},
            now=STAMP,
        )
        self.assertIn("- 真实结局（本人回填）：拿到 offer", md)
        self.assertIn("- 结局备注：口头 offer", md)

    def test_unbackfilled_outcome_adds_no_line(self):
        md = report_text.report_markdown(
            profile=make_profile(),
            evaluation={"overall_score": 80},
            outcome_record={"outcome": {"value": ""}},
            now=STAMP,
        )
        self.assertNotIn("真实结局", md)

    def test_transcript_labels_speakers(self):
        md = report_text.report_markdown(
            profile=make_profile(),
            evaluation={"overall_score": 80},
            history=[
                {"role": "assistant", "content": "请介绍项目"},
                {"role": "user", "content": "我做了个系统"},
            ],
            now=STAMP,
        )
        self.assertIn("**面试官**：请介绍项目", md)
        self.assertIn("**候选人**：我做了个系统", md)


class TestReportMarkdownRubric(unittest.TestCase):
    """考点判定这一段是报告里最容易被追问「依据哪来的」的部分。"""

    def _evaluation(self, **extra) -> dict:
        evaluation = {
            "overall_score": 78,
            "adjusted_score": 82,
            "level": "良好",
            "rubric_check": [
                {
                    "topic": TOPIC_A,
                    "must_hit": ["慢启动", "拥塞避免", "快重传"],
                    "missed": ["快恢复的触发条件", "与 BBR 的取舍"],
                    "errors": [],
                    "comment": "讲清了增长阶段",
                }
            ],
        }
        evaluation.update(extra)
        return evaluation

    def test_topic_verdict_table_and_hit_rate(self):
        md = report_text.report_markdown(
            profile=make_profile(), evaluation=self._evaluation(), now=STAMP
        )
        self.assertIn("### 考点命中情况", md)
        self.assertIn("考点库", md)  # 查库命中，不能标成「现拟」
        self.assertIn("3/5（60%）", md)
        self.assertIn("**要点命中率**：3 / 5（60%）", md)
        self.assertIn("**考点级判定**：共 1 个考点", md)
        # 部分通过 → 进薄弱项，并配上答题框架
        self.assertIn("部分通过", md)
        self.assertIn("本场实际档位分布", md)

    def test_missing_rubric_check_is_stated_not_silently_dropped(self):
        md = report_text.report_markdown(
            profile=make_profile(),
            evaluation={"overall_score": 78},
            asked_topics=["图论", "数据库"],
            now=STAMP,
        )
        self.assertIn("### 考点级判定", md)
        self.assertIn("本场未能生成考点级判定", md)
        self.assertIn("问到了 2 个考点（图论、数据库）", md)

    def test_jd_session_outro_lists_remaining_topics(self):
        md = report_text.report_markdown(
            profile=make_profile(jd_topics=[TOPIC_A]),
            evaluation=self._evaluation(),
            now=STAMP,
        )
        self.assertIn("### JD 定向场收尾", md)
        # 只答到 3/5 → 这一条没过，应收进「还剩」
        self.assertIn("还剩 1 条没过或没考到", md)

    def test_jd_outro_says_all_passed_when_nothing_remains(self):
        md = report_text.report_markdown(
            profile=make_profile(jd_topics=[TOPIC_A]),
            evaluation={
                "overall_score": 90,
                "rubric_check": [
                    {"topic": TOPIC_A, "must_hit": ["慢启动", "拥塞避免"], "missed": []}
                ],
            },
            now=STAMP,
        )
        self.assertIn("本场目标考点都通过了。", md)

    def test_delivery_block_appears_with_timed_answers(self):
        md = report_text.report_markdown(
            profile=make_profile(),
            evaluation={"overall_score": 78},
            history=[{"role": "user", "content": "我做了缓存优化，把命中率提到九成。", "elapsed": 12.0}],
            now=STAMP,
        )
        self.assertIn("### 表达节奏", md)
        self.assertIn("不经过模型", md)


class TestResumeAdviceMarkdown(unittest.TestCase):
    """简历建议导出——同样是用户会拿走的东西，同样此前没人测。"""

    def _result(self) -> dict:
        return {
            "score": 72,
            "verdict": "结构完整，量化不足。",
            "problems": [
                {
                    "category": "经历",
                    "severity": "高",
                    "problem": "只写了做了什么，没写结果",
                    "evidence": "负责支付模块开发",
                    "advice": "补上金额与笔数",
                }
            ],
            "rewrites": [
                {"original": "负责支付模块开发", "improved": "重构支付模块，日均 30 万笔", "why": "补结果"}
            ],
            "missing": ["实习时长"],
            "keywords": ["MySQL"],
            "checklist": ["检查联系方式"],
            "identity": [{"signal": "手机号", "quote": "138****", "advice": "脱敏"}],
            "formatting": {"issues": ["字号不统一"]},
            "quantification": {"lines": 10, "quantified": 4, "ratio": 0.4},
            "dropped_rewrites": 2,
            "invented_numbers": 1,
            "ungrounded_evidence": 1,
        }

    def test_full_advice_has_six_consecutive_sections(self):
        md = report_text.resume_advice_markdown(
            result=self._result(), file_name="张三.pdf", target_role="后端开发", now=STAMP
        )
        self.assertTrue(md.startswith("# 简历优化建议"))
        self.assertIn("- 简历文件：张三.pdf", md)
        self.assertIn("- 目标岗位：后端开发", md)
        self.assertEqual(section_numbers(md), [1, 2, 3, 4, 5, 6])

    def test_partial_advice_does_not_skip_numbers(self):
        md = report_text.resume_advice_markdown(
            result={"problems": [{"category": "格式", "severity": "低", "problem": "太挤", "advice": "空一行"}]},
            now=STAMP,
        )
        self.assertEqual(section_numbers(md), [1])

    def test_local_statistics_and_dropped_counts_are_reported(self):
        md = report_text.resume_advice_markdown(result=self._result(), now=STAMP)
        self.assertIn("10 行里 4 行含量化结果（40%）", md)
        self.assertIn("2 条改写（其中 1 条补了原文没有的数字）、1 条诊断引用", md)

    def test_unspecified_target_role_is_marked(self):
        md = report_text.resume_advice_markdown(result={}, now=STAMP)
        self.assertIn("- 目标岗位：未指定", md)
        self.assertIn("- 简历文件：—", md)


class TestNoStreamlitDependency(unittest.TestCase):
    """这一层必须能在没有 Streamlit 运行时的环境里跑——拆分的全部意义所在。"""

    def test_report_text_does_not_import_streamlit(self):
        import subprocess
        import sys

        code = "import sys, report_text; print('streamlit' in sys.modules)"
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        self.assertIn("False", out.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
