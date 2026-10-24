# -*- coding: utf-8 -*-
"""生成答辩用 PPT：知遇 · 多场景多难度自适应 AI 虚拟面试智能体（19 页）。

设计基调跟随产品本身：暖纸底色 + 砖红强调色 + 深墨文字，克制、左对齐、留白。
运行：<bundled python> build_defense_deck.py
"""

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE
from pptx.oxml.ns import qn

# ---------- 设计常量 ----------
PAPER = RGBColor(0xF5, 0xF0, 0xE8)
INK = RGBColor(0x21, 0x1D, 0x19)
ACCENT = RGBColor(0xC2, 0x48, 0x1B)
MUTED = RGBColor(0x6B, 0x62, 0x59)
FAINT = RGBColor(0x9A, 0x91, 0x84)
LINE = RGBColor(0xE5, 0xDC, 0xCD)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
DEEP = RGBColor(0x2B, 0x21, 0x19)
ACCENT_SOFT = RGBColor(0xF3, 0xDF, 0xD6)

FONT = "微软雅黑"
SLIDE_W = 13.333
SLIDE_H = 7.5
LEFT = 0.95
CONTENT_W = SLIDE_W - LEFT * 2

prs = Presentation()
prs.slide_width = Inches(SLIDE_W)
prs.slide_height = Inches(SLIDE_H)


# ---------- 基础工具 ----------
def set_ea(run, name=FONT):
    """让中文也使用指定字体（python-pptx 的 font.name 只管 latin）。"""
    rpr = run._r.get_or_add_rPr()
    ea = rpr.find(qn("a:ea"))
    if ea is None:
        ea = rpr.makeelement(qn("a:ea"), {})
        rpr.append(ea)
    ea.set("typeface", name)


def no_shadow(shape):
    try:
        shape.shadow.inherit = False
    except Exception:
        pass


def new_slide():
    s = prs.slides.add_slide(prs.slide_layouts[6])
    bg = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, prs.slide_width, prs.slide_height)
    bg.fill.solid()
    bg.fill.fore_color.rgb = PAPER
    bg.line.fill.background()
    no_shadow(bg)
    return s


def tx(slide, x, y, w, h, text, size=15, color=INK, bold=False, align=PP_ALIGN.LEFT,
       anchor=MSO_ANCHOR.TOP, spacing=1.35, space_after=0, italic=False, font=FONT):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    tf.vertical_anchor = anchor
    lines = text.split("\n") if isinstance(text, str) else list(text)
    for i, ln in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.line_spacing = spacing
        p.space_after = Pt(space_after)
        r = p.add_run()
        r.text = ln
        r.font.size = Pt(size)
        r.font.bold = bold
        r.font.italic = italic
        r.font.color.rgb = color
        r.font.name = font
        set_ea(r, font)
    return box


def rule(slide, x, y, w, color=LINE, weight=1.0):
    ln = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Pt(weight))
    ln.fill.solid()
    ln.fill.fore_color.rgb = color
    ln.line.fill.background()
    no_shadow(ln)
    return ln


def vrule(slide, x, y, h, color=ACCENT, weight=3.0):
    ln = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Pt(weight), Inches(h))
    ln.fill.solid()
    ln.fill.fore_color.rgb = color
    ln.line.fill.background()
    no_shadow(ln)
    return ln


def card(slide, x, y, w, h, fill=WHITE, line_color=LINE, radius=0.04):
    sh = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    sh.adjustments[0] = radius
    sh.fill.solid()
    sh.fill.fore_color.rgb = fill
    if line_color is None:
        sh.line.fill.background()
    else:
        sh.line.color.rgb = line_color
        sh.line.width = Pt(1)
    no_shadow(sh)
    return sh


def head(slide, kicker, title, sub=None):
    """统一页头：小标签 + 标题 + 可选副标题。返回正文起始 y。"""
    tx(slide, LEFT, 0.52, CONTENT_W, 0.3, kicker, size=10.5, color=ACCENT, bold=True)
    tx(slide, LEFT, 0.86, CONTENT_W, 0.75, title, size=29, color=INK, bold=True, spacing=1.15)
    y = 1.62
    if sub:
        tx(slide, LEFT, y, CONTENT_W, 0.35, sub, size=13.5, color=MUTED, spacing=1.3)
        y += 0.5
    rule(slide, LEFT, y, CONTENT_W, LINE, 1.0)
    return y + 0.42


_PAGE = [1]  # 封面不调 footer，页码由调用顺序自动递增，插页时不必手工重排


def footer(slide, idx=None, note="知遇 · 多场景多难度自适应 AI 虚拟面试智能体"):
    if idx is None:
        _PAGE[0] += 1
        idx = _PAGE[0]
    tx(slide, LEFT, 6.92, CONTENT_W * 0.7, 0.3, note, size=9.5, color=FAINT)
    tx(slide, SLIDE_W - LEFT - 0.8, 6.92, 0.8, 0.3, f"{idx:02d}", size=9.5, color=FAINT, align=PP_ALIGN.RIGHT)


def bullets(slide, x, y, w, items, size=14.5, gap=0.06, marker_color=ACCENT, lead=1.35):
    """带砖红短横的要点列表。items 元素可为 str 或 (粗体前缀, 正文)。"""
    cur = y
    for it in items:
        box = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(cur + 0.105), Inches(0.14), Pt(2.2))
        box.fill.solid()
        box.fill.fore_color.rgb = marker_color
        box.line.fill.background()
        no_shadow(box)
        if isinstance(it, tuple):
            tb = slide.shapes.add_textbox(Inches(x + 0.32), Inches(cur), Inches(w - 0.32), Inches(0.34))
            tf = tb.text_frame
            tf.word_wrap = True
            tf.margin_left = 0
            tf.margin_right = 0
            tf.margin_top = 0
            tf.margin_bottom = 0
            p = tf.paragraphs[0]
            p.line_spacing = lead
            r1 = p.add_run()
            r1.text = it[0]
            r1.font.size = Pt(size)
            r1.font.bold = True
            r1.font.color.rgb = INK
            r1.font.name = FONT
            set_ea(r1)
            r2 = p.add_run()
            r2.text = it[1]
            r2.font.size = Pt(size)
            r2.font.color.rgb = MUTED
            r2.font.name = FONT
            set_ea(r2)
            n_lines = max(1, int(len(it[0] + it[1]) / (w * 2.05)) + 1)
        else:
            tb = slide.shapes.add_textbox(Inches(x + 0.32), Inches(cur), Inches(w - 0.32), Inches(0.34))
            tf = tb.text_frame
            tf.word_wrap = True
            tf.margin_left = 0
            tf.margin_right = 0
            tf.margin_top = 0
            tf.margin_bottom = 0
            p = tf.paragraphs[0]
            p.line_spacing = lead
            r = p.add_run()
            r.text = it
            r.font.size = Pt(size)
            r.font.color.rgb = MUTED
            r.font.name = FONT
            set_ea(r)
            n_lines = max(1, int(len(it) / (w * 2.05)) + 1)
        cur += 0.30 * n_lines * (lead / 1.35) + gap
    return cur


def kpi(slide, x, y, w, h, number, unit, label, detail):
    card(slide, x, y, w, h)
    tb = slide.shapes.add_textbox(Inches(x + 0.34), Inches(y + 0.34), Inches(w - 0.68), Inches(0.7))
    tf = tb.text_frame
    tf.word_wrap = False
    tf.margin_left = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    p = tf.paragraphs[0]
    r = p.add_run()
    r.text = number
    r.font.size = Pt(40)
    r.font.bold = True
    r.font.color.rgb = ACCENT
    r.font.name = FONT
    set_ea(r)
    if unit:
        r2 = p.add_run()
        r2.text = " " + unit
        r2.font.size = Pt(13)
        r2.font.color.rgb = MUTED
        r2.font.name = FONT
        set_ea(r2)
    tx(slide, x + 0.34, y + 1.12, w - 0.68, 0.3, label, size=13, color=INK, bold=True)
    tx(slide, x + 0.34, y + 1.42, w - 0.68, h - 1.5, detail, size=10.5, color=FAINT, spacing=1.3)


def table(slide, x, y, w, col_widths, rows, header=True, row_h=0.42, head_size=12, body_size=12,
          first_col_bold=True, head_fill=ACCENT_SOFT):
    n_rows = len(rows)
    n_cols = len(rows[0])
    shp = slide.shapes.add_table(n_rows, n_cols, Inches(x), Inches(y), Inches(w), Inches(row_h * n_rows))
    t = shp.table
    total = sum(col_widths)
    for i, cw in enumerate(col_widths):
        t.columns[i].width = Emu(int(Inches(w) * cw / total))
    for r_idx, row in enumerate(rows):
        t.rows[r_idx].height = Inches(row_h)
        for c_idx, val in enumerate(row):
            cell = t.cell(r_idx, c_idx)
            cell.margin_left = Inches(0.12)
            cell.margin_right = Inches(0.1)
            cell.margin_top = Inches(0.05)
            cell.margin_bottom = Inches(0.05)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.fill.solid()
            if r_idx == 0 and header:
                cell.fill.fore_color.rgb = head_fill
            else:
                cell.fill.fore_color.rgb = WHITE if r_idx % 2 == 1 else PAPER
            tf = cell.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            p.line_spacing = 1.2
            r = p.add_run()
            r.text = str(val)
            r.font.size = Pt(head_size if (r_idx == 0 and header) else body_size)
            r.font.name = FONT
            set_ea(r)
            if r_idx == 0 and header:
                r.font.bold = True
                r.font.color.rgb = ACCENT
            elif c_idx == 0 and first_col_bold:
                r.font.bold = True
                r.font.color.rgb = INK
            else:
                r.font.color.rgb = MUTED
    return y + row_h * n_rows


def chip(slide, x, y, w, h, text, fill=WHITE, color=INK, size=11.5, bold=False, line_color=LINE):
    sh = card(slide, x, y, w, h, fill=fill, line_color=line_color, radius=0.25)
    tf = sh.text_frame
    tf.word_wrap = True
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = text
    r.font.size = Pt(size)
    r.font.bold = bold
    r.font.color.rgb = color
    r.font.name = FONT
    set_ea(r)
    return sh


def arrow_down(slide, x, y, h, color=ACCENT):
    sh = slide.shapes.add_shape(MSO_SHAPE.DOWN_ARROW, Inches(x), Inches(y), Inches(0.18), Inches(h))
    sh.fill.solid()
    sh.fill.fore_color.rgb = color
    sh.line.fill.background()
    no_shadow(sh)


def quote_band(slide, y, text, size=15.5):
    rule(slide, LEFT, y, CONTENT_W, LINE, 1.0)
    tx(slide, LEFT, y + 0.22, CONTENT_W, 0.6, text, size=size, color=INK, bold=True, spacing=1.3)


# =====================================================================
# 01 封面
# =====================================================================
s = new_slide()
vrule(s, 0.95, 1.55, 1.95, ACCENT, 4.0)
tx(s, 1.28, 1.42, 8.0, 0.35, "毕业设计答辩  ·  2026", size=12, color=ACCENT, bold=True)
tx(s, 1.24, 1.85, 9.0, 1.5, "知遇", size=76, color=INK, bold=True, spacing=1.0)
tx(s, 1.28, 3.05, 10.5, 0.5, "多场景多难度自适应 AI 虚拟面试智能体", size=24, color=INK, spacing=1.2)
tx(s, 1.28, 3.72, 10.2, 0.9,
   "不是题库，而是一个会当场判断、追问、收尾的面试官。\n四类场景 × 六个专业 × 三档难度，每轮决策都依据你的回答现场做出。",
   size=13.5, color=MUTED, spacing=1.55)
rule(s, 1.28, 5.35, 5.4, LINE, 1.0)
tx(s, 1.28, 5.56, 1.2, 0.3, "答辩人", size=11.5, color=FAINT)
rule(s, 2.15, 5.78, 2.3, LINE, 1.0)
tx(s, 1.28, 5.96, 1.2, 0.3, "指导教师", size=11.5, color=FAINT)
rule(s, 2.15, 6.18, 2.3, LINE, 1.0)
tx(s, SLIDE_W - LEFT - 4.9, 5.56, 4.9, 0.3, "Streamlit · DeepSeek V4.1-Flash · 零前端框架",
   size=10, color=FAINT, align=PP_ALIGN.RIGHT, spacing=1.4)

# =====================================================================
# 02 一页看懂
# =====================================================================
s = new_slide()
y = head(s, "OVERVIEW", "一页看懂这个项目",
         "从「题库播放器」到「会决策的面试官」：面试官每一轮都依据你的回答当场决定继续深挖、换题还是收尾。")
cw = (CONTENT_W - 0.3 * 3) / 4
for i, (num, unit, label, detail) in enumerate([
    ("72", "种", "配置组合", "4 场景 × 6 专业 × 3 难度\n全部由同一套代码编译而成"),
    ("318", "条", "考点 + 判分参照", "6 专业 × 3 档，每档 ≥15 条\n考点与参照一对一"),
    ("554", "项", "离线单元测试", "18 套，不联网、不花 token\n检索用真简历真切片"),
    ("21", "个", "版本迭代", "v1 → v17，每个版本\n解决一个具体问题"),
]):
    kpi(s, LEFT + i * (cw + 0.3), y + 0.1, cw, 2.35, num, unit, label, detail)

quote_band(s, y + 2.85,
           "一句话：把「面试官怎么想」写成可检查的数据结构，而不是交给模型自觉。")
tx(s, LEFT, y + 3.55, CONTENT_W, 1.2,
   "面试官每轮输出结构化决策（追问 / 换题 / 澄清 / 收尾 + 考察点 + 缺什么），代码再按轮次与追问次数做兜底约束。\n"
   "因此追问是可控、可复现、可解释的——报告里能逐轮回看它为什么这样问。",
   size=13, color=MUTED, spacing=1.6)
footer(s)

# =====================================================================
# 03 问题
# =====================================================================
s = new_slide()
y = head(s, "WHY", "为什么现有的 AI 面试练不出来",
         "三类失败，都指向同一个根因：机器只会「出下一题」，不会「判断你这一答」。")
cw = (CONTENT_W - 0.5 * 2) / 3
probs = [
    ("01", "只出题，不追问", "答得空洞它也照样念下一题；没有「缺量化 / 缺个人贡献 / 缺原理」的区分，\n练的是「答完」，不是「答好」。"),
    ("02", "评分凭印象", "同一份回答两次评分不一致，维度分与实际表现脱钩；\n报告无法指向「该改哪一句」。"),
    ("03", "只有一种面试官", "真实面试里冷脸、赶时间、反复质疑是常态；\n只练「完美面试官」，一遇到压力就崩。"),
]
for i, (num, title, body) in enumerate(probs):
    x = LEFT + i * (cw + 0.5)
    card(s, x, y + 0.1, cw, 2.5)
    tx(s, x + 0.32, y + 0.36, 1.0, 0.4, num, size=13, color=ACCENT, bold=True)
    tx(s, x + 0.32, y + 0.78, cw - 0.64, 0.45, title, size=18, color=INK, bold=True)
    tx(s, x + 0.32, y + 1.38, cw - 0.64, 1.0, body, size=11.5, color=MUTED, spacing=1.5)

quote_band(s, y + 3.0, "结论：缺的不是题库，是「决策」。")
tx(s, LEFT, y + 3.7, CONTENT_W, 0.8,
   "题库型产品把「面试」简化成「出题 + 打分」两个动作，中间那层最像人的判断被跳过了——\n"
   "而恰恰是「这一答该不该继续挖、挖什么」决定了这场练习有没有训练价值。",
   size=13, color=MUTED, spacing=1.55)
footer(s)

# =====================================================================
# 04 主张：不是题库是决策
# =====================================================================
s = new_slide()
y = head(s, "POSITION", "核心主张：不是题库，是决策",
         "同一个面试场景，两种实现方式的差别不在「题好不好」，而在「谁在决定下一句」。")
rows = [
    ["维度", "题库型 AI 面试", "本项目"],
    ["出题", "预先排好 N 道题，按序播放", "每轮由模型输出结构化决策：ask / probe / reflect / end"],
    ["追问", "固定脚本，或干脆不追问", "必须引用你上一句的原话、术语或数字，否则视为无效追问"],
    ["可控性", "靠提示词「祈祷」模型别跑偏", "轮次、追问次数由代码计数并强制切换（保险丝）"],
    ["评分", "模型自由发挥，口径不透明", "318 条判分参照，评语须引用考生原话为证"],
]
table(s, LEFT, y + 0.05, CONTENT_W, [1.1, 3.6, 6.2], rows, row_h=0.62, head_size=12.5, body_size=12)
quote_band(s, y + 3.55, "把不确定的部分交给模型，把不能失控的部分交给代码——这是全部设计的出发点。")
footer(s)

# =====================================================================
# 05 系统架构
# =====================================================================
s = new_slide()
y = head(s, "ARCHITECTURE", "系统架构：五层职责分离",
         "界面只负责「呈现与采集」，决策、知识、评估三层各自独立，任何一层降级都不中断面试。")
layers = [
    ("交互层", "Streamlit 聊天界面 · 语音识别 · 语音合成 · 声波球动效",
     ["聊天式 UI", "语音输入", "语音播报", "音量驱动动效"]),
    ("决策层", "面试官 Agent：四动作决策 + 代码保险丝 + 窗口 + 主动查阅简历",
     ["状态机", "追问上限", "窗口 6 条", "resume_lookup"]),
    ("知识层", "318 考点 / 318 参照 · 真题三层 · 简历 BM25 检索",
     ["策略库", "参照库", "真题库", "简历检索"]),
    ("评估层", "评估官 · 教练 · 建议官 · 简历优化官",
     ["评估", "复盘", "能力建议", "简历改写"]),
    ("数据层", "history.json 逐字稿（可删）· .snapshots 断点续面",
     ["记录", "横向对比", "续面", "隐私出口"]),
]
band_h = 0.74
gap = 0.12
chips_w = 1.35 * 4 + 0.1 * 3
chips_x = SLIDE_W - LEFT - 0.16 - chips_w
for i, (name, desc, chips) in enumerate(layers):
    by = y + 0.05 + i * (band_h + gap)
    card(s, LEFT, by, CONTENT_W, band_h)
    vrule(s, LEFT, by + 0.12, band_h - 0.24, ACCENT, 3.0)
    tx(s, LEFT + 0.28, by + 0.10, 1.15, 0.3, name, size=12, color=ACCENT, bold=True)
    tx(s, LEFT + 0.28, by + 0.40, chips_x - LEFT - 0.58, 0.3, desc, size=9.5, color=MUTED, spacing=1.25)
    for j, c in enumerate(chips):
        chip(s, chips_x + j * (1.35 + 0.1), by + 0.16, 1.35, 0.42, c, size=9.5,
             fill=PAPER, color=MUTED)
footer(s)

# =====================================================================
# 06 三级自适应
# =====================================================================
s = new_slide()
y = head(s, "ADAPTIVITY", "三级自适应：72 种组合，一套代码",
         "场景决定考察重心，专业决定能力维度，难度决定三档题的配比与追问强度——三者最终编译成一份面试官配置。")
rows = [
    ["轴", "取值", "它决定什么"],
    ["一级 · 场景", "考研复试 / 应届校招 / 日常实习 / 转正就职", "考察重心：专业基础、科研潜力、项目经历、业务理解"],
    ["二级 · 专业", "计算机 / 人工智能 / 软件工程 / 电子信息 / 自动化 / 通信", "能力维度与高频考点（A/B/C 各 15 条起，共 318 条）"],
    ["三级 · 难度", "A 级（985 / 一线大厂）· B 级（211 / 中型）· C 级（双非 / 中小）", "三档题配比（A 60/30/10 · B 20/60/20 · C 0/30/70）、提问深度、追问上限（A 4 / B 3 / C 2）"],
]
table_end = table(s, LEFT, y + 0.05, CONTENT_W, [1.5, 4.4, 5.4], rows, row_h=0.62, head_size=12.5,
                  body_size=11.5)

ay = table_end + 0.14
arrow_down(s, SLIDE_W / 2 - 0.09, ay, 0.30)
chip(s, SLIDE_W / 2 + 0.22, ay + 0.02, 3.0, 0.28, "编译 compile_profile()", fill=ACCENT, color=WHITE,
     size=9.5, line_color=None)

cy = ay + 0.44
card(s, LEFT, cy, CONTENT_W, 0.78, fill=ACCENT_SOFT, line_color=None)
tx(s, LEFT + 0.32, cy + 0.15, CONTENT_W - 0.64, 0.55,
   "一份面试官配置：人设 + 能力维度 + 可用考点 + 追问强度 + 轮数 + 评分权重 + 真题配额",
   size=13, color=INK, bold=True, spacing=1.2)
tx(s, LEFT, cy + 0.95, CONTENT_W, 0.6,
   "同一套代码服务全部 72 种组合：所有差异都是策略参数，不是分支判断——这是低成本实现「多场景」的根本原因。",
   size=12, color=MUTED, spacing=1.4)
footer(s)

# =====================================================================
# 07 决策状态机 + 保险丝
# =====================================================================
s = new_slide()
y = head(s, "CORE 01", "技术内核①：面试官决策状态机",
         "每一轮，面试官必须交出这样一份 JSON——把「怎么想」变成可检查、可落盘、可解释的数据。")
card(s, LEFT, y + 0.05, 5.5, 2.95, fill=DEEP, line_color=None)
code = [
    "{",
    '  "reply":       "对考生说的话（口播稿）",',
    '  "action":      "ask | probe | reflect | end",',
    '  "question_no": 3,',
    '  "focus":       "本题考察的能力点",',
    '  "gap":         "还缺什么：量化 / 个人贡献 / 原理",',
    '  "tool":        "none | resume_lookup",',
    '  "tool_query":  "检索关键词",',
    '  "note":        "为什么这样决策（内部备注）"',
    "}",
]
tb = s.shapes.add_textbox(Inches(LEFT + 0.3), Inches(y + 0.22), Inches(5.0), Inches(2.6))
tf = tb.text_frame
tf.word_wrap = False
tf.margin_left = 0
tf.margin_top = 0
for i, ln in enumerate(code):
    p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
    p.line_spacing = 1.28
    r = p.add_run()
    r.text = ln
    r.font.size = Pt(10.5)
    r.font.name = "Consolas"
    r.font.color.rgb = WHITE if ('"action"' in ln or '"gap"' in ln) else RGBColor(0xC9, 0xC0, 0xB4)

rx = LEFT + 5.9
rows = [
    ["动作", "含义", "推进题目"],
    ["ask", "换下一道主问题", "是"],
    ["probe", "针对当前回答追问", "否"],
    ["reflect", "澄清 / 复述确认", "否"],
    ["end", "收尾", "—"],
]
table(s, rx, y + 0.05, CONTENT_W - 5.9, [1.0, 3.3, 1.2], rows, row_h=0.34, head_size=11, body_size=11)
tx(s, rx, y + 1.86, CONTENT_W - 5.9, 1.4,
   "决策规则（写进提示词，可逐条检查）：\n"
   "· 回答空洞 → probe，并在 gap 里写明缺什么\n"
   "· 追问必须引用上一句的原话 / 术语 / 数字\n"
   "· 数据模糊（「大概」「几百」）→ reflect 澄清\n"
   "· 主问题数 ≤ 配置轮数，问满自动收尾",
   size=10.5, color=MUTED, spacing=1.45)

rule(s, LEFT, y + 3.2, CONTENT_W, LINE, 1.0)
tx(s, LEFT, y + 3.38, 1.6, 0.3, "保险丝", size=12, color=ACCENT, bold=True)
tx(s, LEFT + 1.55, y + 3.36, CONTENT_W - 1.55, 0.75,
   "轮次 / 追问次数 / 连续 reflect 次数由代码计数并回传提示词；模型越界即强制切换：开场轮强制 ask、问满强制 end、\n"
   "追问超限强制 ask、reflect 连用超限强制回到提问。上限只是保险丝——正常节奏交给模型，只有它明显绕圈时才掐断。",
   size=10.5, color=MUTED, spacing=1.45)
tx(s, LEFT, y + 4.00, CONTENT_W, 0.4,
   "严格模式：追问 ≤2（不论难度档）、关闭 reflect、锁定友善型（录演示要节奏一致）　|　真人模式（默认）：追问按难度档 A 4 / B 3 / C 2、连续 reflect ≤2",
   size=10.5, color=FAINT, spacing=1.3)
footer(s)

# =====================================================================
# 08 五个角色
# =====================================================================
s = new_slide()
y = head(s, "CORE 02", "五个角色，各自只看该看的东西",
         "原方案是一个 ReAct Agent 包打天下；实测更稳的做法是把职责切开，每次调用只做一件事。")
rows = [
    ["角色", "调用时机", "职责", "产出"],
    ["面试官", "多轮对话", "开场 / 提问 / 追问 / 收尾", "口播稿 + 决策 JSON"],
    ["评估官", "面试结束后两次独立调用", "第一次只打分，第二次只归因", "维度分 / 双分数"],
    ["教练", "拿到记录与评估后", "指出哪句答得不好、该怎么讲", "问题 + 改写口语稿 + 练习动作"],
    ["建议官", "教练之后一次", "按评分维度给「下次面试前补什么」", "逐维度 gap + 动作 + 自测"],
    ["简历优化官", "独立入口，不面试", "诊断 + 逐条改写对照", "改写对照 + 关键词 + 自查表"],
]
table(s, LEFT, y + 0.05, CONTENT_W, [1.5, 2.4, 4.3, 4.1], rows, row_h=0.46, head_size=12, body_size=11)

quote_band(s, y + 2.95, "评估官为什么要拆成两次：审查方一旦继承起草方的叙事，审查就退化成自我确认。")
tx(s, LEFT, y + 3.58, CONTENT_W, 1.0,
   "第一次调用只拿到候选人自己的表现材料，不给面试官状态轨迹；第二次才交出轨迹、只问「哪些扣分来自对方状态」。\n"
   "两次都只做一件事，且第二次被明令禁止重打 overall_score、禁止改维度分——打分口径与归因口径互不污染。",
   size=11, color=MUTED, spacing=1.45)
footer(s)

# =====================================================================
# 09 情绪附加轴
# =====================================================================
s = new_slide()
y = head(s, "CORE 03", "附加轴：面试官的情绪是「流动的」，不是人设",
         "难度决定问什么、问多深；情绪决定用什么态度问。两条轴正交，任意组合。")
modes = ["友善型", "疲惫型", "怀疑型", "冷淡型", "压力型", "随机"]
for i, m in enumerate(modes):
    chip(s, LEFT + i * 1.28, y + 0.05, 1.15, 0.4, m, size=10.5,
         fill=ACCENT_SOFT if m in ("压力型", "怀疑型", "冷淡型", "疲惫型") else WHITE,
         color=ACCENT if m in ("压力型", "怀疑型", "冷淡型", "疲惫型") else MUTED)

mid_y = y + 0.72
card(s, LEFT, mid_y, 5.4, 1.85)
tx(s, LEFT + 0.3, mid_y + 0.18, 4.9, 0.3, "隐藏三量（不给考生看）", size=11.5, color=ACCENT, bold=True)
tx(s, LEFT + 0.3, mid_y + 0.52, 4.9, 1.3,
   "耐心 0–3　·　精力 0–2　·　时间压力 0–2\n"
   "派生出四种对外表现：投入 / 例行公事 / 急躁 / 冷淡\n"
   "纯函数（mood.py）：无随机、无全局、无 IO——同一串输入\n"
   "永远得到同一条轨迹，所以能离线单测，归因报告也站得住。",
   size=10.5, color=MUTED, spacing=1.5)

cx2 = LEFT + 5.75
card(s, cx2, mid_y, 5.65, 1.85)
tx(s, cx2 + 0.3, mid_y + 0.18, 5.1, 0.3, "三个输出通道", size=11.5, color=ACCENT, bold=True)
tx(s, cx2 + 0.3, mid_y + 0.52, 5.1, 1.3,
   "① 措辞与追问脾气（冷淡态不出「嗯/不错」）\n"
   "② 追问上限（难度基线 A 4 / B 3 / C 2，情绪最多再加 1 次）\n"
   "③ 语速与音调（难度基线 + 情绪偏移 + 微小抖动）",
   size=10.5, color=MUTED, spacing=1.5)

by = mid_y + 2.05
rule(s, LEFT, by, CONTENT_W, LINE, 1.0)
tx(s, LEFT, by + 0.2, 2.2, 0.3, "为什么必须做归因", size=12, color=ACCENT, bold=True)
tx(s, LEFT + 2.25, by + 0.18, CONTENT_W - 2.25, 0.9,
   "结束后评估官给两个分数——「本场实际表现」与「剥离干扰后的表现」，并说明哪些扣分源于对方状态、哪些源于自己。\n"
   "没有这层归因，情绪轴就退化成「随机扣分」，反而强化错误认知。",
   size=10.5, color=MUTED, spacing=1.45)
tx(s, LEFT, by + 1.00, CONTENT_W, 0.55,
   "伦理红线（写进提示词与界面，不是口头承诺）：高强度模式先勾选知情同意；禁止评价人格、学历、家庭；随时可中止；"
   "结束后明确告知「若真实面试遇到这类面试官，问题在对方，不在你」。",
   size=10.5, color=FAINT, spacing=1.4)
footer(s)

# =====================================================================
# 10 知识库
# =====================================================================
s = new_slide()
y = head(s, "KNOWLEDGE", "知识库：RAG 的现实方案",
         "三次机会都本可以上向量库，但三次都判断为「纯粹的复杂度开销」——理由各不相同，结论一致。")
cw = (CONTENT_W - 0.5 * 2) / 3
blocks = [
    ("策略 + 参照库", "318 条考点 ↔ 318 条参照，一对一。\n参照是 must_hit / bonus /\ncommon_errors 三元组，刻意不是\n「标准答案」——开放作答下给标准\n答案会诱导字面比对，讲对了但用\n词不同就被判错。",
     "字典查表：字段直接命中"),
    ("真题三层", "外部开源题库精选（技术面覆盖\nCS / 软工）→ 自建 318+317 条\n补位其余 4 专业 → 陷阱题由常见\n误区反转，单独存放防泄露。\n配额混合：每场默认 ≤2 道。",
     "外部优先、自建兜底；措辞诚实"),
    ("简历检索", "切片 + 条目索引 + 按话题聚焦。\n本地字符 n-gram BM25，纯 Python\n零依赖。检索未命中只留索引、\n不硬塞不相干条目；切片失败整体\n回落全文注入。",
     "一份简历十几条：BM25 足够"),
]
for i, (title, body, note) in enumerate(blocks):
    x = LEFT + i * (cw + 0.5)
    card(s, x, y + 0.1, cw, 2.85)
    tx(s, x + 0.3, y + 0.32, cw - 0.6, 0.35, title, size=15, color=INK, bold=True)
    tx(s, x + 0.3, y + 0.8, cw - 0.6, 1.5, body, size=9.5, color=MUTED, spacing=1.55)
    rule(s, x + 0.3, y + 2.32, cw - 0.6, LINE, 1.0)
    tx(s, x + 0.3, y + 2.45, cw - 0.6, 0.35, note, size=9.5, color=ACCENT, spacing=1.3)

quote_band(s, y + 3.15, "同一个判断的三次应用：数据量小、字段可直命中时，向量检索换来的不是能力，是维护成本。")
tx(s, LEFT, y + 3.82, CONTENT_W, 0.6,
   "边界如实声明：6 个专业全部属于「计算机 + 电子电气」一簇；经管、文科、理科、其他工科未覆盖，界面选不到——"
   "新增一个专业要同时补齐\n5 维度 × 3 档 × 15 考点起 + 45 条参照，不是加个名字。",
   size=10, color=FAINT, spacing=1.4)
footer(s)

# =====================================================================
# 11 上下文与成本
# =====================================================================
s = new_slide()
y = head(s, "CONTEXT", "上下文与成本：原文窗口 + 本地压缩",
         "轮数一多，全量回传会让成本与延迟线性上涨；长历史 + JSON 输出下模型也更容易返回空内容。")
rows = [
    ["层次", "做法", "为什么这样做"],
    ["短期记忆", "只回传最近 6 条消息（约 3 轮问答）原文", "关键状态本就单独注入，窗口化不丢信息"],
    ["长期记忆", "更早对话用本地启发式压成「前情提要」", "决策取考点与动作，回答取前 80 字"],
    ["决策状态", "question_no / 追问次数 / 剩余轮数 / 已考考点", "单独注入，不依赖对话窗口"],
    ["事后分析", "评估官与教练仍拿完整逐字稿", "一次性调用，不受窗口影响"],
]
table(s, LEFT, y + 0.05, CONTENT_W, [1.5, 5.0, 4.8], rows, row_h=0.6, head_size=12, body_size=11)

quote_band(s, y + 3.15, "刻意不再加一次模型调用来做总结：省一轮延迟与成本，结果还是确定的——同样的历史永远压出同样的文本。")
tx(s, LEFT, y + 3.80, CONTENT_W, 0.9,
   "配套的去重：每道主问题回报一个 topic（考点），累积成本场「已考考点」注入下一轮，禁止重复出题；"
   "新场读取最近 3 场的考点作为 avoid_topics。若模型又选回旧的，代码点名纠正并重试一次，仍重复才接受——绝不无限重试卡死流程。",
   size=10.5, color=MUTED, spacing=1.35)
footer(s)

# =====================================================================
# 12 评估体系
# =====================================================================
s = new_slide()
y = head(s, "EVALUATION", "评估体系：问的和评的必须同源",
         "同一份能力模型既要指导怎么问，也要指导怎么评——否则「问的人不知道评的人在看什么」。")
card(s, LEFT, y + 0.05, 6.1, 1.85)
tx(s, LEFT + 0.32, y + 0.22, 5.5, 0.3, "判分锚点 → 考点级判定", size=12, color=ACCENT, bold=True)
tx(s, LEFT + 0.32, y + 0.56, 5.5, 1.25,
   "每条参照是 must_hit / bonus / common_errors 三元组；\n"
   "每个考点再压成一个可数结论：通过 / 部分通过 /\n"
   "不通过。判分必须引用考生原话为证，否则作废。\n"
   "「一票否决」只在参照显式标注 veto 时才生效——\n"
   "宁可这条分支空着，也不替数据臆造否决项。",
   size=10.5, color=MUTED, spacing=1.45)

card(s, LEFT + 6.4, y + 0.05, CONTENT_W - 6.4, 1.85, fill=ACCENT_SOFT, line_color=None)
tx(s, LEFT + 6.72, y + 0.22, CONTENT_W - 7.0, 0.3, "v11：一个实测发现的真实缺陷", size=12, color=ACCENT, bold=True)
tx(s, LEFT + 6.72, y + 0.58, CONTENT_W - 7.0, 1.2,
   "评估官拿到的是该难度档的全部 17 条参照，\n"
   "而不是本场问到的那几条——于是把没问过的\n"
   "「红黑树」判成考生的遗漏，再经教练变成\n"
   "「去补一个从没被问过的考点」。",
   size=10.5, color=INK, spacing=1.5)

ny = y + 2.05
tx(s, LEFT, ny, 1.5, 0.9, "修法\n实测", size=11, color=ACCENT, bold=True, spacing=1.7)
tx(s, LEFT + 1.2, ny, CONTENT_W - 1.2, 0.95,
   "把 asked_topics 透传给参照查询，只注入本场真正问到的条目。\n"
   "同一场：注入 17 → 2 条，评估官提示词 7125 → 851 字，rubric_check 17 → 2 条，教练建议随之落到真问过的考点上。",
   size=11, color=MUTED, spacing=1.5)

rule(s, LEFT, ny + 1.02, CONTENT_W, LINE, 1.0)
tx(s, LEFT, ny + 1.18, 1.5, 0.9, "校准\n回填", size=11, color=ACCENT, bold=True, spacing=1.7)
tx(s, LEFT + 1.2, ny + 1.16, CONTENT_W - 1.2, 0.95,
   "评分只是预测，候选人的真实结局才是标签。面试结束后可以回填「进入下一轮 / 拿到 offer / 被拒」，\n"
   "系统再把「打了多少分」和「后来怎么样」对照起来：落进灰区的不计对错，只报偏乐观或偏保守。\n"
   "刻意不拿结局反调分数——口径一旦随样本漂移，就和「问评同源」打架了。",
   size=11, color=MUTED, spacing=1.45)
footer(s)

# =====================================================================
# 13 工程健壮性
# =====================================================================
s = new_slide()
y = head(s, "ROBUSTNESS", "工程健壮性：让「不稳定」变成「可预期」",
         "v7 集中做了一轮治理——六件事，每一件都对应一个真实发生过的故障。")
items = [
    ("首字节 15s 硬超时", "显式 httpx.Timeout + max_retries=0。裸数字下 SDK 仍会自己重试，实测把 60s 放大成 540s。"),
    ("失败可恢复", "保留候选人这句回答、回滚情绪状态，给出「重试本轮」入口，不必重新输入。"),
    ("原子写", "记录与快照同目录临时文件 + os.replace：读到的要么旧、要么新，不会有半截 JSON。"),
    ("快照隔离", "一场一个文件（.snapshots/<会话id>.json），多标签页不再互相覆盖。"),
    ("工具循环上限", "MAX_TOOL_ROUNDS = 2，模型可以「查了 A 还要核对 B」，但上限由代码握着。"),
    ("成本可见", "每次调用累计 usage，报告页与导出 Markdown 显示本场 token 消耗。"),
]
cw = (CONTENT_W - 0.4 * 2) / 3
for i, (title, body) in enumerate(items):
    x = LEFT + (i % 3) * (cw + 0.4)
    yy = y + 0.05 + (i // 3) * 1.72
    card(s, x, yy, cw, 1.52)
    tx(s, x + 0.3, yy + 0.24, cw - 0.6, 0.35, title, size=14, color=INK, bold=True)
    tx(s, x + 0.3, yy + 0.68, cw - 0.6, 0.7, body, size=10.5, color=MUTED, spacing=1.5)

quote_band(s, y + 3.6, "共同点：把「偶发故障」变成「有确定行为的降级」——用户看到的不是崩溃，是一句可操作的提示。")
footer(s)

# =====================================================================
# 14 语音与动效
# =====================================================================
s = new_slide()
y = head(s, "VOICE", "语音链路与「去掉人脸」的决策",
         "口播是体验的一部分，也是外部依赖最脆弱的一环——所以每个环节都准备了降级。")

card(s, LEFT, y + 0.05, 6.1, 3.3)
tx(s, LEFT + 0.32, y + 0.24, 5.5, 0.3, "合成链：三级降级", size=12, color=ACCENT, bold=True)
chain = ["edge-tts（免费、音色自然）", "硅基流动 CosyVoice2（复用识别同一把 Key）", "静默降级回文字模式"]
for i, c in enumerate(chain):
    chip(s, LEFT + 0.32, y + 0.62 + i * 0.52, 3.4, 0.4, c, size=10,
         fill=WHITE if i else ACCENT_SOFT, color=INK if i else ACCENT)
tx(s, LEFT + 0.32, y + 2.2, 5.5, 1.0,
   "熔断按故障性质分两档：401/403 这类永久拒绝长熔断 10 分钟；\n"
   "超时、连接重置、空音频这类瞬时故障只熔断 30 秒，且有界重试 3 次——\n"
   "一次网络抖动不该让整场面试都换兜底音色。",
   size=10, color=MUTED, spacing=1.5)

card(s, LEFT + 6.4, y + 0.05, CONTENT_W - 6.4, 3.3)
tx(s, LEFT + 6.72, y + 0.24, CONTENT_W - 7.0, 0.3, "v12：撤掉数字人，换成声波球", size=12, color=ACCENT, bold=True)
metrics = [("10.4 GB", "仓库体积"), ("1.7 GB", "撤除后"), ("0", "图片依赖")]
for i, (num, lab) in enumerate(metrics):
    tx(s, LEFT + 6.72 + i * 1.72, y + 0.66, 1.7, 0.5, num, size=22, color=INK, bold=True)
    tx(s, LEFT + 6.72 + i * 1.72, y + 1.16, 1.7, 0.28, lab, size=9.5, color=FAINT)
tx(s, LEFT + 6.72, y + 1.6, CONTENT_W - 7.0, 1.6,
   "原方案：AI 生成人脸 + 口型 + 眨眼 + SadTalker 离线预渲染视频。\n"
   "改为：一个不出现人脸的抽象动效——光球随音量呼吸扩张、\n"
   "外圈扩散环、底部频谱条（豆包式语音对话观感）。\n"
   "收益：不再需要形象图与 Pillow，avatar.py 精简为零图片依赖；\n"
   "彻底消除「AI 形象被误认为真人」这条风险。",
   size=10, color=MUTED, spacing=1.5)
tx(s, LEFT, y + 3.5, CONTENT_W, 0.5,
   "另外两处细节：voice.tts_text() 在合成前剥掉 Markdown 记号（否则 ** 会被逐字念成「星号星号」）；"
   "is_valid_audio() 校验 4 字节 MP3 帧头，避免空音频被界面静默吞掉。",
   size=10.5, color=FAINT, spacing=1.4)
footer(s)

# =====================================================================
# 15 隐私
# =====================================================================
s = new_slide()
y = head(s, "PRIVACY", "隐私：三道闸，写进代码而不是写在 PPT 上",
         "这个项目要处理简历和完整逐字稿——两者都是最敏感的个人数据。")

gates = [
    ("闸一", "简历只在内存", "解析、切片、索引全程不落盘；快照与记录落盘时显式剔除 resume / resume_chunks。"),
    ("闸二", "逐字稿可删", "历史记录落盘 history.json，但提供「逐场删除」与「清空全部」（两步确认、不可撤销）——逐字稿存了就给出对应出口。"),
    ("闸三", "快照不含密钥与音频", "断点续面快照剔除简历、切片、全部音频字段与 API Key；恢复后界面明示「简历无法恢复，本场按口述背景提问」。"),
]
gy = y + 0.02
for i, (tag, title, body) in enumerate(gates):
    card(s, LEFT, gy, CONTENT_W, 0.92)
    tx(s, LEFT + 0.3, gy + 0.12, 0.7, 0.3, tag, size=11, color=ACCENT, bold=True)
    tx(s, LEFT + 1.05, gy + 0.10, 2.6, 0.35, title, size=13.5, color=INK, bold=True)
    tx(s, LEFT + 3.7, gy + 0.12, CONTENT_W - 4.0, 0.70, body, size=10.5, color=MUTED, spacing=1.42)
    gy += 1.02

quote_band(s, y + 3.10, "还有一条「不静默恢复」的约定：刷新后首页显式问「继续上次面试还是重开」，不替用户做决定。")
tx(s, LEFT, y + 3.70, CONTENT_W, 0.55,
   "v14 的建议与简历优化同样不落盘（含逐字稿引用与简历改写，与简历同级）；save_record 处留了注释——"
   "补落盘前必须同步修改 README 的隐私声明。",
   size=10.5, color=FAINT, spacing=1.4)
footer(s)

# =====================================================================
# 16 验证
# =====================================================================
s = new_slide()
y = head(s, "VERIFICATION", "怎么证明它work：554 项离线单测 + 可量化的指标",
         "两条原则贯穿全部测试：不联网（把模型换成假件）、检索用真件（真简历、真切片、真跑 BM25）。")
card(s, LEFT, y + 0.05, 5.3, 3.2)
tx(s, LEFT + 0.32, y + 0.22, 4.7, 0.3, "18 套单元测试 · 554 项", size=12, color=ACCENT, bold=True)
suite = [
    ("test_advice / optimize", "建议与改写不编造", "89"),
    ("test_tool_call", "工具调用四条回落路径", "46"),
    ("test_voice", "Markdown 剥离 / 错误分流", "48"),
    ("test_history_store", "越界不误删 / 跨专业不混比", "39"),
    ("test_evaluator_rubric", "参照不越界（v11 回归）", "36"),
    ("test_mood", "情绪状态机边界与调制", "36"),
    ("test_calibration", "结局回填与准确率分母", "35"),
    ("test_topic_anchor", "考点锚定 / 现拟参照契约", "40"),
    ("test_session_store", "快照读写与容错", "22"),
    ("test_behavior_rubric", "行为面参照库 15 条", "17"),
    ("其余 7 套", "状态机 / 参照 / 真题 / JSON", "146"),
]
tx(s, LEFT + 0.32, y + 0.60, 4.7, 2.45,
   "\n".join("· %-23s %3s  %s" % (a, c, b) for a, b, c in suite),
   size=9, color=MUTED, spacing=1.55)

card(s, LEFT + 5.7, y + 0.05, CONTENT_W - 5.7, 3.2)
tx(s, LEFT + 6.02, y + 0.22, 5.0, 0.3, "评测指标（目标值）", size=12, color=ACCENT, bold=True)
rows = [
    ["指标", "怎么测", "目标"],
    ["追问有效性", "人工标注 100 轮，是否引用上文", "≥ 90%"],
    ["难度区分度", "同一考生 A/B/C 三档得分差", "A 档显著更低"],
    ["评分一致性", "与真人面试官评分算相关性", "r ≥ 0.6"],
    ["校准准确率", "回填结局后对得上的比例", "≥ 70%"],
    ["降级不中断", "文件缺失 / 损坏 / 结构异常", "一律不中断"],
    ["流程稳定性", "100 场自动测试", "0 次严重异常"],
]
table(s, LEFT + 6.02, y + 0.60, CONTENT_W - 6.35, [1.5, 3.4, 1.6], rows, row_h=0.36, head_size=10.5,
      body_size=9.5)
tx(s, LEFT, y + 3.42, CONTENT_W, 0.75,
   "诚实说明：评分一致性（r ≥ 0.6）、校准准确率（≥ 70%）与报告可用性（≥ 4.0）都需要真人对照实验，\n"
   "目前尚未完成——它们是后续工作，不是既有结论。回填结局这项功能已经做完，缺的是样本。",
   size=10.5, color=FAINT, spacing=1.4)
footer(s)

# =====================================================================
# 17 路线图
# =====================================================================
s = new_slide()
y = head(s, "ROADMAP", "21 个版本，四段递进",
         "每个版本只解决一个具体问题——先能跑，再能控，然后能交付，最后能自证准不准。")

phases = [
    ("能跑", "v1 – v3.2",
     "文本面试 + 三级配置 + 三体 Agent；\n简历针对性提问与语音交互；\n318 考点参照库；真实真题接入。", 0.0),
    ("能控", "v4 – v11",
     "简历 RAG；长会话压缩续面；\n工具调用；健壮性治理；隐私出口；\n历史对比 + 侧边栏重构；\n问评同源，修掉参照越界。", 2.928),
    ("能交付", "v12 – v15",
     "撤数字人换声波球；仓库瘦身\n10.4 → 1.7 GB；真题补到 4 专业；\n建议官 + 简历优化官；\n口播链路加固。", 5.856),
    ("可校准", "v16 – v17",
     "结局回填 + 评分校准；\n考点级判定，判分引用原话；\nv17 JD 对照 + 定向开面试；\nveto 135 条生效（含行为面）。", 8.784),
]
CARD_W = 2.648
for name, span, desc, x in phases:
    card(s, LEFT + x, y + 0.1, CARD_W, 2.15)
    tx(s, LEFT + x + 0.24, y + 0.22, CARD_W - 0.48, 0.35, name, size=14, color=ACCENT, bold=True)
    tx(s, LEFT + x + 0.24, y + 0.62, CARD_W - 0.48, 0.28, span, size=9.5, color=FAINT)
    tx(s, LEFT + x + 0.24, y + 0.95, CARD_W - 0.42, 1.15, desc, size=8.5, color=MUTED, spacing=1.5)

tl_y = y + 2.75
rule(s, LEFT + 0.35, tl_y, CONTENT_W - 0.7, LINE, 2.0)
versions = ["v1", "v2", "v2.1", "v2.2", "v3", "v3.1", "v3.2", "v4", "v5", "v6", "v7", "v8", "v9", "v10",
            "v11", "v12", "v13", "v14", "v15", "v16", "v17"]
n = len(versions)
step = (CONTENT_W - 0.7) / (n - 1)
for i, v in enumerate(versions):
    cx = LEFT + 0.35 + i * step
    dot = s.shapes.add_shape(MSO_SHAPE.OVAL, Inches(cx - 0.045), Inches(tl_y - 0.045), Inches(0.09), Inches(0.09))
    dot.fill.solid()
    dot.fill.fore_color.rgb = ACCENT
    dot.line.fill.background()
    no_shadow(dot)
    tx(s, cx - 0.32, tl_y + 0.16, 0.64, 0.26, v, size=8, color=FAINT, align=PP_ALIGN.CENTER)

tx(s, LEFT, tl_y + 0.7, CONTENT_W, 0.65,
   "被实测推着走的三次：v11 是评估官越界的真实缺陷（17 → 2 条参照）；v12 是主动放弃数字人方案的体积与伦理取舍；\n"
   "v16 是想清楚「评分只是预测」之后补的校准闭环——功能先做完，缺的是真实样本。",
   size=10.5, color=FAINT, spacing=1.4)
footer(s)

# =====================================================================
# 18 局限与边界
# =====================================================================
s = new_slide()
y = head(s, "LIMITS", "局限与边界：哪些事它做不到",
         "答辩里最容易被追问的往往不是「做了什么」，而是「在哪会失效」——所以先把边界写清楚。")
rows = [
    ["局限", "当前状态", "原因 / 代价"],
    ["专业覆盖", "6 专业（计算机 + 电子电气一簇），其余大类选不到", "新增专业需补齐 5 维度 × 3 档 × 15 考点起 + 45 条参照，不是加个名字"],
    ["真题许可", "CS-Notes 为非商业协议", "本项目为参赛作品符合要求；商用前必须移除受该协议约束的数据"],
    ["考点锚定率", "技术面实测 64%\n行为面已有独立参照库", "面试官自拟考点（如「项目经历」）不在库内时，面试官侧仍安静留空（不硬塞不相干参照）；评估官侧 v17 起改为现场现拟要点并标 source=generated，报告分区显示"],
    ["评分一致性", "尚未与真人面试官完成对照实验", "r ≥ 0.6 是目标值，不是既有结论"],
    ["改写防编造", "只核对数字是否在原文出现过", "不判断语义归属：40 同时存在于「40 分钟」时，「提升 40%」会被放行"],
]
table(s, LEFT, y + 0.05, CONTENT_W, [1.9, 4.2, 5.2], rows, row_h=0.55, head_size=12, body_size=10.5)

quote_band(s, y + 3.42, "一个设计判断：宁可承认边界，也不做「看起来能覆盖一切」的空壳——空壳专业会毁掉评分参照的一对一关系。")
footer(s)

# =====================================================================
# 19 结语
# =====================================================================
s = new_slide()
y = head(s, "SUMMARY", "三个可复用的判断")
judgements = [
    ("01", "把「判断」显式化成数据结构",
     "让模型输出决策 JSON，而不是让它「自由发挥」。可控、可复现、可解释，报告里能逐轮回看。\n"
     "代价是多写了字段与兜底，收益是出了问题能定位到「第几轮」。"),
    ("02", "能不用框架就不用框架",
     "行动空间只有 4 个动作时，自研 200 行决策循环比引入 ReAct 更可控；数据量小时，字典查表比向量库更省。\n"
     "少一层依赖，就多一分可解释性。"),
    ("03", "先承认边界，再谈能力",
     "覆盖 6 个专业就直说 6 个；真题有许可约束就写明约束；没做完的对照实验就标成后续工作。\n"
     "答辩时被追问的永远不是「做得少」，而是「说得多」。"),
]
jy = y + 0.05
for num, title, body in judgements:
    tx(s, LEFT, jy, 0.6, 0.35, num, size=13, color=ACCENT, bold=True)
    tx(s, LEFT + 0.7, jy - 0.04, CONTENT_W - 0.7, 0.4, title, size=17, color=INK, bold=True)
    tx(s, LEFT + 0.7, jy + 0.42, CONTENT_W - 0.7, 0.7, body, size=11, color=MUTED, spacing=1.5)
    jy += 1.30

rule(s, LEFT, jy + 0.10, CONTENT_W, LINE, 1.0)
tx(s, LEFT, jy + 0.32, CONTENT_W, 0.45, "谢谢，请老师批评指正。", size=19, color=INK, bold=True)
tx(s, SLIDE_W - LEFT - 2.4, jy + 0.38, 2.4, 0.35, "知遇 · 2026", size=11, color=FAINT, align=PP_ALIGN.RIGHT)
footer(s)

out = "知遇-AI虚拟面试官-答辩.pptx"
prs.save(out)

# ---------- 版式自检：按字符数估算真实文字高度，找出压到页脚区的对象 ----------
import math

print("saved:", out, "slides:", len(prs.slides._sldIdLst))
SAFE_BOTTOM = 6.86


def est_text_height_in(shape):
    """粗略估算文本框里文字实际占的高度（英寸）。CJK 记 1 个单位宽，西文记 0.55。"""
    tf = shape.text_frame
    width_pt = (shape.width / 914400) * 72 - 2
    total = 0.0
    for p in tf.paragraphs:
        runs = p.runs
        if not runs:
            total += 6
            continue
        size = float(runs[0].font.size.pt) if runs[0].font.size else 14.0
        text = "".join(r.text for r in runs)
        eff = sum(1.0 if ord(ch) > 0x2E80 else 0.55 for ch in text)
        cpl = max(1.0, width_pt / size)
        lines = max(1, math.ceil(eff / cpl))
        spacing = p.line_spacing if isinstance(p.line_spacing, float) else 1.2
        total += lines * size * spacing * 1.06
        if p.space_after is not None:
            total += p.space_after.pt
    return total / 72


for idx, sl in enumerate(prs.slides, 1):
    for sh in sl.shapes:
        if not sh.has_text_frame or not sh.text_frame.text.strip():
            continue
        top = sh.top / 914400
        if top >= 6.85:
            continue
        bottom = top + est_text_height_in(sh)
        if bottom > SAFE_BOTTOM:
            label = sh.text_frame.text.strip().replace("\n", " / ")[:40]
            print(f"  [可能溢出] p{idx:02d} est_bottom={bottom:.2f} :: {label}")


