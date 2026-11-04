"""voice.py 的离线单测：口播文本清理、音频校验、错误分类。

不联网、不调用 edge-tts、不需要 API key —— 只测纯函数部分。
真机合成需要网络，属于 `_verify_live` 那类手动验证的范围。
"""

import array
import io
import math
import time
import types
import unittest
import wave

import voice


class TestTtsTextMarkdown(unittest.TestCase):
    """tts_text 剥 markdown：提示词已经要求模型不要用 Markdown，
    这里兜住模型没听话的情况，否则星号/短横线会被逐字念出来。"""

    def test_plain_text_untouched(self):
        self.assertEqual(voice.tts_text("你说的这个我了解。"), "你说的这个我了解。")

    def test_bold_removed(self):
        self.assertEqual(voice.tts_text("**重点**是缓存命中率。"), "重点是缓存命中率。")

    def test_italic_removed(self):
        self.assertEqual(voice.tts_text("*斜体*内容"), "斜体内容")

    def test_unordered_list(self):
        self.assertEqual(voice.tts_text("- 第一点\n- 第二点"), "第一点，第二点")

    def test_ordered_list(self):
        self.assertEqual(voice.tts_text("1. 先做A\n2. 再做B"), "先做A，再做B")

    def test_heading_marker(self):
        self.assertEqual(voice.tts_text("## 结论\n就这样。"), "结论，就这样。")

    def test_inline_code_keeps_content(self):
        self.assertEqual(voice.tts_text("用了 `Redis` 做缓存。"), "用了 Redis 做缓存。")

    def test_link_keeps_text(self):
        self.assertEqual(voice.tts_text("见[这里](http://x.com)。"), "见这里。")

    def test_code_block_dropped(self):
        # 代码块整块丢弃，但不留下「：，」这种连用
        self.assertEqual(voice.tts_text("看这段：\n```py\nprint(1)\n```\n明白吗"), "看这段：明白吗")

    def test_unclosed_bold_stripped(self):
        self.assertEqual(voice.tts_text("这个**没闭合"), "这个没闭合")

    def test_residual_markers_become_space(self):
        # 没被成对规则吃掉的散落记号统一变空格，不会被念出来
        self.assertEqual(voice.tts_text("a|b^c"), "a b c")


class TestTtsTextWhitespace(unittest.TestCase):
    """换行和空格对合成没有意义，处理不好会留下怪停顿。"""

    def test_multiple_newlines_collapse(self):
        self.assertEqual(voice.tts_text("第一行。\n\n\n第二行。"), "第一行。第二行。")

    def test_newline_after_punctuation_no_comma(self):
        # 前一个字符已是标点，就不再补逗号
        self.assertEqual(voice.tts_text("第一行。\n第二行"), "第一行。第二行")

    def test_newline_between_text_adds_comma(self):
        self.assertEqual(voice.tts_text("第一句\n第二句"), "第一句，第二句")

    def test_cjk_spaces_collapsed(self):
        self.assertEqual(voice.tts_text("你 说 的 对"), "你说的对")

    def test_latin_spaces_preserved(self):
        # 英文单词间的空格必须保留
        self.assertEqual(voice.tts_text("use Redis cache"), "use Redis cache")

    def test_leading_trailing_stripped(self):
        self.assertEqual(voice.tts_text("  你好  "), "你好")

    def test_trailing_comma_stripped(self):
        self.assertEqual(voice.tts_text("你好，"), "你好")


class TestTtsTextEdgeCases(unittest.TestCase):
    def test_empty_string(self):
        self.assertEqual(voice.tts_text(""), "")

    def test_none_safe(self):
        self.assertEqual(voice.tts_text(None), "")

    def test_code_block_only_yields_empty(self):
        # 全是代码 → 清完为空 → synth 会直接返回 b""，不发请求
        self.assertEqual(voice.tts_text("```\nonly code\n```"), "")

    def test_non_string_coerced(self):
        self.assertEqual(voice.tts_text(123), "123")


class TestAudioValidation(unittest.TestCase):
    """is_valid_audio：防止把 b"" 或错误页当成音频塞给 st.audio。"""

    def test_empty_rejected(self):
        self.assertFalse(voice.is_valid_audio(b""))

    def test_none_rejected(self):
        self.assertFalse(voice.is_valid_audio(None))

    def test_mp3_frame_sync_accepted(self):
        self.assertTrue(voice.is_valid_audio(b"\xff\xfb\x90\x00"))

    def test_mp3_frame_sync_variants(self):
        self.assertTrue(voice.is_valid_audio(b"\xff\xfa\x00\x00"))
        self.assertTrue(voice.is_valid_audio(b"\xff\xf3\x00\x00"))

    def test_bare_id3_rejected(self):
        # 只有 3 字节的 "ID3" 不是可播音频
        self.assertFalse(voice.is_valid_audio(b"ID3"))

    def test_id3_tagged_mp3_accepted(self):
        # 硅基流动 CosyVoice2 返回的 mp3 首字节是 "ID3"（ID3v2 标签，含 TXXX 水印帧），
        # 音频帧在标签之后。当年这条被误判成"非 MP3 数据"，
        # 连续两次后 app.py 直接停用了整场语音——合成成功，用户却一句都没听到。
        tag = b"ID3\x03\x00\x00\x00\x00\x00\x14" + b"\x00" * 20  # 标签体 0x14=20 字节
        self.assertTrue(voice.is_valid_audio(tag + b"\xff\xfb\x90\x00" + b"\x00" * 64))

    def test_id3_tag_only_rejected(self):
        # 只有标签、标签之后没有音频数据 → 仍算无效
        self.assertFalse(voice.is_valid_audio(b"ID3\x03\x00\x00\x00\x00\x00\x14" + b"\x00" * 20))

    def test_truncated_id3_rejected(self):
        # 标签头都不完整（不足 10 字节）→ 无效
        self.assertFalse(voice.is_valid_audio(b"ID3\x03\x00\x00\x00"))

    def test_html_rejected(self):
        self.assertFalse(voice.is_valid_audio(b"<html>error</html>"))

    def test_too_short_rejected(self):
        self.assertFalse(voice.is_valid_audio(b"\xff"))


class TestPermanentEdgeError(unittest.TestCase):
    """区分「这个音色/文本永远不行」和「这次网络抖了」：
    前者要立刻换后端，后者值得重试一次。"""

    def test_403_is_permanent(self):
        self.assertTrue(voice._is_permanent_edge_error(Exception("Invalid response status: 403")))

    def test_401_is_permanent(self):
        self.assertTrue(voice._is_permanent_edge_error(Exception("Invalid response status: 401")))

    def test_404_is_permanent(self):
        self.assertTrue(voice._is_permanent_edge_error(Exception("Invalid response status: 404")))

    def test_timeout_is_transient(self):
        self.assertFalse(voice._is_permanent_edge_error(Exception("timed out")))

    def test_connection_reset_is_transient(self):
        self.assertFalse(voice._is_permanent_edge_error(Exception("Connection reset by peer")))

    def test_500_is_transient(self):
        # 服务端 5xx 是瞬时故障，值得重试
        self.assertFalse(voice._is_permanent_edge_error(Exception("Invalid response status: 500")))


class TestCallTimeouts(unittest.TestCase):
    """三类语音调用的超时必须分开，且都不许让 SDK 偷偷重试。

    背景（实测 2026-10-24）：三个调用点原先共用 `REQUEST_TIMEOUT = 60`，
    而 OpenAI SDK 默认 `max_retries=2`，于是一次失败要等 3 × 60 = 180 秒——
    `voice.synthesize()` 实测卡了 184.6 秒才抛「两条通道都失败」，
    面试官每说一句界面就冻三分钟。这里把契约钉死。
    """

    def setUp(self):
        self._orig = voice.OpenAI
        self.sink = {}

    def tearDown(self):
        voice.OpenAI = self._orig

    def _install_fake(self):
        sink = self.sink

        class _Speech:
            def create(self, **kwargs):
                sink["speech"] = kwargs
                return types.SimpleNamespace(content=b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 32)

        class _Transcriptions:
            def create(self, **kwargs):
                sink["transcribe"] = kwargs
                return types.SimpleNamespace(text="转写结果")

        class _Completions:
            def create(self, **kwargs):
                sink["chat"] = kwargs
                return types.SimpleNamespace(
                    choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="修正后"))]
                )

        class _Chat:
            completions = _Completions()

        class _Audio:
            speech = _Speech()
            transcriptions = _Transcriptions()

        class _Client:
            def __init__(self, **kwargs):
                sink["client"] = kwargs
                self.audio = _Audio()
                self.chat = _Chat()

        voice.OpenAI = _Client

    def test_tts_timeout_is_interactive_speed(self):
        # 合成是「说完一句要出声」，必须明显短于转写，也不能大于 30 秒
        self.assertLessEqual(voice.TTS_TIMEOUT, 30.0)
        self.assertLess(voice.TTS_TIMEOUT, voice.REQUEST_TIMEOUT)

    def test_correct_timeout_is_interactive_speed(self):
        self.assertLessEqual(voice.CORRECT_TIMEOUT, voice.REQUEST_TIMEOUT)

    def test_synth_passes_tts_timeout_and_no_retries(self):
        self._install_fake()
        voice._synth_siliconflow("你好", voice.DEFAULT_VOICE, "+0%", "sk-test")
        self.assertEqual(self.sink["client"].get("max_retries"), 0, "SDK 会自己重试 2 次，必须显式关掉")
        # 单个模型的上限必须短于整体 TTS_TIMEOUT，否则模型链兜底就失去意义
        self.assertEqual(self.sink["speech"]["timeout"], voice.SF_TTS_ATTEMPT_TIMEOUT)
        self.assertLessEqual(voice.SF_TTS_ATTEMPT_TIMEOUT, voice.TTS_TIMEOUT)

    def test_usable_model_is_tried_first(self):
        # 2026-10-24 实测：CosyVoice2 合法请求零响应，MOSS-TTSD 1.2 秒出声。
        # 把能用的排在前面，坏了的那次等才花得值。
        self.assertEqual(voice.SF_TTS_MODELS[0], "fnlp/MOSS-TTSD-v0.5")
        self.assertIn("FunAudioLLM/CosyVoice2-0.5B", voice.SF_TTS_MODELS)

    def test_falls_through_to_the_next_model(self):
        # 第一个模型抛异常时必须换下一个，而不是直接失败
        self._install_fake()
        calls = []

        class _Flaky:
            def create(self, **kwargs):
                calls.append(kwargs["model"])
                if len(calls) == 1:
                    raise TimeoutError("模拟上游挂起")
                return types.SimpleNamespace(
                    content=b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 32
                )

        voice.OpenAI = lambda **kwargs: types.SimpleNamespace(
            audio=types.SimpleNamespace(speech=_Flaky())
        )
        audio = voice._synth_siliconflow("你好", voice.DEFAULT_VOICE, "+0%", "sk-test")
        self.assertTrue(voice.is_valid_audio(audio))
        self.assertEqual(calls, list(voice.SF_TTS_MODELS[:2]))

    def test_raises_with_each_model_when_all_fail(self):
        def _boom(**kwargs):
            raise TimeoutError("都挂了")

        voice.OpenAI = lambda **kwargs: types.SimpleNamespace(
            audio=types.SimpleNamespace(speech=types.SimpleNamespace(create=_boom))
        )
        with self.assertRaises(RuntimeError) as ctx:
            voice._synth_siliconflow("你好", voice.DEFAULT_VOICE, "+0%", "sk-test")
        message = str(ctx.exception)
        for model in voice.SF_TTS_MODELS:
            self.assertIn(model, message)

    def test_transcribe_does_not_retry(self):
        self._install_fake()
        voice.transcribe(b"RIFF....WAVE", "sk-test")
        self.assertEqual(self.sink["client"].get("max_retries"), 0)
        self.assertEqual(self.sink["transcribe"]["timeout"], voice.REQUEST_TIMEOUT)

    def test_correct_asr_uses_shorter_timeout(self):
        self._install_fake()
        voice.correct_asr("负节点是红色的", "红黑树插入", "sk-test")
        self.assertEqual(self.sink["client"].get("max_retries"), 0)
        self.assertEqual(self.sink["chat"]["timeout"], voice.CORRECT_TIMEOUT)


class TestToneArgs(unittest.TestCase):
    """语速/音调参数：必须带正负号，edge-tts 的 --rate 要求 '+'/'-' 前缀。"""

    def test_has_sign(self):
        rate, pitch = voice._tone_args("B")
        self.assertRegex(rate, r"^[+-]\d+%$")
        self.assertRegex(pitch, r"^[+-]\d+Hz$")

    def test_all_levels(self):
        for level in voice._TONE:
            rate, pitch = voice._tone_args(level)
            self.assertRegex(rate, r"^[+-]\d+%$")
            self.assertRegex(pitch, r"^[+-]\d+Hz$")

    def test_unknown_level_falls_back(self):
        rate, pitch = voice._tone_args("不存在的档位")
        self.assertRegex(rate, r"^[+-]\d+%$")
        self.assertRegex(pitch, r"^[+-]\d+Hz$")

    def test_mood_shifts_tone(self):
        # 同一档位下，不同情绪应给出不同的基准（随机抖动 ±3 之外还有情绪偏移）
        rates = set()
        for mood in voice._MOOD_TONE:
            for _ in range(30):
                rate, _p = voice._tone_args("B", mood)
                rates.add(int(rate.rstrip("%")))
        self.assertGreater(len(rates), 1, "情绪没有影响语速")

    def test_rate_within_reasonable_range(self):
        # 语速不能离谱到听不清
        for level in voice._TONE:
            for mood in voice._MOOD_TONE:
                for _ in range(10):
                    rate, pitch = voice._tone_args(level, mood)
                    r = int(rate.rstrip("%"))
                    p = int(pitch.rstrip("Hz"))
                    self.assertGreaterEqual(r, -50)
                    self.assertLessEqual(r, 50)
                    self.assertGreaterEqual(p, -50)
                    self.assertLessEqual(p, 50)


class TestZhipuTts(unittest.TestCase):
    """智谱 GLM-TTS：音色映射、未知音色的兜底、WAV 校验、通道优先级。

    背景（2026-10-24 实测）：edge-tts 403、硅基流动 CosyVoice2 卡在推理阶段，
    智谱成了唯一稳定出声的通道（34 字约 2.9 秒）。两个坑记在这里：
    ① 只接受 wav / pcm，mp3 会 400 1214；
    ② **会校验音色名**，乱填返回 400 1214「音色不存在」——我一开始误判成
       「乱填也能过」，实测用 `anna` 推翻了自己的结论。
    """

    # 实测可用、且被服务端接受过的音色名（白名单的唯一来源）
    MEASURED = {
        "tongtong",
        "chuichui",
        "xiaochen",
        "jam",
        "kazi",
        "douji",
        "luodo",
        "female",
        "male",
    }

    def setUp(self):
        self._orig_openai = voice.OpenAI
        # 熔断状态按 Key 分桶，快照整个表再清空：用例之间不能互相漏桶。
        self._state = {name: dict(state) for name, state in voice._breakers.items()}
        self._edge = voice._edge_down_until
        voice._breakers.clear()
        voice._edge_down_until = time.time() + 3600  # 别让测试真去连 edge

    def tearDown(self):
        voice.OpenAI = self._orig_openai
        voice._breakers.clear()
        voice._breakers.update(self._state)
        voice._edge_down_until = self._edge

    def _install(self, sink, content=None):
        payload = content if content is not None else (
            b"RIFF\x24\x08\x00\x00WAVEfmt " + b"\x00" * 32
        )

        class _Speech:
            def create(self, **kwargs):
                sink.append(kwargs)
                return types.SimpleNamespace(content=payload)

        voice.OpenAI = lambda **kwargs: types.SimpleNamespace(
            audio=types.SimpleNamespace(speech=_Speech())
        )

    def test_yunyang_is_gone_everywhere(self):
        # 用户 2026-10-24 决定砍掉「播报腔」，三张表都不能残留
        self.assertNotIn("zh-CN-YunyangNeural", voice.VOICES.values())
        self.assertNotIn("zh-CN-YunyangNeural", voice.SF_VOICES)
        self.assertNotIn("zh-CN-YunyangNeural", voice.ZHIPU_VOICES)
        self.assertNotIn("charles", voice.SF_VOICES.values())

    def test_every_selectable_voice_has_a_zhipu_tone(self):
        # 界面上能选几个，映射表就得覆盖几个，否则选到某个就没声音
        self.assertEqual(set(voice.ZHIPU_VOICES), set(voice.VOICES.values()))

    def test_zhipu_tones_are_whitelisted(self):
        self.assertTrue(set(voice.ZHIPU_VOICES.values()) <= self.MEASURED)

    def test_unknown_voice_falls_back_to_default(self):
        sink = []
        self._install(sink)
        audio = voice._synth_zhipu("测试", "zh-CN-UnknownNeural", "+0%", "k")
        self.assertTrue(voice.is_valid_audio(audio))
        self.assertEqual(sink[0]["voice"], voice.ZHIPU_VOICES[voice.DEFAULT_VOICE])

    def test_unknown_voice_error_retries_with_default(self):
        calls = []

        def _create(**kwargs):
            calls.append(kwargs["voice"])
            if len(calls) == 1:
                raise RuntimeError('400: {"code":"1214","message":"音色不存在"}')
            return types.SimpleNamespace(content=b"RIFF\x24\x08\x00\x00WAVEfmt " + b"\x00" * 32)

        # 必须用**非默认**音色：默认音色再回落到默认就是原地打转，代码会直接抛
        voice.OpenAI = lambda **kwargs: types.SimpleNamespace(
            audio=types.SimpleNamespace(speech=types.SimpleNamespace(create=_create))
        )
        audio = voice._synth_zhipu("测试", "zh-CN-YunxiNeural", "+0%", "k")
        self.assertTrue(voice.is_valid_audio(audio))
        self.assertEqual(len(calls), 2, "音色不存在应当换默认音色再试一次")
        self.assertEqual(calls[1], voice.ZHIPU_VOICES[voice.DEFAULT_VOICE])

    def test_other_errors_are_not_retried(self):
        calls = []

        def _create(**kwargs):
            calls.append(kwargs)
            raise TimeoutError("Request timed out.")

        voice.OpenAI = lambda **kwargs: types.SimpleNamespace(
            audio=types.SimpleNamespace(speech=types.SimpleNamespace(create=_create))
        )
        with self.assertRaises(TimeoutError):
            voice._synth_zhipu("测试", voice.DEFAULT_VOICE, "+0%", "k")
        self.assertEqual(len(calls), 1, "超时不该拿默认音色再撞一次")

    def test_request_shape(self):
        sink = []
        self._install(sink)
        voice._synth_zhipu("测试", voice.DEFAULT_VOICE, "+0%", "k")
        self.assertEqual(sink[0]["model"], voice.ZHIPU_TTS_MODEL)
        # 智谱不接受 mp3，写错格式整条通道就废了
        self.assertEqual(sink[0]["response_format"], "wav")
        self.assertEqual(sink[0]["timeout"], voice.ZHIPU_TTS_TIMEOUT)
        # 语速必须跟着难度档/情绪走，否则 A 档和 C 档在语音上没区别
        self.assertEqual(sink[0]["speed"], 1.0)

    def test_rate_becomes_speed(self):
        sink = []
        self._install(sink)
        voice._synth_zhipu("测试", voice.DEFAULT_VOICE, "+8%", "k")
        voice._synth_zhipu("测试", voice.DEFAULT_VOICE, "-6%", "k")
        self.assertEqual(sink[0]["speed"], 1.08)
        self.assertEqual(sink[1]["speed"], 0.94)

    def test_wav_is_valid_audio(self):
        wav = b"RIFF\x24\x08\x00\x00WAVEfmt " + b"\x00" * 64
        self.assertTrue(voice.is_valid_audio(wav))
        # 只有 44 字节头、没有 PCM 数据的不算
        self.assertFalse(voice.is_valid_audio(b"RIFF" + b"\x00" * 40))

    def test_audio_format_routes_by_container(self):
        wav = b"RIFF\x24\x08\x00\x00WAVEfmt " + b"\x00" * 64
        mp3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff" * 64
        self.assertEqual(voice.audio_format(wav), "audio/wav")
        self.assertEqual(voice.audio_format(mp3), "audio/mp3")
        self.assertEqual(voice.audio_format(None), "audio/mp3")

    def test_synthesize_prefers_zhipu(self):
        called = []

        def _fake(text, voice_id, rate, key):
            called.append(voice_id)
            return b"RIFF\x24\x08\x00\x00WAVEfmt " + b"\x00" * 64

        orig = voice._synth_zhipu
        voice._synth_zhipu = _fake
        try:
            audio = voice.synthesize("你好", voice.DEFAULT_VOICE, zhipu_key="k")
        finally:
            voice._synth_zhipu = orig
        self.assertTrue(voice.is_valid_audio(audio))
        self.assertEqual(called, [voice.DEFAULT_VOICE])

    def test_one_transient_failure_does_not_trip_the_breaker(self):
        # 偶发一次超时不该让接下来一分钟全都放弃合成
        def _boom(*_a, **_k):
            raise TimeoutError("Request timed out.")

        orig = voice._synth_zhipu
        voice._synth_zhipu = _boom
        try:
            with self.assertRaises(RuntimeError):
                voice.synthesize("你好", zhipu_key="k")
        finally:
            voice._synth_zhipu = orig
        self.assertEqual(voice.breaker_until("zhipu", "k"), 0.0, "第一次瞬时失败不该熔断")
        self.assertEqual(voice._breaker("zhipu", "k")["streak"], 1)

    def test_two_transient_failures_trip_the_breaker(self):
        def _boom(*_a, **_k):
            raise TimeoutError("Request timed out.")

        orig = voice._synth_zhipu
        voice._synth_zhipu = _boom
        try:
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    voice.synthesize("你好", zhipu_key="k")
        finally:
            voice._synth_zhipu = orig
        self.assertGreater(voice.breaker_until("zhipu", "k"), time.time())
        # 熔断期内不再尝试，报错要说明是熔断而不是又失败一次
        with self.assertRaises(RuntimeError) as ctx:
            voice.synthesize("你好", zhipu_key="k")
        self.assertIn("熔断", str(ctx.exception))

    def test_reset_clears_zhipu_breaker(self):
        voice._mark_zhipu_down("k", permanent=True)
        self.assertGreater(voice.breaker_until("zhipu", "k"), time.time())
        voice.reset_edge_breaker()
        self.assertEqual(voice.breaker_until("zhipu", "k"), 0.0)
        self.assertEqual(voice._breaker("zhipu", "k")["streak"], 0)

    # —— 多会话隔离：熔断必须按 Key 分桶 ——

    def test_breaker_is_bucketed_by_key(self):
        # 别人的 Key 余额不足（长熔断），不该把你的通道一起停掉
        voice._mark_zhipu_down("bad-key", permanent=True)
        self.assertGreater(voice.breaker_until("zhipu", "bad-key"), time.time())
        self.assertEqual(
            voice.breaker_until("zhipu", "good-key"), 0.0,
            "另一把 Key 不该受连累",
        )

    def test_transient_streak_is_not_shared_across_keys(self):
        # 两把 Key 各失败一次，不能凑成「连续两次」而熔断
        voice._mark_zhipu_down("key-a", permanent=False, transient_is_fatal=False)
        voice._mark_zhipu_down("key-b", permanent=False, transient_is_fatal=False)
        self.assertEqual(voice.breaker_until("zhipu", "key-a"), 0.0)
        self.assertEqual(voice.breaker_until("zhipu", "key-b"), 0.0)

    def test_reset_only_clears_the_given_keys(self):
        # 「重试语音合成」是会话级操作：只清自己的 Key，别人的保护要留着
        voice._mark_zhipu_down("mine", permanent=True)
        voice._mark_zhipu_down("theirs", permanent=True)
        voice.reset_edge_breaker("mine")
        self.assertEqual(voice.breaker_until("zhipu", "mine"), 0.0)
        self.assertGreater(voice.breaker_until("zhipu", "theirs"), time.time())


class TestStripLeadTone(unittest.TestCase):
    """裁掉合成音频开头的提示音（2026-11-04「每次说话前嘟嘟」报障的修复）。

    这批用例不联网：用合成波形复现实测到的结构，钉住三件事——
    该裁的裁干净、不该裁一个字都不能动、裁完仍是合法且参数不变的 WAV。
    """

    RATE = 24000

    # ---- 波形构造 ----
    def _tone(self, ms: int, hertz: float = 550.0, amp: float = 1788.0) -> list[int]:
        # amp 取 1788 → 正弦 RMS ≈ 1264，与实测提示音一致
        n = int(self.RATE * ms / 1000)
        return [int(amp * math.sin(2 * math.pi * hertz * i / self.RATE)) for i in range(n)]

    def _silence(self, ms: int) -> list[int]:
        return [0] * int(self.RATE * ms / 1000)

    def _speech(self, ms: int) -> list[int]:
        """伪人声：基频游走、幅度每 20ms 跳变。

        幅度要明显离开提示音的 1264（这里 2400/3400 → RMS 1697/2404），
        否则会被判据认成"幅度稳定的纯音"——真实人声也不会长时间维持同一幅度。
        """
        n = int(self.RATE * ms / 1000)
        out = []
        for i in range(n):
            freq = 200 + 150 * math.sin(2 * math.pi * 3 * i / self.RATE)
            amp = 2400 if (i // 480) % 2 == 0 else 3400
            out.append(int(amp * math.sin(2 * math.pi * freq * i / self.RATE)))
        return out

    def _wav(self, samples: list[int], channels: int = 1) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as handle:
            handle.setnchannels(channels)
            handle.setsampwidth(2)
            handle.setframerate(self.RATE)
            data = samples
            if channels == 2:
                data = []
                for value in samples:
                    data += [value, value]
            handle.writeframes(
                array.array("h", [max(-32768, min(32767, v)) for v in data]).tobytes()
            )
        return buf.getvalue()

    def _lead(self) -> list[int]:
        """复现实测的前导结构：纯音与静音交替，共 1440ms。"""
        return (
            self._tone(120) + self._silence(160)
            + self._tone(340) + self._silence(660)
            + self._tone(120) + self._silence(40)
        )

    def _seconds(self, audio: bytes) -> float:
        with wave.open(io.BytesIO(audio), "rb") as handle:
            return handle.getnframes() / handle.getframerate()

    # ---- 用例 ----
    def test_beep_before_speech_is_cut(self):
        beep = self._wav(self._lead() + self._speech(600))
        out = voice.strip_lead_tone(beep)
        self.assertNotEqual(out, beep, "带提示音的音频必须被处理")
        self.assertAlmostEqual(self._seconds(out), 0.6, delta=0.12)

    def test_trimmed_audio_keeps_wav_params(self):
        out = voice.strip_lead_tone(self._wav(self._lead() + self._speech(600)))
        with wave.open(io.BytesIO(out), "rb") as handle:
            self.assertEqual(handle.getnchannels(), 1)
            self.assertEqual(handle.getsampwidth(), 2)
            self.assertEqual(handle.getframerate(), self.RATE)
        self.assertTrue(voice.is_valid_audio(out))

    def test_stereo_channels_survive(self):
        out = voice.strip_lead_tone(self._wav(self._lead() + self._speech(600), channels=2))
        with wave.open(io.BytesIO(out), "rb") as handle:
            self.assertEqual(handle.getnchannels(), 2)
        self.assertAlmostEqual(self._seconds(out), 0.6, delta=0.12)

    def test_audio_without_beep_is_left_alone(self):
        # 开头直接是人声（第一帧幅度就跳）——一个字节都不许动
        plain = self._wav(self._speech(800))
        self.assertEqual(voice.strip_lead_tone(plain), plain)

    def test_steady_vowel_opening_is_left_alone(self):
        # 女声稳态元音：前几帧幅度稳、频率也可能落在 450~650，
        # 但撑不满半秒，所以不该被当成提示音切掉字头
        steady = []
        for i in range(int(self.RATE * 0.15)):
            steady.append(int(1788 * math.sin(2 * math.pi * 300 * i / self.RATE)))
        audio = self._wav(steady + self._speech(600))
        self.assertEqual(voice.strip_lead_tone(audio), audio)

    def test_all_beep_is_left_alone(self):
        # 整段都是提示音（没有任何人声）：裁完会变空音频，宁可不裁
        only = self._wav(self._lead())
        self.assertEqual(voice.strip_lead_tone(only), only)

    def test_non_wav_bytes_pass_through(self):
        mp3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb" + b"\x00" * 200
        self.assertEqual(voice.strip_lead_tone(mp3), mp3)
        self.assertEqual(voice.strip_lead_tone(b""), b"")

    def test_truncated_wav_does_not_raise(self):
        broken = self._wav(self._lead() + self._speech(600))[:60]
        self.assertEqual(voice.strip_lead_tone(broken), broken)


if __name__ == "__main__":
    unittest.main(verbosity=2)

