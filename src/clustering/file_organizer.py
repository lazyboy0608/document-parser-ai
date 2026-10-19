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
    ) -> Optional[Path]:
        """
        Di chuyển hoặc sao chép tệp vào thư mục theo Ngày (<YYYYMMDD>) và Thể loại tài liệu chuẩn hóa.
        Sử dụng cơ chế Giao dịch 2 pha (Two-Phase Safe Transfer):
        1. Ghi ra tệp tạm '.nlp_tmp' trên thư mục đích để đảm bảo dữ liệu toàn vẹn qua mạng LAN.
        2. So khớp dung lượng tệp; nếu khớp 100%, thực hiện đổi tên nguyên tử (atomic replace).
        3. Dọn dẹp tệp nguồn (nếu mode='move').
        4. Nếu gặp lỗi đứt mạng, tự động dọn tệp rác dở dang và trả về None (báo lỗi chuyển tệp).
        """
        source_file = Path(source_file)
        if not source_file.exists():
            return None

        if not date_str:
            from datetime import datetime
            date_str = datetime.now().strftime("%Y%m%d")

        safe_doc_type = sanitize_folder_name(document_type or "General_Report")

        # Cấu trúc: <base_output_dir>/<YYYYMMDD>/<Document_Type>/<ten_file>
        target_dir = self.base_output_dir / date_str / safe_doc_type
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error(f"❌ Không thể tạo thư mục đích '{target_dir}' qua mạng: {e}")
            return None

        target_file_path = target_dir / source_file.name

        # Nếu tệp nguồn đã nằm đúng vị trí đích
        try:
            if source_file.resolve() == target_file_path.resolve():
                return target_file_path
        except OSError:
            pass

        temp_target = target_dir / f"{source_file.name}.nlp_tmp"

        try:
            source_size = source_file.stat().st_size

            # Ghi ra tệp tạm trên thư mục đích trước để chống đứt kết nối mạng giữa chừng
            shutil.copy2(str(source_file), str(temp_target))

            # Xác thực dữ liệu đã truyền trọn vẹn 100% qua mạng LAN
            if not temp_target.exists() or temp_target.stat().st_size != source_size:
                raise IOError(f"Tệp truyền qua mạng bị lỗi hoặc không khớp kích thước: {source_file.name}")

            # Đổi tên nguyên tử trên cùng thư mục đích
            temp_target.replace(target_file_path)

            if mode == "move":
                try:
                    source_file.unlink()
                    logger.info(f"🚚 [SAFE MOVE] Đã di chuyển an toàn '{source_file.name}' vào '{target_dir.relative_to(self.base_output_dir)}'")
                except PermissionError:
                    logger.warning(f"⚠️ [COPY FALLBACK] Tệp '{source_file.name}' đang bị tiến trình khác giữ khóa. Đã hoàn tất sao chép sang '{target_dir.relative_to(self.base_output_dir)}'")
            else:
                logger.info(f"📋 [SAFE COPY] Đã sao chép an toàn '{source_file.name}' vào '{target_dir.relative_to(self.base_output_dir)}'")

            return target_file_path

        except Exception as e:
            logger.error(f"❌ Lỗi mạng/IO khi chuyển tệp '{source_file.name}' sang '{target_dir}': {e}")
            # Dọn dẹp tệp tạm dở dang nếu còn tồn tại
            try:
                if temp_target.exists():
                    temp_target.unlink()
            except Exception:
                pass
            return None

