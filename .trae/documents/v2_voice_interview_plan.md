# v2 语音面试 —— 实施计划

## 背景

v1 已完成：文本面试 + 三级自适应配置 + 三体 Agent（面试官 / 评估官 / 教练）+ 兜底约束 + 简历针对性提问。
DESIGN.md 第十一节的路线图里，**v2 = 语音交互（ASR / TTS）+ 简历上传解析**，其中简历那半已提前交付，只剩语音。

当前问题是：面试全程靠键盘打字，演示时"像在跟聊天机器人写作文"，不像真面试。
本次要补上语音，让候选人**说**、面试官**开口回**，把演示体感从"文本工具"拉到"面试现场"。

目标产出：在 2026-10-20 校赛截止前，有一个可录进演示视频的语音面试版本。

## 目标与范围

**做**
- 语音提问：浏览器录音 → 云端识别 → 当作正常回答进入现有决策流程
- 语音播报：面试官每轮回复自动合成语音并播放
- 音色可选：侧边栏下拉，面试前切换

**不做**（明确排除，避免范围蔓延）
- 数字人 / 唇形驱动（DESIGN.md 列为 v3，不作为校赛交付项）
- 简历 RAG / 向量库（单份简历全文注入已优于检索，见 DESIGN.md 第七节）
- 打断、实时流式语音（成本高，收益对演示有限）

## 方案总览

```
录音(st.audio_input) → voice.transcribe() → 硅基流动 SenseVoice
                              ↓ 识别文本
                    复用现有 history / advance() 决策流程（零改动）
                              ↓ 面试官回复
                     voice.synthesize() → edge-tts → st.audio(autoplay)
```

关键点：**语音只包在输入输出两端，中间的决策状态机完全不动**。
[agents.py](file:///e:/shuangxingxi/AIagent/agents.py) 一行都不用改。

## 改动清单

### 1. 新增 `voice.py`（约 60 行）

两个函数 + 一张音色表：

- `transcribe(audio_bytes, api_key) -> str`
  用已装的 `openai` 包，`base_url="https://api.siliconflow.cn/v1"`，
  调 `client.audio.transcriptions.create(model="FunAudioLLM/SenseVoiceSmall", file=("answer.wav", data, "audio/wav"))`。
  `st.audio_input` 产出的正是 PCM WAV，格式天然匹配，不需要转码。
- `synthesize(text, voice) -> bytes`
  用 `edge-tts`，内部 `asyncio.run` 收流拼成 mp3 字节返回。
- `VOICES`：中文音色表（晓晓 / 云希 / 晓伊 / 云健 / 云扬），供侧边栏下拉。
- `DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"`

两个函数都**只管抛异常，不做兜底**；降级策略统一放在 app.py，保持职责单一。

### 2. 改 `app.py`

**(a) API Key 加载器泛化**
[load_api_key()](file:///e:/shuangxingxi/AIagent/app.py#L45-L63) 现在写死了 `DEEPSEEK_API_KEY`，
改成接收环境变量名参数 `load_api_key(env_name)`，新增常量 `SILICONFLOW_KEY_ENV = "SILICONFLOW_API_KEY"`。
注册表回退逻辑原样保留——同一套代码服务两个 key。

**(b) `init_state()` 新增字段**
```python
"sf_key": load_api_key(SILICONFLOW_KEY_ENV),
"voice_on": True,
"voice_name": DEFAULT_VOICE,
"audio_by_index": {},      # 面试官消息下标 -> mp3 字节
"autoplay_index": -1,      # 需要自动播放的消息下标
"voice_signature": "",     # 已处理过的录音指纹，防重复识别
"voice_error": "",
```

**(c) 侧边栏新增「语音」区块**（放在简历上传之后、「开始面试」之前）
- 「启用语音」checkbox
- 音色 `selectbox`（`VOICES`），启用语音时才显示
- SiliconFlow Key：有环境变量则只显示一行 caption，否则显示 password 输入框（**完全复用 DeepSeek 那套写法**，见 [app.py#L251-L260](file:///e:/shuangxingxi/AIagent/app.py#L251-L260)）
- 一行隐私/依赖说明：语音会上传到硅基流动识别

**(d) `advance()` 末尾接上播报**
新增小helper `speak(text)`：合成 mp3 存入 `audio_by_index[len(history)-1]`，
并把 `autoplay_index` 指到该下标。整段包 `try/except`，**失败只打印 stderr、绝不中断面试**。
放在 `advance()` 里是因为它同时被 [start_interview()](file:///e:/shuangxingxi/AIagent/app.py#L116-L128) 和
[render_interview()](file:///e:/shuangxingxi/AIagent/app.py#L352-L360) 调用，开场白也要能出声。

**(e) `render_interview()` 加语音输入**
在现有 `st.chat_input` 旁边加 `st.audio_input`，两路输入汇合成同一个 `answer` 变量，
后面的提交逻辑（append → advance → rerun）**一行不改**：

```python
answer = st.chat_input("输入你的回答，按回车发送…")

if voice_on and sfc_key:
    clip = st.audio_input("或者按住说话", key=f"voice_{st.session_state.voice_epoch}")
    if clip is not None:
        sig = hashlib.md5(clip.getvalue()).hexdigest()
        if sig != st.session_state.voice_signature:   # 只处理新录音
            st.session_state.voice_signature = sig
            try:
                text = transcribe(clip.getvalue(), sfc_key)
            except Exception as exc:
                st.session_state.voice_error = f"语音识别失败：{exc}"
                text = ""
            if text and not answer:
                answer = text

if answer:   # ← 以下为现有逻辑，不动
    ...
```

用「录音指纹 + signature 守卫」而不是"识别完就清空控件"，
是因为后者需要 `pending_answer` + 递增 key 的两段式提交，容易写出死循环
（简历上传播件踩过这个坑，见 [clear_resume()](file:///e:/shuangxingxi/AIagent/app.py#L171-L181) 的注释）。
指纹方案和 [sync_resume()](file:///e:/shuangxingxi/AIagent/app.py#L184-L199) 完全同构，一致性好。

**(f) 历史渲染循环加播放器**
```python
for index, msg in enumerate(st.session_state.history):
    with st.chat_message(role, avatar=...):
        st.markdown(msg["content"])
        audio = st.session_state.audio_by_index.get(index)
        if audio:
            st.audio(audio, autoplay=(index == st.session_state.autoplay_index))
st.session_state.autoplay_index = -1        # 只自动播一次
```

**音频绝不写进 `history`**——因为 [finish_interview()](file:///e:/shuangxingxi/AIagent/app.py#L224-L236) 会把
`history` 整个塞进 `history.json` 落盘，带 bytes 的 `json.dumps` 会直接抛 `TypeError`。
单独放 `audio_by_index` 是最省事的隔离方式，也顺带让音频随会话结束自然消失。

`start_interview()` 里重置 `audio_by_index` / `autoplay_index` / `voice_signature`。

### 3. 改 `requirements.txt`
加一行 `edge-tts>=6.1`。ASR 走 HTTP，复用已装的 `openai`，不新增依赖。

### 4. 改 `DESIGN.md` 与 `DESIGN.docx`（顺带把之前欠的账还掉）
- 第十一节路线图：v2 标记完成，v3 保持"简历 RAG / 数字人"（并注明简历 RAG 已由全文注入替代）
- 第九节技术栈表：语音行补上实际选型（SenseVoice ASR + edge-tts）
- 第十三节工程结构：加 `voice.py`
- 第二节/最小原则里"简历属 v2"的表述与现状对齐
- 改完用同样的方式导出 `.docx`

## 关键设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| ASR 落地 | 硅基流动 SenseVoice（云端） | 国内直连、识别准、OpenAI 兼容接口可直接复用已装的 openai 包，零新依赖；本地 whisper 要装 ~150MB 且首次下模型（国内可能卡） |
| TTS 落地 | edge-tts | 免费无限量、中文音色自然度明显更好；代价是它调用微软 Edge 的非官方接口，有失效风险 |
| TTS 兜底 | 失败即静默跳过 | 演示时"没声音"远好过"整个面试崩掉"；且换成硅基流动 CosyVoice 只需改 `synthesize()` 一个函数体 |
| 语音输入提交方式 | 识别完自动提交 | 演示流畅度优先。若现场出现误识别，改成"先确认再发送"是一处小改动 |
| 音频存放 | 独立 `audio_by_index` | 隔离落盘，避免 `json.dumps` 崩溃；也让音频随会话结束自动清掉 |

## 已知风险与降级

| 风险 | 表现 | 降级方案 |
|---|---|---|
| `st.audio_input` 拿不到麦克风 | 非 localhost 访问时浏览器拒绝 | 只在本机 `localhost:8501` 演示；文字输入始终保留可用 |
| edge-tts 接口失效 | 合成抛异常 | `try/except` 静默跳过，面试照常进行；必要时换硅基流动 CosyVoice |
| 硅基流动 Key 未配置 | 录音后报错 | 侧边栏提示 + 自动退回文字输入 |
| 识别延迟 | 说完到出字有 1-3 秒 | `st.spinner` 给出反馈；录音控制在 15 秒内 |
| 误识别 | 答非所问被提交 | 演示前彩排；必要时加确认步骤 |

## 验证方式

1. **依赖**：`py -3.12 -m pip install edge-tts`
2. **单模块**（临时脚本，放系统 temp 目录，不留在项目里）
   - TTS：合成"你好，我是本次面试的面试官"→ 存 mp3 → 播放确认音色正常
   - ASR：拿上一步的 mp3 转 wav 喂给 `transcribe()`，确认能转回原文
3. **界面回归**：用 `AppTest` 跑一遍，断言侧边栏出现「启用语音」「音色」控件、默认 idle 页正常渲染、无异常
4. **端到端**：`py -3.12 -m streamlit run app.py` → 浏览器 http://localhost:8501
   - 点「开始面试」→ 确认**开场白自动播放**
   - 说话作答 → 确认转成文字并作为候选人消息出现 → 确认面试官回复且自动播放
   - 关掉「启用语音」→ 确认退回纯文字模式，功能不受影响
5. **降级演练**：故意填错 SiliconFlow Key → 确认报错提示出现且文字输入仍能完成整场面试

## 需要你配合的前置动作

1. 去 siliconflow.cn 注册并建一个 API Key（免费额度足够演示）
2. 拿到 key 后告诉我，我按上次 DeepSeek 的方式写进 Windows 用户级环境变量，这样启动就不用每次手填
