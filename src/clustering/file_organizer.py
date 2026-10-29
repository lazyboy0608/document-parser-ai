import re
import shutil
from pathlib import Path
from typing import Optional

from src.utils.logger import logger
from src.extraction.domain_cleaner import is_missing_or_unknown_phrase


def sanitize_folder_name(name: str, max_length: int = 50) -> str:
    """
    Chuẩn hóa chuỗi để làm tên thư mục hợp lệ trên Windows (loại bỏ ký tự cấm: \\ / : * ? \" < > |).
    Tự động chuẩn hóa các giá trị rỗng, N/A hoặc chuỗi tiếng Trung về 'Unknown'.
    """
    if not name or is_missing_or_unknown_phrase(name):
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


def resolve_cluster_folder_name(
    is_duplicate: bool = False,
    contract_name: Optional[str] = None,
    well_link: Optional[str] = None,
    originator: Optional[str] = None,
    document_type: Optional[str] = None,
) -> str:
    """
    Xác định tên thư mục gom cụm theo thứ tự ưu tiên:
    1. Trùng lặp -> 'Trùng lặp'
    2. Hợp đồng / Lô mỏ / Dự án (contract_name)
    3. Giếng khoan (well_link) -> 'Well_<well_link>'
    4. Đơn vị ban hành lớn (originator)
    5. Thể loại tài liệu hoặc 'Tai_lieu_chung' (Fallback)
    """
    # 1. Ưu tiên cao nhất: Tệp trùng lặp (SHA-256 hoặc SimHash)
    if is_duplicate:
        return "Trùng lặp"

    # 2. Ưu tiên Lô / Mỏ / Dự án / Hợp đồng
    c_clean = str(contract_name or "").strip()
    if c_clean and not is_missing_or_unknown_phrase(c_clean):
        return sanitize_folder_name(c_clean)

    # 3. Ưu tiên Giếng khoan
    w_clean = str(well_link or "").strip()
    if w_clean and not is_missing_or_unknown_phrase(w_clean):
        prefix = "" if any(w_clean.upper().startswith(k) for k in ["WELL", "LK", "SKV", "BLOCK"]) else "Well_"
        return sanitize_folder_name(f"{prefix}{w_clean}")

    # 4. Ưu tiên Đơn vị ban hành lớn
    o_clean = str(originator or "").strip()
    if o_clean and not is_missing_or_unknown_phrase(o_clean):
        return sanitize_folder_name(o_clean)

    # 5. Fallback về Thể loại tài liệu hoặc Thư mục chung
    if document_type and document_type.strip() and document_type.strip().lower() not in {"general_report", "chưa rõ", "unknown", "none"}:
        return sanitize_folder_name(document_type.strip())

    return "Tai_lieu_chung"


class FileOrganizer:
    """
    Gom cụm và sắp xếp các tệp tài liệu vào các thư mục theo Ngày và Dự án / Lô mỏ / Hợp đồng:
    Cấu trúc: <base_output_dir>/<YYYYMMDD>/<Cluster_Folder>/<ten_file> 
    (Ví dụ: D:\\outputNLP\\20261020\\BLOCKS_15-1_&_15-2\\Report-sample.pdf hoặc D:\\outputNLP\\20261020\\Trùng lặp\\...)
    Thư mục ngày và phân cụm chỉ được tạo khi có tài liệu thực sự cần phân loại.
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
        well_link: Optional[str] = None,
        originator: Optional[str] = None,
        is_duplicate: bool = False,
        mode: str = "move",  # "move" hoặc "copy"
        date_str: Optional[str] = None,
    ) -> Optional[Path]:
        """
        Di chuyển hoặc sao chép tệp vào thư mục theo Ngày (<YYYYMMDD>) và Phân cụm Dự án / Trùng lặp.
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

        cluster_name = resolve_cluster_folder_name(
            is_duplicate=is_duplicate,
            contract_name=contract_name,
            well_link=well_link,
            originator=originator,
            document_type=document_type,
        )

        # Cấu trúc: <base_output_dir>/<YYYYMMDD>/<Cluster_Folder>/<ten_file>
        target_dir = self.base_output_dir / date_str / cluster_name
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

