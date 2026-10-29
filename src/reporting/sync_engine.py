import json
import sqlite3
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import pandas as pd

from config.settings import settings
from src.clustering.file_organizer import resolve_cluster_folder_name
from src.extraction.confidence_scorer import FIELD_WEIGHTS, MANDATORY_FIELDS
from src.utils.logger import logger
from src.utils.state_tracker import StateTracker


class ExcelSyncEngine:
    """
    Bộ đồng bộ dữ liệu hai chiều từ Excel về Cơ sở dữ liệu SQLite (Human-in-the-Loop).
    Cho phép con người kiểm duyệt, sửa đổi bất kỳ ô nào trên bảng tính Excel (kể cả ô có cảnh báo hay ô bình thường),
    tự động công nhận giá trị của con người là Chân lý chuẩn (Ground Truth: 100% tin cậy),
    và tự động điều chuyển thư mục phân loại nếu thay đổi Dự án / Giếng khoan.
    """

    EXCEL_TO_METADATA_MAP = {
        "TITLE": "title",
        "AUTHOR": "author",
        "ORIGINATOR": "originator",
        "CONTRACTOR": "contractor",
        "PUBLIC DATE": "public_date",
        "Well link": "well_link",
        "CONTRACT NAME": "contract_name",
        "LANGUAGE": "language",
        "DOCUMENT TYPE": "document_type",
        "ENCL": "encl",
        "Encl Name": "encl_name",
    }

    def __init__(self, db_path: Optional[Path] = None, output_dir: Optional[Path] = None):
        self.db_path = db_path or (settings.CACHE_DIR / "pipeline_state.db")
        self.output_dir = output_dir or settings.OUTPUT_DIR
        self.state_tracker = StateTracker(self.db_path)

    @staticmethod
    def _clean_str(val: Any) -> str:
        if val is None or pd.isna(val):
            return ""
        s = str(val).strip()
        # Chuẩn hóa nếu là dạng float số nguyên (ví dụ 1975.0 -> 1975)
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        return s

    def sync_from_excel(
        self,
        excel_path: Path,
        auto_recluster: bool = True
    ) -> Dict[str, Any]:
        """
        Đọc và đồng bộ dữ liệu người dùng đã chỉnh sửa từ tệp Excel vào CSDL SQLite.
        """
        excel_path = Path(excel_path).resolve()
        if not excel_path.exists():
            raise FileNotFoundError(f"Không tìm thấy tệp Excel tại: {excel_path}")

        logger.info(f"🔄 Bắt đầu đồng bộ dữ liệu từ Excel vào CSDL: {excel_path.name}")

        df = pd.read_excel(excel_path, sheet_name=0, keep_default_na=False)
        
        # Kiểm tra sự tồn tại của các cột bắt buộc
        if "Đường dẫn tệp gốc" not in df.columns and "Tên tệp" not in df.columns:
            raise ValueError("Tệp Excel không chứa cột 'Đường dẫn tệp gốc' hoặc 'Tên tệp' để nhận diện tài liệu.")

        results = {
            "total_rows": len(df),
            "updated_files_count": 0,
            "updated_details": {},  # file_name -> list of {field, old, new}
            "reclustered_files": [], # list of {file_name, old_path, new_path}
            "reexported_excel": None,
        }

        date_str = None
        # Xác định ngày từ tên file Excel (ví dụ tong_hop_tai_lieu_20261022.xlsx)
        for part in excel_path.stem.split("_"):
            if len(part) == 8 and part.isdigit() and part.startswith("20"):
                date_str = part
                break

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            for _, row in df.iterrows():
                file_path_val = self._clean_str(row.get("Đường dẫn tệp gốc", ""))
                file_name_val = self._clean_str(row.get("Tên tệp", ""))

                if not file_path_val and not file_name_val:
                    continue

                # Tra cứu bản ghi trong CSDL
                row_db = None
                if file_path_val:
                    cursor.execute(
                        "SELECT file_path, is_duplicate, metadata_json, file_hash FROM processed_files WHERE file_path = ?",
                        (file_path_val,)
                    )
                    row_db = cursor.fetchone()

                if not row_db and file_name_val:
                    cursor.execute(
                        "SELECT file_path, is_duplicate, metadata_json, file_hash FROM processed_files WHERE file_path LIKE ?",
                        (f"%{file_name_val}%",)
                    )
                    row_db = cursor.fetchone()

                if not row_db:
                    logger.warning(f"Không tìm thấy bản ghi trong CSDL cho tệp: {file_name_val or file_path_val}")
                    continue

                db_file_path, is_dup, meta_json_str, file_hash = row_db
                meta = json.loads(meta_json_str) if meta_json_str else {}
                field_confs = meta.get("field_confidences", {}) or {}
                low_fields = set(meta.get("low_confidence_fields", []) or [])
                field_warnings = meta.get("field_warnings", {}) or {}
                verified_fields = set(meta.get("verified_fields", []) or [])

                changes = []
                recluster_needed = False

                for col_name, field_key in self.EXCEL_TO_METADATA_MAP.items():
                    if col_name not in df.columns:
                        continue

                    excel_val = self._clean_str(row.get(col_name))
                    db_val = self._clean_str(meta.get(field_key))

                    # Bỏ qua nếu cả hai đều rỗng hoặc giống nhau
                    if excel_val == db_val:
                        continue
                    if excel_val in {"", "N/A", "None"} and db_val in {"", "N/A", "None"}:
                        continue

                    # Nếu giá trị trên Excel thay đổi
                    logger.info(
                        f"  [HUMAN OVERRIDE] {file_name_val} - Trường '{col_name}': '{db_val}' -> '{excel_val}'"
                    )
                    changes.append({
                        "field": col_name,
                        "field_key": field_key,
                        "old_value": db_val,
                        "new_value": excel_val
                    })

                    # 1. Cập nhật giá trị chân lý chuẩn từ con người
                    meta[field_key] = excel_val
                    # 2. Đặt điểm tin cậy 100% cho trường đã được con người duyệt
                    field_confs[field_key] = 100.0
                    # 3. Gỡ bỏ khỏi danh sách cảnh báo mờ/nhòe
                    if field_key in low_fields:
                        low_fields.remove(field_key)
                    if field_key in field_warnings:
                        del field_warnings[field_key]
                    verified_fields.add(field_key)

                    # Đánh dấu cần xem xét phân cụm lại nếu người dùng sửa trường cấu trúc thư mục
                    if field_key in {"contract_name", "well_link", "originator"}:
                        recluster_needed = True

                if not changes:
                    continue

                results["updated_files_count"] += 1
                results["updated_details"][file_name_val or Path(db_file_path).name] = changes

                # Cập nhật lại toàn bộ cấu trúc metadata
                meta["field_confidences"] = field_confs
                meta["low_confidence_fields"] = sorted(list(low_fields))
                meta["field_warnings"] = field_warnings
                meta["verified_fields"] = sorted(list(verified_fields))

                # Tái tính toán điểm tin cậy tổng thể (có tính tới các trường 100% từ con người)
                weighted_sum = sum(field_confs.get(k, 85.0) * w for k, w in FIELD_WEIGHTS.items())
                meta["confidence_score"] = round(min(100.0, weighted_sum), 1)

                # Kiểm tra phân cụm lại nếu các trường Dự án/Giếng khoan bị sửa
                new_file_path = db_file_path
                if auto_recluster and recluster_needed and not is_dup:
                    new_cluster = resolve_cluster_folder_name(
                        is_duplicate=False,
                        contract_name=meta.get("contract_name"),
                        well_link=meta.get("well_link"),
                        originator=meta.get("originator"),
                        document_type=meta.get("document_type")
                    )
                    current_path = Path(db_file_path)
                    if current_path.parent.name != new_cluster and current_path.exists():
                        target_dir = current_path.parent.parent / new_cluster
                        target_dir.mkdir(parents=True, exist_ok=True)
                        target_file = target_dir / current_path.name
                        
                        logger.info(f"  🚚 [RE-CLUSTER] Di chuyển tệp '{current_path.name}' sang thư mục mới: '{new_cluster}'")
                        shutil.move(str(current_path), str(target_file))
                        
                        # Dọn dẹp thư mục cũ nếu rỗng
                        try:
                            if not any(current_path.parent.iterdir()):
                                current_path.parent.rmdir()
                        except Exception:
                            pass

                        new_file_path = str(target_file)
                        results["reclustered_files"].append({
                            "file_name": current_path.name,
                            "old_folder": current_path.parent.name,
                            "new_folder": new_cluster
                        })

                # Ghi lại trạng thái vào SQLite
                cursor.execute(
                    "UPDATE processed_files SET file_path = ?, metadata_json = ? WHERE file_path = ?",
                    (new_file_path, json.dumps(meta, ensure_ascii=False), db_file_path)
                )

            conn.commit()

        logger.info(f"✓ Đã đồng bộ thành công {results['updated_files_count']} tệp vào SQLite.")

        # Tái xuất lại file Excel để xóa các ô viền cam và cập nhật điểm số mới
        if results["updated_files_count"] > 0 and date_str:
            try:
                from src.pipeline import DocumentPipeline
                pipeline = DocumentPipeline(output_dir=self.output_dir, db_path=self.db_path)
                reexported = pipeline.export_current_report(
                    date_str=date_str,
                    output_report=excel_path,
                    allow_backup_on_lock=False
                )
                results["reexported_excel"] = reexported
                logger.info(f"📊 Đã làm mới báo cáo Excel sau đồng bộ: {reexported}")
            except PermissionError:
                logger.info(
                    f"ℹ️ [AUTO-SYNC] File Excel '{excel_path.name}' hiện đang được mở bởi ứng dụng khác. "
                    f"Dữ liệu cập nhật đã được lưu trữ an toàn 100% vào CSDL SQLite và sẽ hiển thị trên Excel khi đóng tệp hoặc trong đợt xuất tiếp theo."
                )
            except Exception as e:
                logger.warning(f"Không thể làm mới báo cáo Excel sau đồng bộ: {e}")

        return results
