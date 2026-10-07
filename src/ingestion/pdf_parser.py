import re
from pathlib import Path
from typing import Tuple, List, Optional, Dict, Any
import pymupdf as fitz

from .ocr_engine import extract_text_and_scores_from_image
from config.settings import settings


def _is_blank_or_flyleaf(page: fitz.Page) -> bool:
    """
    Kiểm tra trang giấy trắng hoặc trang lót rỗng (flyleaf) trong mili-giây:
    1. Nếu có text số sạch >= 25 ký tự -> chắc chắn là trang có nội dung thật.
    2. Nếu text < 25 ký tự: render thumbnail xám 50 DPI (< 2ms) và tính độ phủ pixel tối.
       Nếu tỷ lệ pixel tối < 0.4% -> trang trắng hoặc chỉ có hạt bụi scan/mộc lưu trữ mờ.
    """
    try:
        text = page.get_text("text").strip()
        alpha_count = sum(1 for c in text if c.isalnum())
        if alpha_count >= 25:
            return False

        # Kiểm tra nhanh ảnh scan độ phân giải thấp 50 DPI
        pix = page.get_pixmap(dpi=50, colorspace=fitz.csGRAY)
        samples = pix.samples
        if not samples:
            return True

        total_pixels = len(samples)
        if total_pixels == 0:
            return True

        # Đếm số pixel có màu tối (pixel < 220, thang 0-255 với 255 là trắng tinh)
        dark_pixels = sum(1 for b in samples if b < 220)
        dark_ratio = dark_pixels / total_pixels

        # Nếu tỷ lệ pixel tối < 0.004 (dưới 0.4% diện tích trang) -> trang giấy trắng/rỗng
        return dark_ratio < 0.004
    except Exception:
        return False


def _scan_header_slice_for_keywords(
    page: fitz.Page,
    keywords_regex: re.Pattern,
    temp_dir: Path,
    ocr_lang: str = "vi",
) -> bool:
    """
    Cắt 20% trên cùng của trang scan (dải băng Header) để OCR siêu tốc (< 0.3s)
    tìm kiếm từ khóa Mục lục / Phụ bản (ENCLOSURE, CONTENTS, PHỤ BẢN...).
    """
    try:
        # Cắt 20% chiều cao trên cùng của trang
        rect = fitz.Rect(0, 0, page.rect.width, page.rect.height * 0.20)
        slice_pix = page.get_pixmap(dpi=150, clip=rect, alpha=False)
        slice_path = temp_dir / f"slice_top_{page.number + 1}.png"
        slice_pix.save(str(slice_path))

        ocr_text, _ = extract_text_and_scores_from_image(slice_path, lang=ocr_lang)
        try:
            slice_path.unlink()
        except Exception:
            pass

        return bool(keywords_regex.search(ocr_text))
    except Exception:
        return False


def extract_text_from_pdf(
    file_path: Path,
    head_pages: int = 5,
    tail_pages: int = 1,
    backward_scan_pages: Optional[int] = None,
    ocr_lang: str = "vi",
    min_char_threshold: int = 30,
    return_ocr_lines: bool = False,
):
    """
    Trích xuất văn bản từ tệp PDF thông minh (Smart Content-Aware Page Sampling):
    1. Nhảy cóc qua các trang trắng/trang lót scan để gom đủ `head_pages` trang có nội dung thật.
    2. Quét nhanh động (Dynamic Discovery) co giãn theo độ dày tài liệu kèm cơ chế dừng sớm (Early Stopping).
    3. Quét ngược thông minh (Smart Backward Discovery): Quét lùi từ các trang áp chót để bắt trọn phụ bản nằm ở cuối file.
    4. Quét dải băng Header Top 20% cho trang scan thuần để không bỏ sót bảng phụ lục scan.
    5. Trích xuất text số gốc qua PyMuPDF hoặc gọi Mobile OCR nếu là trang scan.
    """
    doc = fitz.open(file_path)
    total_pages = len(doc)
    if total_pages == 0:
        return ("", False, [], []) if return_ocr_lines else ("", False, [])

    if backward_scan_pages is None:
        backward_scan_pages = getattr(settings, "BACKWARD_SCAN_MAX_PAGES", 6)

    rendered_dir = settings.CACHE_DIR / "rendered_pages" / file_path.stem
    rendered_dir.mkdir(parents=True, exist_ok=True)

    # =========================================================================
    # BƯỚC 1: LỌC TRANG ĐẦU CÓ NỘI DUNG THẬT (BỎ QUA TRANG TRẮNG / TRANG LÓT)
    # =========================================================================
    target_page_indices: List[int] = []
    curr_idx = 0
    max_search_head = min(12, total_pages)
    while len(target_page_indices) < head_pages and curr_idx < max_search_head:
        if not _is_blank_or_flyleaf(doc[curr_idx]):
            target_page_indices.append(curr_idx)
        curr_idx += 1

    # Nếu tài liệu có toàn trang scan mờ hoặc không tìm đủ, đảm bảo lấy các trang đầu tiên
    if not target_page_indices and total_pages > 0:
        target_page_indices = list(range(min(head_pages, total_pages)))

    # =========================================================================
    # BƯỚC 2: QUÉT ĐỘNG CO GIÃN THEO ĐỘ DÀY & DỪNG SỚM (EARLY STOPPING)
    # =========================================================================
    start_dynamic_scan = (max(target_page_indices) + 1) if target_page_indices else head_pages
    # Tự động co giãn theo độ dày: tài liệu 40 trang quét tới trang 25; 100 trang quét tới trang 35
    max_scan_page = min(total_pages, max(25, int(total_pages * 0.35)))

    content_discovery_keywords = re.compile(
        r"(enclosure|phụ\s*bản|phụ\s*lục|приложени|contents|mục\s*lục|approval|phê\s*duyệt|signoff|sign-off|drawing\s*list)",
        re.IGNORECASE,
    )
    body_chapter_keywords = re.compile(
        r"\b(CHAPTER\s+[0-9I|V]+|SECTION\s+[0-9I|V]+|CHƯƠNG\s+[0-9I|V]+|1\.\s+INTRODUCTION|1\.\s+TỔNG\s+QUAN)\b",
        re.IGNORECASE,
    )

    found_enclosure_or_toc = False

    for p_idx in range(start_dynamic_scan, max_scan_page):
        if p_idx in target_page_indices:
            continue
        try:
            page = doc[p_idx]
            sample_text = page.get_text("text")[:1000]
            has_digital_text = len(sample_text.strip()) >= 30

            if has_digital_text:
                # Nếu có text layer: kiểm tra từ khóa siêu tốc (< 1ms)
                if content_discovery_keywords.search(sample_text):
                    target_page_indices.append(p_idx)
                    found_enclosure_or_toc = True
                elif found_enclosure_or_toc and body_chapter_keywords.search(sample_text):
                    # Dừng sớm khi đã qua mục lục và chính thức bắt đầu Chương 1
                    break
            else:
                # Trang scan thuần (ảnh 100%): Quét dải băng tiêu đề Top 20% siêu tốc (~0.3s)
                # Chỉ quét dải băng cho tối đa 10 trang scan kế tiếp để tối ưu thời gian
                if p_idx <= start_dynamic_scan + 10:
                    if _scan_header_slice_for_keywords(
                        page, content_discovery_keywords, rendered_dir, ocr_lang=ocr_lang
                    ):
                        target_page_indices.append(p_idx)
                        found_enclosure_or_toc = True
        except Exception:
            pass

    # =========================================================================
    # BƯỚC 2.5: CƠ CHẾ QUÉT NGƯỢC THÔNG MINH (SMART BACKWARD DISCOVERY SCAN)
    # =========================================================================
    if backward_scan_pages > 0 and total_pages > head_pages:
        backward_limit = min(backward_scan_pages, total_pages)
        backward_start = total_pages - 1
        min_allowed_idx = (max(target_page_indices) + 1) if target_page_indices else head_pages

        backward_encl_keywords = re.compile(
            r"(enclosure|phụ\s*bản|phụ\s*lục|drawing\s*list|attachments|danh\s*mục\s*bản\s*vẽ|приложени)",
            re.IGNORECASE,
        )
        backward_body_stop_keywords = re.compile(
            r"\b(CONCLUSION|RECOMMENDATIONS|KẾT\s*LUẬN|KIẾN\s*NGHỊ|SUMMARY|TỔNG\s*KẾT)\b",
            re.IGNORECASE,
        )

        found_any_tail_enclosure = False
        tail_enclosure_pages: List[int] = []

        # Quét lùi từ trang áp chót về trước
        for p_idx in range(backward_start - 1, max(min_allowed_idx - 1, backward_start - backward_limit), -1):
            if p_idx in target_page_indices:
                continue
            try:
                page = doc[p_idx]
                # Bỏ qua nếu là trang giấy scan trắng rỗng
                if _is_blank_or_flyleaf(page):
                    continue

                sample_text = page.get_text("text")[:1000]
                has_digital_text = len(sample_text.strip()) >= 30

                is_enclosure_page = False
                if has_digital_text:
                    if backward_encl_keywords.search(sample_text):
                        is_enclosure_page = True
                    elif found_any_tail_enclosure:
                        # Đã gom các trang phụ bản phía sau, gặp trang không có từ khóa -> đã đến đầu danh mục phụ bản
                        break
                    elif backward_body_stop_keywords.search(sample_text):
                        # Đã lùi chạm vào phần Kết luận của thân bài trước khi thấy phụ bản -> dừng quét ngược
                        break
                else:
                    # Trang scan thuần: Cắt 20% dải băng đỉnh trang (Header slice) OCR trong ~0.3s
                    if _scan_header_slice_for_keywords(
                        page, backward_encl_keywords, rendered_dir, ocr_lang=ocr_lang
                    ):
                        is_enclosure_page = True
                    elif found_any_tail_enclosure:
                        break

                if is_enclosure_page:
                    tail_enclosure_pages.append(p_idx)
                    found_any_tail_enclosure = True
            except Exception:
                pass

        for p in tail_enclosure_pages:
            if p not in target_page_indices:
                target_page_indices.append(p)

    # =========================================================================
    # BƯỚC 3: CÁC TRANG CUỐI (TAIL PAGES - PHÊ DUYỆT / CHỮ KÝ)
    # =========================================================================
    tail_candidates: List[int] = []
    idx = total_pages - 1
    while idx >= 0 and len(tail_candidates) < tail_pages and idx >= min(head_pages, total_pages):
        try:
            if not _is_blank_or_flyleaf(doc[idx]):
                tail_candidates.append(idx)
        except Exception:
            tail_candidates.append(idx)
        idx -= 1

    # Nếu không lọc được trang nội dung nào, fallback lấy đúng các trang cuối
    if not tail_candidates:
        tail_candidates = list(range(max(0, total_pages - tail_pages), total_pages))

    for i in tail_candidates:
        if i not in target_page_indices:
            target_page_indices.append(i)

    target_page_indices = sorted(list(set(target_page_indices)))

    full_texts: List[str] = []
    page_images: List[Path] = []
    all_ocr_lines: List[Dict[str, Any]] = []

    digital_pages_count = 0
    scanned_pages_count = 0

    # =========================================================================
    # BƯỚC 4: BÓC TÁCH NỘI DUNG TỪNG TRANG MỤC TIÊU
    # =========================================================================
    for page_idx in target_page_indices:
        page = doc[page_idx]
        text = page.get_text("text", flags=fitz.TEXT_PRESERVE_LIGATURES | fitz.TEXT_DEHYPHENATE).strip()

        # Render ảnh độ nét cao cho tất cả các trang mục tiêu (để sẵn sàng cho VLM nếu cần)
        img_path = rendered_dir / f"page_{page_idx + 1}.png"
        if not img_path.exists():
            pix = page.get_pixmap(dpi=150, alpha=False)
            pix.save(str(img_path))
        page_images.append(img_path)

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
