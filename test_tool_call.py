"""面试官工具调用机制（主动查阅简历原文）的单元测试。

被测对象：agents.py 里的 `_normalize_tool` / `_clear_tool` /
`_resume_tool_round` / `interviewer_turn` 中的工具分支。

测试原则（对应 DESIGN.md 第五节）：
    1. 全部离线：把 `agents.chat_json` 换成假模型，不碰网络、不花 token；
    2. 检索用真件：`resume_rag` 是本地 BM25，真跑一遍才有意义，
       所以给真简历、真切片，只把"模型"这一层换成假的；
    3. 断言"行为"而不是"实现"：重点验证四条回落路径都不中断面试、
       不调工具时调用次数不变（成本不增）、返回的决策不带"待查"标记。

运行方式（项目根目录，零新增依赖）：
    py -3.12 -m unittest test_tool_call -v
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

import agents
import resume_rag

# 一份结构清晰、能被 BM25 命中的简历：三个小节 → R1 / R2 / R3
RESUME = """教育背景
某某大学 计算机科学与技术 本科 2021-2025

实习经历
2024.06-2024.09 某互联网公司 后端开发实习生
负责推荐系统的离线特征回填模块，用 Spark 聚合日活用户行为日志，QPS 峰值 3 万，回填耗时从 4 小时降到 40 分钟。

项目经历
基于协同过滤的图书推荐系统（毕业设计）
Python + Flask 实现，离线评测 Recall@10 从 0.12 提升到 0.21。
"""

# 能命中简历的检索词（命中 R2 实习经历 / R3 项目经历）
HIT_QUERY = "推荐系统 特征回填"
# 与简历没有共同字符二元组的检索词（用于测"查了但没命中"）
MISS_QUERY = "量子纠缠态"

SYS_MESSAGE = {"role": "system", "content": "（测试用系统提示词）"}


@contextlib.contextmanager
def quiet_stderr():
    """静音 stderr。

    故意触发失败路径的用例会打一行"二次调用失败，回落首轮决策"的诊断日志，
    那是生产代码该有的行为，但混在测试输出里像报错，读起来容易被误导。
    """
    with contextlib.redirect_stderr(io.StringIO()):
        yield


class FakeChat:
    """假模型：按预设顺序吐出响应，并记录每次收到的 messages。

    响应可以是一条 dict（正常返回），也可以是一个异常实例（模拟接口报错）。
    """

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(
        self, messages, api_key, model=None, temperature=0.3, attempts=3, timeout=None
    ):
        # 签名必须与 agents.chat_json 保持一致：调用方一旦新增关键字参数，
        # 这里会立刻报错，而不是被 **kwargs 悄悄吞掉。
        self.calls.append(
            {
                "messages": [dict(m) for m in messages],
                "temperature": temperature,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError(
                f"假模型被调用了第 {len(self.calls)} 次，但预设响应已经用完"
            )
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def messages_of(self, index: int) -> list[dict]:
        return self.calls[index]["messages"]

    def injected(self, index: int = -1) -> str:
        """取出第 index 次调用里最后一条消息，即回灌给模型的工具结果。"""
        return self.messages_of(index)[-1]["content"]


def make_profile(resume: str = RESUME, **overrides) -> dict:
    """造一份最小可用的面试官配置。

    strict=True + max_probes=2 是为了让追问上限不随情绪浮动，
    断言里就能直接写死"追问超限"这类边界。
    """
    profile = {
        "resume": resume,
        "resume_chunks": resume_rag.build(resume) if resume else [],
        "total_rounds": 5,
        "max_probes": 2,
        "strict": True,
        "allow_reflect": True,
        "topics": [],
        "avoid_topics": [],
        "real_questions": [],
        "dimensions": ["专业基础"],
    }
    profile.update(overrides)
    return profile


def make_decision(**overrides) -> dict:
    """造一条面试官决策，字段齐全，避免测到一半被 KeyError 打断。"""
    data = {
        "reply": "那你这个回填模块具体怎么设计的？",
        "action": "ask",
        "question_no": 1,
        "topic": "工程实践",
        "focus": "技术选型",
        "gap": "",
        "tool": agents.TOOL_NONE,
        "tool_query": "",
        "note": "内部备注",
    }
    data.update(overrides)
    return data


def resume_lookup_plan(**overrides) -> dict:
    """造一条"决定查阅简历"的决策。"""
    data = make_decision(
        reply="我先看一下你简历里那段实习。",
        tool=agents.TOOL_RESUME,
        tool_query=HIT_QUERY,
    )
    data.update(overrides)
    return data


class TestNormalizeTool(unittest.TestCase):
    """tool / tool_query 的取值收敛：模型填什么都不能让后续逻辑跑偏。"""

    def test_resume_lookup_passthrough(self):
        self.assertEqual(
            agents._normalize_tool({"tool": "resume_lookup", "tool_query": "推荐系统"}),
            ("resume_lookup", "推荐系统"),
        )

    def test_case_and_whitespace_tolerated(self):
        # 模型可能把枚举值写成大写或带空格，收敛时一并处理
        self.assertEqual(
            agents._normalize_tool({"tool": "  RESUME_LOOKUP ", "tool_query": " 项目 "}),
            ("resume_lookup", "项目"),
        )

    def test_missing_fields_default_to_no_tool(self):
        self.assertEqual(agents._normalize_tool({}), ("none", ""))

    def test_invalid_values_collapse_to_no_tool(self):
        for bad in ["search", "resume", "None", "resume_lookup2", "查简历"]:
            with self.subTest(tool=bad):
                self.assertEqual(agents._normalize_tool({"tool": bad})[0], "none")

    def test_non_string_values_are_safe(self):
        self.assertEqual(agents._normalize_tool({"tool": None})[0], "none")
        self.assertEqual(agents._normalize_tool({"tool": 123})[0], "none")
        self.assertEqual(agents._normalize_tool({"tool_query": None})[1], "")
        self.assertEqual(agents._normalize_tool({"tool_query": 123})[1], "123")

    def test_apply_fallbacks_always_sets_both_fields(self):
        # 走一遍代码兜底，保证 tool 字段在整条链路上都存在
        data = agents._apply_fallbacks(
            {"action": "probe", "reply": "追问一句", "note": ""},
            make_profile(),
            cur_q=1,
            probes_used=0,
            reflects_in_a_row=0,
            mood_state={},
        )
        self.assertEqual(data["tool"], "none")
        self.assertEqual(data["tool_query"], "")

    def test_clear_tool_wipes_pending_marker(self):
        data = resume_lookup_plan()
        agents._clear_tool(data)
        self.assertEqual(data["tool"], "none")
        self.assertEqual(data["tool_query"], "")


class TestToolRuleInjection(unittest.TestCase):
    """提示词注入：有简历才提"可以查阅"，没简历不提。"""

    def _system(self, profile: dict) -> str:
        return agents._build_interviewer_system(profile, 0, 0, 0, {}, [], "", "")

    def test_rule_text_mentions_the_tool_and_its_query(self):
        self.assertIn("resume_lookup", agents._TOOL_RULE)
        self.assertIn("tool_query", agents._TOOL_RULE)

    def test_rule_injected_when_resume_present(self):
        self.assertIn("主动查阅简历原文", self._system(make_profile()))

    def test_rule_absent_without_resume(self):
        # 没有简历就不该让模型知道有"查阅"这回事，免得对着空索引发起检索
        self.assertNotIn("主动查阅简历原文", self._system(make_profile(resume="")))

    def test_schema_always_declares_tool_fields(self):
        # 字段声明全场都在：没有简历时靠它保证返回结构一致（取值一律 none）
        self.assertIn('"tool"', agents.INTERVIEWER_SYSTEM)
        self.assertIn('"tool_query"', agents.INTERVIEWER_SYSTEM)


class TestResumeToolRound(unittest.TestCase):
    """两步决策（先检索、再出话）以及四条回落路径。"""

    def setUp(self):
        self.profile = make_profile()
        self.messages = [dict(SYS_MESSAGE)]

    def run_round(
        self,
        plan,
        fake,
        profile=None,
        cur_q=0,
        probes_used=0,
        reflects_in_a_row=0,
        mood_state=None,
    ):
        with mock.patch.object(agents, "chat_json", fake):
            return agents._resume_tool_round(
                self.messages,
                plan,
                profile if profile is not None else self.profile,
                "fake-key",
                agents.DEFAULT_MODEL,
                cur_q,
                probes_used,
                reflects_in_a_row,
                mood_state if mood_state is not None else {},
            )

    # -- 不调工具：行为与 v5 完全一致 --------------------------------------

    def test_no_tool_call_does_not_hit_the_model(self):
        fake = FakeChat()
        plan = make_decision()  # tool = none
        final, trace = self.run_round(plan, fake)

        self.assertIs(final, plan, "不调工具时应原样返回首轮决策")
        self.assertEqual(trace, "")
        self.assertEqual(fake.call_count, 0, "不调工具不该产生任何额外模型调用")

    def test_tool_without_resume_is_ignored(self):
        # 本场没有简历：即使模型填了 resume_lookup，也不该检索、不该多调一次
        fake = FakeChat()
        plan = resume_lookup_plan()
        final, trace = self.run_round(plan, fake, profile=make_profile(resume=""))

        self.assertIs(final, plan)
        self.assertEqual(trace, "")
        self.assertEqual(fake.call_count, 0)
        self.assertEqual(final["tool"], "none", "回落路径也必须清掉待查标记")

    # -- 查了但没命中 ------------------------------------------------------

    def test_lookup_miss_records_trace_without_extra_call(self):
        self.assertFalse(
            resume_rag.top(self.profile, MISS_QUERY), "前提：该词确实命不中这份简历"
        )

        fake = FakeChat()
        plan = resume_lookup_plan(tool_query=MISS_QUERY)
        final, trace = self.run_round(plan, fake)

        self.assertIs(final, plan, "没命中就按首轮决策继续，不重新让模型出话")
        self.assertEqual(fake.call_count, 0)
        self.assertIn("未命中", trace)
        self.assertIn(MISS_QUERY, trace)
        self.assertEqual(final["tool"], "none")

    # -- 命中：真正走完两步 -------------------------------------------------

    def test_hit_injects_resume_text_and_returns_second_decision(self):
        hits = resume_rag.top(self.profile, HIT_QUERY)
        self.assertTrue(hits, "前提：这个词应该能命中简历切片")

        second = make_decision(reply="你简历里那个回填模块，为什么用 Spark 而不是 Flink？")
        fake = FakeChat(second)
        plan = resume_lookup_plan()
        final, trace = self.run_round(plan, fake)

        # 1) 真的多调了一次模型
        self.assertEqual(fake.call_count, 1)
        # 2) 最终决策取自第二次调用，而不是首轮那句"我先看一下简历"
        self.assertEqual(final["reply"], second["reply"])
        self.assertNotEqual(final["reply"], plan["reply"])
        # 3) 检索到的原文确实回灌进去了
        injected = fake.injected()
        self.assertIn("你查阅到的简历原文", injected)
        for hit in hits:
            self.assertIn(hit["text"].strip(), injected, "命中切片原文必须完整回灌")
        # 4) 回灌前先把模型自己那条决策作为 assistant 轮还给它，保持对话结构
        assistant_turn = fake.messages_of(0)[-2]
        self.assertEqual(assistant_turn["role"], "assistant")
        self.assertEqual(json.loads(assistant_turn["content"])["tool"], "resume_lookup")
        # 5) 提示词要求这一轮落地后回填 none
        self.assertIn('"none"', injected)
        # 6) 轨迹可读，且与命中的切片一致
        self.assertIn("查阅简历", trace)
        self.assertIn(HIT_QUERY, trace)
        for hit in hits:
            self.assertIn(hit["id"], trace)

    def test_hit_path_clears_pending_marker(self):
        fake = FakeChat(make_decision(reply="第二条回复"))
        final, trace = self.run_round(resume_lookup_plan(), fake)

        self.assertEqual(final["tool"], "none", "落地后必须清标记，否则下一轮会重复检索")
        self.assertEqual(final["tool_query"], "")
        self.assertIn("查阅简历", trace)

    # -- 带上限的循环（v6 起由写死两步改为最多 MAX_TOOL_ROUNDS 次）----------

    def test_second_lookup_allowed_when_model_asks_again(self):
        # 模型查了 A 之后还想核对 B：应当允许再查一次，而不是被写死的两步截断
        fake = FakeChat(
            resume_lookup_plan(tool_query=HIT_QUERY, reply="我再核对一段。"),
            make_decision(reply="这次可以出话了。"),
        )
        final, trace = self.run_round(resume_lookup_plan(), fake)

        self.assertEqual(fake.call_count, 2, "两轮查阅各产生一次模型调用")
        self.assertEqual(final["reply"], "这次可以出话了。")
        self.assertEqual(final["tool"], "none")
        # 轨迹要把两次查阅都记下来，报告里才看得到"查了两段"
        self.assertEqual(trace.count("查阅简历"), 2)

    def test_lookup_loop_is_capped(self):
        # 模型一直要求查阅：必须在上限处停下，不能无限调用
        responses = [
            resume_lookup_plan(tool_query=HIT_QUERY, reply=f"第 {i} 次还要查")
            for i in range(1, 6)
        ]
        fake = FakeChat(*responses)
        with quiet_stderr():
            final, _ = self.run_round(resume_lookup_plan(), fake)

        self.assertEqual(
            fake.call_count,
            agents.MAX_TOOL_ROUNDS,
            "查阅次数必须被 MAX_TOOL_ROUNDS 封顶，否则单轮成本可被模型拉爆",
        )
        self.assertEqual(final["tool"], "none", "封顶返回的决策同样不能带待查标记")

    def test_second_iteration_sees_the_first_lookup(self):
        # 第二轮回灌要接在第一轮之后，模型才看得到"我已经查过什么"
        fake = FakeChat(
            resume_lookup_plan(tool_query=HIT_QUERY, reply="再核对一段。"),
            make_decision(reply="出话。"),
        )
        self.run_round(resume_lookup_plan(), fake)

        second_call = fake.messages_of(1)
        joined = json.dumps(second_call, ensure_ascii=False)
        self.assertIn("你查阅到的简历原文", joined, "第二轮必须带上第一轮的查阅结果")

    def test_injected_chunks_respect_top_k(self):
        # 注入几条由 TOP_K 决定：一次查阅不能把整份简历灌进上下文
        chunks = [
            {"id": f"R{i}", "section": f"项目{i}", "text": f"推荐系统相关经历第 {i} 段，负责召回模块。"}
            for i in range(1, 5)
        ]
        profile = make_profile(resume_chunks=chunks)
        fake = FakeChat(make_decision(reply="第二条回复"))
        self.run_round(resume_lookup_plan(tool_query="推荐系统 召回"), fake, profile=profile)

        injected = fake.injected()
        used = [c["id"] for c in chunks if f"[{c['id']} · " in injected]
        self.assertEqual(len(used), resume_rag.TOP_K_INTERVIEW)
        self.assertEqual(resume_rag.TOP_K_INTERVIEW, 2, "本断言依赖当前配置值")

    # -- 回落路径：二次调用出问题时不能中断面试 -----------------------------

    def test_second_call_error_falls_back_to_first_decision(self):
        fake = FakeChat(ValueError("模拟：模型连续返回不可用 JSON"))
        plan = resume_lookup_plan()
        with quiet_stderr():
            final, trace = self.run_round(plan, fake)

        self.assertEqual(fake.call_count, 1, "失败也不重试第二次工具回合，直接回落")
        self.assertEqual(final["reply"], plan["reply"], "回落首轮决策，面试继续")
        self.assertIn("查阅简历", trace, "查过就要留痕，即使二次调用失败")
        self.assertEqual(final["tool"], "none")

    def test_second_call_empty_reply_falls_back(self):
        fake = FakeChat(make_decision(reply=""))
        plan = resume_lookup_plan()
        final, trace = self.run_round(plan, fake)

        self.assertEqual(fake.call_count, 1)
        self.assertEqual(
            final["reply"], plan["reply"], "空白回复必须回落，不能给候选人一片空白"
        )
        self.assertIn("查阅简历", trace)
        self.assertEqual(final["tool"], "none")

    def test_second_call_still_goes_through_code_fallbacks(self):
        # 第二步的决策同样受代码兜底约束：追问次数已满时不许再追问
        fake = FakeChat(make_decision(action="probe", reply="还想再追问一次"))
        final, _ = self.run_round(resume_lookup_plan(), fake, cur_q=1, probes_used=2)

        self.assertEqual(final["action"], "ask", "追问超限时第二步也要被纠正为换题")

    def test_all_paths_return_without_pending_tool(self):
        # 契约：无论走哪条路，返回的决策都不带"待查"标记
        cases = {
            "不调工具": (make_decision(), FakeChat()),
            "没有简历": (resume_lookup_plan(), FakeChat()),
            "检索未命中": (resume_lookup_plan(tool_query=MISS_QUERY), FakeChat()),
            "二次调用失败": (resume_lookup_plan(), FakeChat(ValueError("boom"))),
            "二次回复为空": (resume_lookup_plan(), FakeChat(make_decision(reply=""))),
            "成功两步": (resume_lookup_plan(), FakeChat(make_decision(reply="第二条回复"))),
        }
        for name, (plan, fake) in cases.items():
            with self.subTest(场景=name):
                profile = make_profile(resume="") if name == "没有简历" else self.profile
                with quiet_stderr():
                    final, _ = self.run_round(plan, fake, profile=profile)
                self.assertEqual(final["tool"], "none")
                self.assertEqual(final["tool_query"], "")


class TestInterviewerTurnToolIntegration(unittest.TestCase):
    """整轮视角：调用次数、工具轨迹、以及不污染下一轮的历史。"""

    def setUp(self):
        self.profile = make_profile()
        self.history = [
            {"role": "assistant", "content": "先聊聊你的经历吧。", "decision": make_decision()},
            {"role": "user", "content": "我在实习里做了推荐系统的离线特征回填。"},
        ]

    def call_turn(self, fake, profile=None, history=None, **kwargs):
        with mock.patch.object(agents, "chat_json", fake):
            return agents.interviewer_turn(
                self.history if history is None else history,
                profile if profile is not None else self.profile,
                "fake-key",
                **kwargs,
            )

    def test_no_tool_keeps_single_model_call(self):
        fake = FakeChat(make_decision(reply="那你用 Spark 具体做了哪一步？"))
        data = self.call_turn(fake, cur_q=1, asked_topics=[])

        self.assertEqual(fake.call_count, 1, "不调工具的老路径成本必须不变")
        self.assertEqual(data["tool_trace"], "")
        self.assertEqual(data["tool"], "none")
        self.assertEqual(data["reply"], "那你用 Spark 具体做了哪一步？")

    def test_tool_path_uses_two_calls_and_reports_trace(self):
        fake = FakeChat(
            resume_lookup_plan(),
            make_decision(reply="你简历里写的是 4 小时降到 40 分钟，这个数字怎么来的？"),
        )
        data = self.call_turn(fake, cur_q=1, asked_topics=[])
        hits = resume_rag.top(self.profile, HIT_QUERY)

        self.assertEqual(fake.call_count, 2, "调工具时应是两步：先查、再出话")
        self.assertIn("查阅简历", data["tool_trace"])
        for hit in hits:
            self.assertIn(hit["id"], data["tool_trace"])
        self.assertIn("4 小时降到 40 分钟", data["reply"])
        # 关键回归：待查标记必须在本轮抹掉，否则历史回传会让下一轮重复检索
        self.assertEqual(data["tool"], "none")
        self.assertEqual(data["tool_query"], "")

    def test_history_messages_carry_no_pending_tool(self):
        # 决策会作为 assistant 轮回传给模型，里面不能残留 resume_lookup
        fake = FakeChat(resume_lookup_plan(), make_decision(reply="第二条回复"))
        data = self.call_turn(fake, cur_q=1, asked_topics=[])

        payload = json.dumps(data, ensure_ascii=False)
        self.assertIn('"tool": "none"', payload)
        self.assertNotIn("resume_lookup", payload)
        # 轨迹是"查过了"的记录，与"还要查"的标记是两回事，两者共存是正常的
        self.assertIn("查阅简历", data["tool_trace"])

    def test_tool_ignored_when_no_resume(self):
        fake = FakeChat(resume_lookup_plan(reply="没有简历也照样提问。"))
        data = self.call_turn(fake, profile=make_profile(resume=""), cur_q=1, asked_topics=[])

        self.assertEqual(fake.call_count, 1, "没简历就不该多花一次调用")
        self.assertEqual(data["tool_trace"], "")
        self.assertEqual(data["reply"], "没有简历也照样提问。")

    def test_lookup_miss_still_produces_a_usable_turn(self):
        fake = FakeChat(resume_lookup_plan(tool_query=MISS_QUERY, reply="那我换个方向问。"))
        data = self.call_turn(fake, cur_q=1, asked_topics=[])

        self.assertEqual(fake.call_count, 1)
        self.assertIn("未命中", data["tool_trace"])
        self.assertEqual(data["reply"], "那我换个方向问。")
        self.assertEqual(data["action"], "ask")

    def test_topic_dedup_retry_only_applies_to_first_round(self):
        # 去重重试只在首轮决策上触发；工具回合不再触发，避免一轮里越试越多
        fake = FakeChat(
            resume_lookup_plan(topic="一个新的考点"),
            make_decision(topic="工程实践", reply="第二条回复"),
        )
        data = self.call_turn(fake, cur_q=1, asked_topics=["工程实践"])

        self.assertEqual(fake.call_count, 2)
        self.assertEqual(data["topic"], "工程实践")

    def test_duplicate_topic_triggers_one_retry_and_adopts_fresh_topic(self):
        # 撞车 → 点名纠正一次；重试给出未问过的考点 → 采纳新产出
        fake = FakeChat(
            make_decision(topic="工程实践"),
            make_decision(topic="并发编程", reply="换个考点问。"),
        )
        data = self.call_turn(fake, cur_q=1, asked_topics=["工程实践"])

        self.assertEqual(fake.call_count, 2)
        self.assertEqual(data["topic"], "并发编程")
        self.assertEqual(data["reply"], "换个考点问。")

    def test_still_duplicate_retry_keeps_the_first_decision(self):
        # 重试仍撞车：不无限重试，沿用第一版决策（宁可重复一次也不能把面试卡死）
        first = make_decision(topic="工程实践")
        fake = FakeChat(first, make_decision(topic="工程实践", reply="还是这个考点。"))
        data = self.call_turn(fake, cur_q=1, asked_topics=["工程实践"])

        self.assertEqual(fake.call_count, 2)
        self.assertEqual(data["topic"], "工程实践")
        self.assertEqual(data["reply"], first["reply"], "沿用第一版决策")

    def test_opening_turn_forces_ask_even_after_lookup(self):
        # 开场轮必须是提问：工具回合的最终决策也要过这道兜底
        fake = FakeChat(
            resume_lookup_plan(action="probe", question_no=1),
            make_decision(action="probe", reply="开场还是得问问题。"),
        )
        data = self.call_turn(fake, history=[], cur_q=0, asked_topics=[])

        self.assertEqual(data["action"], "ask")
        self.assertEqual(data["reply"], "开场还是得问问题。")


class TestAcceptanceGates(unittest.TestCase):
    """统一验收门：具名关卡只做"放行 / 拦下并说明原因"，纠正仍归调用点。

    关卡是纯函数，直接喂构造数据即可，不需要假模型，也不需要网络。
    """

    def test_action_gate_accepts_case_and_whitespace(self):
        for good in ["ask", "probe", "reflect", "end", " ASK ", "Probe"]:
            with self.subTest(action=good):
                self.assertTrue(agents.gate_action(good)[0])

    def test_action_gate_rejects_everything_else(self):
        for bad in ["", None, "追问", "ask2", "reflection", 123]:
            with self.subTest(action=bad):
                ok, reason = agents.gate_action(bad)
                self.assertFalse(ok)
                self.assertTrue(reason, "拦下时必须给出可读的原因")

    def test_action_gate_is_the_single_source_of_the_action_space(self):
        # 关卡判定的动作空间必须与 _apply_fallbacks 的收敛同源
        self.assertEqual(agents.ACTIONS, {"ask", "probe", "reflect", "end"})
        for action in agents.ACTIONS:
            with self.subTest(action=action):
                data = agents._apply_fallbacks(
                    {"action": action, "reply": "问一句"},
                    make_profile(),
                    cur_q=1,
                    probes_used=0,
                    reflects_in_a_row=0,
                    mood_state={},
                )
                self.assertEqual(data["action"], action, "合法动作不该被兜底改写")
        self.assertEqual(
            agents._apply_fallbacks(
                {"action": "点名", "reply": "问一句"},
                make_profile(),
                cur_q=1,
                probes_used=0,
                reflects_in_a_row=0,
                mood_state={},
            )["action"],
            "ask",
            "非法动作一律回落 ask",
        )

    def test_topic_duplicate_gate_only_judges_new_questions(self):
        asked = ["工程实践"]
        self.assertTrue(
            agents.gate_topic_duplicate({"action": "ask", "topic": "并发编程"}, asked)[0]
        )
        ok, reason = agents.gate_topic_duplicate(
            {"action": "ask", "topic": "工程实践"}, asked
        )
        self.assertFalse(ok)
        self.assertIn("工程实践", reason)
        # 追问 / 复述 / 收尾沿用同一道题，topic 不变是正常的
        for action in ["probe", "reflect", "end"]:
            with self.subTest(action=action):
                self.assertTrue(
                    agents.gate_topic_duplicate(
                        {"action": action, "topic": "工程实践"}, asked
                    )[0]
                )
        # 空的 ask 不在这里拦：那属于"产出不完整"，由产出侧关卡负责
        self.assertTrue(
            agents.gate_topic_duplicate({"action": "ask", "topic": ""}, asked)[0]
        )

    def test_new_question_gate_requires_ask_topic_and_freshness(self):
        asked = ["工程实践"]
        self.assertTrue(
            agents.gate_new_question({"action": "ask", "topic": "并发编程"}, asked)[0]
        )
        for bad in [
            {"action": "probe", "topic": "并发编程"},
            {"action": "end", "topic": ""},
            {"action": "ask", "topic": ""},
            {"action": "ask", "topic": "工程实践"},
        ]:
            with self.subTest(data=bad):
                ok, reason = agents.gate_new_question(bad, asked)
                self.assertFalse(ok)
                self.assertTrue(reason)

    def test_topic_gates_tolerate_missing_asked_list(self):
        # 本场还没记录任何考点时不应误报重复
        for gate in (agents.gate_topic_duplicate, agents.gate_new_question):
            with self.subTest(gate=gate.__name__):
                self.assertTrue(gate({"action": "ask", "topic": "并发"}, None)[0])

    def test_reply_gate_rejects_blank(self):
        self.assertTrue(agents.gate_reply({"reply": "你刚说的那点展开讲讲？"})[0])
        for blank in ["", "   ", None, {}]:
            with self.subTest(reply=blank):
                self.assertFalse(agents.gate_reply({"reply": blank})[0])


class TestUsageMetering(unittest.TestCase):
    """token 计量：DESIGN 反复强调"省成本"，这里把计量本身钉住。"""

    def setUp(self):
        agents.reset_usage()

    def tearDown(self):
        agents.reset_usage()

    def test_reset_zeroes_everything(self):
        snap = agents.usage_snapshot()
        self.assertEqual(snap, {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0})

    def test_records_usage_from_response(self):
        class Usage:
            prompt_tokens = 120
            completion_tokens = 30

        class Response:
            usage = Usage()

        agents._record_usage(Response())
        snap = agents.usage_snapshot()
        self.assertEqual(snap["calls"], 1)
        self.assertEqual(snap["prompt_tokens"], 120)
        self.assertEqual(snap["completion_tokens"], 30)

    def test_accumulates_across_calls(self):
        class Usage:
            prompt_tokens = 10
            completion_tokens = 5

        class Response:
            usage = Usage()

        for _ in range(3):
            agents._record_usage(Response())
        snap = agents.usage_snapshot()
        self.assertEqual(snap["calls"], 3)
        self.assertEqual(snap["prompt_tokens"], 30)

    def test_missing_usage_does_not_raise(self):
        # 有些兼容端点不返回 usage：计量绝不能因此报错
        class Response:
            pass

        agents._record_usage(Response())
        self.assertEqual(agents.usage_snapshot()["calls"], 0)

    def test_partial_usage_is_tolerated(self):
        class Usage:
            prompt_tokens = 42

        class Response:
            usage = Usage()

        agents._record_usage(Response())
        snap = agents.usage_snapshot()
        self.assertEqual(snap["calls"], 1, "调用次数照常计")
        self.assertEqual(snap["prompt_tokens"], 42)
        self.assertEqual(snap["completion_tokens"], 0)

    def test_snapshot_is_a_copy(self):
        snap = agents.usage_snapshot()
        snap["calls"] = 999
        self.assertEqual(
            agents.usage_snapshot()["calls"], 0, "调用方改不动内部状态"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
