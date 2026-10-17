"""面试官语音动效：磨砂玻璃语音可视化（glasatarjs）。

历史沿革：
- v3.1/v3.2 用 AI 人脸 + 口型 + 眨眼，以及 SadTalker 离线预渲染视频。
- v12 整体换为纯 CSS 声波球（光球 + 扩散环 + 频谱条），零图片依赖。
- v14 引入 [glasatarjs](https://github.com/jimhill/glasatarjs)（MIT）做磨砂玻璃动效，
  纯 CSS 声波球保留为 WebGL 不可用时的兜底。

四条关键设计决策（均为实测结论，见 vendor/README.md 与变更清单）：
1. **音频必须走 WebAudio，不能用 `<audio>.captureStream()`**。
   实测：Blob URL 的 `<audio>` 调 captureStream() 得到 **0 条音频轨**，
   而 `AudioContext → MediaStreamDestination` 稳定得到 1 条。
   因此这里用 `decodeAudioData` + `AudioBufferSourceNode` 播放，
   同时接 `ctx.destination`（出声）与 `MediaStreamDestination`（驱动动效）。
2. **背景用「同色底」而非真透明**。glasatarjs 的片元着色器里写死
   `finalAlpha = u_opacity`，且背景 rgb 被乘上玻璃色，真透明必须改源码。
   改为把背景色设成与页面纸面同一个十六进制值，视觉上完全等同透明。
3. **core.js 内联并改写导出**。`st.iframe` 是独立文档，不能跨文档 import，
   且内联 `<script>` 不能用 `export` 语句，故把 ESM 导出挂到 `window`。
4. **WebGL 不可用时自动回落到纯 CSS 声波球**——面试绝不能因动效失败而中断。
"""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

# glasatarjs 的核心实现（MIT，来源与许可证见 vendor/README.md）
_VENDOR = Path(__file__).with_name("vendor") / "glasatarjs.core.js"

# 暖纸主题配色（与 .streamlit/config.toml 及 app.py 的 CSS 变量一致）
_PAPER = "#F5F0E8"
_ACCENT = "#C2481B"

# 默认玻璃纹理；锤纹在暖纸底上观感最规整
_DEFAULT_TEXTURE = "flemish"

# 兜底声波球的配色（沿用 v12 方案）
_ACCENT_SOFT = "#60a5fa"
_BG = "#0f172a"
_BARS = 7
_BINS = [1, 2, 3, 5, 8, 13, 21]


def _audio_attr(audio: bytes | None) -> str:
    """把 mp3 字节编成 data URI；没有音频时返回空串（不影响 DOM 合法性）。"""
    if not audio:
        return ""
    encoded = base64.b64encode(audio).decode("ascii")
    return f'src="data:audio/mpeg;base64,{encoded}"'


def _audio_src(audio: bytes | None) -> str:
    """只返回 data URI 字符串（WebAudio 的 fetch 需要它）。"""
    if not audio:
        return ""
    return "data:audio/mpeg;base64," + base64.b64encode(audio).decode("ascii")


@lru_cache(maxsize=1)
def _vendor_source() -> str:
    """读取内联的 glasatarjs 核心源码；缺失时返回空串（触发兜底）。

    注意：这里**只**判断文件能否读到，不做任何别的判断，
    以保证缓存语义单纯（文件内容在同一进程内视为不变）。
    """
    try:
        return _VENDOR.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def avatar_component(audio: bytes | None = None) -> str:
    """生成语音动效组件的自包含 HTML。

    audio 为最新一条面试官语音（mp3 字节）。永远返回可用 HTML——
    没有音频时也要显示待机画面，不能返回空串让界面跳动。
    WebGL 或动效库不可用时自动回落到纯 CSS 声波球。
    """
    vendor = _vendor_source()
    if not vendor:
        return _css_ball(audio)
    return _glass_html(vendor, audio)


@lru_cache(maxsize=8)
def _glass_html(vendor: str, audio: bytes | None) -> str:
    """组装磨砂玻璃组件；vendor 源码作为缓存键的一部分。"""
    # 把 ESM 导出改写为挂到 window：内联 <script> 不能用 export 语句。
    # 兼容带分号与不带分号两种写法。
    source = vendor
    for pattern in (
        "export { AudioAnalyzer, GlastarJS };",
        "export { AudioAnalyzer, GlastarJS }",
    ):
        source = source.replace(
            pattern, "window.AudioAnalyzer = AudioAnalyzer; window.GlastarJS = GlastarJS;"
        )

    return (
        _GLASS_TEMPLATE.replace("__VENDOR__", source)
        .replace("__AUDIO_SRC__", _audio_src(audio))
        .replace("__HAS_AUDIO__", "1" if audio else "0")
        .replace("__PAPER__", _PAPER)
        .replace("__ACCENT__", _ACCENT)
        .replace("__TEXTURE__", _DEFAULT_TEXTURE)
    )


# ---------------------------------------------------------------------------
# 主方案：磨砂玻璃（glasatarjs）
# ---------------------------------------------------------------------------

_GLASS_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    overflow: hidden; background: __PAPER__;
    font-family: system-ui, "Segoe UI", "Microsoft YaHei", sans-serif;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
  }
  #stage { position: relative; }
  canvas { display: block; border-radius: 14px; }
  #status {
    margin-top: 8px; font-size: 12px; color: #6B6259;
    min-height: 17px; text-align: center;
  }
  #replay {
    margin-top: 8px; border: 1px solid #E5DCCD; border-radius: 999px;
    padding: 5px 15px; cursor: pointer; background: #fff; color: __ACCENT__;
    font-size: 12px; font-weight: 600;
  }
  #replay:hover { border-color: __ACCENT__; }
  #replay[hidden] { display: none; }
</style>
</head>
<body data-audio="__HAS_AUDIO__">
  <div id="stage"><canvas id="cv" width="300" height="300"></canvas></div>
  <div id="status">等待提问</div>
  <button id="replay" hidden>▶ 重播</button>

<script>
__VENDOR__
</script>
<script>
(function () {
  var hasAudio = document.body.dataset.audio === '1';
  var AUDIO_SRC = '__AUDIO_SRC__';
  var cv = document.getElementById('cv');
  var statusEl = document.getElementById('status');
  var btn = document.getElementById('replay');

  var renderer = null, ctx = null, dest = null, buf = null, srcNode = null;
  var started = false;

  function setStatus(t) { statusEl.textContent = t; }

  // ---- 1) 初始化渲染器 ----
  try {
    if (!window.GlastarJS) { setStatus('动效库未加载'); return; }
    if (!(cv.getContext('webgl2') || cv.getContext('webgl'))) {
      setStatus('当前浏览器不支持 WebGL');
      return;                       // 由外层 Streamlit 决定是否回落
    }
    renderer = new window.GlastarJS(cv, {
      width: 300, height: 300,
      texture: '__TEXTURE__',
      backgroundType: 'color',
      backgroundColor: '__PAPER__',   // 同色底 → 视觉上等同透明
      glassOpacity: 0.92,
      refractionStrength: 22.0,
      blurAmount: 12.0,
      avatarColor: '__ACCENT__',
      avatarSize: 66,
      avatarSensitivity: 1.9,
      avatarExpansion: 50,
      avatarSmoothing: 0.24,
      avatarFadeWithAudio: true,      // 说话才亮，贴合语音对话语义
      backgroundRotation: false,
      backgroundScale: 1.4,
      avatarState: 'listening',
    });
    renderer.start();
  } catch (e) {
    setStatus('动效初始化失败');
    return;
  }

  if (!hasAudio) {
    setStatus('等待提问');
    return;
  }

  // ---- 2) 音频：WebAudio（captureStream 拿不到轨道）----
  function setupAudio() {
    if (ctx) return true;
    try {
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      dest = ctx.createMediaStreamDestination();
      var analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0.72;
      ctx.createMediaStreamSource(dest.stream).connect(analyser);
      renderer.connectAudioStream(dest.stream);   // 喂给渲染器
      return true;
    } catch (e) {
      setStatus('音频初始化失败');
      return false;
    }
  }

  // 解码 mp3 → AudioBuffer（只需一次）
  function decode() {
    if (buf) return Promise.resolve();
    if (!AUDIO_SRC) return Promise.reject(new Error('无音频'));
    return fetch(AUDIO_SRC)
      .then(function (r) { return r.arrayBuffer(); })
      .then(function (ab) {
        return new Promise(function (res, rej) { ctx.decodeAudioData(ab, res, rej); });
      })
      .then(function (b) { buf = b; });
  }

  function playOnce() {
    if (!setupAudio()) return;
    if (ctx.state === 'suspended') { ctx.resume(); }
    decode().then(function () {
      if (!buf) { return; }
      if (srcNode) { try { srcNode.stop(); } catch (e) {} }
      srcNode = ctx.createBufferSource();
      srcNode.buffer = buf;
      srcNode.connect(ctx.destination);   // 出声
      srcNode.connect(dest);              // 同时进动效
      srcNode.onended = function () {
        setStatus('等待你的回答');
        btn.hidden = false;
        btn.textContent = '▶ 重播';
        renderer.updateConfig({ avatarState: 'listening' });
      };
      srcNode.start(0);
      started = true;
      btn.hidden = true;
      setStatus('正在说话…');
      renderer.updateConfig({ avatarState: 'speaking' });
    }).catch(function () {
      btn.hidden = false;
      setStatus('点击播放面试官语音');
    });
  }

  btn.addEventListener('click', playOnce);

  // 首次自动播放：浏览器可能拦截，失败则显示按钮
  setupAudio();
  if (ctx && ctx.state === 'suspended') { ctx.resume(); }
  decode().then(playOnce).catch(function () {
    btn.hidden = false;
    setStatus('点击播放面试官语音');
  });
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 兜底方案：纯 CSS 声波球（v12 方案）
# ---------------------------------------------------------------------------


def _css_ball(audio: bytes | None) -> str:
    bars = "".join("<i></i>" for _ in range(_BARS))
    return (
        _CSS_TEMPLATE.replace("__AUDIO_ATTR__", _audio_attr(audio))
        .replace("__HAS_AUDIO__", "1" if audio else "0")
        .replace("__BARS__", bars)
        .replace("__BINS__", ",".join(str(b) for b in _BINS))
        .replace("__BG__", _BG)
        .replace("__ACCENT_SOFT__", _ACCENT_SOFT)
        .replace("__ACCENT__", _ACCENT)
    )


_CSS_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    overflow: hidden; background: transparent;
    font-family: system-ui, "Segoe UI", "Microsoft YaHei", sans-serif;
    display: flex; align-items: center; justify-content: center;
  }
  .card {
    width: min(100%, 340px); background: __BG__; border-radius: 16px;
    padding: 14px 12px 10px; box-shadow: 0 6px 24px rgba(15, 23, 42, .30);
  }
  .bar { display: flex; align-items: center; gap: 8px; margin: 0 4px 10px; }
  .dot {
    width: 8px; height: 8px; border-radius: 50%; background: #22c55e;
    box-shadow: 0 0 6px rgba(34, 197, 94, .9); flex: none;
  }
  .dot.speaking { animation: pulse 1.1s ease-in-out infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .3; } }
  .title { color: #e2e8f0; font-size: 13px; font-weight: 600; }
  .badge { margin-left: auto; color: #94a3b8; font-size: 11px; }
  .stage {
    position: relative; width: 100%; height: 150px;
    display: flex; align-items: center; justify-content: center;
  }
  .ring {
    position: absolute; left: 50%; top: 50%; width: 104px; height: 104px;
    margin: -52px 0 0 -52px; border-radius: 50%; border: 2px solid __ACCENT_SOFT__;
    opacity: 0; transform: scale(.9); will-change: transform, opacity;
  }
  .ring.b { border-style: dashed; }
  .halo {
    position: absolute; left: 50%; top: 50%; width: 116px; height: 116px;
    margin: -58px 0 0 -58px; border-radius: 50%;
    background: radial-gradient(circle, rgba(96,165,250,.55) 0%, rgba(59,130,246,0) 68%);
    opacity: .18; transform: scale(1);
  }
  .orb {
    position: relative; width: 84px; height: 84px; border-radius: 50%;
    background: radial-gradient(circle at 34% 30%, #bcd7ff 0%, __ACCENT__ 52%, #1d4ed8 100%);
    box-shadow: 0 0 0 6px rgba(59,130,246,.14), 0 10px 28px rgba(29,78,216,.42);
    animation: breathe 4.6s ease-in-out infinite;
  }
  @keyframes breathe {
    0%, 100% { transform: translateY(-2px) scale(1); }
    50%      { transform: translateY(2px)  scale(1.035); }
  }
  .orb::after {
    content: ""; position: absolute; left: 20%; top: 16%;
    width: 26%; height: 20%; border-radius: 50%;
    background: rgba(255,255,255,.55); filter: blur(3px);
  }
  .wave {
    display: flex; align-items: flex-end; justify-content: center;
    gap: 4px; height: 30px; margin-top: 8px;
  }
  .wave i {
    width: 4px; height: 100%; border-radius: 3px; background: __ACCENT_SOFT__;
    transform: scaleY(.14); transform-origin: 50% 100%;
    transition: transform .08s linear;
  }
  .foot {
    display: flex; align-items: center; justify-content: space-between;
    gap: 8px; margin: 8px 4px 2px; min-height: 24px;
  }
  #status { color: #cbd5e1; font-size: 12px; }
  #replay {
    border: 0; border-radius: 8px; padding: 5px 12px; cursor: pointer;
    background: __ACCENT__; color: #fff; font-size: 12px; font-weight: 600;
  }
  #replay[hidden] { display: none; }
</style>
</head>
<body data-audio="__HAS_AUDIO__">
  <div class="card">
    <div class="bar">
      <span class="dot" id="dot"></span>
      <span class="title">知遇</span>
      <span class="badge">AI 虚拟面试官</span>
    </div>
    <div class="stage">
      <div class="halo" id="halo"></div>
      <div class="ring" id="ring"></div>
      <div class="ring b" id="ring-b"></div>
      <div class="orb" id="orb"></div>
    </div>
    <div class="wave" id="wave">__BARS__</div>
    <div class="foot">
      <span id="status">等待提问</span>
      <button id="replay" hidden>▶ 播放</button>
    </div>
  </div>
  <audio id="tts" preload="auto" __AUDIO_ATTR__></audio>
<script>
(function () {
  var hasAudio = document.body.dataset.audio === '1';
  var audio = document.getElementById('tts');
  var halo = document.getElementById('halo');
  var orb = document.getElementById('orb');
  var ring = document.getElementById('ring');
  var ringB = document.getElementById('ring-b');
  var bars = document.querySelectorAll('#wave i');
  var statusEl = document.getElementById('status');
  var btn = document.getElementById('replay');
  var dot = document.getElementById('dot');
  var BINS = [__BINS__];
  var ctx = null, analyser = null, freq = null;
  var speaking = false, level = 0, startedOnce = false;

  function ensureGraph() {
    if (analyser || !hasAudio) { return; }
    try {
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      var source = ctx.createMediaElementSource(audio);
      analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0.72;
      source.connect(analyser);
      analyser.connect(ctx.destination);
      freq = new Uint8Array(analyser.frequencyBinCount);
    } catch (err) { analyser = null; }
  }

  function tick() {
    var lvl = 0;
    if (analyser && speaking && freq) {
      analyser.getByteFrequencyData(freq);
      for (var i = 0; i < bars.length; i++) {
        var v = freq[BINS[i]] / 255;
        bars[i].style.transform = 'scaleY(' + (0.14 + v * 0.86).toFixed(3) + ')';
      }
      var sum = 0, n = 40;
      for (var j = 0; j < n; j++) { sum += freq[j] / 255; }
      lvl = Math.min(1, (sum / n) * 2.6);
    } else {
      for (var k = 0; k < bars.length; k++) {
        bars[k].style.transform = 'scaleY(0.14)';
      }
    }
    level = Math.max(lvl, level * 0.72);
    orb.style.transform = 'scale(' + (1 + level * 0.34).toFixed(3) + ')';
    halo.style.transform = 'scale(' + (1 + level * 0.5).toFixed(3) + ')';
    halo.style.opacity = (0.18 + level * 0.62).toFixed(2);
    ring.style.transform = 'scale(' + (1 + level * 0.55).toFixed(3) + ')';
    ring.style.opacity = Math.min(0.55, level * 0.75).toFixed(2);
    ringB.style.transform = 'scale(' + (1 + level * 0.95).toFixed(3) + ')';
    ringB.style.opacity = Math.min(0.30, level * 0.45).toFixed(2);
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);

  function setSpeaking(on) {
    speaking = on;
    if (on) { ensureGraph(); }
    if (ctx && ctx.state === 'suspended') { ctx.resume(); }
    dot.classList.toggle('speaking', on);
    statusEl.textContent = on ? '正在说话…' : (hasAudio ? '等待你的回答' : '等待提问');
    btn.textContent = startedOnce ? '▶ 重播' : '▶ 播放';
    btn.hidden = on || !hasAudio;
  }

  if (hasAudio) {
    audio.addEventListener('playing', function () { startedOnce = true; setSpeaking(true); });
    audio.addEventListener('ended', function () { setSpeaking(false); });
    audio.addEventListener('pause', function () { setSpeaking(false); });
    btn.addEventListener('click', function () { audio.currentTime = 0; audio.play(); });
    var attempt = audio.play();
    if (attempt && attempt.catch) {
      attempt.catch(function () {
        btn.hidden = false;
        statusEl.textContent = '点击播放面试官语音';
      });
    }
  }
})();
</script>
</body>
</html>
"""
