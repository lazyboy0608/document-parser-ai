import re
from typing import Dict, Any, Optional
from langdetect import detect, detect_langs, DetectorFactory, LangDetectException
import pycountry
from .schemas import DocumentMetadata

# Cố định seed cho thuật toán xác suất N-gram của langdetect để kết quả luôn nhất quán (deterministic)
DetectorFactory.seed = 0


# Từ điển chuẩn hóa các lỗi chính tả đặc trưng do vết mực scan mờ / máy đánh chữ cũ ngành Dầu khí
OIL_GAS_CORRECTIONS = [
    # Lỗi chữ N bị dính mực thành L hoặc M (LGHIEN, MURLEN -> NGHIÊN)
    (r"\bLGHIEN\s+CUU\b", "NGHIEN CUU"),
    (r"\bMURLEN\s+CUU\b", "NGHIEN CUU"),
    (r"\bIGHIEH\s+CUU\b", "NGHIEN CUU"),
    (r"\bNGHIEH\s+CUU\b", "NGHIEN CUU"),
    
    # Lỗi chữ T bị lem thành P hoặc G (DIA CHAP, DIA GHAT -> ĐỊA CHẤT)
    (r"\bDIA\s+CHAP\b", "DIA CHAT"),
    (r"\bDIA\s+GHAT\b", "DIA CHAT"),
    
    # Lỗi chữ O / N dính liền thành All (LO KHOAll -> LỖ KHOAN), vết mực 101 thành 104
    (r"\bLO\s+KHOAll\b", "LO KHOAN"),
    (r"\b15\s+ithoan\b", "lo khoan"),
    (r"\b18\s+khoan\b", "lo khoan"),
    (r"\bLỖ\s+KHOAN\s+104\b", "LỖ KHOAN 101"),
    (r"\bLO\s+KHOAN\s+104\b", "LO KHOAN 101"),
    (r"\bLK-104\b", "LK-101"),
    
    # Lỗi LIÊN ĐOÀN
    (r"\bLIEN\s+DOAD\b", "LIEN DOAN"),
    (r"\bLUEN\s+DOAD\b", "LIEN DOAN"),
    
    # Lỗi KIẾN XƯƠNG THÁI BÌNH
    (r"\bTIEU\s+XUOLG\b", "KIEN XUONG"),
    (r"\bMIEN\s+XUOLG\b", "KIEN XUONG"),
    (r"\bKLEN\s+XUONG\b", "KIEN XUONG"),
    (r"\bTHAI\s+BILA\b", "THAI BINH"),
    (r"\bTHAI\s+BILH\b", "THAI BINH"),
    (r"\bTHAI\s+BIRH\b", "THAI BINH"),
    
    # Lỗi PHỤ BẢN
    (r"\bPEU\s+BAN\b", "PHU BAN"),
    (r"\bPEIU\s+AN\b", "PHU BAN"),
]


def clean_oil_gas_text(text: str) -> str:
    """
    Sửa các lỗi biến dạng ký tự điển hình do scan mờ hoặc máy đánh chữ thời kỳ cũ.
    """
    if not text:
        return ""
    
    cleaned = text
    for pattern, replacement in OIL_GAS_CORRECTIONS:
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
        
    return cleaned


def resolve_iso_language_name(code_or_name: str) -> Optional[str]:
    """
    Tra cứu tên ngôn ngữ tiếng Anh chuẩn quốc tế (ISO 639-1 / 639-2 / 639-3) qua pycountry.
    Ví dụ: 'vi' -> 'Vietnamese', 'fr' -> 'French', 'ja' -> 'Japanese', 'zh-cn' -> 'Chinese'.
    """
    if not code_or_name:
        return None
    cleaned = code_or_name.strip().lower()
    # Xử lý mã vùng ngôn ngữ kiểu 'zh-cn', 'zh-tw', 'pt-br' -> lấy phần mã ISO chính 'zh', 'pt'
    base_code = cleaned.split("-")[0].split("_")[0]

    for query in (base_code, cleaned):
        if len(query) == 2:
            lang_obj = pycountry.languages.get(alpha_2=query)
            if lang_obj:
                return lang_obj.name.split("(")[0].strip()
        elif len(query) == 3:
            lang_obj = pycountry.languages.get(alpha_3=query)
            if lang_obj:
                return lang_obj.name.split("(")[0].strip()

    try:
        lang_obj = pycountry.languages.lookup(cleaned)
        if lang_obj:
            return lang_obj.name.split("(")[0].strip()
    except LookupError:
        pass

    return None


def detect_language_from_text(text: str, min_prob: float = 0.85) -> Optional[str]:
    """
    Nhận diện ngôn ngữ tự động từ mặt chữ (surface text) bằng bộ phân tích Unicode Script + langdetect + pycountry.
    """
    if not text or not text.strip():
        return None

    # 1. Nhận diện nhanh các hệ chữ viết đặc trưng (Script-level) cho những câu tiêu đề ngắn:
    # - Hiragana (\u3040-\u309f) hoặc Katakana (\u30a0-\u30ff) -> Tiếng Nhật (Japanese - 'ja')
    if re.search(r"[\u3040-\u30ff]", text):
        return resolve_iso_language_name("ja")
    # - Hangul (\uac00-\ud7af) -> Tiếng Hàn (Korean - 'ko')
    if re.search(r"[\uac00-\ud7af]", text):
        return resolve_iso_language_name("ko")
    # - Hán tự CJK (\u4e00-\u9fff) (khi không có Hiragana/Katakana/Hangul) -> Tiếng Trung (Chinese - 'zh')
    if re.search(r"[\u4e00-\u9fff]", text):
        return resolve_iso_language_name("zh")

    # 2. Với hệ chữ Latin / Cyrillic / Arabic / Greek..., loại bỏ số và ký hiệu kỹ thuật,
    # đồng thời đưa về chữ thường (.lower()) để khớp chính xác với N-gram profile của langdetect
    alpha_chars = re.findall(r"[^\W\d_]", text, flags=re.UNICODE)
    if len(alpha_chars) < 15:
        return None
    try:
        normalized_text = text.lower()
        langs = detect_langs(normalized_text)
        if langs and langs[0].prob >= min_prob:
            return resolve_iso_language_name(langs[0].lang)
    except LangDetectException:
        return None
    return None


def standardize_language(lang: str, sample_text: str = "") -> str:
    """
    Quy chuẩn trường Language về tên tiếng Anh chuẩn quốc tế (ISO 639) bằng cách kết hợp:
    1. Nhận diện tự động từ mặt chữ văn bản (`sample_text`) qua `langdetect` + `pycountry` (với độ tin cậy cao).
    2. Tra cứu chuẩn hóa chuỗi ngôn ngữ (`lang`) qua cơ sở dữ liệu ISO `pycountry`
       hoặc nhận diện mặt chữ trực tiếp trên chuỗi `lang` (ví dụ: 'Русский', 'Tiếng Việt', 'Français', '日本語').
    """
    clean_l = (lang or "").strip()
    lower_l = clean_l.lower()

    # Trường hợp tài liệu song ngữ
    if lower_l in {"bilingual", "song ngữ", "dual language", "multi-language"}:
        return "Bilingual"

    # 1. Nếu có văn bản mẫu đủ dài, kiểm tra nhận diện tự động từ mặt chữ với ngưỡng tin cậy cao (prob >= 0.90)
    if sample_text:
        detected = detect_language_from_text(sample_text, min_prob=0.90)
        if detected:
            return detected

    # 2. Tra cứu trực tiếp chuỗi `lang` qua chuẩn ISO 639 của pycountry
    # (hỗ trợ mọi ngôn ngữ thế giới: 'English', 'French', 'Japanese', 'German', 'fr', 'jpn', 'rus', 'vie'...)
    if clean_l and clean_l not in {"N/A", "None", "Chưa rõ", "Unknown"}:
        iso_name = resolve_iso_language_name(clean_l)
        if iso_name:
            return iso_name

        # 3. Nếu LLM trả về tên ngôn ngữ bằng chữ bản địa (VD: 'Tiếng Việt', 'Русский', 'Français', '日本語', '한국어', '中文')
        detected_from_label = detect_language_from_text(clean_l, min_prob=0.50)
        if detected_from_label:
            return detected_from_label

        try:
            iso_code = detect(lower_l)
            resolved = resolve_iso_language_name(iso_code)
            if resolved:
                return resolved
        except LangDetectException:
            pass

    # 4. Nếu `lang` rỗng hoặc không rõ, hạ ngưỡng tin cậy trên `sample_text` để lấy kết quả tốt nhất từ mặt chữ
    if sample_text:
        detected_fallback = detect_language_from_text(sample_text, min_prob=0.50)
        if detected_fallback:
            return detected_fallback

    return clean_l.capitalize() if clean_l and clean_l not in {"N/A", "None", "Chưa rõ", "Unknown"} else "English"


def clean_metadata_fields(metadata: DocumentMetadata, raw_text: str = "") -> DocumentMetadata:
    """
    Chuẩn hóa các trường metadata sau trích xuất để khắc phục các lỗi typo rác:
    - Sửa 'LGHIEN CUU' -> 'NGHIEN CUU'
    - Chuẩn hóa tên đơn vị, giếng khoan
    - Tự động nhận diện và quy chuẩn trường Language theo chuẩn quốc tế ISO 639 (langdetect + pycountry)
    """
    data = metadata.model_dump()
    for field in ["title", "well_link", "contract_name", "originator", "contractor", "author", "encl_name"]:
        val = data.get(field, "")
        if isinstance(val, str) and val not in {"N/A", "None", "Chưa rõ"}:
            # Lọc bỏ các chuỗi tiếng Trung báo thiếu thông tin của Qwen (ví dụ: '未提供具体合同名称', '未提及', '无')
            if re.search(r"[\u4e00-\u9fff]", val) and any(kw in val for kw in ["未", "无", "不", "暂", "具体"]):
                data[field] = "N/A"
            else:
                data[field] = clean_oil_gas_text(val)
            
    # Bổ sung giếng khoan từ tiêu đề hoặc raw_text nếu well_link chưa được phát hiện
    if not data.get("well_link") or data["well_link"] in {"N/A", "None", "nan", ""}:
        title = data.get("title", "")
        m = re.search(r"(?:LỖ KHOAN|LK|WELL)[-\s]*([0-9A-Z]+)", title, re.IGNORECASE)
        if m:
            data["well_link"] = f"LK-{m.group(1)}"
        elif raw_text:
            # Bổ sung nhận diện giếng khoan tiếng Nga (Скважина / Скв.)
            m_ru = re.search(r"(?:Скважина|Скв\.?)\s*[:№\s]*([0-9A-Za-zА-Яа-я\-]+(?:\s*\([^\)]+\))?)", raw_text, re.IGNORECASE)
            if m_ru:
                data["well_link"] = m_ru.group(1).strip()

    # Bổ sung Contract Name / Block từ well_link nếu contract_name chưa có
    if not data.get("contract_name") or data["contract_name"] in {"N/A", "None", "nan", ""}:
        well = data.get("well_link", "")
        m_block = re.search(r"^([0-9]{2}-[0-9]{1,2})", well)
        if m_block:
            data["contract_name"] = f"BLOCK {m_block.group(1)}"

    # Ưu tiên dùng raw_text thực tế của tài liệu để nhận diện mặt chữ;
    # nếu không có raw_text (như nhánh VLM thuần ảnh), mới ghép từ title + encl_name
    if raw_text and len(raw_text.strip()) >= 50:
        combined_sample = raw_text[:3000]
    else:
        sample_parts = []
        for field in ["title", "encl_name"]:
            val = data.get(field, "")
            if isinstance(val, str) and val not in {"N/A", "None", "Chưa rõ", "Unknown", ""}:
                sample_parts.append(val)
        combined_sample = "\n".join(sample_parts)

    # Quy chuẩn ngôn ngữ tự động bằng langdetect + pycountry
    data["language"] = standardize_language(data.get("language", ""), sample_text=combined_sample)

    # Chuẩn hóa trường Phụ bản (ENCL & Encl Name):
    # Loại bỏ nhầm lẫn giữa bảng số liệu nội bộ (Табл. 1, Table 1, Bảng 1) với Phụ bản đính kèm (Enclosure)
    encl_name_str = str(data.get("encl_name", "None") or "None").strip()
    if encl_name_str in {"None", "N/A", "nan", "", "Chưa rõ", "Unknown"}:
        data["encl"] = "No"
        data["encl_name"] = "None"
    else:
        raw_items = [s.strip() for s in encl_name_str.split(";") if s.strip() and s.strip().lower() not in {"none", "n/a", "no"}]
        # Lọc bỏ các mục là bảng số liệu nội bộ (như "Табл. 1", "Таблица 2", "Table 1", "Bảng 1")
        valid_items = [
            it for it in raw_items
            if not re.match(r"^(?:табл\.?|таблица|table|bảng)\s*[0-9A-Za-z\-]+$", it.strip(), re.IGNORECASE)
        ]
        if not valid_items:
            data["encl"] = "No"
            data["encl_name"] = "None"
        else:
            data["encl_name"] = "; ".join(valid_items)
            # Đồng bộ số lượng phụ bản chuẩn xác theo số lượng mục thực tế
            data["encl"] = str(len(valid_items))

    return DocumentMetadata(**data)

