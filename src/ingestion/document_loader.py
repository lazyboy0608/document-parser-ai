from pathlib import Path
from typing import Dict, Any, Optional

from .pdf_parser import extract_text_from_pdf
from .docx_parser import extract_text_from_docx
from .doc_parser import extract_text_from_doc
from .ocr_engine import extract_text_from_image


class DocumentLoader:
    """
    Bộ nạp tài liệu tự động nhận dạng định dạng: PDF, DOCX, DOC, TXT, Hình ảnh.
    """
    SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt", ".png", ".jpg", ".jpeg", ".tiff", ".bmp"}

    @classmethod
    def load_document(cls, file_path: Path) -> Dict[str, Any]:
        ext = file_path.suffix.lower()
        if ext not in cls.SUPPORTED_EXTENSIONS:
            raise ValueError(f"Định dạng tệp không được hỗ trợ: {ext}")

        text = ""
        is_scanned = False
        page_images = []
        ocr_lines = []

        if ext == ".pdf":
            from config.settings import settings
            text, is_scanned, page_images, ocr_lines = extract_text_from_pdf(
                file_path,
                head_pages=settings.HEAD_PAGES,
                tail_pages=settings.TAIL_PAGES,
                ocr_lang=settings.OCR_LANG,
                return_ocr_lines=True,
            )
        elif ext == ".docx":
            text = extract_text_from_docx(file_path)
        elif ext == ".doc":
            text = extract_text_from_doc(file_path)
        elif ext == ".txt":
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        elif ext in {".png", ".jpg", ".jpeg", ".tiff", ".bmp"}:
            from .ocr_engine import extract_text_and_scores_from_image
            text, ocr_lines = extract_text_and_scores_from_image(file_path)
            is_scanned = True
            page_images = [file_path]

        return {
            "file_name": file_path.name,
            "file_path": str(file_path),
            "file_ext": ext,
            "text": text,
            "is_scanned": is_scanned,
            "page_images": page_images,
            "ocr_lines": ocr_lines,
        }

