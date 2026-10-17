"""把 DESIGN.md 重新生成为 DESIGN.docx（一次性生成器，可复用）。

设计决策：
1. **复用旧 docx 的 styles.xml**：从现有 DESIGN.docx 打开、清空正文后再写新内容，
   这样 Heading 1/2/3、List Bullet/Number、表格样式「Light Grid Accent 1」、
   Normal 字体（Calibri + eastAsia 微软雅黑）与页面设置全部原样保留——
   比从空白模板重建更不容易丢设计。
2. **中文字体显式写入 w:eastAsia**：仅设 run.font.name 不保证 CJK 字形，
   即使样式里已有也再写一次，避免个别 run 落到默认字体。
3. 正文只用样式定位，不手工设字号 —— 让 Word 自己按样式排版。
"""

from __future__ import annotations

import re

import docx
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Pt

SRC = "DESIGN.md"
DST = "DESIGN.docx"
TABLE_STYLE = "Light Grid Accent 1"
LATIN = "Calibri"
HAN = "微软雅黑"
MONO = "Consolas"


# ---------------------------------------------------------------------------
# 清空正文（保留 sectPr 与 styles.xml）
# ---------------------------------------------------------------------------


def clear_body(document: docx.Document) -> None:
    body = document.element.body
    for child in list(body):
        if child.tag == qn("w:sectPr"):
            continue
        body.remove(child)


# ---------------------------------------------------------------------------
# 行内格式：**粗体** 与 `代码`
# ---------------------------------------------------------------------------

_INLINE = re.compile(r"(\*\*(?P<bold>[^*]+)\*\*|`(?P<code>[^`]+)`)")


def add_runs(paragraph, text: str) -> None:
    """把一行文本按 **粗体** / `代码` 拆成多个 run，其余为常规文本。"""
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            _plain(paragraph, text[pos : m.start()])
        bold = m.group("bold")
        code = m.group("code")
        if bold is not None:
            run = paragraph.add_run(bold)
            run.bold = True
            _fonts(run, LATIN, HAN)
        elif code is not None:
            run = paragraph.add_run(code)
            _fonts(run, MONO, HAN)
            run.font.size = Pt(9.5)
        pos = m.end()
    if pos < len(text):
        _plain(paragraph, text[pos:])


def _plain(paragraph, text: str):
    if not text:
        return
    run = paragraph.add_run(text)
    _fonts(run, LATIN, HAN)
    return run


def _fonts(run, latin: str, han: str) -> None:
    """同时设置西文与东亚字体，否则中文可能落到默认字体。"""
    run.font.name = latin
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    rfonts.set(qn("w:ascii"), latin)
    rfonts.set(qn("w:hAnsi"), latin)
    rfonts.set(qn("w:eastAsia"), han)


# ---------------------------------------------------------------------------
# Markdown 解析
# ---------------------------------------------------------------------------

_SEP_ROW = re.compile(r"^\|[\s:|-]+\|$")
_LIST_NUM = re.compile(r"^(\d{1,2})\.\s+(.*)$")
_LIST_BUL = re.compile(r"^(\s*)-\s+(.*)$")


def split_row(line: str) -> list[str]:
    inner = line.strip().strip("|")
    # 支持转义竖线 \|
    parts = re.split(r"(?<!\\)\|", inner)
    return [p.replace("\\|", "|").strip() for p in parts]


def flush_table(document, rows: list[list[str]]) -> None:
    if not rows:
        return
    cols = max(len(r) for r in rows)
    table = document.add_table(rows=len(rows), cols=cols)
    try:
        table.style = TABLE_STYLE
    except KeyError:
        table.style = "Table Grid"

    # 关键：显式设定表格总宽 = 正文可用宽度，并锁定固定布局。
    # 旧 docx 的表格是 tblW=0 / type=auto，LibreOffice 会按内容把表格撑到页面
    # 最右缘（实测 254 行越界到 21.57cm，而正文右边界只有 18.42cm）。
    section = document.sections[0]
    usable = Emu(
        int(section.page_width) - int(section.left_margin) - int(section.right_margin)
    )
    tbl = table._tbl
    tblPr = tbl.tblPr
    tblW = tblPr.find(qn("w:tblW"))
    if tblW is None:
        tblW = OxmlElement("w:tblW")
        tblPr.append(tblW)
    tblW.set(qn("w:w"), str(int(usable.twips)))
    tblW.set(qn("w:type"), "dxa")
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tblPr.append(layout)
    # 网格列宽也要按可用宽度均分，否则固定布局仍会沿用旧网格
    col_w = int(usable.twips) // cols
    for gc in tbl.tblGrid.findall(qn("w:gridCol")):
        gc.set(qn("w:w"), str(col_w))

    cell_w = Emu(int(usable) // cols)
    for ri, row in enumerate(rows):
        for ci in range(cols):
            cell = table.cell(ri, ci)
            cell.width = cell_w
            cell.text = ""
            para = cell.paragraphs[0]
            para.paragraph_format.space_after = Pt(2)
            text = row[ci] if ci < len(row) else ""
            add_runs(para, text)
            if ri == 0:
                for r in para.runs:
                    r.bold = True
    document.add_paragraph()


def flush_code(document, lines: list[str]) -> None:
    for line in lines:
        para = document.add_paragraph()
        para.paragraph_format.space_after = Pt(0)
        para.paragraph_format.left_indent = Pt(12)
        run = para.add_run(line if line else " ")
        _fonts(run, MONO, HAN)
        run.font.size = Pt(9)
    document.add_paragraph()


def build(document: docx.Document, md: str) -> tuple[int, int, int]:
    lines = md.split("\n")
    tables = codes = headings = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 代码块
        if stripped.startswith("```"):
            codes += 1
            j = i + 1
            buf: list[str] = []
            while j < len(lines) and not lines[j].strip().startswith("```"):
                buf.append(lines[j])
                j += 1
            flush_code(document, buf)
            i = j + 1
            continue

        # 表格
        if stripped.startswith("|") and i + 1 < len(lines) and _SEP_ROW.match(lines[i + 1].strip()):
            tables += 1
            rows = [split_row(line)]
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append(split_row(lines[j]))
                j += 1
            flush_table(document, rows)
            i = j
            continue

        # 分隔线
        if re.match(r"^-{3,}$", stripped):
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            headings += 1
            para = document.add_paragraph(style=f"Heading {level}")
            add_runs(para, m.group(2))
            i += 1
            continue

        # 引用
        if stripped.startswith(">"):
            body = stripped.lstrip(">").strip()
            if body:
                para = document.add_paragraph(style="Intense Quote")
                add_runs(para, body)
            i += 1
            continue

        # 编号列表
        m = _LIST_NUM.match(stripped)
        if m:
            para = document.add_paragraph(style="List Number")
            add_runs(para, m.group(2))
            i += 1
            continue

        # 无序列表（含缩进层级）
        m = _LIST_BUL.match(line)
        if m:
            indent = len(m.group(1))
            style = "List Bullet 2" if indent >= 2 else "List Bullet"
            para = document.add_paragraph(style=style)
            add_runs(para, m.group(2))
            i += 1
            continue

        # 空行
        if not stripped:
            i += 1
            continue

        # 普通段落
        para = document.add_paragraph()
        add_runs(para, stripped)
        i += 1

    return tables, codes, headings


def main() -> None:
    md = open(SRC, encoding="utf-8").read()
    document = docx.Document(DST)          # 复用旧样式表
    clear_body(document)

    # 封面标题（原 Heading 1 已被清空，重建一个）
    title = md.split("\n", 1)[0].lstrip("# ").strip()
    para = document.add_paragraph(style="Heading 1")
    add_runs(para, title)
    para.alignment = WD_ALIGN_PARAGRAPH.LEFT

    rest = md.split("\n", 1)[1] if "\n" in md else ""
    tables, codes, headings = build(document, rest)

    document.save(DST)
    print(f"  已生成 {DST}")
    print(f"  表格 {tables} / 代码块 {codes} / 标题 {headings + 1}")


if __name__ == "__main__":
    main()
