# -*- coding: utf-8 -*-
"""表达的客观指标：字数、答题时长、语速、语气填充词。

报告里关于「你答得怎么样」此前全是模型的评语——同样是「表达不够清晰」，
可能是语速太快，也可能是嗯嗯啊啊太多，还可能是答得太短。这些都能直接量出来，
不需要再过一遍模型，也不会随模型心情变化。

三道刻意的边界：

1. **只统计有计时数据的回答**。`elapsed` 是 v17 起才写的字段，旧记录没有；
   拿 0 当分母会让语速变成无穷大，所以缺计时的题直接不进统计，只报「N 题无计时」。
2. **填充词只认语气词，不认正常连接词**。「然后」「就是」「其实」在中文里是
   合法的连接与限定，把它们算成口癖会让每个正常人都被判「填充词过多」。
   只数「嗯 / 呃 / 额 / 那个 / 就是说 / 怎么说呢 / 这个这个」这类。
3. **语速是区间不是分数**。A 档要求把原理讲透，慢一点正常；C 档要干脆。
   超出区间只提示「偏快/偏慢」，不扣分——扣分权在评估官，这里只提供事实。
"""
from __future__ import annotations

import re
from typing import Any

# 语气填充词。刻意不含「然后 / 就是 / 其实 / 这个」这些正常连接词：
# 把它们算进去，等于把「说话有条理」误判成「口癖多」。
HESITATIONS: tuple[str, ...] = (
    "嗯",
    "呃",
    "额",
    "唉",
    "那个",
    "就是说",
    "怎么说呢",
    "这个这个",
    "那个那个",
    "反正吧",
)
_HESITATION_RE = re.compile("|".join(re.escape(word) for word in HESITATIONS))

# 语速区间（字/秒）：按难度档给参考带。慢一点正常，快一点也未必差，
# 所以只用于提示，不参与评分。
PACE_TARGET: dict[str, tuple[float, float]] = {
    "A": (3.0, 4.5),
    "B": (2.8, 4.2),
    "C": (2.5, 3.8),
}

# 低于这个长度的回答不参与「最短/最长」比较，也不参与语速统计：
# 一句话的「嗯…不知道」既不能说明语速，也不该拉低平均。
MIN_MEASURABLE_CHARS = 8

_SPACE_RE = re.compile(r"\s+")
# 标点不算"说出来的字"：中文里 ，。、； 只是停顿，把它们算进字数会把语速
# 高估一成左右；算进去还会让"标点多"看起来像"说得多"。
_PUNCT_RE = re.compile(r"[\s，。、；：？！…—～·「」『』（）()《》〈〉\"'“”‘’,.!?;:—\-]+")


def spoken_chars(text: str) -> int:
    """按口播长度算字数：去掉空白与标点，不按词切。"""
    return len(_PUNCT_RE.sub("", str(text or "")))


def count_fillers(text: str) -> int:
    """语气填充词出现次数（重叠不重复计，正则一次扫完）。"""
    return len(_HESITATION_RE.findall(str(text or "")))


def answer_metrics(text: str, elapsed: Any) -> dict[str, Any]:
    """单条回答的指标。没有合法计时数据时 `seconds` 与 `chars_per_second` 为 None。"""
    chars = spoken_chars(text)
    seconds: float | None = None
    try:
        value = float(elapsed)
    except (TypeError, ValueError):
        value = 0.0
    if value > 0:
        seconds = round(value, 1)
    cps = round(chars / seconds, 2) if seconds and seconds > 0 else None
    return {
        "chars": chars,
        "seconds": seconds,
        "chars_per_second": cps,
        "fillers": count_fillers(text),
    }


def session_metrics(transcript: list[dict[str, Any]] | None) -> dict[str, Any]:
    """一场面试的回答侧汇总。

    返回：
        rows        —— 每条可统计回答的 {index, chars, seconds, chars_per_second, fillers, fillers_per_100}
        count       —— 参与统计的回答条数
        skipped     —— 因为没有计时/太短而跳过的时间条数
        chars / seconds / chars_per_second —— 汇总值（语速按总字数 / 总时长算，不是各条平均）
        filler_count / filler_per_100     —— 填充词总数与每百字密度
        longest / shortest —— 字数最多的那条与最少的（只给字数，不暴露原文）
    """
    rows: list[dict[str, Any]] = []
    skipped = 0
    for message in transcript or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        metrics = answer_metrics(message.get("content", ""), message.get("elapsed"))
        if metrics["seconds"] is None or metrics["chars"] < MIN_MEASURABLE_CHARS:
            skipped += 1
            continue
        metrics["index"] = len(rows) + 1
        metrics["fillers_per_100"] = (
            round(metrics["fillers"] * 100 / metrics["chars"], 1) if metrics["chars"] else 0.0
        )
        rows.append(metrics)

    chars = sum(row["chars"] for row in rows)
    seconds = round(sum(row["seconds"] for row in rows), 1) if rows else 0.0
    fillers = sum(row["fillers"] for row in rows)
    return {
        "rows": rows,
        "count": len(rows),
        "skipped": skipped,
        "chars": chars,
        "seconds": seconds,
        "chars_per_second": round(chars / seconds, 2) if seconds else None,
        "filler_count": fillers,
        "filler_per_100": round(fillers * 100 / chars, 1) if chars else 0.0,
        "longest": max((row["chars"] for row in rows), default=0),
        "shortest": min((row["chars"] for row in rows), default=0),
    }


def pace_target(level: str) -> tuple[float, float]:
    """该难度档的语速参考区间。未知档回落 B。"""
    return PACE_TARGET.get(str(level or "").strip(), PACE_TARGET["B"])


def pace_verdict(chars_per_second: float | None, level: str) -> str:
    """给一句人话结论：偏慢 / 合适 / 偏快；没有数据时返回空串。"""
    if not chars_per_second:
        return ""
    low, high = pace_target(level)
    if chars_per_second < low:
        return "偏慢"
    if chars_per_second > high:
        return "偏快"
    return "合适"


def summary_line(metrics: dict[str, Any], level: str = "B") -> str:
    """把汇总压成一句可读结论；没有数据时返回空串。"""
    if not metrics.get("count"):
        return ""
    parts = [
        f"平均每题 {round(metrics['chars'] / metrics['count'])} 字、"
        f"{round(metrics['seconds'] / metrics['count'], 1)} 秒"
    ]
    cps = metrics.get("chars_per_second")
    if cps:
        verdict = pace_verdict(cps, level)
        low, high = pace_target(level)
        target = f"（{level} 档参考 {low:g}~{high:g} 字/秒）"
        parts.append(f"语速 {cps:g} 字/秒{target}，{verdict}" if verdict else f"语速 {cps:g} 字/秒")
    parts.append(
        f"语气填充词 {metrics['filler_count']} 次（每百字 {metrics['filler_per_100']:g} 个）"
    )
    return "；".join(parts)
