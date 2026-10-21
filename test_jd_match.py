"""岗位 JD 对照（`jd_match.py`）的离线单元测试。

被测对象：JD → 考点/维度映射，以及「练过几条」的聚合。
这层不调用模型，所以测试全离线；关键钉住三件事：

    1. 维度映射覆盖全部考点，且具体考点不会被泛化关键词抢走；
    2. JD 里的技术词真能对上考点，笼统 JD 不会硬凑（2 字泛词门控）；
    3. 练习情况只统计真正问到过的考点，`本场未问到` 占位行不算。

运行方式（项目根目录，零新增依赖）：
    py -3.12 -m unittest test_jd_match -v
"""

from __future__ import annotations

import unittest

import jd_match
import knowledge

RUBRICS = knowledge.load_rubrics()

CS = "计算机科学与技术"

BE_JD = (
    "岗位职责：负责推荐系统后端开发；熟悉 MySQL 索引优化与慢查询调优；"
    "了解事务隔离级别；熟悉 TCP/IP 与 HTTP；有 Git 协作经验；熟悉 Python 或 Java。"
)

TXN_TOPIC = "事务隔离级别与 MVCC 的实现原理"


class TestDimensionMapping(unittest.TestCase):
    def test_every_topic_has_a_dimension(self):
        for major, topics in RUBRICS.items():
            dims = set()
            for topic in topics:
                dim = jd_match.dimension(major, topic)
                self.assertTrue(dim, f"{major}/{topic} 没有映射到维度")
                dims.add(dim)
            self.assertEqual(len(dims), 5, f"{major} 应恰好落在 5 个维度上，实际 {dims}")

    def test_specific_topics_go_to_expected_dimension(self):
        expected = {
            TXN_TOPIC: "数据库与存储设计",
            "事务的 ACID 特性": "数据库与存储设计",
            "索引原理与索引失效的常见场景": "数据库与存储设计",
            "缓存一致性协议（MESI）": "操作系统与计算机网络",
            "TCP 三次握手为什么不是两次": "操作系统与计算机网络",
            "进程与线程的区别及各自开销": "操作系统与计算机网络",
            "图的存储结构与遍历方式": "编程基础与数据结构",
            "红黑树的性质与旋转调整过程": "编程基础与数据结构",
            "断点调试与单元测试的实践": "工程实践与调试能力",
            "Git 版本控制与分支协作流程": "工程实践与调试能力",
            "项目经历的 STAR 结构化讲述": "沟通与表达",
        }
        for topic, dim in expected.items():
            self.assertEqual(jd_match.dimension(CS, topic), dim, topic)


class TestAnalyzeJd(unittest.TestCase):
    def analyze(self, jd: str, major: str = CS, records=None):
        return jd_match.analyze_jd(jd, major, rubrics=RUBRICS, records=records or [])

    def test_backend_jd_matches_transaction_topic(self):
        result = self.analyze(BE_JD)
        topics = [item["topic"] for item in result["topics"]]
        self.assertIn(TXN_TOPIC, topics[:2])
        self.assertEqual(result["major"], CS)
        self.assertEqual(result["library_size"], len(RUBRICS[CS]))

    def test_evidence_is_quoted_from_jd(self):
        result = self.analyze(BE_JD)
        for item in result["topics"]:
            for evidence in item["evidence"]:
                self.assertIn(evidence.rstrip("…"), BE_JD)

    def test_evidence_ignores_generic_clauses(self):
        # 「负责后端服务的设计与开发」只含 2 字泛词命中，不该出现在依据里
        result = self.analyze(
            "岗位职责：负责后端服务的设计与开发；熟悉 TCP/IP 与 HTTP。"
        )
        for item in result["topics"]:
            for evidence in item["evidence"]:
                self.assertNotIn("负责后端服务", evidence, item["topic"])

    def test_slash_compound_half_is_not_uncovered(self):
        # TCP/IP 拆出来的 IP 不算独立缺口（TCP 在库里已覆盖）
        result = self.analyze("熟悉 TCP/IP 与 HTTP，了解 MySQL。")
        self.assertNotIn("IP", result["uncovered"])

    def test_uncovered_english_terms_are_reported(self):
        result = self.analyze(BE_JD)
        self.assertIn("Python", result["uncovered"])
        self.assertIn("Java", result["uncovered"])

    def test_generic_grammar_does_not_match(self):
        # 「事务」「系统」「要求」这类 2 字泛词不该单独把考点拉进来
        result = self.analyze("负责公司日常行政事务与办公用品采购，要求做事细心。")
        self.assertEqual(result["topics"], [])
        self.assertEqual(result["practiced"], 0)

    def test_blank_jd_returns_empty(self):
        result = self.analyze("   ")
        self.assertEqual(result["topics"], [])
        self.assertEqual(result["dimensions"], [])

    def test_unknown_major_returns_empty(self):
        result = self.analyze(BE_JD, major="不存在的专业")
        self.assertEqual(result["topics"], [])
        self.assertEqual(result["library_size"], 0)


class TestMastery(unittest.TestCase):
    def record(self, rows, major=CS, when="2026-01-01 10:00"):
        return {"time": when, "profile": {"major": major}, "evaluation": {"rubric_check": rows}}

    def rubric(self):
        return knowledge.rubric_for_topic({"major": CS}, TXN_TOPIC)

    def pass_row(self):
        return {
            "topic": TXN_TOPIC,
            "must_hit": list(self.rubric()["must_hit"]),
            "missed": [],
            "errors": [],
            "comment": "答到了要点",
        }

    def unasked_row(self):
        return {
            "topic": TXN_TOPIC,
            "must_hit": [],
            "missed": ["本场未问到"],
            "errors": [],
            "comment": "",
        }

    def test_unasked_placeholder_is_ignored(self):
        stats = jd_match.mastery([self.record([self.unasked_row()])], CS)
        self.assertEqual(stats, {})

    def test_other_major_records_are_ignored(self):
        stats = jd_match.mastery(
            [self.record([self.pass_row()], major="软件工程")], CS
        )
        self.assertEqual(stats, {})

    def test_pass_counts_as_practiced(self):
        result = jd_match.analyze_jd(
            BE_JD,
            CS,
            rubrics=RUBRICS,
            records=[self.record([self.pass_row()])],
        )
        item = next(t for t in result["topics"] if t["topic"] == TXN_TOPIC)
        self.assertEqual(item["attempts"], 1)
        self.assertEqual(item["pass"], 1)
        self.assertEqual(item["fail"], 0)
        self.assertEqual(result["practiced"], 1)
        self.assertEqual(result["practiced_total"], 1)
        self.assertEqual(result["passed_total"], 1)

    def test_latest_verdict_uses_record_time(self):
        fail_row = self.pass_row()
        fail_row["missed"] = list(self.rubric()["must_hit"])[:1]
        records = [
            self.record([self.pass_row()], when="2026-01-01 10:00"),
            self.record([fail_row], when="2026-02-01 10:00"),
        ]
        result = jd_match.analyze_jd(BE_JD, CS, rubrics=RUBRICS, records=records)
        item = next(t for t in result["topics"] if t["topic"] == TXN_TOPIC)
        self.assertEqual(item["attempts"], 2)
        self.assertEqual(item["pass"], 1)
        self.assertEqual(item["partial"], 1)
        self.assertEqual(item["latest"], "partial")


class TestBridgeHints(unittest.TestCase):
    """库外英文词在其他专业库里有考点时，只做对照提示，不算本专业命中。"""

    def fake_rubrics(self):
        return {
            "甲专业": {
                "甲考点": {
                    "level": "A",
                    "must_hit": ["向量检索与排序"],
                    "bonus": [],
                    "common_errors": [],
                }
            },
            "乙专业": {
                "乙考点": {
                    "level": "B",
                    "must_hit": ["Redis 缓存与持久化"],
                    "bonus": [],
                    "common_errors": [],
                }
            },
        }

    def test_uncovered_tech_hints_other_major(self):
        result = jd_match.analyze_jd(
            "熟悉 Redis 缓存", "甲专业", rubrics=self.fake_rubrics(), records=[]
        )
        self.assertEqual(result["topics"], [])
        self.assertIn("Redis", result["uncovered"])
        self.assertEqual(
            result["bridges"],
            [{"token": "Redis", "major": "乙专业", "topic": "乙考点"}],
        )

    def test_covered_tech_has_no_bridge(self):
        rubrics = self.fake_rubrics()
        rubrics["甲专业"]["甲考点"]["must_hit"] = ["Redis 向量检索"]
        result = jd_match.analyze_jd(
            "熟悉 Redis 缓存", "甲专业", rubrics=rubrics, records=[]
        )
        self.assertEqual(result["bridges"], [])


if __name__ == "__main__":
    unittest.main()
