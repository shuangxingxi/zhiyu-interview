"""模型调用容错层（agents.chat_json / _loads_lenient）的单元测试。

被测对象：`agents._loads_lenient` 与 `agents.chat_json` 的重试逻辑。

为什么单独测这一层：`chat_json` 是全项目**所有**模型调用的唯一入口，
它挂了就等于面试挂了。而它的重试分支此前**从未被真正走到**——
`test_tool_call.py` 里的 `FakeChat` 直接把 `chat_json` 整个替换掉了，
所以"第一次返回坏 JSON、第二次返回好的"这条路径没有任何测试。

DeepSeek 确实会偶发返回空内容（finish_reason='stop'、content 全是空格），
对话轮数变多后更容易出现——这是真实存在的服务端行为，必须钉住。

测试原则：
    1. 全部离线：patch `agents._client` 返回假客户端，不碰网络、不花 token；
    2. 断言"行为"而不是"实现"：重点是重试次数、重试时降温、以及最终抛错；
    3. 顺带钉住计量：每次尝试都要计入 usage，否则成本统计会漏。

运行方式（项目根目录，零新增依赖）：
    py -3.12 -m unittest test_chat_json -v
"""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

import agents


@contextlib.contextmanager
def quiet_stderr():
    """chat_json 失败时会往 stderr 打日志，测试里静音掉，保持输出干净。"""
    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer):
        yield buffer


class FakeResponse:
    """假响应：只要 chat_json 读到的那几个字段。"""

    def __init__(self, content, finish_reason="stop", usage=None):
        message = type("Message", (), {"content": content})()
        choice = type("Choice", (), {"message": message, "finish_reason": finish_reason})()
        self.choices = [choice]
        self.usage = usage


class FakeUsage:
    def __init__(self, prompt_tokens=10, completion_tokens=5):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class FakeClient:
    """假客户端：按预设顺序返回响应，并记录每次收到的参数。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError(
                f"假客户端被调用了第 {len(self.calls)} 次，但预设响应已经用完"
            )
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class TestLoadsLenient(unittest.TestCase):
    """容错解析：模型经常把 JSON 包在代码块里，或前后带一句话。"""

    def test_plain_json(self):
        self.assertEqual(agents._loads_lenient('{"a": 1}'), {"a": 1})

    def test_fenced_json(self):
        self.assertEqual(agents._loads_lenient('```json\n{"a": 1}\n```'), {"a": 1})

    def test_fenced_without_language(self):
        self.assertEqual(agents._loads_lenient('```\n{"a": 1}\n```'), {"a": 1})

    def test_json_embedded_in_prose(self):
        # 模型偶尔会在 JSON 前后多写一句话
        text = '好的，这是我的判断：{"action": "ask"} 以上。'
        self.assertEqual(agents._loads_lenient(text), {"action": "ask"})

    def test_empty_content_raises(self):
        for empty in (None, "", "   ", "\n\n"):
            with self.subTest(content=empty):
                with self.assertRaises(ValueError):
                    agents._loads_lenient(empty)

    def test_unparsable_raises_with_excerpt(self):
        # 报错要带上原文，否则只有一句没有信息量的 json 报错
        with self.assertRaises(ValueError) as ctx:
            agents._loads_lenient("这完全不是 JSON")
        self.assertIn("这完全不是 JSON", str(ctx.exception))

    def test_empty_error_mentions_the_cause(self):
        with self.assertRaises(ValueError) as ctx:
            agents._loads_lenient("")
        self.assertIn("空内容", str(ctx.exception))


class TestChatJsonRetry(unittest.TestCase):
    """重试逻辑：这是此前从未被覆盖的分支。"""

    def setUp(self):
        agents.reset_usage()

    def tearDown(self):
        agents.reset_usage()

    def _patch(self, client):
        # _client 现在带 timeout 参数（报告类角色放宽 read 超时），
        # 假客户端不关心它，用 *args 吞掉即可。
        return mock.patch.object(agents, "_client", lambda *args, **kwargs: client)

    def test_first_attempt_success_calls_model_once(self):
        client = FakeClient(FakeResponse('{"ok": true}'))
        with self._patch(client):
            result = agents.chat_json([{"role": "user", "content": "hi"}], "key")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(client.calls), 1, "第一次就成功不该重试")

    def test_retries_after_empty_content(self):
        # 这正是 DeepSeek 的已知偶发行为：content 全是空格
        client = FakeClient(
            FakeResponse("   "),
            FakeResponse('{"ok": true}'),
        )
        with self._patch(client), quiet_stderr():
            result = agents.chat_json([{"role": "user", "content": "hi"}], "key")
        self.assertEqual(result, {"ok": True}, "第二次应恢复")
        self.assertEqual(len(client.calls), 2)

    def test_retries_after_malformed_json(self):
        client = FakeClient(
            FakeResponse("不是 JSON"),
            FakeResponse('{"ok": true}'),
        )
        with self._patch(client), quiet_stderr():
            result = agents.chat_json([{"role": "user", "content": "hi"}], "key")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(client.calls), 2)

    def test_temperature_drops_on_retry(self):
        # 重试时降温，让它更"守规矩"
        client = FakeClient(FakeResponse("坏的"), FakeResponse('{"ok": true}'))
        with self._patch(client), quiet_stderr():
            agents.chat_json(
                [{"role": "user", "content": "hi"}], "key", temperature=0.7
            )
        self.assertEqual(client.calls[0]["temperature"], 0.7, "首次用调用方给的温度")
        self.assertEqual(client.calls[1]["temperature"], 0.1, "重试必须降温")

    def test_raises_after_all_attempts_exhausted(self):
        client = FakeClient(*[FakeResponse("坏的") for _ in range(3)])
        with self._patch(client), quiet_stderr():
            with self.assertRaises(ValueError) as ctx:
                agents.chat_json([{"role": "user", "content": "hi"}], "key")
        self.assertEqual(len(client.calls), 3, "应当尝试满 3 次")
        self.assertIn("连续 3 次", str(ctx.exception))

    def test_custom_attempt_count_is_respected(self):
        client = FakeClient(*[FakeResponse("坏的") for _ in range(5)])
        with self._patch(client), quiet_stderr():
            with self.assertRaises(ValueError):
                agents.chat_json(
                    [{"role": "user", "content": "hi"}], "key", attempts=5
                )
        self.assertEqual(len(client.calls), 5)

    def test_network_error_propagates_without_retry(self):
        # 网络异常不是"返回坏内容"，不该被吞掉当作可重试——由上层决定怎么处理
        client = FakeClient(RuntimeError("连接被重置"))
        with self._patch(client):
            with self.assertRaises(RuntimeError):
                agents.chat_json([{"role": "user", "content": "hi"}], "key")
        self.assertEqual(len(client.calls), 1)

    def test_request_shape_is_correct(self):
        client = FakeClient(FakeResponse('{"ok": true}'))
        with self._patch(client):
            agents.chat_json(
                [{"role": "user", "content": "hi"}], "key", model="deepseek-chat"
            )
        call = client.calls[0]
        self.assertEqual(call["model"], "deepseek-chat")
        self.assertEqual(call["response_format"], {"type": "json_object"})
        self.assertEqual(call["messages"], [{"role": "user", "content": "hi"}])

    def test_client_fails_fast_without_retry(self):
        # 超时与重试策略挂在客户端上，而不是单次请求上：
        # read 超时压到 FIRST_RESPONSE_TIMEOUT，且 max_retries=0——
        # 否则一次"服务器没响应"会被 SDK 的默认重试放大成数倍等待。
        # 构造 OpenAI 客户端不发网络请求，可以离线断言。
        client = agents._client("key")
        self.assertEqual(client.timeout.read, agents.FIRST_RESPONSE_TIMEOUT)
        self.assertEqual(client.max_retries, 0)

    def test_timeout_raises_readable_error_without_retry(self):
        # 超时不是"返回坏内容"，不该进重试分支；要立刻抛出可读报错。
        from openai import APITimeoutError

        client = FakeClient(APITimeoutError(request=mock.Mock()))
        with self._patch(client):
            with quiet_stderr():
                with self.assertRaises(RuntimeError) as ctx:
                    agents.chat_json([{"role": "user", "content": "hi"}], "key")
        self.assertIn("没有响应", str(ctx.exception))
        self.assertEqual(len(client.calls), 1, "超时不该重试")


class TestRoleTimeouts(unittest.TestCase):
    """超时要按角色分：交互路径等 15 秒，报告类角色等 90 秒。

    这是一个真实缺陷的回归钉：原先所有角色共用 15 秒 read 超时，而
    coach_review 实测 23.0s、advisor_review 22.8s、resume_review 27.5s，
    连试 6 次 6 次超时——三个功能在当前网络下等于必然失败。
    """

    def setUp(self):
        agents.reset_usage()

    def tearDown(self):
        agents.reset_usage()

    def test_interaction_path_keeps_the_short_timeout(self):
        client = agents._client("key")
        self.assertEqual(client.timeout.read, agents.FIRST_RESPONSE_TIMEOUT)
        self.assertEqual(client.max_retries, 0, "超时不该被 SDK 重试放大")

    def test_report_path_gets_a_much_longer_read_timeout(self):
        client = agents._client("key", agents.REPORT_TIMEOUT)
        self.assertEqual(client.timeout.read, agents.REPORT_RESPONSE_TIMEOUT)
        self.assertGreater(
            agents.REPORT_RESPONSE_TIMEOUT,
            agents.FIRST_RESPONSE_TIMEOUT * 3,
            "报告类角色的输出是面试官决策的数倍，余量要给足",
        )
        self.assertEqual(client.max_retries, 0)

    def test_connect_timeout_is_shared(self):
        # 只有 read 按角色分；连接阶段的超时对两者都该是同一个短值。
        self.assertEqual(agents.MODEL_TIMEOUT.connect, agents.REPORT_TIMEOUT.connect)
        self.assertEqual(agents.REPORT_TIMEOUT.connect, 10.0)

    def test_chat_json_passes_the_timeout_to_the_client(self):
        seen: list = []
        client = FakeClient(FakeResponse('{"ok": true}'), FakeResponse('{"ok": true}'))

        def fake_client(api_key, timeout=None):
            seen.append(timeout)
            return client

        with mock.patch.object(agents, "_client", fake_client):
            agents.chat_json([{"role": "user", "content": "hi"}], "key")
            agents.chat_json(
                [{"role": "user", "content": "hi"}], "key", timeout=agents.REPORT_TIMEOUT
            )

        self.assertIs(seen[0], agents.MODEL_TIMEOUT, "不传就用交互路径的默认值")
        self.assertIs(seen[1], agents.REPORT_TIMEOUT)

    def test_timeout_error_reports_the_limit_that_was_used(self):
        """报错里的秒数必须是实际生效的那个，否则用户会以为卡了 15 秒。"""
        from openai import APITimeoutError

        client = FakeClient(APITimeoutError(request=mock.Mock()))
        with mock.patch.object(agents, "_client", lambda *a, **k: client):
            with quiet_stderr():
                with self.assertRaises(RuntimeError) as ctx:
                    agents.chat_json(
                        [{"role": "user", "content": "hi"}],
                        "key",
                        timeout=agents.REPORT_TIMEOUT,
                    )
        message = str(ctx.exception)
        self.assertIn("90 秒", message)
        self.assertNotIn("15 秒", message)


class TestChatJsonUsage(unittest.TestCase):
    """计量：每次尝试都要计入，否则成本统计会漏。"""

    def setUp(self):
        agents.reset_usage()

    def tearDown(self):
        agents.reset_usage()

    def _patch(self, client):
        return mock.patch.object(agents, "_client", lambda *args, **kwargs: client)

    def test_successful_call_is_counted(self):
        client = FakeClient(FakeResponse('{"ok": true}', usage=FakeUsage(100, 20)))
        with self._patch(client):
            agents.chat_json([{"role": "user", "content": "hi"}], "key")

        snap = agents.usage_snapshot()
        self.assertEqual(snap["calls"], 1)
        self.assertEqual(snap["prompt_tokens"], 100)
        self.assertEqual(snap["completion_tokens"], 20)

    def test_failed_attempts_are_also_counted(self):
        # 失败的那次调用照样花了 token，不计量就会低估成本
        client = FakeClient(
            FakeResponse("坏的", usage=FakeUsage(50, 10)),
            FakeResponse('{"ok": true}', usage=FakeUsage(50, 10)),
        )
        with self._patch(client), quiet_stderr():
            agents.chat_json([{"role": "user", "content": "hi"}], "key")

        snap = agents.usage_snapshot()
        self.assertEqual(snap["calls"], 2, "两次尝试都要计")
        self.assertEqual(snap["prompt_tokens"], 100)

    def test_response_without_usage_is_tolerated(self):
        client = FakeClient(FakeResponse('{"ok": true}', usage=None))
        with self._patch(client):
            result = agents.chat_json([{"role": "user", "content": "hi"}], "key")
        self.assertEqual(result, {"ok": True}, "没有 usage 也不该影响正常返回")
        self.assertEqual(agents.usage_snapshot()["calls"], 0)


class TestThinkingSwitch(unittest.TestCase):
    """交互轮要关思考：deepseek-flash 默认开，单轮 2~4 秒才出字，
    而且官方明确 thinking 下 temperature 不生效——重试降温度是空转的。"""

    def _patch(self, client):
        return mock.patch.object(agents, "_client", lambda *args, **kwargs: client)

    def test_thinking_disabled_is_passed_through(self):
        client = FakeClient(FakeResponse('{"ok": true}'))
        with self._patch(client):
            agents.chat_json([{"role": "user", "content": "hi"}], "key", think=False)
        self.assertEqual(
            client.calls[0].get("extra_body"), {"thinking": {"type": "disabled"}}
        )

    def test_thinking_stays_on_by_default(self):
        # 报告类角色要保留思考：不发字段就是服务端默认（开）
        client = FakeClient(FakeResponse('{"ok": true}'))
        with self._patch(client):
            agents.chat_json([{"role": "user", "content": "hi"}], "key")
        self.assertNotIn("extra_body", client.calls[0])


class TestUsageMeterIsolation(unittest.TestCase):
    """多会话隔离：Streamlit 一个进程跑所有标签页，计量必须各记各的账。

    旧实现用一个模块级累加器：B 一开局 reset 就会把 A 的账清零，
    A 结束时读到的又是两个人的混合值。这里钉住"按会话绑定"这条契约。
    """

    def tearDown(self):
        agents.bind_usage_meter(None)

    def _patch(self, client):
        return mock.patch.object(agents, "_client", lambda *args, **kwargs: client)

    def test_two_sessions_do_not_overwrite_each_other(self):
        a = agents.new_usage_meter()
        b = agents.new_usage_meter()

        agents.bind_usage_meter(a)
        agents.reset_usage()  # A 开局
        with self._patch(
            FakeClient(FakeResponse('{"ok": true}', usage=FakeUsage(100, 20)))
        ):
            agents.chat_json([{"role": "user", "content": "hi"}], "key")

        agents.bind_usage_meter(b)
        agents.reset_usage()  # B 开局：不能抹掉 A 的账
        with self._patch(
            FakeClient(FakeResponse('{"ok": true}', usage=FakeUsage(7, 3)))
        ):
            agents.chat_json([{"role": "user", "content": "hi"}], "key")

        self.assertEqual(agents.usage_snapshot(a)["calls"], 1)
        self.assertEqual(agents.usage_snapshot(a)["prompt_tokens"], 100)
        self.assertEqual(agents.usage_snapshot(b)["calls"], 1)
        self.assertEqual(agents.usage_snapshot(b)["prompt_tokens"], 7)

    def test_unbound_calls_fall_back_to_default_meter(self):
        # 没绑定会话时（单测、脚本直调）也不能崩，落在默认账上
        agents.bind_usage_meter(None)
        agents.reset_usage()
        with self._patch(
            FakeClient(FakeResponse('{"ok": true}', usage=FakeUsage(5, 1)))
        ):
            agents.chat_json([{"role": "user", "content": "hi"}], "key")
        self.assertEqual(agents.usage_snapshot()["calls"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
