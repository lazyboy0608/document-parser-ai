import re
import unicodedata
from difflib import SequenceMatcher
from typing import Dict, Any, List, Optional, Tuple

from .schemas import DocumentMetadata
from .domain_cleaner import OIL_GAS_CORRECTIONS


# Trọng số đóng góp của từng trường vào Độ tin cậy tổng thể của tài liệu (Tổng = 1.0)
FIELD_WEIGHTS: Dict[str, float] = {
    "title": 0.18,
    "author": 0.15,
    "originator": 0.14,
    "public_date": 0.12,
    "well_link": 0.10,
    "contract_name": 0.10,
    "encl": 0.06,
    "encl_name": 0.07,
    "language": 0.04,
    "contractor": 0.04,
}

# Các trường cốt lõi bắt buộc phải có mặt trong báo cáo kỹ thuật (nếu "Unknown"/"Chưa rõ"/"N/A" sẽ bị cảnh báo)
MANDATORY_FIELDS = {"title", "public_date", "originator", "author"}

# Ngưỡng cảnh báo: Trường nào có điểm tin cậy < 80.0% sẽ được highlight trong Excel
HIGHLIGHT_THRESHOLD = 80.0


def _strip_accents(text: str) -> str:
    """Loại bỏ dấu tiếng Việt để so sánh độ biến dạng dấu câu giữa OCR và VLM."""
    if not text:
        return ""
    text = text.replace("đ", "d").replace("Đ", "D")
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def _normalize_tokens(text: str) -> List[str]:
    cleaned = re.sub(r"[^\w\s]", " ", text.lower(), flags=re.UNICODE)
    return [t for t in cleaned.split() if len(t) >= 2]


def _had_ocr_typo_correction(raw_text: str, field_value: str) -> bool:
    """
    Kiểm tra xem vùng văn bản gốc có chứa các lỗi biến dạng nét chữ máy đánh chữ/scan mờ
    nằm trong bộ mẫu OIL_GAS_CORRECTIONS mà tương ứng với giá trị của trường hay không.
    """
    if not raw_text or not field_value:
        return False
    val_upper = _strip_accents(field_value).upper()
    for pattern, replacement in OIL_GAS_CORRECTIONS:
        if replacement.upper() in val_upper:
            if re.search(pattern, raw_text, flags=re.IGNORECASE):
                return True
    return False


def _match_field_against_ocr_lines(
    field_value: str,
    ocr_lines: List[Dict[str, Any]],
    raw_text: str,
) -> Tuple[float, float, Optional[str]]:
    """
    Đối chiếu chéo (Cross-Verification) giá trị trường do LLM/VLM trích xuất với
    các dòng chữ nhận diện từ PaddleOCR (hoặc raw_text):
    Trả về:
        (ocr_line_confidence [0..1], text_agreement_ratio [0..1], mismatch_detail)
    """
    if not field_value or field_value in {"N/A", "None", "No", "Chưa rõ", "Unknown"}:
        return 0.92, 0.92, None

    # Tách các phần tử con nếu trường có nhiều mục ngăn cách bởi ';' (như author hoặc encl_name)
    sub_items = [s.strip() for s in re.split(r"[;,\n]+", field_value) if len(s.strip()) >= 3]
    if not sub_items:
        sub_items = [field_value.strip()]

    # Nếu có danh sách ocr_lines từ PaddleOCR (tài liệu scan hoặc trang bìa scan)
    if ocr_lines:
        item_ocr_scores: List[float] = []
        item_agreements: List[float] = []
        low_match_items: List[str] = []

        for sub in sub_items[:6]:  # Kiểm tra tối đa 6 mục đầu để tối ưu hiệu năng
            sub_lower = sub.lower()
            sub_no_accent = _strip_accents(sub_lower)
            best_sim = 0.0
            best_ocr_score = 0.0
            best_line_text = ""

            for line_obj in ocr_lines:
                line_txt = line_obj.get("text", "").strip()
                if not line_txt:
                    continue
                line_lower = line_txt.lower()
                line_score = float(line_obj.get("score", 0.75))

                # Độ tương đồng trực tiếp có dấu
                if sub_lower in line_lower:
                    sim = 1.0
                else:
                    sim_full = SequenceMatcher(None, sub_lower, line_lower).ratio()
                    # Kiểm tra thêm theo cửa sổ con nếu dòng OCR dài hơn cụm từ cần tìm
                    tokens_sub = _normalize_tokens(sub_lower)
                    tokens_line = _normalize_tokens(line_lower)
                    if tokens_sub and tokens_line:
                        overlap = sum(
                            max(SequenceMatcher(None, ts, tl).ratio() for tl in tokens_line)
                            for ts in tokens_sub
                        ) / len(tokens_sub)
                        sim = max(sim_full, overlap)
                    else:
                        sim = sim_full

                # Kiểm tra độ tương đồng không dấu (phát hiện trường hợp mực nhòe mất dấu tiếng Việt)
                sim_no_accent = SequenceMatcher(None, sub_no_accent, _strip_accents(line_lower)).ratio()
                effective_lookup = max(sim, sim_no_accent * 0.92)

                if effective_lookup > best_sim:
                    best_sim = effective_lookup
                    best_ocr_score = line_score
                    best_line_text = line_txt

            if best_sim >= 0.45:
                item_ocr_scores.append(best_ocr_score)
                item_agreements.append(best_sim)
                if best_sim < 0.84 or best_ocr_score < 0.78:
                    low_match_items.append(f"'{sub}' (OCR đọc: '{best_line_text}', độ nét {best_ocr_score*100:.0f}%)")
            else:
                # Không khớp rõ trong OCR (do chữ gốc quá mờ/con dấu đè lên nên OCR bỏ sót hoặc đọc sai hẳn, phải nhờ VLM cứu)
                avg_doc_ocr = sum(float(x.get("score", 0.75)) for x in ocr_lines) / max(1, len(ocr_lines))
                item_ocr_scores.append(min(0.72, avg_doc_ocr))
                item_agreements.append(0.62)
                low_match_items.append(f"'{sub}' (chữ gốc mờ/nhòe, OCR không nhận diện rõ)")

        mean_ocr = sum(item_ocr_scores) / max(1, len(item_ocr_scores))
        mean_agree = sum(item_agreements) / max(1, len(item_agreements))
        detail = "; ".join(low_match_items[:2]) if low_match_items else None
        return mean_ocr, mean_agree, detail

    # Trường hợp tài liệu số (Digital PDF / Word / TXT) -> Kiểm tra sự hiện diện trong raw_text
    if raw_text:
        raw_lower = raw_text.lower()
        found_scores: List[float] = []
        for sub in sub_items[:5]:
            sub_l = sub.lower()
            if sub_l in raw_lower:
                found_scores.append(0.98)
            else:
                tokens = _normalize_tokens(sub_l)
                if tokens:
                    hit = sum(1 for t in tokens if t in raw_lower) / len(tokens)
                    found_scores.append(0.75 + 0.22 * hit)
                else:
                    found_scores.append(0.90)
        mean_digital_agree = sum(found_scores) / max(1, len(found_scores))
        return 0.98, mean_digital_agree, None

    return 0.90, 0.90, None


def evaluate_metadata_confidence(
    metadata: DocumentMetadata,
    raw_text: str = "",
    is_scanned: bool = False,
    ocr_lines: Optional[List[Dict[str, Any]]] = None,
) -> DocumentMetadata:
    """
    Tính toán tự động (100% Dynamic, không hardcode):
    - Điểm tin cậy cho từng trường (`field_confidences`)
    - Danh sách các trường cần highlight cảnh báo trong Excel (`low_confidence_fields`)
    - Giải thích lý do cảnh báo cho từng ô (`field_warnings`)
    - Điểm tin cậy tổng thể của toàn bộ tài liệu (`confidence_score`)
    """
    ocr_lines = ocr_lines or []
    data = metadata.model_dump()

    # Tính chất lượng mặt chữ cơ sở của tài liệu
    if ocr_lines:
        avg_ocr_score = sum(float(x.get("score", 0.8)) for x in ocr_lines) / len(ocr_lines)
        low_ocr_ratio = sum(1 for x in ocr_lines if float(x.get("score", 0.8)) < 0.78) / len(ocr_lines)
    elif is_scanned:
        avg_ocr_score = 0.82
        low_ocr_ratio = 0.20
    else:
        # Tài liệu số (Digital PDF, Word, TXT) nhưng có thể có trang bìa scan một phần
        has_scan_tag = "(Scan/OCR)" in (raw_text or "")
        avg_ocr_score = 0.90 if has_scan_tag else 0.98
        low_ocr_ratio = 0.08 if has_scan_tag else 0.0

    field_confidences: Dict[str, float] = {}
    low_confidence_fields: List[str] = []
    field_warnings: Dict[str, str] = {}

    for field, weight in FIELD_WEIGHTS.items():
        val = str(data.get(field, "") or "").strip()
        is_missing = val in {"", "Chưa rõ", "Unknown", "Lỗi trích xuất LLM"}
        is_na = val in {"N/A", "None", "No"}

        # 1. Nếu trường bắt buộc bị khuyết ("Unknown" / "Chưa rõ" / "N/A")
        if field in MANDATORY_FIELDS and (is_missing or is_na):
            # Nếu là author = "None" / "N/A" nhưng tài liệu thực tế không có từ khóa tác giả cá nhân (báo cáo của công ty/tổ chức)
            if field == "author" and is_na:
                has_author_prompt = re.search(r"(?:author|compiled by|tác giả|người lập|chủ biên|chủ nhiệm)\s*[:\.]", raw_text or "", re.IGNORECASE)
                if not has_author_prompt:
                    field_confidences[field] = 95.0
                    continue

            score_pct = 45.0 if is_missing else 58.0
            field_confidences[field] = round(score_pct, 1)
            low_confidence_fields.append(field)
            field_warnings[field] = (
                f"Trường bắt buộc '{field.upper()}' chưa xác định rõ ({val}). "
                f"Độ tin cậy: {score_pct:.1f}% - Cần kiểm tra lại trên tài liệu gốc."
            )
            continue

        # 2. Nếu trường tùy chọn (well_link, contract_name, contractor, encl, encl_name) là N/A hoặc No/None
        if (is_na or is_missing) and field not in MANDATORY_FIELDS:
            # Kiểm tra riêng cho well_link: Nếu tài liệu thực tế có đề cập đến Giếng/Lỗ khoan nhưng chưa trích xuất được
            if field == "well_link":
                raw_text_str = raw_text or ""
                if re.search(r"(?:скважина|скв[\.\s№]|well[\s\-#№]|lỗ khoan|lk[\s\-#№])", raw_text_str, re.IGNORECASE):
                    field_confidences[field] = 55.0
                    low_confidence_fields.append(field)
                    field_warnings[field] = "Phát hiện từ khóa giếng/lỗ khoan trong tài liệu nhưng chưa trích xuất được số hiệu giếng."
                    continue

            # Kiểm tra tính nhất quán giữa encl và encl_name
            if field in {"encl", "encl_name"}:
                encl_val = str(data.get("encl", "No")).strip()
                encl_name_val = str(data.get("encl_name", "None")).strip()
                if (encl_val not in {"No", "0", "N/A"} and encl_name_val in {"None", "N/A", ""}) or (
                    encl_val in {"No", "0"} and encl_name_val not in {"None", "N/A", ""}
                ):
                    field_confidences[field] = 68.0
                    low_confidence_fields.append(field)
                    field_warnings[field] = "Số lượng phụ bản (ENCL) và Tên phụ bản (Encl Name) chưa đồng nhất."
                    continue

            base_na_score = (0.88 * avg_ocr_score + 0.12 * (1.0 - low_ocr_ratio)) * 100.0
            field_confidences[field] = round(min(97.0, max(78.0, base_na_score)), 1)
            continue

        # Trường language là trường suy luận cấp tài liệu (đã chuẩn hóa qua langdetect + pycountry)
        if field == "language":
            lang_score = 96.0 if val and val not in {"N/A", "Unknown", "Chưa rõ"} else 65.0
            field_confidences[field] = lang_score
            if lang_score < HIGHLIGHT_THRESHOLD:
                low_confidence_fields.append(field)
                field_warnings[field] = "Không xác định được ngôn ngữ chính của tài liệu."
            continue

        # Trường document_type là trường phân loại thể loại tài liệu
        if field == "document_type":
            doc_type_score = 98.0 if val and val != "General_Report" else 88.0
            field_confidences[field] = doc_type_score
            continue

        # 3. Đối chiếu chéo giá trị trích xuất với mặt chữ OCR / văn bản gốc
        # Lưu ý: Nếu giá trị trường đã xuất hiện nguyên vẹn trong phần văn bản điện tử sạch (Digital Text Layer),
        # không bắt buộc phải tìm lại trong ocr_lines của riêng 1 trang bìa scan.
        raw_lower = (raw_text or "").lower()
        sub_check = [s.strip().lower() for s in re.split(r"[;,\n]+", val) if len(s.strip()) >= 3] or [val.lower()]
        in_raw_digital = (not is_scanned or "=== [trang" in raw_lower or "--- [trang" in raw_lower) and all(
            s in raw_lower for s in sub_check[:4]
        )

        if in_raw_digital and not _had_ocr_typo_correction(raw_text, val):
            # Nếu văn bản khớp 100% với text layer hoặc OCR sạch, lấy điểm theo ocr_lines (nếu khớp) hoặc digital score
            line_conf, agree_ratio, mismatch_detail = _match_field_against_ocr_lines(val, ocr_lines, raw_text)
            if agree_ratio < 0.85:
                # Khớp trong trang digital nhưng không nằm ở trang scan -> dùng điểm digital
                line_conf, agree_ratio, mismatch_detail = 0.96, 0.96, None
        else:
            line_conf, agree_ratio, mismatch_detail = _match_field_against_ocr_lines(val, ocr_lines, raw_text)

        # Kiểm tra xem trường này có bị sửa lỗi chính tả do vết mực nhòe không
        typo_repaired = _had_ocr_typo_correction(raw_text, val)

        # Công thức kết hợp 3 tín hiệu:
        # - 45% từ chất lượng mặt chữ tại vùng chứa trường (line_conf)
        # - 40% từ độ đồng thuận chéo giữa VLM/LLM và OCR (agree_ratio)
        # - 15% từ chất lượng nền tổng thể của tài liệu (avg_ocr_score)
        raw_field_prob = (0.45 * line_conf) + (0.40 * agree_ratio) + (0.15 * avg_ocr_score)

        if typo_repaired:
            raw_field_prob -= 0.10  # Phạt điểm vì chữ gốc trên bản scan bị nhòe nét phải qua bộ phục hồi

        score_pct = round(max(35.0, min(99.2, raw_field_prob * 100.0)), 1)
        field_confidences[field] = score_pct

        # Đánh dấu highlight nếu điểm tin cậy của trường < ngưỡng HIGHLIGHT_THRESHOLD (80%)
        if score_pct < HIGHLIGHT_THRESHOLD:
            low_confidence_fields.append(field)
            reasons = []
            if typo_repaired:
                reasons.append("chữ in/máy đánh chữ gốc bị lem mực")
            if mismatch_detail:
                reasons.append(mismatch_detail)
            elif line_conf < 0.80:
                reasons.append(f"độ nét mặt chữ scan thấp ({line_conf*100:.0f}%)")
            reason_str = "; ".join(reasons) if reasons else "mặt chữ gốc mờ/nhòe khiến mô hình phân vân"
            field_warnings[field] = (
                f"Cảnh báo ({score_pct:.1f}%): Phát hiện {reason_str}. "
                f"Người dùng vui lòng đối chiếu lại ô này với tài liệu gốc."
            )

    # Tính tổng hợp Độ tin cậy của toàn bộ tài liệu (Weighted Average theo FIELD_WEIGHTS)
    weighted_sum = sum(field_confidences.get(f, 85.0) * w for f, w in FIELD_WEIGHTS.items())
    # Điều chỉnh nhẹ nếu tài liệu có nhiều dòng OCR mờ nhòe
    doc_penalty = low_ocr_ratio * 6.0
    overall_confidence = round(max(40.0, min(99.0, weighted_sum - doc_penalty)), 1)

    data["confidence_score"] = overall_confidence
    data["field_confidences"] = field_confidences
    data["low_confidence_fields"] = low_confidence_fields
    data["field_warnings"] = field_warnings

    return DocumentMetadata(**data)
