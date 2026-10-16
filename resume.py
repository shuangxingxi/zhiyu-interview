"""简历解析：把上传的简历文件转成纯文本。

隐私设计：
- 全程在内存中处理，不写任何临时文件、不落盘；
- 调用方把结果存进会话内存（st.session_state），面试结束即丢弃；
- 本模块不负责持久化，也不应把简历内容写进任何记录文件。
"""

from __future__ import annotations

import docx
import fitz

# 简历通常一两页。v4 起简历走「切片 + 按话题聚焦检索」，单轮注入量有上界，
# 所以上限放宽到 6000 字，长简历也不会因此把每轮上下文撑爆；超出部分截断。
MAX_CHARS = 6000

ACCEPTED_TYPES = ["pdf", "docx", "txt"]


def extract_text(filename: str, data: bytes) -> tuple[str, str]:
    """解析简历文件，返回 (纯文本, 错误提示)。

    解析成功时错误提示为空串；失败时纯文本为空串并给出原因。
    """
    name = (filename or "").lower()
    try:
        if name.endswith(".pdf"):
            text = _from_pdf(data)
        elif name.endswith(".docx"):
            text = _from_docx(data)
        elif name.endswith((".txt", ".md")):
            text = _from_txt(data)
        else:
            return "", "只支持 PDF、Word（.docx）和纯文本文件。"
    except Exception as exc:  # noqa: BLE001
        return "", f"解析失败：{exc}"

    text = _tidy(text)
    if not text:
        return "", "没能从这份文件里读出文字。如果是扫描件或图片版 PDF，请改用 Word 或纯文本。"
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n（简历过长，后面部分已省略）"
    return text, ""


def _from_pdf(data: bytes) -> str:
    with fitz.open(stream=data, filetype="pdf") as document:
        return "\n".join(page.get_text() for page in document)


def _from_docx(data: bytes) -> str:
    from io import BytesIO

    document = docx.Document(BytesIO(data))
    parts = [p.text for p in document.paragraphs]
    # 简历常用表格排版，表格内容也要取出来
    for table in document.tables:
        for row in table.rows:
            parts.append("\t".join(cell.text for cell in row.cells))
    return "\n".join(parts)


def _from_txt(data: bytes) -> str:
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _tidy(text: str) -> str:
    """去掉空行和行首尾空白，让简历更适合塞进 prompt。"""
    return "\n".join(line.strip() for line in (text or "").splitlines() if line.strip())
