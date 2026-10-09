import re
import shutil
from pathlib import Path
from typing import Optional

from src.utils.logger import logger


def sanitize_folder_name(name: str, max_length: int = 50) -> str:
    """
    Chuẩn hóa chuỗi để làm tên thư mục hợp lệ trên Windows (loại bỏ ký tự cấm: \\ / : * ? \" < > |).
    Tự động chuẩn hóa các giá trị rỗng, N/A hoặc chuỗi tiếng Trung về 'Unknown'.
    """
    if not name:
        return "Unknown"
    name_str = name.strip()
    if name_str.lower() in {"", "chưa rõ", "none", "null", "n/a", "na", "unknown", "chua_ro", "chua_phan_loai"}:
        return "Unknown"
    # Lọc bỏ các chuỗi tiếng Trung biểu thị 'chưa cung cấp/không rõ'
    if re.search(r"[\u4e00-\u9fff]", name_str) and any(kw in name_str for kw in ["未", "无", "不", "暂"]):
        return "Unknown"
    # Thay thế các ký tự cấm bằng dấu gạch dưới
    sanitized = re.sub(r'[\\/*?:"<>|]', '_', name_str)
    # Rút gọn khoảng trắng
    sanitized = re.sub(r'\s+', '_', sanitized)
    return sanitized[:max_length]


class FileOrganizer:
    """
    Gom cụm và sắp xếp các tệp tài liệu vào các thư mục theo Ngày và Thể loại tài liệu (Document Type):
    Cấu trúc: <base_output_dir>/<YYYYMMDD>/<Document_Type>/<ten_file> 
    (Ví dụ: D:\\outputNLP\\20261002\\Well_Proposal\\Report-sample.pdf)
    Thư mục ngày và thể loại chỉ được tạo khi có tài liệu thực sự cần phân loại.
    """
    def __init__(self, base_output_dir: Path):
        self.base_output_dir = Path(base_output_dir).resolve()
        self.base_output_dir.mkdir(parents=True, exist_ok=True)

    def organize(
        self,
        source_file: Path,
        document_type: Optional[str] = None,
        language: Optional[str] = None,
        contract_name: Optional[str] = None,
        originator: Optional[str] = None,
        mode: str = "move",  # "move" hoặc "copy"
        date_str: Optional[str] = None,
    ) -> Path:
        """
        Di chuyển hoặc sao chép tệp vào thư mục theo Ngày (<YYYYMMDD>) và Thể loại tài liệu chuẩn hóa.
        Chỉ tạo thư mục ngày và thể loại khi có tệp thực sự cần chuyển vào (on-demand).
        """
        source_file = Path(source_file)
        if not source_file.exists():
            return source_file

        if not date_str:
            from datetime import datetime
            date_str = datetime.now().strftime("%Y%m%d")

        safe_doc_type = sanitize_folder_name(document_type or "General_Report")

        # Cấu trúc: <base_output_dir>/<YYYYMMDD>/<Document_Type>/<ten_file>
        target_dir = self.base_output_dir / date_str / safe_doc_type
        target_dir.mkdir(parents=True, exist_ok=True)

        target_file_path = target_dir / source_file.name

        # Nếu tệp nguồn đã nằm đúng vị trí đích
        if source_file.resolve() == target_file_path.resolve():
            return target_file_path

        # Nếu tệp đích đã tồn tại, xóa trước khi move để tránh lỗi FileExistsError trên Windows
        if target_file_path.exists():
            try:
                target_file_path.unlink()
            except Exception:
                pass

        try:
            if mode == "move":
                try:
                    shutil.move(str(source_file), str(target_file_path))
                    logger.info(f"🚚 [MOVE] Đã di chuyển file '{source_file.name}' vào '{target_dir.relative_to(self.base_output_dir)}'")
                except PermissionError:
                    # Nếu tệp đang bị mở trong Word/Reader, sao chép sang trước để không làm gián đoạn
                    shutil.copy2(source_file, target_file_path)
                    logger.warning(f"⚠️ [COPY FALLBACK] Tệp '{source_file.name}' đang mở trong ứng dụng khác. Đã sao chép sang '{target_dir.relative_to(self.base_output_dir)}'")
            else:
                shutil.copy2(source_file, target_file_path)
                logger.info(f"📋 [COPY] Đã sao chép file '{source_file.name}' vào '{target_dir.relative_to(self.base_output_dir)}'")
            return target_file_path
        except Exception as e:
            logger.error(f"Lỗi khi di chuyển/sao chép tệp '{source_file}': {e}")
            return source_file

