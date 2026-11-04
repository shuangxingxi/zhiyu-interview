"""面试相关 Agent 的实现：面试官 / 评估官 / 教练 / 建议官 / 简历优化官。

设计说明（对应 DESIGN.md 第四、五节）：
    不引入 LangChain / AutoGen。本场景的"行动空间"只有 3 种
    （追问 / 换题 / 结束），自研一个轻量决策循环即可，可控、可解释、零框架依赖。

五个角色的分工：
    面试官 interviewer_turn   —— 多轮对话，全量 messages 回传。
        每轮返回结构化决策，代码再做一次兜底约束（验收门见 gate_*，兜底见 _apply_fallbacks）。
    评估官 evaluate_interview —— 面试结束后两次独立调用：先按能力模型打分，
    再由独立上下文的归因调用剥离面试官状态干扰。
    教练   coach_review       —— 基于记录 + 评估，给出可执行的改进方案。
    建议官 advisor_review     —— 在教练之后调用，**按评分维度**逐条给出"下次面试前
        该补什么"，与教练的"这句话该怎么讲"互补（分工见 advice.py 表）。
    简历优化官 resume_review  —— 独立于面试：上传简历即可调用，产出诊断 + 改写对照。
        与面试链路无关，所以它**不读逐字稿**、也不需要先面一场试。

本模块只负责"怎么问模型"。「模型输出怎么收敛成确定结构」以及所有本地判定
（维度是否真实存在、改写有没有编造、量化密度）都在 advice / resume_optimize
两个纯函数模块里，可离线单测。
"""

from __future__ import annotations

import contextvars
import json
import sys
import threading
from typing import Any

import httpx
from openai import APITimeoutError, OpenAI

from knowledge import (
    MAX_PROBES_STRICT,
    MAX_REFLECTS_IN_A_ROW,
    UNTRUSTED_GUARD,
    deep_dive_block,
    profile_summary,
    real_question_block,
    reference_for,
    resume_block,
    resume_evidence_block,
    rubric_block,
    rubric_hint_block,
    topic_mix_block,
    unanchored_block,
    wrap_untrusted,
)
from mood import describe as describe_mood, effective_max_probes
import advice
import resume_optimize
import resume_rag

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

# 单次模型调用的超时。刻意用显式 httpx.Timeout 而不是一个裸数字：
# 裸数字下 SDK 仍会按自己的默认值重试（max_retries=2），一次"服务器没响应"
# 会被放大成数倍等待（实测挂过 540 多秒）；这里把 read 超时压到 15 秒。
# 面试是交互场景，宁可快速失败让用户重试，也不要页面长时间毫无反应。
FIRST_RESPONSE_TIMEOUT = 15.0

# 报告类角色（评估官 / 复盘教练 / 提升建议 / 简历优化）单独放宽。
# 它们的共同点是"面试结束或点完按钮后一次性跑完"，用户本来就在等结果，
# 而输出长度是面试官那轮决策的数倍——实测 coach 23.0s、advisor 22.8s、
# 简历优化 27.5s，用交互路径的 15 秒去卡，等于这三个功能在当前网络下必然失败
# （连试 6 次 6 次超时）。所以超时必须按角色分，不能一张网卡到底。
REPORT_RESPONSE_TIMEOUT = 90.0


def _timeout(read: float) -> httpx.Timeout:
    """连接/写入超时统一，只有 read（等模型出字）按角色区分。"""
    return httpx.Timeout(connect=10.0, read=read, write=10.0, pool=10.0)


MODEL_TIMEOUT = _timeout(FIRST_RESPONSE_TIMEOUT)
REPORT_TIMEOUT = _timeout(REPORT_RESPONSE_TIMEOUT)

# 面试官唯一可用的"工具"：主动查阅简历原文。
# 刻意用结构化 JSON 字段模拟 tool call，而不切换到 DeepSeek 的 function calling：
# 后者与 response_format=json_object 混用容易出问题，而 chat_json 已有重试与容错。
# 用 JSON 字段表达动作，能力照旧、风险为零（对应 DESIGN.md 第五节）。
TOOL_NONE = "none"
TOOL_RESUME = "resume_lookup"

# 一轮面试官发言里，最多允许几次"主动查阅简历"。
# 1 次是 v6 的原始行为；放到 2 次是为了让模型能"查了 A 发现还要核对 B"，
# 但必须有上限——否则模型可能靠反复查阅把单轮成本无限拉高。
MAX_TOOL_ROUNDS = 2

# 长会话压缩：发给模型的原文只保留最近这么多条消息（约 3 轮问答），
# 更早的内容用本地启发式压成一段"前情提要"拼进系统提示词里。
# 轮数一多，全量回传会让成本与延迟线性上涨；而且 DeepSeek 在长历史 +
# response_format=json_object 下更容易返回空内容（见 chat_json 的注释）。
HISTORY_WINDOW = 6
_RECAP_REPLY_CHARS = 40  # 面试官那轮说的话，压缩后保留多少字
_RECAP_ANSWER_CHARS = 80  # 候选人回答，压缩后保留多少字


# ---------------------------------------------------------------------------
# 基础调用
# ---------------------------------------------------------------------------


def _client(api_key: str, timeout: httpx.Timeout | None = None) -> OpenAI:
    # max_retries=0：超时意味着"服务器没响应"，重试只会让用户的干等再加一轮，
    # 不如直接把可读的报错抛给上层。
    return OpenAI(
        api_key=api_key,
        base_url=DEEPSEEK_BASE_URL,
        timeout=timeout or MODEL_TIMEOUT,
        max_retries=0,
    )


# ---------------------------------------------------------------------------
# Token 计量
#
# 这里必须区分两个层级：
#   · "本场"是一次会话私有的边界（Streamlit 把它放在 session_state 里）；
#   · 而模块级变量是**进程级**的——Streamlit 用一个进程跑所有浏览器会话，
#     模块只 import 一次，所有标签页共享同一份内存。
# 早期版本把累加器做成模块级全局：单机单人没问题，但两个人同时开标签页时，
# 后开场的那位一 reset_usage() 就把前一位的账清零，前一位结束时
# usage_snapshot() 拿到的是两个人的混合值，还会写进 history.json 变成错账。
#
# 所以计量状态改成**按会话绑定**：调用方（app）在每次 rerun 开头
# bind_usage_meter() 绑定自己那本账，本模块只往"当前绑定的账"累加。
#
# 用 ContextVar 而不是把 meter 一路传参：chat_json 的调用点有二十多处，
# 逐个加参数会把计量污染到每个函数签名里；ContextVar 是随上下文走的隐式参数，
# agents 也因此不必依赖 streamlit。
# ---------------------------------------------------------------------------

_usage_lock = threading.Lock()

# 没有绑定会话时（单测、脚本直接调用）记在这本默认账上，行为与旧版一致。
_DEFAULT_USAGE: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
_usage_ctx: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar(
    "zy_usage_meter", default=None
)


def new_usage_meter() -> dict[str, int]:
    """建一本空账，由调用方存进会话状态。"""
    return {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}


def bind_usage_meter(meter: dict[str, int] | None) -> None:
    """把后续调用记到这本账上；传 None 退回默认账（单测 / 非会话场景）。"""
    _usage_ctx.set(meter)


def _meter() -> dict[str, int]:
    meter = _usage_ctx.get()
    return meter if meter is not None else _DEFAULT_USAGE


def reset_usage(meter: dict[str, int] | None = None) -> None:
    """把计量清零。不传参数时清**当前绑定的那本账**，而不是进程全局。"""
    target = meter if meter is not None else _meter()
    with _usage_lock:
        target.update({"prompt_tokens": 0, "completion_tokens": 0, "calls": 0})


def usage_snapshot(meter: dict[str, int] | None = None) -> dict[str, int]:
    """读取累计值（拷贝，调用方改不动内部状态）。"""
    target = meter if meter is not None else _meter()
    with _usage_lock:
        return dict(target)


def _record_usage(response: Any) -> None:
    """把一次响应的 usage 累加进去；缺字段就跳过，绝不因为计量而报错。"""
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    target = _meter()
    with _usage_lock:
        target["calls"] += 1
        if isinstance(prompt, int):
            target["prompt_tokens"] += prompt
        if isinstance(completion, int):
            target["completion_tokens"] += completion


def _loads_lenient(text: str | None) -> dict[str, Any]:
    """模型偶尔会把 JSON 包在 ```json 代码块里，这里做一次容错解析。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("模型返回了空内容（content 为空），没东西可解析。")
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                pass
        # 解析不了就把原文抛出来，否则只能看到一句没有信息量的 json 报错
        raise ValueError(
            f"模型返回的内容不是合法 JSON。原始内容（前 300 字）：{raw[:300]!r}"
        ) from None


def chat_json(
    messages: list[dict[str, str]],
    api_key: str,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.3,
    attempts: int = 3,
    timeout: httpx.Timeout | None = None,
    think: bool = True,
) -> dict[str, Any]:
    """调用 DeepSeek，要求返回 JSON 并解析成字典。

    `think=False` 关掉思考模式，只给**交互轮**用（面试官那一轮）。两个理由：
    ① deepseek-flash 默认开思考，单轮要 2~4 秒才出字，而交互轮只有 15 秒上限；
    ② 官方明确「thinking 模式下 temperature 不生效」——本函数重试时靠"降温度
       让它更守规矩"的策略，在思考模式下是空转的，关掉才真的起作用。
    报告类角色（评估官 / 教练 / 建议官 / 简历优化）保留默认 True：他们本来就在
    等结果，多几秒换更稳的判定值得，何况超时给到 90 秒。

    DeepSeek 偶尔会返回空白内容（finish_reason='stop'，content 全是空格），
    对话轮数变多后更容易出现。这是服务端偶发行为，重试即可恢复，
    所以这里做有限次重试；重试时降低温度让它更"守规矩"。

    `timeout` 缺省走交互路径的 MODEL_TIMEOUT（15 秒）；报告类角色传
    REPORT_TIMEOUT（90 秒）——它们的输出长度是面试官那轮的数倍。
    """
    last_error: Exception | None = None
    limit = timeout or MODEL_TIMEOUT

    for attempt in range(1, attempts + 1):
        try:
            response = _client(api_key, limit).chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.1 if attempt > 1 else temperature,
                # 关思考时才显式传：开思考是服务端默认，多发一个字段只会多一份被
                # 旧接口拒绝的风险（extra_body 是 OpenAI SDK 的透传位）。
                **({} if think else {"extra_body": {"thinking": {"type": "disabled"}}}),
            )
        except APITimeoutError as exc:
            # 超时不重试：服务器没响应时再等一轮只是把干等翻倍。
            raise RuntimeError(
                f"DeepSeek 在 {limit.read:.0f} 秒内没有响应（服务器过载或链路不通）。"
                "请稍后重试；若持续如此，可先确认 API 服务是否正常。"
            ) from exc
        choice = response.choices[0]
        content = choice.message.content
        _record_usage(response)

        try:
            return _loads_lenient(content)
        except ValueError as exc:
            last_error = exc
            print(
                f"[deepseek 返回异常] 第 {attempt}/{attempts} 次 "
                f"finish_reason={choice.finish_reason!r} content={content!r}",
                file=sys.stderr,
                flush=True,
            )

    raise ValueError(
        f"模型连续 {attempts} 次没有返回可用 JSON，最后一次的返回是：{last_error}"
    ) from None


def test_connection(api_key: str, model: str = DEFAULT_MODEL) -> str:
    """最简连通性测试，用于确认 API Key 是否可用。

    `max_tokens` 不能再压到 20：deepseek-flash 默认开启思考模式，
    reasoning tokens 也计入 completion_tokens。给 20 个 token 时思维链会把
    预算全部吃掉，返回 `finish_reason='length'` + `content=''`——
    实测 12 次里 11 次为空。界面于是显示「连接成功，模型回复：」，
    后面什么都没有，用户只会当成链路坏了。

    这里反而要留足预算：思考模式需要先想再答，卡太紧只会把答案挤掉。
    拿到空内容时显式报错，而不是把空串当成"成功"交给界面。
    """
    response = _client(api_key).chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "只回复两个字：你好"}],
        max_tokens=200,
    )
    choice = response.choices[0]
    reply = (choice.message.content or "").strip()
    if not reply:
        raise RuntimeError(
            f"模型返回了空内容（finish_reason={choice.finish_reason!r}）。"
            "多半是思考模式把 token 配额用尽了，请重试一次。"
        )
    return reply


def _neutralize_utterance(text: Any) -> str:
    """把候选人原话压成单行并中和结构标记。

    逐字稿是「面试官：/候选人：」按行组织的。候选人只要在回答里打一个换行再写
    「面试官：」，就能在评估官眼里伪造一轮面试官发言；写「【考点：X】」则能把
    轮次归属带偏（评估官是按考点归属逐轮对照要点的）。这里统一压平并替换标记。
    真实的换行在口头作答里没有语义，压平不损失信息。
    """
    flat = " ".join(str(text or "").split())
    return flat.replace("【考点：", "【考点(原文引用)：")


def format_transcript(
    history: list[dict[str, Any]], *, tag_topics: bool = False
) -> str:
    """把对话历史转成「面试官：… / 候选人：…」的纯文本，供评估官和教练阅读。

    `tag_topics=True` 时每轮面试官发言前会带上**这一轮的考点**（`topic` 字段，
    由 app 在落历史时写进 assistant 消息）。

    为什么评估官必须带：它此前只拿到一串不带考点的问答，加上一份"本场问到
    哪些考点"的参照清单，无法知道哪句话属于哪一道题。当本场既有锚定考点、
    又有自拟题时，它会把自拟题的表现也硬判到锚定考点的要点上——判出来的
    结论在报告上看不出任何异常。把轮次与考点的对应关系显式交给它，是
    「现拟参照」能成立的前提。

    为什么教练 / 建议官**不**带（默认 False）：`【考点：…】` 是内部标记，
    只有评估官的提示词里解释过它。教练和建议官拿到的是给候选人看的建议，
    看到一堆无解释的标签，轻则当噪音，重则把标签原文抄进建议里
    （"你在【考点：X】那道题上…"）。这个参数就是为了把两者分开。
    """
    lines: list[str] = []
    for msg in history:
        if msg.get("role") == "assistant":
            head = ""
            if tag_topics:
                # 考点优先读消息上的顶层 topic（渲染/导出时可以补写），
                # 否则回落到决策体里的 topic —— app 落历史时把整份决策存在 decision，
                # 绝大多数记录只有这一个来源。
                topic = str(msg.get("topic") or "").strip()
                if not topic:
                    decision = msg.get("decision")
                    if isinstance(decision, dict):
                        topic = str(decision.get("topic") or "").strip()
                head = f"【考点：{topic}】" if topic else "【考点：（未标注）】"
            lines.append(f"{head}面试官：{msg.get('content', '')}")
        else:
            lines.append(f"候选人：{_neutralize_utterance(msg.get('content', ''))}")
    return "\n".join(lines)


def format_mood_trace(trace: list[dict[str, Any]] | None, mode_label: str = "") -> str:
    """把情绪状态轨迹整理成评估官能读的逐轮明细。

    归因要落到"第几轮"上才有说服力，所以这里给的是逐轮明细而不是一句概括。
    """
    head = f"本场情绪模式：{mode_label}\n" if mode_label else ""
    if not trace:
        return head + "（本场未记录状态轨迹）"
    lines = [
        f"第 {item.get('round', '?')} 轮：{item.get('mood', '')}"
        f"（耐心 {item.get('patience', '?')}／精力 {item.get('energy', '?')}／"
        f"时间压力 {item.get('pressure', '?')}）"
        for item in trace
    ]
    return head + "\n".join(lines)


def to_api_messages(history: list[dict[str, Any]]) -> list[dict[str, str]]:
    """把会话历史转换成发给模型的 messages。

    DeepSeek 在 response_format=json_object 模式下有个坑：如果历史里累计出现
    两轮以上「纯口语文本」的 assistant 消息，接口会返回一个 content 全是空格的
    响应（finish_reason=stop），连续重试、调整温度、加大 max_tokens 都无效。
    把 assistant 轮次包成 JSON 就能恢复正常，而且这和要求模型输出的格式本来就一致。
    """
    api_messages: list[dict[str, str]] = []
    for item in history:
        role = item.get("role", "")
        content = item.get("content", "")
        if role == "assistant":
            decision = item.get("decision")
            payload = decision if decision else {"reply": content}
            content = json.dumps(payload, ensure_ascii=False)
        api_messages.append({"role": role, "content": content})
    return api_messages


def _clip(text: Any, limit: int) -> str:
    """把一段文本压成单行、截断到 limit 字，超出补省略号。"""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def summarize_history(
    history: list[dict[str, Any]], keep: int = HISTORY_WINDOW
) -> str:
    """把窗口之外的较早对话压成一段「前情提要」。

    刻意用本地启发式压缩、不额外调用一次模型：省一轮延迟与成本，
    结果也是确定的（同样的历史永远压出同样的文本），便于排查问题。
    面试官真正需要记住的是"问过什么方向、候选人答了什么要点"，
    这些从每轮的 decision（考点/动作）和回答原文里各取一小段就够了。
    """
    older = list(history[:-keep]) if len(history) > keep else []
    if not older:
        return ""

    lines: list[str] = []
    for item in older:
        role = item.get("role")
        if role == "assistant":
            decision = item.get("decision") or {}
            topic = str(decision.get("topic") or "").strip()
            head = topic or _clip(decision.get("reply") or item.get("content"), 20)
            lines.append(
                f"- 面试官问过「{head}」（{_clip(item.get('content'), _RECAP_REPLY_CHARS)}）"
            )
        elif role == "user":
            lines.append(f"  候选人答：{_clip(item.get('content'), _RECAP_ANSWER_CHARS)}")

    if not lines:
        return ""
    return (
        "【前情提要】（较早的对话已压缩成要点，帮助你保持连贯；不要向候选人复述这段）\n"
        + "\n".join(lines)
    )


# ---------------------------------------------------------------------------
# 智能体 1 · 面试官（多轮对话 + 决策状态机）
# ---------------------------------------------------------------------------

INTERVIEWER_SYSTEM = UNTRUSTED_GUARD + """你叫「知遇」，是一位真实的面试官，正在主持一场{interview_type}，面试对象是「{scene}」场景下的候选人。

【你的身份】
你在{level_label}做{major}方向的面试。
提问深度：{depth}
追问尺度：{probe_style}
评判标准：{strictness}
本场考察重心：{focus}

【你此刻的状态】
{mood_block}

【本场能力维度】（提问要覆盖这些维度，但不要在对话里念出维度名称）
{dimensions}

【高频考点参考】（只是方向参考，严禁照抄照念；括号里是内部档位）
{reference}
{mix_block}

【候选人自述背景】
{candidate_bg}

{context_blocks}【当前进度】
主问题计划 {total_rounds} 道，已经问出 {cur_q} 道；当前这道题已追问 {probes_used} 次（上限 {max_probes} 次）。
{state_hint}

【最重要的规则：不许念题库】
你要根据候选人的实际回答决定下一步，而不是按固定顺序念预设问题。
- 回答空洞（只讲概念、没有具体事例、没有个人贡献、没有数据）→ action = "probe"，针对他刚说的内容追问。
- 追问必须引用候选人上一句话里的具体信息（原话、技术名词、数字），禁止「能再详细说说吗」「还有吗」这类无效追问。
- 回答扎实、有细节、有反思 → action = "ask"，换下一道主问题。
- 只有两种情况可以用 action = "end"：主问题已经问满 {total_rounds} 道，或候选人明确表示要结束面试。
- action = "probe" 时 question_no 保持不变；action = "ask" 时 question_no 等于当前主问题编号 + 1。
- 如果上面提供了简历，主问题必须优先围绕简历里的实习经历、毕业设计、获奖情况、项目经历展开，并点名具体条目；不要让简历只是摆设。
- 如果没有简历但有候选人自述背景，主问题就要围绕他写下的岗位目标、项目经历、技能栈来问，同样要点名具体条目。
- 无论有没有简历，都要把候选人自己的经历问深（背景与目标、他的具体贡献、技术选型依据、遇到的难点、可量化的结果），不要只考背书。

【考点去重：不许重复出题】
上面「已经考过的考点」里列出的题目，一律不要再问，哪怕换种问法也不行。
- 每道主问题都要落在一个明确的考点上，并在 topic 字段里回报这个考点。

{real_rule}{tool_rule}【跨轮记忆：把这场对话串起来】
如果候选人在前面某一轮提过与当前话题相关的东西（技术名词、数字、经历、当时没说清的点），
必须显式回引，例如「你前面提到过用 Redis 做缓存，那……」。
已经听到的信息不要浪费，已经问过的不要重复问。真人面试官是记得住前面说过什么的。

{reflect_block}
【表达要求】
1. 全部用中文口语，像一个真人在说话，一次只推进一件事。
2. 不要用 Markdown、不要用列表、不要写小作文，回复控制在 120 字以内；reflect 回合更要短，一句话就够。
3. 第一次发言要简短自我介绍：报出你的名字「知遇」，说明流程，然后立刻问第一道题；之后不要再重复自我介绍。
4. 不要自己给出答案，不要评价自己的问题，不要替候选人回答。
5. 若 action = "end"，用两三句话自然收尾，并告知结果会以报告形式给出。

只输出 JSON，不要输出任何其他文字，格式如下：
{{
  "reply": "你要对候选人说的话（纯口播文本）",
  "action": "ask 或 probe 或 reflect 或 end",
  "question_no": 当前主问题编号（整数）,
  "topic": "这一轮所处的考点，必须在下面两种里选一种：① 上面「考点参考」里有贴切的，就填它的原文，逐字一致，不许缩写、改写、合并；② 确实没有贴切的，填一个不超过 12 字的短语概括这道题考什么。绝不要为了让这个字段非空，把题目硬挂到一个「沾边」的考点上——挂错会让评分参照对到别的题上去，比留空危害大得多。probe / reflect 时填当前正在问的那个考点；end 时填空字符串",
  "focus": "这一题考察的能力点（短语）",
  "gap": "action 为 probe 时填：还缺什么（缺量化结果 / 缺「我」本人做了什么 / 缺技术原理 / 缺反思），其它情况填空字符串",
  "tool": "none 或 resume_lookup：需要查看简历原文才能问准时才填 resume_lookup，否则一律填 none",
  "tool_query": "tool 为 resume_lookup 时填要查的关键词（项目名 / 技术名词 / 岗位 / 奖项）；否则填空字符串",
  "note": "内部备注：你对上一轮回答的判断，以及为什么这样决策"
}}"""

# reflect 的开关由 build_profile 编译进配置：严格模式追求节奏一致，真人模式允许停顿与确认
_REFLECT_BLOCK = """【action = "reflect"：这三种情况用一次"不提问回合"】
真人面试里并不是每一轮都在抛问题。整场出现 1~2 次即可，不必多。
- 候选人给的是模糊的量化数据（原话里带「大概」「几百」「差不多」「有时候」）→ 一句话澄清，例如「你说的 QPS 是峰值还是均值？」
- 候选人的说法前后对不上，或和他自述的背景、简历对不上 → 复述确认，例如「所以你那次优化主要是缓存命中率提升，不是算法层面的，对吧？」
- 一个话题刚聊透、马上要换题 → 极简反馈，例如「嗯，这块我清楚了。」
reflect 不推进题目、也不算一次追问；确认完之后下一轮照常往下问，不会耽误进度。
但别连着用两次，连着用就显得像在拖延。

"""

_REFLECT_BLOCK_OFF = """【本场不启用 reflect】每次发言都必须是一个具体问题（action = ask 或 probe）。

"""

# 真题取材规则：只有本场真的抽到真题时才注入，没抽到就不给模型提这回事，
# 免得它在没有素材的情况下"凭空回忆"出一道真题来。
_REAL_RULE = """【真题取材：本场最多 {quota} 道主问题可用真题】
上面「真实企业面试真题」是本场的取材素材，最多挑其中 {quota} 道作为主问题，其余主问题仍然要从考点参考里选，不要整场都在念真题。
- 真题是素材不是脚本：用自己的话口语化地问出来，可以结合候选人的经历调整问法，严禁照念原文。
- 取材真题时，topic 字段填该题的主题短语（题目后面括号里已给出），这样同场与跨场的去重逻辑才能生效。
- 真题同样受追问、非提问回合、节奏规则约束，没有例外。

"""

# 简历工具规则：只有本场真的带了简历才注入。没有简历就不提"查阅"这回事，
# 免得模型对着一个空的索引字段发起无意义的检索。
_TOOL_RULE = """【可选动作：主动查阅简历原文（tool）】
上面「简历条目索引」每条只有一句话摘要。如果你要问的那条经历必须看到完整原文才问得准
（核对具体数字、技术选型、时间线、职责边界），可以自己发起一次检索，而不是凭摘要猜着问：
- 决定查阅：tool 填 "resume_lookup"，tool_query 填你要查的关键词（项目名 / 技术名词 / 岗位 / 奖项，几个词就够）。
  系统会把检索到的简历原文补给你，你据此再给出这一轮的最终话术。
- 不需要查阅：tool 一律填 "none"，tool_query 填空字符串。
是否查阅完全由你判断，不要每轮都查；凭已有信息能问准就别查。

"""


def _build_avoid_block(profile: dict[str, Any], asked_topics: list[str] | None) -> str:
    """拼出「已经考过的考点」段落；没有历史时返回空串。

    两个来源合并：本场已经问过的（asked_topics，由 app 逐轮累积），
    以及开新场时从最近几场练习记录里带过来的（profile["avoid_topics"]）。

    例外：`profile["jd_topics"]` 是本场明确要练的考点（JD 定向 / 只练没过的），
    必须从"不要再问"里剔掉——否则刚练过的考点会被最近几场记录带进 avoid 列表，
    把本场唯一该问的题目全部排除，定向场直接空转。
    """
    target = {str(t).strip() for t in (profile.get("jd_topics") or [])}
    seen: list[str] = []
    for topic in list(profile.get("avoid_topics") or []) + list(asked_topics or []):
        text = str(topic).strip()
        if text and text not in seen and text not in target:
            seen.append(text)
    if not seen:
        return ""
    lines = "\n".join(f"- {t}" for t in seen)
    return (
        "【已经考过的考点】（本场 + 最近几次练习问过的）\n"
        "这些题目一律不要再问，换一种问法也不行；请从下面考点参考里另选，"
        "或结合候选人背景换个角度命题。\n"
        f"{lines}"
    )


def _current_topic(asked_topics: list[str] | None) -> str:
    """当前正在问的考点。

    `asked_topics` 由 app 逐轮累积：ask 落一个新考点，probe / reflect 沿用当前考点
    （见 interview_state.next_asked_topics），所以最后一个就是"这道题"。
    开场轮列表为空 → 返回空串，此时没有"当前考点"可给参照。
    """
    for topic in reversed(asked_topics or []):
        text = str(topic).strip()
        if text:
            return text
    return ""


def _build_interviewer_system(
    profile: dict[str, Any],
    cur_q: int,
    probes_used: int,
    reflects_in_a_row: int,
    mood_state: dict[str, Any] | None = None,
    asked_topics: list[str] | None = None,
    query: str = "",
    recap: str = "",
) -> str:
    total = int(profile.get("total_rounds", 5))
    state = mood_state or {}
    max_probes = effective_max_probes(profile, state)
    allow_reflect = bool(profile.get("allow_reflect", True))

    if cur_q >= total:
        state_hint = "主问题已经问满，本轮必须收尾结束（action = end）。"
    elif probes_used >= max_probes:
        state_hint = "当前题目的追问次数已达上限，本轮必须换新题（action = ask），不许再追问。"
    elif not allow_reflect:
        state_hint = "本场是严格模式，不启用 reflect，按规则判断追问还是换题。"
    elif reflects_in_a_row >= MAX_REFLECTS_IN_A_ROW:
        state_hint = "你刚才已经连着做过几次非提问回合，本轮必须提出一个具体问题（action = ask 或 probe）。"
    else:
        state_hint = "按决策规则自行判断是追问、换题，还是做一次非提问回合。"

    # 情绪这条轴走两条信息：本场固定的人设风格 + 此刻变化的内部状态
    mood_block = profile.get("mood_style") or ""
    mood_block += ("\n" if mood_block else "") + describe_mood(state)

    # 简历 / 经历深挖方向 / 真题素材 / 当前考点参照 / 已考考点：按顺序拼成一段。
    # 简历按 query（当前话题 + 候选人上一句）聚焦检索，而不是每轮灌全文。
    #
    # 「当前考点参照」放在靠后位置：它是本轮最该被看到的东西（决定追问往哪挖），
    # 而模型对提示词尾部更敏感。没有参照（无数据 / 自拟题 / 行为面）时自动为空。
    blocks = [
        text.strip()
        for text in (
            resume_block(profile, query),
            deep_dive_block(profile),
            real_question_block(profile),
            recap,
            _build_avoid_block(profile, asked_topics),
            rubric_hint_block(profile, _current_topic(asked_topics)),
        )
        if text and text.strip()
    ]
    context_blocks = "\n\n".join(blocks) + "\n\n" if blocks else ""

    # 真题规则只在真的抽到真题时注入；否则留空，避免模型无中生有
    real_questions = profile.get("real_questions") or []
    real_rule = _REAL_RULE.format(quota=len(real_questions)) if real_questions else ""

    # 简历工具规则同样只在真的有简历时注入：没有简历就没有可查的东西
    tool_rule = _TOOL_RULE if (profile.get("resume") or "").strip() else ""

    return INTERVIEWER_SYSTEM.format(
        interview_type=profile.get("interview_type", "技术面"),
        scene=profile.get("scene", ""),
        level_label=profile.get("level_label", ""),
        major=profile.get("major", ""),
        depth=profile.get("depth", ""),
        probe_style=profile.get("probe_style", ""),
        strictness=profile.get("strictness", ""),
        focus=profile.get("focus", ""),
        mood_block=mood_block,
        dimensions="\n".join(f"- {d}" for d in profile.get("dimensions", [])),
        reference=reference_for(profile),
        mix_block=topic_mix_block(profile),
        candidate_bg=profile.get("candidate_bg", ""),
        context_blocks=context_blocks,
        real_rule=real_rule,
        tool_rule=tool_rule,
        total_rounds=total,
        cur_q=cur_q,
        probes_used=probes_used,
        max_probes=max_probes,
        state_hint=state_hint,
        reflect_block=_REFLECT_BLOCK if profile.get("allow_reflect", True) else _REFLECT_BLOCK_OFF,
    )


# ---------------------------------------------------------------------------
# 统一验收门（对应 DESIGN.md 第五节：模型输出结构化 JSON，代码再做兜底约束）
#
# 三个智能体走的是同一条链路：模型产出 JSON → 代码验收 → 不合格走兜底。
# "不合格"此前以零散的内联条件散在 interviewer_turn / _resume_tool_round /
# _merge_attribution 里——同一条规则换个调用点就要重写一遍，也没法单独验证。
# 这里把每道验收收成具名关卡：纯函数、无副作用、统一返回 (ok, reason)。
#
# 关卡只回答"这份产出合不合格、因为什么"，**不负责纠正**。纠正仍由各调用点
# 既有的兜底分支执行：_apply_fallbacks 的计数裁决、考点撞车时的点名重试、
# 工具回合的回落、归因缺失时的静默降级。不把关卡写成"自动纠正"，是因为纠正
# 带有场景取舍——同样是撞车考点，"重试一次"还是"直接接受"由调用点决定，
# 塞进关卡反而会把调用点的语义藏起来。
#
# 全项目关卡地图（同一种签名约定，位置按职责分放）：
#     action / topic / reply / adjusted_score —— 本模块，三个智能体的决策产出
#     resume_optimize.is_grounded             —— 简历改写能不能对上原文（不许编造）
#     resume_optimize.introduced_numbers      —— 改写里有没有原文没有的数字
#     knowledge.load_rubrics / rubric_for     —— 评分参照缺失或损坏时静默降级
# 它们验收的是不同产物，合并成一个函数只会让彼此耦合，所以这里只统一"签名约定"，
# 不强行把散在各模块的关卡收拢到一处。
# ---------------------------------------------------------------------------

# 面试官允许的动作空间，与提示词里的 JSON 契约逐字对应。
# 单独提出来是为了让"合法动作有哪些"只有一个出处，而不是散落在正则/判断里。
ACTIONS = frozenset({"ask", "probe", "reflect", "end"})


def _asked_set(asked_topics: list[str] | None) -> set[str]:
    """本场已问考点集合：统一去空白、忽略空串。"""
    return {str(t).strip() for t in (asked_topics or []) if str(t).strip()}


def gate_action(action: Any) -> tuple[bool, str]:
    """关卡·动作：决策动作是否落在合法动作空间内。

    大小写与空白由关卡自己收敛——模型把枚举写成 "ASK" 或 " probe " 都不算越界。
    """
    value = str(action or "").strip().lower()
    if value in ACTIONS:
        return True, ""
    return False, f"动作取值非法：{value or '（空）'}"


def gate_topic_duplicate(
    data: dict[str, Any], asked_topics: list[str] | None
) -> tuple[bool, str]:
    """关卡·考点：这一轮提出的**新主问题**是不是撞上了本场已问过的考点。

    只在 action == "ask" 时判定：追问 / 复述 / 收尾沿用同一道题，topic 不变是正常的，
    不该被当成重复。考点为空的 ask 也不在这里拦——那属于"产出不完整"，
    由 gate_new_question 负责。
    """
    if str(data.get("action") or "").strip().lower() != "ask":
        return True, ""
    topic = str(data.get("topic") or "").strip()
    if not topic:
        return True, ""
    if topic in _asked_set(asked_topics):
        return False, f"考点「{topic}」本场已经问过"
    return True, ""


def gate_new_question(
    data: dict[str, Any], asked_topics: list[str] | None
) -> tuple[bool, str]:
    """关卡·考点（产出侧）：这一轮是否**构成一个可接受的新主问题**。

    与 gate_topic_duplicate 是同一份产出的两个视角：前者负责"发现撞车"，
    后者负责"确认可用"——重试之后拿它判断新产出能不能顶替被拦下的那一条。
    合格要三件同时成立：action 是 ask、考点非空、考点没问过。
    """
    if str(data.get("action") or "").strip().lower() != "ask":
        return False, "产出不是新主问题（action 不是 ask）"
    topic = str(data.get("topic") or "").strip()
    if not topic:
        return False, "产出没有携带考点"
    if topic in _asked_set(asked_topics):
        return False, f"考点「{topic}」本场已经问过"
    return True, ""


def gate_reply(data: dict[str, Any]) -> tuple[bool, str]:
    """关卡·回复：这一轮是否真的说出了能给候选人看的话。

    空回复若被当成面试官发言写进历史，界面上就是一条空白消息——宁可回落上一轮，
    也不能让候选人对着空气作答。
    """
    if str(data.get("reply") or "").strip():
        return True, ""
    return False, "回复为空"


def gate_adjusted_score(data: dict[str, Any]) -> tuple[bool, str]:
    """关卡·归因产物：归因调用给出的 adjusted_score 能不能拿来校正综合分。

    只认非 bool 的数值：字符串 "85"、null、True 一并回落原始分，
    否则报告里的"校正分"会变成一串脏数据。
    """
    adjusted = data.get("adjusted_score")
    if isinstance(adjusted, (int, float)) and not isinstance(adjusted, bool):
        return True, ""
    return False, "归因产物没有可用的 adjusted_score"


def _apply_fallbacks(
    data: dict[str, Any],
    profile: dict[str, Any],
    cur_q: int,
    probes_used: int,
    reflects_in_a_row: int,
    mood_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """代码兜底：把模型给出的决策限制在合法区间内。

    这是 DESIGN.md 第五节强调的一层——模型可能绕圈或越界，
    计数权必须握在代码手里，而不是交给模型自觉。

    注意这里只把上限当"保险丝"：正常情况下节奏由模型判断，
    只有它明显失控（连续空转、追问不收敛、主问题问满）时才强行掐断。
    上限本身受情绪调制（不耐烦时更快放弃这道题），但调制与裁决都在代码侧完成。
    """
    total_rounds = int(profile.get("total_rounds", 5))
    max_probes = effective_max_probes(profile, mood_state or {})
    allow_reflect = bool(profile.get("allow_reflect", True))

    action = str(data.get("action") or "").strip().lower()
    if not gate_action(action)[0]:
        action = "ask"

    def _fallback_question() -> str:
        return "probe" if probes_used < max_probes else "ask"

    if cur_q == 0:
        # 开场那轮必须是提问
        action = "ask"
    elif cur_q >= total_rounds:
        # 主问题问满，强制收尾
        action = "end"
    elif action == "reflect" and (
        not allow_reflect or reflects_in_a_row >= MAX_REFLECTS_IN_A_ROW
    ):
        # 非提问回合超限，或者本场根本没开这个口子
        action = _fallback_question()
    elif action == "probe" and probes_used >= max_probes:
        # 追问超限，强制换题
        action = "ask"

    data["action"] = action
    data["reply"] = str(data.get("reply") or "").strip()
    # topic 是"这轮问的是哪个考点"，用来做同场 / 跨场去重。
    # 模型偶尔会漏填或填成非同类型，这里统一收敛成去空白的字符串，缺省为空串。
    topic = data.get("topic")
    data["topic"] = str(topic).strip() if topic is not None else ""
    # tool / tool_query 也一并收敛：非法取值归为"不调用工具"，
    # 避免模型偶发填出第三种取值时把后续检索逻辑带偏。
    data["tool"], data["tool_query"] = _normalize_tool(data)
    return data


def _normalize_tool(data: dict[str, Any]) -> tuple[str, str]:
    """收敛 tool / tool_query 两个字段，非法取值一律归为不调用工具。"""
    tool = str(data.get("tool") or "").strip().lower()
    if tool not in {TOOL_NONE, TOOL_RESUME}:
        tool = TOOL_NONE
    return tool, str(data.get("tool_query") or "").strip()


def _clear_tool(data: dict[str, Any]) -> None:
    """抹掉"待查"标记。

    每一轮的工具动作在这一轮内就已经落地（查到了 / 没命中 / 二次调用失败），
    决策里就不能再留着 resume_lookup —— 否则它会跟着历史消息回传给下一轮，
    被模型当成"我这一轮还该去查"，造成重复检索。
    """
    data["tool"] = TOOL_NONE
    data["tool_query"] = ""


def _resume_tool_round(
    messages: list[dict[str, str]],
    plan: dict[str, Any],
    profile: dict[str, Any],
    api_key: str,
    model: str,
    cur_q: int,
    probes_used: int,
    reflects_in_a_row: int,
    mood_state: dict[str, Any] | None,
) -> tuple[dict[str, Any], str]:
    """面试官主动查阅简历原文：先检索、再出话，最多循环 MAX_TOOL_ROUNDS 轮。

    模型在第一轮决策里把 tool 置为 resume_lookup 时，代码去执行这次检索，
    再把检索到的原文回灌给模型，让它给出真正面向候选人的那一轮话——
    这样"先查简历再提问"是真实发生的动作，而不是模型嘴上说说。

    v6 起这曾是**写死的两步**（查一次、再出话一次）。改成带上限的循环后，
    模型可以"查了 A 发现还要核对 B"，但上限由代码握着：
    MAX_TOOL_ROUNDS 到了就停，绝不因为模型反复要查而把单轮成本拉爆。

    返回 (最终决策, 工具轨迹说明)。没有简历、检索没命中、二次调用失败
    都回落到第一轮决策，保证面试不因新增能力而中断。

    契约：**返回的决策一律不带"待查"标记**（走完循环、或任何回落路径），
    所以调用方拿到的 data 可以直接写进历史。
    """
    tool, tool_query = _normalize_tool(plan)
    if tool != TOOL_RESUME or not (profile.get("resume_chunks") or []):
        _clear_tool(plan)
        return plan, ""

    current = plan
    convo = list(messages)
    traces: list[str] = []

    for round_no in range(1, MAX_TOOL_ROUNDS + 1):
        tool, tool_query = _normalize_tool(current)
        if tool != TOOL_RESUME:
            break  # 模型不再要查了，这一轮到此为止

        hits = resume_rag.top(profile, tool_query, resume_rag.TOP_K_INTERVIEW)
        if not hits:
            # 查了但没命中：如实记一笔，不再让模型接着查。
            # 同一份索引再查一次还是同样的结果，继续循环只是白花调用。
            traces.append(f"查阅简历「{tool_query or '—'}」（未命中）")
            break

        traces.append(
            "查阅简历「{}」→ {}".format(
                tool_query or "—", "、".join(f"{c['id']}·{c['section']}" for c in hits)
            )
        )

        # 关键：最后一次查阅时，必须让模型**这一轮就出最终话术**。
        # 否则命中上限时留下的 reply 是"我再核对一段"这类内部动作描述，
        # 会被当成面试官的话念给候选人听。
        last_chance = round_no >= MAX_TOOL_ROUNDS
        if last_chance:
            instruction = (
                "（以上是你刚刚检索到的简历原文。这是本轮最后一次查阅机会，"
                "请立刻给出这一轮的最终回复，JSON 格式保持不变；"
                'tool 一律填 "none"、tool_query 填空字符串。）'
            )
        else:
            instruction = (
                "（以上是你刚刚检索到的简历原文。请据此给出这一轮的最终回复，"
                'JSON 格式保持不变；不需要再查阅就填 tool="none"，'
                "确实还要核对别的条目才填 resume_lookup。）"
            )

        follow_up = convo + [
            {"role": "assistant", "content": json.dumps(current, ensure_ascii=False)},
            {
                "role": "user",
                "content": wrap_untrusted(
                    resume_rag.detail_block(hits, title="你查阅到的简历原文"),
                    "resume",
                )
                + "\n\n"
                + instruction,
            },
        ]
        try:
            nxt = _apply_fallbacks(
                chat_json(follow_up, api_key, model, temperature=0.7, think=False),
                profile,
                cur_q,
                probes_used,
                reflects_in_a_row,
                mood_state,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"[简历工具调用失败，回落上一轮决策] {exc}",
                file=sys.stderr,
                flush=True,
            )
            break

        if not gate_reply(nxt)[0]:
            # 调用没崩，但话是空的——同样回落，宁可少查一次也不能给候选人一片空白
            break

        current = nxt
        convo = follow_up  # 下一轮接在这一轮之后，模型能看到已经查过什么

    _clear_tool(current)
    return current, "；".join(traces)


def _resume_query(
    history: list[dict[str, Any]], asked_topics: list[str] | None = None
) -> str:
    """给简历检索用的查询串：候选人上一句 + 当前考点。

    用「候选人刚说的话」而不是面试官的问题，是因为下一轮要不要深挖、
    往哪个方向深挖，取决于候选人提到了简历里的哪段经历。
    """
    parts: list[str] = []
    for msg in reversed(history or []):
        if msg.get("role") == "user" and str(msg.get("content") or "").strip():
            parts.append(str(msg["content"]))
            break
    if asked_topics:
        parts.append(str(asked_topics[-1]))
    return " ".join(parts)


def interviewer_turn(
    history: list[dict[str, str]],
    profile: dict[str, Any],
    api_key: str,
    cur_q: int = 0,
    probes_used: int = 0,
    reflects_in_a_row: int = 0,
    mood_state: dict[str, Any] | None = None,
    asked_topics: list[str] | None = None,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """面试官走一轮。

    参数：
        history            —— 对话历史（assistant = 面试官，user = 候选人）；空列表表示刚开场
        cur_q              —— 已经问出的主问题数量
        probes_used        —— 当前这道主问题已经追问的次数
        reflects_in_a_row  —— 已经连续做了几次"不提问回合"
        mood_state         —— 面试官此刻的情绪状态（mood.py 产出），影响措辞与追问上限
        asked_topics       —— 本场已经问过的考点，用来避免重复出题
    """
    # 长会话压缩：只把最近 HISTORY_WINDOW 条原文发给模型，更早的压成前情提要。
    # 去重、进度这些状态本来就单独注入系统提示词，所以窗口化不会丢关键信息。
    window = history[-HISTORY_WINDOW:] if history else []
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": _build_interviewer_system(
                profile,
                cur_q,
                probes_used,
                reflects_in_a_row,
                mood_state,
                asked_topics,
                _resume_query(history, asked_topics),
                summarize_history(history),
            ),
        }
    ]
    if window:
        messages.extend(to_api_messages(window))
    else:
        messages.append({"role": "user", "content": "（面试现在开始，请开场并问第一道题。）"})

    data = chat_json(messages, api_key, model, temperature=0.7, think=False)
    data = _apply_fallbacks(
        data, profile, cur_q, probes_used, reflects_in_a_row, mood_state
    )

    # 同场 / 跨场去重的代码兜底：提示词里已经列了"已考考点"，但模型偶尔仍会
    # 无视它、换题时又选回同一个考点。这里给一次点名纠正的机会——仍重复就接受，
    # 不无限重试（宁可能一次重复，也不能把面试卡死）。
    if not gate_topic_duplicate(data, asked_topics)[0]:
        retry_messages = messages + [
            {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
            {
                "role": "user",
                "content": (
                    f"（「{data['topic']}」这个考点前面已经问过了。请换一个还没问过的考点，"
                    "重新给出这一轮的主问题，并保持 JSON 格式不变。）"
                ),
            },
        ]
        try:
            retried = _apply_fallbacks(
                chat_json(retry_messages, api_key, model, temperature=0.7, think=False),
                profile, cur_q, probes_used, reflects_in_a_row, mood_state,
            )
        except ValueError:
            retried = None
        # 重试产出要能顶替被拦下的那一条，必须**确实是个新主问题**（关卡判定），
        # 否则宁可沿用第一版决策（哪怕考点重复一次），也不接受一个残缺产出。
        if retried and gate_new_question(retried, asked_topics)[0]:
            data = retried

    # 面试官主动查阅简历原文（tool = resume_lookup）：两步走——先检索、再出话。
    # 不调工具时，首轮决策就是最终决策，行为与之前完全一致（安全降级）。
    data, tool_trace = _resume_tool_round(
        messages,
        data,
        profile,
        api_key,
        model,
        cur_q,
        probes_used,
        reflects_in_a_row,
        mood_state,
    )
    # 工具轨迹随决策一起落盘，报告里"面试官决策轨迹"表据此多出一列，
    # 让评委看得到"面试官这一轮主动查了简历第几段"。
    # 待查标记由 _resume_tool_round 统一清掉（见该函数的契约）。
    data["tool_trace"] = tool_trace
    _clear_tool(data)

    return data


# ---------------------------------------------------------------------------
# 智能体 2 · 评估官
# ---------------------------------------------------------------------------

EVALUATOR_SCORE_SYSTEM = UNTRUSTED_GUARD + """你是一位资深面试评委。请根据这场{interview_type}的完整对话记录，对候选人做出结构化评估。

【本场面试配置】
{summary}
提问深度：{depth}
评判标准：{strictness}

【评分维度】必须使用下面给定的维度，不要自己发明，也不要增减：
{dimensions}

【考点评分参照】本场面试官问到了下面这些考点，请对照要点清单判分。
{rubric_block}
{unanchored_block}
{resume_block}
参照使用规则：
1. must_hit 是核心要点，候选人没讲到就要扣，并写明漏了哪条。
2. 候选人给出参照之外但同样正确的答法，照常算分——参照是底线不是天花板，禁止因为「和参照不一样」而扣分。
3. common_errors 里踩中的概念错误，写进该考点的 errors；若属于「一票否决的误区」，必须原样照抄到 errors，并在 risks 里概括。
4. 某个考点如果本场根本没被问到，就在 comment 里写明「本场未问到」，missed 留空；不要把没问过的考点算成候选人的遗漏。
5. 面试记录里每轮面试官发言前都标了【考点：…】。判分时必须**按轮次归属**：只把属于某个考点的那几轮对话拿去对照该考点的要点，不要把这个考点的标准套到别的题上。
6. 对【没有参照的题】那一组（见上），按下面的现拟规则处理。

【没有参照的题：请现场现拟参照，再判分】
上面「考点评分参照」里没有的考点，就是本场面试官自拟的题。它们同样要判，规则如下：
1. 先回到面试记录里，找出标着这个【考点：…】的那几轮对话，看清这道题究竟在问什么。
2. 替这道题现拟 3~5 条 must_hit：一个合格应届生答这道题必须讲到的核心要点，只写这道题本身要求的，不要把别的考点的要点拼进来。
3. 再逐条判断候选人讲到了哪几条、漏了哪几条、有没有明显讲错的地方，照常写进 must_hit / missed / errors。
4. **现拟参照不许写「一票否决」**。库里带否决权的误区是人工逐条核对过的，现场现拟的要点没有这个资格；即便候选人答得很差，也只按普通扣分处理。
5. 现拟参照的可靠性低于库参照，判分时宁可保守：拿不准算不算漏，就不要算漏。
6. 所有 rubric_check 条目都要如实填 source 字段：对照库里既定参照判的填 "library"，用你现场现拟的要点判的填 "generated"。这个字段会直接显示给候选人，填错等于把现拟的结论冒充成核对过的结论。

【评分要求】
1. 每个维度 0-100 分。评语必须引用候选人在面试记录里的原话或具体表现，不许写「感觉不错」这类空话。
2. 严格但不苛刻：对应届生不要求工程经验深度，但要求逻辑清晰、有真实细节。
3. 有明显硬伤（概念错误、答非所问、全程没有具体事例）要直接指出。
{extra}

本次调用**只提供候选人自身的表现材料，不含面试官状态信息**。面试官状态对表现的影响
由另一次独立调用单独归因，所以你不要考虑它，也不要替面试官的行为开脱或加重。

rubric_check 是可选字段：本场只要问到过考点就要逐条给出（每个考点一条，包括没有库参照、由你现场现拟的）。若本场一个考点都没记录，可省略该字段。
resume_check 同样是可选字段：只有在本场提供了「候选人简历」时才填。没有提供简历就省略该字段。

只输出 JSON，不要输出任何其他文字，格式如下：
{{
  "overall_score": 0 到 100 的整数,
  "level": "不建议推进 或 待定 或 建议推进 或 强烈推荐",
  "dimensions": [
    {{"name": "维度名称", "score": 0 到 100 的整数, "comment": "评语，必须引用候选人原话或具体表现"}}
  ],
  "rubric_check": [
    {{"topic": "考点原文", "source": "library 或 generated", "must_hit": ["候选人讲到的核心要点"], "missed": ["没讲到的核心要点"],
     "errors": ["踩中的常见误区"], "comment": "一句话说明"}}
  ],
  "resume_check": {{
    "consistent": true 或 false（自述与简历是否一致）,
    "issues": [
      {{"claim": "简历里的原句或要点", "said": "候选人在面试中的说法", "comment": "矛盾 / 夸大的具体说明"}}
    ],
    "uncovered": ["简历里有、但整场没被问到的关键经历"],
    "comment": "一句话总结；没有简历或未发现不一致时如实说明"
  }},
  "highlights": ["亮点，2-3 条"],
  "risks": ["风险或硬伤，2-3 条"],
  "summary": "一段话总评，150 字以内"
}}"""

# 归因是**独立上下文**的一次调用：只回答一件事——候选人的表现有多少是被面试官状态逼出来的。
# 刻意与评分子调用分开，而不是在同一次调用里多要两个字段：同一次调用里，
# 面试官的情绪表演会顺着上下文影响他对候选人的判断（审查方一旦继承起草方的叙事，
# 审查就退化成自我确认）。所以评分子调用根本拿不到状态轨迹。
EVALUATOR_ATTRIBUTION_SYSTEM = UNTRUSTED_GUARD + """你是一位资深面试评委。评分子任务已经完成，现在你只做一件事：
判断候选人的表现有多少是被面试官状态逼出来的。

【本场面试配置】
{summary}

【面试官状态轨迹】这是刻意模拟出来的实验条件，不是客观标准。
{mood_block}

【评分子任务的结果】不要推翻它，只在此基础上做校正。
{score_brief}

要求：
1. adjusted_score：剥离面试官状态干扰后，你估计的他的真实水平（0 到 100 的整数）。
2. attribution 必须写具体：指出是哪几轮受到干扰、哪个扣分项是被面试官逼出来的
   （紧张、过度道歉、语无伦次不应全部算进真实水平）。
3. 如果本场面试官状态始终中性，adjusted_score 可以与 overall_score 相同，并如实说明。
4. 不许重新打分 overall_score，也不许修改维度分——你只输出下面两个字段。

只输出 JSON，不要输出任何其他文字，格式如下：
{{
  "adjusted_score": 0 到 100 的整数（剥离面试官状态干扰后的真实水平估计）,
  "attribution": "归因说明：哪些扣分来自面试官状态，哪些来自候选人自身；状态中性时如实写明"
}}"""


_STAR_EXTRA = """4. 本场是行为面，请额外检查每个回答是否符合 STAR 结构
（Situation 情境 / Task 任务 / Action 行动 / Result 结果），
指出缺了哪一环，并重点看 Action 里候选人【本人】做了什么、Result 是否有量化结果。"""

_TECH_EXTRA = """4. 本场是技术面，请额外关注：概念是否说清、有没有真实的项目或实习细节、
遇到不会的问题时是硬编还是坦诚说明思路。"""

# format_transcript 给"没有考点"的轮次打的占位符。评估官偶尔会把它当成一个
# 真实考点补出一行 rubric_check，这里统一认这几个字面量。
_UNTOPICED = {"（未标注）", "(未标注)", "未标注", "（未标注考点）", "-", "—"}


def evaluate_interview(
    history: list[dict[str, str]],
    profile: dict[str, Any],
    api_key: str,
    mood_trace: list[dict[str, Any]] | None = None,
    asked_topics: list[str] | None = None,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """评估官：按本场能力模型给面试表现打分，并对面试官状态做归因。

    `asked_topics` 是本场**实际问到**的考点（app 逐轮累积）。
    必须传：否则评估官会拿到该难度档的全部 15~17 条参照，把没问过的考点
    判成候选人的「遗漏」，再喂给教练生成"去补这个考点"的错误建议。
    """
    # 一致性核查的检索串：候选人全场说过的话 + 本场考点。
    # 评估官要核对的是"整场自述 vs 简历"，所以比面试官侧看得更全（top-3 条切片）。
    evidence_query = " ".join(
        str(m.get("content") or "") for m in history if m.get("role") == "user"
    )[:1200] + " " + " ".join(str(t) for t in (profile.get("topics") or []))
    transcript = format_transcript(history, tag_topics=True)

    # 第一次调用：只评分。系统提示词里没有状态轨迹，评委看到的就是候选人自己的表现。
    score = chat_json(
        [
            {
                "role": "system",
                "content": EVALUATOR_SCORE_SYSTEM.format(
                    interview_type=profile.get("interview_type", "技术面"),
                    summary=profile_summary(profile),
                    depth=profile.get("depth", ""),
                    strictness=profile.get("strictness", ""),
                    dimensions="\n".join(f"- {d}" for d in profile.get("dimensions", [])),
                    rubric_block=rubric_block(profile, asked_topics),
                    unanchored_block=unanchored_block(profile, asked_topics),
                    resume_block=resume_evidence_block(profile, evidence_query),
                    extra=_STAR_EXTRA
                    if profile.get("interview_type") == "行为面"
                    else _TECH_EXTRA,
                ),
            },
            {"role": "user", "content": f"以下是完整面试记录：\n\n{transcript}"},
        ],
        api_key,
        model,
        temperature=0.2,
        timeout=REPORT_TIMEOUT,
    )
    if not isinstance(score, dict):
        score = {}
    score = _normalize_rubric_check(score)
    return _merge_attribution(score, transcript, profile, api_key, mood_trace, model)


def _normalize_rubric_check(score: dict[str, Any]) -> dict[str, Any]:
    """把 rubric_check 里那些**不对应任何考点**的行剔掉。

    来历：`format_transcript` 会给每轮面试官发言标【考点：…】，收尾那一轮
    （action=end）本来就是一句「今天先到这里」，没有考点，于是被标成
    【考点：（未标注）】。实测评估官会照着这个标记补一行
    `{"topic": "（未标注）", "source": "generated", ...}`，报告里就多出一条
    既没有参照、也没有任何内容的判定。

    这里按字面量剔除，不做模糊判断：只认那几个明确的占位符。
    """
    rows = score.get("rubric_check")
    if not isinstance(rows, list):
        return score
    kept = [
        item
        for item in rows
        if isinstance(item, dict)
        and str(item.get("topic") or "").strip()
        and str(item.get("topic") or "").strip() not in _UNTOPICED
    ]
    if len(kept) != len(rows):
        score = {**score, "rubric_check": kept}
    return score


def _score_brief(score: dict[str, Any]) -> str:
    """把评分结果压成归因调用能读的一段文字（维度分 + 评语 + 风险）。

    归因调用要"不推翻评分、只做校正"，所以它必须看到评分结果本身，
    尤其是各维度的评语——那里带着候选人原话，是判断"哪一轮被逼出来"的依据。
    """
    lines = [
        f"综合分 overall_score：{score.get('overall_score', '未知')}",
        f"结论 level：{score.get('level', '未知')}",
    ]
    for item in score.get("dimensions") or []:
        if isinstance(item, dict):
            lines.append(
                f"- {item.get('name', '')}：{item.get('score', '')} 分｜"
                f"{item.get('comment', '')}"
            )
    risks = score.get("risks") or []
    if risks:
        lines.append("风险：" + "；".join(str(r) for r in risks))
    return "\n".join(lines)


def _merge_attribution(
    score: dict[str, Any],
    transcript: str,
    profile: dict[str, Any],
    api_key: str,
    mood_trace: list[dict[str, Any]] | None,
    model: str,
) -> dict[str, Any]:
    """把「评分」与「归因」合并回一份评估结果。

    评分结果里刻意不含 adjusted_score / attribution，这里先把它们丢掉，
    只认归因那一次独立调用的产出：否则"评分调用顺手给了个校正分"就会绕过隔离。

    任何失败都静默降级：报告宁可少一个归因字段，也不能因为多做一次调用而整份产不出来。
    """
    result = {
        key: value
        for key, value in score.items()
        if key not in ("adjusted_score", "attribution")
    }

    if not mood_trace:
        # 本场没有状态轨迹，归因无从谈起：不额外花一次调用，校正分与原始分一致。
        result["adjusted_score"] = score.get("overall_score")
        result["attribution"] = "本场未记录面试官状态轨迹，未做状态干扰归因。"
        return result

    # 第二次调用：只归因。这里才把状态轨迹交出去，且交出去的是**独立上下文**。
    try:
        attribution = chat_json(
            [
                {
                    "role": "system",
                    "content": EVALUATOR_ATTRIBUTION_SYSTEM.format(
                        summary=profile_summary(profile),
                        mood_block=format_mood_trace(
                            mood_trace, profile.get("mood_mode", "")
                        ),
                        score_brief=_score_brief(score),
                    ),
                },
                {"role": "user", "content": f"以下是完整面试记录：\n\n{transcript}"},
            ],
            api_key,
            model,
            temperature=0.2,
            timeout=REPORT_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[评估官归因调用失败，已按未校正分数记录] {exc}",
            file=sys.stderr,
            flush=True,
        )
        attribution = None

    data = attribution if isinstance(attribution, dict) else {}
    adjusted = data.get("adjusted_score")
    # 归因产物的验收：拿到可用的 adjusted_score 才用它校正，否则一律回落到原始分。
    result["adjusted_score"] = (
        int(adjusted) if gate_adjusted_score(data)[0] else score.get("overall_score")
    )
    result["attribution"] = str(data.get("attribution") or "").strip() or (
        "本场归因未产出，已按未校正分数记录。"
    )
    return result


# ---------------------------------------------------------------------------
# 智能体 3 · 教练
# ---------------------------------------------------------------------------

COACH_SYSTEM = UNTRUSTED_GUARD + """你是一位专门辅导应届生的面试教练，说话直接、务实，不讲套话。

【本场背景】
{summary}

【面试官状态轨迹】
{mood_block}

{extra}

请根据面试记录和评委评估，做一份可执行的复盘。要求：
1. 指出的每个问题都必须有面试记录里的证据（引用候选人的原话或表现），不许凭空评价。
2. 示范答案要写成候选人能直接背下来的口语稿，不要写成书面报告。
3. drills 必须是 3 天内能做完的具体动作，不写「多练习」这种空话。
4. 如果本场面试官状态明显偏离中性（不耐烦、时间压力升高、刻意挑刺），
   必须额外看一件事：候选人在这种压力下的稳定性——是否慌乱、是否反复道歉、
   是否一被追问就推翻自己原本正确的结论。这部分写进 resilience。
   同时明确告诉候选人：哪些表现是被对方逼出来的，遇到这类面试官要如何稳住自己。
   如果本场面试官状态中性，resilience 里如实写"本场未出现压力场景"。
5. 如果给了「考点遗漏与误区」，drills 要直接针对漏掉的具体要点来练，
   不要写成「多练习」这类泛化动作。

只输出 JSON，不要输出任何其他文字，格式如下：
{{
  "weakest": [
    {{"problem": "问题", "evidence": "面试记录里的原话或表现", "fix": "具体怎么改"}}
  ],
  "rewrites": [
    {{"question": "面试官问的问题", "original": "候选人的原回答（可压缩，保留问题特征）",
      "improved": "改写后的示范答案（口语稿，150 字以内）", "why": "为什么这样改更好"}}
  ],
  "resilience": {{
    "summary": "候选人在压力下的稳定性表现",
    "evidence": "面试记录里的原话或表现",
    "fix": "下次遇到这类面试官该怎么稳住"
  }},
  "drills": ["接下来 3 天可以练的具体动作，共 3 条"],
  "encouragement": "一句真诚的鼓励，不要浮夸"
}}"""

_COACH_STAR = """本次是行为面，请特别检查候选人的回答是否补齐了 STAR 四环
（情境 / 任务 / 行动 / 结果），指出缺了哪一环；示范答案里要把 Action 中「我」做了什么
和 Result 的量化结果补上。"""

_COACH_TECH = """本次是技术面，请特别关注：概念说得清不清楚、有没有真实的项目或实习细节、
遇到不会的问题时是硬编还是坦诚说明思路。示范答案要给出「结构化表达」的模板，
比如先给结论、再讲原理、最后落到项目实践。"""


def _rubric_missed_block(evaluation: dict[str, Any]) -> str:
    """从评估官的 rubric_check 里摘出「漏了哪条要点 / 踩了哪个误区」，供教练定位 drills。

    没有该字段（或参照缺失）时返回空串，调用方据此决定要不要拼进提示词。
    """
    rows = evaluation.get("rubric_check") or []
    lines: list[str] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        missed = item.get("missed") or []
        errors = item.get("errors") or []
        if not missed and not errors:
            continue
        piece = f"考点「{item.get('topic', '')}」："
        if missed:
            piece += "漏了 " + "；".join(str(x) for x in missed) + "。"
        if errors:
            piece += "概念错误：" + "；".join(str(x) for x in errors) + "。"
        lines.append(piece)
    return "\n".join(lines)


def coach_review(
    history: list[dict[str, str]],
    evaluation: dict[str, Any],
    profile: dict[str, Any],
    api_key: str,
    mood_trace: list[dict[str, Any]] | None = None,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """教练：基于面试记录 + 评估结果，输出改进建议与示范答案。"""
    is_behavior = profile.get("interview_type") == "行为面"
    system = COACH_SYSTEM.format(
        summary=profile_summary(profile),
        mood_block=format_mood_trace(mood_trace, profile.get("mood_mode", "")),
        extra=_COACH_STAR if is_behavior else _COACH_TECH,
    )
    missed = _rubric_missed_block(evaluation)
    missed_part = f"评委标注的考点遗漏与误区：\n{missed}\n\n" if missed else ""
    user = (
        f"评委给出的综合得分：{evaluation.get('overall_score', '未知')}\n"
        f"评委估计的真实水平：{evaluation.get('adjusted_score', '未知')}\n"
        f"评委的归因说明：{evaluation.get('attribution', '')}\n"
        f"评委结论：{evaluation.get('level', '')}\n"
        f"评委总评：{evaluation.get('summary', '')}\n"
        f"评委指出的风险：{'；'.join(evaluation.get('risks', []))}\n\n"
        f"{missed_part}"
        f"面试记录：\n\n{format_transcript(history)}"
    )
    return chat_json(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        api_key,
        model,
        temperature=0.4,
        timeout=REPORT_TIMEOUT,
    )


# ---------------------------------------------------------------------------
# 智能体 4 · 建议官
#
# 与教练的分野是这一节最要紧的事。两者都拿评估结果 + 逐字稿，但视角相反：
#     教练：**回看** —— 你刚才那句话答得不好，它本来该怎么讲（rewrites 是话术）
#     建议官：**前看** —— 这个能力维度上你缺的是哪一类东西，下次面试前补什么
# 所以建议官被要求按**评分维度**给建议（每个维度一条），而不是按"哪道题答错"，
# 从数据形状上就把两者区分开，避免报告里出现两份高度雷同的"最需要改的问题"。
# ---------------------------------------------------------------------------

ADVISOR_SYSTEM = UNTRUSTED_GUARD + """你是一位带过上百名应届生的求职教练。你不是来复述这场面试的，而是来告诉候选人：**下一次面试前，具体该补什么**。

【本场背景】
{summary}

【评委的评分结果】
{evaluation_block}

{dimensions_block}
{extra}
请按下面四条要求给出建议：

1. **按维度给，不按题目给**。上面每个维度写一条建议（`dimension` 必须逐字使用上面给出的维度名称）。
   已经做得好的维度也要给——写"保持"，并说明**具体保持什么**，不要写"继续保持"这类空话。
2. **每条建议必须能落地**。`actions` 是本条维度下 1~3 个具体动作，要能在 3 天内开始做：
   写"把 Redis 的持久化机制（RDB/AOF 触发条件与恢复流程）整理成一张对照卡，下次面试能画出来"，
   不要写"多学习 Redis"。动作要指向**可交付物**（一张卡、一段代码、一次复盘、一个能讲 90 秒的故事）。
3. **`gap` 要指出缺的是哪一类东西**，而不是重复维度分低这件事。区别在于：
   - 缺知识：某个知识点没答上来 → 补哪个具体知识点；
   - 缺经历：讲不出真实项目细节 → 需要造哪一段可讲的经历（做一个小项目 / 把课程作业讲深）；
   - 缺表达：知道但讲不清、没有结构 → 练哪种表达结构（先结论后原理、STAR 里补哪一环）；
   - 缺量化：全程没有数字结果 → 需要补出哪个具体数字，以及怎么把它测出来。
4. **`evidence` 必须是面试记录里的原话或具体表现**。引用候选人说过的一句原话最好；
   如果要说的是"全程没有量化结果"这类表现，就写清楚是**哪一轮、什么表现**。
   不许写"感觉基础不扎实"这类没有依据的评价。

`priority` 是**跨维度的执行顺序**：如果时间只够改一件事，先改哪件，为什么。
`quick_wins` 是**今晚/明天就能改、且下一场立刻见效**的动作，只写 2~4 条，必须具体到"改哪句话、加哪个数字"。
`next_setup` 是**下一场面试该怎么配置**：用哪一档难度、技术面还是行为面、要不要开高强度情绪模式来练抗压。
结合上面评委的结论给出理由。

只输出 JSON，不要输出任何其他文字，格式如下：
{{
  "advice": [
    {{"dimension": "维度名称（必须逐字使用上面给出的名称）",
      "gap": "这个维度上缺的是哪一类东西：缺知识 / 缺经历 / 缺表达 / 缺量化，以及具体缺什么",
      "evidence": "面试记录里的原话或具体表现",
      "actions": ["1~3 个 3 天内能开始做的具体动作，指向可交付物"],
      "self_check": "一个自测问题：候选人能用它判断自己补到位了没有"}}
  ],
  "priority": ["跨维度的执行顺序，1~3 条，说明先改什么、为什么"],
  "quick_wins": ["2~4 条今晚或明天就能改、下一场立刻见效的具体动作"],
  "next_setup": "下一场面试该怎么配置，以及理由"
}}"""

_ADVISOR_STAR = """本场是行为面。给建议时请注意：行为面最常缺的是 **Action 里「我」做了什么** 和
**Result 里的量化结果**，而不是知识本身。如果候选人讲不出个人贡献，`gap` 要写成
「缺经历：这段经历里你本人的贡献边界没说清」，`actions` 要指向"把这段经历拆成
『我负责的具体动作 + 可验证的结果』，写成 90 秒能讲完的稿子"。"""

_ADVISOR_TECH = """本场是技术面。给建议时请注意：技术面最常缺的是 **原理层面的深度**
（会用来会用，但说不出为什么这样设计）和 **真实项目细节**（只有课程作业，没有可深挖的取舍）。
针对"表述不专业"这类问题，`actions` 要指向"把结论—原理—实践的表述模板套到某个具体知识点上练"。"""


def advisor_review(
    history: list[dict[str, str]],
    evaluation: dict[str, Any],
    profile: dict[str, Any],
    api_key: str,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """建议官：按评分维度给出"下次面试前该补什么"，并收敛成确定结构。

    刻意**不接收** `mood_trace`：压力下的稳定性已经由教练的 `resilience` 覆盖，
    建议官再看一遍只会产出重复内容。它要看的是"能力项"，不是"这一场的过程"。

    返回结构由 `advice.normalize` 收敛：维度名不在本场维度里的一律丢弃，
    没拿到建议的维度补占位。所以调用方拿到的一定与评分页的维度对得上。
    """
    is_behavior = profile.get("interview_type") == "行为面"
    system = ADVISOR_SYSTEM.format(
        summary=profile_summary(profile),
        evaluation_block=advice.advice_block(evaluation),
        dimensions_block="【本场的评分维度】（每条都要给一条建议，名称逐字一致）\n"
        + "\n".join(f"- {name}" for name in profile.get("dimensions", [])),
        extra=_ADVISOR_STAR if is_behavior else _ADVISOR_TECH,
    )
    user = (
        f"面试记录：\n\n{format_transcript(history)}"
    )
    data = chat_json(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        api_key,
        model,
        temperature=0.4,
        timeout=REPORT_TIMEOUT,
    )
    return advice.normalize(data, evaluation)


# ---------------------------------------------------------------------------
# 智能体 5 · 简历优化官
#
# 与面试链路完全解耦：输入是简历原文，输出是诊断 + 改写对照。
# 所以它既不需要逐字稿，也不需要先面一场试——上传简历就能拿到结果。
#
# 提示词里最要紧的是那条红线：**不许编造**。模型天然倾向于"把简历写得更好看"，
# 而"更好看"很容易滑向"替他编一段经历"。这条红线在提示词里写死一次，
# 再由 resume_optimize.normalize_report 在本地逐条核对一次（引用对不上就丢弃），
# 两道关卡都必要：提示词降低发生率，本地核对兜住漏网的。
# ---------------------------------------------------------------------------

RESUME_SYSTEM = UNTRUSTED_GUARD + """你是一位在大厂做过五年招聘、也帮人改过上千份简历的简历顾问。现在请你帮候选人优化简历。

【本次目标】
{target}

【简历原文】
{resume}

【本地已检出的结构情况】（系统自动统计，供你参考，不必重复计算）
{skeleton}

请按下面五条要求给出优化建议：

1. **不许编造**。这是红线：改写只能用简历里**已有**的信息去重新组织、量化、收紧措辞。
   严禁新增候选人没写过的经历、项目、公司、头衔、时间、奖项或数字。
   如果原文没有数字，就在 `advice` 里告诉他"这个结果需要你自己补一个真实数字，比如
   处理条数 / 耗时 / 提升比例"，而不是替他填一个。
2. **逐条给改写对照**。`rewrites` 里挑最该改的 3~5 条，逐条给出：
   `original` 必须**逐字引用简历原文里的句子**（系统会核对，对不上的一律丢弃）；
   `improved` 是改写后的句子，可以直接替换进简历；
   `why` 说明为什么这样改更好（例如"把『负责』换成『独立实现』并补上规模，职责边界才清楚"）。
   改写要遵守一条：
   - 用「动作 + 对象 + 方法 + 可验证结果」四要素重写，把"参与了""负责了"这类虚词换成具体动词；
   - 把笼统的描述收紧成有边界的一句话；
   - **没有数字就不许编数字**，宁可在 `advice` 里指出这里缺一个数字。
3. **诊断要有依据**。`problems` 的 `evidence` 要逐字引用简历原文里的句子（系统会核对，
   对不上的引用会被清掉）。如果某条诊断（例如"没看到量化成果"）本来就不针对某一句，
   `evidence` 填空字符串。
4. **`missing` 只提真正该有而没写的内容**：结合目标岗位说清楚"还需要补一段什么样的经历"，
   而不是泛泛地说"再多写点项目"。
5. **`keywords` 是关键词建议**：结合目标岗位，列出这份简历里**应该出现但没出现**的
   关键词（技术名词、工具、方法论）。只列他真实掌握的——这里只做提示，
   不要引导他去堆砌不熟悉的名词。
6. `score` 是你给这份简历整体的 0~100 分（针对上面的目标岗位）。
   `checklist` 是投出去之前的自查清单，每条都要能当场验证（例如"每段经历都有一句量化结果"）。

只输出 JSON，不要输出任何其他文字，格式如下：
{{
  "verdict": "一句话总评，说清这份简历最大的问题",
  "score": 0 到 100 的整数,
  "problems": [
    {{"severity": "high 或 medium 或 low",
      "category": "分类：结构 / 量化 / 措辞 / 匹配度 / 真实性",
      "problem": "问题是什么",
      "evidence": "简历原文里的原句；不针对某一句时填空字符串",
      "advice": "具体怎么改"}}
  ],
  "rewrites": [
    {{"original": "简历原文里的原句（逐字引用）",
      "improved": "改写后的句子，可直接替换",
      "why": "为什么这样改更好"}}
  ],
  "missing": ["这份简历针对该目标岗位还缺什么（缺哪类经历 / 缺哪块技能）"],
  "keywords": ["结合目标岗位，建议补上的关键词（只列他真实掌握的）"],
  "checklist": ["投简历前逐条自查，每条都能当场验证"]
}}"""


def _resume_skeleton(text: str) -> str:
    """把本地检出的结构情况拼成一段提示词。

    给这一段是为了让模型**别去猜**结构：小节有没有、量化占比多少，本地已经
    用启发式算过一遍了。模型看到之后就不会把注意力浪费在"这份简历有没有技能小节"
    这种本地就能答的问题上，而集中去写诊断和改写。
    """
    present = resume_optimize.present_sections(text)
    missing = resume_optimize.missing_sections(text)
    quant = resume_optimize.quantification(text)
    lines = [
        f"- 检出的小节：{'、'.join(present) if present else '（一个都没检出）'}",
        f"- 通常必备但没检出：{'、'.join(missing) if missing else '（无）'}",
        f"- 量化密度：{quant['lines']} 行里 {quant['quantified']} 行含量化结果"
        f"（{quant['ratio']:.0%}）",
        "- 说明：上面是本地正则的粗判，可能误判；与你看到的不一致时，以简历原文为准。",
    ]
    return "\n".join(lines)


def resume_review(
    resume_text: str,
    api_key: str,
    target_role: str = "",
    major: str = "",
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """简历优化官：按简历原文给诊断 + 改写对照。

    独立于面试：不读逐字稿、不进 `history.json`、不写快照。简历原文与结果都
    只活在本会话内存里，与 `resume.py` / `resume_rag.py` 同一条隐私约束。

    `target_role` / `major` 只用于让建议"对着目标岗位说"；都不填也能用，
    此时提示词里的目标是"通用求职优化"，不假装知道他的方向。
    """
    text = (resume_text or "").strip()
    if not text:
        return {}

    targets: list[str] = []
    if target_role.strip():
        targets.append(f"目标岗位：{target_role.strip()}")
    if major.strip():
        targets.append(f"专业方向：{major.strip()}")
    target = "\n".join(targets) if targets else "未指定目标岗位，按通用校招简历标准优化。"

    system = RESUME_SYSTEM.format(
        target=target,
        resume=wrap_untrusted(text, "resume"),
        skeleton=_resume_skeleton(text),
    )
    data = chat_json(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": "请按上面的要求，输出这份简历的优化建议。"},
        ],
        api_key,
        model,
        temperature=0.4,
        timeout=REPORT_TIMEOUT,
    )
    return resume_optimize.normalize_report(data, text)
