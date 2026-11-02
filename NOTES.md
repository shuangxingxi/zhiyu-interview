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
