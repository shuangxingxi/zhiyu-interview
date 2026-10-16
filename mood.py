"""面试官情绪状态机 —— 让面试官像真人一样有状态波动。

设计说明（对应 DESIGN.md 第五节）：
    这个模块只做一件事：根据「候选人刚给的回答」和「面试官上一轮的决策」，
    推出面试官此刻的内部状态。状态本身不给候选人看，而是分三个方向流出去：
        措辞与追问脾气 → agents.py 注入提示词
        追问上限       → agents.py 的 _apply_fallbacks
        语速与音调     → voice.py
    面试结束后，整条状态轨迹交给评估官做归因（见 app.py 的 mood_trace）。

    刻意写成纯函数：无随机、无全局状态、无 IO。
    同一串输入永远得到同一条轨迹，这样既能离线单测，也让归因报告站得住脚。

情绪与难度是两条正交的轴：
    难度（knowledge.LEVEL_STYLE）决定「问什么、问多深」；
    情绪（本模块）决定「用什么态度问」。
"""

from __future__ import annotations

import re
from typing import Any

PATIENCE_MAX = 3
ENERGY_MAX = 2
PRESSURE_MAX = 2

# 量化信号：出现这些说明候选人在用事实说话，面试官会更耐心。
# 刻意不收「有个数字就算」，也不收光秃秃的 "QPS" ——「大概是 2019 年」「几百 QPS」
# 这类没有具体数值的说法不算量化成果，必须数字和单位一起出现。
# 中文口语里「数字在前」（3000 QPS）和「单位在前」（QPS 3000）两种说法都常见，都收。
_QUANTIFIED = re.compile(
    r"\d+\s*(?:%|ms|毫秒|秒|倍|万|次|QPS|qps)|(?:QPS|qps)\s*\d+|[Pp]9\d"
)
# 因果连接词：配合足够长度，说明候选人在讲逻辑而不是背结论
_CAUSAL = re.compile(r"因为|所以|原因是|由于|导致|是为了")

_MOOD_HINT: dict[str, str] = {
    "投入": "你既有精力也有耐心，愿意听候选人展开，可以给一点正向反馈。",
    "例行公事": "你有些疲倦，回复从简，别寒暄，只推进问题本身。",
    "急躁": "你时间紧、耐心不足，跳过客套直接追问，不给候选人缓冲。",
    "冷淡": "你没什么反应，回复极短，不评价、不引导、不鼓励。",
}


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def initial(patience: int = 2, energy: int = 2, pressure: int = 0) -> dict[str, Any]:
    """开局状态。各模式的初值由 knowledge.MOOD_MODES 给定。"""
    return {
        "patience": _clamp(patience, 0, PATIENCE_MAX),
        "energy": _clamp(energy, 0, ENERGY_MAX),
        "pressure": _clamp(pressure, 0, PRESSURE_MAX),
        "ask_streak": 0,
    }


def update(
    state: dict[str, Any],
    *,
    answer: str = "",
    decision: dict[str, Any] | None = None,
    elapsed_min: float = 0.0,
    planned_rounds: int = 5,
    cur_q: int = 0,
) -> dict[str, Any]:
    """推进一轮状态。返回新字典，不修改传入的 state。

    参数：
        answer         —— 候选人这一轮的回答原文
        decision       —— 面试官**上一轮**的决策（用它判断上轮是追问还是换题）
        elapsed_min    —— 面试已进行的分钟数
        planned_rounds —— 计划的主问题数量
        cur_q          —— 当前已问出的主问题数量
    """
    s = dict(state)
    action = str((decision or {}).get("action") or "").strip().lower()
    gap = str((decision or {}).get("gap") or "")

    # 1) 上一轮是追问，且缺口在量化结果或技术原理上 —— 消耗耐心
    if action == "probe" and any(key in gap for key in ("量化", "原理")):
        s["patience"] = s.get("patience", 2) - 1

    # 2) 连续两轮都能直接换题 —— 一直答得不错，耐心回升
    s["ask_streak"] = s.get("ask_streak", 0) + 1 if action == "ask" else 0
    if s["ask_streak"] == 2:
        s["patience"] = s.get("patience", 2) + 1

    # 3) 从这一轮的回答里读信号
    text = (answer or "").strip()
    if _QUANTIFIED.search(text):
        s["patience"] = s.get("patience", 2) + 1
        s["energy"] = s.get("energy", 2) + 1
    if len(text) > 120 and _CAUSAL.search(text):
        s["energy"] = s.get("energy", 2) + 1

    # 4) 时间压力：拖过计划用时，或主问题已经问满
    if elapsed_min > planned_rounds * 4 or cur_q >= planned_rounds:
        s["pressure"] = s.get("pressure", 0) + 1

    s["patience"] = _clamp(s["patience"], 0, PATIENCE_MAX)
    s["energy"] = _clamp(s["energy"], 0, ENERGY_MAX)
    s["pressure"] = _clamp(s["pressure"], 0, PRESSURE_MAX)
    return s


def mood_of(state: dict[str, Any]) -> str:
    """由耐心 × 精力派生出对外的表现标签。

    耐心高 + 精力高 → 投入      耐心高 + 精力低 → 例行公事
    耐心低 + 精力高 → 急躁      耐心低 + 精力低 → 冷淡
    """
    patient = int(state.get("patience", PATIENCE_MAX)) >= 2
    energetic = int(state.get("energy", ENERGY_MAX)) >= 1
    if patient and energetic:
        return "投入"
    if patient:
        return "例行公事"
    if energetic:
        return "急躁"
    return "冷淡"


def describe(state: dict[str, Any]) -> str:
    """给面试官提示词用的【当前状态】段落。

    只描述态度与节奏，并带一条硬红线：只质疑答案，不许评价人。
    """
    mood = mood_of(state)
    lines = [
        f"当前状态：{mood}"
        f"（耐心 {state.get('patience', 0)}/{PATIENCE_MAX}，"
        f"精力 {state.get('energy', 0)}/{ENERGY_MAX}，"
        f"时间压力 {state.get('pressure', 0)}/{PRESSURE_MAX}）",
        _MOOD_HINT[mood],
    ]
    if int(state.get("pressure", 0)) >= 1:
        lines.append(
            "时间压力偏高：跳过一切客套与过渡，第一句直接承接上一轮，且不超过 8 个字。"
        )
    if mood == "冷淡":
        lines.append("不要出现「嗯」「不错」这类回应，问完就停，不要解释。")
    elif mood == "急躁":
        lines.append("不要复述候选人的话，不要解释你为什么要问这个。")
    lines.append(
        "以上状态只改变你的态度和节奏：可以质疑答案本身，"
        "但禁止评价候选人的人格、学历、家庭或背景。"
    )
    return "\n".join(lines)


def effective_max_probes(profile: dict[str, Any], state: dict[str, Any]) -> int:
    """情绪调制后的追问上限。

    不耐烦的面试官会更快放弃这道题，耐心的愿意多给一次机会。
    但上限的最终裁决权仍在代码手里：夹在 1~4 之间，且严格模式下一律用配置里的固定值，
    保证"录演示视频时每场节奏一致"这个前提不被情绪轴破坏。
    """
    base = int(profile.get("max_probes") or 2)
    if profile.get("strict"):
        return base
    return _clamp(base + int(state.get("patience", 2)) - 1, 1, 4)


def summarize(trace: list[dict[str, Any]], mode_label: str = "") -> str:
    """把状态轨迹压成一两句可读的话，给评估官和报告页用。"""
    if not trace:
        return "本场没有记录到状态变化。"
    first, last = trace[0], trace[-1]
    head = f"情绪模式：{mode_label}；" if mode_label else ""
    return (
        f"{head}状态从「{first.get('mood', '')}」走到「{last.get('mood', '')}」，"
        f"耐心 {first.get('patience', 0)}→{last.get('patience', 0)}，"
        f"精力 {first.get('energy', 0)}→{last.get('energy', 0)}，"
        f"时间压力 {first.get('pressure', 0)}→{last.get('pressure', 0)}。"
    )
