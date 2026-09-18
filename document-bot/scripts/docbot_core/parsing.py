from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .storage import BotError


SUPPORTED = {".pdf", ".docx", ".md", ".markdown", ".txt"}


@dataclass
class Passage:
    text: str
    locator: dict


def parse(path):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise BotError("unsupported_format", "只支援文字 PDF、DOCX、Markdown、TXT。")
    passages, warnings = [], []
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise BotError("encrypted_pdf", "加密 PDF 不支援，請提供未加密的附件。")
        for page_no, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            if text.strip():
                passages.append(Passage(text, {"type": "pdf", "page": page_no}))
            else:
                warnings.append(f"第 {page_no} 頁無可抽取文字；此頁未建立索引，第一版不提供 OCR。")
    elif suffix == ".docx":
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        document = Document(path)
        paragraph_no, table_no, heading = 0, 0, ""
        for block in document.iter_inner_content():
            if isinstance(block, Paragraph):
                paragraph_no += 1
                if block.style and block.style.name.startswith("Heading"):
                    heading = block.text
                if block.text.strip():
                    passages.append(Passage(block.text, {"type": "docx", "paragraph": paragraph_no, "section": heading}))
            elif isinstance(block, Table):
                table_no += 1
                for row_no, row in enumerate(block.rows, 1):
                    text = " | ".join(c.text for c in row.cells)
                    if text.strip(" |"):
                        passages.append(Passage(text, {"type": "docx", "table": table_no, "row": row_no, "section": heading}))
    else:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise BotError("unsupported_encoding", "文字附件必須是 UTF-8，請重新匯出後上傳。") from exc
        lines, buffer, start, heading = text.splitlines(), [], 1, ""
        fenced = False
        for number, line in enumerate(lines, 1):
            is_heading = suffix != ".txt" and not fenced and re.match(r"^#{1,6}\s", line)
            if (not line.strip() or is_heading) and buffer:
                passages.append(Passage("\n".join(buffer), {"type": "text", "line_start": start, "line_end": number - 1, "section": heading}))
                buffer = []
            if is_heading:
                heading = line.lstrip("# ")
            if line.lstrip().startswith(("```", "~~~")):
                fenced = not fenced
            if line.strip() or (fenced and buffer):
                if not buffer:
                    start = number
                buffer.append(line)
        if buffer:
            passages.append(Passage("\n".join(buffer), {"type": "text", "line_start": start, "line_end": len(lines), "section": heading}))
    if not passages:
        raise BotError("no_extractable_text", "文件沒有可抽取文字；掃描 PDF 需要 OCR，第一版尚未支援。")
    return passages, warnings


def chunk(passages, tokenizer, maximum=400, overlap=60):
    """Slice original text by token offsets so citations never quote decoded approximations."""
    output = []
    for passage in passages:
        offsets = tokenizer(passage.text, add_special_tokens=False, return_offsets_mapping=True)["offset_mapping"]
        for start in range(0, len(offsets), maximum - overlap):
            end = min(start + maximum, len(offsets))
            char_start, char_end = offsets[start][0], offsets[end - 1][1]
            excerpt = passage.text[char_start:char_end]
            if not excerpt.strip():
                continue
            locator = dict(passage.locator)
            locator.update(char_start=char_start, char_end=char_end)
            if locator["type"] == "text":
                locator["line_start"] = passage.locator["line_start"] + passage.text[:char_start].count("\n")
                locator["line_end"] = locator["line_start"] + excerpt.count("\n")
            output.append(Passage(excerpt, locator))
            if end == len(offsets):
                break
    return output
