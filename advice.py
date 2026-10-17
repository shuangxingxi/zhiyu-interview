"""提升建议：把「本场评分维度」翻译成「下一场面试前该补什么」（纯函数）。

为什么单独成一个模块：与 `mood.py` / `interview_state.py` / `history_store.py`
同一个理由。这里判断的是三件**确定性**的事——哪些维度是短板、按什么顺序补、
模型给的每条建议能不能用（维度是否真实存在、有没有原话依据）。它们都不该混在
Streamlit 或模型调用里，否则只能靠"看着像对"来判断，没法离线钉住。

与「教练复盘」的分工（刻意不重叠）：

| | 视角 | 改的是什么 |
|---|---|---|
| 教练 `coach_review` | 回看型 | 这一场你哪句话答得不好，那句话本来该怎么讲（`rewrites`） |
| 建议官 `advisor_review` | 前看型 | 这个能力维度上你缺的是哪一类东西，下次面试前做什么动作补上（`actions`） |

两者都要求引用候选人原话；但一个改「说法」，一个补「能力与经历」。
"""

from __future__ import annotations

from typing import Any

# 维度分低于它就算"明显短板"，在提示词里被标出来并优先排序
WEAK_SCORE = 70
MAX_PRIORITY = 3
MAX_QUICK_WINS = 4
MAX_ACTIONS = 3


# ---------------------------------------------------------------------------
# 取值规整
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    """压成单行、去首尾空白；None / 数字 / 嵌套值都安全。"""
    return " ".join(str(value or "").split())


def _score(value: Any) -> int | None:
    """把分值规整成 0~100 的整数；非数值一律 None。

    `True` 在 Python 里也是 int，必须显式排除——与 `history_store._as_score`
    同一条约定：一个布尔值不该被当成 1 分或 100 分。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, min(100, int(round(value))))
    return None


def _clean_list(value: Any, limit: int) -> list[str]:
    """把模型给的列表收敛成去空、去重、限量的一串字符串。"""
    if not isinstance(value, (list, tuple)):
        value = [value] if value else []
    out: list[str] = []
    for item in value:
        text = _text(item)
        if text and text not in out:
            out.append(text)
    return out[:limit]


# ---------------------------------------------------------------------------
# 维度：规整 / 排序 / 拼段落
# ---------------------------------------------------------------------------


def dimension_rows(evaluation: dict[str, Any] | None) -> list[dict[str, Any]]:
    """取出评估官给的维度，规整成 `[{"name", "score", "comment"}]`。

    只认结构合法的条目：非 dict、没有名字的一律丢弃。报告里的建议要与评分页
    显示同一份维度，这里多出一条凭空冒出来的维度，反而会让人以为评委判了它。
    """
    rows: list[dict[str, Any]] = []
    for item in (evaluation or {}).get("dimensions") or []:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"))
        if not name:
            continue
        rows.append(
            {
                "name": name,
                "score": _score(item.get("score")),
                "comment": _text(item.get("comment")),
            }
        )
    return rows


def weakest_dimensions(
    evaluation: dict[str, Any] | None, limit: int = 2
) -> list[str]:
    """分最低的若干维度名；没有可用分数时返回空列表。

    分数缺失的维度不参与排序：不知道分，就不该假装它最弱。
    """
    rows = [row for row in dimension_rows(evaluation) if row["score"] is not None]
    if not rows:
        return []
    rows.sort(key=lambda row: row["score"])
    return [row["name"] for row in rows[: max(1, int(limit))]]


def advice_block(evaluation: dict[str, Any] | None) -> str:
    """拼给建议官的「本场得分与评委结论」段落，短板排在前面。

    刻意与 `knowledge.rubric_block`（给评估官）不同：那份是"判分依据"，
    这份是"下一步该往哪补"。同一份维度分换一种排法，就能把注意力从
    "我几分"转到"我缺哪一块"。
    """
    ev = evaluation or {}
    rows = dimension_rows(ev)
    lines: list[str] = []

    if rows:
        # 先从低到高排：报告里最该先补的那条永远出现在第一行
        ordered = sorted(rows, key=lambda row: (row["score"] is None, row["score"] or 0))
        lines.append("【本场维度得分】（从低到高排，上面的是最该先补的）")
        for row in ordered:
            score = "—" if row["score"] is None else str(row["score"])
            line = f"- {row['name']}：{score} 分"
            if row["comment"]:
                line += f"｜评委评语：{row['comment']}"
            if row["score"] is not None and row["score"] < WEAK_SCORE:
                line += "（短板）"
            lines.append(line)
    else:
        lines.append("（本场没有维度分，请只依据面试记录给通用建议。）")

    for title, key in (("评委指出的亮点", "highlights"), ("评委指出的风险", "risks")):
        items = _clean_list(ev.get(key), 5)
        if items:
            lines.append(f"【{title}】")
            lines.extend(f"- {item}" for item in items)

    if _text(ev.get("summary")):
        lines.append(f"【评委总评】{_text(ev.get('summary'))}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 收敛模型输出
# ---------------------------------------------------------------------------


def _placeholder(name: str, score: int | None) -> dict[str, Any]:
    """某个维度这一场没有拿到建议时的占位条目。"""
    return {
        "dimension": name,
        "score": score,
        "gap": "",
        "evidence": "",
        "has_evidence": False,
        "actions": [],
        "self_check": "",
        "missing": True,
    }


def normalize(
    data: dict[str, Any] | None, evaluation: dict[str, Any] | None = None
) -> dict[str, Any]:
    """收敛建议官输出，产出报告与导出都能直接用的确定结构。

    三条硬规则：

    1. `dimension` 必须真的出现在**本场维度**里，否则丢弃——模型不能自己发明
       一个维度，更不能把别的专业的维度塞进来；
    2. 每个维度至多一条建议，重复的只留第一条；
    3. 没给出建议的维度**补一条占位**（`missing=True`），报告里如实显示
       "该维度本次无建议"。刻意不静默丢弃：维度是从评分页抄过来的，
       缺一条会让人以为只是漏渲染了。

    `evidence` 为空时记 `has_evidence=False`。这里**不做**"必须逐字命中
    逐字稿"的强校验：建议官经常要引用"整场没有量化结果"这类**表现**而不是原话，
    强行要求逐字匹配会把正确的话也判死。报告侧把没有原话依据的条目如实标出来即可。
    """
    payload = data if isinstance(data, dict) else {}
    rows = dimension_rows(evaluation)
    allowed = [row["name"] for row in rows]
    scores = {row["name"]: row["score"] for row in rows}

    items = payload.get("advice")
    if not isinstance(items, list):
        items = []

    kept: dict[str, dict[str, Any]] = {}
    dropped = 0
    for raw in items:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        name = _text(raw.get("dimension"))
        gap = _text(raw.get("gap"))
        actions = _clean_list(raw.get("actions"), MAX_ACTIONS)
        # 维度名不在本场维度里（allowed 非空时）→ 丢弃
        if not name or (allowed and name not in allowed) or (not gap and not actions):
            dropped += 1
            continue
        if name in kept:
            dropped += 1
            continue
        evidence = _text(raw.get("evidence"))
        kept[name] = {
            "dimension": name,
            "score": scores.get(name),
            "gap": gap,
            "evidence": evidence,
            "has_evidence": bool(evidence),
            "actions": actions,
            "self_check": _text(raw.get("self_check")),
            "missing": False,
        }

    if allowed:
        # 按评分页的维度顺序输出，没拿到建议的补占位
        advice = [kept.get(name) or _placeholder(name, scores.get(name)) for name in allowed]
    else:
        # 评估官没给维度（降级情形）：无从校验，直接采信模型自己报的名字
        advice = list(kept.values())

    return {
        "advice": advice,
        "priority": _clean_list(payload.get("priority"), MAX_PRIORITY),
        "quick_wins": _clean_list(payload.get("quick_wins"), MAX_QUICK_WINS),
        "next_setup": _text(payload.get("next_setup")),
        "dropped": dropped,
        "evidence_missing": sum(
            1 for item in advice if not item["missing"] and not item["has_evidence"]
        ),
    }
