# 项目笔记（AI 助理必读，先读再动手）

> 纪律：这里已有的结论不重新分析；有新结论立刻追加，带日期与依据。

## 2026-10-24 · 让别人打开本地面试页（分享功能）

### 已新增文件
- `分享给别人.ps1` —— 双模式分享脚本，`-Mode lan|public`，`-Port 8501`（默认）。
- `分享给别人.bat` —— 双击入口，交互选 1（局域网）/2（公网），**必须 GBK(936) 编码**。

### 关键结论（含踩过的坑，别再重复试）
1. **.ps1 必须带 BOM**。本机 `powershell.exe`（5.1）无 BOM 时按 GBK 解析 UTF-8 源码，
   中文注释里的字节被吃成引号 → `意外的标记"}"`。每次编辑后补 BOM：
   `[System.IO.File]::WriteAllText($p,$t,(New-Object System.Text.UTF8Encoding($true)))`
2. **.bat 必须 GBK**，否则 cmd 报 `'€鍩熺綉...' 不是内部或外部命令`。
   写：`[System.IO.File]::WriteAllText($p,$t,[System.Text.Encoding]::GetEncoding(936))`
3. **不要设 `[Console]::OutputEncoding = UTF8`**。设了反而全乱码。文件带 BOM 后
   PowerShell 能正确解码中文，输出按 chcp 936 转码即正常（已实测）。
4. **Python 探测命令不能用带双引号的 `-c`**。`py -3.12 -c "import streamlit;print('ok')"`
   里双引号被 py 启动器吞掉 → `NameError: name 'ok' is not defined`。用 `-c 'import streamlit'`。
   另：本机 `py -3` 是坏的（`Unable to create process`，退出码 101），可用的是 `py -3.12` 和 `python`。
5. **streamlit 参数要先拼好再入数组**。`@('--server.port=' + $Port)` 会被拆成两个元素，
   streamlit 报 `--server.port` 无效。必须先 `$argPort = '--server.port=' + $Port`。
6. **cloudflared 必须加 `--protocol http2`**。默认走 QUIC(UDP)，本机这张 WLAN
   （校园网）UDP 丢包严重，日志报 `datagram manager error: timeout: no recent network activity`
   / `failed to accept QUIC stream`，表现是 trycloudflare 地址一直 530。
   加 http2 后实测 `/_stcore/health` 返回 200、首页 200 len=6463。
7. **拿到 trycloudflare 地址不等于可用**。DNS 传播 + 边缘接入有 10–20 秒空窗，
   期间访问是 530。必须轮询 `https://URL/_stcore/health` 等到 200 再把链接给用户。
8. **复用已运行的服务要校验监听地址**。`launcher.ps1:125` 绑的是 `127.0.0.1`，
   直接复用会导致别人连不上。用 `netstat -ano` 匹配 `LISTENING` + `0.0.0.0|\[::\]` 校验。
9. **报内网 IP 只报有默认路由的接口**。`Get-NetRoute -DestinationPrefix '0.0.0.0/0'`
   取 InterfaceIndex，否则会把 VMware/WSL/小米互联的 192.168.x.1 报出去，对方连不上。
   本机真实地址：`10.35.188.132`（WLAN）。
10. **streamlit 自报的 `External URL: http://223.160.145.167:PORT` 不可用**，
    路由器没做端口映射。不要给用户。
11. **WebSocket 不用额外配置**。实测 `curl -H "Upgrade: websocket" .../_stcore/stream`
    带不带 Origin 都返回 `101 Switching Protocols`，XSRF 不拦局域网 IP。

### 实测可用地址格式
- 局域网：`http://10.35.188.132:8501`（同一 WiFi 直接开）
- 公网：`https://xxxx.trycloudflare.com`（临时，窗口一关就失效）

### 已知限制（不是 bug，别去"修"）
- 单实例 + 共享 `history.json`：多人同时用会互相覆盖历史，见 `使用教程.md:246`。
- 数据明文存 `history.json`，断点快照在 `.snapshots/`，公网暴露等于把简历数据放到临时公网地址上。

## 2026-11-07 · v18/v19/v20 全量复验（其他 agent 完成后的一次验收）

### 结论
全通，0 失败。726 个用例、0 个运行时异常、0 条编译错误。

### 复验范围与命令
| 层 | 命令 | 结果 |
|---|---|---|
| 全量单测 | `python -m unittest discover -s . -p "test_*.py"` | 726 tests / OK / 6.5s |
| 语法编译 | `python -m compileall -q . -x "(.tmp\|__pycache__\|vendor)"` | 0 错误 |
| 模块导入 + 数据 + 纯函数 | `.tmp/verify_smoke.py` | 18 项全过 |
| 服务启动 | `python -m streamlit run app.py --server.port=8599` | `/_stcore/health` 200、首页 200 len=6463 |
| 登录 → 主界面 | `.tmp/verify_login_flow.py`（streamlit AppTest） | 0 异常、侧边栏 14 控件、7 个下拉 |
| 并发 / 隔离 / 认领 | `.tmp/verify_concurrency.py` | 12 线程×5 条无丢失、alice/bob 各 30、认领幂等 |

### 新结论（值得记住）
1. **本机无 pytest，测试一律 `unittest`**。`python -m unittest discover` 直接可跑，零新增依赖。
2. **有登录门（v19/v20）**，首屏是期刊封面式双栏，未登录时 `at.sidebar` 为 0 是正常的，别误判成"侧边栏坏了"。
   演示账号按钮 key = `zy_demo_login`。
3. **`history.json` 里 9 条老记录没有 `owner` 字段**：本机（loopback）全可见，远端访问者隐藏——
   这是 `history_store.visible_to` 的既定语义，不是数据损坏。要让外网看到旧记录得走 `claim_local_records`。
4. **签名易猜错的三处**（写验证脚本时踩过，别再猜）：
   `mood.update(state, *, answer, decision, elapsed_min, planned_rounds, cur_q)` 是**关键字专用**；
   `interview_state.next_asked_topics(asked, topic)` 返回 `(列表, 规整串)`，不是三元组；
   `delivery` 的入口是 `answer_metrics(text, elapsed)`、`jd_match` 的入口是 `analyze_jd(...)`。
5. **AppTest 是这项目最划算的端到端验证手段**：能真跑 `app.py` 全脚本并抓 `at.exception`，
   不必起浏览器，也不会调真实 API。

### 真实模型链路（2026-11-07 补验通过）
**此前记录「本机无 API Key」是错的。** 三个 Key 都在 **Windows 用户级环境变量**（`HKCU\Environment`）里：
`DEEPSEEK_API_KEY`（35 位）、`SILICONFLOW_API_KEY`（51 位）、`ZHIPU_API_KEY`（49 位）。
当时的判断只查了 `os.getenv()`，而当前 shell 进程的环境副本早于这些变量写入，于是读到空——
`app.py:115 load_api_key()` 正是为此写了第二层回退（`winreg` 直读注册表），所以线上一直能用。

- 实测 `.tmp/verify_key_live.py`（AppTest）：登录 demo 后「开始面试」`disabled=False`，
  三条标题分别为「🔑 DeepSeek API Key 已从环境变量读取」「🎙️ 硅基流动…」「🔊 智谱…」。
- 实测 `.tmp/verify_live_llm.py`：`build_profile(应届生校招 / 计算机科学与技术 / B / 友善型 / 3 轮)`
  → `interviewer_turn` 跑 3 轮（2.4s / 1.8s / 1.2s）→ `evaluate_interview` 6s，总计 31s。
  第 3 轮在候选人答跑题时回「等一下，我问的是检索结果的融合方式和命中率的评测口径，你答的是缓存」
  并自动转 `action=probe` —— 跑题识别与追问降级都在真模型上生效。
- 评估返回结构：`overall_score / adjusted_score / level / dimensions[5] / rubric_check / highlights / risks / summary / attribution`。
  对一段故意留矛盾的作答（先称 84% 后称 78%、追问不答）给出 **42 分 / 不建议推进**，
  且 `summary` 明确指出自相矛盾与口径缺失 —— 说明一致性核查链路是真的在跑，不是摆设。
- 结论：**真实模型链路可用，全部功能已端到端验证**。本机运行时什么都不用配。

## 2026-11-07 · 公网部署加固（参赛报名表要"链接 + 账号 + 密码"）

### 背景
报名表要求给评委一个可体验的链接与凭证。演示账号是 `demo` / `ZhiyuDemo2026`（`auth.py:61-62`），
但原状下公网开放有四个问题：服务端没 Key（评委看到灰按钮）、谁都能注册烧 Key、
所有评委共用 demo 于是互相看见历史、demo 的断点快照会串到下一个人。

### 改了什么
| 文件 | 改动 | 为什么 |
|---|---|---|
| `app.py` | 新增 `_bridge_secrets()`，在 `st.set_page_config` 后调用 | `load_api_key` 只认 `os.getenv`，而 Streamlit Cloud 只给 `st.secrets`，不通就得在云端永远灰按钮 |
| `app.py` | `_demo_mode()` + `load_records`/`save_record`/`delete_record_at`/`clear_records`/`set_record_outcome` 六个入口分叉 | demo 的记录改存 `st.session_state["demo_records"]`，不落盘 → 评委之间互不可见，且历史/趋势/回填功能照常可演示 |
| `app.py` | `save_snapshot` / `load_snapshot` 对 demo 直接短路 | 快照按 owner 过滤，owner 都是 demo 时"继续上次面试"会接上别人的简历 |
| `app.py` | 注册表单加条件渲染的「访问口令」栏 | 配了 `ZY_ACCESS_CODE` 才出现，没配完全无感（本机自用零影响） |
| `auth.py` | `ACCESS_CODE_ENV` / `access_code_required()` / `verify_access_code()` / `may_save_history()` | 规则独立于 Streamlit，可离线单测；口令走环境变量，不跟仓库走 |
| `test_auth.py` | 新增 `TestAccessCode`（3 例）+ demo 不落盘断言 | 钉住"没配口令时一律放行"这个不能被改坏的契约 |

### 验证
- `python -m unittest discover` → **730 tests OK**（原 726 + 新增 4）。
- `.tmp/verify_deploy_hardening.py`（AppTest）→ 无口令时输入框无「访问口令」，配 `ZY_ACCESS_CODE` 后出现；
  demo 登录后侧边栏 14 控件；6 处代码接线静态核对全中；真实 `history.json` 字节未变。
- `.tmp/verify_secrets_bridge.py` → 造一个临时 `secrets.toml`，确认 `DEEPSEEK_API_KEY` 真被灌进
  `os.environ`，且**已有环境变量时不被覆盖**；测完自动删掉，`.streamlit/` 只剩 `config.toml`。

### 部署结论（下次不要再从头想）
- 临时看效果：`.tools/cloudflared.exe` 已在仓库里，`cloudflared tunnel --url http://127.0.0.1:8501`
  十秒出 HTTPS 链接。**但链接每次重启都变、本机关机即断，不能填进报名表。**
- 正式链接走 Streamlit Community Cloud：仓库推公开 GitHub（`.gitignore` 已挡住 `history.json` /
  `.auth/` / `secrets.toml` / `.env`），平台 Secrets 里填 `DEEPSEEK_API_KEY`、`ZY_ACCESS_CODE`，
  桥接会自动搬进环境变量，评委零配置。
- 演示账号的账号密码**不要指望能改**：`app.py` 里根本没有改密码界面（`change_password` 只被测试调用），
  所以"评委改掉 demo 密码把后面人锁死"这条风险不存在。真要防，等接设置页时再加 `may_change_password`。
