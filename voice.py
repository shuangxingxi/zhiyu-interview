"""语音能力：语音识别（硅基流动 SenseVoice）与语音合成（edge-tts + 硅基流动兜底）。

设计说明（对应 DESIGN.md 第九节技术栈）：
    DeepSeek 本身不提供语音接口，所以两头各接一个外部能力——
        识别 -> 硅基流动 SenseVoice（OpenAI 兼容，直接复用已装的 openai 包）
        合成 -> edge-tts（免费、无需 key，中文音色自然）
                + 硅基流动 CosyVoice2 兜底（同一把硅基流动 Key，应对 edge-tts 被拒）

为什么要有兜底：edge-tts 走的是微软"朗读"接口，2026-10 起该接口开始拒绝第三方
客户端（WSS 握手 403，见 rany2/edge-tts#490）。这不是本机问题，重装升级都无效。
为了让演示不受影响，edge-tts 失败时自动改用硅基流动的 CosyVoice2 合成
（复用识别用的同一把 Key，零新增依赖）；成功后 10 分钟内不再重试 edge，
避免每句都先白等一次失败。

这两个函数只负责"把事做了"，不做任何兜底：出错就抛异常，
由 app.py 决定是提示用户还是静默降级回文字模式。
"""

from __future__ import annotations

import asyncio
import array
import hashlib
import io
import math
import random
import re
import sys
import time
import wave

import edge_tts
from openai import OpenAI

SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
ASR_MODEL = "FunAudioLLM/SenseVoiceSmall"

# 硅基流动的 TTS 模型，**按顺序试**，第一个成功就返回。
#
# v17 实测（2026-10-24，同一把 Key、同一个端点）：
#   · `FunAudioLLM/CosyVoice2-0.5B`：参数校验正常（换假 voice 0.4 秒返 400
#     `Invalid voice`、换假 model 0.3 秒返 400 `Model does not exist`），
#     但请求合法时**一个字节都不返回**——Alex / Claire / Diana / Benjamin /
#     Charles 五个音色全部在 25~180 秒处读超时；
#   · 同一把 Key 调 `/v1/chat/completions` 0.5 秒 200、调
#     `/v1/audio/transcriptions` 25 秒转写成功 —— 网关、鉴权、网络都没问题，
#     卡的是这个模型的推理服务；
#   · `fnlp/MOSS-TTSD-v0.5`：1.2 秒出声，五个音色全部可用且互不相同。
# 所以把能用的排第一，坏的那个留作兜底（服务端状态会变，哪天可能反过来）。
SF_TTS_MODELS: tuple[str, ...] = (
    "fnlp/MOSS-TTSD-v0.5",
    "FunAudioLLM/CosyVoice2-0.5B",
)
# 兼作默认值，保留旧名字以免外部引用断掉
SF_TTS_MODEL = SF_TTS_MODELS[0]

# 识别后纠错用的模型：复用项目已经接入的 DeepSeek，不新增依赖
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
ASR_CORRECT_MODEL = "deepseek-flash"

# 单次网络调用的超时（秒）。SDK 默认 600 秒，语音是交互场景，
# 卡住十分钟不如快速失败——调用方一律有回退路径（回退文字输入 / 原文）。
#
# v17 修正：这里原先把「转写」「纠错」「合成」三类调用压成同一个 60 秒，
# 而 SDK 默认还会自己重试 2 次（max_retries=2），于是一次失败实际要等
# 3 × 60 = 180 秒。实测（2026-10-24，edge-tts 已被上游 403 封禁）：
# `voice.synthesize()` 整整卡了 184.6 秒才抛出「两条通道都失败」——
# 面试官每说一句，界面就冻三分钟。三类调用必须分开给：
#   · 合成（TTS）—— 说完一句就要出声，25 秒没回来就等于不可用；
#   · 转写（ASR）—— 用户说完一段话，60 秒可以接受；
#   · 纠错 —— 交互路径，20 秒，失败由调用方回退原文。
# 三者一律 `max_retries=0`：重试在这里是负收益，把「失败」变成「长时间卡住」。
REQUEST_TIMEOUT = 60.0
TTS_TIMEOUT = 25.0
CORRECT_TIMEOUT = 20.0

# 单个 TTS 模型给多久。用模型链兜底时，不能给每个都 25 秒——两个都挂就是 50 秒，
# 比整体 TTS_TIMEOUT 还长就失去意义了。10 秒够正常模型出声（实测 1.2 秒）。
SF_TTS_ATTEMPT_TIMEOUT = 10.0

# 音色表：界面下拉显示中文名，底层是 edge-tts 的 voice id
# （这套 id 现在只是「音色的内部标识」，edge 自己已经 403 用不上了，
#  智谱与硅基流动都靠下面的映射表把它们翻成各自的音色名。
#  「云扬（播报腔）」2026-10-24 按用户要求下线，四个音色已够用。）
VOICES: dict[str, str] = {
    "女声 · 晓晓（亲和自然）": "zh-CN-XiaoxiaoNeural",
    "男声 · 云希（沉稳）": "zh-CN-YunxiNeural",
    "女声 · 晓伊（年轻）": "zh-CN-XiaoyiNeural",
    "男声 · 云健（浑厚）": "zh-CN-YunjianNeural",
}

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

# edge voice id -> 硅基流动 CosyVoice2 系统音色（语气尽量对齐，如"沉稳"对"沉稳"）
SF_VOICES: dict[str, str] = {
    "zh-CN-XiaoxiaoNeural": "claire",  # 温柔女声
    "zh-CN-YunxiNeural": "alex",  # 沉稳男声
    "zh-CN-XiaoyiNeural": "diana",  # 欢快女声（年轻感）
    "zh-CN-YunjianNeural": "benjamin",  # 低沉男声（浑厚）
}

# 智谱 GLM-TTS。它是目前**唯一稳定出声**的通道（edge-tts 403、硅基流动的
# CosyVoice2 会卡在推理阶段），所以排在链首。
ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_TTS_MODEL = "glm-tts"

# edge voice id -> 智谱音色名（2026-10-24 由用户试听后指定：
# 晓晓=tongtong、云希=xiaochen、晓伊=female、云健=male）。
# 智谱**会校验音色名**（乱填返回 400 {"code":"1214","message":"音色不存在"}），
# 所以这张表就是白名单；下面 `_synth_zhipu` 还留了一层兜底，
# 防的是哪天官方把音色改名导致整场没声音。
ZHIPU_VOICES: dict[str, str] = {
    "zh-CN-XiaoxiaoNeural": "tongtong",  # 亲和自然
    "zh-CN-YunxiNeural": "xiaochen",  # 沉稳
    "zh-CN-XiaoyiNeural": "female",  # 年轻
    "zh-CN-YunjianNeural": "male",  # 浑厚
}

# 智谱只接受 wav 或 pcm（mp3/flac/opus 一律 400 1214）。
# 实测同一句 34 字：返回耗时中位数约 2.9 秒，最慢见过 41 秒（冷启动/排队）。
# 12 秒 = 中位数的 4 倍，慢到这份上就不值得让候选人干等了，直接换下一条通道。
ZHIPU_TTS_TIMEOUT = 12.0

# —— 熔断 ——
#
# 熔断要回答的问题是"这条通道现在还能不能打通"，而智谱 / 硅基流动能不能打通
# 取决于**用户在各自会话里填的那把 Key**。所以状态必须按 Key 分桶，
# 不能做成一个全局开关：
#   · A 的 Key 余额不足 → 全局熔断 600 秒 → B 的好 Key 也十分钟不被尝试，
#     B 听到的音色与延迟跟自己的配置对不上，界面上还没有任何解释；
#   · B 点一次「重试语音合成」→ 全局清零 → 拆掉 A 正在享受的"已知故障快速跳过"。
#
# edge-tts 是匿名接口、没有 Key，它的 403 对所有客户端一样，因此保持全局。
#
# 智谱熔断：余额不足（429 / code 1113）是账号级问题，长熔断；
# 超时、5xx 算瞬时。
_ZHIPU_RETRY_AFTER_HARD = 600
_ZHIPU_RETRY_AFTER_SOFT = 60
# 瞬时失败连续两次才熔断。实测 8 连发全部 1.7~3.1 秒，但偶发一次 12 秒超时
# （接线测试里真遇到过）。如果一次超时就熔断 60 秒，接下来一分钟里每轮
# 都只能靠下一条通道——用户听到的是"这一分钟都没声音"，比偶发慢一轮更糟。
_ZHIPU_FAIL_LIMIT = 2

# edge-tts 熔断时长（秒）。分两档：永久性故障（403 拒绝第三方客户端）长熔断，
# 瞬时故障（超时、连接重置、空音频）短熔断——只抖一下不该停掉十分钟。
_EDGE_RETRY_AFTER_HARD = 600
_EDGE_RETRY_AFTER_SOFT = 30
_edge_down_until = 0.0

# 兜底通道（硅基流动）的熔断时长。它同样是**同步阻塞**调用，实测一次失败
# 要花 5 秒上下；如果每轮都去撞一遍，用户等到的只是"更长的沉默"。
# 返回空音频/非 MP3 说明这个音色在这把 Key 下根本不可用，按永久故障长熔断；
# 超时、5xx、429 只算瞬时故障。
_SF_RETRY_AFTER_HARD = 600
_SF_RETRY_AFTER_SOFT = 60

# 分桶表：{"zhipu:ab12cd34": {"down_until": 时间戳, "streak": 连续瞬时失败数}}
# 键里的指纹只用于区分 Key，不保存 Key 本身，也不进日志。
_breakers: dict[str, dict[str, float]] = {}


def _key_fp(key: str) -> str:
    """Key 指纹（前 8 位十六进制）。同一个 Key 稳定，不同 Key 几乎不会撞。"""
    return hashlib.sha1((key or "").strip().encode("utf-8")).hexdigest()[:8]


def _breaker(provider: str, key: str) -> dict[str, float]:
    """取某把 Key 在某条通道上的熔断状态；没有就建一个空桶。"""
    name = f"{provider}:{_key_fp(key)}"
    state = _breakers.get(name)
    if state is None:
        state = {"down_until": 0.0, "streak": 0.0}
        _breakers[name] = state
    return state


def breaker_until(provider: str, key: str) -> float:
    """这条通道在这把 Key 上的熔断截止时间戳；0 表示未熔断。"""
    return _breaker(provider, key)["down_until"]

# 单句合成的有界重试次数。瞬时故障重试，永久故障不重试（重试也白搭）。
_EDGE_MAX_ATTEMPTS = 3

# MP3 帧以 0xFF 同步字开头。空字节串或非 MP3 头都算无效——
# 否则空音频会被 app.py 的 `if audio:` 静默吞掉：用户没声音，也不知道为什么。
_MP3_MAGIC = 0xFF
# 一个完整的 MP3 帧头是 4 字节；不足 4 字节的数据即使首字节是 0xFF 也不可播。
_MP3_HEADER_MIN = 4
# 但 MP3 允许在音频数据前挂 ID3v2 标签（硅基流动 CosyVoice2 返回的音频就带一段
# TXXX 水印帧），这种文件首字节是 "ID3" 而不是 0xFF，同样是可以播的音频。
_ID3_MAGIC = b"ID3"
_ID3_HEADER_LEN = 10
# 智谱 GLM-TTS 只给 WAV（RIFF 容器）。44 字节是标准 PCM 头的长度，
# 比它还短说明只有头没有音频数据。
_WAV_MAGIC = b"RIFF"
_WAV_HEADER_MIN = 44

# 智谱合成音频开头的提示音参数（实测 2026-11-04，见 strip_lead_tone）。
# 频率量出来约 550Hz，两侧各留 100Hz 余量；静音阈值取 -55dBFS 上下——
# 比它低的片段裁掉没有任何损失，比它高又恒定在 450~650Hz 的才是提示音。
_LEAD_TONE_HZ: tuple[float, float] = (450.0, 650.0)
_LEAD_SILENCE_RMS = 60.0
# 实测提示音最长 1.94 秒（三组文本 × 两个音色都在 1.84~1.94 之间，与文本无关）。
# 给到 2.5 秒是留余量：万一哪天服务端把等待音拉长，别裁到一半就收手。
_LEAD_SCAN_SECONDS = 2.5

# 语气由两条正交的轴叠加：难度档给基线，情绪状态给偏移。
# 值是 (语速偏移 %, 音调偏移 Hz)。每句再叠一点随机抖动，
# 免得同一状态下每一句都像同一个模子刻出来的。
_TONE: dict[str, tuple[int, int]] = {
    "A": (8, -4),
    "B": (0, 0),
    "C": (-6, 2),
}

# 键是 mood.py 派生出来的四种状态标签，没命中就不叠偏移
_MOOD_TONE: dict[str, tuple[int, int]] = {
    "投入": (-2, 1),
    "例行公事": (-4, 1),
    "急躁": (6, -3),
    "冷淡": (-5, -2),
}


def _tone_args(level: str, mood: str = "") -> tuple[str, str]:
    base_rate, pitch = _TONE.get(level, _TONE["B"])
    delta_rate, delta_pitch = _MOOD_TONE.get(mood, (0, 0))
    rate = base_rate + delta_rate + random.randint(-3, 3)
    return f"{rate:+d}%", f"{pitch + delta_pitch:+d}Hz"


# 口播前要剥掉的 markdown 记号。提示词里已经要求面试官「不要用 Markdown」，
# 但模型不总照做；一旦漏出 ** 或 - 列表，TTS 会把星号、短横线逐字念出来。
_MD_RULES: tuple[tuple[str, str], ...] = (
    (r"```[\s\S]*?```", " "),  # 代码块整块丢掉
    (r"`([^`]*)`", r"\1"),  # 行内代码留内容
    (r"!\[[^\]]*\]\([^)]*\)", " "),  # 图片整条丢掉
    (r"\[([^\]]*)\]\([^)]*\)", r"\1"),  # 链接留文字
    (r"^#{1,6}\s+", ""),  # 标题号
    (r"\*\*([^*]+)\*\*", r"\1"),  # 加粗
    (r"\*([^*]+)\*", r"\1"),  # 斜体
    (r"^[-*+]\s+", ""),  # 无序列表
    (r"^\d+\.\s+", ""),  # 有序列表
    (r"<\/?[a-zA-Z][^>]*>", " "),  # 残留 HTML 标签
)


def tts_text(text: str) -> str:
    """把准备朗读的文本清成纯口播文本。

    与 `agents.py` 提示词里的「不要用 Markdown」是**双保险**：
    提示词约束模型，这里兜住模型没听话的情况。剥完再压掉多余空白，
    否则标记符被替换成空格后会在句中留下奇怪的停顿。
    """
    out = str(text or "")
    for pattern, repl in _MD_RULES:
        out = re.sub(pattern, repl, out, flags=re.MULTILINE)
    # 标记符剥掉后残留的噪声字符（全角/半角都算），避免被逐字念出
    out = re.sub(r"[*_#|^=+~`]", " ", out)
    # 换行在朗读里没有意义，只在句中留下怪停顿。
    # 先把「含换行的整段空白」压成一个 \n（只吃一个会把 \n\n 的第二个 \n
    # 当成前导字符，误判成非标点而多补一个逗号），
    # 再按「换行前的字符是不是标点」决定补不补逗号：
    # 已经是标点就不再补，否则会念出「：，」这种连用。
    out = re.sub(r"[ \t]*\n[ \t\n]*", "\n", out)
    out = re.sub(r"([^。！？；：，、,.!?;:])\n", r"\1，", out)
    out = out.replace("\n", "")
    out = re.sub(r"\s{2,}", " ", out)
    # 汉字之间的空格对中文合成没有意义，塌掉避免怪停顿
    out = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", out)
    # 连续标点压成一个（换行替换可能和原文标点撞车）
    out = re.sub(r"([，,])[，,]+", r"\1", out)
    return out.strip().strip("，")


def transcribe(audio_bytes: bytes, api_key: str) -> str:
    """把一段 wav 录音转成文字。

    录音由 st.audio_input 产出，本身就是 PCM WAV，和这个接口天然匹配，不需要转码。
    """
    client = OpenAI(api_key=api_key, base_url=SILICONFLOW_BASE_URL, max_retries=0)
    result = client.audio.transcriptions.create(
        model=ASR_MODEL,
        file=("answer.wav", audio_bytes, "audio/wav"),
        timeout=REQUEST_TIMEOUT,
    )
    return (result.text or "").strip()


def correct_asr(raw_text: str, question: str, api_key: str) -> str:
    """用 DeepSeek 结合面试问题上下文，纠正语音识别的同音 / 近音错误。

    SenseVoiceSmall 是轻量模型，遇到中英文夹杂、技术名词、数字单位时容易听错，
    这里借项目已经接入的 DeepSeek 做一次轻量纠错。纠错失败由调用方回退原文，
    因此本函数不负责兜底，出错就抛异常。
    """
    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL, max_retries=0)
    messages = [
        {
            "role": "system",
            "content": (
                "你是语音转写纠错器。把我给的口述内容里的语音识别错误改正确，"
                "只输出修正后的完整文本，不要解释、不要加引号、不要标注改动。\n"
                "重点：同音 / 近音字、中英文混读产生的错词（技术名词、缩写、数字、版本号、单位）。\n"
                "只改明显听错的字词，保留原意和口语风格，不润色成书面语，不凭空补写内容。"
            ),
        },
        {"role": "user", "content": f"面试官的问题是：{question}\n\n口述原文：{raw_text}"},
    ]
    resp = client.chat.completions.create(
        model=ASR_CORRECT_MODEL,
        messages=messages,
        temperature=0.0,
        max_tokens=1500,
        timeout=CORRECT_TIMEOUT,
    )
    corrected = (resp.choices[0].message.content or "").strip()
    return corrected or raw_text


def synthesize(
    text: str,
    voice: str = DEFAULT_VOICE,
    level: str = "B",
    mood: str = "",
    fallback_key: str = "",
    zhipu_key: str = "",
) -> bytes:
    """把文字合成成音频：智谱 → 硅基流动 → edge-tts，逐级降级。

    顺序是实测定的（2026-10-24）：
    - **智谱 GLM-TTS**（`zhipu_key`）：唯一稳定出声的通道，34 字约 2.9 秒；
    - **硅基流动 MOSS-TTSD**（`fallback_key`）：CosyVoice2 的推理服务会卡死
      （校验 0.3 秒过、合成 120 秒无字节），MOSS 正常，1.2 秒出声；
    - **edge-tts**：自 2026-10 起对第三方客户端 403，放在最后只是留个念想。

    edge-tts 是异步接口，而 Streamlit 的脚本线程里没有事件循环，
    所以那一条用 asyncio.run 起临时循环收流。

    传进来的文本先过一遍 `tts_text` 清掉 markdown 记号：提示词要求面试官
    不要用 Markdown，但模型不总照做，漏出来的星号会被逐字念出来。

    智谱那条通道拿到的音频还会过一遍 `strip_lead_tone`：它在人声前贴了约
    1.9 秒的等待音，不裁掉就是每句话开口前"嘟…嘟"（2026-11-04 报障）。
    """
    text = tts_text(text)
    if not text:
        return b""
    rate, pitch = _tone_args(level, mood)
    errors: list[str] = []

    # —— 通道一：智谱 ——
    if zhipu_key:
        if time.time() >= breaker_until("zhipu", zhipu_key):
            try:
                audio = _synth_zhipu(text, voice, rate, zhipu_key)
                if is_valid_audio(audio):
                    _reset_zhipu_streak(zhipu_key)
                    return audio
                errors.append("智谱返回空音频或非 WAV 数据")
                # 接口说成功却给不出能播的音频，多半是账号/模型侧问题，长熔断
                _mark_zhipu_down(zhipu_key, permanent=True)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"智谱={exc}")
                _mark_zhipu_down(
                    zhipu_key,
                    permanent=_is_permanent_zhipu_error(exc),
                    transient_is_fatal=False,
                )
        else:
            errors.append("智谱处于熔断期")

    # —— 通道二：硅基流动 ——
    if fallback_key and time.time() >= breaker_until("sf", fallback_key):
        try:
            audio = _synth_siliconflow(text, voice, rate, fallback_key)
            if is_valid_audio(audio):
                return audio
            errors.append("硅基流动返回空音频或非 MP3 数据")
            _mark_sf_down(fallback_key, permanent=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"硅基流动={exc}")
            unusable_output = "空音频" in str(exc)
            _mark_sf_down(
                fallback_key,
                permanent=unusable_output or _is_permanent_edge_error(exc),
            )
    elif fallback_key:
        errors.append("硅基流动处于熔断期")

    # —— 通道三：edge-tts（目前必然 403，留在最后） ——
    if time.time() >= _edge_down_until:
        # 瞬时故障重试、永久故障立刻放弃：把两类错误分开处理，
        # 否则一次网络抖动就会和"403 永久拒绝"一样把 edge 停用十分钟。
        edge_error: Exception | None = None
        for attempt in range(_EDGE_MAX_ATTEMPTS):
            try:
                audio = asyncio.run(_synth(text, voice, rate, pitch))
                if is_valid_audio(audio):
                    return audio
                # 合成"成功"但拿到空流/坏帧：当作瞬时故障，重试后仍空才降级
                edge_error = RuntimeError("edge-tts 返回空音频或非 MP3 数据")
            except Exception as exc:  # noqa: BLE001
                edge_error = exc
                if _is_permanent_edge_error(exc):
                    _mark_edge_down(permanent=True)
                    break
            if attempt < _EDGE_MAX_ATTEMPTS - 1:
                time.sleep(0.4 * (attempt + 1))
        else:
            _mark_edge_down(permanent=False)
        if edge_error is not None:
            errors.append(f"edge-tts={edge_error}")
    else:
        errors.append("edge-tts 处于熔断期")

    if not errors:
        raise RuntimeError("没有可用的语音通道：先在「🎙️ 语音与动效」里填一个 API Key")
    raise RuntimeError("所有语音通道均失败：" + "；".join(errors))


def _is_permanent_zhipu_error(exc: Exception) -> bool:
    """智谱这个错误是永久性的吗？

    余额不足（429 / code 1113）与鉴权失败（401/403）是账号级问题，
    重试和换音色都没用，长熔断；超时、5xx 只算瞬时。
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    if "1113" in text or "余额不足" in text:
        return True
    for marker in ("401", "403", "unauthorized", "forbidden", "authentication"):
        if marker in text:
            return True
    return False


def _mark_zhipu_down(
    key: str, permanent: bool, *, transient_is_fatal: bool = True
) -> None:
    """记一次智谱失败（只记在这把 Key 的桶里）。永久错误立刻长熔断，
    瞬时错误要连续两次才熔断。

    `transient_is_fatal=False` 是 synthesize 里用的：偶发一次超时不该让
    接下来一分钟全都放弃合成，下一轮再试一次的成本（最多 12 秒）比
    "一分钟没声音"小得多。
    """
    state = _breaker("zhipu", key)
    if permanent:
        state["streak"] = 0.0
        state["down_until"] = time.time() + _ZHIPU_RETRY_AFTER_HARD
        return
    state["streak"] += 1
    if transient_is_fatal or state["streak"] >= _ZHIPU_FAIL_LIMIT:
        state["streak"] = 0.0
        state["down_until"] = time.time() + _ZHIPU_RETRY_AFTER_SOFT


def _reset_zhipu_streak(key: str) -> None:
    """合成成功，清掉这把 Key 的连续失败计数。"""
    _breaker("zhipu", key)["streak"] = 0.0


def _id3_tag_end(data: bytes) -> int:
    """ID3v2 标签的总长度（含 10 字节头）；不是完整标签则返回 0。

    4 个长度字节是 synchsafe 的（每字节只用低 7 位），按普通整数解析会算错。
    """
    if len(data) < _ID3_HEADER_LEN:
        return 0
    size = 0
    for byte in data[6:10]:
        size = (size << 7) | (byte & 0x7F)
    return _ID3_HEADER_LEN + size


def is_valid_audio(data: bytes) -> bool:
    """音频字节是否可用：非空，且是裸 MP3 帧头或「ID3v2 标签 + 音频数据」。

    这一步不能省。合成接口可能"成功"返回空流（例如英文音色读不了中文），
    空字节串传到界面会被 `if audio:` 静默吞掉——用户只是听不到声音，
    界面上没有任何提示，排查时也无从下手。

    只判首字节 0xFF 会让 `b"\\xff"` 这种残缺数据通过，所以要求至少有
    完整的帧头长度（4 字节）且首字节为 0xFF。但**不能因此把 ID3 开头的文件
    判死**：ID3v2 标签是 MP3 的标准元数据，硅基流动 CosyVoice2 返回的音频
    首字节正是 "ID3"（标签后才是帧数据）。2026-10-18 排查「面试官没有语音输出」
    时确认：把这类正常音频判成"非 MP3 数据"，会让 app.py 连续两次后直接
    停用整场语音——合成明明成功，用户却一句都没听到。
    `None` 先被 `bool` 挡住，否则 `len(None)` 会抛 TypeError。
    """
    if not bool(data) or len(data) < _MP3_HEADER_MIN:
        return False
    if data.startswith(_WAV_MAGIC):
        # 智谱返回的是 WAV：RIFF 头之后要有真实 PCM 数据，否则是空壳
        return len(data) > _WAV_HEADER_MIN
    if data.startswith(_ID3_MAGIC):
        # 标签之后必须还有音频数据：只有标签、或标签长度本身超出整段数据，
        # 都说明拿到的不是可播音频
        end = _id3_tag_end(data)
        return end > 0 and len(data) > end
    return data[0] == _MP3_MAGIC


def strip_lead_tone(audio: bytes) -> bytes:
    """裁掉合成音频开头自带的提示音与静音。

    2026-11-04 报障「每次说话前都有嘟嘟的声音」。逐帧量了四组文本 × 两个音色
    的智谱返回值，前导完全一致：约 550Hz 的恒定纯音（RMS 1264、过零率 22 / 20ms）
    与静音交替出现，最长一路拖到 **1.8 秒**才进人声。人与合成语音都发不出「频率
    恒定、幅度恒定」的音，所以这不是念错了字，是接口在音频前贴了等待音。
    听感就是开口前"嘟…嘟…嘟"几下。

    判定分三档，逐 20ms 帧：
        silent —— RMS 低于 -55dBFS，且出现在开头，裁掉无损；
        tone   —— 频率落在 450~650Hz **且**幅度与开头几帧几乎相同（提示音幅度恒定，
                  而人声同一个字的清音、爆破音幅度是跳变的）；
        other  —— 其余。不能见到 other 就停：提示音每段之间都有淡入淡出帧
                  （实测 280 / 640 / 1340 / 1460 / 1760ms 各一帧），停在那里只会
                  裁掉 0.12 秒，嘟嘟声照旧。

    所以 other 帧要看**往后 10 帧（200ms）**再决定：只有后面 mostly 都是 other、
    几乎没有 tone，才算真的进了人声。这条把「淡入淡出帧」（后面紧跟着成串 tone
    或静音）和「人声起点」（后面是清音→元音的连续变化）分开。

    两道保护，宁可不裁也不能误伤：
        ① 开头三帧必须都是 tone，否则认为这段音频本来就没有提示音，原样返回；
        ② 前导几乎占满整段（裁完不足一帧）时也原样返回，不产出空音频。

    只处理 16bit PCM WAV（智谱这条通道）。MP3 要解码才能裁，而项目不想为这点
    事引入音频解码依赖；硅基流动那条通道返回 MP3，原样返回。
    """
    if len(audio) < _WAV_HEADER_MIN or not audio.startswith(_WAV_MAGIC):
        return audio
    try:
        with wave.open(io.BytesIO(audio), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            raw = handle.readframes(handle.getnframes())
    except (wave.Error, EOFError):
        return audio
    if width != 2 or channels not in (1, 2) or rate <= 0:
        return audio

    usable = len(raw) - len(raw) % (width * channels)
    if usable < rate * width * channels // 50:  # 不足一帧，没什么可裁的
        return audio
    mono = array.array("h")
    mono.frombytes(raw[:usable])
    if channels == 2:
        mono = array.array("h", [(mono[i] + mono[i + 1]) // 2 for i in range(0, len(mono) - 1, 2)])

    span = max(1, rate // 50)                     # 20ms 一帧
    scanned = min(len(mono) // span, int(rate * _LEAD_SCAN_SECONDS / span))
    if scanned < 3:
        return audio

    # 每帧 (rms, 估计频率) 只算一次：后面要反复回看窗口。
    frames: list[tuple[float, float]] = []
    for index in range(scanned):
        segment = mono[index * span:(index + 1) * span]
        rms = math.sqrt(sum(v * v for v in segment) / len(segment))
        crossings = sum(1 for a, b in zip(segment, segment[1:]) if (a < 0) != (b < 0))
        frames.append((rms, crossings * rate / (2.0 * span)))

    def is_tone(rms: float, hertz: float, reference: float) -> bool:
        return (
            abs(rms - reference) <= max(40.0, reference * 0.06)
            and _LEAD_TONE_HZ[0] <= hertz <= _LEAD_TONE_HZ[1]
        )

    # 保护①：开头三帧都是提示音才算"这段带提示音"，否则不动。
    reference = frames[0][0]
    if reference <= _LEAD_SILENCE_RMS or not all(
        is_tone(rms, hertz, reference) for rms, hertz in frames[1:3]
    ):
        return audio

    window = 10                                    # 200ms 的回看窗口
    start = 0
    for index, (rms, hertz) in enumerate(frames):
        if rms <= _LEAD_SILENCE_RMS or is_tone(rms, hertz, reference):
            start = (index + 1) * span
            continue
        ahead = frames[index:index + window]
        tones = sum(1 for r, h in ahead if is_tone(r, h, reference))
        others = sum(1 for r, h in ahead if r > _LEAD_SILENCE_RMS and not is_tone(r, h, reference))
        if others >= len(ahead) * 0.4 and tones < len(ahead) * 0.2:
            break                                  # 进入人声，start 停在上一帧末尾
        start = (index + 1) * span                 # 提示音的淡入淡出帧，继续往后

    if start < rate // 2:                          # 前导不足 0.5 秒，不值得动刀
        # 实测提示音 1.84~1.94 秒，远长于这个门槛。设这道坎是为了挡住误伤：
        # 女声稳态元音（基频 300Hz 上下）也可能连着几帧"幅度稳、频率落在
        # 450~650"，但它们撑不满半秒——一旦真被认成提示音，切掉的就是字头。
        return audio
    if start >= len(mono) - span:
        return audio                               # 几乎整段都是前导，宁可不裁

    out = io.BytesIO()
    try:
        with wave.open(out, "wb") as handle:
            handle.setnchannels(channels)
            handle.setsampwidth(width)
            handle.setframerate(rate)
            handle.writeframes(raw[start * width * channels:])
    except (wave.Error, ValueError):
        return audio
    return out.getvalue()


def audio_format(data: bytes) -> str:
    """音频字节的 MIME 类型，给 st.audio / 头像动效用。

    不能让界面写死 "audio/mp3"：智谱给 WAV，写错格式有的浏览器不放。
    判不出来时回落到 mp3——历史上唯一的来源就是 MP3。
    """
    if isinstance(data, (bytes, bytearray)) and bytes(data[:4]) == _WAV_MAGIC:
        return "audio/wav"
    return "audio/mp3"


def _is_permanent_edge_error(exc: Exception) -> bool:
    """这个异常是永久性的（重试也不会好）吗？

    edge-tts 自 2026-10 起对第三方客户端拒绝握手（403，见 rany2/edge-tts#490），
    这类错误重试无意义，应当直接长熔断；超时、连接重置属于瞬时故障，值得重试。

    注意 `"invalid response status"` 这句措辞 403 和 500 都会用，所以不能只看措辞，
    必须先按状态码分流：429（限流）和 5xx（服务端故障）都是瞬时的，
    否则一次后端抖动就会把 edge 熔断好几分钟。
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    # 先按状态码判：429 / 5xx 一律算瞬时
    for code in _parse_status_codes(text):
        if code == 429 or 500 <= code <= 599:
            return False
    for marker in ("403", "401", "forbidden", "unauthorized", "invalid response status"):
        if marker in text:
            return True
    return False


_STATUS_RE = re.compile(r"\b([1-5]\d{2})\b")


def _parse_status_codes(text: str) -> tuple[int, ...]:
    """从异常文本里挖出可能的 HTTP 状态码。

    只在 100–599 之间取值，避免把 "read timeout 3000ms" 里的 3000 当成状态码。
    """
    return tuple(int(m) for m in _STATUS_RE.findall(text) if 100 <= int(m) <= 599)


def reset_edge_breaker(*keys: str) -> None:
    """手动解除熔断。

    熔断本来是为了避免每轮都去撞一个已经不可用的接口；但用户在侧边栏
    点「重试语音合成」时必须真的重新试一次，否则点了也还是走兜底/直接跳过，
    看起来就像按钮坏了。

    **只清当前会话的 Key**：这是会话级操作，顺手清空全局会把别人正在被保护的
    通道重新打开（他那边下一轮又要白等一次超时）。不带参数时清全部，
    留给单测和显式全局重置。
    """
    global _edge_down_until
    _edge_down_until = 0.0
    if not keys:
        _breakers.clear()
        return
    for key in keys:
        if not (key or "").strip():
            continue
        _breaker("zhipu", key).update({"down_until": 0.0, "streak": 0.0})
        _breaker("sf", key).update({"down_until": 0.0, "streak": 0.0})


def _mark_sf_down(key: str, permanent: bool) -> None:
    """标记硅基流动在这把 Key 上进入熔断期（与 edge-tts 同一套软/硬两档策略）。"""
    window = _SF_RETRY_AFTER_HARD if permanent else _SF_RETRY_AFTER_SOFT
    _breaker("sf", key)["down_until"] = time.time() + window
    print(
        f"[TTS] 硅基流动{'永久故障' if permanent else '瞬时故障'}，{window}s 内不再尝试",
        file=sys.stderr,
    )


def _mark_edge_down(permanent: bool) -> None:
    """标记 edge-tts 进入熔断期，并往 stderr 留一行提示（方便排查）。

    永久故障（403）熔断十分钟，瞬时故障只熔断半分钟——一次抖动不该
    让整场面试都用兜底音色。
    """
    global _edge_down_until
    window = _EDGE_RETRY_AFTER_HARD if permanent else _EDGE_RETRY_AFTER_SOFT
    _edge_down_until = time.time() + window
    kind = "永久故障" if permanent else "瞬时故障"
    print(
        f"[TTS] edge-tts {kind}，{window}s 内改用硅基流动合成",
        file=sys.stderr,
    )


def _is_unknown_voice_error(exc: Exception) -> bool:
    """智谱是否在说「音色不存在」（400 / code 1214）？

    实测：`voice="anna"`、`voice="zzz_definitely_not_a_voice"` 都返回
    `{"code":"1214","message":"音色不存在"}`；而 1214 同时被用在
    「不支持当前response_format值」上，所以还要看措辞里有「音色」。
    """
    text = f"{type(exc).__name__}: {exc}"
    return "1214" in text and "音色" in text


def _synth_zhipu(text: str, voice: str, rate: str, api_key: str) -> bytes:
    """智谱 GLM-TTS 合成，返回 WAV 字节。

    两个实测出来的坑：
    1. **只接受 wav / pcm**——mp3 / flac / opus 一律 400 1214「不支持当前response_format值」；
    2. **会校验音色名**——错名字 400 1214「音色不存在」（我一开始误判成
       「乱填也能过」，实测推翻：female / male 能过是因为它们本身就是官方音色）。
       所以映射表兜一层，真报了这个错就换默认音色再试一次，
       别让一个拼写错误让整场没声音。

    `rate` 是 edge-tts 那套「+8% / -6%」格式，换算成智谱的 speed 倍数：
    难度档与情绪本来就是这个项目区分快慢的唯一手段，丢掉它 A 档和 C 档
    在语音上就没区别了。`speed=1.2` 实测被接受，换算区间 0.85~1.22 也在安全范围内。
    """
    client = OpenAI(api_key=api_key, base_url=ZHIPU_BASE_URL, max_retries=0)
    speed = min(max(1.0 + int(rate.rstrip("%")) / 100.0, 0.5), 2.0)

    def _call(tone: str) -> bytes:
        result = client.audio.speech.create(
            model=ZHIPU_TTS_MODEL,
            voice=tone,
            input=text,
            response_format="wav",
            speed=round(speed, 2),
            timeout=ZHIPU_TTS_TIMEOUT,
        )
        # 智谱会在音频前贴一段 550Hz 提示音（见 strip_lead_tone），
        # 不裁掉就是每句话开口前"嘟…嘟"两下。
        return strip_lead_tone(result.content)

    tone = ZHIPU_VOICES.get(voice, ZHIPU_VOICES[DEFAULT_VOICE])
    try:
        return _call(tone)
    except Exception as exc:  # noqa: BLE001
        if not _is_unknown_voice_error(exc) or tone == ZHIPU_VOICES[DEFAULT_VOICE]:
            raise
        return _call(ZHIPU_VOICES[DEFAULT_VOICE])


def _synth_siliconflow(text: str, voice: str, rate: str, api_key: str) -> bytes:
    """硅基流动 TTS 合成：按 `SF_TTS_MODELS` 顺序试，第一个拿到能播的 MP3 就返回。

    音色要写成 "模型名:音色名" 的形式（官方要求带模型前缀，缺前缀会 400
    `Invalid voice`）。语速用 speed 近似 edge-tts 的 rate 百分比（1.0 = 原速）。

    为什么要模型链：2026-10-24 实测 CosyVoice2 的推理服务卡死（参数校验正常、
    合法请求零响应），换 MOSS-TTSD 1.2 秒就出声。把「哪个模型能用」写死成
    单点，等于上游一抖整个语音功能就没了。
    """
    client = OpenAI(
        api_key=api_key, base_url=SILICONFLOW_BASE_URL, max_retries=0
    )
    speed = min(max(1.0 + int(rate.rstrip("%")) / 100.0, 0.25), 4.0)
    per_model_timeout = min(TTS_TIMEOUT, SF_TTS_ATTEMPT_TIMEOUT)
    tone = SF_VOICES.get(voice, SF_VOICES[DEFAULT_VOICE])

    failures: list[str] = []
    for model in SF_TTS_MODELS:
        try:
            result = client.audio.speech.create(
                model=model,
                voice=f"{model}:{tone}",
                input=text,
                response_format="mp3",
                speed=round(speed, 2),
                timeout=per_model_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{model}: {type(exc).__name__}: {exc}")
            continue
        audio = result.content
        if is_valid_audio(audio):
            return audio
        failures.append(f"{model}: 返回空音频或非 MP3 数据")

    raise RuntimeError("硅基流动 TTS 各模型均失败：" + "；".join(failures))


async def _synth(text: str, voice: str, rate: str, pitch: str) -> bytes:
    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    buffer = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buffer.extend(chunk["data"])
    return bytes(buffer)
