"""面试官语音动效：声波球（纯前端，零新增依赖）。

设计要点：
- **不出现人脸**。此前方案用 AI 生成的人像 + 口型 / 眨眼 / 呼吸，现已整体替换为
  一类「豆包语音对话」式的动效：画面中央一个会呼吸的光球，说话时被 Web Audio
  实时解析出的音量驱动——球体随响度扩张、外圈光晕扩散、底部声波条随频谱跳动。
- **零依赖、离线可用**：整段组件是自包含 HTML（内联 CSS + JS），不起额外服务、
  不读任何图片文件、不需要 Pillow。此前的人像裁切 / base64 内嵌逻辑一并移除。
- **只驱动动效，不驱动口型**：不做音素对齐，也不做唇形生成——只用音量与频谱
  表达"正在说话"这件事。这正是"语音助手动效"与"数字人对口型"的分野。
- 组件只内嵌**最新一条**面试官语音；HTML 内容不变时 Streamlit 不会重载 iframe，
  所以同一句话在页面刷新时不会被重新加载、更不会从头重播。
- 自动播放被浏览器拦截时，组件内降级为「▶ 播放」按钮，面试主流程不受影响。
- 音频分析失败（浏览器不支持 / 跨域限制）时静默退化为**纯呼吸动效**，不影响听。
"""

from __future__ import annotations

import base64

# 颜色：与 Streamlit light 主题协调，不刺眼
_ACCENT = "#3b82f6"       # 主蓝
_ACCENT_SOFT = "#60a5fa"  # 浅蓝（光晕）
_BG = "#0f172a"           # 卡片底色（深色更衬光球）

# 声波条数量（与模板里的 <i> 个数一致）
_BARS = 7
# 应对应 bars 的中低频桶索引（人声能量集中在中低频）
_BINS = [1, 2, 3, 5, 8, 13, 21]


def _audio_attr(audio: bytes | None) -> str:
    """把 mp3 字节编成 data URI；没有音频时返回空串（不影响 DOM 合法性）。"""
    if not audio:
        return ""
    encoded = base64.b64encode(audio).decode("ascii")
    return f'src="data:audio/mpeg;base64,{encoded}"'


def avatar_component(audio: bytes | None = None) -> str:
    """生成声波球组件的自包含 HTML。

    audio 为最新一条面试官语音（mp3 字节）；为 None 时只显示待机呼吸动效。
    永远返回可用 HTML——没有音频也一样要显示静止球体，不能返回空串让界面跳动。
    """
    bars = "".join("<i></i>" for _ in range(_BARS))
    return _TEMPLATE.replace("__AUDIO_ATTR__", _audio_attr(audio)).replace(
        "__HAS_AUDIO__", "1" if audio else "0"
    ).replace("__BARS__", bars).replace("__BINS__", ",".join(str(b) for b in _BINS))


_TEMPLATE = """<!DOCTYPE html>
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

  /* 舞台：球体与两圈光晕居中叠放 */
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
    opacity: .18; transform: scale(1); will-change: transform, opacity;
  }
  .orb {
    position: relative; width: 84px; height: 84px; border-radius: 50%;
    background: radial-gradient(circle at 34% 30%, #bcd7ff 0%, __ACCENT__ 52%, #1d4ed8 100%);
    box-shadow: 0 0 0 6px rgba(59,130,246,.14), 0 10px 28px rgba(29,78,216,.42);
    animation: breathe 4.6s ease-in-out infinite;
    will-change: transform;
  }
  @keyframes breathe {
    0%, 100% { transform: translateY(-2px) scale(1); }
    50%      { transform: translateY(2px)  scale(1.035); }
  }
  /* 高光：让球体有体积感 */
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
    transition: transform .08s linear; will-change: transform;
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
  #replay:hover { background: #2563eb; }
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

  // 应对应 bars 数量的中低频桶索引（人声能量集中在中低频）
  var BINS = [__BINS__];

  // ---- 音量/频谱驱动：不做音素对齐，只表达"正在说话" ----
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
    } catch (err) {
      analyser = null;   // 分析失败 → 退化为纯呼吸动效，不影响听
    }
  }

  function tick() {
    var lvl = 0;
    if (analyser && speaking && freq) {
      analyser.getByteFrequencyData(freq);
      for (var i = 0; i < bars.length; i++) {
        var v = freq[BINS[i]] / 255;
        bars[i].style.transform = 'scaleY(' + (0.14 + v * 0.86).toFixed(3) + ')';
      }
      var sum = 0, n = 40;   // 低频段能量均值 ≈ 整体响度
      for (var j = 0; j < n; j++) { sum += freq[j] / 255; }
      lvl = Math.min(1, (sum / n) * 2.6);
    } else {
      for (var k = 0; k < bars.length; k++) {
        bars[k].style.transform = 'scaleY(0.14)';
      }
    }
    level = Math.max(lvl, level * 0.72);   // 快起慢落

    var s = 1 + level * 0.34;
    orb.style.transform = 'scale(' + s.toFixed(3) + ')';
    halo.style.transform = 'scale(' + (1 + level * 0.5).toFixed(3) + ')';
    halo.style.opacity = (0.18 + level * 0.62).toFixed(2);

    // 两圈扩散环：说话时向外推，并随音量淡出
    var r1 = 1 + level * 0.55;
    ring.style.transform  = 'scale(' + r1.toFixed(3) + ')';
    ring.style.opacity    = Math.min(0.55, level * 0.75).toFixed(2);
    var r2 = 1 + level * 0.95;
    ringB.style.transform = 'scale(' + r2.toFixed(3) + ')';
    ringB.style.opacity   = Math.min(0.30, level * 0.45).toFixed(2);

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
    audio.addEventListener('ended',  function () { setSpeaking(false); });
    audio.addEventListener('pause',  function () { setSpeaking(false); });
    btn.addEventListener('click', function () {
      audio.currentTime = 0;
      audio.play();
    });
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
""".replace("__BG__", _BG).replace("__ACCENT_SOFT__", _ACCENT_SOFT).replace("__ACCENT__", _ACCENT)
