"""面试官语音动效：磨砂玻璃语音可视化（glasatarjs）。

历史沿革：
- v3.1/v3.2 用 AI 人脸 + 口型 + 眨眼，以及 SadTalker 离线预渲染视频。
- v12 整体换为纯 CSS 声波球（光球 + 扩散环 + 频谱条），零图片依赖。
- v14 引入 [glasatarjs](https://github.com/jimhill/glasatarjs)（MIT）做磨砂玻璃动效，
  纯 CSS 声波球保留为 WebGL 不可用时的兜底。
- v14.1 修掉「WebGL 不可用 → 面试官整场失声」：原实现发现 WebGL 不可用后
  只写一行提示就 `return`，兜底从未真正发生过（2026-10-18 报障）。
- v14.2 修掉「有声音但说话没有动效」和「动效顶部被遮挡、重播按钮下半截被裁」：
  兜底态拿不到频谱时改用合成包络驱动画面，卡片缩放比例与玻璃路径的画布高度
  都不再写死（2026-10-18 报障）。
- v14.3 修掉玻璃路径「语音播出来了，画布却纹丝不动」。两个独立原因：
  ① glasatarjs 的 AudioAnalyzer 在构造函数里自建 AudioContext，而且**从不**
  resume()——iframe 里这个 ctx 被自动播放策略挂起后，`getByteTimeDomainData`
  读到的全是静默数据，audioLevel 恒为 0，于是 avatarState='speaking' 下
  expandedSize 不变、avatarFadeWithAudio 的 opacity 恒为 0，画面完全静止。
  ② avatarExpansion 原为 50（即最大 50 倍），而画布只有 300px：任何一点电平都会
  把方块一步顶到 3000px 铺满画布，之后剩下的电平变化全落在画面之外，看着就是
  一块不动的橙红方块。现在播放时补一次 resume，expansion 收到 2.5，并且加一圈
  由组件自己的 analyser（它在出声的那个 ctx 里，一定拿得到真实频谱）驱动的
  内嵌光晕——有声音就必须有看得见的动效（2026-10-18 报障）。

七条关键设计决策（均为实测结论，见 vendor/README.md 与变更清单）：
1. **玻璃路径的音频走 WebAudio，不能用 `<audio>.captureStream()`**。
   实测：Blob URL 的 `<audio>` 调 captureStream() 得到 **0 条音频轨**，
   而 `AudioContext → MediaStreamDestination` 稳定得到 1 条。
   因此这里用 `decodeAudioData` + `AudioBufferSourceNode` 播放，
   同时接 `ctx.destination`（出声）与 `MediaStreamDestination`（驱动动效）。
2. **背景用「同色底」而非真透明**。glasatarjs 的片元着色器里写死
   `finalAlpha = u_opacity`，且背景 rgb 被乘上玻璃色，真透明必须改源码。
   改为把背景色设成与页面纸面同一个十六进制值，视觉上完全等同透明。
3. **core.js 内联并改写导出**。`st.iframe` 是独立文档，不能跨文档 import，
   且内联 `<script>` 不能用 `export` 语句，故把 ESM 导出挂到 `window`。
4. **WebGL 不可用时回落到纯 CSS 声波球**，且回落必须发生在**组件内部**。
   `st.iframe` 是独立文档，外层 Streamlit 拿不到「浏览器支不支持 WebGL」，
   无从替它决定——所以兜底只能由组件自己发现、自己切换。
5. **音频播放不得挂在动效链路上**（v14.1）。画面可以降级，声音不可以：
   app.py 在动效组件可见时会把消息上的 `st.audio` 自动播放关掉，
   于是这里一失声就是整场静音。玻璃路径与兜底路径各自负责出声，
   WebGL 或 AudioContext 任何一环不可用都整体切到兜底，由它用
   `<audio>` 原生播放（连取频谱的 WebAudio 都只做「取了才接管输出」的降级处理）。
6. **动效也不能因为拿不到频谱就静止，缩放的余地也不能写死**（v14.2）。
   `<audio>` 出声与"取到频谱"是两件事：iframe 里 AudioContext 常被自动播放
   策略挂起，挂起时接管输出等于静音，所以只能放弃频谱——这时用合成包络
   驱动画面（2026-10-18 报障「有声音但说话没有动效」）。同理，卡片缩放比例
   不能写死在 CSS 里：iframe 高度是 app.py 里的一个常数，而卡片自然高度随
   列宽与字体度量浮动，写死就会出现「动效上半截被遮挡、重播按钮下半截被裁」。
   现在由 JS 运行时量测后等比缩放（`fitCard`），玻璃路径的画布高度改由
   flex 分配而不是 `calc(100% - 26px)`——后者没算播放结束后才出现的重播按钮。
7. **动效的频谱要取自"正在出声的那个 AudioContext"**（v14.3）。glasatarjs 自带
   的 AudioAnalyzer 会另建一个 AudioContext 且从不 resume()，被挂起时它读到
   的全是静默数据，渲染器于是把动效画成了一块不动的色块（2026-10-18 报障
   「语音输出的时候还是没有动效」）。所以：播放时顺手 resume 它那份 ctx，同时
   给画布套一圈由组件自己 analyser 驱动的内嵌光晕作为保底——出得了声，就说明
   这个 ctx 一定在跑，它的频谱一定拿得到，动效就一定有。另：`avatarExpansion`
   不能给大值，50 倍会让方块一帧铺满 300px 画布，之后电平怎么变都看不出。
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
# _ACCENT 的 rgba 分量：玻璃路径的内嵌光晕要用 rgba() 写 box-shadow，
# 单独列出来，免得同一个颜色在模板里再写死一遍。
_ACCENT_RGB = "194, 72, 27"

# 默认玻璃纹理；锤纹在暖纸底上观感最规整
_DEFAULT_TEXTURE = "flemish"

# 兜底声波球的配色（沿用 v12 方案）
_ACCENT_SOFT = "#60a5fa"
_BG = "#0f172a"
_BARS = 7
_BINS = [1, 2, 3, 5, 8, 13, 21]


def _audio_mime(audio: bytes) -> str:
    """按容器判 MIME：智谱给 WAV，硅基流动/edge 给 MP3。

    WebAudio 的 decodeAudioData 其实不看 MIME，但 <audio> 的 data URI 会看，
    写死 audio/mpeg 播 WAV 有的浏览器会静默不出声。
    """
    return "audio/wav" if bytes(audio[:4]) == b"RIFF" else "audio/mpeg"


def _audio_attr(audio: bytes | None) -> str:
    """把音频字节编成 data URI；没有音频时返回空串（不影响 DOM 合法性）。"""
    if not audio:
        return ""
    encoded = base64.b64encode(audio).decode("ascii")
    return f'src="data:{_audio_mime(audio)};base64,{encoded}"'


def _audio_src(audio: bytes | None) -> str:
    """只返回 data URI 字符串（WebAudio 的 fetch 需要它）。"""
    if not audio:
        return ""
    return f"data:{_audio_mime(audio)};base64," + base64.b64encode(audio).decode("ascii")


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
    vendor 源码读不到时直接给纯 CSS 声波球；读到则由组件自己在运行时
    判断 WebGL / AudioContext 是否可用，不可用就切到同一套声波球。
    """
    vendor = _vendor_source()
    if not vendor:
        return _css_ball(audio)
    return _glass_html(vendor, audio)


def _sub(template: str, subs: dict[str, str]) -> str:
    """按 token 表替换模板占位符。

    显式列全每一项，漏一个就会把 `__XXX__` 原样渲染到页面上——
    所以这里用一个 dict 把替换集中写在一处，方便对照模板自检。
    """
    out = template
    for token, value in subs.items():
        out = out.replace(token, value)
    return out


@lru_cache(maxsize=1)
def _ball_parts() -> tuple[str, str]:
    """兜底声波球的（标记, 脚本）。

    玻璃模板与纯 CSS 模板共用这一份，避免两处重复维护同一段画法。
    """
    bars = "".join("<i></i>" for _ in range(_BARS))
    markup = _BALL_MARKUP.replace("__BARS__", bars)
    script = _BALL_SCRIPT.replace("__BINS__", ", ".join(str(b) for b in _BINS))
    return markup, script


def _ball_style_subs() -> dict[str, str]:
    """兜底声波球样式里的配色占位符（两个模板都要替）。"""
    return {
        "__BG__": _BG,
        "__ACCENT_SOFT__": _ACCENT_SOFT,
        "__ACCENT__": _ACCENT,
        "__ACCENT_RGB__": _ACCENT_RGB,
    }


def _css_ball(audio: bytes | None) -> str:
    """兜底组件：纯 CSS 声波球（不依赖 WebGL，也不需要 vendor 源码）。"""
    markup, script = _ball_parts()
    return _sub(
        _BALL_DOC_TEMPLATE,
        {
            "__BALL_STYLE__": _BALL_STYLE,
            "__BALL_MARKUP__": markup,
            "__BALL_SCRIPT__": script,
            "__AUDIO_ATTR__": _audio_attr(audio),
            # 这个模板直接调 __ballBoot，所以给 JS 布尔字面量；
            # 玻璃模板要的是 HTML 属性值（"1"/"0"），两个 token 分开避免歧义
            "__HAS_AUDIO_JS__": "true" if audio else "false",
            **_ball_style_subs(),
        },
    )


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

    markup, script = _ball_parts()
    return _sub(
        _GLASS_TEMPLATE,
        {
            "__BALL_STYLE__": _BALL_STYLE,
            "__BALL_MARKUP__": markup,
            "__BALL_SCRIPT__": script,
            "__AUDIO_SRC__": _audio_src(audio),
            "__HAS_AUDIO__": "1" if audio else "0",
            # 没有音频时也停在 listening：thinking 态只在画布边缘画旋转描边
            # （vendor 用 padding = -5，实际几乎被裁掉），头像层又因 opacity 绑在
            # speaking 上而不可见，整块画布接近纯白，看着像加载失败；listening
            # 至少能把暖橙纹理画满，和有音频时的空档态一致。
            "__AVATAR_STATE__": "listening",
            "__PAPER__": _PAPER,
            "__TEXTURE__": _DEFAULT_TEXTURE,
            **_ball_style_subs(),
            # vendor 源码最后插入：它的内容是第三方原文，不参与任何占位符替换
            "__VENDOR__": source,
        },
    )


# ---------------------------------------------------------------------------
# 兜底方案：纯 CSS 声波球（v12 方案）
# ---------------------------------------------------------------------------

# 声波球的画法（球体 + 扩散环 + 频谱条）在两个模板里共用；
# 只有外层容器与「谁调 __ballBoot」不同。
_BALL_STYLE = """  /* 纯 CSS 声波球：不依赖 WebGL，用 <audio> 原生播放 */
  #ball-ui {
    /* 缩放比例不写死：st.iframe 的高度是一处写死的数字（app.py 里的 height=），
       而卡片自然高度随列宽与字体度量浮动，写死 0.68 就会出现「动效上半截和
       重播按钮下半截都被裁掉」（2026-10-18 报障）。改由脚本运行时量测后
       等比缩放，见 __ballBoot 里的 fitCard。 */
    transform-origin: center center; max-width: 100%;
  }
  #ball-ui[hidden] { display: none; }
  .card {
    width: min(100%, 340px); min-width: 0; background: __BG__; border-radius: 16px;
    padding: 12px 12px 8px; box-shadow: 0 6px 24px rgba(15, 23, 42, .30);
  }
  .bar { display: flex; align-items: center; gap: 8px; margin: 0 4px 8px; min-width: 0; }
  .dot {
    width: 8px; height: 8px; border-radius: 50%; background: #22c55e;
    box-shadow: 0 0 6px rgba(34, 197, 94, .9); flex: none;
  }
  .dot.speaking { animation: pulse 1.1s ease-in-out infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .3; } }
  /* 标题与徽标都不许换行：一换行卡片就高一截，在窄列里正好把上下两端顶出视口 */
  .title {
    color: #e2e8f0; font-size: 13px; font-weight: 600;
    flex: none; white-space: nowrap;
  }
  .badge {
    margin-left: auto; color: #94a3b8; font-size: 11px;
    flex: 0 1 auto; min-width: 0; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
  }
  .stage {
    position: relative; width: 100%; height: 140px;
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
    gap: 4px; height: 26px; margin-top: 6px;
  }
  .wave i {
    width: 4px; height: 100%; border-radius: 3px; background: __ACCENT_SOFT__;
    transform: scaleY(.14); transform-origin: 50% 100%;
    transition: transform .08s linear;
  }
  .foot {
    display: flex; align-items: center; justify-content: space-between;
    gap: 8px; margin: 6px 4px 0; min-height: 22px;
  }
  #ball-status {
    color: #cbd5e1; font-size: 12px;
    flex: 1 1 auto; min-width: 0; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
  }
  #ball-replay {
    border: 0; border-radius: 8px; padding: 5px 12px; cursor: pointer;
    background: __ACCENT__; color: #fff; font-size: 12px; font-weight: 600;
    flex: none; white-space: nowrap;
  }
  #ball-replay[hidden] { display: none; }
"""

_BALL_MARKUP = """<div class="card">
  <div class="bar">
    <span class="dot" id="ball-dot"></span>
    <span class="title">知遇</span>
    <span class="badge">AI 虚拟面试官</span>
  </div>
  <div class="stage">
    <div class="halo" id="ball-halo"></div>
    <div class="ring" id="ball-ring"></div>
    <div class="ring b" id="ball-ring-b"></div>
    <div class="orb" id="ball-orb"></div>
  </div>
  <div class="wave" id="ball-wave">__BARS__</div>
  <div class="foot">
    <span id="ball-status">等待提问</span>
    <button id="ball-replay" hidden>▶ 播放</button>
  </div>
</div>"""

# 一段只定义、不自执行的脚本：两个模板各自决定什么时候调用它。
# 玻璃模板在 WebGL/AudioContext 不可用时调它，纯 CSS 模板直接调它。
_BALL_SCRIPT = """window.__ballBoot = function (hasAudio) {
  var audio = document.getElementById('tts');
  var halo = document.getElementById('ball-halo');
  var orb = document.getElementById('ball-orb');
  var ring = document.getElementById('ball-ring');
  var ringB = document.getElementById('ball-ring-b');
  var bars = document.querySelectorAll('#ball-wave i');
  var statusEl = document.getElementById('ball-status');
  var btn = document.getElementById('ball-replay');
  var dot = document.getElementById('ball-dot');
  var ui = document.getElementById('ball-ui');
  var BINS = [__BINS__];
  var ctx = null, analyser = null, freq = null;
  var speaking = false, level = 0, startedOnce = false;

  // 接 WebAudio 只为了取频谱驱动动效，但 MediaElementSource 一旦接管，
  // <audio> 的声音就只走 WebAudio——AudioContext 被自动播放策略挂起时，
  // 那等于把声音掐了。所以：确认 ctx 真的在跑，才接管输出。
  function capture() {
    if (analyser) { return; }
    try {
      var source = ctx.createMediaElementSource(audio);
      analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0.72;
      source.connect(analyser);
      analyser.connect(ctx.destination);
      freq = new Uint8Array(analyser.frequencyBinCount);
    } catch (err) { analyser = null; }
  }

  function ensureGraph() {
    if (analyser || !hasAudio) { return; }
    if (!ctx) {
      try { ctx = new (window.AudioContext || window.webkitAudioContext)(); }
      catch (err) { return; }
    }
    if (ctx.state === 'running') { capture(); return; }
    var resumed = ctx.resume();
    if (resumed && resumed.then) {
      resumed.then(function () {
        if (ctx.state === 'running') { capture(); }
      }, function () {});
    }
  }

  // 卡片自然高度约 250px，iframe 只给 190~220px。缩放比例不能写死在 CSS 里：
  // 列宽、字体度量、徽标是否折行都会让卡片高度浮动，写死的比例必然在某些
  // 情况下把卡片顶出可视区——用户看到的就是「动效上半截被遮挡、重播按钮
  // 下半部分被裁掉」（2026-10-18 报障）。这里量测后等比缩放，留 10px 余量，
  // 顺便给扩散环留出溢出空间。
  function fitCard() {
    if (!ui) { return; }
    ui.style.transform = 'none';
    var h = ui.getBoundingClientRect().height;
    var avail = document.documentElement.clientHeight - 10;
    if (h > 0 && avail > 0) {
      ui.style.transform = 'scale(' + Math.min(1, avail / h).toFixed(4) + ')';
    }
  }

  // 合成包络：几路相位错开的正弦，只求"看得出在说话"，不追求贴合真实语音。
  function envelope(t) {
    for (var m = 0; m < bars.length; m++) {
      var w = 0.5 + 0.5 * Math.sin(t * (5.2 + m * 1.37) + m * 0.9);
      bars[m].style.transform = 'scaleY(' + (0.18 + w * 0.78).toFixed(3) + ')';
    }
    return Math.min(1, 0.28 + 0.22 * (0.5 + 0.5 * Math.sin(t * 3.1)));
  }

  function tick() {
    var lvl = 0, real = false;
    if (analyser && speaking && freq) {
      analyser.getByteFrequencyData(freq);
      var sum = 0, n = 40;
      for (var j = 0; j < n; j++) { sum += freq[j] / 255; }
      lvl = Math.min(1, (sum / n) * 2.6);
      // 图谱接上了，读数却一直是 0：说明这段音频并没有真的在输出（设备静音、
      // 内核没在渲染等）。这和"拿不到频谱"是一回事——画面不能就这么干着。
      real = lvl > 0.02;
      if (real) {
        for (var i = 0; i < bars.length; i++) {
          var v = freq[BINS[i]] / 255;
          bars[i].style.transform = 'scaleY(' + (0.14 + v * 0.86).toFixed(3) + ')';
        }
      }
    }
    if (!real) {
      // 拿不到频谱（iframe 里 AudioContext 常被自动播放策略挂起，而挂起时接管
      // <audio> 输出就等于静音，所以不敢接管）也不能静止着——声音不能为了动效
      // 让步，动效也不能因为拿不到频谱就整场不动。取到真频谱后自动切回上面。
      if (speaking) {
        lvl = envelope(Date.now() / 1000);
      } else {
        for (var k = 0; k < bars.length; k++) {
          bars[k].style.transform = 'scaleY(0.14)';
        }
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
    if (on) {
      // 挂起状态下 resume() 可能要到用户手势之后才生效，说一句的工夫多试几次，
      // 能升级到真频谱就升级，升不了也有合成包络顶着，画面不会静止。
      ensureGraph();
      setTimeout(ensureGraph, 400);
      setTimeout(ensureGraph, 1200);
    }
    dot.classList.toggle('speaking', on);
    statusEl.textContent = on ? '正在说话…' : (hasAudio ? '等待你的回答' : '等待提问');
    btn.textContent = startedOnce ? '▶ 重播' : '▶ 播放';
    btn.hidden = on || !hasAudio;
  }

  fitCard();
  window.addEventListener('resize', fitCard);
  // 用户一点（例如点重播）就有了手势，这时再接管音频输出不会被挂起
  document.addEventListener('click', ensureGraph);

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
};"""

_BALL_DOC_TEMPLATE = """<!DOCTYPE html>
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
__BALL_STYLE__
</style>
</head>
<body>
<div id="ball-ui">
__BALL_MARKUP__
</div>
<audio id="tts" preload="auto" __AUDIO_ATTR__></audio>
<script>
__BALL_SCRIPT__
window.__ballBoot(__HAS_AUDIO_JS__);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 主方案：磨砂玻璃（glasatarjs），WebGL 不可用时切到上面的声波球
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
    gap: 8px; padding: 3px;
  }
  /* 画布占满「状态行 + 重播按钮」之外的全部高度。
     原来是 max-height: calc(100% - 26px)，只扣了状态行——播放结束后
     #replay 出现，内容比 iframe 高出约 35px，而 body 是居中布局，
     于是画布顶端与按钮底部各被裁掉一截（2026-10-18 报障：
     动效上面被遮挡、重播按钮下半部分被裁）。
     注意 #stage 在回落时会被 hidden 属性隐藏，而下面的 display: flex 是作者
     样式、优先级高于 UA 的 [hidden]，所以必须显式补一条 #stage[hidden]。 */
  #stage {
    position: relative; flex: 1 1 auto; min-height: 0; min-width: 0;
    width: 100%; display: flex; align-items: center; justify-content: center;
  }
  #stage[hidden] { display: none; }
  canvas { display: block; border-radius: 14px; max-width: 100%; max-height: 100%; height: auto; width: auto; }
  /* 内嵌光晕：贴着画布边缘往里亮的一圈，由组件自己的 analyser 驱动。
     渲染器那套音频分析一旦拿不到数据（它自建的 AudioContext 从不 resume()），
     画布会是一块不动的色块，这圈光晕就是「有声音一定有动效」的保底。
     绝对定位 + 只向内发光，不占布局、不向外溢出，不会碰到裁剪问题。 */
  #rim {
    position: absolute; border-radius: 14px; pointer-events: none;
    border: 2px solid __ACCENT__; opacity: 0;
    will-change: opacity, border-width, box-shadow;
  }
  #rim[hidden] { display: none; }
  #status { flex: none; font-size: 12px; color: #6B6259; text-align: center; }
  #status[hidden] { display: none; }
  #replay {
    flex: none; border: 1px solid #E5DCCD; border-radius: 999px;
    padding: 5px 15px; cursor: pointer; background: #fff; color: __ACCENT__;
    font-size: 12px; font-weight: 600;
  }
  #replay:hover { border-color: __ACCENT__; }
  #replay[hidden] { display: none; }
__BALL_STYLE__
</style>
</head>
<body data-audio="__HAS_AUDIO__">
  <div id="stage"><canvas id="cv" width="300" height="300"></canvas><div id="rim" hidden></div></div>
  <div id="status">等待提问</div>
  <button id="replay" hidden>▶ 重播</button>
  <div id="ball-ui" hidden>
__BALL_MARKUP__
  </div>
  <audio id="tts" preload="auto"></audio>

<script>
__VENDOR__
</script>
<script>
__BALL_SCRIPT__
</script>
<script>
(function () {
  var hasAudio = document.body.dataset.audio === '1';
  var AUDIO_SRC = '__AUDIO_SRC__';
  var cv = document.getElementById('cv');
  var statusEl = document.getElementById('status');
  var btn = document.getElementById('replay');
  var stage = document.getElementById('stage');
  var rim = document.getElementById('rim');

  var renderer = null, ctx = null, dest = null, buf = null, srcNode = null;
  // 画面侧的取样与状态。ana 挂在"正在出声的那个 AudioContext"里（见 setupAudio），
  // 所以只要听得到声音，这里就一定能拿到真实频谱——动效因此不可能静止。
  var ana = null, anaData = null, speaking = false, glow = 0;
  // 每帧算一次的有效电平：渲染器与内嵌光晕共用这一个值，避免两处各读一次频谱。
  var currentLevel = 0;

  // 渲染器只认 audioAnalyzer 的两个成员：渲染循环里的 getAudioData()（vendor
  // L469-472，且只取返回值的 .level）与 dispose()（L1455-1457）。用一个读
  // currentLevel 的代理顶替它，视觉、着色器、纹理一行都不用改。
  //
  // 注意：这段必须定义在 installLevelProxy() 调用之前。`var` 只提升声明、
  // 不提升赋值，若把它写到调用点后面，installLevelProxy 拿到的 LEVEL_STUB
  // 还是 undefined，渲染循环就退回默认的 {level:0}——画面永远不动。
  var LEVEL_STUB = {
    getAudioData: function () { return { level: currentLevel }; },
    dispose: function () {},
  };
  function installLevelProxy() {
    if (!renderer) { return; }
    renderer.audioAnalyzer = LEVEL_STUB;
  }

  function setStatus(t) { statusEl.textContent = t; }

  // 光晕要正好压在画布上：画布是 flex 居中、还被 max-height 缩放过的，
  // 尺寸只能运行时量，写死在 CSS 里必然对不上。首次量测常常早于布局落定
  // （这时画布还是 0 宽），所以由 tick 定期复量一次。
  function placeRim() {
    if (!rim || !cv || !stage) { return; }
    var r = cv.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) { return; }
    var s = stage.getBoundingClientRect();
    rim.style.left = (r.left - s.left) + 'px';
    rim.style.top = (r.top - s.top) + 'px';
    rim.style.width = r.width + 'px';
    rim.style.height = r.height + 'px';
  }

  // 合成包络：拿不到频谱时用它驱动光晕，避免"有声音却一动不动"。
  function envelope(t) {
    var w1 = 0.5 + 0.5 * Math.sin(t * 4.7);
    var w2 = 0.5 + 0.5 * Math.sin(t * 2.1 + 1.3);
    return 0.18 + 0.62 * (w1 * 0.6 + w2 * 0.4);
  }

  function readLevel() {
    if (!ana || !anaData) { return 0; }
    ana.getByteTimeDomainData(anaData);
    var sum = 0;
    for (var i = 0; i < anaData.length; i++) {
      var x = (anaData[i] - 128) / 128;
      sum += x * x;
    }
    return Math.min(1, Math.sqrt(sum / anaData.length) * 2);
  }

  var frame = 0;
  function tick() {
    // 布局会因为字体载入、列宽变化、缩放而变动，画布尺寸也就跟着变，
    // 光晕每半秒复量一次（一秒两次 getBoundingClientRect，代价可忽略）。
    if (frame++ % 30 === 0) { placeRim(); }
    var lvl = readLevel();
    // 图谱接上了却读不到东西（内核没在渲染、设备静音）与"接不上"是一回事：
    // 都不能让画面干着，这时改用合成包络。
    if (lvl <= 0.02) { lvl = speaking ? envelope(Date.now() / 1000) : 0; }
    currentLevel = lvl;
    glow = Math.max(lvl, glow * 0.7);
    if (rim) {
      rim.hidden = !(speaking || glow > 0.02);
      rim.style.opacity = (0.12 + glow * 0.85).toFixed(3);
      rim.style.borderWidth = (1 + glow * 4).toFixed(2) + 'px';
      rim.style.boxShadow =
        'inset 0 0 ' + (4 + glow * 26).toFixed(1) + 'px ' +
        (glow * 9).toFixed(1) + 'px rgba(__ACCENT_RGB__, ' +
        (0.10 + glow * 0.45).toFixed(3) + ')';
    }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);

  // 画面可以降级，声音不可以：WebGL 或 AudioContext 任何一环不可用都切到
  // 纯 CSS 声波球，由它用 <audio> 原生播放。
  // 只写一行提示就 return 是不行的——app.py 在动效组件可见时已经把消息上的
  // st.audio 自动播放关掉了，那样整场面试一句声音都没有。
  function fallbackToBall(reason) {
    console.warn('[avatar] ' + reason + '，已回落到 CSS 声波球');
    if (typeof window.__ballBoot !== 'function') {
      setStatus('语音组件初始化失败');
      return;
    }
    document.getElementById('stage').hidden = true;
    statusEl.hidden = true;
    btn.hidden = true;
    document.getElementById('ball-ui').hidden = false;
    var el = document.getElementById('tts');
    if (AUDIO_SRC) { el.src = AUDIO_SRC; }
    window.__ballBoot(hasAudio);
  }

  // ---- 1) 初始化渲染器 ----
  try {
    if (!window.GlastarJS) { fallbackToBall('动效库未加载'); return; }
    if (!(cv.getContext('webgl2') || cv.getContext('webgl'))) {
      fallbackToBall('当前浏览器不支持 WebGL');
      return;
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
      // drawAvatarCircle 把这个值当「半径」用（ctx.arc 的 r），不是直径：
      // 66 时直径 132px 已接近 300px 画布的一半，鼓到上限 165 直径 330px 直接
      // 铺满整块画布，看上去就是一块不动的橙块。取 33（直径 66）→ 上限 79
      // （直径 158），方块在画布中央 66→158px 之间随音量鼓动，四周留白可见。
      avatarSize: 33,
      avatarSensitivity: 1.9,
      avatarExpansion: 2.4,
      avatarSmoothing: 0.24,
      avatarFadeWithAudio: true,      // 说话才亮，贴合语音对话语义
      backgroundRotation: false,
      backgroundScale: 1.4,
      avatarState: '__AVATAR_STATE__',
    });
    renderer.start();
    installLevelProxy();
    // vendor 只在「背景配置变了」或「thinking 状态」时才重绘背景层，其余状态
    // 每一帧都把头像圆往同一张 background 画布上叠一层（drawAvatarCircle 只
    // fill 不 clear）。半透明圆叠几十帧就把整块画布涂满橙色，之后电平再怎么变
    // 都落在已经饱和的画面里——看上去就是一块不动的橙块。强制每帧重绘背景
    // （drawBackground 会用底色整幅 fillRect，等价于清屏），叠加问题即消失。
    var __da = renderer.drawAvatar.bind(renderer);
    renderer.drawAvatar = function (lvl) {
      renderer.lastBackgroundConfig = '';
      return __da(lvl);
    };
  } catch (e) {
    fallbackToBall('动效初始化失败');
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
      // 光晕的频谱也取自同一个 ctx：srcNode 与这个 analyser 同处一个
      // AudioContext，不跨 context，读到的一定是真实的播放数据。
      ana = ctx.createAnalyser();
      ana.fftSize = 512;
      ana.smoothingTimeConstant = 0.6;
      anaData = new Uint8Array(ana.fftSize);
      ctx.createMediaStreamSource(dest.stream).connect(ana);
      return true;
    } catch (e) {
      return false;
    }
  }

  // AudioContext 被自动播放策略挂起时，WebAudio 路线就是"看着在说话却没声音"，
  // 这时宁可换成兜底的 <audio> 原生播放，也不接受静音。
  function withRunningCtx(next, fail) {
    if (!setupAudio()) { fail('音频上下文初始化失败'); return; }
    if (ctx.state === 'running') { next(); return; }
    var resumed = ctx.resume();
    if (!resumed || !resumed.then) { next(); return; }
    resumed.then(
      function () { ctx.state === 'running' ? next() : fail('音频上下文被浏览器挂起'); },
      function () { fail('音频上下文被浏览器挂起'); }
    );
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

  // glasatarjs 的 AudioAnalyzer 在构造函数里自建 AudioContext，而整份源码里
  // 没有任何一处 resume()：iframe 内没有用户手势，它被自动播放策略永久挂在
  // suspended，getByteTimeDomainData 读到的全是 128 → level 恒 0 → 头像层
  // opacity 恒 0，画面看着就是一块不动的纹理。
  //
  // 渲染器对音频只依赖 audioAnalyzer 的两个成员：渲染循环里的
  // getAudioData()（vendor L469-472，且只取返回值的 .level 字段）与
  // dispose()（L1455-1457）。代理本体见上面的 LEVEL_STUB。

  function playWebAudio() {
    decode().then(function () {
      if (!buf) { return; }
      // 说话时「重播」按钮会收起、状态行文字变短，舞台高度随之变化，
      // 画布尺寸也变——这里立刻量一次，别等 tick 的半秒周期。
      placeRim();
      if (srcNode) { try { srcNode.stop(); } catch (e) {} }
      srcNode = ctx.createBufferSource();
      srcNode.buffer = buf;
      srcNode.connect(ctx.destination);   // 出声
      srcNode.connect(dest);              // 同时进动效
      srcNode.onended = function () {
        speaking = false;
        setStatus('等待你的回答');
        btn.hidden = false;
        btn.textContent = '▶ 重播';
        renderer.updateConfig({ avatarState: 'listening' });
        placeRim();   // 重播按钮回来了，舞台又矮一截
      };
      srcNode.start(0);
      speaking = true;
      btn.hidden = true;
      setStatus('正在说话…');
      renderer.updateConfig({ avatarState: 'speaking' });
    }).catch(function () {
      fallbackToBall('语音解码失败');
    });
  }

  function playOnce() {
    withRunningCtx(playWebAudio, fallbackToBall);
  }

  btn.addEventListener('click', playOnce);

  // 画布尺寸随 iframe 变化（flex 分配 + max-height 缩放），光晕要重新贴合
  placeRim();
  window.addEventListener('resize', placeRim);

  // 首次自动播放：被浏览器拦截时不用慌，兜底画面里有个可点的播放按钮
  playOnce();
})();
</script>
</body>
</html>
"""
