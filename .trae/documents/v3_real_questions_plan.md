# v3 · 真实企业真题扩展 —— 实施方案

## 一、Context（为什么做）

参赛作品「多场景多难度自适应 AI 虚拟面试智能体」目前的知识库是**自建**的 270 条考点（6 专业 × 3 难度档 × 15 条，见 `knowledge.py` 的 `MAJOR_MODEL`）。用户的原始诉求是「模拟真实企业面试中可能的问题和对话」——但 270 考点是教材体系的抽象知识点短语，缺少**真实企业面试题的原味**。

此前已用清洗管线从三个开源仓库产出干净题库（临时目录）：

| 文件 | 来源 | 许可证 | 条数 |
|---|---|---|---|
| `clean_fe.json` | haizlin/fe-interview（前端） | MIT | 5981 |
| `clean_advanced_java.json` | doocs/advanced-java（Java 后端） | CC-BY-SA-4.0 | 130 |
| `clean_csnotes.json` | CyC2018/CS-Notes（计算机基础） | CC BY-NC-SA 4.0 | 1073 |

合计 7184 条。本方案把其中**精选子集**接入面试官素材，让面试更贴近真实企业面试，同时**不破坏**现有 270 考点 ↔ `rubrics.json` 的 1:1 评分参照体系。

**用户已确认的三项决策**：
1. 真题角色 = **配额混合**（每场主问题中最多默认 2 道从真题取材，其余仍锚定 270 考点）
2. **纳入行为面**（fe-interview 的「软技能」题进行为面池，可覆盖全部 6 专业）
3. **精选规模**（严格过滤后约 1000–1500 条，可人工审校）

## 二、范围与硬约束

- **覆盖范围（必须如实告知）**：技术面真题只覆盖 **计算机科学与技术 / 软件工程**；人工智能仅个位数；**电子信息 / 自动化 / 通信工程技术面无真题**，走静默降级（不报错、不显示）。行为面真题不受专业限制，6 专业通用。
- **不得破坏**：270 考点 ↔ `rubrics.json` 的 1:1 对齐；`knowledge.py` 现有对外函数签名（新增参数一律带默认值）。
- **不引入**：向量库 / RAG / 重型框架。真题取材仍走**字典查表 + 随机抽样**。
- **静默降级**：真题文件缺失 / 损坏 / 结构异常时，一律退化为「无真题」，面试与评分绝不中断（与 `load_rubrics()` 同款处理）。
- **许可证**：三个来源必须在产物与文档中署名；CS-Notes 为 **非商业** 协议，本项目为非商业参赛作品，符合要求。
- **不改动**：`rubrics.json`、`mood.py`、`resume.py`、`voice.py`。

## 三、设计要点

### 3.1 数据管线（在临时目录跑，产物落项目）

对三份已清洗 JSON 做**精选过滤**，产出 `real_questions.json`：

**过滤口径（技术面池）**
- 剔除 fe 的「软技能」类 765 条（→ 转入行为面池，不丢弃）
- 剔除 fe 的**时效性**内容（IE6 / IE9 兼容、具体产品与年份、Firefox 与 Chrome 差异等）约 202 条
- 剔除 csnotes 的**超短标题**（题干 ≤ 4 字）约 358 条
- 剔除 `suspected_generated=true`（仅 fe 17 条）
- 跨文件按**归一化题干**去重（沿用此前管线的去重口径）
- 专业归属用**关键词口径**（CS / SE / AI），并按 `topic` + `category` 双字段判定，避免把「算法」误判进 AI

**条目结构**（单条尽量小）
```json
{"q": "题干（原样，可为名词短语标题）", "topic": "后端主题，如 消息队列", "cat": "分类",
 "src": "haizlin/fe-interview", "lic": "MIT", "ref": "原始链接或文件路径"}
```

**产物顶层结构**
```json
{
  "_meta": {
    "purpose": "...", "generated": "2026-10-11",
    "filter_rules": ["..."], "counts": {"技术面": {...}, "行为面": {...}},
    "sources": [{"repo": "...", "license": "...", "url": "...", "count": N}]
  },
  "技术面": { "计算机科学与技术": [ ... ], "软件工程": [ ... ], "人工智能": [ ... ] },
  "行为面": [ ... ]
}
```

> 注：题干为名词短语标题时**不做离线改写**——面试官本身就是 LLM，提示词要求它「用口语化的问法讲出来」，与现有「考点不许照念」的做法一致，零额外成本。

### 3.2 `knowledge.py`（新增，不改现有函数语义）

- `REAL_Q_FILE = Path(__file__).with_name("real_questions.json")`
- `load_real_questions() -> dict`：读取 + **静默降级**，与 `load_rubrics()`（`knowledge.py#L699-L709`）完全同款。
- `real_question_source(profile) -> list`：按 `interview_type` 选池（行为面 → `行为面`；技术面 → `技术面[专业]`）。无数据返回 `[]`。
- `build_profile(...)` **新增参数** `real_quota: int = 2`、`use_real: bool = True`（带默认值，兼容现有调用），在编译配置时**抽定** `profile["real_questions"]`（随机抽样，整场不变）与 `profile["real_q_topics"]`（抽样条目的 topic，供代码侧计数）。
- `real_question_block(profile) -> str`：拼「真实企业真题」提示词段；无抽样时返回 `""`（不产生空段落）。

### 3.3 `agents.py`

- `from knowledge import real_question_block`，并把 `real_question_block(profile)` 加进 `_build_interviewer_system` 的 `blocks` 列表（`agents.py#L301-L311` 的 `context_blocks`）。
- `INTERVIEWER_SYSTEM` 模板新增一小节规则：
  - 本场最多 `{real_quota}` 道主问题从「真实企业真题」取材，其余必须落在考点参考里；
  - 真题是**素材不是脚本**，要用自己的话口语化问出来，禁止照念；
  - 取材真题时 `topic` 填该真题的主题短语（保证同场 / 跨场去重仍生效，`_build_avoid_block` 逻辑不变）。
- 评估官侧**不改**：真题若未被 270 考点覆盖，`rubric_block` 自然不含它，按「参照是底线不是天花板」照常评分。

### 3.4 `app.py`

- 侧边栏新增「真题扩展」开关 + 「真题配额」滑块（0–3，默认 2），并在开关旁**明示覆盖范围**（技术面仅计算机 / 软件工程；行为面全专业）。
- `start_interview` 调用 `build_profile(...)` 时传入 `use_real` / `real_quota`。
- 面试报告 / 历史里标注本场用了几道真题（轻量，便于答辩演示）。

### 3.5 文档同步

- `DESIGN.md`：版本路线图新增 `v3（已交付）·真实企业真题扩展`；第七节知识库补充本条；第十二节风险表补一行（真题许可与覆盖范围）。
- 数据来源声明：把三个仓库的许可与署名补进 `DESIGN.md`（与 `知识库考点溯源表.md` 第三节口径一致）。
- `大赛材料/AI虚拟面试智能体-作品PPT.pptx`：第 5 / 14 / 18 页数字与 v3 条目同步（**先确认文件未被 WPS 占用**）。

## 四、改动文件清单

| 文件 | 动作 |
|---|---|
| `real_questions.json` | **新建**（产物，含 `_meta` 许可与口径） |
| `knowledge.py` | 新增 `REAL_Q_FILE` / `load_real_questions` / `real_question_source` / `real_question_block`；`build_profile` 增 2 个带默认值的参数 |
| `agents.py` | 新增 import；`context_blocks` 增一段；`INTERVIEWER_SYSTEM` 增真题规则小节 |
| `app.py` | 侧边栏开关 + 配额滑块 + 覆盖率说明；`start_interview` 传参；报告标注 |
| `DESIGN.md` | 新增 v3 条目、数据来源声明、风险表一行 |
| `大赛材料/…作品PPT.pptx` | 数字与 v3 条目同步 |

不动：`rubrics.json` / `mood.py` / `resume.py` / `voice.py` / `history.json`。

## 五、验证方案

**A. 离线断言（临时脚本，不落项目）**
1. `real_questions.json` 可解析；`_meta.sources` 三个仓库齐全且 license 非空
2. 技术面：CS / SE 池非空；AI 池允许为空；各池**无重复题干**、无超短标题（≤4 字）、无软技能残留
3. 行为面池非空，且不含时效性内容
4. `build_profile` 对 6 专业 × 2 面试类型全部可编译；EE / AUTO / COMM 技术面 → `real_question_block == ""`（静默降级）
5. `real_quota=0` 或 `use_real=False` → block 为空、行为与 v2.2 完全一致
6. 文件缺失 / 损坏 → `load_real_questions()` 返回 `{}` 且不抛异常
7. **回归**：270 考点 ↔ `rubrics.json` 1:1 断言、`load_rubrics` 降级断言、同场 / 跨场去重断言仍全绿

**B. 在线端到端（需 `DEEPSEEK_API_KEY`）**
1. 计算机科学与技术 · 校招 · B 档：确认面试官出现真题取材、且 `topic` 可被去重逻辑识别
2. 行为面：确认用上软技能真题
3. 电子信息 · 技术面：确认无真题注入、流程与 v2.2 一致、无报错
4. 严格模式录演示：确认每场节奏稳定、真题配额不越界

**C. 数据抽检**
- 生成「真题抽检清单」，按专业与来源分层各抽 10 条人工核对（延续此前"生成后人工抽检"的质量门习惯）

## 六、风险与取舍

| 风险 | 应对 |
|---|---|
| 真题提问导致 rubric 覆盖下降 | 配额混合（默认 ≤2 道）；严格模式录演示时可将配额设为 0 |
| 提示词变长稀释指令 | 仅注入 2–3 条真题，且只在本场；与 `reference_for` 的 15–17 条同量级 |
| 3 个专业无技术面真题 | UI 明示 + 静默降级；行为面真题兜底覆盖 6 专业 |
| 时效性 / 低质题混入 | 精选过滤（剔软技能、时效性、超短标题）+ 人工抽检清单 |
| 许可合规 | 产物 `_meta` 与 `DESIGN.md` 双重署名；CS-Notes 非商业协议与参赛性质相符 |
| 破坏 270 ↔ rubric 1:1 | 明确不改 `rubrics.json`；回归断言兜底 |

## 七、明确不做

- 不做真题 → 考点的语义映射/向量检索（复杂度高、收益低）
- 不做离线 LLM 批量改写题干（交给面试官运行时口语化）
- 不为电子信息 / 自动化 / 通信工程技术面凭空造真题
- 不把 7184 条全量灌入（会稀释质量，与「精选」决策相悖）
