import os
import re
from pathlib import Path
from typing import Optional


def _extract_text_via_olefile(file_path: Path) -> str:
    """
    Trích xuất văn bản trực tiếp từ tệp nhị phân .doc (Word 97-2003) qua olefile.
    100% thuần Python, KHÔNG gọi tiến trình Microsoft Word, không bao giờ bị treo
    bởi các hộp thoại 'Show Repairs', lỗi đối tượng vẽ hỏng, hay lỗi RPC.
    Đồng thời lọc sạch các byte cấu trúc nhị phân và mã font rác.
    """
    import olefile

    if not olefile.isOleFile(str(file_path)):
        return ""

    with olefile.OleFileIO(str(file_path)) as ole:
        raw_bytes = b""
        if ole.exists("WordDocument"):
            raw_bytes = ole.openstream("WordDocument").read()
        elif ole.exists("1Table"):
            raw_bytes = ole.openstream("1Table").read()

        if not raw_bytes:
            return ""

        # Tệp Word .doc lưu văn bản dưới dạng UTF-16LE
        text_u16 = raw_bytes.decode("utf-16le", errors="ignore")
        # Thay thế ký tự điều khiển binary
        cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]+", " ", text_u16)

        # Lọc các dòng văn bản tự nhiên (chứa chữ cái Latin, Cyrillic tiếng Nga, hoặc tiếng Việt)
        # Loại bỏ các chuỗi rác nhị phân CJK do Word lưu metadata font nội bộ
        keywords = [
            "скважина", "скв", "well", "месторождение", "заключение", "отчет",
            "giếng", "lỗ khoan", "lk", "ngày", "đơn vị", "nhà thầu", "báo cáo",
            "kết quả", "chủ đầu tư", "công ty", "tổng công ty", "xí nghiệp", "liên doanh"
        ]
        valid_lines = []
        for line in cleaned.split("\r"):
            line_str = line.strip()
            if len(line_str) < 3:
                continue
            # Đếm số lượng ký tự chữ cái chuẩn (Latin, Cyrillic, tiếng Việt \u1e00-\u1eff)
            letters = re.findall(r"[\u0400-\u04ff\u0041-\u007a\u00c0-\u024f\u1e00-\u1eff]", line_str, flags=re.IGNORECASE)
            if len(letters) >= 3:
                # Loại bỏ các ký tự lạ, symbol cổ, CJK font rác (giữ lại tiếng Việt \u1e00-\u1eff)
                line_clean = re.sub(r"[^\w\s.,;:/\-()\[\]%\"\'«»№]+", " ", line_str)
                line_clean = re.sub(r"[\u2e80-\u9fff\uac00-\ud7af\u1200-\u137f\u10a0-\u10ff\u0f00-\u0fff\u1900-\u19ff\u1d00-\u1dff]+", " ", line_clean)
                line_clean = " ".join(line_clean.split())
                found_letters = len(re.findall(r"[\u0400-\u04ff\u0041-\u007a\u00c0-\u024f\u1e00-\u1eff]", line_clean))
                ratio = found_letters / max(1, len(line_clean))
                has_keyword = any(k in line_clean.lower() for k in keywords)
                # Chấp nhận dòng nếu tỷ lệ chữ cái tự nhiên >= 0.35 (cho phép dòng chứa số liệu/kích thước kỹ thuật)
                # hoặc dòng chứa các từ khóa dầu khí/đầu mục quan trọng
                if found_letters >= 3 and (ratio >= 0.35 or has_keyword):
                    valid_lines.append(line_clean)

        return "\n".join(valid_lines).strip()



def extract_text_from_doc(file_path: Path) -> str:
    """
    Trích xuất nội dung văn bản từ tệp Word legacy .doc (Word 97-2003):
    1. Ưu tiên giải pháp Pure-Python (olefile): Tốc độ mili-giây, chống hoàn toàn popup 'Show Repairs' của MS Word.
    2. Fallback sang MS Word COM Automation ngầm nếu tệp OLE đặc thù và Windows Word khả dụng.
    """
    abs_path = str(file_path.resolve())
    if not os.path.exists(abs_path):
        raise FileNotFoundError(f"Không tìm thấy tệp: {abs_path}")

    # 1. Thử trích xuất thuần Python bằng olefile trước
    try:
        ole_text = _extract_text_via_olefile(file_path)
        if len(ole_text.strip()) >= 30:
            return ole_text
    except Exception:
        pass

    # 2. Fallback: Sử dụng MS Word COM Automation với các cờ chặn tuyệt đối mọi cảnh báo/hộp thoại
    word = None
    doc = None
    try:
        import win32com.client
        import pythoncom
        pythoncom.CoInitialize()

        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0  # wdAlertsNone: Chặn 100% mọi hộp thoại thông báo

        # Mở ở chế độ ReadOnly, không hỏi chuyển đổi, không hiện hộp thoại sửa chữa
        doc = word.Documents.Open(
            FileName=abs_path,
            ConfirmConversions=False,
            ReadOnly=True,
            AddToRecentFiles=False,
            Visible=False,
            OpenAndRepair=True,
            NoEncodingDialog=True
        )
        text = doc.Content.Text
        return text.strip()
    except Exception as e:
        # Nếu Word COM cũng lỗi, thử quét nhị phân thô lấy chuỗi ký tự UTF-8/Latin/Cyrillic
        try:
            with open(abs_path, "rb") as f:
                raw = f.read()
            u16 = raw.decode("utf-16le", errors="ignore")
            cleaned = re.sub(r"[^\w\s.,;:/\-()\[\]]", " ", u16)
            words = [w for w in cleaned.split() if len(w) >= 2]
            if len(words) >= 15:
                return " ".join(words)
        except Exception:
            pass
        raise RuntimeError(f"Lỗi khi đọc tệp Word .doc '{file_path.name}': {e}")
    finally:
        if doc is not None:
            try:
                doc.Close(False)
            except Exception:
                pass
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass

