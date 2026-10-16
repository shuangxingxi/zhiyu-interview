# 知遇 · 多场景多难度自适应 AI 虚拟面试智能体

面向**考研复试 / 应届校招 / 日常实习 / 转正就职**四类考生的模拟面试 Agent。
不是题库，而是一套**会现场追问**的面试官引擎：同一套代码，靠「场景 × 专业 × 难度」
三级配置，还原不同面试官的真实差异，并在面试后给出**可执行**的复盘报告。

## 快速开始

```powershell
py -3.12 -m pip install -r requirements.txt
$env:DEEPSEEK_API_KEY = "sk-xxxx"        # 必需：面试官 / 评估官 / 教练
$env:SILICONFLOW_API_KEY = "sk-xxxx"     # 可选：语音识别与合成兜底
py -3.12 -m streamlit run app.py
```

浏览器打开后，在左侧选「场景 → 专业 → 难度」，可选填候选人背景或上传简历，
点「开始面试」即可。API Key 也可以直接在侧边栏手填，不必设环境变量。

## 跑测试

**不联网、不花 token、零新增依赖**（只用标准库 `unittest`）：

```powershell
py -3.12 -m unittest test_tool_call -v
py -3.12 -m unittest test_mood -v
py -3.12 -m unittest test_interview_state -v
py -3.12 -m unittest test_session_store -v
py -3.12 -m unittest test_chat_json -v
py -3.12 -m unittest test_history_store -v
py -3.12 -m unittest test_rubric_hint -v
py -3.12 -m unittest test_evaluator_rubric -v
```

8 套共 **209 个用例**。设计上有两条硬要求：**不联网**（把网络层换成替身）、
**检索用真件**（真简历、真切片、真跑本地 BM25）。

## 文档导航

| 文档 | 内容 |
|---|---|
| [`DESIGN.md`](DESIGN.md) | **权威设计文档**：定位、三级自适应、决策状态机、评估体系、风险与应对 |
| [`变更清单-v7.md`](变更清单-v7.md) | v7–v11 的逐项改动与**实测验证结论**（含一个实测发现的缺陷修复） |
| [`知识库考点溯源表.md`](知识库考点溯源表.md) | 270 条考点逐条映射到本科教材章节（含核对状态） |
| [`真题抽检清单.md`](真题抽检清单.md) | 外部真题来源的覆盖范围与人工抽检清单 |
| [`大赛材料/`](大赛材料/) | 演示视频脚本与作品 PPT |

## 目录结构

```
AIagent/
├── app.py              # Streamlit 界面 + 面试状态机
├── agents.py           # 三体 Agent：面试官 / 评估官 / 教练
├── knowledge.py        # 场景 × 专业 × 难度 策略库 + 能力模型 + 参照库加载
├── mood.py             # 面试官情绪纯函数状态机
├── interview_state.py  # 计数器状态机（纯函数）
├── resume.py           # 简历解析（PDF / Word / TXT → 纯文本，仅内存）
├── resume_rag.py       # 简历 RAG（切片 + 本地字符 n-gram BM25）
├── voice.py            # 语音识别（SenseVoice）与合成（edge-tts + 兜底）
├── avatar.py           # 语音动效组件（声波球，纯前端 HTML）
├── session_store.py    # 会话快照存储（一场一文件 + 原子写）
├── history_store.py    # 历史记录存储与统计（趋势 / 维度对比 / 删除）
├── rubrics.json        # 评分参照库（270 条，与代码分离便于校订）
├── real_questions.json # 真实企业真题素材库（含来源署名与许可证）
└── test_*.py           # 8 套离线单测（共 209 个用例）
```

## 隐私说明

- **简历**：只在当前会话内存中处理，**从不写入磁盘**，面试结束即清除。
- **面试逐字稿**：会落盘到 `history.json`（供回看与跨场对比）。该文件已加入
  `.gitignore`，因为其中可能含真实候选人的个人信息。界面提供**逐场删除**与
  **清空全部**（两步确认）两个入口。
- **API Key**：一律从环境变量或界面读取，**不写入任何文件**。

## 许可与数据来源

`real_questions.json` 取自三个开源题库的精选子集，产物与文档中均保留署名：

| 来源 | 许可证 |
|---|---|
| [doocs/advanced-java](https://github.com/doocs/advanced-java) | CC-BY-SA-4.0 |
| [CyC2018/CS-Notes](https://github.com/CyC2018/CS-Notes) | CC BY-NC-SA 4.0（**非商业**） |
| [haizlin/fe-interview](https://github.com/haizlin/fe-interview) | MIT |

> CS-Notes 为非商业协议。本项目为非商业参赛作品；**若后续商用，必须先移除该来源数据**。
