"""面试计数器状态机 —— 与 Streamlit 解耦的纯逻辑。

为什么要单独一个模块：`app.advance()` 里风险最高的一段就是"按决策推进计数器"——
`cur_q` / `probes_used` / `reflects_in_a_row` 三者联动，一旦错了，表现出来是
**绕圈、超轮次、或该追问时不追问**，而且从界面上很难一眼看出是哪一轮错的。
这段逻辑本身完全不需要 Streamlit，抽出来就能离线直测。

（`app.py` 在 import 阶段就调用 `st.set_page_config`，没法在 unittest 里导入，
所以"能测"的前提是先把纯逻辑挪出来。）

三个计数器的语义（对应 DESIGN.md 第五节）：
    cur_q               已经问出的**主问题**数量
    probes_used         当前这道主问题已经**追问**的次数
    reflects_in_a_row   连续做了几次"不提问回合"（复述确认 / 澄清 / 极简反馈）

计数权必须握在代码手里，而不是交给模型自觉——这正是本模块存在的意义。
"""

from __future__ import annotations

from typing import Any

# 合法的四种动作。模型偶发填出别的值一律当作"不推进"，与 app.advance 的
# if/elif 链行为一致（既不推进题目，也不计追问）。
ACTION_ASK = "ask"
ACTION_PROBE = "probe"
ACTION_REFLECT = "reflect"
ACTION_END = "end"

ACTIONS = (ACTION_ASK, ACTION_PROBE, ACTION_REFLECT, ACTION_END)


def advance_counters(
    cur_q: int,
    probes_used: int,
    reflects_in_a_row: int,
    action: str,
) -> tuple[int, int, int]:
    """按本轮动作推进三个计数器，返回新的 (cur_q, probes_used, reflects_in_a_row)。

    规则：
        ask     —— 换新主问题：题目 +1，追问清零，连续非提问计数清零
        probe   —— 追问：题目不变，追问 +1，连续非提问计数清零
        reflect —— 非提问回合：只累计"连着说了几句没提问的话"
        end / 其它 —— 不推进任何计数

    注意 probe 与 reflect 都会把 reflects_in_a_row 清零：
    一旦重新开始提问，就不再是"连着没提问"了。
    """
    if action == ACTION_ASK:
        return cur_q + 1, 0, 0
    if action == ACTION_PROBE:
        return cur_q, probes_used + 1, 0
    if action == ACTION_REFLECT:
        return cur_q, probes_used, reflects_in_a_row + 1
    return cur_q, probes_used, reflects_in_a_row


def next_asked_topics(
    asked_topics: list[str], topic: Any
) -> tuple[list[str], str]:
    """把本轮考点并进"已考考点"，返回 (新列表, 规整后的考点)。

    去重在这里做：同一个考点重复出现时不重复追加。
    返回新列表而不是就地修改，是为了让调用方（和测试）拿得到清晰的边界。
    """
    text = str(topic).strip() if topic is not None else ""
    if not text:
        return list(asked_topics), ""
    if text in asked_topics:
        return list(asked_topics), text
    return [*asked_topics, text], text


def decision_row(
    cur_q: int,
    data: dict[str, Any],
    action_labels: dict[str, str],
) -> dict[str, Any]:
    """构造报告里「面试官决策轨迹」的一行。

    cur_q 传的是**推进之后**的值，与 app.advance 的记录口径一致
    （第 N 行的"主问题"就是这一轮问出的第 N 道题）。
    """
    action = str(data.get("action") or "")
    return {
        "主问题": cur_q,
        "动作": action_labels.get(action, action),
        "考点": str(data.get("topic") or "").strip(),
        "考察点": data.get("focus", ""),
        "工具调用": data.get("tool_trace", "") or "—",
        "面试官判断": data.get("note", ""),
    }


def mood_trace_row(cur_q: int, mood_state: dict[str, Any], mood_label: str) -> dict[str, Any]:
    """构造一条情绪状态轨迹记录（评估官与教练靠它做归因）。

    键名保持英文，与 `mood.summarize` / `agents.format_mood_trace` 一致；
    展示时再映射成中文表头，避免两处各读一套键名。
    """
    return {
        "round": cur_q,
        "mood": mood_label,
        "patience": mood_state.get("patience", 0),
        "energy": mood_state.get("energy", 0),
        "pressure": mood_state.get("pressure", 0),
    }
