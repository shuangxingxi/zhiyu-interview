"""自建真题补位的单元测试。

设计原则（与其他套件一致）：**不联网、不花 token、零新增依赖**。
这里只测纯逻辑：池子选择、补位规则、措辞区分、陷阱题不混入主问题池。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import knowledge as k

BUILTIN = Path("real_questions_builtin.json")


class BuiltinFileTest(unittest.TestCase):
    """自建真题文件本身的完整性。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.data = json.loads(BUILTIN.read_text(encoding="utf-8"))

    def test_six_majors_covered(self) -> None:
        tech = self.data["技术面"]
        self.assertEqual(sorted(tech), sorted(k.MAJORS))

    def test_each_major_matches_rubric_count(self) -> None:
        """自建真题条数必须与考点库一一对应（新增考点后重建真题库）。"""
        for major in k.MAJORS:
            with self.subTest(major=major):
                expected = sum(len(names) for names in k.MAJOR_MODEL[major]["topics"].values())
                self.assertEqual(len(self.data["技术面"][major]), expected)

    def test_no_duplicate_questions(self) -> None:
        for major in k.MAJORS:
            with self.subTest(major=major):
                qs = [x["q"] for x in self.data["技术面"][major]]
                self.assertEqual(len(qs), len(set(qs)))

    def test_required_fields(self) -> None:
        for major in k.MAJORS:
            for item in self.data["技术面"][major]:
                for field in ("q", "topic", "cat", "src", "lic", "ref"):
                    self.assertIn(field, item, f"{major} 缺字段 {field}")

    def test_marked_as_builtin(self) -> None:
        """必须能一眼看出是自建，不能冒充外部真题。"""
        for major in k.MAJORS:
            for item in self.data["技术面"][major]:
                self.assertEqual(item["src"], "自建")
                self.assertIn("原创", item["lic"])

    def test_trap_pool_separate(self) -> None:
        """陷阱题必须单独存放，不能混进主问题池。"""
        self.assertIn("陷阱追问", self.data)
        for major in k.MAJORS:
            main = self.data["技术面"][major]
            traps = self.data["陷阱追问"][major]
            self.assertFalse(
                any(x.get("kind") == "trap" for x in main),
                f"{major} 主问题池里混入了陷阱题",
            )
            self.assertTrue(all(x.get("kind") == "trap" for x in traps))

    def test_trap_does_not_leak_answer(self) -> None:
        """陷阱题只保留误区主张，不能把「实际上……」的正确答案也写进去。"""
        for major in k.MAJORS:
            for item in self.data["陷阱追问"][major]:
                q = item["q"]
                for leak in ("实际上", "其实", "但实际", "而实际"):
                    self.assertNotIn(leak, q, f"{major} 陷阱题泄露答案：{q[:40]}")

    def test_trap_phrasing(self) -> None:
        """陷阱题要是可问出口的问句，且不出现「认为」开头的病句。"""
        for major in k.MAJORS:
            for item in self.data["陷阱追问"][major]:
                q = item["q"]
                self.assertTrue(q.endswith("？") or q.endswith("?"), q[:40])
                self.assertNotIn("有人说「认为", q, q[:40])


class FallbackTest(unittest.TestCase):
    """补位规则：外部真题优先，为空才用自建。"""

    def test_external_first(self) -> None:
        """计算机 / 软件工程有外部真题，必须优先用外部。"""
        for major in ("计算机科学与技术", "软件工程"):
            with self.subTest(major=major):
                p = k.build_profile("应届生校招", major, "B", "技术面", 5, real_quota=2)
                pool = k.real_question_source(p)
                self.assertTrue(pool, f"{major} 应有真题")
                self.assertNotEqual(pool[0]["src"], "自建")

    def test_builtin_fills_gap(self) -> None:
        """4 个原本没有外部真题的专业，必须能拿到自建题。"""
        for major in ("人工智能", "电子信息", "自动化", "通信工程"):
            with self.subTest(major=major):
                p = k.build_profile("应届生校招", major, "B", "技术面", 5, real_quota=2)
                pool = k.real_question_source(p)
                self.assertTrue(pool, f"{major} 应能拿到自建题")
                self.assertEqual(pool[0]["src"], "自建")

    def test_behavior_unchanged(self) -> None:
        """行为面仍用外部池，不受自建影响。"""
        p = k.build_profile("应届生校招", "计算机科学与技术", "B", "行为面", 5, real_quota=2)
        pool = k.real_question_source(p)
        self.assertTrue(pool)
        self.assertNotEqual(pool[0]["src"], "自建")

    def test_quota_zero_yields_nothing(self) -> None:
        p = k.build_profile("应届生校招", "电子信息", "B", "技术面", 5, real_quota=0)
        self.assertEqual(p["real_questions"], [])

    def test_sample_size_bounded(self) -> None:
        """抽样数量不能超过配额。"""
        for quota in (1, 2, 3):
            p = k.build_profile("应届生校招", "自动化", "A", "技术面", 5, real_quota=quota)
            self.assertLessEqual(len(p["real_questions"]), quota)


class WordingTest(unittest.TestCase):
    """措辞必须与实际来源一致，不能对候选人宣称不实出处。"""

    def test_builtin_wording_is_honest(self) -> None:
        p = k.build_profile("应届生校招", "电子信息", "A", "技术面", 5, real_quota=2)
        block = k.real_question_block(p)
        self.assertIn("按评分参照改写", block)
        self.assertNotIn("来自真实企业面试题库", block)
        self.assertIn("不要向候选人声称", block)

    def test_external_wording_kept(self) -> None:
        p = k.build_profile("应届生校招", "软件工程", "A", "技术面", 5, real_quota=2)
        block = k.real_question_block(p)
        self.assertIn("真实企业面试真题", block)
        self.assertNotIn("按评分参照改写", block)

    def test_empty_when_no_questions(self) -> None:
        p = k.build_profile("应届生校招", "软件工程", "A", "技术面", 5, real_quota=0)
        self.assertEqual(k.real_question_block(p), "")


class DegradeTest(unittest.TestCase):
    """文件缺失时必须静默降级，绝不抛异常。"""

    def test_missing_builtin_file(self) -> None:
        original = k.BUILTIN_Q_FILE
        try:
            k.BUILTIN_Q_FILE = Path("__no_such_file__.json")
            self.assertEqual(k.load_builtin_questions(), {})
        finally:
            k.BUILTIN_Q_FILE = original

    def test_missing_file_source_returns_list(self) -> None:
        """即使自建文件不存在，取池也不能抛异常，只能返回空。"""
        original = k.BUILTIN_Q_FILE
        try:
            k.BUILTIN_Q_FILE = Path("__no_such_file__.json")
            p = k.build_profile("应届生校招", "通信工程", "B", "技术面", 5, real_quota=2)
            self.assertIsInstance(k.real_question_source(p), list)
        finally:
            k.BUILTIN_Q_FILE = original


if __name__ == "__main__":
    unittest.main()
