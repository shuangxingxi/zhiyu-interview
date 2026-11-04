"""复盘报告与简历建议的 Markdown 生成层 —— 纯函数，不 import streamlit。

为什么单独一层：报告文本是用户唯一会带走、会存进参赛材料的产物，但它原本
长在 app.py 里，和 st.* 混在一起。`import app` 会执行 st.set_page_config，
测试根本导入不了，于是这两百多行字符串拼装长期没有任何断言——改错一个字段
名，只有人工点开界面才会发现。

这里只保留「给定输入 → 固定文本」：取数（st.session_state）、按需查
history.json、落盘都留在 app.py。所以本模块可以像 delivery / mood 那样
直接单测，不需要跑 Streamlit 运行时。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

import calibration
import delivery
import jd_match
import mood
from knowledge import (
    LEVELS,
    TIER_LABELS,
    VERDICT_FAIL,
    VERDICT_PARTIAL,
    VERDICT_UNKNOWN,
    aggregate_rubric_check,
    answer_outline,
    profile_summary,
    topic_tier,
)

_NUMERALS = "一二三四五六七八九十"


def _cn_num(index: int) -> str:
    """1 → 一，10 → 十，超出范围原样返回阿拉伯数字（不至于崩）。"""
    if 1 <= index <= len(_NUMERALS):
        return _NUMERALS[index - 1]
    return str(index)


class _Sections:
    """按「实际出现的顺序」发章节号。

    为什么不写死数字：报告各章节是有开关的（没有教练复盘、没有决策轨迹都
    可能整节不出现）。写死 `## 二、教练复盘` 就会出现「一、三、五」跳号，
    打印出来像缺页。这里改成只在真的写了这一节时才占号。
    """

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self._count = 0

    def heading(self, title: str) -> None:
        self._count += 1
        self._lines.append(f"## {_cn_num(self._count)}、{title}")
        self._lines.append("")


# ---------------------------------------------------------------------------
# 页面与导出共用的四个纯判断
#
# 这四条原本也在 app.py，但既然 report_text 要用、render_report 也要用，
# 放这里能让"页面怎么显示"和"导出怎么写"共用同一份判断——两处各写一遍
# 迟早会漂移，而用户会同时看到界面和导出件。
# ---------------------------------------------------------------------------


def jd_progress(profile: dict, rubric_check: list[dict]) -> dict:
    """本场是 JD 定向场时，把 rubric_check 对回本场目标考点；否则返回空字典。"""
    jd_topics = [str(t).strip() for t in (profile.get("jd_topics") or []) if str(t).strip()]
    if not jd_topics:
        return {}
    return jd_match.session_progress(jd_topics, rubric_check, profile.get("major", ""))


def tier_distribution_caption(profile: dict, rubric_check: list[dict]) -> str:
    """本场**实际问到**的考点在 A/B/C 三档上怎么分布，拼成一行 markdown。

    为什么不回读 profile["topic_tiers"]：那是抽池时的构成，不等于这场真问了什么。
    用问过的考点现算，才能回答「这场到底考了几道高难题」。
    行为面 / JD 定向场 / 查不到档位时不输出（不编一个「未知」出来凑数）。
    """
    asked = [
        str(row.get("topic", "")).strip()
        for row in (rubric_check or [])
        if str(row.get("topic", "")).strip()
    ]
    if not asked:
        return ""
    major = str(profile.get("major", ""))
    tiers = [topic_tier(major, topic) for topic in asked]
    if not any(tiers):
        return ""
    counts = Counter(tier for tier in tiers if tier)
    parts = [
        f"{TIER_LABELS[tier]} {counts[tier]} 道"
        for tier in LEVELS
        if counts.get(tier, 0) > 0
    ]
    unknown = len([tier for tier in tiers if not tier])
    line = f"**本场实际档位分布**：{'、'.join(parts)}"
    if unknown:
        line += f"（另有 {unknown} 条考点不在档位表里，未计入）"
    return line + "。"


def rate_text(hit: Any, missed: Any) -> str:
    """要点命中率的一格文本：`3/5（60%）`；没有可比要点时给「—」。"""
    got = len([x for x in (hit or []) if str(x).strip()])
    lost = len([x for x in (missed or []) if str(x).strip()])
    total = got + lost
    if not total:
        return "—"
    return f"{got}/{total}（{got * 100 // total}%）"


def answer_outline_lines(entries: list[dict], major: str) -> list[str]:
    """把「这类题该怎么答」渲染成 Markdown 行，导出与页面共用同一份判断。

    做成纯函数是为了可测：导出路径在 AppTest 里拿不到 download_button 的载荷，
    所以把"该输出什么"与"怎么塞进 Streamlit"分开。
    """
    lines = [
        "### 这类题该怎么答",
        "",
        "> 只给结构，不给标准答案：照着念不会加分，评分看的是你自己讲出的要点。",
    ]
    for entry in entries:
        outline = answer_outline(major, str(entry.get("topic", "")))
        if not outline:
            continue
        lines += ["", f"**{entry['topic']}**", ""]
        for idx, step in enumerate(outline["outline"], 1):
            lines.append(f"{idx}. {step}")
        if outline["avoid"]:
            lines += ["", f"别这么说：{outline['avoid']}"]
    return lines if len(lines) > 3 else []


# ---------------------------------------------------------------------------
# 导出件一：模拟面试复盘报告
# ---------------------------------------------------------------------------


def report_markdown(
    *,
    profile: dict | None = None,
    evaluation: dict | None = None,
    coach: dict | None = None,
    advice: dict | None = None,
    trace: list[dict] | None = None,
    history: list[dict] | None = None,
    decisions: list[dict] | None = None,
    usage: dict | None = None,
    asked_topics: list[str] | None = None,
    outcome_record: dict | None = None,
    now: datetime | None = None,
) -> str:
    """把一场面试的全部结果整理成 Markdown。

    `outcome_record` 是 history.json 里本场那条记录（用户回填过结局才有内容）；
    由调用方查好传进来，本层不碰文件系统。
    """
    profile = profile or {}
    evaluation = evaluation or {}
    coach = coach or {}
    advice = advice or {}
    trace = trace or []
    history = history or []
    decisions = decisions or []
    usage = usage or {}
    lines: list[str] = []

    def add(text: str = "") -> None:
        lines.append(text)

    def table(headers: list[str], rows: list[list[Any]]) -> None:
        add("| " + " | ".join(headers) + " |")
        add("|" + "---|" * len(headers))
        for row in rows:
            add("| " + " | ".join(str(c).replace("|", "／") for c in row) + " |")
        add()

    def jd_outro(progress: dict) -> None:
        """JD 定向场的收尾块：过了哪些、还剩哪些。两处调用，抽出来免得写两遍。"""
        add("### JD 定向场收尾")
        add()
        table(
            ["考点", "判定", "原因"],
            [[i["topic"], i["label"], i["reason"]] for i in progress["topics"]],
        )
        if progress["remaining"]:
            add(
                f"**还剩 {len(progress['remaining'])} 条没过或没考到**："
                + "、".join(progress["remaining"])
            )
        else:
            add("本场目标考点都通过了。")
        add()

    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
    add("# 模拟面试复盘报告")
    add()
    add(f"- 生成时间：{stamp}")
    add(f"- 面试配置：{profile_summary(profile)}")
    if profile.get("jd_topics"):
        add(f"- JD 定向考点：{'、'.join(profile['jd_topics'])}")
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

    sections = _Sections(lines)

    if evaluation:
        sections.heading("评分")
        add(f"- 本场实际表现：**{evaluation.get('overall_score', '-')}**")
        add(
            "- 剥离面试官状态干扰后："
            f"**{evaluation.get('adjusted_score', evaluation.get('overall_score', '-'))}**"
        )
        add(f"- 评委结论：{evaluation.get('level', '-')}")
        # 回填过的结局写进报告：这份报告本身就是参赛/复盘材料，
        # "这场后来被拒了"比一个孤立分数更能说明问题。
        if outcome_record and calibration.outcome_of(outcome_record):
            add(f"- 真实结局（本人回填）：{calibration.outcome_label(outcome_record)}")
            if calibration.outcome_note(outcome_record):
                add(f"- 结局备注：{calibration.outcome_note(outcome_record)}")
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
            # 要点粒度的对照结果由代码升成考点粒度（规则见 knowledge.topic_verdict），
            # 不额外叫模型判一次——同一份记录每次得到同一个结论。
            agg = aggregate_rubric_check(profile, rubric_check)
            labels = {t["topic"]: t["label"] for t in agg["topics"]}
            sources = {t["topic"]: t["source"] for t in agg["topics"]}
            add("### 考点命中情况")
            add()
            add("> 参照是底线不是天花板：清单之外的正确答案同样算分，没命中不等于答错。")
            add()
            if agg["generated"]:
                add(
                    f"> 来源：「考点库」= 自建考点库里人工核对过的参照（{agg['library']} 条）；"
                    f"「现拟」= 评委按题面现场现拟的要点，仅供参考（{agg['generated']} 条）。"
                )
                add()
            table(
                ["考点", "来源", "判定", "命中率", "已命中", "遗漏", "误区"],
                [
                    [
                        item.get("topic", ""),
                        "考点库"
                        if sources.get(item.get("topic", "")) == "library"
                        else "现拟",
                        labels.get(item.get("topic", ""), ""),
                        rate_text(item.get("must_hit"), item.get("missed")),
                        "；".join(str(x) for x in (item.get("must_hit") or [])) or "—",
                        "；".join(str(x) for x in (item.get("missed") or [])) or "—",
                        "；".join(str(x) for x in (item.get("errors") or [])) or "—",
                    ]
                    for item in rubric_check
                ],
            )
            counts = agg["counts"]
            add()
            add(
                f"**考点级判定**：共 {agg['total']} 个考点，通过 {agg['passed']} 个，"
                f"部分通过 {counts[VERDICT_PARTIAL]} 个，不通过 {counts[VERDICT_FAIL]} 个，"
                f"本场未问到 {counts[VERDICT_UNKNOWN]} 个。"
            )
            if agg.get("hit_rate") is not None:
                add(
                    f"**要点命中率**：{agg['hit_total']} / {agg['planned_total']}"
                    f"（{agg['hit_rate'] * 100:.0f}%），只统计本场真的判过的考点。"
                )
            if agg["weak"]:
                add()
                for entry in agg["weak"]:
                    tag = "" if entry["source"] == "library" else "（现拟）"
                    add(f"- {entry['label']}｜{entry['topic']}{tag}：{entry['reason']}")
                add()
                for line in answer_outline_lines(agg["weak"], profile.get("major", "")):
                    if line:
                        add(line)
            tier_line = tier_distribution_caption(profile, rubric_check)
            if tier_line:
                add()
                add(tier_line)
            progress = jd_progress(profile, rubric_check)
            if progress.get("total"):
                add()
                jd_outro(progress)
        else:
            asked = [str(t).strip() for t in (asked_topics or []) if str(t).strip()]
            if asked:
                # 导出件与页面同口径：问过考点却一条判定都没有，就如实写在这里，
                # 而不是让读者以为「报告中本来就没有这一节」。
                add("### 考点级判定")
                add()
                add(
                    f"> 本场未能生成考点级判定。这场问到了 {len(asked)} 个考点"
                    f"（{'、'.join(asked)}），但评委没有给出可用的对照结论。"
                )
                add()
                # 与页面同源：JD 定向场即使判定缺失，也要写清「还剩哪几条、下一场练什么」。
                progress = jd_progress(profile, [])
                if progress.get("total"):
                    jd_outro(progress)

        delivery_metrics = delivery.session_metrics(
            [m for m in history if isinstance(m, dict)]
        )
        if delivery_metrics["count"]:
            add("### 表达节奏")
            add()
            add("> 本机计时，不经过模型；时长从面试官问完算到提交回答，已扣掉语音识别耗时。")
            add()
            add(delivery.summary_line(delivery_metrics, str(profile.get("level") or "B")))
            add()
            table(
                ["轮次", "字数", "答题时长（秒）", "语速（字/秒）", "填充词"],
                [
                    [
                        row["index"],
                        row["chars"],
                        row["seconds"],
                        row["chars_per_second"],
                        row["fillers"],
                    ]
                    for row in delivery_metrics["rows"]
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
        sections.heading("教练复盘")
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

    if advice.get("advice"):
        sections.heading("提升建议（下一场面试前该补什么）")
        add("> 与上一节的「教练复盘」互补：教练改的是**说法**（这句话该怎么讲），")
        add("> 这一节补的是**能力与经历**（这个维度缺什么、下次面试前做什么）。")
        add()
        if advice.get("priority"):
            add("### 如果只改一件事")
            add()
            for i, item in enumerate(advice["priority"], start=1):
                add(f"{i}. {item}")
            add()
        for item in advice["advice"]:
            add(f"### {item.get('dimension', '')}"
                + (f"（{item['score']} 分）" if item.get("score") is not None else ""))
            add()
            if item.get("missing"):
                add("- 本次未给出该维度的建议。")
                add()
                continue
            if item.get("gap"):
                add(f"- 缺什么：{item['gap']}")
            if item.get("evidence"):
                add(f"- 依据：{item['evidence']}")
            for action in item.get("actions") or []:
                add(f"- 动作：{action}")
            if item.get("self_check"):
                add(f"- 自测：{item['self_check']}")
            add()
        if advice.get("quick_wins"):
            add("### 明天就能改")
            add()
            for i, item in enumerate(advice["quick_wins"], start=1):
                add(f"{i}. {item}")
            add()
        if advice.get("next_setup"):
            add("### 下一场怎么练")
            add()
            add(advice["next_setup"])
            add()

    if decisions:
        sections.heading("面试官决策轨迹")
        add("> 这一栏体现「不是念题库」：每一轮追问或换题都有明确依据，「工具调用」列可见面试官是否主动查阅了简历原文。")
        add()
        table(
            ["主问题", "动作", "考点", "考察点", "工具调用", "面试官判断"],
            [[d.get(k, "") for k in ("主问题", "动作", "考点", "考察点", "工具调用", "面试官判断")] for d in decisions],
        )

    if trace:
        mode_label = profile.get("mood_mode", "")
        sections.heading(f"面试官状态轨迹（情绪模式：{mode_label or '—'}）")
        add(mood.summarize(trace, mode_label))
        add()
        table(
            ["轮次", "状态", "耐心", "精力", "时间压力"],
            [
                [i.get("round", ""), i.get("mood", ""), i.get("patience", ""), i.get("energy", ""), i.get("pressure", "")]
                for i in trace
            ],
        )

    if history:
        sections.heading("完整文字稿")
        for msg in history:
            speaker = "面试官" if msg.get("role") == "assistant" else "候选人"
            add(f"**{speaker}**：{msg.get('content', '')}")
            add()

    add("---")
    add()
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
# 导出件二：简历优化建议
# ---------------------------------------------------------------------------


def resume_advice_markdown(
    *,
    result: dict | None = None,
    file_name: str = "",
    target_role: str = "",
    now: datetime | None = None,
) -> str:
    """把简历优化建议整理成一份 Markdown 文本（用户主动导出时才产出）。"""
    result = result or {}
    lines: list[str] = []

    def add(text: str = "") -> None:
        lines.append(text)

    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
    add("# 简历优化建议")
    add()
    add(f"- 生成时间：{stamp}")
    add(f"- 简历文件：{file_name or '—'}")
    add(f"- 目标岗位：{str(target_role or '').strip() or '未指定'}")
    if result.get("score") is not None:
        add(f"- 简历评分：**{result['score']}**")
    add()
    if result.get("verdict"):
        add(f"> {result['verdict']}")
        add()

    sections = _Sections(lines)

    problems = result.get("problems") or []
    if problems:
        sections.heading("诊断")
        for item in problems:
            add(f"### {item.get('category', '') or '问题'}（{item.get('severity', '')}）")
            add()
            add(item.get("problem", ""))
            add()
            if item.get("evidence"):
                add(f"- 原文：{item['evidence']}")
            add(f"- 怎么改：{item.get('advice', '')}")
            add()

    rewrites = result.get("rewrites") or []
    if rewrites:
        sections.heading("改写对照")
        add("> 改写只用简历里已有的信息重新组织；本地已逐条核对原句出处，找不到出处的已剔除。")
        add()
        for i, item in enumerate(rewrites, start=1):
            add(f"**{i}.** {item.get('original', '')}")
            add()
            add(f"- 改写：{item.get('improved', '')}")
            if item.get("why"):
                add(f"- 为什么：{item['why']}")
            add()

    if result.get("missing"):
        sections.heading("还需要补的内容")
        for item in result["missing"]:
            add(f"- {item}")
        add()
    if result.get("keywords"):
        sections.heading("关键词建议")
        add("> 只列你真实掌握的方向，不建议堆砌不熟悉的名词。")
        add()
        for item in result["keywords"]:
            add(f"- {item}")
        add()
    if result.get("checklist"):
        sections.heading("投简历前自查")
        for item in result["checklist"]:
            add(f"- [ ] {item}")
        add()

    identity = result.get("identity") or []
    formatting = result.get("formatting") or {}
    if identity or formatting.get("issues"):
        sections.heading("身份与格式核对（本地）")
        for item in identity:
            add(f"- **{item.get('signal', '')}**")
            add(f"  - 原文：{item.get('quote', '')}")
            add(f"  - 怎么改：{item.get('advice', '')}")
        for issue in formatting.get("issues") or []:
            add(f"- {issue}")
        add()

    quant = result.get("quantification") or {}
    if quant.get("lines"):
        add("---")
        add(f"- 本地统计：{quant['lines']} 行里 {quant['quantified']} 行含量化结果"
            f"（{quant['ratio']:.0%}）。")
    dropped = int(result.get("dropped_rewrites") or 0)
    invented = int(result.get("invented_numbers") or 0)
    ungrounded = int(result.get("ungrounded_evidence") or 0)
    if dropped or ungrounded:
        add(f"- 本地核对剔除：{dropped} 条改写（其中 {invented} 条补了原文没有的数字）、"
            f"{ungrounded} 条诊断引用（对不上原文）。")
    add()
    add("> 本建议由「知遇」AI 虚拟面试智能体的简历优化功能生成。")
    add("> 改写只重组简历里已有的信息，不新增任何未写过的经历或数字；")
    add("> 标注为「缺数字」的地方请用你自己的真实数据补上。")
    return "\n".join(lines)
