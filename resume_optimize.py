"""简历优化：诊断 + 逐条改写对照（纯函数部分）。

定位：这是一条**独立于面试**的能力——上传一份简历就能拿到诊断与改写建议，
不需要先面一场试。所以它的结果**不进入任何面试记录**（`history.json`）、
也不写快照；只活在本会话内存里，由用户主动导出时才落成文件。
与 `resume.py` / `resume_rag.py` 同一条隐私约束。

两条硬要求：

1. **不许编造**。改写只能用简历原文里已有的信息重新组织、量化、收紧措辞，
   不得新增候选人没写过的经历、项目、头衔、时间或数字。模型给出的每一条改写
   都必须能在原文里找到出处，`normalize_report` 会逐条核对，
   对不上的直接丢弃并计数——这样"优化简历"才不会被做成"帮你虚构简历"。
2. **不许越界评判**。诊断只提可执行的改法，不评判候选人"适不适合"这个方向。
"""

from __future__ import annotations

import difflib
import re
from typing import Any

MAX_PROBLEMS = 8
MAX_REWRITES = 5
MAX_MISSING = 6
MAX_KEYWORDS = 12
MAX_CHECKLIST = 6

SEVERITIES = ("high", "medium", "low")

# 引用被认为"对得上原文"的最低门槛：
#   - 至少这么长，且
#   - 占引用自身长度的这个比例
# 语义是"这句话至少有六成是从简历原文里抄出来的"——允许模型截半句、略去从句
# （那是正常改写手法），但拦得住"夹带一段原文里没有的叙述"。
_MIN_MATCH_CHARS = 6
_MIN_MATCH_RATIO = 0.6

# 数字（含小数）与百分号：用于拦"改写里塞进原文没有的数字"
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


# ---------------------------------------------------------------------------
# 取值规整
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    """压成单行、去首尾空白。"""
    return " ".join(str(value or "").split())


def _flat(value: Any) -> str:
    """去掉**全部**空白字符。

    核对「模型引用的原句是否真在简历里」时用：简历原文可能一行很长、
    模型引用时可能跨行截断，只有忽略空白才比得准。
    """
    return "".join(str(value or "").split())


def _lines(text: Any) -> list[str]:
    """非空行列表，行首尾已去空白。"""
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def _clean_list(value: Any, limit: int) -> list[str]:
    """去空、去重、限量的一串字符串。"""
    if not isinstance(value, (list, tuple)):
        value = [value] if value else []
    out: list[str] = []
    for item in value:
        text = _text(item)
        if text and text not in out:
            out.append(text)
    return out[:limit]


def _score(value: Any) -> int | None:
    """0~100 的整数；非数值（含 bool）一律 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, min(100, int(round(value))))
    return None


# ---------------------------------------------------------------------------
# 结构诊断：检出哪些小节、缺哪些、量化成果有多少
#
# 全部是启发式。它们的作用是给出**可读的提示**（"没看到技能小节"），
# 而不是当成判分依据；所以宁可宽进（检出就算有），也不要误报缺失。
# ---------------------------------------------------------------------------

_CONTACT_RE = re.compile(
    r"(?:1[3-9]\d{9}|\d{3,4}[-\s]?\d{7,8}|[\w.+-]+@[\w-]+\.[A-Za-z]{2,})"
)
_METRIC_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|％|万|千|百|亿|倍|人次|人|天|小时|分钟|秒|ms|毫秒|"
    r"QPS|TPS|MB|GB|KB|并发|个)",
    re.IGNORECASE,
)

# (小节名, 关键词, 补充正则)
SECTION_RULES: tuple[tuple[str, tuple[str, ...], re.Pattern[str] | None], ...] = (
    ("联系方式", ("电话", "手机", "邮箱", "微信", "联系方式"), _CONTACT_RE),
    (
        "教育背景",
        ("教育背景", "教育经历", "学历", "大学", "学院", "学校", "专业", "GPA", "绩点"),
        None,
    ),
    ("实习/工作经历", ("实习", "工作经历", "工作经验", "在职"), None),
    ("项目经历", ("项目经历", "项目经验", "课题", "毕业设计", "作品"), None),
    ("技能", ("技能", "技术栈", "熟练掌握", "语言能力", "工具"), None),
    ("获奖/证书", ("获奖", "奖项", "荣誉", "证书", "奖学金", "竞赛"), None),
)

# 「经历」这一类：实习/工作与项目二者有其一即可，不要求两份都写
_EXPERIENCE_SECTIONS = ("实习/工作经历", "项目经历")
# 现代校招简历里通常必备、缺失就值得提示的三项
_ESSENTIAL_SECTIONS = ("联系方式", "教育背景", "技能")


def present_sections(text: str) -> list[str]:
    """检出这份简历里出现了哪些常见小节（宽口径，检出就算有）。"""
    flat = _flat(text)
    found: list[str] = []
    for name, keys, pattern in SECTION_RULES:
        hit = any(_flat(key) in flat for key in keys)
        if not hit and pattern is not None:
            hit = bool(pattern.search(str(text or "")))
        if hit:
            found.append(name)
    return found


def missing_sections(text: str) -> list[str]:
    """列出通常必备、而这份简历里没检出的小节。"""
    present = present_sections(text)
    missing = [name for name in _ESSENTIAL_SECTIONS if name not in present]
    if not any(name in present for name in _EXPERIENCE_SECTIONS):
        missing.append("实习/项目经历")
    return missing


def quantified_lines(text: str) -> list[str]:
    """含「数量 + 单位」的条目。

    简历里真正能证明成果的就是这类句子（"QPS 峰值 3 万"、"耗时从 4 小时降到 40 分钟"）。
    刻意排除只有年份的行：`2021-2025` 不含单位，不会被这条正则命中，
    否则整份简历的每一年份都会被算成"量化成果"。
    """
    return [line for line in _lines(text) if _METRIC_RE.search(line)]


def quantification(text: str) -> dict[str, Any]:
    """量化密度：条目总数、含量化成果的条数、占比。"""
    lines = _lines(text)
    hits = quantified_lines(text)
    total = len(lines)
    return {
        "lines": total,
        "quantified": len(hits),
        "ratio": round(len(hits) / total, 3) if total else 0.0,
    }


# ---------------------------------------------------------------------------
# 收敛模型输出（本功能最要紧的一步：改写必须对得上原文）
# ---------------------------------------------------------------------------


def is_grounded(quote: str, resume_text: str) -> bool:
    """模型的引用能否在简历原文里找到出处。

    两种匹配都算对得上：

    1. 引用整体（忽略空白）是简历原文的子串；
    2. 引用与原文的**最长公共片段**达到 `_MIN_MATCH_CHARS` 字，且占引用自身
       长度的 `_MIN_MATCH_RATIO` 以上。

    第 2 条是必需的：模型经常引用"原句的前半截 + 一句概括"，或者跨行取一段。
    早先的写法要求"某一整行原文必须完整出现在引用里"，结果把这种**正常**的截取
    也判成了编造（实测真简历上就会误杀）。改用最长公共片段后，既能放过截取，
    也拦得住"夹带一段原文里没有的叙述"——那种情况下公共片段会因为掺了别的内容
    而摊薄到比例以下。

    过短的引用（<4 字，如"提升""项目"）一律不算：太容易在别处撞上，没有核对价值。
    """
    q = _flat(quote)
    if len(q) < 4:
        return False
    flat = _flat(resume_text)
    if q in flat:
        return True
    match = difflib.SequenceMatcher(None, q, flat).find_longest_match(0, len(q), 0, len(flat))
    return match.size >= _MIN_MATCH_CHARS and match.size >= len(q) * _MIN_MATCH_RATIO


def introduced_numbers(improved: str, resume_text: str) -> list[str]:
    """改写句里出现、而简历原文里**从未出现**的数字。

    这是那条"不许编造"红线在数字上的落点：模型最危险的举动就是给一句没有数字的
    经历补一个看起来很像的量化结果（"性能提升 40%"），而这在真实面试里会当场翻车。
    改写的**原句**本来就出自简历，所以只需核对改写句。

    只做"数字是否在原文出现过"的核对，不判断语义：原文里出现过的同一个数字，
    换个位置用是允许的（例如把"QPS 峰值 3 万"里的 3 万挪到结论句里）。
    """
    if not _flat(improved):
        return []
    known = set(_NUMBER_RE.findall(str(resume_text or "")))
    out: list[str] = []
    for number in _NUMBER_RE.findall(str(improved or "")):
        if number not in known and number not in out:
            out.append(number)
    return out


def normalize_report(data: dict[str, Any] | None, resume_text: str) -> dict[str, Any]:
    """收敛模型输出，并本地补上结构统计。

    **最关键的一条**：`rewrites` 里的 `original` 必须能对上简历原文，
    对不上就整条丢弃并计入 `dropped_rewrites`。理由很直接——模型完全可能
    顺手把"负责模块开发"写成"主导 3 人团队完成模块开发，性能提升 40%"，
    那已经不是优化简历了，是替他编简历；而编造出来的经历在真实面试里会当场翻车。

    `problems` 里的 `evidence` 只做**标注**不做丢弃：诊断条目本身（"量化不足"）
    即使引用对不上也依然成立，所以保留问题、清掉可疑的引用并计入
    `ungrounded_evidence`，由界面如实提示。
    """
    payload = data if isinstance(data, dict) else {}

    problems: list[dict[str, Any]] = []
    ungrounded_evidence = 0
    raw_problems = payload.get("problems")
    for raw in raw_problems if isinstance(raw_problems, list) else []:
        if not isinstance(raw, dict):
            continue
        problem = _text(raw.get("problem"))
        if not problem:
            continue
        severity = _text(raw.get("severity")).lower()
        if severity not in SEVERITIES:
            severity = "medium"
        evidence = _text(raw.get("evidence"))
        grounded = True
        if evidence and not is_grounded(evidence, resume_text):
            evidence = ""
            grounded = False
            ungrounded_evidence += 1
        problems.append(
            {
                "severity": severity,
                "category": _text(raw.get("category")),
                "problem": problem,
                "evidence": evidence,
                "evidence_grounded": grounded,
                "advice": _text(raw.get("advice")),
            }
        )
        if len(problems) >= MAX_PROBLEMS:
            break

    rewrites: list[dict[str, Any]] = []
    dropped_rewrites = 0
    invented_numbers = 0
    raw_rewrites = payload.get("rewrites")
    for raw in raw_rewrites if isinstance(raw_rewrites, list) else []:
        if not isinstance(raw, dict):
            dropped_rewrites += 1
            continue
        original = _text(raw.get("original"))
        improved = _text(raw.get("improved"))
        if not original or not improved or not is_grounded(original, resume_text):
            dropped_rewrites += 1
            continue
        # 第二道闸：改写句不许出现原文里从没有过的数字。
        # 引用核对管得住"原句有没有出处"，管不住"改写有没有塞数字"——两句都必要。
        if introduced_numbers(improved, resume_text):
            dropped_rewrites += 1
            invented_numbers += 1
            continue
        rewrites.append(
            {"original": original, "improved": improved, "why": _text(raw.get("why"))}
        )
        if len(rewrites) >= MAX_REWRITES:
            break

    return {
        "verdict": _text(payload.get("verdict")),
        "score": _score(payload.get("score")),
        "problems": problems,
        "rewrites": rewrites,
        "missing": _clean_list(payload.get("missing"), MAX_MISSING),
        "keywords": _clean_list(payload.get("keywords"), MAX_KEYWORDS),
        "checklist": _clean_list(payload.get("checklist"), MAX_CHECKLIST),
        # 下面这几项由本地算，不采信模型
        "dropped_rewrites": dropped_rewrites,
        "invented_numbers": invented_numbers,
        "ungrounded_evidence": ungrounded_evidence,
        "sections": {
            "present": present_sections(resume_text),
            "missing": missing_sections(resume_text),
        },
        "quantification": quantification(resume_text),
    }
