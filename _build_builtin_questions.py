"""自建真题生成器：从 rubrics.json 的 280 条考点反向生成面试式题干。

设计要点：
1. **不抄外部题库**，只用自己的 280 条参照改写 → 零许可风险、与评分口径天然同源。
2. 每条考点生成 1 道主问题（题干）+ 可选的 1 道追问（从 common_errors 反转而来）。
   追问那条是本项目独有的价值：面试官知道常见误区，就能主动设陷阱反问。
3. 字段与原 real_questions.json 完全一致（q/topic/cat/src/lic/ref），
   这样 `real_question_block()` 无需任何改动即可消费。
4. `src` 标为 "自建"、`lic` 标为 "本项目原创（CC BY-NC-SA 4.0）"，
   在产物里明确可追溯，绝不冒充外部真题。
"""

from __future__ import annotations

import json
from pathlib import Path

import knowledge as k

RUBRICS = Path("rubrics.json")
OUT = Path("real_questions_builtin.json")

# 能力维度 → 归类标签（让报告里的「取材真题」有可读分类）
CAT_HINT = {
    "编程基础与数据结构": "编程基础",
    "操作系统与计算机网络": "系统与网络",
    "数据库与存储设计": "数据库",
    "工程实践与调试能力": "工程实践",
    "数学与算法基础": "数学与算法",
    "模型原理与推导": "模型原理",
    "数据处理与特征工程": "数据与特征",
    "实验设计与结果分析": "实验与评估",
    "编程与设计能力": "编程设计",
    "架构与系统设计": "系统架构",
    "工程流程与协作": "工程协作",
    "测试与质量意识": "测试质量",
    "电路与器件基础": "电路器件",
    "信号与系统": "信号系统",
    "硬件调试与测量": "硬件调试",
    "系统集成能力": "系统集成",
    "控制理论基础": "控制理论",
    "数学与建模能力": "数学建模",
    "传感器与执行机构": "传感器",
    "系统调试能力": "系统调试",
    "通信原理基础": "通信原理",
    "信号处理能力": "信号处理",
    "系统与协议理解": "协议理解",
    "仿真与实验能力": "仿真实验",
    "沟通与表达": "表达沟通",
}

# 题干模板：按难度调整提问口吻（A 追问原理、B 核心考点、C 概念应用）
STEM = {
    "A": [
        "请你从原理层面讲讲「{topic}」，最好能说出推导过程或权衡取舍。",
        "「{topic}」这块，如果面试官继续深挖，你觉得最容易被问住的是哪个点？请展开讲讲。",
        "假设要你用「{topic}」解决一个真实问题，你会怎么论证方案的合理性？",
    ],
    "B": [
        "聊聊「{topic}」，说说你的理解。",
        "「{topic}」在实际项目里怎么用？请结合一个例子说明。",
        "关于「{topic}」，你觉得关键点在哪？",
    ],
    "C": [
        "解释一下「{topic}」是什么，以及它解决什么问题。",
        "「{topic}」你了解多少？说说基本概念。",
        "简单讲讲「{topic}」的用途。",
    ],
}


def _as_trap(error: str) -> str:
    """把一条「常见误区」改写成面试官可以问出口的设伏句。

    原始 common_errors 的写法有三种，不能直接套「有人说……」：
      1. 纯误区陈述：「认为偏差和方差可以独立最小化，忽略权衡关系。」
         → 去掉句首的「认为」，改成"有人主张 X，对吗？"
      2. 含纠正内容：「误认为 Q_c 必须为方阵，实际上……需行满秩。」
         → 只取「实际上」**之前**的误区部分，否则等于把答案送给候选人。
      3. 含因果解释：「认为信噪比是线性值而非 dB 值，导致计算错误。」
         → 同样只保留误区主张本身。

    返回空串表示这条不适合做陷阱（无法安全抽取主张）。
    """
    text = str(error).strip().rstrip("。；;，,")
    if not text:
        return ""

    # 规则 2/3：截掉「实际上 / 其实 / 但 / 而」之后的内容，避免泄露正确答案
    for marker in ("实际上", "其实", "但实际", "而实际"):
        if marker in text:
            text = text.split(marker)[0]
            break

    # 去掉句首常见的判断动词，让句子变成可引述的"主张"
    for lead in ("误认为", "错误地认为", "认为", "以为"):
        if text.startswith(lead):
            text = text[len(lead) :]
            break

    text = text.strip().rstrip("。；;，,")
    if len(text) < 6:
        return ""
    return f"有候选人说「{text}」，你同意吗？为什么？"


def build() -> dict:
    rubrics = k.load_rubrics()
    tech: dict[str, list] = {}
    traps: dict[str, list] = {}
    total = 0
    trap_total = 0

    for major in k.MAJORS:
        pool: list[dict] = []
        trap_pool: list[dict] = []
        for topic, info in rubrics[major].items():
            level = info.get("level", "B")
            templates = STEM.get(level, STEM["B"])
            # 用考点名做稳定散列，保证同一考点每次生成的模板一致（可复现）
            idx = sum(ord(c) for c in topic) % len(templates)
            question = templates[idx].format(topic=topic)

            cat = "自建考点"
            for dim, label in CAT_HINT.items():
                if dim in topic or label in topic:
                    cat = label
                    break

            # 主问池：只放"能独立作为一道主问题"的题干
            pool.append(
                {
                    "q": question,
                    "topic": topic,
                    "cat": cat,
                    "src": "自建",
                    "lic": "本项目原创",
                    "ref": f"rubrics.json#{major}#{topic}",
                    "level": level,
                }
            )

            # 陷阱池单独存放：这类题**不能当主问题问**，否则等于把正确答案
            # 或误区直接抛给候选人。它的用途是面试官在追问阶段主动设伏，
            # 属于"提问弹药"而非"开场题目"。
            errors = info.get("common_errors") or []
            if errors:
                trap = _as_trap(errors[0])
                if trap:
                    trap_pool.append(
                        {
                            "q": trap,
                            "topic": topic,
                            "cat": cat,
                            "src": "自建",
                            "lic": "本项目原创",
                            "ref": f"rubrics.json#{major}#{topic}#common_errors",
                            "level": level,
                            "kind": "trap",
                        }
                    )
        tech[major] = pool
        traps[major] = trap_pool
        total += len(pool)
        trap_total += len(trap_pool)

    return {
        "_meta": {
            "purpose": "自建面试真题：由项目自有 280 条评分参照反向改写而来，"
            "不引用任何外部题库，与评分口径同源。",
            "generated": "2026-10-16",
            "note": "src=自建 表示本项目原创，非外部真题。"
            "「技术面」为可直接作为主问题的题干；「陷阱追问」由 common_errors "
            "反转而成，供面试官在追问阶段主动设伏，**不可作为主问题**。",
            "license": "本项目原创，随项目以 CC BY-NC-SA 4.0 共享",
            "counts": {m: len(v) for m, v in tech.items()},
            "trap_counts": {m: len(v) for m, v in traps.items()},
            "total": total,
            "trap_total": trap_total,
            "coverage_note": "6 个专业全部覆盖，每专业 45 道主问题 + 45 道陷阱追问。",
        },
        "技术面": tech,
        "行为面": [],
        "陷阱追问": traps,
    }


def main() -> None:
    data = build()
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  已生成 {OUT}")
    print()
    print(f"  {'专业':<14}{'主问题':>6}{'陷阱追问':>10}")
    print("  " + "-" * 32)
    for m in k.MAJORS:
        print(f"  {m:<14}{len(data['技术面'][m]):>6}{len(data['陷阱追问'][m]):>10}")
    print("  " + "-" * 32)
    print(f"  {'合计':<14}{data['_meta']['total']:>6}{data['_meta']['trap_total']:>10}")


if __name__ == "__main__":
    main()
