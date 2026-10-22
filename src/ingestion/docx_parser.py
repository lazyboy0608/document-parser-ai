from pathlib import Path
import re
from typing import List, Tuple
import docx
from docx.text.paragraph import Paragraph
from docx.table import Table


def _extract_blocks_from_element(element, doc) -> List[Tuple[str, str]]:
    """
    Trích xuất các khối văn bản (Paragraphs, Tables, SDT Structured Controls)
    theo đúng thứ tự tài liệu tự nhiên trong cấu trúc XML của Word.
    """
    blocks = []
    for child in element.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            t = p.text.strip()
            if t:
                blocks.append(("P", t))
        elif tag == "tbl":
            tbl = Table(child, doc)
            rows = []
            for row in tbl.rows:
                row_t = [c.text.strip() for c in row.cells if c.text.strip()]
                if row_t:
                    rows.append(" | ".join(row_t))
            t = "\n".join(rows)
            if t:
                blocks.append(("T", t))
        elif tag == "sdt":
            # Duyệt đệ quy bên trong Structured Document Tag (như Mục lục tự động TOC của Word)
            sdt_content = [c for c in child.iterchildren() if c.tag.endswith("}sdtContent")]
            container = sdt_content[0] if sdt_content else child
            blocks.extend(_extract_blocks_from_element(container, doc))
    return blocks


def extract_text_from_docx(
    file_path: Path,
    max_head_chars: int = 12000,
    tail_blocks_count: int = 2,
    full_text: bool = False,
) -> str:
    """
    Trích xuất văn bản từ tệp Word .docx theo cơ chế Smart Content-Aware Sampling:
    1. Duyệt khối tuần tự theo thứ tự tự nhiên (Paragraphs, Tables, SDT TOC).
    2. Head Sampling: Thu thập phần bìa, mục lục, bảng biểu thông tin, danh mục viết tắt.
       Tự động Dừng sớm (Early Stopping) khi chạm vào thân bài (Chương 1 / Phần 1 / Section 1)
       hoặc khi đã thu thập đủ ~8.500 ký tự (tương đương 5-7 trang đầu PDF).
    3. Tail Sampling: Lấy mẫu các khối cuối tài liệu (Bảng xác nhận, phê duyệt, chữ ký người hướng dẫn).
    4. Tránh hoàn toàn việc nạp toàn bộ hàng chục trang thân bài gây quá tải LLM và làm lệch SimHash.
    """
    doc = docx.Document(file_path)
    all_blocks = _extract_blocks_from_element(doc.element.body, doc)

    if not all_blocks:
        return ""

    if full_text:
        return "\n\n".join(text for _, text in all_blocks).strip()

    body_stop_regex = re.compile(
        r"^\s*(CHƯƠNG\s+[0-9IVX]+|PHẦN\s+[0-9IVX]+|1\.\s+TỔNG\s+QUAN|1\.\s+ĐẶT\s+VẤN\s+ĐỀ|CHAPTER\s+[0-9IVX]+|SECTION\s+[0-9IVX]+|1\.\s+INTRODUCTION)\b",
        re.IGNORECASE,
    )
    # Bỏ qua các dòng trong mục lục (thường kết thúc bằng số trang hoặc chuỗi dấu chấm/tab số)
    toc_line_regex = re.compile(r"\t\d+$|\.{2,}\s*\d+$")

    head_blocks: List[str] = []
    curr_head_chars = 0
    stop_idx = len(all_blocks)

    for idx, (b_type, text) in enumerate(all_blocks):
        first_line = text.split("\n")[0].strip()

        # Dừng sớm khi phát hiện bắt đầu thân bài (sau khối bìa và mục lục ban đầu)
        if idx > 3 and body_stop_regex.search(first_line) and not toc_line_regex.search(first_line):
            stop_idx = idx
            break

        head_blocks.append(text)
        curr_head_chars += len(text)

        # Ngưỡng dung lượng tối đa cho phần đầu nếu tài liệu không có tiêu đề phân đoạn
        if curr_head_chars >= max_head_chars:
            stop_idx = idx + 1
            break

    # Nếu tài liệu ngắn, toàn bộ khối nằm trọn trong head_blocks
    if stop_idx >= len(all_blocks) - tail_blocks_count:
        return "\n\n".join(head_blocks).strip()

    # Thu thập Tail Sampling từ cuối tài liệu (bảng xác nhận, phê duyệt, chữ ký)
    tail_blocks: List[str] = []
    start_tail_idx = max(stop_idx, len(all_blocks) - tail_blocks_count)
    for b_type, text in all_blocks[start_tail_idx:]:
        tail_blocks.append(text)

    sampled_parts = list(head_blocks)
    if tail_blocks:
        sampled_parts.append("--- [Phần kết & Xác nhận phê duyệt] ---")
        sampled_parts.extend(tail_blocks)

    return "\n\n".join(sampled_parts).strip()
