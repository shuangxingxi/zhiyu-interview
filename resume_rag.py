"""简历 RAG：把简历切成条目建索引，按当前话题检索相关原文。

与「全文注入」的关系（对应 DESIGN.md 第七节）：
    单份简历的全文注入本来就不算差，所以这里不是把全文注入换掉，
    而是在它之上补两件单份简历下真正有增量的事：
      1. 聚焦检索 —— 面试官每轮只带「简历条目索引 + 与当前话题相关的条目原文」，
         长简历不会在十几轮对话里反复占满上下文、把指令稀释掉；
      2. 一致性校验 —— 评估官拿到与本次讨论相关的简历原文，核对候选人自述
         与简历是否对得上（夸大 / 前后矛盾），写进报告。
    切片或检索不可用时，调用方一律回落到全文注入，行为与 v3 完全一致。

检索实现：本地字符 n-gram BM25（纯 Python，零新增依赖、离线可用、结果可解释）。
    中文按字符二元组、英文/数字按词切分，既不需要分词器，也不需要向量库。
    数据量是「一份简历十几条」，这就是最合适的方案——上 embedding 是纯粹的复杂度开销。

隐私：与 resume.py 一致，全程只在内存里处理，不落盘、不进历史记录。
"""

from __future__ import annotations

import math
import re
from typing import Any

# 面试官每轮聚焦注入的条数；评估官核查用的条数（核查要看得更全一点）
TOP_K_INTERVIEW = 2
TOP_K_EVIDENCE = 3

# 超长小节按这个字符数再切一刀，避免一条切片顶掉整个上下文预算
_CHUNK_MAX_CHARS = 260

# 识别小节标题用：命中其一，且整行很短、不像句子，就当小节标题
_SECTION_HINTS = (
    "教育", "学历", "学校", "专业", "实习", "工作", "经历", "项目", "技能",
    "证书", "奖项", "获奖", "荣誉", "校园", "社团", "实践", "科研", "论文",
    "竞赛", "自我评价", "个人简介", "求职意向", "联系方式", "基本信息",
)

_WORD_RE = re.compile(r"[A-Za-z0-9+#.]+")
_BULLET_RE = re.compile(r"^[\s]*(?:[•·▪◦●○*\-–—]|\d{1,2}\s*[、.．)）])")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


# ---------------------------------------------------------------------------
# 切片
# ---------------------------------------------------------------------------


def _is_heading(line: str) -> bool:
    """判断一行是不是小节标题。

    条件刻意保守：短、含小节关键词、不以句读结尾。宁可少切几刀，
    也不要误把一条经历正文当成标题，切碎了反而更难检索。
    """
    text = line.strip()
    if not text or len(text) > 14:
        return False
    if text.endswith(("。", "，", "；", ";", ",", "!", "！", "?", "？")):
        return False
    return any(hint in text for hint in _SECTION_HINTS)


def _clean(line: str) -> str:
    """去掉行首的项目符号，让正文更干净（索引里再统一加符号）。"""
    return _BULLET_RE.sub("", line).strip()


def _flush(chunks: list[dict[str, str]], section: str, buf: list[str]) -> None:
    text = "\n".join(buf).strip()
    if len(text) < 4:
        return
    chunks.append({"section": section, "text": text})


def chunk_resume(text: str) -> list[dict[str, str]]:
    """把简历切成「条目」切片，返回 [{"id", "section", "text"}]。

    先按小节标题分段，段内超长再按长度切一刀；识别不出标题时整份当一段，
    行为与全文注入等价。任何异常都不抛出——返回空列表让调用方回落全文。
    """
    try:
        lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
        chunks: list[dict[str, str]] = []
        section = "简历"
        buf: list[str] = []
        for line in lines:
            if _is_heading(line):
                _flush(chunks, section, buf)
                buf = []
                section = _clean(line)
                continue
            buf.append(_clean(line))
            if len("\n".join(buf)) >= _CHUNK_MAX_CHARS:
                _flush(chunks, section, buf)
                buf = []
        _flush(chunks, section, buf)

        for index, chunk in enumerate(chunks, 1):
            chunk["id"] = f"R{index}"
        return chunks
    except Exception:  # noqa: BLE001
        return []


def _title(chunk: dict[str, str], limit: int = 30) -> str:
    """条目的单行摘要，给索引用：取正文首行，超长截断。"""
    first = (chunk.get("text") or "").splitlines()[0] if chunk.get("text") else ""
    first = first.strip()
    return first[:limit] + ("…" if len(first) > limit else "")


def index_block(chunks: list[dict[str, str]]) -> str:
    """拼「简历条目索引」：每条一行，让面试官知道有哪些条目可选、哪些还没问到。"""
    if not chunks:
        return ""
    lines = [f"- {c['id']} · {c['section']}：{_title(c)}" for c in chunks]
    return (
        "【简历条目索引】（全场可见：用来挑选还没被问到的条目）\n" + "\n".join(lines)
    )


def detail_block(chunks: list[dict[str, str]], title: str = "与当前话题相关的简历原文") -> str:
    """拼若干条目的原文；标题可定制（面试官聚焦 / 评估官取证）。"""
    if not chunks:
        return ""
    parts = [f"[{c['id']} · {c['section']}]\n{c['text']}" for c in chunks]
    return f"【{title}】\n" + "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 检索：本地字符 n-gram BM25
# ---------------------------------------------------------------------------


def _terms(text: str) -> list[str]:
    """切成检索词：英文/数字按词，中文按字符二元组。无需分词器。"""
    raw = str(text or "").lower()
    terms = [w for w in _WORD_RE.findall(raw) if len(w) >= 2]
    cjk = "".join(_CJK_RE.findall(raw))
    terms.extend(cjk[i : i + 2] for i in range(len(cjk) - 1))
    return terms


def retrieve(
    chunks: list[dict[str, str]], query: str, k: int = TOP_K_INTERVIEW
) -> list[dict[str, str]]:
    """按 BM25 取与 query 最相关的 k 个切片。

    query 为空、切片为空或全部零分时返回空列表——调用方据此决定回落策略。
    """
    if not chunks or not str(query or "").strip():
        return []

    query_terms = set(_terms(query))
    if not query_terms:
        return []

    docs = [_terms(f"{c.get('section', '')} {c.get('text', '')}") for c in chunks]
    lengths = [len(d) or 1 for d in docs]
    avg_len = sum(lengths) / len(lengths)

    df: dict[str, int] = {}
    for doc in docs:
        for term in set(doc):
            df[term] = df.get(term, 0) + 1

    total = len(docs)
    k1, b = 1.5, 0.75
    scores: list[tuple[float, int]] = []
    for idx, doc in enumerate(docs):
        tf: dict[str, int] = {}
        for term in doc:
            tf[term] = tf.get(term, 0) + 1
        score = 0.0
        for term in query_terms:
            freq = tf.get(term, 0)
            if not freq:
                continue
            idf = math.log(1 + (total - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
            score += idf * freq * (k1 + 1) / (freq + k1 * (1 - b + b * lengths[idx] / avg_len))
        if score > 0:
            scores.append((score, idx))

    scores.sort(key=lambda item: item[0], reverse=True)
    return [chunks[idx] for _, idx in scores[: max(1, k)]]


def build(text: str) -> list[dict[str, str]]:
    """编译入口：切片一次，攒进 profile 供后续每轮检索。"""
    return chunk_resume(text)


def top(profile: dict[str, Any], query: str, k: int = TOP_K_INTERVIEW) -> list[dict[str, str]]:
    """按 profile 里已建好的切片检索；没有切片或没检索到都返回空列表。"""
    return retrieve(profile.get("resume_chunks") or [], query, k)
