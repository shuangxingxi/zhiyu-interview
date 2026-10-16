"""多场景多难度自适应 AI 虚拟面试智能体 —— v1 主程序

对应 DESIGN.md 第十三节的工程结构。v1 范围：文本面试 + 三级配置 + 三体 Agent + 复盘报告。

运行方式（Windows PowerShell，在项目目录下执行）：
    py -3.12 -m pip install -r requirements.txt
    py -3.12 -m streamlit run app.py
然后在浏览器打开的页面左侧填入 DeepSeek API Key 即可。
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

import streamlit as st

import history_store
import interview_state
import mood
import session_store
from agents import (
    coach_review,
    evaluate_interview,
    interviewer_turn,
    reset_usage,
    test_connection,
    usage_snapshot,
)
from avatar import avatar_component
from knowledge import (
    LEVELS,
    MAJORS,
    MOOD_MODES,
    MOOD_ORDER,
    SCENES,
    build_profile,
    level_labels,
    profile_summary,
)
from resume import ACCEPTED_TYPES, extract_text
from voice import DEFAULT_VOICE, VOICES, correct_asr, synthesize, transcribe

BASE_DIR = Path(__file__).resolve().parent
HISTORY_FILE = BASE_DIR / "history.json"

ACTION_LABELS = {"ask": "换新题", "probe": "追问", "reflect": "反馈", "end": "收尾"}

DEEPSEEK_KEY_ENV = "DEEPSEEK_API_KEY"
SILICONFLOW_KEY_ENV = "SILICONFLOW_API_KEY"


def load_api_key(env_name: str) -> str:
    """读取某个 API Key：进程环境变量 → Windows 用户级环境变量。

    加第二层回退是因为：如果应用是从"环境变量写入之前"就已经打开的终端里启动的，
    该进程的环境副本里没有这个变量，这时只能直接读注册表才拿得到。
    """
    key = os.getenv(env_name, "").strip()
    if key:
        return key
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
                value, _ = winreg.QueryValueEx(handle, env_name)
            return str(value).strip()
        except OSError:
            return ""
    return ""


def siliconflow_key() -> str:
    """硅基流动 Key：环境变量优先，其次是侧边栏手填的那个输入框。"""
    return st.session_state.sf_key or st.session_state.get("sf_key_input", "")


def _last_question() -> str:
    """取面试官最近一次提问的原文，作为语音纠错的上下文。"""
    for msg in reversed(st.session_state.history):
        if msg["role"] == "assistant":
            return msg["content"]
    return ""


def _correct_voice(raw: str) -> str:
    """语音识别结果再用 DeepSeek 结合题干轻量纠错；任何失败都回退原文，绝不中断。"""
    api_key = st.session_state.get("api_key", "")
    if not raw or not api_key:
        return raw
    try:
        return correct_asr(raw, _last_question(), api_key)
    except Exception:  # noqa: BLE001
        return raw


def _awaiting_interviewer() -> bool:
    """面试官该说话、但还没说出口——说明上一轮模型调用失败了。

    两种情形都算：
      - history 末尾停在候选人回答（本轮失败）→ 那句回答还在，可直接重试；
      - history 为空（开场就失败）→ 重试即重新开场。
    正常一轮结束时末尾必是 assistant，所以这里返回 False。
    """
    history = st.session_state.get("history") or []
    if not history:
        return True
    return history[-1].get("role") == "user"


st.set_page_config(page_title="知遇 · AI 虚拟面试官", page_icon="🎤", layout="wide")


# ---------------------------------------------------------------------------
# 记录落盘
#
# 读写与统计的实现在 history_store.py（与 Streamlit 解耦，可离线单测）。
# 这里保留薄封装，是因为界面代码读起来更顺，也因为下面几个调用点用惯了旧名字。
# ---------------------------------------------------------------------------


def load_records() -> list[dict]:
    return history_store.load_records(HISTORY_FILE)


def save_record(record: dict) -> None:
    history_store.append_record(HISTORY_FILE, record)


def recent_asked_topics(limit: int = 3) -> list[str]:
    """读取最近几场练习问过的考点，开新场时用来避开重复出题。

    历史文件缺失或损坏时返回空列表——去重只是锦上添花，绝不能因此挡住面试。
    """
    seen: list[str] = []
    for record in reversed(load_records()[-limit:]):
        for topic in record.get("asked_topics") or []:
            text = str(topic).strip()
            if text and text not in seen:
                seen.append(text)
    return seen


# ---------------------------------------------------------------------------
# 会话快照（断点续面）
#
# Streamlit 的 session_state 是纯内存态：浏览器一刷新，整场面试就没了。
# 这里把"进行中"的面试存成快照文件，刷新后可选择继续。
#
# 存储是**一场一个文件**（`.snapshots/<会话id>.json`，实现见 session_store.py）：
# 早先所有会话共用一个 `.session_snapshot.json`，开第二个标签页面试就会把
# 第一个标签页的快照顶掉。现在每个标签页写自己的文件，首页再从所有残留场次里
# 挑最近保存的那个来提示"继续上次面试"。旧版单文件仍会被扫到（只读兼容）。
#
# 明确不落盘的东西：
#   - 简历（resume / resume_chunks）：项目硬约束，简历只在内存里，绝不写磁盘
#   - 音频（audio_by_index / avatar_audio）：体积大且无意义，恢复后旧消息不再有声音
#   - API Key：一律从环境变量重读，密钥不进快照文件
# ---------------------------------------------------------------------------

# 需要写进快照的字段（其余字段要么能从环境重建，要么上面已说明不落盘）
_SNAPSHOT_FIELDS = (
    "cur_q",
    "probes_used",
    "reflects_in_a_row",
    "asked_topics",
    "mood_state",
    "mood_trace",
    "mood_choice",
    "voice_on",
    "voice_name",
    "avatar_on",
    "decisions",
    "history",
)


def _session_id() -> str:
    """本浏览器会话的稳定 id。

    Streamlit 每个标签页是一个独立 session，所以这个 id 天然做到"一个标签页一场"。
    存在 session_state 里，刷新（同一标签页）时由 Streamlit 保留；换标签页则不同。
    """
    ss = st.session_state
    if not ss.get(session_store.SESSION_KEY):
        ss[session_store.SESSION_KEY] = uuid.uuid4().hex[:12]
    return str(ss[session_store.SESSION_KEY])


def save_snapshot() -> None:
    """把"进行中的面试"写进**本会话自己的**快照文件。

    只在 status == running 时保存；写失败绝不能影响面试，静默跳过即可。
    """
    if st.session_state.get("status") != "running" or not st.session_state.get("profile"):
        return
    data = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "started_at": st.session_state.started_at.isoformat(timespec="seconds"),
        # 简历不落盘：profile 里剔除 resume / resume_chunks，
        # 另记一个"本场是否用过简历"的标记，恢复后好提示用户。
        "profile": {
            k: v
            for k, v in (st.session_state.profile or {}).items()
            if k not in ("resume", "resume_chunks")
        },
        "had_resume": bool(st.session_state.get("resume_text")),
    }
    for field in _SNAPSHOT_FIELDS:
        data[field] = st.session_state.get(field)
    try:
        session_store.write_snapshot(BASE_DIR, _session_id(), data)
    except (OSError, TypeError) as exc:  # noqa: BLE001
        print(f"[快照保存失败] {exc}", file=sys.stderr, flush=True)


def load_snapshot() -> dict | None:
    """读取最近一场未完成的面试（排除本会话自己）；没有则 None。"""
    return session_store.latest_snapshot(BASE_DIR, exclude_session=_session_id())


def clear_snapshot() -> None:
    """删掉本会话自己的快照（面试正常结束、重新开始或用户主动放弃时调用）。"""
    session_store.clear_snapshot(BASE_DIR, _session_id())


def restore_snapshot(data: dict) -> None:
    """把快照恢复进 session_state，并把界面切回"面试进行中"。"""
    ss = st.session_state
    # 接管这一场：内容搬进本会话，然后删掉源快照。
    # 不能直接沿用源会话 id——那个标签页可能还开着，两边同写一个文件会互相覆盖；
    # 删掉源文件则保证"一场面试 = 一个快照文件"，也不会留下孤儿。
    # source 为空说明它来自旧版单文件，同样要清掉，否则续面后横幅会再冒出来。
    source = str(data.get(session_store.SESSION_KEY) or "")
    session_store.clear_snapshot(
        BASE_DIR, source or session_store.LEGACY_SNAPSHOT_NAME
    )
    ss["profile"] = data["profile"]
    for field in _SNAPSHOT_FIELDS:
        if field in data:
            ss[field] = data[field]
    try:
        ss["started_at"] = datetime.fromisoformat(str(data.get("started_at") or ""))
    except ValueError:
        ss["started_at"] = datetime.now()
    # 音频与简历都不可能恢复：清空，避免残留上一场的内容
    ss["audio_by_index"] = {}
    ss["autoplay_index"] = -1
    ss["avatar_audio"] = b""
    ss["voice_signature"] = ""
    ss["evaluation"] = {}
    ss["coach"] = {}
    ss["error"] = ""
    ss["status"] = "running"
    # 原场次用过简历，但简历不落盘 → 恢复后针对性提问会退化，界面上要明确提示
    ss["resume_lost_note"] = bool(data.get("had_resume"))


def render_recovery_banner() -> None:
    """首页：发现未完成的面试快照时，给一个明确的"继续 / 重新开始"入口。

    刻意不做静默自动恢复：让用户自己选，避免他刚想开一场新面试却被旧场次顶掉。
    """
    data = load_snapshot()
    if not data:
        return
    profile = data.get("profile") or {}
    answered = len([m for m in (data.get("history") or []) if m.get("role") == "user"])
    st.warning(
        "检测到一场未完成的面试，可以继续。\n\n"
        f"- 场次：{profile_summary(profile)}\n"
        f"- 已作答：{answered} 道\n"
        f"- 快照时间：{data.get('saved_at', '')}\n\n"
        "简历不会被保存，恢复后本场将按你口述的背景继续提问。"
    )
    col1, col2 = st.columns(2)
    if col1.button("继续上次面试", type="primary", use_container_width=True):
        restore_snapshot(data)
        st.rerun()
    if col2.button("放弃并重新开始", use_container_width=True):
        # 要丢的是**横幅上那一场**（可能来自另一个标签页），不是本会话自己的。
        # 误删本会话快照会让正在进行的面试失去断点续面能力。
        stale = str(data.get(session_store.SESSION_KEY) or "")
        if stale:
            session_store.clear_snapshot(BASE_DIR, stale)
        st.rerun()


# ---------------------------------------------------------------------------
# 会话状态
# ---------------------------------------------------------------------------


def init_state() -> None:
    defaults = {
        "profile": None,
        "history": [],
        "decisions": [],
        "status": "idle",  # idle / running / finished
        "cur_q": 0,  # 已经问出的主问题数量
        "probes_used": 0,  # 当前主问题已追问次数
        "reflects_in_a_row": 0,  # 已经连续做了几次"不提问回合"（复述确认 / 澄清 / 反馈）
        "asked_topics": [],  # 本场已经问过的主问题考点，用来避免同场重复出题
        # 面试官情绪：此刻的状态、整场的状态轨迹、开场时间（用来推算时间压力）
        "mood_state": mood.initial(),
        "mood_trace": [],
        "mood_choice": MOOD_ORDER[0],  # 侧边栏选的情绪模式（默认「随机」）
        "started_at": datetime.now(),
        "evaluation": {},
        "coach": {},
        "usage": {},  # 本场 token 计量（开局清零，报告生成后取值）
        # 清空历史记录需要两步确认（不可撤销），这个标记记住"已点过一次"
        "confirm_clear_history": False,
        # API 连通性测试结果。存进 session_state，否则 rerun 一次就消失
        "conn_test": None,
        # 注意：`strict_mode` 刻意不在这里设默认值——它由侧边栏那个带 key 的
        # checkbox 自己管理。两边都设会让 Streamlit 报"默认值与 Session State 冲突"。
        "api_key": load_api_key(DEEPSEEK_KEY_ENV),
        "sf_key": load_api_key(SILICONFLOW_KEY_ENV),
        "error": "",
        # 简历只存在于内存：不落盘、不进历史记录，面试结束即清空
        "resume_text": "",
        "resume_name": "",
        "resume_error": "",
        "resume_signature": "",
        "resume_epoch": 0,
        # 从快照恢复时若原场次用过简历，这里置 True，界面上提示"简历无法恢复"
        "resume_lost_note": False,
        # 语音：音频单独存放，绝不进 history（否则落盘 json.dumps 会直接崩）
        "voice_on": True,
        "voice_name": DEFAULT_VOICE,
        "audio_by_index": {},  # 面试官消息下标 -> mp3 字节
        "autoplay_index": -1,  # 需要自动播放的消息下标
        "voice_signature": "",  # 已处理过的录音指纹，防止同一条录音被反复识别
        # 面试官语音动效：开关 + 最新一条语音（组件专用，rerun 时保持不变，避免 iframe 重载打断播放）
        "avatar_on": True,
        "avatar_audio": b"",
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def start_interview(profile: dict, api_key: str) -> None:
    """重置状态并让面试官开场。"""
    clear_snapshot()  # 开新场次即丢弃上一场残留的快照，避免首页又提示"继续上次面试"
    reset_usage()  # 本场 token 计量从零开始
    st.session_state.profile = profile
    st.session_state.api_key = api_key
    st.session_state.history = []
    st.session_state.decisions = []
    st.session_state.cur_q = 0
    st.session_state.probes_used = 0
    st.session_state.reflects_in_a_row = 0
    st.session_state.asked_topics = []
    st.session_state.resume_lost_note = False
    # 情绪状态在开局锁定：整场不再换模式，状态轨迹才连续，归因报告才有依据
    st.session_state.mood_state = mood.initial(**profile["mood_init"])
    st.session_state.mood_trace = []
    st.session_state.started_at = datetime.now()
    st.session_state.evaluation = {}
    st.session_state.coach = {}
    st.session_state.error = ""
    st.session_state.audio_by_index = {}
    st.session_state.autoplay_index = -1
    st.session_state.voice_signature = ""
    st.session_state.avatar_audio = b""
    st.session_state.status = "running"
    advance()


def speak(text: str) -> None:
    """把面试官的话合成语音，挂到刚写入的那条消息上等待播放。

    合成失败绝不能影响面试：宁可没声音，也不能让整场面试报错中断。
    """
    if not (st.session_state.voice_on and text.strip()):
        return
    try:
        level = (st.session_state.profile or {}).get("level", "B")
        current_mood = mood.mood_of(st.session_state.mood_state)
        audio = synthesize(
            text,
            st.session_state.voice_name,
            level,
            current_mood,
            fallback_key=siliconflow_key(),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[TTS 失败] {exc}", file=sys.stderr, flush=True)
        return
    index = len(st.session_state.history) - 1
    st.session_state.audio_by_index[index] = audio
    st.session_state.autoplay_index = index
    # 声波球组件只认"最新一条"：内容变化 = 有新语音 = 该它开口
    st.session_state.avatar_audio = audio


def advance() -> None:
    """让面试官走一轮，并按决策更新计数器。"""
    profile = st.session_state.profile

    # 先推进情绪状态：依据是「候选人刚给的回答」+「面试官上一轮的决策」。
    # 放在调用模型之前，本轮的口播语气与追问上限才是这份状态的产物。
    last_answer, last_decision = "", {}
    for msg in reversed(st.session_state.history):
        if msg["role"] == "user" and not last_answer:
            last_answer = msg.get("content", "")
        elif msg["role"] == "assistant" and not last_decision:
            last_decision = msg.get("decision") or {}
        if last_answer and last_decision:
            break

    elapsed_min = (datetime.now() - st.session_state.started_at).total_seconds() / 60
    # 先备份情绪状态：模型调用失败时要把这一步退回去。mood.update 是确定性的，
    # 失败后若不回滚，重试就会把同一轮的情绪变化重复叠加一次。
    mood_before = dict(st.session_state.mood_state)
    st.session_state.mood_state = mood.update(
        st.session_state.mood_state,
        answer=last_answer,
        decision=last_decision,
        elapsed_min=elapsed_min,
        planned_rounds=int(profile.get("total_rounds", 5)),
        cur_q=st.session_state.cur_q,
    )

    try:
        data = interviewer_turn(
            st.session_state.history,
            profile,
            st.session_state.api_key,
            cur_q=st.session_state.cur_q,
            probes_used=st.session_state.probes_used,
            reflects_in_a_row=st.session_state.reflects_in_a_row,
            mood_state=st.session_state.mood_state,
            asked_topics=st.session_state.asked_topics,
        )
    except Exception as exc:  # noqa: BLE001
        # 回滚情绪状态，并保留候选人这条回答：界面据此给出「重试本轮」入口，
        # 用户不必重新输入（重新输入会留下两条连续的候选人消息）。
        st.session_state.mood_state = mood_before
        st.session_state.error = f"调用模型失败：{exc}"
        return

    # 本轮成功：清掉上一轮可能残留的错误横幅，否则它会一直挂在页面上。
    st.session_state.error = ""
    action = data["action"]

    # 计数权在代码手里：先按决策推进状态，再记录。
    # 具体规则抽在 interview_state.advance_counters（纯函数，可离线单测）。
    (
        st.session_state.cur_q,
        st.session_state.probes_used,
        st.session_state.reflects_in_a_row,
    ) = interview_state.advance_counters(
        st.session_state.cur_q,
        st.session_state.probes_used,
        st.session_state.reflects_in_a_row,
        action,
    )

    # 记录本轮考点用于同场去重：ask 落一个新考点，probe / reflect 沿用当前考点。
    # 规整后的考点由 decision_row 自行读取 data["topic"]，这里只要新列表。
    st.session_state.asked_topics, _ = interview_state.next_asked_topics(
        st.session_state.asked_topics, data.get("topic")
    )

    st.session_state.history.append(
        {"role": "assistant", "content": data["reply"], "decision": data}
    )
    speak(data["reply"])
    st.session_state.decisions.append(
        interview_state.decision_row(st.session_state.cur_q, data, ACTION_LABELS)
    )

    # 状态轨迹：这一轮面试官是带着什么状态说话的。评估官与教练靠它做归因。
    state = st.session_state.mood_state
    st.session_state.mood_trace.append(
        interview_state.mood_trace_row(
            st.session_state.cur_q, state, mood.mood_of(state)
        )
    )

    if action == "end":
        finish_interview()
    else:
        # 每轮问答后存一次快照：刷新浏览器就能从上一步接着面
        save_snapshot()


def clear_resume() -> None:
    """丢弃内存中的简历，并把上传控件重置为空。

    上传播件自身持有一份状态，只清 session_state 不足以让它变空，
    所以递增 resume_epoch 去换一个新的控件 key，强制重建。
    """
    st.session_state.resume_text = ""
    st.session_state.resume_name = ""
    st.session_state.resume_error = ""
    st.session_state.resume_signature = ""
    st.session_state.resume_epoch += 1


def sync_resume(uploaded) -> None:
    """上传控件有变化时解析简历；控件被清空时同步清掉内存里的简历。"""
    if uploaded is None:
        if st.session_state.resume_name or st.session_state.resume_text:
            clear_resume()
        return

    signature = f"{uploaded.name}:{uploaded.size}"
    if signature == st.session_state.resume_signature:
        return  # 同一个文件，不必每次页面刷新都重新解析

    text, message = extract_text(uploaded.name, uploaded.getvalue())
    st.session_state.resume_signature = signature
    st.session_state.resume_error = message
    st.session_state.resume_text = text
    st.session_state.resume_name = uploaded.name if text else ""


def finish_interview() -> None:
    """面试结束：依次调用评估官和教练，并落盘。"""
    st.session_state.status = "finished"
    history = st.session_state.history
    profile = st.session_state.profile

    try:
        # 至少要有几轮真实问答，否则报告没有意义
        if len([m for m in history if m["role"] == "user"]) < 2:
            st.session_state.error = "有效回答太少，本次不生成报告。"
            return

        try:
            trace = st.session_state.mood_trace
            # 把**本场实际问到**的考点交给评估官：不传的话它会拿到该难度档的
            # 全部 15~17 条参照，把没问过的判成「遗漏」，教练再据此给出
            # "去补某个从没问到的考点"的错误建议。
            evaluation = evaluate_interview(
                history,
                profile,
                st.session_state.api_key,
                mood_trace=trace,
                asked_topics=st.session_state.asked_topics,
            )
            coach = coach_review(
                history, evaluation, profile, st.session_state.api_key, mood_trace=trace
            )
        except Exception as exc:  # noqa: BLE001
            st.session_state.error = f"生成报告失败：{exc}"
            return

        st.session_state.evaluation = evaluation
        st.session_state.coach = coach
        # 报告生成完再取一次计量，这样本场消耗把评估官与教练的调用也算进去
        st.session_state.usage = usage_snapshot()

        save_record(
            {
                "time": datetime.now().isoformat(timespec="seconds"),
                # 落盘时剔除简历及其切片索引：简历不进入任何持久化文件
                "profile": {
                    k: v
                    for k, v in profile.items()
                    if k not in ("resume", "resume_chunks")
                },
                "overall_score": evaluation.get("overall_score"),
                "adjusted_score": evaluation.get("adjusted_score"),
                "level": evaluation.get("level"),
                "transcript": history,
                "decisions": st.session_state.decisions,
                "asked_topics": st.session_state.asked_topics,
                "mood_trace": trace,
                "usage": st.session_state.usage,
                "evaluation": evaluation,
                "coach": coach,
            }
        )
    finally:
        # 面试一结束就丢弃简历，无论报告是否生成成功
        clear_resume()
        # 同时清掉快照：这场已经收尾，首页不该再提示"继续上次面试"
        clear_snapshot()


# ---------------------------------------------------------------------------
# 界面 · 侧边栏配置
# ---------------------------------------------------------------------------


def _safe_index(options: list, value) -> int:
    """在 options 里找 value 的下标，找不到就返回 0。

    直接 .index() 会抛 ValueError —— 而这里的取值可能来自**快照恢复**
    （跨版本、跨配置），或者来自用户改过的环境变量。侧边栏因为一个过期取值
    整个崩掉是不可接受的，所以一律退化为第一项。
    """
    try:
        return options.index(value)
    except ValueError:
        return 0


def _render_api_key_section() -> str:
    """DeepSeek API Key：环境变量优先，否则让用户手填。返回当前可用的 key。

    判断"是否来自环境变量"用的是**值比对**而不是"字段非空"：
    `start_interview` 会把生效的 key 写进 `st.session_state.api_key`，
    若只看字段非空，手填的 key 在开局后会被误报成"已从环境变量读取"。
    """
    env_key = load_api_key(DEEPSEEK_KEY_ENV)
    if env_key:
        st.caption("🔑 DeepSeek API Key 已从环境变量读取")
        return env_key

    typed = st.text_input(
        "DeepSeek API Key",
        value="",
        type="password",
        key="api_key_input",
        help="在 platform.deepseek.com 申请，形如 sk-xxxx",
    )
    return (typed or "").strip()


def _render_core_config() -> dict:
    """三级自适应（场景 × 专业 × 难度）—— 本项目的核心配置，默认展开。"""
    scene = st.selectbox("① 面试场景", SCENES, index=1)
    major = st.selectbox("② 专业方向", MAJORS, index=0)

    labels = level_labels(scene)
    level = st.selectbox(
        "③ 难度等级",
        LEVELS,
        index=1,
        format_func=lambda code: f"{code} 级 · {labels[code]}",
    )

    interview_type = st.radio("面试类型", ["技术面", "行为面"], horizontal=True)
    total_rounds = st.slider("主问题数量", 3, 10, 5)

    return {
        "scene": scene,
        "major": major,
        "level": level,
        "interview_type": interview_type,
        "total_rounds": total_rounds,
    }


def _render_mood_section(strict: bool) -> bool:
    """附加轴 · 面试官情绪。返回是否满足"可以开始"的知情同意条件。

    刻意不编号：DESIGN 里情绪是**与难度正交的第二条轴**，
    编成"④"会暗示它是三级自适应的下一步，与设计不符。
    """
    st.markdown("**附加轴 · 面试官情绪**")

    if strict:
        # 严格模式锁定友善型。这里直接把控件禁用，而不是"能选但不生效"——
        # 后者会让人以为自己选的模式被采纳了。
        st.selectbox(
            "情绪模式",
            ["友善型（严格模式锁定）"],
            index=0,
            disabled=True,
            label_visibility="collapsed",
            help="严格模式要求每场节奏一致；情绪漂移会改变追问上限与措辞，两者目标冲突，所以锁定。",
        )
        st.caption("严格模式已锁定「友善型」——保证每场节奏一致，情绪不随机也不漂移。")
        return True

    st.session_state.mood_choice = st.selectbox(
        "情绪模式",
        MOOD_ORDER,
        index=_safe_index(MOOD_ORDER, st.session_state.mood_choice),
        label_visibility="collapsed",
        help="与难度正交：难度决定「问什么」，情绪决定「用什么态度问」。"
        "面试官的情绪还会随你的回答实时变化，面试结束后在报告里揭晓。",
    )

    if st.session_state.mood_choice == "随机":
        st.caption(
            "「随机」会在五种模式里抽取，其中包含疲惫型、怀疑型、冷淡型、压力型等高强度模式。"
            "如果只想练正常发挥，请显式选择「友善型」。"
        )

    if not MOOD_MODES[st.session_state.mood_choice]["confirm"]:
        return True

    agreed = st.checkbox(
        "我了解这是为训练目的设计的模拟状态，不是对真实面试官的平均描述",
        value=False,
        help="这类模式会刻意表现得没耐心、少反馈甚至挑刺，用来练高压下的稳定性。"
        "面试过程中可以随时中止。",
    )
    if not agreed:
        st.caption("勾选后即可开始面试。")
    return agreed


def _render_real_questions_section() -> tuple[bool, int]:
    """真题扩展：开关 + 配额，附覆盖范围说明。"""
    use_real = st.checkbox(
        "真题扩展（取材真实企业面试题）",
        value=True,
        help="从开源的真实企业面试题库里挑选素材，每场最多挑几道作为主问题，"
        "让面试更接近真实企业面试；其余主问题仍按上面的考点体系出题。",
    )
    real_quota = st.slider(
        "真题配额（每场最多几道）",
        0,
        3,
        2,
        disabled=not use_real,
        help="设为 0 即完全不使用真题。录演示视频、或想让每场节奏完全一致时，建议设为 0 或关掉。",
    )
    st.caption(
        "覆盖范围：技术面仅「计算机科学与技术 / 软件工程」有可信来源；"
        "人工智能与电子信息 / 自动化 / 通信工程暂无技术面真题，会自动跳过、不影响面试；"
        "行为面真题不限专业。"
    )
    return use_real, real_quota


def _render_candidate_section() -> str:
    """候选人背景 + 简历上传。返回拼好的背景文本。"""
    st.caption("填了之后面试官会围绕你的真实经历提问；不传简历也能问出针对性。")
    target_role = st.text_input(
        "目标岗位",
        placeholder="例：后端开发实习生 / 算法工程师",
        key="bg_role",
    )
    bg_project = st.text_area(
        "项目 / 实习经历",
        height=80,
        placeholder="例：做过校园二手交易小程序，SpringBoot + MySQL，我负责订单与支付模块。",
        key="bg_project",
    )
    bg_skills = st.text_input(
        "技能栈",
        placeholder="例：Java / SpringBoot / MySQL / Redis / Git",
        key="bg_skills",
    )

    parts = []
    if target_role.strip():
        parts.append(f"目标岗位：{target_role.strip()}")
    if bg_project.strip():
        parts.append(f"项目/实习经历：{bg_project.strip()}")
    if bg_skills.strip():
        parts.append(f"技能栈：{bg_skills.strip()}")

    uploaded = st.file_uploader(
        "上传简历（可选）",
        type=ACCEPTED_TYPES,
        key=f"resume_upload_{st.session_state.resume_epoch}",
        help="上传后面试官会围绕你的实习经历、毕业设计、获奖情况等具体条目提问；不传也能正常面试。",
    )
    sync_resume(uploaded)

    if st.session_state.resume_name:
        st.success(f"已读取简历：{st.session_state.resume_name}")
        if st.button("清除简历"):
            clear_resume()
            st.rerun()
    elif st.session_state.resume_error:
        st.warning(st.session_state.resume_error)

    st.caption(
        "简历只在本次会话的内存里暂存，不写入磁盘、不进入历史记录，面试结束后立即清除。"
        "面试中会按当前话题检索相关片段发给 DeepSeek 生成针对性问题；"
        "结束后评估官还会用简历原文核对你的自述。"
    )
    return "\n".join(parts)


def _render_voice_section() -> None:
    """语音与动效。关闭语音时整段收起，避免无意义的控件堆在侧边栏。"""
    st.session_state.voice_on = st.checkbox(
        "启用语音面试",
        value=st.session_state.voice_on,
        help="打开后可以按住说话作答，面试官也会开口回复；关闭则回到纯文字模式。",
    )
    if not st.session_state.voice_on:
        return

    names = list(VOICES)
    current = _safe_index(
        list(VOICES.values()), st.session_state.voice_name
    )
    picked = st.selectbox("面试官音色", names, index=current)
    st.session_state.voice_name = VOICES[picked]

    st.session_state.avatar_on = st.checkbox(
        "面试官语音动效",
        value=st.session_state.avatar_on,
        help="说话时显示一个随声音起伏的声波球（不出现人脸）。"
        "纯前端渲染，不需要额外配置；关掉就只保留语音与文字。",
    )

    if st.session_state.sf_key:
        st.caption("🎙️ 硅基流动 API Key 已从环境变量读取")
    else:
        st.text_input(
            "硅基流动 API Key",
            type="password",
            key="sf_key_input",
            help="在 siliconflow.cn 申请，用于语音识别；不填仍然可以用文字作答。",
        )

    st.caption(
        "录音会上传到硅基流动做识别，不做保存；面试官的声音默认由 edge-tts 合成，"
        "如不可用会自动改用硅基流动（复用同一把 Key）。识别失败会自动退回文字输入。"
    )


def _render_connection_test(api_key: str) -> None:
    """连通性测试。结果存进 session_state，否则 rerun 一次就消失。"""
    if st.button("测试 API 连通性"):
        if not api_key:
            st.session_state.conn_test = ("warning", "请先填写 API Key。")
        else:
            with st.spinner("正在连接…"):
                try:
                    reply = test_connection(api_key)
                    st.session_state.conn_test = ("success", f"连接成功，模型回复：{reply}")
                except Exception as exc:  # noqa: BLE001
                    st.session_state.conn_test = ("error", f"连接失败：{exc}")

    result = st.session_state.get("conn_test")
    if result:
        level, message = result
        getattr(st, level)(message)


def _render_running_panel() -> None:
    """面试进行中：只显示进度，不再摆一堆改了也没用的配置项。

    两条刻意为之：

    1. **不渲染任何配置控件**。`profile` 在开局时就编译好了，面试中改这些值
       不会生效——摆在那里只会让人以为改了有用。
    2. **不显示面试官的内部状态**（耐心 / 精力 / 时间压力）。DESIGN 明确
       "状态本身不给考生看"：看见了就会去迎合它，训练意义就没了。
       本场的状态轨迹在**面试结束后的报告**里揭晓（含归因）。
    """
    profile = st.session_state.profile or {}
    st.header("面试进行中")

    progress = f"已问 {st.session_state.cur_q}/{profile.get('total_rounds', 5)} 道主问题"
    if st.session_state.probes_used:
        max_probes = mood.effective_max_probes(profile, st.session_state.mood_state)
        progress += f"\n当前题已追问 {st.session_state.probes_used}/{max_probes} 次"
    st.progress(
        min(st.session_state.cur_q / max(profile.get("total_rounds", 5), 1), 1.0),
        text=progress,
    )

    st.divider()
    st.caption(profile_summary(profile))
    st.caption("本场配置在开局时已锁定；如需换配置，请先结束本场。")
    st.caption("面试官的情绪会随你的回答变化，但本场结束前不显示——报告里会揭晓整条状态轨迹。")


def render_sidebar() -> None:
    """侧边栏。

    分两种形态：**面试进行中**只显示进度（配置已锁定，摆出来只会误导）；
    **空闲时**按用途分组折叠，避免两百行控件平铺。
    """
    with st.sidebar:
        if st.session_state.status == "running" and st.session_state.profile:
            _render_running_panel()
            return

        st.header("面试配置")
        api_key = _render_api_key_section()

        # strict 要在情绪那一节之前就已知（严格模式会锁定友善型），
        # 但它的控件在「高级选项」里——所以用带 key 的控件 + 先读 session_state。
        # 这是 Streamlit 里处理"后渲染的控件影响先渲染的内容"的标准做法。
        strict = bool(st.session_state.get("strict_mode", False))

        with st.expander("🎯 基础配置", expanded=True):
            config = _render_core_config()

        with st.expander("🎭 面试官情绪", expanded=False):
            mood_ok = _render_mood_section(strict=strict)

        with st.expander("👤 候选人背景与简历", expanded=False):
            candidate_bg = _render_candidate_section()

        with st.expander("🎙️ 语音与动效", expanded=False):
            _render_voice_section()

        with st.expander("⚙️ 高级选项", expanded=False):
            st.checkbox(
                "严格模式（节奏固定）",
                value=False,
                key="strict_mode",
                help="打开后：追问上限收紧为 2 次、不出现「复述确认/澄清/反馈」这类非提问回合，"
                "每场节奏基本一致，适合录演示视频。关闭则更像真人：追问上限放宽到 3 次，"
                "面试官会偶尔停下来确认、澄清或反馈，节奏有呼吸感。",
            )
            st.caption("严格模式会锁定「友善型」情绪：录演示要求每场节奏一致，而情绪漂移会改变追问上限与措辞。")
            use_real, real_quota = _render_real_questions_section()
            st.divider()
            _render_connection_test(api_key)

        st.divider()
        can_start = bool(api_key) and mood_ok
        if st.button("开始面试", type="primary", disabled=not can_start, use_container_width=True):
            profile = build_profile(
                config["scene"],
                config["major"],
                config["level"],
                config["interview_type"],
                config["total_rounds"],
                candidate_bg,
                resume=st.session_state.resume_text,
                strict=strict,
                mood=st.session_state.mood_choice,
                avoid_topics=recent_asked_topics(),
                use_real=use_real,
                real_quota=real_quota,
            )
            start_interview(profile, api_key)
            st.rerun()

        if not api_key:
            st.caption("填入 DeepSeek API Key 后即可开始。")
        elif not mood_ok:
            st.caption("勾选上面的知情同意后即可开始。")


# ---------------------------------------------------------------------------
# 界面 · 面试对话
# ---------------------------------------------------------------------------


def render_interview() -> None:
    profile = st.session_state.profile

    st.title("知遇 · AI 虚拟面试官")
    st.caption(f"当前场次：{profile_summary(profile)}")

    if st.session_state.get("resume_lost_note"):
        st.warning(
            "本场是从快照恢复的：简历只在内存里、不会落盘，所以无法一起恢复。"
            "本场将继续进行，但针对简历的提问会退化为按你口述的背景提问；"
            "如需简历提问，可结束本场后重新上传简历再开一场。"
        )

    progress = f"已问 {st.session_state.cur_q}/{profile['total_rounds']} 道主问题"
    if st.session_state.probes_used:
        max_probes = mood.effective_max_probes(profile, st.session_state.mood_state)
        progress += f"（当前题已追问 {st.session_state.probes_used}/{max_probes} 次）"
    st.progress(min(st.session_state.cur_q / max(profile["total_rounds"], 1), 1.0), text=progress)

    # 面试官语音动效：开启语音时显示声波球，并接管"最新一条"语音的自动播放
    avatar_visible = st.session_state.avatar_on and st.session_state.voice_on
    avatar_html = avatar_component(st.session_state.avatar_audio) if avatar_visible else ""

    for index, msg in enumerate(st.session_state.history):
        role = "assistant" if msg["role"] == "assistant" else "user"
        with st.chat_message(role, avatar="🧑‍💼" if role == "assistant" else "🙋"):
            st.markdown(msg["content"])
            audio = st.session_state.audio_by_index.get(index)
            if audio:
                # 声波球接管自动播放时，这里的播放器只留手动回听，避免双重出声
                st.audio(
                    audio,
                    format="audio/mp3",
                    autoplay=(index == st.session_state.autoplay_index)
                    and not avatar_html,
                )
    # 只让最新一条自动出声，否则每次刷新都会重播
    st.session_state.autoplay_index = -1

    # 放在对话流末尾、输入框上方：面试官问完话，画面就在你要作答的地方
    if avatar_html:
        _, avatar_col, _ = st.columns([1, 1.4, 1])
        with avatar_col:
            st.iframe(avatar_html, height=330)

    # 上一轮调用失败：面试官还没接上话。
    # 这时**不再渲染输入框**（否则再答一次会留下两条连续的候选人消息），
    # 而是让用户直接重试本轮——已答的那句还在，不用重打。
    if _awaiting_interviewer():
        st.warning(
            "面试官这一轮没有接上话（多半是网络或接口临时故障）。"
            "你的进度已经保留，直接重试即可。"
        )
        col1, col2 = st.columns([1, 4])
        if col1.button("重试本轮", type="primary"):
            with st.chat_message("assistant", avatar="🧑‍💼"):
                with st.spinner("面试官思考中…"):
                    advance()
            st.rerun()
        # 至少答过一轮才有报告可生成；开场就失败时只给重试
        if len([m for m in st.session_state.history if m.get("role") == "user"]) >= 2:
            if col2.button("放弃这一轮，直接生成报告"):
                with st.spinner("评估官与教练正在生成报告…"):
                    finish_interview()
                st.rerun()
        return

    answer = st.chat_input("输入你的回答，按回车发送…")

    sfc_key = siliconflow_key()
    if st.session_state.voice_on and sfc_key and not answer:
        clip = st.audio_input("也可以按住说话作答", key="answer_clip")
        if clip is not None:
            # 同一段录音只识别一次：靠指纹比对，而不是识别完就去清空控件
            signature = hashlib.md5(clip.getvalue()).hexdigest()
            if signature != st.session_state.voice_signature:
                st.session_state.voice_signature = signature
                with st.spinner("正在识别语音…"):
                    try:
                        answer = transcribe(clip.getvalue(), sfc_key)
                        answer = _correct_voice(answer)
                    except Exception as exc:  # noqa: BLE001
                        st.session_state.error = f"语音识别失败：{exc}"
                        answer = ""
    elif st.session_state.voice_on and not answer and not sfc_key:
        st.caption("想用语音作答？在左侧填入硅基流动 API Key 后即可录音。")

    if answer:
        st.session_state.history.append({"role": "user", "content": answer})
        # 先把回答存进快照，再调用模型：万一本轮模型报错，刷新后至少不会丢掉这句回答
        save_snapshot()
        with st.chat_message("user", avatar="🙋"):
            st.markdown(answer)
        with st.chat_message("assistant", avatar="🧑‍💼"):
            with st.spinner("面试官思考中…"):
                advance()
        st.rerun()

    col1, col2 = st.columns([1, 4])
    if col1.button("结束面试并生成报告"):
        with st.spinner("评估官与教练正在生成报告…"):
            finish_interview()
        st.rerun()


# ---------------------------------------------------------------------------
# 复盘报告导出
#
# 界面上看到的内容不落盘，导出成 Markdown 后可以存档、用 VSCode 打开，
# 或直接作为参赛材料 / 演示视频的附件。
# 纯字符串拼接，不调用模型，也不影响面试主流程。
# ---------------------------------------------------------------------------


def build_report_markdown() -> str:
    """把本场报告整理成一份 Markdown 文本。"""
    profile = st.session_state.profile or {}
    evaluation = st.session_state.evaluation or {}
    coach = st.session_state.coach or {}
    trace = st.session_state.mood_trace or []
    lines: list[str] = []

    def add(text: str = "") -> None:
        lines.append(text)

    def table(headers: list[str], rows: list[list[str]]) -> None:
        add("| " + " | ".join(headers) + " |")
        add("|" + "---|" * len(headers))
        for row in rows:
            add("| " + " | ".join(str(c).replace("|", "／") for c in row) + " |")
        add()

    add("# 模拟面试复盘报告")
    add()
    add(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    add(f"- 面试配置：{profile_summary(profile)}")
    add(f"- 主问题数量：{profile.get('total_rounds', '-')}")
    if profile.get("mood_mode"):
        add(f"- 面试官情绪模式：{profile['mood_mode']}")
    real_items = [
        str(item.get("q", "")).strip()
        for item in (profile.get("real_questions") or [])
        if str(item.get("q", "")).strip()
    ]
    if real_items:
        add(f"- 取材真题（{len(real_items)} 道）：" + "；".join(real_items))
    add()

    if evaluation:
        add("## 一、评分")
        add()
        add(f"- 本场实际表现：**{evaluation.get('overall_score', '-')}**")
        add(
            "- 剥离面试官状态干扰后："
            f"**{evaluation.get('adjusted_score', evaluation.get('overall_score', '-'))}**"
        )
        add(f"- 评委结论：{evaluation.get('level', '-')}")
        add()
        if evaluation.get("attribution"):
            add("### 归因：哪些扣分不该算在你头上")
            add()
            add(evaluation["attribution"])
            add()
        dims = evaluation.get("dimensions") or []
        if dims:
            add("### 维度得分")
            add()
            table(
                ["维度", "得分", "评语"],
                [[d.get("name", ""), d.get("score", 0), d.get("comment", "")] for d in dims],
            )
        rubric_check = [x for x in (evaluation.get("rubric_check") or []) if isinstance(x, dict)]
        if rubric_check:
            add("### 考点命中情况")
            add()
            add("> 参照是底线不是天花板：清单之外的正确答案同样算分，没命中不等于答错。")
            add()
            table(
                ["考点", "已命中", "遗漏", "误区"],
                [
                    [
                        item.get("topic", ""),
                        "；".join(str(x) for x in (item.get("must_hit") or [])) or "—",
                        "；".join(str(x) for x in (item.get("missed") or [])) or "—",
                        "；".join(str(x) for x in (item.get("errors") or [])) or "—",
                    ]
                    for item in rubric_check
                ],
            )
        resume_check = evaluation.get("resume_check")
        if isinstance(resume_check, dict):
            issues = [x for x in (resume_check.get("issues") or []) if isinstance(x, dict)]
            uncovered = [str(x) for x in (resume_check.get("uncovered") or []) if str(x).strip()]
            if issues or uncovered or resume_check.get("comment"):
                add("### 简历一致性核查")
                add()
                add("> 用简历原文核对你在面试里的自述，只标记确有依据的出入。")
                add()
                if issues:
                    table(
                        ["简历里的说法", "面试中的说法", "问题"],
                        [
                            [i.get("claim", ""), i.get("said", ""), i.get("comment", "")]
                            for i in issues
                        ],
                    )
                for item in uncovered:
                    add(f"- 简历里有、本场没问到的经历：{item}")
                if resume_check.get("comment"):
                    add()
                    add(f"> {resume_check['comment']}")
                add()
        for title, key in (("亮点", "highlights"), ("风险", "risks")):
            if evaluation.get(key):
                add(f"### {title}")
                add()
                for item in evaluation[key]:
                    add(f"- {item}")
                add()
        if evaluation.get("summary"):
            add("### 总评")
            add()
            add(evaluation["summary"])
            add()

    if coach:
        add("## 二、教练复盘")
        add()
        if coach.get("weakest"):
            add("### 最需要改的问题")
            add()
            for item in coach["weakest"]:
                add(f"- **{item.get('problem', '')}**")
                add(f"  - 证据：{item.get('evidence', '')}")
                add(f"  - 怎么改：{item.get('fix', '')}")
            add()
        if coach.get("rewrites"):
            add("### 回答改写示范")
            add()
            for i, item in enumerate(coach["rewrites"], start=1):
                add(f"**{i}. {item.get('question', '')}**")
                add()
                add(f"- 你的原答：{item.get('original', '')}")
                add(f"- 示范答案：{item.get('improved', '')}")
                add(f"- 为什么这样改：{item.get('why', '')}")
                add()
        if coach.get("drills"):
            add("### 接下来 3 天练什么")
            add()
            for i, item in enumerate(coach["drills"], start=1):
                add(f"{i}. {item}")
            add()
        resilience = coach.get("resilience") or {}
        if resilience.get("summary"):
            add("### 压力下的稳定性")
            add()
            add(resilience["summary"])
            add()
            if resilience.get("evidence"):
                add(f"- 证据：{resilience['evidence']}")
            if resilience.get("fix"):
                add(f"- 怎么稳住：{resilience['fix']}")
            add()
        if coach.get("encouragement"):
            add(f"> {coach['encouragement']}")
            add()

    decisions = st.session_state.decisions or []
    if decisions:
        add("## 三、面试官决策轨迹")
        add()
        add("> 这一栏体现「不是念题库」：每一轮追问或换题都有明确依据，「工具调用」列可见面试官是否主动查阅了简历原文。")
        add()
        table(
            ["主问题", "动作", "考点", "考察点", "工具调用", "面试官判断"],
            [[d.get(k, "") for k in ("主问题", "动作", "考点", "考察点", "工具调用", "面试官判断")] for d in decisions],
        )

    if trace:
        mode_label = profile.get("mood_mode", "")
        add(f"## 四、面试官状态轨迹（情绪模式：{mode_label or '—'}）")
        add()
        add(mood.summarize(trace, mode_label))
        add()
        table(
            ["轮次", "状态", "耐心", "精力", "时间压力"],
            [
                [i.get("round", ""), i.get("mood", ""), i.get("patience", ""), i.get("energy", ""), i.get("pressure", "")]
                for i in trace
            ],
        )

    history = st.session_state.history or []
    if history:
        add("## 五、完整文字稿")
        add()
        for msg in history:
            speaker = "面试官" if msg.get("role") == "assistant" else "候选人"
            add(f"**{speaker}**：{msg.get('content', '')}")
            add()

    add("---")
    add()
    usage = st.session_state.get("usage") or {}
    if usage.get("calls"):
        add(
            f"> 本场共调用模型 {usage.get('calls', 0)} 次，"
            f"输入 {usage.get('prompt_tokens', 0)} tokens、"
            f"输出 {usage.get('completion_tokens', 0)} tokens。"
        )
        add()
    add("> 本报告由「知遇」AI 虚拟面试智能体自动生成。面试官的情绪状态是刻意模拟的实验条件，")
    add("> 「剥离干扰后」分数估计的是排除对方状态影响后的真实水平。")
    add()
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 界面 · 历史记录与跨场对比
# ---------------------------------------------------------------------------


def render_history_tab() -> None:
    """历史记录页：跨场趋势 + 维度对比 + 逐场回看 + 删除入口。

    这里补上的是 DESIGN 第七节承诺的「横向对比」——数据本来就全在
    history.json 里，此前只做了逐场回看，没有变成"我这几场有没有进步"。
    """
    records = load_records()

    if not records:
        st.info("还没有面试记录。完成一场面试后，这里会出现你的进步曲线。")
        return

    st.caption(f"共 {len(records)} 场面试记录，保存在 history.json")

    # -- 跨场趋势 ---------------------------------------------------------
    series = history_store.trend_series(records)
    if series["labels"]:
        st.subheader("分数趋势")
        chart: dict[str, list[float]] = {"本场实际表现": series["overall"]}
        # 剥离干扰分只有高强度情绪模式下才有，缺的场次不画这条线
        if any(v is not None for v in series["adjusted"]):
            chart["剥离干扰后"] = [
                v if v is not None else None for v in series["adjusted"]
            ]
        st.line_chart(chart)
        st.caption(
            "按时间从左到右。两条线的差距反映面试官情绪状态造成的干扰——"
            "差距越大，说明那几场你越是被对方状态带着走。"
        )

        # 首末对比：一句话回答"有没有进步"
        if len(series["overall"]) >= 2:
            first, last = series["overall"][0], series["overall"][-1]
            delta = last - first
            trend = "上升" if delta > 0 else ("下降" if delta < 0 else "持平")
            st.caption(
                f"从首场 {first:g} 分到最近一场 {last:g} 分，{trend} "
                f"{abs(delta):g} 分（共 {len(series['overall'])} 场）。"
            )

    # -- 维度对比（只在同专业 / 同面试类型内比）---------------------------
    groups = history_store.dimension_groups(records)
    if groups:
        st.subheader("维度对比")
        st.caption(
            "不同专业、不同面试类型的维度口径不同，所以分开对比——"
            "把计算机的「编程基础」和人工智能的「算法能力」放在一张表里比是没有意义的。"
        )
        for key, group in sorted(groups.items()):
            names = history_store.dimension_names(group)
            sessions = group["sessions"]
            with st.expander(f"{key}（{len(sessions)} 场）", expanded=len(groups) == 1):
                weakest = history_store.weakest_dimension(group)
                if weakest and len(sessions) >= 2:
                    st.caption(
                        f"平均分最低的维度是「{weakest[0]}」（{weakest[1]:g} 分）——"
                        "如果只补一项，先补它。"
                    )

                latest = sessions[-1]["dimensions"]
                rows: list[dict[str, str]] = []
                averages: dict[str, float] = {}
                for name in names:
                    avg = history_store.dimension_average(group, name)
                    if avg is not None:
                        averages[name] = avg
                    rows.append(
                        {
                            "维度": name,
                            "最近一场": (
                                f"{latest[name]:g}" if name in latest else "—"
                            ),
                            "平均分": f"{avg:g}" if avg is not None else "—",
                        }
                    )
                st.dataframe(rows, hide_index=True)
                if averages:
                    st.bar_chart(averages)

    # -- 逐场回看与删除 ---------------------------------------------------
    st.subheader("逐场回看")
    for index in range(len(records) - 1, -1, -1):
        record = records[index]
        with st.expander(history_store.record_label(record)):
            st.caption(record.get("level", ""))
            st.json(record.get("evaluation", {}))
            # 删除入口：DESIGN 的隐私应对写了"提供删除入口"，
            # 而落盘的 history.json 里存着完整逐字稿，必须让用户能自己清掉。
            if st.button("删除这一场", key=f"del_record_{index}"):
                history_store.delete_record(HISTORY_FILE, index)
                st.rerun()

    # -- 清空全部 ---------------------------------------------------------
    st.divider()
    st.caption(
        "历史记录里保存着完整逐字稿（你的回答原文与自述背景）。"
        "简历本身从不落盘，但逐字稿会——想清掉的话用下面的按钮。"
    )
    if st.session_state.get("confirm_clear_history"):
        st.warning(f"将永久删除全部 {len(records)} 场记录，无法撤销。")
        col1, col2 = st.columns([1, 3])
        if col1.button("确认清空", type="primary"):
            history_store.clear_records(HISTORY_FILE)
            st.session_state.confirm_clear_history = False
            st.rerun()
        if col2.button("取消"):
            st.session_state.confirm_clear_history = False
            st.rerun()
    else:
        if st.button("清空全部历史记录"):
            # 两步确认：清空不可撤销，不能一次点击就执行
            st.session_state.confirm_clear_history = True
            st.rerun()


# ---------------------------------------------------------------------------
# 界面 · 复盘报告
# ---------------------------------------------------------------------------


def render_report() -> None:
    st.title("知遇 · AI 虚拟面试官")
    st.success(f"面试已结束 · {profile_summary(st.session_state.profile)}")

    real_items = [
        item
        for item in (st.session_state.profile.get("real_questions") or [])
        if str(item.get("q", "")).strip()
    ]
    if real_items:
        st.caption(
            f"本场取材了 {len(real_items)} 道真实企业面试真题："
            + "；".join(str(item.get("q", "")).strip() for item in real_items)
        )

    if st.session_state.evaluation:
        _, btn_col = st.columns([4, 1])
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        btn_col.download_button(
            "导出报告",
            data=build_report_markdown(),
            file_name=f"模拟面试报告-{stamp}.md",
            mime="text/markdown",
            help="导出为 Markdown 文件，可用 VSCode 打开，也可作为参赛材料或演示视频的附件。",
            use_container_width=True,
        )

    evaluation = st.session_state.evaluation
    coach = st.session_state.coach

    tab1, tab2, tab3, tab4 = st.tabs(["评分报告", "教练复盘", "面试记录", "历史记录"])

    with tab1:
        if not evaluation:
            st.info("暂未生成评分报告，可点击下方「重新开始面试」。")
        else:
            col1, col2, col3 = st.columns([1, 1, 2])
            col1.metric("本场实际表现", evaluation.get("overall_score", "-"))
            col2.metric(
                "剥离干扰后",
                evaluation.get("adjusted_score", evaluation.get("overall_score", "-")),
                help="面试官的情绪状态是刻意模拟的实验条件。这个分数估计的是排除对方状态影响后，你的真实水平。",
            )
            col3.metric("评委结论", evaluation.get("level", "-"))

            usage = st.session_state.get("usage") or {}
            if usage.get("calls"):
                st.caption(
                    f"本场共调用模型 {usage.get('calls', 0)} 次 · "
                    f"输入 {usage.get('prompt_tokens', 0)} tokens · "
                    f"输出 {usage.get('completion_tokens', 0)} tokens。"
                    "「主动查阅简历」的轮次会多算一次调用，这个数字能看出窗口化压缩省下了多少。"
                )

            if evaluation.get("attribution"):
                st.subheader("归因：哪些扣分不该算在你头上")
                st.info(evaluation["attribution"])

            trace = st.session_state.mood_trace
            if trace:
                mode_label = (st.session_state.profile or {}).get("mood_mode", "")
                with st.expander(f"本场面试官状态轨迹（情绪模式：{mode_label or '—'}）"):
                    st.caption(mood.summarize(trace, mode_label))
                    columns = {
                        "round": "轮次",
                        "mood": "状态",
                        "patience": "耐心",
                        "energy": "精力",
                        "pressure": "时间压力",
                    }
                    st.dataframe(
                        [{label: item.get(key, "") for key, label in columns.items()} for item in trace],
                        hide_index=True,
                    )
                st.warning(
                    "本场面试官的部分表现是为训练目的设计的状态模拟，不代表真实面试的普遍情况。"
                    "如果你在真实面试里遇到这类面试官，问题在对方，不在你。"
                )

            dims = evaluation.get("dimensions", [])
            if dims:
                st.subheader("维度得分")
                st.dataframe(
                    [
                        {
                            "维度": d.get("name", ""),
                            "得分": d.get("score", 0),
                            "评语": d.get("comment", ""),
                        }
                        for d in dims
                    ],
                    hide_index=True,
                )
                st.bar_chart({d.get("name", ""): d.get("score", 0) for d in dims})

            rubric_check = [
                item for item in (evaluation.get("rubric_check") or []) if isinstance(item, dict)
            ]
            if rubric_check:
                with st.expander("考点命中情况（评委判分时对照的要点清单）"):
                    st.caption(
                        "参照是底线不是天花板：清单之外的正确答案同样算分，没命中不等于答错。"
                    )
                    st.dataframe(
                        [
                            {
                                "考点": item.get("topic", ""),
                                "已命中": "；".join(str(x) for x in (item.get("must_hit") or [])),
                                "遗漏": "；".join(str(x) for x in (item.get("missed") or [])),
                                "误区": "；".join(str(x) for x in (item.get("errors") or [])),
                                "说明": item.get("comment", ""),
                            }
                            for item in rubric_check
                        ],
                        hide_index=True,
                    )

            resume_check = evaluation.get("resume_check")
            if isinstance(resume_check, dict):
                issues = [x for x in (resume_check.get("issues") or []) if isinstance(x, dict)]
                uncovered = [
                    str(x) for x in (resume_check.get("uncovered") or []) if str(x).strip()
                ]
                if issues or uncovered or resume_check.get("comment"):
                    with st.expander("简历一致性核查（你的自述 vs 简历原文）"):
                        st.caption("只标记确有依据的出入；简历里没被问到的经历不算你的问题。")
                        if issues:
                            st.dataframe(
                                [
                                    {
                                        "简历里的说法": i.get("claim", ""),
                                        "面试中的说法": i.get("said", ""),
                                        "问题": i.get("comment", ""),
                                    }
                                    for i in issues
                                ],
                                hide_index=True,
                            )
                        for item in uncovered:
                            st.markdown(f"- 简历里有、本场没问到的经历：{item}")
                        if resume_check.get("comment"):
                            st.info(resume_check["comment"])

            col_a, col_b = st.columns(2)
            with col_a:
                st.subheader("亮点")
                for item in evaluation.get("highlights", []):
                    st.markdown(f"- {item}")
            with col_b:
                st.subheader("风险")
                for item in evaluation.get("risks", []):
                    st.markdown(f"- {item}")

            if evaluation.get("summary"):
                st.info(evaluation["summary"])

    with tab2:
        if not coach:
            st.info("暂未生成复盘报告。")
        else:
            st.subheader("最需要改的问题")
            for item in coach.get("weakest", []):
                st.markdown(f"**{item.get('problem', '')}**")
                st.caption(f"证据：{item.get('evidence', '')}")
                st.write(f"怎么改：{item.get('fix', '')}")

            rewrites = coach.get("rewrites", [])
            if rewrites:
                st.subheader("回答改写示范")
                for i, item in enumerate(rewrites, start=1):
                    with st.expander(f"{i}. {item.get('question', '')}"):
                        st.markdown(f"**你的原答**：{item.get('original', '')}")
                        st.success(f"**示范答案**：{item.get('improved', '')}")
                        st.caption(f"为什么这样改：{item.get('why', '')}")

            drills = coach.get("drills", [])
            if drills:
                st.subheader("接下来 3 天练什么")
                for i, item in enumerate(drills, start=1):
                    st.markdown(f"{i}. {item}")

            resilience = coach.get("resilience") or {}
            if resilience.get("summary"):
                st.subheader("压力下的稳定性")
                st.markdown(resilience["summary"])
                if resilience.get("evidence"):
                    st.caption(f"证据：{resilience['evidence']}")
                if resilience.get("fix"):
                    st.write(f"怎么稳住：{resilience['fix']}")

            if coach.get("encouragement"):
                st.info(coach["encouragement"])

    with tab3:
        decisions = st.session_state.decisions
        if decisions:
            st.subheader("面试官决策轨迹")
            st.caption("这一栏体现「不是念题库」：每一轮追问或换题都有明确依据；「工具调用」列显示面试官是否主动查阅了简历原文。")
            st.dataframe(decisions, hide_index=True)

        st.subheader("完整文字稿")
        for msg in st.session_state.history:
            speaker = "面试官" if msg["role"] == "assistant" else "候选人"
            st.markdown(f"**{speaker}**：{msg['content']}")

    with tab4:
        render_history_tab()

    if st.button("重新开始面试", type="primary"):
        clear_snapshot()
        st.session_state.status = "idle"
        st.session_state.profile = None
        st.rerun()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> None:
    init_state()
    render_sidebar()

    if st.session_state.error:
        st.error(st.session_state.error)

    status = st.session_state.status
    if status == "idle" or st.session_state.profile is None:
        st.title("知遇 · AI 虚拟面试官")
        st.caption("多场景 · 多专业 · 多难度自适应的模拟面试智能体")
        # 有未完成的面试快照时，先给出「继续 / 重新开始」的选择，不做静默恢复
        render_recovery_banner()
        st.markdown(
            """
在左侧完成配置（**场景 → 专业 → 难度**，以及**面试官情绪**），填好 API Key 后点击「开始面试」。

面试官不会念题库：他会根据你的回答现场判断是**继续追问**还是**换下一道题**，
情绪也会随你的回答实时变化。面试结束后自动生成**评分报告**和**教练复盘**
（含归因分析和可以直接背的示范答案）。
            """
        )
        st.info("小提示：技术面和专业方向相关，行为面则主要考 STAR 结构，可分别各练一场。")
    elif status == "running":
        render_interview()
    else:
        render_report()


main()