"""结局回填与评分校准 —— 纯函数，不碰文件、也不碰 Streamlit。

面试评分本质上是一次**预测**，候选人的真实结局（进下一轮 / 拿到 offer / 被拒）才是**标签**。
没有标签时只能看"分数涨没涨"；有了标签才能回答"我的分数准不准、是偏乐观还是偏保守"。

刻意**不改写评分**：评分口径由「场景 × 专业 × 难度」三级配置唯一决定（DESIGN 第三节），
若拿历史结局反过来调分，口径会随样本漂移，与"提问与评分同源、可控可解释"这条设计前提冲突。
所以这一版只做对照与归因——把结论摆在用户面前，要不要据它调整由人决定。

对外主要暴露：
    OUTCOMES / POSITIVE_OUTCOMES / NEGATIVE_OUTCOMES —— 结局词汇表
    outcome_of(record)        —— 读一场记录回填的结局（异常一律返回"未回填"）
    calibrate(records)        —— 把"评分 vs 结局"对照成一份可直接展示的结论
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 结局词汇表
#
# "" 就是"用户还没回填"，所以它是下拉框的第一项，也是所有异常取值的归处——
# 多一个"未知结局"状态没有意义，回填这个动作本身就该由人来完成。
# ---------------------------------------------------------------------------
OUTCOME_UNSET = ""
OUTCOME_NEXT_ROUND = "进入下一轮"
OUTCOME_OFFER = "拿到 offer"
OUTCOME_REJECTED = "被拒"
OUTCOME_PENDING = "还没结果"
OUTCOME_NA = "不适用"

OUTCOMES: list[str] = [
    OUTCOME_UNSET,
    OUTCOME_NEXT_ROUND,
    OUTCOME_OFFER,
    OUTCOME_REJECTED,
    OUTCOME_PENDING,
    OUTCOME_NA,
]

# 只有"确实过了 / 确实没过"才拿来对照评分：
# "还没结果"和"不适用"都缺少标签，算进来只会稀释结论。
POSITIVE_OUTCOMES = frozenset({OUTCOME_NEXT_ROUND, OUTCOME_OFFER})
NEGATIVE_OUTCOMES = frozenset({OUTCOME_REJECTED})

# 分档线：分数到了高分线却被拒 → 评分偏乐观；没到线却晋级 → 偏保守。
# 两个值只用于**对照展示**，不参与任何评分计算，所以可以随时按自己的体感调。
HIGH_SCORE = 75.0
LOW_SCORE = 55.0

# 少于这么多场有标签的面试就不下结论——两场就总结趋势是过度解读。
MIN_SAMPLES_FOR_CONCLUSION = 3

# 同一个难度档至少这么多场才点评"全过 / 全拒"。
MIN_LEVEL_SAMPLES = 2


def _as_score(value: Any) -> float | None:
    """把分值规整成 float；非数值（含 bool）一律 None。

    `True` 在 Python 里也是 int，必须显式排除，否则会被当成 1 分。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def normalize_outcome(value: Any) -> str:
    """把任意取值收敛成合法结局；不认识的（含空、None、被改坏的历史文件）当"未回填"。"""
    text = str(value or "").strip()
    return text if text in OUTCOMES else OUTCOME_UNSET


def outcome_of(record: Any) -> str:
    """读一场记录回填的结局。

    兼容两种落盘形态：`{"value": ..., "note": ...}`（当前写法）与纯字符串
    （早期或手改过的文件），结构异常一律当"未回填"，绝不让历史文件把界面搞崩。
    """
    if not isinstance(record, dict):
        return OUTCOME_UNSET
    outcome = record.get("outcome")
    if isinstance(outcome, dict):
        return normalize_outcome(outcome.get("value"))
    return normalize_outcome(outcome)


def outcome_note(record: Any) -> str:
    """读一场记录回填的备注；没有就返回空串。"""
    if not isinstance(record, dict):
        return ""
    outcome = record.get("outcome")
    if isinstance(outcome, dict):
        return str(outcome.get("note") or "").strip()
    return ""


def outcome_label(record: Any) -> str:
    """给界面/报告用的一行结局标签（未回填时明确写出来，而不是留空）。"""
    return outcome_of(record) or "未回填"


def _level_of(record: dict) -> str:
    """难度档：拿不到就归到"未标难度"，宁可显示得糙一点也不要漏掉这一场。"""
    profile = record.get("profile")
    level = profile.get("level") if isinstance(profile, dict) else ""
    return str(level or "").strip() or "未标难度"


def _notes(
    samples: int,
    decided: int,
    hits: int,
    misses: int,
    unclear: int,
    optimistic: int,
    conservative: int,
    by_level: dict[str, dict[str, Any]],
    labeled: int = 0,
) -> list[str]:
    """把计数翻成人话。结论只在样本够时才给，样本不够就直说不够。"""
    if samples == 0:
        if labeled:
            # 回填了，但填的全是"还没结果 / 不适用"——不能反过来说没回填过
            return [
                f"已回填 {labeled} 场的结局，但都还不是明确结果"
                "（「还没结果」「不适用」这类）。等结果出来再改成「进入下一轮 / 拿到 offer / 被拒」，"
                "这里才能开始对照。"
            ]
        return ["还没有回填过结局的面试。回填之后，这里会告诉你评分准不准。"]
    if decided < MIN_SAMPLES_FOR_CONCLUSION:
        return [
            f"已回填 {samples} 场有明确结果的面试，其中只有 {decided} 场的分数给出了"
            f"明确判断，样本还太少（凑到 {MIN_SAMPLES_FOR_CONCLUSION} 场再下结论更稳）。"
        ]

    rate = round(100 * hits / decided)
    notes = [
        f"{samples} 场已回填结局的面试里，评分与结局一致 {hits} 场、"
        f"不一致 {misses} 场（{rate}%）。"
    ]

    if optimistic > conservative:
        notes.append(
            f"其中 {optimistic} 场分数到了高分线之上却被拒——自评容易偏乐观，"
            "报告里的高分要打个折看。"
        )
    elif conservative > optimistic:
        notes.append(
            f"其中 {conservative} 场分数落在低分线之下却晋级——自评偏保守，"
            "别因为分数不高就放弃投递。"
        )
    elif optimistic:
        notes.append(f"偏乐观与偏保守各 {optimistic} 场，没有单向偏移。")
    else:
        notes.append("已经对得上号的场次里，评分与结局完全一致。")

    if unclear:
        notes.append(
            f"另有 {unclear} 场分数落在 {LOW_SCORE:g}~{HIGH_SCORE:g} 之间——"
            "这个区间评分本身就没给明确判断，所以不计入准确率。"
        )

    for level, stats in sorted(by_level.items()):
        if stats["samples"] < MIN_LEVEL_SAMPLES:
            continue
        if stats["positive"] == 0:
            notes.append(
                f"{level} 档 {stats['samples']} 场全部被拒（均分 {stats['avg_score']:g}），"
                "这个档位的准备方向需要重估。"
            )
        elif stats["negative"] == 0:
            notes.append(
                f"{level} 档 {stats['samples']} 场全部通过，可以往上加一档难度试试。"
            )
    return notes


def calibrate(records: Any) -> dict[str, Any]:
    """把历史记录里"评分 vs 真实结局"对照成一份可直接展示的结论。

    判定用的是**两条线夹出一个灰区**，而不是单条线：
        >= HIGH_SCORE 且晋级 —— 对得上；<= LOW_SCORE 且被拒 —— 对得上；
        <= LOW_SCORE 却晋级   —— 偏保守；  >= HIGH_SCORE 却被拒 —— 偏乐观；
        落在两条线之间        —— 评分本身没给明确判断，单独计数、不算对错。
    留这个灰区是为了不冤枉评分：60 分被拒既不算"预测对了"也不算"偏乐观"。

    返回结构（界面与报告共用）：
        scored / labeled          —— 有分数的场次数 / 已回填结局的场次数
        samples                   —— 结局明确为过或没过的场次数
        outcome_counts            —— 各结局分布（含"还没结果"这类不可校准的）
        hits / misses / decided   —— 对得上 / 对不上 / 参与准确率计算的场次
        accuracy                  —— 准确率，decided 为 0 时是 None
        unclear                   —— 落在灰区、不计入准确率的场次数
        optimistic / conservative —— 偏乐观 / 偏保守的场次数
        by_level                  —— 按难度档的样本数、正负结局数、均分
        notes                     —— 一句话结论
    """
    rows = [r for r in (records or []) if isinstance(r, dict)]

    outcome_counts: dict[str, int] = {}
    scored = 0
    labeled = 0
    hits = misses = unclear = optimistic = conservative = 0
    by_level: dict[str, dict[str, Any]] = {}
    level_scores: dict[str, list[float]] = {}

    for record in rows:
        score = _as_score(record.get("overall_score"))
        if score is None:
            # 没生成报告的场次没有分数，没法当预测来对照
            continue
        scored += 1

        outcome = outcome_of(record)
        if outcome:
            outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
            labeled += 1

        positive = outcome in POSITIVE_OUTCOMES
        if not positive and outcome not in NEGATIVE_OUTCOMES:
            continue  # 没有明确标签的场次只计数，不参与校准

        level = _level_of(record)
        stats = by_level.setdefault(
            level, {"level": level, "samples": 0, "positive": 0, "negative": 0}
        )
        stats["samples"] += 1
        stats["positive" if positive else "negative"] += 1
        level_scores.setdefault(level, []).append(score)

        if (positive and score >= HIGH_SCORE) or (not positive and score <= LOW_SCORE):
            hits += 1
        elif (positive and score <= LOW_SCORE) or (not positive and score >= HIGH_SCORE):
            misses += 1
            if positive:
                conservative += 1
            else:
                optimistic += 1
        else:
            unclear += 1

    samples = hits + misses + unclear
    decided = hits + misses
    for level, stats in by_level.items():
        scores = level_scores.get(level) or []
        stats["avg_score"] = round(sum(scores) / len(scores), 1) if scores else None

    return {
        "scored": scored,
        "labeled": labeled,
        "samples": samples,
        "outcome_counts": outcome_counts,
        "hits": hits,
        "misses": misses,
        "decided": decided,
        "accuracy": round(hits / decided, 3) if decided else None,
        "unclear": unclear,
        "optimistic": optimistic,
        "conservative": conservative,
        "by_level": by_level,
        "notes": _notes(
            samples, decided, hits, misses, unclear, optimistic, conservative, by_level, labeled
        ),
    }
