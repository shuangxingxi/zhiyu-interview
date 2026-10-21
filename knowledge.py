"""场景 × 专业 × 难度 三级策略库 —— 面试官与评估官共用的"能力口径"。

设计说明（对应 DESIGN.md 第三、七节）：
    第一版故意不上向量库。数据量小、字段可直接命中，字典查表就够用，
    引入 Chroma / 向量检索在这里是纯粹的复杂度开销。

对外主要暴露：
    SCENES / MAJORS / LEVELS            —— 三级参数的可选值
    build_profile(...)                  —— 把三级参数编译成一份面试官配置
    profile_summary(profile)            —— 给界面用的一行摘要
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any

import resume_rag

SCENES = ["考研复试", "应届生校招", "日常实习", "转正就职"]
MAJORS = [
    "计算机科学与技术",
    "人工智能",
    "软件工程",
    "电子信息",
    "自动化",
    "通信工程",
]
LEVELS = ["A", "B", "C"]

# 面试官的节奏控制（对应 DESIGN.md 第五节）
# 追问上限不写死在决策代码里，而是随模式一起编译进配置：
#   严格模式 —— 每场节奏一致，录演示视频时可控
#   真人模式 —— 上限只当"保险丝"，正常情况让模型自己判断该不该继续深挖
MAX_PROBES_STRICT = 2
MAX_PROBES_HUMAN = 3
# 连续几次"不提问回合"（复述确认 / 澄清 / 简短反馈）之后必须重新提问
MAX_REFLECTS_IN_A_ROW = 2

# ---------------------------------------------------------------------------
# 一级 · 场景：决定考察重心
# ---------------------------------------------------------------------------
SCENE_FOCUS: dict[str, str] = {
    "考研复试": "专业基础、科研潜力、毕设与项目经历",
    "应届生校招": "项目经历、基础知识、成长潜力",
    "日常实习": "基础扎实度、学习能力、可投入度",
    "转正就职": "业务理解、协作推进、结果交付",
}

# ---------------------------------------------------------------------------
# 三级 · 难度：决定提问深度、追问强度、评分严格度
# ---------------------------------------------------------------------------
LEVEL_STYLE: dict[str, dict[str, str]] = {
    "A": {
        "depth": "底层原理、推导过程、跨知识点拓展",
        "probe_style": "连续深挖，直到答到原理层",
        "strictness": "严格，对「大概」「应该」这类模糊表述不放过",
    },
    "B": {
        "depth": "核心考点 + 少量拓展",
        "probe_style": "在关键处追问 1-2 次",
        "strictness": "标准要求，答对主干即可",
    },
    "C": {
        "depth": "教材概念与应用实操",
        "probe_style": "追问从简，问到概念层即可",
        "strictness": "对应届生放宽，以鼓励和纠错为主",
    },
}

# ---------------------------------------------------------------------------
# 情绪：决定"用什么态度问"，与难度正交的第二条轴
#
# 难度管「问什么、问多深」，情绪管「用什么态度问」。
# 之所以要做这条轴，是因为真实面试里遇到冷脸、赶时间、反复质疑的面试官是常态，
# 只练"完美面试官"的候选人一遇到就崩。
#
# 每种模式定义三样东西：
#     style              —— 注入面试官提示词的行为描述
#     patience / energy  —— 开局状态（mood.py 会在此基础上按对话演变）
#     confirm            —— 是否需要用户先勾选知情同意才能开始
# ---------------------------------------------------------------------------
MOOD_RANDOM = "随机"
MOOD_FRIENDLY = "友善型"

# 随机模式的开局抽取池
MOOD_POOL = ["友善型", "疲惫型", "怀疑型", "冷淡型", "压力型"]

MOOD_MODES: dict[str, dict[str, Any]] = {
    "随机": {
        "style": "",  # 开局时从 MOOD_POOL 抽定一个，再把它的 style 填进来
        "patience": 2,
        "energy": 2,
        "confirm": False,
    },
    "友善型": {
        "style": "态度温和，愿意听候选人把话说完；答得不错时明确给一句正向反馈，卡住时给一点提示方向。",
        "patience": 3,
        "energy": 2,
        "confirm": False,
    },
    "疲惫型": {
        "style": "你已经连着面试了一整天，只想快点把流程走完：回复短、少评价、不寒暄，候选人说得啰嗦就收口打断。",
        "patience": 1,
        "energy": 1,
        "confirm": True,
    },
    "怀疑型": {
        "style": "你对候选人给出的结论天然持保留态度，凡是没有证据支撑的说法都要追问依据：凭什么这么判断、数据从哪来。",
        "patience": 2,
        "energy": 2,
        "confirm": True,
    },
    "冷淡型": {
        "style": "你几乎不给情绪反馈：不评价、不鼓励、不引导，问完问题就停，让候选人自己往下说。",
        "patience": 1,
        "energy": 0,
        "confirm": True,
    },
    "压力型": {
        "style": "你刻意制造压力：对结论主动挑刺、抢话、压缩候选人的思考时间，逼他把理由讲清楚。",
        "patience": 0,
        "energy": 2,
        "confirm": True,
    },
}

# 下拉顺序：随机放最前，其余按强度递增
MOOD_ORDER = ["随机", "友善型", "疲惫型", "怀疑型", "冷淡型", "压力型"]

# 难度档在两类场景下的真实含义
_LEVEL_LABELS: dict[str, dict[str, str]] = {
    "考研复试": {"A": "985 高校", "B": "211 高校", "C": "双非本科"},
    "求职": {"A": "一线大厂", "B": "中型互联网 / 上市公司", "C": "中小企业"},
}


def level_labels(scene: str) -> dict[str, str]:
    """难度档在不同场景下的叫法。考研看院校，求职看企业梯度。"""
    return _LEVEL_LABELS["考研复试" if scene == "考研复试" else "求职"]


# ---------------------------------------------------------------------------
# 二级 · 专业：决定能力维度与高频考点
# 考点按 A / B / C 三档分层，这样"难度"才真正影响出题，而不是一句空话
# ---------------------------------------------------------------------------
MAJOR_MODEL: dict[str, dict[str, Any]] = {
    "计算机科学与技术": {
        "dimensions": [
            "编程基础与数据结构",
            "操作系统与计算机网络",
            "数据库与存储设计",
            "工程实践与调试能力",
            "沟通与表达",
        ],
        "topics": {
            "A": [
                "红黑树的性质与旋转调整过程",
                "动态规划状态设计与最优子结构推导",
                "哈希表冲突解决与渐进式 rehash 机制",
                "进程调度算法与上下文切换开销",
                "TCP 拥塞控制算法的演进与取舍",
                "缓存一致性协议（MESI）",
                "B+ 树与 LSM 树的适用场景对比",
                "分布式一致性（Raft）与故障恢复",
                "事务隔离级别与 MVCC 的实现原理",
                "Redis 缓存设计与持久化取舍",
                "线上 CPU 飙高的排查思路与工具",
                "内存泄漏的定位方法与工具体系",
                "分布式链路追踪与故障根因定位",
                "技术方案评审中的结构化表达",
                "跨团队技术分歧的沟通与决策",
                "向非技术人员讲清复杂技术方案",
                "从输入 URL 到页面响应的全链路",
            ],
            "B": [
                "二叉树的遍历方式与实现",
                "图的存储结构与遍历方式",
                "递归调用与栈的关系",
                "进程与线程的区别及各自开销",
                "TCP 三次握手为什么不是两次",
                "HTTP 与 HTTPS 的加密过程",
                "索引原理与索引失效的常见场景",
                "SQL 慢查询的定位与优化思路",
                "数据库主从复制与读写分离",
                "InnoDB 存储引擎与事务日志",
                "死锁产生的四个必要条件",
                "断点调试与单元测试的实践",
                "Git 版本控制与分支协作流程",
                "项目经历的 STAR 结构化讲述",
                "技术方案的书面文档表达",
                "面试追问下的澄清与应对",
                "线程池的核心参数与拒绝策略",
                "登录态鉴权：JWT 与 Session 的取舍",
                "Spring Boot 自动配置与组件职责划分",
                "数据库变更订阅（Canal）与缓存一致性",
            ],
            "C": [
                "数组与链表的区别及适用场景",
                "栈和队列的典型应用",
                "常见排序的时间复杂度",
                "进程的状态与状态转换",
                "TCP 与 UDP 的区别",
                "HTTP 常见请求方法与状态码",
                "SQL 增删改查与简单连接查询",
                "数据库范式的概念与应用",
                "事务的 ACID 特性",
                "面向对象三大特性",
                "异常处理与常见报错的排查",
                "代码规范与命名约定",
                "自我介绍的结构与重点",
                "项目经历的清晰描述",
                "与面试官有效沟通与提问",
                "项目复盘：失败、返工与经验提炼",
            ],
        },
    },
    "人工智能": {
        "dimensions": [
            "数学与算法基础",
            "模型原理与推导",
            "数据处理与特征工程",
            "实验设计与结果分析",
            "沟通与表达",
        ],
        "topics": {
            "A": [
                "模型误差的偏差方差分解",
                "过拟合与正则化的数学解释",
                "最大似然估计与最大后验估计的联系",
                "反向传播的完整推导",
                "Attention 的计算复杂度与优化思路",
                "预训练 - 微调范式的适用边界",
                "RAG 检索增强生成的流程与失败模式",
                "Agent 工具调用与规划控制",
                "LoRA 与参数高效微调",
                "Prefill / Decode 两阶段与推理优化",
                "大模型幻觉的成因与评测方法",
                "特征选择与降维的本质区别",
                "类别不平衡的采样与代价敏感学习",
                "数据分布偏移的识别与校正",
                "嵌套交叉验证与模型选择偏差",
                "模型对比的显著性检验与置信区间",
                "消融实验的设计原则与因果归因",
                "向非技术受众解释模型的能力边界",
                "技术选型汇报中的权衡论证",
                "论文答辩中实验结论的严谨表述",
            ],
            "B": [
                "梯度下降与学习率的影响",
                "梯度消失与梯度爆炸的成因与缓解",
                "距离度量与相似度计算的选择",
                "常见损失函数的适用场景",
                "卷积的感受野与池化的作用",
                "批归一化的原理与作用",
                "缺失值与异常值的处理策略",
                "特征缩放与归一化的作用",
                "类别特征的编码方式（One-Hot / 目标编码）",
                "过拟合的表现与应对手段",
                "评价指标的选择（准确率 / 召回 / AUC）",
                "A/B 测试与离线评估的差异",
                "向产品经理解释模型指标与业务目标的映射",
                "技术评审中如何陈述实验证据",
                "跨团队协作中的模型接口与文档表达",
            ],
            "C": [
                "向量、矩阵与张量的基本概念",
                "概率、条件概率与贝叶斯公式的直观理解",
                "平均值、方差与标准差在数据分析中的作用",
                "监督学习与无监督学习的区别",
                "常见激活函数与其作用",
                "线性回归与逻辑回归的区别",
                "数据清洗的基本步骤",
                "特征工程的基本方法（特征构造与选择）",
                "文本数据的简单表示（词袋与 TF-IDF）",
                "训练集 / 验证集 / 测试集的划分",
                "混淆矩阵各字段的含义",
                "调参的基本流程",
                "向非技术同事解释模型预测结果",
                "用 STAR 法则描述项目经历",
                "撰写实验结论与复现说明",
                "项目复盘：失败、返工与经验提炼",
            ],
        },
    },
    "软件工程": {
        "dimensions": [
            "编程与设计能力",
            "架构与系统设计",
            "工程流程与协作",
            "测试与质量意识",
            "沟通与表达",
        ],
        "topics": {
            "A": [
                "领域驱动设计的适用争议",
                "并发编程中的内存模型与可见性",
                "代码坏味道与重构手法",
                "微服务拆分的边界依据",
                "CAP 理论与最终一致性的落地",
                "高并发下的限流与降级设计",
                "持续集成与持续交付流水线设计",
                "大型项目的依赖治理与模块化",
                "技术债的识别与偿还策略",
                "面向可测性的代码设计",
                "分布式系统的混沌工程实践",
                "契约测试与集成测试的分层策略",
                "技术方案评审中的权衡表达",
                "跨团队技术对齐与决策记录 (ADR)",
                "面向不同受众的技术沟通",
            ],
            "B": [
                "SOLID 原则在项目中的体现",
                "常用设计模式的适用场景",
                "异常处理与错误码设计",
                "接口的幂等设计",
                "缓存与数据库一致性问题",
                "消息队列的可靠投递与消费",
                "Git 分支策略与协作流程",
                "代码评审应该关注什么",
                "需求评审与排期估算",
                "单元测试的编写与测试替身",
                "接口测试与自动化回归",
                "日志、监控与线上问题定位",
                "技术文档的编写规范",
                "跨部门协作中的需求澄清",
                "故障复盘与对外沟通",
            ],
            "C": [
                "常见 UML 图及其用途",
                "面向对象三大特性与封装",
                "代码注释与命名规范",
                "三层架构与 MVC 模式",
                "前后端分离与 RESTful 接口设计",
                "数据库索引与慢查询优化入门",
                "版本控制的基础操作",
                "敏捷开发的基本流程",
                "缺陷管理与 Bug 生命周期",
                "测试用例的设计方法",
                "软件测试的基本分类与流程",
                "代码静态检查与代码规范工具",
                "需求分析的基本流程",
                "用户故事与验收标准",
                "技术方案汇报与提问应对",
                "项目复盘：失败、返工与经验提炼",
            ],
        },
    },
    "电子信息": {
        "dimensions": [
            "电路与器件基础",
            "信号与系统",
            "硬件调试与测量",
            "系统集成能力",
            "沟通与表达",
        ],
        "topics": {
            "A": [
                "运算放大器的频率补偿",
                "MOSFET 的小信号等效模型与跨导",
                "负反馈对放大器增益、带宽与输入输出阻抗的影响",
                "奈奎斯特采样定理与信号重构",
                "数字滤波器设计的取舍",
                "锁相环的工作原理",
                "S 参数与矢量网络分析仪测量",
                "眼图分析与信号完整性评估",
                "时域反射（TDR）与传输线阻抗测量",
                "电磁兼容与干扰抑制设计",
                "高速电路的电源完整性与目标阻抗设计",
                "系统级时钟树与抖动预算设计",
                "复杂技术方案的逻辑化表达与答辩",
                "与架构/算法团队的技术方案对齐",
                "技术评审中的方案论证与风险陈述",
            ],
            "B": [
                "三极管放大电路的静态工作点",
                "运算放大器的虚短虚断与基本运算电路",
                "MOS 管的开关特性与驱动电路",
                "RC 滤波器截止频率的计算",
                "ADC 采样精度与量化误差",
                "AM/FM 调制与解调的基本原理",
                "逻辑分析仪与串行协议解码的基本使用",
                "电源纹波与噪声的测量方法",
                "频率响应与波特图的测量方法",
                "PCB 布局布线的基本规则",
                "差分信号抗干扰的原理",
                "电源方案选型与 LDO/DC-DC 的取舍",
                "项目经验的 STAR 结构化讲述",
                "面向客户的技术需求沟通与确认",
                "调试问题的跨部门协同与信息同步",
            ],
            "C": [
                "欧姆定律与基尔霍夫定律",
                "常见元器件的作用与识别",
                "电容的充放电过程与 RC 时间常数",
                "串口通信的基础概念",
                "I2C 与 SPI 总线的基本概念",
                "模拟信号与数字信号的区别及模数转换的意义",
                "示波器与万用表的基本使用",
                "直流稳压电源与信号发生器的基本使用",
                "焊接与面包板搭接的基本操作",
                "电源与地的概念",
                "电路原理图与 PCB 图的识读",
                "常见电子模块的选型与联调",
                "实习/课程项目的清晰描述",
                "向非技术人员解释电路与产品功能",
                "技术文档与测试记录的规范书写",
                "项目复盘：失败、返工与经验提炼",
            ],
        },
    },
    "自动化": {
        "dimensions": [
            "控制理论基础",
            "数学与建模能力",
            "传感器与执行机构",
            "系统调试能力",
            "沟通与表达",
        ],
        "topics": {
            "A": [
                "状态空间模型与能控能观性",
                "李雅普诺夫稳定性分析",
                "PID 参数整定的频域方法",
                "卡尔曼滤波的推导思路",
                "非线性系统的线性化处理",
                "传递函数与状态空间的相互转换",
                "传感器动态特性与标定模型",
                "电机伺服系统的机电建模与饱和特性",
                "多传感器信息融合与状态估计",
                "闭环带宽与相位裕度的现场调试方法",
                "伺服系统机械谐振的辨识与抑制",
                "数字控制实现中的采样延迟与补偿",
                "控制算法方案的技术答辩与质疑回应",
                "控制系统设计说明书与接口协议撰写",
                "跨专业控制需求的沟通与协调",
            ],
            "B": [
                "PID 三项各自的作用",
                "开环控制与闭环控制的区别",
                "系统稳定性的判据",
                "传递函数与方块图化简",
                "一阶与二阶系统的时域响应分析",
                "频率特性与 Bode 图分析",
                "传感器噪声与滤波处理",
                "电机驱动与控制环路设计",
                "增量式编码器与测速反馈处理",
                "控制系统的阶跃响应调试",
                "PID 参数的工程整定方法",
                "干扰源定位与抗干扰措施",
                "控制方案的可行性与风险评估汇报",
                "电气原理图与控制流程的讲解",
                "需求确认与验收指标的沟通",
            ],
            "C": [
                "反馈控制的基本概念",
                "误差与稳态误差",
                "控制系统的基本组成与分类",
                "拉普拉斯变换与传递函数入门",
                "控制系统的微分方程建模",
                "方块图与信号流图基础",
                "常见传感器的类型与选型",
                "继电器与接触器的使用",
                "电机与变频器的基础应用",
                "PLC 梯形图基础",
                "PLC 的接线与输入输出调试",
                "常见电气故障排查方法",
                "电气接线图与元件标识的识读",
                "设备操作说明书与作业指导书编写",
                "现场调试问题的复述与汇报",
                "项目复盘：失败、返工与经验提炼",
            ],
        },
    },
    "通信工程": {
        "dimensions": [
            "通信原理基础",
            "信号处理能力",
            "系统与协议理解",
            "仿真与实验能力",
            "沟通与表达",
        ],
        "topics": {
            "A": [
                "香农公式的工程含义",
                "OFDM 原理与峰均比问题",
                "MIMO 的信道容量分析",
                "信道编码（LDPC / Turbo）",
                "多径衰落与均衡技术",
                "匹配滤波器与最佳接收",
                "多址接入技术（FDMA/TDMA/CDMA/OFDMA）",
                "自适应调制编码（AMC）与链路自适应",
                "5G NR 的帧结构与参数集（Numerology）",
                "通信链路级仿真方法",
                "蒙特卡洛仿真与误码率曲线评估",
                "多径信道建模与仿真",
                "链路预算与系统容量论证",
                "算法复杂度与性能折衷的技术表述",
                "关键技术选型的跨方案对比与辩护",
            ],
            "B": [
                "常见调制解调方式",
                "带宽与传输速率的关系",
                "信噪比与误码率的关系",
                "抽样定理与脉冲编码调制（PCM）",
                "成形滤波与眼图",
                "扩频与解扩（DSSS / FHSS）",
                "复用技术（FDM / TDM）",
                "天线增益与方向图",
                "交换技术（电路交换 / 分组交换）",
                "MATLAB 通信系统仿真基础",
                "星座图观测与判决区域分析",
                "频谱仪与信号源的使用",
                "通信指标（速率 / 时延 / 误码率）的汇报表达",
                "通信方案选型的对比说明",
                "结合应用场景讲解通信技术",
            ],
            "C": [
                "通信系统的基本组成",
                "模拟信号与数字信号的区别",
                "频率与波长的关系",
                "基带与频带的概念",
                "采样与量化的基本概念",
                "常用滤波器（低通 / 高通 / 带通）的作用",
                "单工 / 半双工 / 全双工",
                "信道的基本概念与常用信道",
                "编码与解码的基本概念",
                "常用通信仪器（示波器 / 频谱仪）的认识",
                "用 MATLAB 绘制基本信号波形",
                "实验数据的记录与图表呈现",
                "通信术语的准确使用",
                "结合生活实例讲解通信概念",
                "实验现象的口头描述与总结",
                "项目复盘：失败、返工与经验提炼",
            ],
        },
    },
}

# 行为面：不按专业分，统一考 STAR
BEHAVIOR_DIMENSIONS = [
    "情境描述清晰度（S/T）",
    "个人行动与贡献（A）",
    "结果与量化（R）",
    "反思与成长",
    "表达结构与感染力",
]
BEHAVIOR_TOPICS = [
    "一次你推动团队完成目标的经历",
    "一次你与他人产生分歧并解决的经历",
    "一次在时间压力下赶进度的经历",
    "一次你主动发现并解决问题的经历",
    "一次失败或受挫的经历",
    "一次你快速学会新东西并用上的经历",
    "一次你说服他人接受你方案的经历",
    "一次你承担超出职责范围任务的经历",
    "一次你面对信息不全仍要做决策的经历",
    "一次你被别人指出错误并改正的经历",
    "一次你协调多方资源推进事情的经历",
    "一次你坚持原则拒绝不合理要求的经历",
    "一次你把一件小事做到极致带来改变的经历",
    "一次你帮助同事或同学成长进步的经历",
    "一次你对结果不满意并复盘改进的经历",
]

# 无简历时用来"挖经历"的通用方向：不属于任何专业，任何专业都能用。
# 候选人不上传简历时，面试官若只念专业考点就会显得很干；有了这组方向，
# 就能沿着候选人自己写下的岗位目标 / 项目经历 / 技能栈把经历问深。
PROJECT_DEEP_DIVE = [
    "这段经历要解决的核心问题与目标",
    "你本人在其中的具体分工与贡献边界",
    "关键技术 / 方案选型的依据与取舍",
    "过程中最难的一个坎，以及你是怎么排查解决的",
    "可量化的结果，以及你是怎么验证的",
    "如果重新做一遍，你会怎么改进",
]


def build_profile(
    scene: str,
    major: str,
    level: str,
    interview_type: str = "技术面",
    total_rounds: int = 5,
    candidate_bg: str = "",
    resume: str = "",
    strict: bool = False,
    mood: str = MOOD_RANDOM,
    avoid_topics: list[str] | None = None,
    use_real: bool = True,
    real_quota: int = 2,
) -> dict[str, Any]:
    """把「场景 × 专业 × 难度」三级参数 + 情绪模式编译成一份面试官配置。

    这份配置同时也是评估官的评分口径来源，保证"问的"和"评的"对得上。
    strict 只影响面试官的节奏（追问上限、是否允许非提问回合），不影响评分口径。

    use_real / real_quota 控制"真实企业真题"这条素材支线：每场最多抽 real_quota
    道真题作为主问题素材（配额混合），其余主问题仍锚定上面的考点参考。
    真题只是素材、不替代考点，也不参与 rubrics.json 的评分参照体系。

    mood 是与难度正交的第二条轴：难度定"问什么"，情绪定"用什么态度问"。
    strict 追求每场节奏一致，与情绪漂移矛盾，所以严格模式下强制锁定友善型。
    选了"随机"就当场从 MOOD_POOL 抽定一个并记进配置 —— 抽定之后整场不再变，
    这样面试过程中的状态轨迹才是连续的，归因报告才有依据。
    """
    style = LEVEL_STYLE.get(level, LEVEL_STYLE["B"])
    label = level_labels(scene).get(level, level)
    is_behavior = interview_type == "行为面"

    # 情绪模式：请求值 → 实际生效值
    requested = mood if mood in MOOD_MODES else MOOD_RANDOM
    if strict:
        requested = MOOD_FRIENDLY
    resolved = random.choice(MOOD_POOL) if requested == MOOD_RANDOM else requested
    mood_cfg = MOOD_MODES[resolved]

    if is_behavior:
        dimensions = list(BEHAVIOR_DIMENSIONS)
        topics = list(BEHAVIOR_TOPICS)
    else:
        model = MAJOR_MODEL.get(major) or MAJOR_MODEL["计算机科学与技术"]
        dimensions = list(model["dimensions"])
        # 按难度取对应层级的考点；A 档额外带入 B 档，避免问得太悬空
        topics = list(model["topics"].get(level, model["topics"]["B"]))
        if level == "A":
            topics += model["topics"]["B"][:2]

    profile: dict[str, Any] = {
        "scene": scene,
        "major": major,
        "level": level,
        "level_label": label,
        "interview_type": interview_type,
        "total_rounds": total_rounds,
        "candidate_bg": candidate_bg.strip() or "（候选人未填写背景，请通过提问自行了解）",
        "resume": resume.strip(),
        # 简历切片索引（resume_rag 编译）：面试官按当前话题聚焦注入、评估官用来核对一致性。
        # 切片失败时是空列表，注入逻辑会自动回落到全文，行为与 v3 一致。
        # 与 resume 一样只存在于内存，落盘时必须一并剔除。
        "resume_chunks": resume_rag.build(resume),
        "focus": SCENE_FOCUS.get(scene, ""),
        "depth": style["depth"],
        "probe_style": style["probe_style"],
        "strictness": style["strictness"],
        "dimensions": dimensions,
        "topics": topics,
        # 没有简历时，用通用「经历深挖方向」补足取材：让面试官仍能围绕候选人
        # 自述的岗位目标 / 项目经历 / 技能栈把经历问深，而不是干念专业考点。
        "deep_dive": [] if is_behavior or resume.strip() else list(PROJECT_DEEP_DIVE),
        # 开新场时从最近几场练习带过来的考点，交给面试官去重（本场已问的在 app 侧累积）
        "avoid_topics": [str(t).strip() for t in (avoid_topics or []) if str(t).strip()],
        "strict": bool(strict),
        "max_probes": MAX_PROBES_STRICT if strict else MAX_PROBES_HUMAN,
        "allow_reflect": not strict,
        # 情绪这条轴：请求的模式、实际生效的模式、以及开局状态
        "mood_requested": requested,
        "mood_mode": resolved,
        "mood_style": mood_cfg["style"],
        # 要不要弹知情同意，看的是用户"选了什么"，而不是抽到了什么：
        # 选「随机」就意味着接受随机抽取的任意一种模式
        "mood_confirm": bool(MOOD_MODES[requested]["confirm"]),
        "mood_init": {
            "patience": int(mood_cfg["patience"]),
            "energy": int(mood_cfg["energy"]),
            "pressure": 0,
        },
    }

    # 真实企业真题：编译期一次性抽定，整场不再变；无数据 / 关闭 / 配额 0 时为空。
    # 抽定而不是逐轮抽样，是为了让"本场用了几道真题"可复算，也便于报告里标注。
    profile["real_quota"] = max(0, int(real_quota)) if use_real else 0
    profile["use_real"] = bool(use_real)
    profile["real_questions"] = _sample_real_questions(profile, profile["real_quota"])
    profile["real_q_topics"] = [
        str(item.get("topic", "")).strip()
        for item in profile["real_questions"]
        if str(item.get("topic", "")).strip()
    ]
    return profile


def reference_for(profile: dict[str, Any]) -> str:
    """给面试官看的考点参考（严禁照抄照念）。"""
    return "\n".join(f"- {t}" for t in profile.get("topics", []))


_RESUME_HEAD = (
    "【候选人上传的简历】\n"
    "这份简历只用于取材提问，不要复述、不要罗列、不要念出来。\n"
    "主问题必须优先针对简历里的具体条目（实习经历、毕业设计、获奖情况、项目经历）来问，"
    "并且要点出条目名称，比如「你简历里写的那个 XX 项目」；泛泛而问不算合格。\n"
)


def resume_block(profile: dict[str, Any], query: str = "") -> str:
    """把简历整理成给面试官看的段落；候选人没上传简历时返回空串。

    RAG 版：先给一份「条目索引」（全场可见，用来挑还没问到的条目），
    再按 query（当前话题 / 候选人上一句）检索出最相关的条目原文。
    query 为空（开场）时给全部条目——开场先看全貌，后面才逐轮聚焦。
    切片或检索不可用时回落到全文注入，与 v3 行为一致。
    """
    text = (profile.get("resume") or "").strip()
    if not text:
        return ""

    chunks = profile.get("resume_chunks") or []
    if not chunks:
        return _RESUME_HEAD + text

    hits = resume_rag.top(profile, query, resume_rag.TOP_K_INTERVIEW) if query.strip() else chunks
    if not hits:  # 检索没命中（话题与简历无关）→ 只留索引，别硬塞不相干的条目
        return _RESUME_HEAD + resume_rag.index_block(chunks)
    return (
        _RESUME_HEAD
        + resume_rag.index_block(chunks)
        + "\n\n"
        + resume_rag.detail_block(hits)
    )


def resume_evidence_block(profile: dict[str, Any], query: str = "") -> str:
    """给评估官的简历取样：与本次讨论相关的原文 + 一致性核查要求。

    没有简历时返回空串；切片不可用时回落全文。核查要求写在段落里，
    这样评估官拿到的是"简历原文 + 该怎么用"，而不是被凭空要求回忆简历内容。
    """
    text = (profile.get("resume") or "").strip()
    if not text:
        return ""

    chunks = profile.get("resume_chunks") or []
    if not chunks:
        body = text
    else:
        hits = resume_rag.top(profile, query, resume_rag.TOP_K_EVIDENCE) if query.strip() else chunks
        body = resume_rag.detail_block(hits or chunks, title="候选人简历原文（与本次讨论相关）")

    return (
        "【候选人简历（用于一致性核查）】\n"
        "请把候选人面试中的自述与下面的简历原文对照，检查是否存在：\n"
        "1. 夸大或前后矛盾（例如简历写「负责支付模块」，回答里却说「主要做测试」）；\n"
        "2. 简历里有、但整场都没被问到的关键经历（可提示面试覆盖的不足，不算考生的错）。\n"
        "只在确有依据时写进 resume_check，不要凭印象推测；没发现不一致就如实说明。\n"
        f"{body}"
    )


def deep_dive_block(profile: dict[str, Any]) -> str:
    """无简历时给面试官的「经历深挖方向」；有简历或不适用时返回空串。"""
    items = profile.get("deep_dive") or []
    if not items:
        return ""
    lines = "\n".join(f"- {t}" for t in items)
    return (
        "【经历深挖方向】候选人没有上传简历，请结合他的自述背景，"
        "优先沿下面这些方向把某一段真实经历问深，避免只考背书：\n"
        f"{lines}"
    )


def profile_summary(profile: dict[str, Any]) -> str:
    """给界面用的一行摘要。"""
    return (
        f"{profile['scene']} · {profile['major']} · {profile['level']} 级（{profile['level_label']}）"
        f" · {profile['interview_type']} · 共 {profile['total_rounds']} 道主问题"
        f" · 面试官情绪：{profile.get('mood_mode', '—')}"
    )


# ---------------------------------------------------------------------------
# 真实企业真题：来自开源题库的精选子集，作为面试官"取材"素材（不是题库脚本）
#
# 与考点库的关系：考点是"评分基线"，真题是"提问原味"。真题走配额混合——
# 每场最多 real_quota 道主问题从真题取材，其余仍锚定考点，从而不破坏
# 考点 ↔ rubrics.json 的 1:1 评分参照体系。
#
# 覆盖范围的客观限制（必须如实对待，不做粉饰）：
#     技术面真题只覆盖「计算机科学与技术 / 软件工程」；
#     人工智能与电子信息 / 自动化 / 通信工程暂无可用来源 → 静默降级为空；
#     行为面真题不受专业限制，6 个专业通用。
#
# 红线：真题文件缺失 / 损坏 / 结构异常都不能影响面试主流程，
# 所以加载器与 load_rubrics() 一样只做静默降级，绝不抛异常。
# ---------------------------------------------------------------------------
REAL_Q_FILE = Path(__file__).with_name("real_questions.json")
# 自建真题：由项目自有评分参照反向改写而来（零外部许可风险）。
# 它只作为**补位**——外部真题优先，池子为空时（人工智能 / 电子信息 /
# 自动化 / 通信工程）才启用，避免把"真实企业真题"的说法用在自建题上。
BUILTIN_Q_FILE = Path(__file__).with_name("real_questions_builtin.json")


def load_real_questions() -> dict[str, Any]:
    """读取真题素材库；文件缺失或损坏都返回空字典，绝不抛异常。"""
    try:
        with REAL_Q_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[real_q] 真题库读取失败，已降级为无真题：{exc}", file=sys.stderr, flush=True)
        return {}
    return data if isinstance(data, dict) else {}


def load_builtin_questions() -> dict[str, Any]:
    """读取自建真题库；与 load_real_questions 同款静默降级。"""
    try:
        with BUILTIN_Q_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[builtin_q] 自建真题库读取失败，已降级：{exc}", file=sys.stderr, flush=True)
        return {}
    return data if isinstance(data, dict) else {}


def real_question_source(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """按面试类型取真题池：行为面用「行为面」池，技术面用「技术面[专业]」池。

    没有对应数据（例如电子信息的技术面）时返回空列表，调用方据此静默降级。

    补位规则：外部真题优先；外部池为空时改用**自建真题**（同样按专业取）。
    两者都为空才真的没有题。
    """
    data = load_real_questions()
    if not data:
        data = {}

    if profile.get("interview_type") == "行为面":
        pool = data.get("行为面")
        if isinstance(pool, list) and pool:
            return pool
        # 行为面没有外部题时也回落到自建（当前自建未生成行为面，故通常为空）
        builtin = load_builtin_questions()
        fallback = builtin.get("行为面")
        return fallback if isinstance(fallback, list) else []

    major = profile.get("major", "")
    pool = (data.get("技术面") or {}).get(major)
    if isinstance(pool, list) and pool:
        return pool

    builtin = load_builtin_questions()
    fallback = (builtin.get("技术面") or {}).get(major)
    return fallback if isinstance(fallback, list) else []


def _sample_real_questions(profile: dict[str, Any], quota: int) -> list[dict[str, Any]]:
    """从对应真题池里随机抽定本场素材。

    配额再受两个上限约束：池子大小、主问题总数（真题也得占一道主问题）。
    """
    if quota <= 0:
        return []
    pool = real_question_source(profile)
    if not pool:
        return []
    size = min(quota, len(pool), int(profile.get("total_rounds", 5)))
    if size <= 0:
        return []
    return random.sample(pool, size)


def real_question_block(profile: dict[str, Any]) -> str:
    """拼「真实企业面试真题」提示词段；本场没抽到真题时返回空串（不产生空段落）。"""
    items = profile.get("real_questions") or []
    if not items:
        return ""

    lines: list[str] = []
    for item in items:
        question = str(item.get("q", "")).strip()
        if not question:
            continue
        topic = str(item.get("topic", "")).strip()
        lines.append(f"- {question}" + (f"（主题：{topic}）" if topic else ""))
    if not lines:
        return ""

    quota = len(lines)
    # 措辞必须与实际来源一致：外部题才叫「真实企业面试真题」，
    # 自建题（src=自建）要如实说明是「按评分参照改写的题」——
    # 否则等于让模型对候选人宣称一个不实出处。
    is_builtin = any(str(it.get("src", "")) == "自建" for it in items)
    if is_builtin:
        header = "【面试题素材 · 按评分参照改写】（素材，不是脚本）\n"
        origin = (
            f"下面 {quota} 道题是按本专业考点与常见误区改写的题目，"
            f"本场最多挑其中 {quota} 道作为主问题素材，"
            "其余主问题仍要从上面的考点参考里选，不要整场都用这些题。\n"
            "**不要向候选人声称这些题来自某家真实企业**；若被问起，"
            "如实说明是围绕本专业考点设计的题目。\n"
        )
    else:
        header = "【真实企业面试真题】（素材，不是脚本）\n"
        origin = (
            f"下面 {quota} 道题来自真实企业面试题库，本场最多挑其中 {quota} 道作为主问题素材，"
            "其余主问题仍要从上面的考点参考里选，不要整场都用真题。\n"
        )
    return (
        header
        + origin
        + "这些题是素材不是脚本：请用你自己的话口语化地问出来，可以结合候选人的经历调整问法，"
        "严禁照念原文；取材真题时，topic 字段请填该题的主题短语（见括号内），"
        "这样同场 / 跨场去重逻辑才能识别。\n"
        + "\n".join(lines)
    )


# ---------------------------------------------------------------------------
# 评分参照库：给评估官一份「考点 → 要点清单」，让判分有据可查
#
# 与上面的 topics 的区别：topics 只是"题目方向"，这里给出"答到哪几条算扎实"。
# 每条写成 must_hit / bonus / common_errors 三元组，而不是一段"标准答案"——
# 面试是开放作答，给标准答案会诱导模型做字面比对，候选人讲对但用词不同就被判错，
# 那比没有参照更糟。
#
# 红线：任何情况下评分功能都不能因为参照库缺失或损坏而中断，
# 所以加载器只做静默降级，绝不向外抛异常。
# ---------------------------------------------------------------------------
RUBRIC_FILE = Path(__file__).with_name("rubrics.json")


def load_rubrics() -> dict[str, Any]:
    """读取评分参照库；文件缺失或损坏都返回空字典，绝不抛异常。"""
    try:
        with RUBRIC_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[rubrics] 参照库读取失败，已降级为无参照：{exc}", file=sys.stderr, flush=True)
        return {}
    return data if isinstance(data, dict) else {}


def rubric_for(
    profile: dict[str, Any], asked_topics: list[str] | None = None
) -> list[dict[str, Any]]:
    """取出**本场实际问到的**考点的参照条目。

    检索键是（专业, 考点原文），不按难度过滤——因为 A 档会把 B 档的前 2 条考点
    一并带入（见 build_profile），按难度过滤反而会漏。

    `asked_topics` 是本场逐轮累积的考点（app 侧维护）。**必须传**，否则就会退化成
    "把该难度档的全部考点都当成问过的"——那正是这里修掉的一个真实缺陷：
    评估官拿到 15~17 条参照，而本场可能只问了其中 1 条，于是把没问过的考点
    判成候选人的「遗漏」，再喂给教练生成"去补这个考点"的建议，与 DESIGN
    "本场没被问到的考点不计入遗漏"直接矛盾。

    传 None 时保留旧行为（返回 profile 全部考点），仅供不掌握逐轮信息的调用方
    使用；传空列表表示"本场确实一个考点都没记录"，此时返回空列表——宁可让
    评估官退回通用标准，也不要凭空塞给它一批没问过的参照。
    """
    by_topic = load_rubrics().get(profile.get("major", ""))
    if not isinstance(by_topic, dict):
        return []

    if asked_topics is None:
        candidates = list(profile.get("topics", []))
    else:
        candidates = []
        for topic in asked_topics:
            text = str(topic).strip()
            if text and text not in candidates:
                candidates.append(text)

    items: list[dict[str, Any]] = []
    for topic in candidates:
        entry = by_topic.get(topic)
        if isinstance(entry, dict):
            items.append({"topic": topic, **entry})
    return items


def rubric_for_topic(profile: dict[str, Any], topic: str) -> dict[str, Any] | None:
    """按考点原文取**单条**参照；取不到返回 None。

    与 rubric_for 的区别：那个是"本场所有考点的参照"（给评估官，一次性注入），
    这个是"**当前这一道题**的参照"（给面试官，逐轮注入）。

    为什么需要它：面试官此前只拿到考点**名字**（reference_for），不知道参照里
    写了什么必中要点、哪些是常见误区。于是"问的人"和"评的人"口径是断开的——
    问的人不知道评的人在看什么，DESIGN 说的"评分口径与提问口径同源"只发生在
    编译配置那一刻，运行时并没有兑现。

    注意这里**不做难度过滤**，与 rubric_for 同理：A 档会带入 B 档的前 2 条考点，
    按难度过滤会漏。
    """
    text = str(topic or "").strip()
    if not text:
        return None
    by_topic = load_rubrics().get(profile.get("major", ""))
    if not isinstance(by_topic, dict):
        return None
    entry = by_topic.get(text)
    if not isinstance(entry, dict):
        return None
    return {"topic": text, **entry}


def rubric_hint_block(profile: dict[str, Any], topic: str) -> str:
    """给**面试官**看的单条参照提示；无参照时返回空串。

    与 rubric_block（给评估官）的三处刻意差异：

    1. **只给当前这一条**。评估官拿全场 5~7 条一次性判分；面试官是逐轮问的，
       灌全场只会稀释当前这一题的注意力。
    2. **措辞是"提问弹药"而不是"判分标准"**。这里明确告诉面试官：
       参照是给你出题和追问用的，**不是**让你把要点念出来或拿来对答案。
       同一份数据换个用途，措辞必须跟着换，否则模型会去念清单。
    3. **常见误区转成"可以设的陷阱"**。这是这一版真正的能力增量——
       知道误区清单后，面试官可以故意问"红黑树是严格平衡的树，对吧？"，
       而不是干等候选人自己踩。
    """
    item = rubric_for_topic(profile, topic)
    if item is None:
        return ""

    lines = [f"【你正在问的考点：{item['topic']}】本场评委判分时会对照下面这份要点清单。"]
    must_hit = item.get("must_hit") or []
    if must_hit:
        lines.append(
            "必中要点（候选人讲到才算答到位）：" + "；".join(str(x) for x in must_hit)
        )
    bonus = item.get("bonus") or []
    if bonus:
        lines.append("加分项（讲到算亮点）：" + "；".join(str(x) for x in bonus))
    errors = item.get("common_errors") or []
    if errors:
        lines.append(
            "常见误区（**可以设成陷阱去问**，例如反问「是不是……？」看候选人会不会纠正）："
            + "；".join(str(x) for x in errors)
        )

    lines.append(
        "使用规则：这份清单是给你**出题和追问**用的弹药，不是给你念的。"
        "严禁把要点逐条念出来、严禁拿它当标准答案去对候选人、严禁提示正确答案。"
        "候选人给出清单之外但同样正确的答法，照常认可。"
        "追问要针对他**没讲到的那一条**，而不是重复他已经说过的。"
    )
    return "\n".join(lines)


def rubric_block(
    profile: dict[str, Any], asked_topics: list[str] | None = None
) -> str:
    """把本场参照拼成注入评估官提示词的段落；无命中时返回降级文案。

    只注入**本场实际问到**的那几条（通常 5~7 条），不是全量 15~17 条，
    避免提示词被稀释、也避免把没问过的考点判成候选人的遗漏。
    `asked_topics` 的语义与降级行为见 rubric_for。
    """
    items = rubric_for(profile, asked_topics)
    if not items:
        return "（本场未提供考点参照，请按通用标准正常评价。）"

    chunks: list[str] = []
    for item in items:
        lines = [f"考点：{item['topic']}"]
        must_hit = item.get("must_hit") or []
        if must_hit:
            lines.append("必须命中的要点：" + "；".join(str(x) for x in must_hit))
        bonus = item.get("bonus") or []
        if bonus:
            lines.append("加分项（命中加分，不命中不扣）：" + "；".join(str(x) for x in bonus))
        errors = item.get("common_errors") or []
        if errors:
            lines.append(
                "常见误区（踩中要扣分：写进该考点的 errors，并概括进 risks）："
                + "；".join(str(x) for x in errors)
            )
        # veto 是条目上的可选标记：标了才渲染这一行，没标就与旧行为完全一致。
        # 它把"误区里最致命的那几条"单独拎出来，让评估官知道踩中它就是该考点不通过，
        # 而不是与其它误区一起当普通扣分项处理。
        # 措辞要求"原样写进 errors"：判定在代码里做的是子串匹配，
        # 只要评估官改写成自己的话就会漏判（不能把否决权交给模型自由发挥）。
        veto = _clean_list(item.get("veto"))
        if veto:
            lines.append(
                "一票否决的误区（踩中即该考点判为不通过：必须把命中的这一条**原样**"
                "写进该考点的 errors，并写进 risks）："
                + "；".join(veto)
            )
        chunks.append("\n".join(lines))
    return "\n\n".join(chunks)


# ---------------------------------------------------------------------------
# 考点级判定与聚合
#
# 评估官给的是**要点粒度**的对照结果（must_hit / missed / errors），
# 但报告真正要回答的是"这道题整体过没过"。漏一条要点算不算硬伤，取决于它是不是核心，
# 所以这里用一组固定规则把要点粒度升成考点粒度。规则全部写在代码里（不再让模型判一次），
# 同一份 rubric_check 每次得到同一个结论，也便于离线复现。
#
# 否决项有两个来源：
#   1. 条目显式标出的 veto 误区——踩中即判不通过（数据侧未标时这一段自然失效）；
#   2. must_hit 全数未命中——整道题没答上，不该落进"部分通过"的模糊地带。
# 两条都不触发时，才按命中 / 遗漏 / 误区的情况给出通过或部分通过。
#
# 与 rubric_for 一样，参照缺失或损坏时静默降级：只是不启用"显式否决"那一档，
# 判定本身仍能算出来（评估官给的对照结果不依赖参照文件）。
# ---------------------------------------------------------------------------

VERDICT_PASS = "pass"
VERDICT_PARTIAL = "partial"
VERDICT_FAIL = "fail"
VERDICT_UNKNOWN = "unknown"

# 报告与界面共用这一套中文标签，分档名只在这一处定义。
VERDICT_LABELS: dict[str, str] = {
    VERDICT_PASS: "通过",
    VERDICT_PARTIAL: "部分通过",
    VERDICT_FAIL: "不通过",
    VERDICT_UNKNOWN: "未记录",
}


def _clean_list(value: Any) -> list[str]:
    """去空白、去空的字符串列表；非列表一律当空。"""
    if not isinstance(value, (list, tuple)):
        return []
    return [str(x).strip() for x in value if str(x).strip()]


def _veto_of(entry: Any) -> list[str]:
    """取条目显式标出的一票否决误区；没标（或条目缺失）返回空列表，即不启用否决。"""
    if not isinstance(entry, dict):
        return []
    return _clean_list(entry.get("veto"))


def topic_verdict(row: Any, veto: list[str] | None = None) -> tuple[str, str]:
    """把一条 rubric_check 升成考点级判定，返回 (verdict, reason)。

    分档规则自上而下，先命中的先算：
        fail    —— 踩中一票否决误区 / 核心要点一条都没答到 / 出现概念错误；
        partial —— 至少答到一条要点，但仍有遗漏；
        pass    —— 要点全覆盖且没有误区；
        unknown —— 既没命中也没遗漏，没留下可用的对照记录。
    """
    if not isinstance(row, dict):
        return VERDICT_UNKNOWN, "该考点没有可用的对照记录"
    hit = _clean_list(row.get("must_hit"))
    missed = _clean_list(row.get("missed"))
    errors = _clean_list(row.get("errors"))

    veto_hits = [e for e in errors if any(v and v in e for v in (veto or []))]
    if veto_hits:
        return VERDICT_FAIL, "踩中一票否决误区：" + "；".join(veto_hits)
    if missed and not hit:
        return VERDICT_FAIL, "核心要点一条都没答到"
    if errors:
        return VERDICT_FAIL, "出现概念错误：" + "；".join(errors)
    if missed:
        return VERDICT_PARTIAL, "答到部分要点，仍有遗漏"
    if hit:
        return VERDICT_PASS, "要点均已覆盖"
    return VERDICT_UNKNOWN, "该考点没有留下可用的对照记录"


def aggregate_rubric_check(profile: dict[str, Any], rows: Any) -> dict[str, Any]:
    """考点级聚合：本场每个考点过没过、哪些算薄弱项。

    `profile` 只用来取该专业的参照条目（看条目有没有显式 veto 标记），
    因此参照缺失时聚合照常工作，只是不启用"显式否决"那一档。

    返回结构（报告与界面共用）：
        topics  —— 每个考点的 {topic, verdict, label, reason, missed, errors}
        counts  —— 各判定档的条数（键为 VERDICT_*）
        total / passed / weak —— 考点数、通过数、薄弱考点（不通过 + 部分通过）
    """
    by_topic = load_rubrics().get(profile.get("major", ""))
    if not isinstance(by_topic, dict):
        by_topic = {}

    topics: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        topic = str(row.get("topic") or "").strip()
        verdict, reason = topic_verdict(row, _veto_of(by_topic.get(topic)))
        topics.append(
            {
                "topic": topic,
                "verdict": verdict,
                "label": VERDICT_LABELS.get(verdict, verdict),
                "reason": reason,
                "missed": _clean_list(row.get("missed")),
                "errors": _clean_list(row.get("errors")),
            }
        )

    counts = {VERDICT_PASS: 0, VERDICT_PARTIAL: 0, VERDICT_FAIL: 0, VERDICT_UNKNOWN: 0}
    for item in topics:
        counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1

    return {
        "topics": topics,
        "counts": counts,
        "total": len(topics),
        "passed": counts[VERDICT_PASS],
        "weak": [t for t in topics if t["verdict"] in (VERDICT_FAIL, VERDICT_PARTIAL)],
    }
