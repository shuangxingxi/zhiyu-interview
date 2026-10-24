"""把 项目说明.md 生成为 项目说明.docx（一次性生成器，可复用）。

与 _build_design_docx.py 的分工：
- 那个脚本**复用 DESIGN.docx 的样式表**（从旧文件打开、清空正文再写），
  因为它要保住已交付文档的版式；
- 这个脚本**从空白模板新建**，因为 项目说明.docx 此前不存在，
  不能依赖任何已存在的 docx。Markdown 解析与表格/字体处理直接复用
  前者的函数，避免两套实现漂移。

与 DESIGN.docx 刻意保持一致的版式：A4、左右 3.17cm / 上下 2.54cm、
正文 Calibri + 微软雅黑、表格「Light Grid Accent 1」。
"""

from __future__ import annotations

import docx
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

from _build_design_docx import (
    HAN,
    LATIN,
    add_runs,
    build,
)

SRC = "项目说明.md"
DST = "项目说明.docx"


def setup_page(document: docx.Document) -> None:
    """A4 纸 + 与 DESIGN.docx 相同的页边距。"""
    section = document.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.left_margin = Cm(3.17)
    section.right_margin = Cm(3.17)
    section.top_margin = Cm(2.54)
    section.bottom_margin = Cm(2.54)


def setup_styles(document: docx.Document) -> None:
    """正文与各级标题都用「Calibri + 微软雅黑」，否则中文会落到默认字体。

    python-docx 的空白模板里 Normal 没有显式字体（实测 font.name 为 None），
    所以这里必须写全：西文名、hAnsi、eastAsia 三个属性都要设。
    """
    from docx.oxml.ns import qn

    normal = document.styles["Normal"]
    normal.font.name = LATIN
    normal.font.size = Pt(10.5)
    rpr = normal.element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    rfonts.set(qn("w:ascii"), LATIN)
    rfonts.set(qn("w:hAnsi"), LATIN)
    rfonts.set(qn("w:eastAsia"), HAN)

    for name in ("Heading 1", "Heading 2", "Heading 3"):
        style = document.styles[name]
        style.font.name = LATIN
        srpr = style.element.get_or_add_rPr()
        srfonts = srpr.get_or_add_rFonts()
        srfonts.set(qn("w:ascii"), LATIN)
        srfonts.set(qn("w:hAnsi"), LATIN)
        srfonts.set(qn("w:eastAsia"), HAN)


def normalize_table_widths(document: docx.Document) -> int:
    """把每张表的列宽归一，消除 gridCol 与 tcW 之间的取整偏差。

    `_build_design_docx.flush_table()` 里两处独立取整：
      col_w  = usable.twips // cols      → 向下取整
      cell_w = Emu(int(usable) // cols)  → 也向下取整，但 Emu 再转 twips 时又会
                                            引入一次舍入，实测 3 列表得到 2771
    于是 8312 twips 宽的表，gridCol 合计 8310、单元格却是 2771×3=8313，
    在 Word 里表现为表格右缘比正文边界多出几个 twips。数值很小、肉眼几乎不可见，
    但既然是固定布局（tblLayout=fixed），列宽自相矛盾就该修掉。

    做法：用最大余数法把 tblW 精确分配到各列，gridCol 与每个 tcW 写同一个值。
    返回修正过的表格数。
    """
    fixed = 0
    for table in document.tables:
        tbl = table._tbl
        tblPr = tbl.tblPr
        tblW = tblPr.find(qn("w:tblW"))
        if tblW is None:
            continue
        total = int(tblW.get(qn("w:w")))
        grid = tbl.tblGrid.findall(qn("w:gridCol"))
        cols = len(grid)
        if cols == 0:
            continue
        base, extra = divmod(total, cols)
        widths = [base + (1 if i < extra else 0) for i in range(cols)]
        for gc, w in zip(grid, widths):
            gc.set(qn("w:w"), str(w))
        for tr in tbl.findall(qn("w:tr")):
            for tc, w in zip(tr.findall(qn("w:tc")), widths):
                tcPr = tc.find(qn("w:tcPr"))
                if tcPr is None:
                    continue
                tcW = tcPr.find(qn("w:tcW"))
                if tcW is None:
                    continue
                tcW.set(qn("w:w"), str(w))
                tcW.set(qn("w:type"), "dxa")
        fixed += 1
    return fixed


def main() -> None:
    md = open(SRC, encoding="utf-8").read()
    document = docx.Document()          # 空白模板新建
    setup_page(document)
    setup_styles(document)

    title = md.split("\n", 1)[0].lstrip("# ").strip()
    para = document.add_paragraph(style="Heading 1")
    add_runs(para, title)
    para.alignment = WD_ALIGN_PARAGRAPH.LEFT

    rest = md.split("\n", 1)[1] if "\n" in md else ""
    tables, codes, headings = build(document, rest)
    fixed = normalize_table_widths(document)

    document.save(DST)
    print(f"  已生成 {DST}")
    print(f"  表格 {tables}（列宽已归一 {fixed} 张）/ 代码块 {codes} / 标题 {headings + 1}")


if __name__ == "__main__":
    main()
