from pathlib import Path
import docx


def extract_text_from_docx(file_path: Path) -> str:
    """
    Trích xuất toàn bộ nội dung văn bản từ tệp Word .docx.
    """
    doc = docx.Document(file_path)
    full_text = []
    for para in doc.paragraphs:
        if para.text.strip():
            full_text.append(para.text.strip())
    
    # Đọc thêm bảng biểu nếu có
    for table in doc.tables:
        for row in table.rows:
            row_text = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if row_text:
                full_text.append(" | ".join(row_text))
                
    return "\n".join(full_text)
