import re
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

    # Xác định các trang cần đọc để lấy metadata:
    # 1. Các trang đầu cố định (head_pages)
    target_page_indices: List[int] = list(range(min(head_pages, total_pages)))

    # 2. Quét nhanh (Dynamic Content Discovery) từ trang 5 đến tối đa trang 20:
    # Bắt các trang Phụ lục (Enclosures), Mục lục (Contents), Phê duyệt (Approval)
    # Tốc độ PyMuPDF get_text trên bộ nhớ cực nhanh (< 2ms/trang)
    content_discovery_keywords = re.compile(
        r"(enclosure|phụ\s*bản|phụ\s*lục|приложени|contents|mục\s*lục|approval|phê\s*duyệt|signoff|sign-off)",
        re.IGNORECASE
    )
    for p_idx in range(min(head_pages, total_pages), min(20, total_pages)):
        try:
            sample_text = doc[p_idx].get_text("text")[:1000]
            if content_discovery_keywords.search(sample_text):
                if p_idx not in target_page_indices:
                    target_page_indices.append(p_idx)
        except Exception:
            pass

    # 3. Các trang cuối (tail_pages)
    for i in range(max(0, total_pages - tail_pages), total_pages):
        if i not in target_page_indices:
            target_page_indices.append(i)
    target_page_indices.sort()

    full_texts: List[str] = []
    page_images: List[Path] = []
    all_ocr_lines: List[Dict[str, Any]] = []
    rendered_dir = settings.CACHE_DIR / "rendered_pages" / file_path.stem
    rendered_dir.mkdir(parents=True, exist_ok=True)

    digital_pages_count = 0
    scanned_pages_count = 0

    for page_idx in target_page_indices:
        page = doc[page_idx]
        text = page.get_text("text", flags=fitz.TEXT_PRESERVE_LIGATURES | fitz.TEXT_DEHYPHENATE).strip()

        # Render ảnh độ nét cao cho tất cả các trang mục tiêu (để sẵn sàng cho VLM nếu cần)
        img_path = rendered_dir / f"page_{page_idx + 1}.png"
        if not img_path.exists():
            pix = page.get_pixmap(dpi=200, alpha=False)
            pix.save(str(img_path))
        page_images.append(img_path)

        has_page_image = len(page.get_images()) > 0
        alphanumeric_count = sum(1 for c in text if c.isalnum())
        ratio = alphanumeric_count / max(1, len(text))
        junk_count = sum(1 for c in text if c in "~<>\\|`^_{}[]*\ufffd:")

        # Một trang được coi là có text số sạch khi có đủ ký tự, tỷ lệ chữ cái tự nhiên cao và ít ký tự rác
        is_clean_digital = (
            len(text) >= min_char_threshold
            and ratio >= 0.65
            and (junk_count / max(1, len(text)) <= 0.05)
            and text.count("I\n") <= 4
            and text.count("|\n") <= 4
        )

        if is_clean_digital:
            digital_pages_count += 1
            full_texts.append(f"--- [Trang {page_idx + 1} (Digital)] ---\n{text}")
        else:
            scanned_pages_count += 1
            # Trang scan hoặc text layer bị lỗi -> gọi PaddleOCR
            try:
                ocr_text, ocr_lines = extract_text_and_scores_from_image(img_path, lang=ocr_lang)
                for item in ocr_lines:
                    item["page"] = page_idx + 1
                all_ocr_lines.extend(ocr_lines)
                if ocr_text.strip():
                    full_texts.append(f"--- [Trang {page_idx + 1} (Scan/OCR)] ---\n{ocr_text}")
                elif text:
                    full_texts.append(f"--- [Trang {page_idx + 1} (Fallback)] ---\n{text}")
            except Exception:
                if text:
                    full_texts.append(f"--- [Trang {page_idx + 1} (Fallback)] ---\n{text}")

    doc.close()

    # Phân loại tài liệu tổng thể
    if scanned_pages_count == 0:
        doc_type = "digital"
    elif digital_pages_count == 0:
        doc_type = "scan"
    else:
        doc_type = "hybrid"

    has_scanned_page = scanned_pages_count > 0

    if return_ocr_lines:
        return "\n\n".join(full_texts), has_scanned_page, page_images, all_ocr_lines
    return "\n\n".join(full_texts), has_scanned_page, page_images


