from pathlib import Path
from typing import Tuple, List, Optional, Dict, Any
import pymupdf as fitz

from .ocr_engine import extract_text_and_scores_from_image
from config.settings import settings


def extract_text_from_pdf(
    file_path: Path,
    head_pages: int = 2,
    tail_pages: int = 1,
    ocr_lang: str = "vi",
    min_char_threshold: int = 30,
    return_ocr_lines: bool = False,
):
    """
    Trích xuất văn bản từ tệp PDF:
    - Nếu PDF có text layer sẵn -> trích xuất trực tiếp siêu tốc qua PyMuPDF.
    - Nếu trang PDF là bản scan (dưới min_char_threshold ký tự) -> render ảnh và gọi PaddleOCR.
    - Lấy thông minh các trang đầu (head_pages) và trang cuối (tail_pages) nhằm tối ưu tốc độ.
    """
    doc = fitz.open(file_path)
    total_pages = len(doc)
    if total_pages == 0:
        return ("", False, [], []) if return_ocr_lines else ("", False, [])

    # Xác định các trang cần đọc để lấy metadata
    target_page_indices: List[int] = []
    # Các trang đầu
    for i in range(min(head_pages, total_pages)):
        if i not in target_page_indices:
            target_page_indices.append(i)
    # Các trang cuối
    for i in range(max(0, total_pages - tail_pages), total_pages):
        if i not in target_page_indices:
            target_page_indices.append(i)
    target_page_indices.sort()

    full_texts: List[str] = []
    has_scanned_page = False
    page_images: List[Path] = []
    all_ocr_lines: List[Dict[str, Any]] = []
    rendered_dir = settings.CACHE_DIR / "rendered_pages" / file_path.stem
    rendered_dir.mkdir(parents=True, exist_ok=True)

    # Kiểm tra cấp tài liệu: Nếu các trang mục tiêu có nhiều text layer số, đây là PDF số (digital)
    total_digital_chars = sum(len(doc[p].get_text("text").strip()) for p in target_page_indices)
    is_doc_digital = total_digital_chars >= (min_char_threshold * len(target_page_indices))

    for page_idx in target_page_indices:
        page = doc[page_idx]
        text = page.get_text("text").strip()

        # Kiểm tra nếu trang này là scan hoặc text layer bị lỗi/thiếu sót (như trang scan có OCR ẩn bị lỗi)
        junk_margin = text.count("I\n") > 5 or text.count("|\n") > 5
        has_page_image = len(page.get_images()) > 0

        alphanumeric_count = sum(1 for c in text if c.isalnum())
        ratio = alphanumeric_count / max(1, len(text))
        junk_count = sum(1 for c in text if c in "~<>\\|`^_{}[]*\ufffd:")
        is_garbled_layer = has_page_image and (
            ratio < 0.68
            or (junk_count / max(1, len(text)) > 0.04)
            or junk_margin
            or (page_idx == 0 and len(text) < 150)
        )

        if len(text) < min_char_threshold or is_garbled_layer:
            is_scanned_page = True
        else:
            is_scanned_page = False

        if is_scanned_page:
            has_scanned_page = True
            # Render trang PDF thành ảnh độ nét cao (200 DPI) để phục vụ OCR & VLM
            pix = page.get_pixmap(dpi=200, alpha=False)
            img_path = rendered_dir / f"page_{page_idx + 1}.png"
            pix.save(str(img_path))
            page_images.append(img_path)

            # Giải pháp 1: Tự động trích xuất vùng quan tâm (RoI) Crop-Zoom 300 DPI cho trang phê duyệt/tác giả (trang 2)
            if page_idx == 1:
                w_p, h_p = page.rect.width, page.rect.height
                roi_rect = fitz.Rect(0.42 * w_p, 0.54 * h_p, 0.96 * w_p, 0.86 * h_p)
                pix_roi = page.get_pixmap(clip=roi_rect, dpi=300, alpha=False)
                roi_img_path = rendered_dir / f"page_{page_idx + 1}_roi_author.png"
                pix_roi.save(str(roi_img_path))
                page_images.append(roi_img_path)

            try:
                ocr_text, ocr_lines = extract_text_and_scores_from_image(img_path, lang=ocr_lang)
                for item in ocr_lines:
                    item["page"] = page_idx + 1
                all_ocr_lines.extend(ocr_lines)
                if ocr_text.strip():
                    full_texts.append(f"--- [Trang {page_idx + 1} (Scan/OCR)] ---\n{ocr_text}")
                elif text:
                    full_texts.append(f"--- [Trang {page_idx + 1}] ---\n{text}")
            except Exception:
                if text:
                    full_texts.append(f"--- [Trang {page_idx + 1}] ---\n{text}")
        else:
            full_texts.append(f"--- [Trang {page_idx + 1}] ---\n{text}")

    doc.close()
    if return_ocr_lines:
        return "\n\n".join(full_texts), has_scanned_page, page_images, all_ocr_lines
    return "\n\n".join(full_texts), has_scanned_page, page_images


