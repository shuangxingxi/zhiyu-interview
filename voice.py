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
import re
import sys
import time

import edge_tts
from openai import OpenAI

SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
ASR_MODEL = "FunAudioLLM/SenseVoiceSmall"
SF_TTS_MODEL = "FunAudioLLM/CosyVoice2-0.5B"

# 识别后纠错用的模型：复用项目已经接入的 DeepSeek，不新增依赖
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
ASR_CORRECT_MODEL = "deepseek-flash"

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
_sf_down_until = 0.0

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

    传进来的文本先过一遍 `tts_text` 清掉 markdown 记号：提示词要求面试官
    不要用 Markdown，但模型不总照做，漏出来的星号会被逐字念出来。
    """
    text = tts_text(text)
    if not text:
        return b""
    rate, pitch = _tone_args(level, mood)

    edge_error: Exception | None = None
    if time.time() >= _edge_down_until:
        # 瞬时故障重试、永久故障立刻放弃：把两类错误分开处理，
        # 否则一次网络抖动就会和"403 永久拒绝"一样把 edge 停用十分钟。
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

    if fallback_key and time.time() >= _sf_down_until:
        ok = False
        sf_error: Exception | None = None
        try:
            audio = _synth_siliconflow(text, voice, rate, fallback_key)
            ok = is_valid_audio(audio)
            if not ok:
                sf_error = RuntimeError("硅基流动返回空音频或非 MP3 数据")
        except Exception as exc:  # noqa: BLE001
            sf_error = exc
        if ok:
            return audio
        # 「返回了不可用的音频」按永久故障处理：接口说成功但给不出能播的 MP3，
        # 多半是这个音色在这把 Key 下根本不可用，不必每分钟再去白等五秒。
        unusable_output = sf_error is None or "空音频" in str(sf_error)
        _mark_sf_down(
            permanent=unusable_output or _is_permanent_edge_error(sf_error)
        )
        raise RuntimeError(
            f"edge-tts 与硅基流动均合成失败：edge={edge_error}; sf={sf_error}"
        ) from sf_error

    if edge_error is not None:
        raise edge_error
    if fallback_key:
        raise RuntimeError("edge-tts 与硅基流动都处于熔断期，暂时无法合成语音")
    raise RuntimeError("edge-tts 暂不可用（熔断期内），且未配置硅基流动 Key")


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
    if data.startswith(_ID3_MAGIC):
        # 标签之后必须还有音频数据：只有标签、或标签长度本身超出整段数据，
        # 都说明拿到的不是可播音频
        end = _id3_tag_end(data)
        return end > 0 and len(data) > end
    return data[0] == _MP3_MAGIC


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


def reset_edge_breaker() -> None:
    """手动解除 edge-tts 与硅基流动的熔断。

    熔断本来是为了避免每轮都去撞一个已经不可用的接口；但用户在侧边栏
    点「重试语音合成」时必须真的重新试一次，否则点了也还是走兜底/直接跳过，
    看起来就像按钮坏了。
    """
    global _edge_down_until, _sf_down_until
    _edge_down_until = 0.0
    _sf_down_until = 0.0


def _mark_sf_down(permanent: bool) -> None:
    """标记硅基流动进入熔断期（与 edge-tts 同一套软/硬两档策略）。"""
    global _sf_down_until
    window = _SF_RETRY_AFTER_HARD if permanent else _SF_RETRY_AFTER_SOFT
    _sf_down_until = time.time() + window
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
