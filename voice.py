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
import random
import sys
import time

import edge_tts
from openai import OpenAI

SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
ASR_MODEL = "FunAudioLLM/SenseVoiceSmall"
SF_TTS_MODEL = "FunAudioLLM/CosyVoice2-0.5B"

# 识别后纠错用的模型：复用项目已经接入的 DeepSeek，不新增依赖
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
ASR_CORRECT_MODEL = "deepseek-chat"

# 单次网络调用的超时（秒）。SDK 默认 600 秒，语音是交互场景，
# 卡住十分钟不如快速失败——调用方一律有回退路径（回退文字输入 / 原文）。
REQUEST_TIMEOUT = 60.0

# 音色表：界面下拉显示中文名，底层是 edge-tts 的 voice id
VOICES: dict[str, str] = {
    "女声 · 晓晓（亲和自然）": "zh-CN-XiaoxiaoNeural",
    "男声 · 云希（沉稳）": "zh-CN-YunxiNeural",
    "女声 · 晓伊（年轻）": "zh-CN-XiaoyiNeural",
    "男声 · 云健（浑厚）": "zh-CN-YunjianNeural",
    "男声 · 云扬（播报腔）": "zh-CN-YunyangNeural",
}

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

# edge voice id -> 硅基流动 CosyVoice2 系统音色（语气尽量对齐，如"沉稳"对"沉稳"）
SF_VOICES: dict[str, str] = {
    "zh-CN-XiaoxiaoNeural": "claire",  # 温柔女声
    "zh-CN-YunxiNeural": "alex",  # 沉稳男声
    "zh-CN-XiaoyiNeural": "diana",  # 欢快女声（年轻感）
    "zh-CN-YunjianNeural": "benjamin",  # 低沉男声（浑厚）
    "zh-CN-YunyangNeural": "charles",  # 磁性男声（播报腔）
}

# edge-tts 被拒后静默多久不再重试（秒）。期间直接用硅基流动合成，避免每句白等。
_EDGE_RETRY_AFTER = 600
_edge_down_until = 0.0

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


def transcribe(audio_bytes: bytes, api_key: str) -> str:
    """把一段 wav 录音转成文字。

    录音由 st.audio_input 产出，本身就是 PCM WAV，和这个接口天然匹配，不需要转码。
    """
    client = OpenAI(api_key=api_key, base_url=SILICONFLOW_BASE_URL)
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
    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
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
        timeout=REQUEST_TIMEOUT,
    )
    corrected = (resp.choices[0].message.content or "").strip()
    return corrected or raw_text


def synthesize(
    text: str,
    voice: str = DEFAULT_VOICE,
    level: str = "B",
    mood: str = "",
    fallback_key: str = "",
) -> bytes:
    """把文字合成成 mp3 字节：edge-tts 优先，失败自动换硅基流动（需传 fallback_key）。

    edge-tts 是异步接口，而 Streamlit 的脚本线程里没有事件循环，
    所以这里用 asyncio.run 起一个临时循环收完整个音频流。
    """
    rate, pitch = _tone_args(level, mood)

    edge_error: Exception | None = None
    if time.time() >= _edge_down_until:
        try:
            return asyncio.run(_synth(text, voice, rate, pitch))
        except Exception as exc:  # 网络、403、空音频……一律进入兜底
            edge_error = exc
            _mark_edge_down()

    if fallback_key:
        try:
            return _synth_siliconflow(text, voice, rate, fallback_key)
        except Exception as exc:
            raise RuntimeError(
                f"edge-tts 与硅基流动均合成失败：edge={edge_error}; sf={exc}"
            ) from exc

    if edge_error is not None:
        raise edge_error
    raise RuntimeError("edge-tts 暂不可用（熔断期内），且未配置硅基流动 Key")


def _mark_edge_down() -> None:
    """标记 edge-tts 进入熔断期，并往 stderr 留一行提示（方便排查）。"""
    global _edge_down_until
    _edge_down_until = time.time() + _EDGE_RETRY_AFTER
    print(
        f"[TTS] edge-tts 不可用，{_EDGE_RETRY_AFTER}s 内改用硅基流动合成",
        file=sys.stderr,
    )


def _synth_siliconflow(text: str, voice: str, rate: str, api_key: str) -> bytes:
    """硅基流动 CosyVoice2 合成。

    音色要写成 "模型名:音色名" 的形式（官方要求带模型前缀）；
    语速用 speed 近似 edge-tts 的 rate 百分比（1.0 = 原速）。
    """
    client = OpenAI(api_key=api_key, base_url=SILICONFLOW_BASE_URL)
    speed = min(max(1.0 + int(rate.rstrip("%")) / 100.0, 0.25), 4.0)
    result = client.audio.speech.create(
        model=SF_TTS_MODEL,
        voice=f"{SF_TTS_MODEL}:{SF_VOICES.get(voice, SF_VOICES[DEFAULT_VOICE])}",
        input=text,
        response_format="mp3",
        speed=round(speed, 2),
        timeout=REQUEST_TIMEOUT,
    )
    return result.content


async def _synth(text: str, voice: str, rate: str, pitch: str) -> bytes:
    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    buffer = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buffer.extend(chunk["data"])
    return bytes(buffer)
