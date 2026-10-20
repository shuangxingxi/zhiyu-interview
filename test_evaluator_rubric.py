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


class TestScoreAttributionIsolation(unittest.TestCase):
    """评分与归因必须是**两次独立调用**：状态轨迹不得进入评分上下文。

    这是本次改动的核心不变量。原先一次调用里同时索要 overall_score 与
    adjusted_score，等于把面试官的情绪轨迹摆在评委面前，让他先看见"不耐烦"
    再给候选人打分——审查方一旦继承起草方的叙事，审查就退化成自我确认。
    """

    MOOD = [{"round": 1, "mood": "不耐烦", "patience": 1, "energy": 2, "pressure": 2}]

    def _run(self, profile, replies, mood_trace):
        """replies 按调用顺序依次返回（异常对象表示该次调用抛错）。

        返回 (每次调用的 system/user, evaluate_interview 的结果)。
        """
        calls: list[dict[str, str]] = []
        queue = list(replies)

        def fake_chat_json(messages, api_key, model=None, **kwargs):
            calls.append(
                {"system": messages[0]["content"], "user": messages[-1]["content"]}
            )
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(agents, "chat_json", fake_chat_json):
            result = agents.evaluate_interview(
                [
                    {"role": "assistant", "content": "讲讲你做的项目。"},
                    {"role": "user", "content": "我做过推荐系统"},
                ],
                profile,
                "fake-key",
                mood_trace=mood_trace,
                asked_topics=[profile["topics"][0]],
            )
        return calls, result

    def test_mood_trace_only_reaches_the_attribution_call(self):
        profile = make_profile("A")
        calls, _ = self._run(
            profile,
            [
                {"overall_score": 60, "dimensions": []},
                {"adjusted_score": 88, "attribution": "第 1 轮的不耐烦把他带乱了"},
            ],
            self.MOOD,
        )
        self.assertEqual(len(calls), 2, "评分与归因必须是两次独立调用")
        self.assertNotIn(
            "不耐烦", calls[0]["system"], "状态轨迹不能进评分上下文"
        )
        self.assertIn("不耐烦", calls[1]["system"], "归因调用才该看到状态轨迹")

    def test_adjusted_score_comes_from_the_attribution_call(self):
        profile = make_profile("A")
        _, result = self._run(
            profile,
            [
                {"overall_score": 60, "dimensions": []},
                {"adjusted_score": 88, "attribution": "第 1 轮受到干扰"},
            ],
            self.MOOD,
        )
        self.assertEqual(result["adjusted_score"], 88)
        self.assertIn("第 1 轮", result["attribution"])

    def test_scoring_call_cannot_dictate_the_adjusted_score(self):
        # 评分子调用即便"顺手"给了校正分也必须丢弃，否则隔离形同虚设
        profile = make_profile("A")
        _, result = self._run(
            profile,
            [
                {
                    "overall_score": 60,
                    "adjusted_score": 99,
                    "attribution": "我自己顺手算的",
                    "dimensions": [],
                },
                {"adjusted_score": 88, "attribution": "第 1 轮受到干扰"},
            ],
            self.MOOD,
        )
        self.assertEqual(result["adjusted_score"], 88)

    def test_without_mood_trace_only_one_call_and_score_is_unchanged(self):
        profile = make_profile("A")
        calls, result = self._run(
            profile, [{"overall_score": 60, "dimensions": []}], None
        )
        self.assertEqual(len(calls), 1, "没有状态轨迹就不该再花一次调用")
        self.assertEqual(result["adjusted_score"], 60)
        self.assertTrue(result["attribution"].strip())

    def test_attribution_failure_degrades_to_the_original_score(self):
        # 多做一次调用可以失败，但报告必须照常产出
        profile = make_profile("A")
        _, result = self._run(
            profile,
            [{"overall_score": 60, "dimensions": []}, ValueError("boom")],
            self.MOOD,
        )
        self.assertEqual(result["adjusted_score"], 60)
        self.assertTrue(result["attribution"].strip())


class TestAdjustedScoreGate(unittest.TestCase):
    """归因产物关卡：只有可用的 adjusted_score 才允许校正综合分。

    归因是"多做一次调用"的附加产物，它的字段质量不能反过来污染报告——
    模型给出字符串、布尔或 null 时，一律回落原始分（静默降级）。
    """

    MOOD = [{"round": 1, "mood": "不耐烦", "patience": 1, "energy": 2, "pressure": 2}]

    def test_gate_accepts_only_real_numbers(self):
        self.assertTrue(agents.gate_adjusted_score({"adjusted_score": 88})[0])
        self.assertTrue(agents.gate_adjusted_score({"adjusted_score": 88.5})[0])
        for bad in ["88", None, True, False, "", [], {}]:
            with self.subTest(adjusted=bad):
                ok, reason = agents.gate_adjusted_score({"adjusted_score": bad})
                self.assertFalse(ok)
                self.assertTrue(reason)
        # 字段缺失也走同一条降级路径
        self.assertFalse(agents.gate_adjusted_score({})[0])

    def test_summary_block_does_not_raise_on_empty_score(self):
        # _score_brief 是喂给归因调用的"评分摘要"，字段缺失时不能抛异常
        self.assertTrue(agents._score_brief({}).strip())

    def _run(self, profile, replies, mood_trace):
        queue = list(replies)

        def fake_chat_json(messages, api_key, model=None, **kwargs):
            return queue.pop(0)

        with mock.patch.object(agents, "chat_json", fake_chat_json):
            return agents.evaluate_interview(
                [
                    {"role": "assistant", "content": "讲讲你做的项目。"},
                    {"role": "user", "content": "我做过推荐系统"},
                ],
                profile,
                "fake-key",
                mood_trace=mood_trace,
                asked_topics=[profile["topics"][0]],
            )

    def test_string_adjusted_score_degrades_to_original(self):
        profile = make_profile("A")
        result = self._run(
            profile,
            [
                {"overall_score": 60, "dimensions": []},
                {"adjusted_score": "88", "attribution": "第 1 轮受到干扰"},
            ],
            self.MOOD,
        )
        self.assertEqual(result["adjusted_score"], 60, "脏数据必须回落原始分")

    def test_missing_attribution_field_keeps_reason_text(self):
        # 归因调用没给出校正分，但仍写了说明：说明要保留，分数回落
        profile = make_profile("A")
        result = self._run(
            profile,
            [
                {"overall_score": 60, "dimensions": []},
                {"attribution": "本场状态基本中性"},
            ],
            self.MOOD,
        )
        self.assertEqual(result["adjusted_score"], 60)
        self.assertIn("中性", result["attribution"])


class TestTopicVerdict(unittest.TestCase):
    """考点级判定：把要点粒度的对照结果升成"这道题整体过没过"。

    规则全在代码里（knowledge.topic_verdict），同一份 rubric_check 每次得到同一结论。
    """

    def test_veto_error_is_a_hard_fail(self):
        row = {
            "topic": "TCP 三次握手",
            "must_hit": ["第三次握手确认双方收发能力"],
            "missed": [],
            "errors": ["说成两次握手就够了"],
        }
        verdict, reason = knowledge.topic_verdict(row, veto=["说成两次握手就够了"])
        self.assertEqual(verdict, knowledge.VERDICT_FAIL)
        self.assertIn("一票否决", reason)

    def test_error_without_veto_marker_is_still_a_fail(self):
        # 条目没标 veto：踩误区照样判不通过，只是不会说成"一票否决"
        row = {"topic": "x", "must_hit": ["a"], "missed": [], "errors": ["概念说反了"]}
        verdict, reason = knowledge.topic_verdict(row)
        self.assertEqual(verdict, knowledge.VERDICT_FAIL)
        self.assertNotIn("一票否决", reason)
        self.assertIn("概念错误", reason)

    def test_all_must_hit_missed_is_a_fail(self):
        row = {"topic": "x", "must_hit": [], "missed": ["要点一", "要点二"], "errors": []}
        verdict, reason = knowledge.topic_verdict(row)
        self.assertEqual(verdict, knowledge.VERDICT_FAIL)
        self.assertIn("一条都没答到", reason)

    def test_some_hit_some_missed_is_partial(self):
        row = {"topic": "x", "must_hit": ["要点一"], "missed": ["要点二"], "errors": []}
        self.assertEqual(knowledge.topic_verdict(row)[0], knowledge.VERDICT_PARTIAL)

    def test_full_coverage_is_pass(self):
        row = {"topic": "x", "must_hit": ["要点一", "要点二"], "missed": [], "errors": []}
        self.assertEqual(knowledge.topic_verdict(row)[0], knowledge.VERDICT_PASS)

    def test_empty_record_is_unknown(self):
        for row in [{}, {"topic": "x"}, {"topic": "x", "must_hit": []}, None, "不是字典"]:
            with self.subTest(row=row):
                self.assertEqual(knowledge.topic_verdict(row)[0], knowledge.VERDICT_UNKNOWN)

    def test_every_verdict_has_a_chinese_label(self):
        for verdict in (
            knowledge.VERDICT_PASS,
            knowledge.VERDICT_PARTIAL,
            knowledge.VERDICT_FAIL,
            knowledge.VERDICT_UNKNOWN,
        ):
            with self.subTest(verdict=verdict):
                self.assertTrue(knowledge.VERDICT_LABELS[verdict])


class TestRubricVetoAndAggregate(unittest.TestCase):
    """veto 标记渲染 + 考点级聚合（参照缺失时静默降级）。"""

    FIXTURE = {
        "计算机科学与技术": {
            "TCP 三次握手": {
                "must_hit": ["第三次握手确认双方收发能力"],
                "bonus": [],
                "common_errors": ["说成两次握手就够了"],
                "veto": ["说成两次握手就够了"],
            },
            "进程调度": {
                "must_hit": ["时间片轮转", "优先级抢占"],
                "bonus": [],
                "common_errors": ["把调度说成同步机制"],
            },
        }
    }

    ROWS = [
        {"topic": "TCP 三次握手", "must_hit": ["第三次握手确认双方收发能力"], "missed": [], "errors": []},
        {"topic": "进程调度", "must_hit": ["时间片轮转"], "missed": ["优先级抢占"], "errors": []},
        {"topic": "模型自拟的考点", "must_hit": [], "missed": ["没答到"], "errors": []},
    ]

    def _profile(self):
        profile = make_profile("B")
        profile["major"] = "计算机科学与技术"
        return profile

    def test_veto_line_is_rendered_only_when_marked(self):
        with mock.patch.object(knowledge, "load_rubrics", lambda: self.FIXTURE):
            marked = knowledge.rubric_block(self._profile(), ["TCP 三次握手"])
            plain = knowledge.rubric_block(self._profile(), ["进程调度"])
        self.assertIn("一票否决", marked)
        self.assertIn("说成两次握手就够了", marked)
        self.assertNotIn("一票否决", plain, "没标 veto 的条目不该多出这一行")

    def test_veto_marker_upgrades_a_scored_topic_to_fail(self):
        row = {
            "topic": "TCP 三次握手",
            "must_hit": ["第三次握手确认双方收发能力"],
            "missed": [],
            "errors": ["说成两次握手就够了"],
        }
        with mock.patch.object(knowledge, "load_rubrics", lambda: self.FIXTURE):
            agg = knowledge.aggregate_rubric_check(self._profile(), [row])
        self.assertEqual(agg["topics"][0]["verdict"], knowledge.VERDICT_FAIL)
        self.assertEqual(agg["counts"][knowledge.VERDICT_FAIL], 1)
        self.assertEqual(agg["passed"], 0)

    def test_counts_and_weak_list(self):
        with mock.patch.object(knowledge, "load_rubrics", lambda: self.FIXTURE):
            agg = knowledge.aggregate_rubric_check(self._profile(), self.ROWS)
        self.assertEqual(agg["total"], 3)
        self.assertEqual(agg["passed"], 1)
        self.assertEqual(agg["counts"][knowledge.VERDICT_PARTIAL], 1)
        self.assertEqual(agg["counts"][knowledge.VERDICT_FAIL], 1)
        # 薄弱项只收不通过与部分通过，且保留原来的顺序
        self.assertEqual([t["topic"] for t in agg["weak"]], ["进程调度", "模型自拟的考点"])
        self.assertTrue(all(t["label"] for t in agg["topics"]))

    def test_missing_rubrics_degrades_silently(self):
        with mock.patch.object(knowledge, "load_rubrics", lambda: {}):
            agg = knowledge.aggregate_rubric_check({}, self.ROWS)
        self.assertEqual(agg["total"], 3, "参照缺失时判定仍要能算出来")
        self.assertEqual(agg["passed"], 1)

    def test_junk_rows_are_ignored(self):
        rows = [{"topic": "进程调度", "must_hit": ["时间片轮转"]}, None, "不是字典", 3]
        with mock.patch.object(knowledge, "load_rubrics", lambda: {}):
            agg = knowledge.aggregate_rubric_check(self._profile(), rows)
        self.assertEqual(agg["total"], 1)
        self.assertEqual(agg["passed"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
