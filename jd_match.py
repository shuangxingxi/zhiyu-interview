"""岗位 JD 对照：把一段岗位描述换算成「考点库里的考点 + 能力维度 + 你的练习情况」。

纯本地计算，不调用模型。三层信息：

1. **考点**：中文按 2~4 字 n-gram 的 IDF 加权匹配，英文技术词（React / TCP/IP /
   MySQL…）按 token 集合取交集。IDF 在 270 条考点组成的语料上统计——只能匹配
   考点库里真实存在的东西，不会凭空造考点。
2. **维度**：每个专业 5 个能力维度，按关键词表启发式归类（一个考点只归一个）。
   这是近似映射，不是模型判断，所以结果标注「启发式」。
3. **练习情况**：读历史记录里 `evaluation.rubric_check`，用与复盘报告同一个
   `knowledge.topic_verdict` 升成考点级判定，回答「这个岗位的考点你答得上几条」。

用法：`analyze_jd(jd_text, major, rubrics=load_rubrics(), records=load_records())`。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

import knowledge

MAX_TOPICS = 6           # 最多列几个考点
MAX_PER_DIMENSION = 2    # 同一维度最多占几个名额，避免一屏全是同类
MIN_SCORE = 3.0          # 总分低于这个分不算命中
MIN_BEST_IDF = 3.0       # 至少有一个「够稀有」的命中词才算命中，挡住只靠「系统/事务」凑分的
MAX_EVIDENCE = 3         # 每个考点最多展示几句 JD 原文
UNASKED = "本场未问到"   # rubric_check 里未问到考点的占位标记

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_EN = re.compile(r"[A-Za-z][A-Za-z0-9+#.]*")
_EN_COMPOUND = re.compile(r"[A-Za-z][A-Za-z0-9+#.]*(?:/[A-Za-z][A-Za-z0-9+#.]*)+")
_CLAUSE = re.compile(r"[；;，,。\n！!？?、]")

# n-gram 首尾是这些字，说明它跨了词边界（如「与线」「据结」「用数据」），丢掉。
_FUNCTION = set("的了和与在是有或及等就也都被对为以而但并且之其所能可将使更最很用")
# JD 套话：这些词在 JD 里几乎必然出现，命中它们说明不了任何技术水平。
_FILLER = (
    "熟悉", "了解", "掌握", "优先", "经验", "能力", "负责", "参与", "团队",
    "沟通", "表达", "良好", "要求", "岗位", "工作", "相关", "具备", "熟练",
    "至少", "以上", "常用", "常见", "基础", "加分", "任职", "精神", "协作",
)
_EN_STOP = {
    "and", "or", "the", "with", "for", "you", "your", "job", "team", "years",
    "plus", "preferred", "etc", "hr", "pm", "ok", "we", "our", "will", "are",
}

# 维度关键词表：按顺序匹配，先命中先算；一个考点只归一个维度。
# 顺序有讲究：越具体的维度排越前（例如「索引」必须先于「系统」）。
DIMENSION_RULES: dict[str, list[tuple[str, str]]] = {
    "计算机科学与技术": [
        ("操作系统与计算机网络", "线程|进程|内存|虚拟|中断|调度|死锁|TCP|UDP|HTTP|DNS|TLS|URL|拥塞|握手|网络|文件系统|I/O|MESI|缓存一致性协议|内存模型"),
        ("数据库与存储设计", "数据库|索引|SQL|事务|Redis|InnoDB|redo|undo|存储引擎|隔离级别|MVCC|分库|慢查询|持久化|B\\+|ACID"),
        ("编程基础与数据结构", "树|链表|栈|队列|堆|哈希|排序|递归|动态规划|复杂度|数组|字符串|图|算法|位运算|数据结构|面向对象|封装|继承|多态"),
        ("工程实践与调试能力", "JWT|Session|鉴权|登录|认证|Git|测试|调试|日志|监控|重构|版本|CI|部署|异常|性能|压测|代码|分布式|Raft|链路|CPU|排查|稳定性"),
        ("沟通与表达", "沟通|表达|汇报|讲|解释|面试|自我介绍|提问|澄清|选型|复盘|文档|协调|STAR|描述|项目|经历"),
    ],
    "人工智能": [
        ("模型原理与推导", "RAG|检索增强|Agent|工具调用|规划|Prefill|Decode|推理优化|推导|Transformer|Attention|预训练|微调|卷积|池化|BN|批归一|激活|损失|梯度|反向|过拟合|正则|监督学习|无监督|回归|泛化|似然"),
        ("数学与算法基础", "向量|矩阵|概率|贝叶斯|方差|偏差|距离|学习率|优化器|降维|检验|置信|复杂度"),
        ("数据处理与特征工程", "特征|采样|编码|缺失|异常值|缩放|归一化|清洗|不平衡|分布|词袋|TF-IDF"),
        ("实验设计与结果分析", "幻觉|评测|忠实度|事实性|校准|实验|评估|指标|A/B|交叉验证|消融|显著|模型选择|召回|AUC|调参|训练集|验证集|测试集|划分"),
        ("沟通与表达", "沟通|表达|汇报|解释|面试|答辩|讲述|陈述|文档|撰写|STAR|描述|项目"),
    ],
    "软件工程": [
        ("架构与系统设计", "微服务|CAP|最终一致|限流|降级|分布式|拆分|边界|依赖治理|混沌|模块化|架构|设计模式|SOLID|接口|领域驱动|契约"),
        ("测试与质量意识", "测试|契约|单元测试|测试替身|回归|缺陷|Bug|代码评审|静态检查|可测性"),
        ("工程流程与协作", "Git|分支|CI|CD|流水线|版本控制|敏捷|需求评审|排期|文档|日志|监控|线上|故障|技术债|重构|发布|需求分析|用户故事|验收标准"),
        ("编程与设计能力", "代码|命名|注释|封装|UML|MVC|三层|RESTful|异常|幂等|消息队列|缓存|数据库索引|并发|内存模型|可见性|JMM"),
        ("沟通与表达", "沟通|表达|汇报|讲解|STAR|提问|澄清|协调|对齐|复盘|描述|项目|实习"),
    ],
    "电子信息": [
        ("信号与系统", "信号|采样|奈奎斯特|滤波|频率|频谱|调制|波特|眼图|传输线|阻抗|S 参数|TDR|抖动|锁相环"),
        ("电路与器件基础", "放大|运放|MOS|三极管|欧姆|基尔霍夫|电容|RC|电源|纹波|电路|器件|负载|电源与地"),
        ("硬件调试与测量", "示波器|万用表|测量|调试|逻辑分析仪|频谱仪|串口|I2C|SPI|PCB|焊接|布局布线"),
        ("系统集成能力", "系统|时钟|电源完整|EMC|电磁|选型|联调|模块|集成"),
        ("沟通与表达", "沟通|表达|讲解|汇报|面试|文档|STAR|解释|答辩|描述|项目|对齐|评审|论证|陈述|风险"),
    ],
    "自动化": [
        ("控制理论基础", "PID|传递函数|状态空间|李雅普诺夫|稳定性|闭环|开环|Bode|频域|裕度|线性化|反馈|误差|系统|时域|阶跃|采样延迟|补偿"),
        ("数学与建模能力", "建模|微分方程|拉普拉斯|方块图|信号流图|卡尔曼|矩阵|频率特性"),
        ("传感器与执行机构", "传感器|编码器|电机|伺服|执行|继电器|接触器|变频器|PLC|标定"),
        ("系统调试能力", "调试|故障|排查|干扰|噪声|整定|现场|接线|联调"),
        ("沟通与表达", "沟通|表达|汇报|讲解|面试|文档|STAR|协调|描述|项目|答辩|质疑|说明书"),
    ],
    "通信工程": [
        ("通信原理基础", "香农|OFDM|MIMO|信道编码|衰落|均衡|调制|解调|多址|帧结构|NR|扩频|复用|PCM|交换|天线|方向图|增益|信道"),
        ("信号处理能力", "信号|采样|量化|抽样|滤波|眼图|星座|频谱|成形|基带|频带|带宽|码|频率|波长"),
        ("系统与协议理解", "协议|链路|系统|仿真|全双工|误码|速率|时延|网络"),
        ("仿真与实验能力", "MATLAB|仿真|蒙特卡洛|实验|误码率|测量|仪器|示波器|频谱仪"),
        ("沟通与表达", "沟通|表达|汇报|讲解|面试|文档|STAR|答辩|术语|描述|项目|表述|辩护|选型|对比"),
    ],
}
_DIMENSION_PATTERNS = {
    major: [(name, re.compile(pattern)) for name, pattern in rules]
    for major, rules in DIMENSION_RULES.items()
}

_INDEX_CACHE: dict[int, dict[str, Any]] = {}


def _cn_ngrams(text: str) -> set[str]:
    """2~4 字中文 n-gram，去掉跨词边界的与 JD 套话。"""
    grams: set[str] = set()
    for run in _CJK.findall(text):
        for size in (2, 3, 4):
            for i in range(len(run) - size + 1):
                gram = run[i : i + size]
                if gram[0] in _FUNCTION or gram[-1] in _FUNCTION:
                    continue
                if any(filler in gram for filler in _FILLER):
                    continue
                grams.add(gram)
    return grams


def _en_tokens(text: str) -> dict[str, str]:
    """英文技术词：小写 token → 出现时的原始写法（用于展示）。"""
    tokens: dict[str, str] = {}
    for raw in _EN.findall(text):
        for part in re.split(r"[/,;、]", raw):
            cleaned = part.strip(".-_")
            key = cleaned.lower()
            if len(key) >= 2 and not key.isdigit() and key not in _EN_STOP:
                tokens.setdefault(key, cleaned)
    return tokens


def _doc_text(topic: str, entry: dict[str, Any]) -> str:
    """一条考点的全部文本：名称 + 必中要点 + 加分点 + 常见误区。"""
    parts = [topic]
    for field in ("must_hit", "bonus", "common_errors"):
        parts.extend(str(item) for item in (entry.get(field) or []))
    return "。".join(parts)


def _build_index(rubrics: dict[str, Any]) -> dict[str, Any]:
    """在全库上统计 IDF，评分时再按专业过滤。

    为什么用全库：单个专业只有 45 条考点，像「系统」「性能」这类词在 45 条里
    出现十几次，按专业统计会把它们当成关键词；放到 270 条语料上它们的 IDF
    才接近真实稀有度。
    """
    key = id(rubrics)
    cached = _INDEX_CACHE.get(key)
    if cached is not None:
        return cached

    grams: dict[tuple[str, str], set[str]] = {}
    en: dict[tuple[str, str], set[str]] = {}
    for major, topics in rubrics.items():
        for topic, entry in topics.items():
            text = _doc_text(topic, entry)
            grams[(major, topic)] = _cn_ngrams(text)
            en[(major, topic)] = set(_en_tokens(text))

    df = Counter(gram for grams_set in grams.values() for gram in grams_set)
    en_df = Counter(token for tokens in en.values() for token in tokens)
    index = {
        "grams": grams,
        "en": en,
        "df": df,
        "en_df": en_df,
        "n": max(len(grams), 1),
    }
    _INDEX_CACHE.clear()  # 只保留最近一份语料；id 复用时不至于串库
    _INDEX_CACHE[key] = index
    return index


def dimension(major: str, topic: str) -> str:
    """考点的能力维度；关键词表没覆盖到时返回空串。"""
    for name, pattern in _DIMENSION_PATTERNS.get(major, []):
        if pattern.search(topic):
            return name
    return ""


def _evidence(jd: str, hits: set[str], en_tokens: set[str]) -> list[str]:
    """从 JD 里取出命中词所在的短句，作为「为什么认为它相关」的依据。

    直接展示 n-gram 会冒出「务隔离级」这类碎片；展示整句又太长。
    折中：按标点切句，只取包含命中词的那几句，超过 28 字的截断。
    句式也顺带暴露误报——如果命中依据本身不相关，用户一眼就能看出来。

    优先只取包含 3 字以上命中词或英文 token 的句子：考点入选本来就要求一个
    「够具体」的命中，2 字泛词命中（服务/设计/开发）的句子做依据只会添噪声。
    """
    clauses = [part.strip() for part in _CLAUSE.split(jd) if part.strip()]
    strong = {gram for gram in hits if len(gram) >= 3}
    # 没有 3 字以上命中时，宁可只展示英文 token 所在句，也不拿 2 字泛词凑依据：
    # 那会把「负责后端服务的设计与开发」这种句子挂到 TCP 题下面。
    found: list[str] = []
    for use_hits in ([strong] if strong else [set()]):
        for clause in clauses:
            lowered = clause.lower()
            if not (
                any(gram in clause for gram in use_hits)
                or any(token in lowered for token in en_tokens)
            ):
                continue
            text = clause if len(clause) <= 28 else clause[:28] + "…"
            if text not in found:
                found.append(text)
            if len(found) >= MAX_EVIDENCE:
                return found
        if found:
            break
    return found


def _covered_compound_part(text: str, token: str, covered: set[str]) -> bool:
    """TCP/IP 拆出来的 IP 不算独立缺口：斜杠组合里只要有一半在库里就算覆盖。"""
    for compound in _EN_COMPOUND.findall(text):
        parts = [part.lower() for part in compound.split("/")]
        if token in parts and any(part in covered for part in parts):
            return True
    return False


def _bridge_hints(
    rubrics: dict[str, Any],
    index: dict[str, Any],
    major: str,
    uncovered: list[tuple[str, str]],
) -> list[dict[str, str]]:
    """库外英文词在别的专业库里有对应考点时，给一条跨库对照提示。

    只做提示、不算命中：本专业面试不会因为别的专业有考点就换库，但
    「Redis 没覆盖」和「Redis 在软件工程库里有考点」对用户是两种信息量。
    """
    hints: list[dict[str, str]] = []
    for key, raw in uncovered:
        per_token = 0
        for other_major, topics in (rubrics or {}).items():
            if other_major == major:
                continue
            for topic in topics:
                if key not in index["en"].get((other_major, topic), set()):
                    continue
                hints.append({"token": raw, "major": other_major, "topic": topic})
                per_token += 1
                break  # 每个专业只给一条代表，避免把一个词铺满屏
            if per_token >= 2:
                break
        if len(hints) >= 6:
            break
    return hints


def mastery(records: list[dict[str, Any]] | None, major: str) -> dict[str, dict[str, Any]]:
    """按考点聚合历史练习情况。

    只统计**真正问到过**的考点；`missed == ["本场未问到"]` 是占位行，跳过，
    否则 `topic_verdict` 会把它判成"一条要点都没答到"。
    """
    stats: dict[str, dict[str, Any]] = {}
    for record in records or []:
        profile = record.get("profile") or {}
        if profile.get("major") != major:
            continue
        evaluation = record.get("evaluation") or {}
        for row in evaluation.get("rubric_check") or []:
            if not isinstance(row, dict):
                continue
            topic = str(row.get("topic") or "").strip()
            missed = [str(item).strip() for item in (row.get("missed") or [])]
            if not topic or missed == [UNASKED]:
                continue
            rubric = knowledge.rubric_for_topic({"major": major}, topic)
            if not isinstance(rubric, dict):
                continue
            veto = rubric.get("veto")
            verdict, _ = knowledge.topic_verdict(
                row, veto if isinstance(veto, list) else None
            )
            if verdict == knowledge.VERDICT_UNKNOWN:
                continue
            entry = stats.setdefault(
                topic, {"pass": 0, "partial": 0, "fail": 0, "latest": "", "time": ""}
            )
            key = {
                knowledge.VERDICT_PASS: "pass",
                knowledge.VERDICT_PARTIAL: "partial",
                knowledge.VERDICT_FAIL: "fail",
            }.get(verdict)
            if key is None:
                continue
            entry[key] += 1
            when = str(record.get("time") or "")
            if when >= entry["time"]:
                entry["time"] = when
                entry["latest"] = key
    return stats


def _empty_result(major: str) -> dict[str, Any]:
    return {
        "major": major,
        "topics": [],
        "dimensions": [],
        "library_size": 0,
        "uncovered": [],
        "bridges": [],
        "practiced": 0,
        "unpracticed": 0,
        "practiced_total": 0,
        "passed_total": 0,
    }


def analyze_jd(
    jd_text: str,
    major: str,
    rubrics: dict[str, Any] | None = None,
    records: list[dict[str, Any]] | None = None,
    top_n: int = MAX_TOPICS,
) -> dict[str, Any]:
    """主入口：JD 文本 + 专业 → 命中的考点、维度分布、练习情况。

    返回结构（UI 直接用）::

        {"major", "topics": [{"topic","dimension","level","score","evidence",
                              "attempts","pass","partial","fail","latest"}],
         "dimensions": [{"name","topics":[考点名…]}],
         "library_size", "uncovered":[JD 里考点库没有的英文词],
         "bridges":[{"token","major","topic"} 其他专业库的对照提示],
         "practiced", "unpracticed",
         "practiced_total", "passed_total"}
    """
    rubrics = rubrics if rubrics is not None else knowledge.load_rubrics()
    majors = set(rubrics or {})
    if not (jd_text or "").strip() or major not in majors:
        return _empty_result(major)

    index = _build_index(rubrics)
    df, en_df, total = index["df"], index["en_df"], index["n"]
    query_grams = _cn_ngrams(jd_text)
    query_en = _en_tokens(jd_text)
    if not query_grams and not query_en:
        return _empty_result(major)

    library = rubrics.get(major) or {}
    all_en = set()
    for topic in library:
        all_en |= index["en"].get((major, topic), set())

    scored: list[tuple[float, str, list[str], list[str]]] = []
    for topic in library:
        doc_grams = index["grams"].get((major, topic), set())
        gram_hits = [
            (gram, math.log((total + 1) / (df[gram] + 1)))
            for gram in query_grams & doc_grams
            if df.get(gram, total) <= max(1, int(total * 0.15))
        ]
        doc_en = index["en"].get((major, topic), set())
        en_hits = [
            (token, math.log((total + 1) / (en_df[token] + 1)))
            for token in query_en.keys() & doc_en
        ]
        score = sum(idf for _, idf in gram_hits + en_hits)
        best = max((idf for _, idf in gram_hits + en_hits), default=0.0)
        # 入选还要求至少有一个 3 字以上的中文命中词或英文 token：
        # 「事务/优化/协议/系统/缓存」这类 2 字泛词很容易撞上不相干考点（MESI 撞「协议」）。
        specific = any(len(gram) >= 3 for gram, _ in gram_hits) or bool(en_hits)
        if score >= MIN_SCORE and best >= MIN_BEST_IDF and specific:
            scored.append(
                (
                    score,
                    topic,
                    [gram for gram, _ in gram_hits],
                    [token for token, _ in en_hits],
                )
            )

    scored.sort(key=lambda item: item[0], reverse=True)
    stats = mastery(records, major)

    topics: list[dict[str, Any]] = []
    used_per_dimension: Counter[str] = Counter()
    dimensions: dict[str, list[str]] = {}
    for score, topic, hit_grams, hit_en in scored:
        if len(topics) >= top_n:
            break
        dim = dimension(major, topic)
        if used_per_dimension[dim] >= MAX_PER_DIMENSION:
            continue
        used_per_dimension[dim] += 1
        dimensions.setdefault(dim, []).append(topic)
        entry = library.get(topic) or {}
        evidence = _evidence(jd_text, set(hit_grams), set(hit_en))
        stat = stats.get(topic) or {}
        topics.append(
            {
                "topic": topic,
                "dimension": dim,
                "level": str(entry.get("level") or ""),
                "score": round(score, 2),
                "evidence": evidence[:MAX_EVIDENCE],
                "attempts": sum(stat.get(key, 0) for key in ("pass", "partial", "fail")),
                "pass": stat.get("pass", 0),
                "partial": stat.get("partial", 0),
                "fail": stat.get("fail", 0),
                "latest": stat.get("latest", ""),
            }
        )

    uncovered_pairs = [
        (key, raw)
        for key, raw in query_en.items()
        if key not in all_en and not _covered_compound_part(jd_text, key, all_en)
    ][:12]
    uncovered = [raw for _, raw in uncovered_pairs]
    bridges = _bridge_hints(rubrics, index, major, uncovered_pairs)
    practiced = sum(1 for item in topics if item["attempts"] > 0)
    practiced_total = len(stats)
    passed_total = sum(1 for stat in stats.values() if stat.get("latest") == "pass")
    return {
        "major": major,
        "topics": topics,
        "dimensions": [
            {"name": name, "topics": names} for name, names in dimensions.items()
        ],
        "library_size": len(library),
        "uncovered": uncovered,
        "bridges": bridges,
        "practiced": practiced,
        "unpracticed": len(topics) - practiced,
        "practiced_total": practiced_total,
        "passed_total": passed_total,
    }
