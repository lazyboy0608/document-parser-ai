import re
from typing import Dict, Any, Optional, Tuple, List
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
    
    # Lỗi chữ O / N dính liền thành All (LO KHOAll -> LỖ KHOAN)
    (r"\bLO\s+KHOAll\b", "LO KHOAN"),
    (r"\b15\s+ithoan\b", "lo khoan"),
    (r"\b18\s+khoan\b", "lo khoan"),
    
    # Lỗi PHỤ BẢN
    (r"\bPEU\s+BAN\b", "PHU BAN"),
    (r"\bPEIU\s+AN\b", "PHU BAN"),
]


# Biểu thức chính quy phát hiện câu/cụm từ báo thiếu thông tin đa ngôn ngữ do LLM tạo ra
MISSING_PHRASE_REGEX = re.compile(
    r"^(?:"
    # Tiếng Việt: Không có thông tin, không đề cập, chưa rõ, không tìm thấy...
    r"(?:không\s+(?:có\s+|được\s+|thấy\s+)?(?:thông\s+tin|dữ\s+liệu|đề\s+cập|rõ|tìm\s+thấy|nhắc\s+đến|ghi\s+nhận))|"
    r"(?:chưa\s+(?:có\s+)?(?:thông\s+tin|rõ|cung\s+cấp|xác\s+định|đề\s+cập))|"
    r"(?:không\s+(?:thấy|rõ|có))|"
    r"(?:chưa\s+rõ)|"
    r"(?:văn\s+bản\s+không\s+(?:có|đề\s+cập|nhắc\s+đến))|"
    r"(?:đoạn\s+văn\s+không\s+(?:có|đề\s+cập))|"
    r"(?:tài\s+liệu\s+không\s+(?:có|đề\s+cập|nhắc\s+đến))|"
    # Tiếng Anh
    r"(?:not\s+(?:mentioned|provided|available|found|specified|applicable|stated))|"
    r"(?:no\s+(?:information|data|details|mention))|"
    r"(?:unknown|unspecified|none|n/?a|nan)"
    r")\b",
    re.IGNORECASE
)


def is_missing_or_unknown_phrase(val: Any) -> bool:
    """Kiểm tra chuỗi có phải câu báo thiếu thông tin của LLM (tiếng Việt, tiếng Anh, tiếng Trung)."""
    if val is None:
        return True
    s = str(val).strip()
    if not s or s.lower() in {"", "n/a", "na", "none", "no", "unknown", "chưa rõ", "null", "nan"}:
        return True
    # Kiểm tra chuỗi tiếng Trung báo thiếu
    if re.search(r"[\u4e00-\u9fff]", s) and any(kw in s for kw in ["未", "无", "不", "暂", "具体"]):
        return True
    # Kiểm tra tiếng Việt / tiếng Anh bằng Regex
    if MISSING_PHRASE_REGEX.search(s):
        return True
    return False


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


def extract_enclosures_from_raw_text(raw_text: str) -> Tuple[str, str]:
    """
    Rút trích danh mục phụ bản (Enclosures / Danh mục bản vẽ / Приложения) trực tiếp từ văn bản thô.
    """
    if not raw_text:
        return "No", "None"

    m = re.search(
        r"(?:LIST\s+OF\s+ENCLOSURES|ENCLOSURES|DANH\s+MỤC\s+PHỤ\s+BẢN|PHỤ\s+BẢN\s+KÈM\s+THEO|ПРИЛОЖЕНИЯ)(.*?)(?:\n\s*(?:LIST\s+OF\s+TABLES|TABLES|FIGURES|LIST\s+OF\s+FIGURES|CONTENTS|MỤC\s+LỤC|1\.\s+[A-Z])|\Z)",
        raw_text,
        re.IGNORECASE | re.DOTALL
    )
    if not m:
        return "No", "None"

    section_text = m.group(1)
    clean_text = " ".join(section_text.split())
    items = []

    # Định dạng Enclosure 1 ... Enclosure 2 ...
    matches = re.findall(
        r"(?:Enclosure|Phụ\s+bản|Приложение)\s*(\d+)[\s:\.-]+(.*?)(?=(?:Enclosure|Phụ\s+bản|Приложение)\s*\d+|\Z)",
        clean_text,
        re.IGNORECASE
    )
    if matches:
        for num, name in matches:
            name = name.strip(" -:;,")
            # Loại bỏ các từ khóa bảng biểu / rác lọt vào
            if name and not any(term in name.upper() for term in ["LIST OF TABLES", "FIGURES", "TABLES"]):
                items.append(name)

    # Nếu không có từ khóa Enclosure đứng trước từng dòng, kiểm tra danh sách đánh số 1. ... 2. ...
    if not items:
        lines = [l.strip() for l in section_text.split("\n") if l.strip()]
        for line in lines:
            m_num = re.match(r"^\d+[\.\)]\s*(.+)$", line)
            if m_num:
                items.append(m_num.group(1).strip(" -:;,"))

    if items:
        seen = set()
        deduped = []
        for it in items:
            # Loại bỏ các ký tự rác OCR như 'I I I I' ở cuối dòng
            it_clean = re.sub(r"(\s+[I|jJ\-])+$", "", it).strip()
            it_clean = re.sub(r"\s+", " ", it_clean).strip()
            if it_clean.lower() not in seen and len(it_clean) > 2:
                seen.add(it_clean.lower())
                deduped.append(it_clean)
        if deduped:
            return str(len(deduped)), "; ".join(deduped)

    return "No", "None"


def clean_metadata_fields(metadata: DocumentMetadata, raw_text: str = "") -> DocumentMetadata:
    """
    Chuẩn hóa các trường metadata sau trích xuất để khắc phục các lỗi typo rác:
    - Sửa 'LGHIEN CUU' -> 'NGHIEN CUU'
    - Chuẩn hóa tên đơn vị, giếng khoan
    - Tự động nhận diện và quy chuẩn trường Language theo chuẩn quốc tế ISO 639 (langdetect + pycountry)
    """
    data = metadata.model_dump()
    for field in ["title", "well_link", "contract_name", "originator", "contractor", "author", "public_date", "encl_name"]:
        val = data.get(field, "")
        if isinstance(val, str):
            if is_missing_or_unknown_phrase(val):
                if field in {"title", "public_date"}:
                    data[field] = "Chưa rõ"
                elif field == "author":
                    data[field] = "None"
                else:
                    data[field] = "N/A"
            else:
                data[field] = clean_oil_gas_text(val)

    # 1. Bổ sung giếng khoan từ tiêu đề hoặc raw_text nếu well_link chưa được phát hiện
    if not data.get("well_link") or data["well_link"] in {"N/A", "None", "nan", ""}:
        title = data.get("title", "")
        m = re.search(r"(?:LỖ KHOAN|LK|WELL)[-\s]*([0-9A-Z\-]+)", title, re.IGNORECASE)
        if m:
            data["well_link"] = m.group(1) if "-" in m.group(1) else f"LK-{m.group(1)}"
        elif raw_text:
            # Bổ sung nhận diện giếng khoan tiếng Nga (Скважина / Скв.)
            m_ru = re.search(r"(?:Скважина|Скв\.?)\s*[:№\s]*([0-9A-Za-zА-Яа-я\-]+(?:\s*\([^\)]+\))?)", raw_text, re.IGNORECASE)
            if m_ru:
                data["well_link"] = m_ru.group(1).strip()

    # 1.5. Khử ảo giác giếng khoan từ các cơ quan hành chính/học viện/khoa phi dầu khí
    well_val_str = str(data.get("well_link", "") or "").strip()
    if well_val_str and well_val_str not in {"N/A", "None", "Chưa rõ"}:
        has_well_keyword = bool(re.search(r"\b(?:LK|WELL|BLOCK|BLOCKS|LÔ|LỖ\s+KHOAN|SKV|Скважина)\b", well_val_str, re.IGNORECASE))
        if not has_well_keyword:
            if re.search(r"\b(?:H\s*VIỆN|HỌC\s*VIỆN|KHOA\b|TRƯỜNG\b|ĐẠI\s*HỌC|PHÒNG\b|BAN\b|SINH\s*VIÊN)\b", well_val_str, re.IGNORECASE):
                data["well_link"] = "N/A"

    # 2. Xử lý trường hợp Well link vs Contract Name (VD: BLOCKS 15-1 & 15-2 bị gán nhầm vào Contract Name)
    contract_val = str(data.get("contract_name", "") or "").strip()
    well_val = str(data.get("well_link", "") or "").strip()
    title_val = str(data.get("title", "") or "").strip()

    # Nếu contract_name thực chất là tên Block (ví dụ: BLOCKS 15-1 & 15-2, BLOCK 15-1, LÔ 05-3)
    m_block_contract = re.search(r"\b((?:BLOCKS?|LÔ)\s+[0-9\-]+(?:\s*(?:&|AND|VÀ|,)\s*[0-9\-]+)*)", contract_val, re.IGNORECASE)
    if m_block_contract:
        if well_val in {"N/A", "None", "nan", ""}:
            # Chuyển Block sang well_link
            data["well_link"] = re.sub(r"\s+(?:AND|VÀ)\s+", " & ", m_block_contract.group(1).strip(), flags=re.IGNORECASE)
            # Nếu originator có tên công ty (ví dụ: SANTOS LIMITED), contract_name sẽ là tên công ty/chủ quản
            orig_val = str(data.get("originator", "") or "").strip()
            if orig_val and orig_val not in {"N/A", "None", "Chưa rõ"}:
                data["contract_name"] = orig_val
            else:
                data["contract_name"] = "N/A"

    # Nếu well_link vẫn chưa có nhưng tiêu đề có số hiệu BLOCKS (ví dụ: BLOCKS 15-1 & 15-2 hoặc BLOCKS 15-1 AND 15-2)
    m_block_in_title = re.search(r"\b((?:BLOCKS?|LÔ)\s+[0-9\-]+(?:\s*(?:&|AND|VÀ|,)\s*[0-9\-]+)*)", title_val, re.IGNORECASE)
    if m_block_in_title:
        full_block_title = re.sub(r"\s+(?:AND|VÀ)\s+", " & ", m_block_in_title.group(1).strip(), flags=re.IGNORECASE)
        curr_well = str(data.get("well_link", "") or "").strip()
        if curr_well in {"N/A", "None", "nan", ""} or (curr_well.startswith("BLOCK") and len(curr_well) < len(full_block_title)):
            data["well_link"] = full_block_title

    # Nhận diện đơn vị điều hành / công ty phát hành từ cấu trúc 'by <COMPANY>' hoặc pháp nhân dầu khí tổng quát
    # Áp dụng bình đẳng cho mọi công ty quốc tế & Việt Nam (SANTOS, AEDC, CUU LONG JOC, VIETSOVPETRO, JVPC, KNOC...)
    if data.get("contract_name") in {"N/A", "None", "nan", ""} or data.get("originator") in {"N/A", "None", "nan", "", "Chưa rõ"}:
        search_zone = (title_val + "\n" + (raw_text[:1500] if raw_text else "")).strip()
        # Tìm mẫu: by <TÊN CÔNG TY> hoặc operator: <TÊN CÔNG TY>
        m_by = re.search(r"\b(?:by|operator|client|for)\s+([A-Z0-9\s.,&-]+(?:\bLTD\b|\bLIMITED\b|\bCORP\b|\bCORPORATION\b|\bINC\b|\bJOC\b|\bCOMPANY\b|\bVIETSOVPETRO\b|\bPETROVIETNAM\b|\bZARUBEZHNEFT\b|\bPVEP\b|\bPVD\b|\bENTERPRISE\b))", search_zone, re.IGNORECASE)
        if m_by:
            found_company = " ".join(m_by.group(1).split()).strip(" ,.-")
            if data.get("contract_name") in {"N/A", "None", "nan", ""}:
                data["contract_name"] = found_company
            if data.get("originator") in {"N/A", "None", "nan", "", "Chưa rõ"}:
                data["originator"] = found_company
        else:
            # Tìm bất kỳ pháp nhân dầu khí chuẩn nào ở phần đầu tài liệu
            m_corp = re.search(r"\b([A-Z0-9\s.,&-]{3,40}(?:\bLTD\b|\bLIMITED\b|\bCORP\b|\bCORPORATION\b|\bINC\b|\bJOC\b|\bCOMPANY\b|\bVIETSOVPETRO\b|\bPETROVIETNAM\b|\bZARUBEZHNEFT\b))\b", search_zone)
            if m_corp:
                found_company = " ".join(m_corp.group(1).split()).strip(" ,.-")
                if data.get("contract_name") in {"N/A", "None", "nan", ""}:
                    data["contract_name"] = found_company
                if data.get("originator") in {"N/A", "None", "nan", "", "Chưa rõ"}:
                    data["originator"] = found_company

    # 3. Khử Hallucination Originator: Đơn vị ban hành phải có căn cứ trong văn bản
    orig_val = str(data.get("originator", "") or "").strip()
    if orig_val and orig_val not in {"N/A", "None", "Chưa rõ"}:
        orig_sig = re.sub(r"[^\w\s]", "", orig_val).strip()
        # Nếu cụm từ đơn vị ban hành hoàn toàn không xuất hiện trong raw_text
        if raw_text and len(orig_sig) >= 6 and orig_sig.lower() not in raw_text.lower():
            if data.get("contractor") and data["contractor"] not in {"N/A", "None"}:
                data["originator"] = data["contractor"]
            else:
                data["originator"] = "N/A"

    # 4. Khử Hallucination Tác giả (Author):
    author_val = str(data.get("author", "") or "").strip()
    if is_missing_or_unknown_phrase(author_val):
        data["author"] = "None"
    else:
        # Nếu ngôn ngữ tài liệu là tiếng Anh hoặc tiếng Nga, tác giả phải xuất hiện thực tế trong văn bản gốc
        lang_guess = str(data.get("language", "")).lower()
        author_parts = [p.strip() for p in author_val.split(";") if p.strip()]
        if lang_guess in {"english", "russian", "en", "ru"} and raw_text:
            valid_parts = [p for p in author_parts if p.lower() in raw_text.lower()]
            data["author"] = "; ".join(valid_parts) if valid_parts else "None"
        elif raw_text:
            valid_parts = [p for p in author_parts if p.lower() in raw_text.lower() or len(p) >= 3]
            data["author"] = "; ".join(valid_parts) if valid_parts else "None"

    # Bổ sung Contract Name / Block từ well_link nếu contract_name vẫn chưa có
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

    # 5. Chuẩn hóa trường Phụ bản (ENCL & Encl Name):
    encl_name_str = str(data.get("encl_name", "None") or "None").strip()
    encl_val_str = str(data.get("encl", "No") or "No").strip()

    # Nếu VLM hoặc LLM chưa nhận diện được phụ bản (hoặc là No/None), tự động rà quét từ raw_text
    if (encl_val_str in {"No", "0", "N/A", "None", ""} or encl_name_str in {"None", "N/A", "nan", "", "Chưa rõ", "Unknown"}) and raw_text:
        text_encl_count, text_encl_names = extract_enclosures_from_raw_text(raw_text)
        if text_encl_count != "No":
            data["encl"] = text_encl_count
            data["encl_name"] = text_encl_names
            encl_name_str = text_encl_names

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
            data["encl"] = str(len(valid_items))

    # 6. Chuẩn hóa thể loại tài liệu (Document Type Classifier):
    data["document_type"] = classify_document_type(
        title=data.get("title", ""),
        raw_text=raw_text or "",
        current_type=data.get("document_type", ""),
    )

    return DocumentMetadata(**data)


VALID_DOCUMENT_TYPES = {
    "Well_Proposal",
    "Final_Well_Report",
    "Well_Logging_Report",
    "Geological_Report",
    "Block_Review",
    "Seismic_Report",
    "General_Report",
}


def classify_document_type(title: str = "", raw_text: str = "", current_type: str = "") -> str:
    """
    Phân loại thể loại tài liệu (Document Type Classifier) chuẩn ngành Dầu khí:
    Ưu tiên nhận diện theo mẫu từ khóa đặc trưng đa ngôn ngữ (Anh - Việt - Nga) từ Tiêu đề và Văn bản đầu.
    Nếu không khớp quy tắc, sử dụng kết quả từ LLM nếu hợp lệ, ngược lại fallback về 'General_Report'.
    """
    text_to_check = f"{title}\n{raw_text[:1500]}".strip().lower()

    # 1. Well Proposal (Đề cương / Thiết kế giếng khoan)
    if re.search(r"\b(well\s+proposal|drilling\s+proposal|drilling\s+program|well\s+program|đề\s+cương\s+khoan|đề\s+cương\s+giếng|thiết\s+kế\s+giếng|kế\s+hoạch\s+khoan|проект\s+скважин|программ[аы]\s+бурен)", text_to_check):
        return "Well_Proposal"

    # 2. Final Well Report (Báo cáo hoàn công / kết thúc giếng khoan)
    if re.search(r"\b(final\s+well\s+report|well\s+completion\s+report|completion\s+report|post-?drilling\s+report|end\s+of\s+well\s+report|báo\s+cáo\s+kết\s+thúc\s+giếng|báo\s+cáo\s+hoàn\s+thành\s+giếng|báo\s+cáo\s+hoàn\s+công\s+giếng|kết\s+thúc\s+thi\s+công\s+giếng|отчет\s+по\s+скважин|отчет\s+о\s+завершен|отчет\s+по\s+бурению)", text_to_check):
        return "Final_Well_Report"

    # 3. Well Logging Report (Báo cáo Địa vật lý / Carota / Kiểm tra xi măng & Ống chống)
    if re.search(r"\b(гис|цементирован|колонн|каротаж|well\s+logging|wireline\s+log|cement\s+bond|cbl|vdl|casing\s+inspection|mud\s+logging|carota|địa\s+vật\s+lý\s+giếng|kiểm\s+tra\s+xi\s+măng|trám\s+xi\s+măng|ống\s+chống)", text_to_check):
        return "Well_Logging_Report"

    # 4. Geological Report (Báo cáo Địa chất / Sinh địa tầng / Tầng chứa)
    if re.search(r"\b(geological\s+report|geology\s+report|geological\s+evaluation|stratigraph|biostratigraph|petrophysic|nghiên\s+cứu\s+địa\s+chất|báo\s+cáo\s+địa\s+chất|địa\s+chất\s+lỗ\s+khoan|địa\s+tầng|trầm\s+tích|геологическ|стратиграфи)", text_to_check):
        return "Geological_Report"

    # 5. Block Review (Báo cáo Đánh giá Lô / Bể trầm tích)
    if re.search(r"\b(block\s+review|concession\s+review|basin\s+review|regional\s+study|basin\s+study|block\s+evaluation|đánh\s+giá\s+lô|tổng\s+quan\s+lô|đánh\s+giá\s+bể|обзор\s+блока|оценка\s+бассейн)", text_to_check):
        return "Block_Review"

    # 6. Seismic Report (Báo cáo Địa chấn / Minh giải địa chấn)
    if re.search(r"\b(seismic\s+report|seismic\s+interpretation|seismic\s+acquisition|seismic\s+survey|địa\s+chấn|minh\s+giải\s+địa\s+chấn|thu\s+nổ\s+địa\s+chấn|сейсмическ|сейсморазвед)", text_to_check):
        return "Seismic_Report"

    # 7. Nếu LLM đã phân loại đúng vào 1 trong các nhóm hợp lệ
    clean_curr = re.sub(r"[^\w]", "_", str(current_type or "").strip())
    for valid_type in VALID_DOCUMENT_TYPES:
        if clean_curr.lower() == valid_type.lower():
            return valid_type

    return "General_Report"


