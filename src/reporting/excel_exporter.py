from pathlib import Path
from typing import List, Dict, Any
import uuid
import shutil
import logging
import pandas as pd
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.comments import Comment
from openpyxl.utils import get_column_letter
from config.settings import settings

logger = logging.getLogger("NLP_Pipeline")


class ExcelExporter:
    """
    Xuất danh sách metadata tài liệu Dầu khí đã trích xuất ra tệp Excel (.xlsx) chuẩn 10 trường + Độ tin cậy,
    kèm tính năng tự động tô màu cảnh báo (Highlight) và gắn ghi chú (Comment) vào các ô có nét chữ gốc mờ/nhòe
    hoặc độ tin cậy thấp để người dùng dễ dàng kiểm tra.
    """
    COLUMNS = [
        "STT",
        "Tên tệp",
        "Well link",
        "CONTRACT NAME",
        "TITLE",
        "PUBLIC DATE",
        "ORIGINATOR",
        "CONTRACTOR",
        "AUTHOR",
        "LANGUAGE",
        "DOCUMENT TYPE",
        "ENCL",
        "Encl Name",
        "Độ tin cậy",
        "Đường dẫn tệp gốc",
        "Trạng thái trùng lặp"
    ]

    # Ánh xạ từ tên trường nội bộ trong DocumentMetadata sang tên cột trong Excel
    FIELD_TO_COLUMN = {
        "well_link": "Well link",
        "contract_name": "CONTRACT NAME",
        "title": "TITLE",
        "public_date": "PUBLIC DATE",
        "originator": "ORIGINATOR",
        "contractor": "CONTRACTOR",
        "author": "AUTHOR",
        "language": "LANGUAGE",
        "document_type": "DOCUMENT TYPE",
        "encl": "ENCL",
        "encl_name": "Encl Name",
    }

    @classmethod
    def _apply_formatting_and_highlights(cls, ws, data: List[Dict[str, Any]]):
        """
        Định dạng bảng Excel chuyên nghiệp và tô màu Highlight cho:
        1. Các ô dữ liệu nằm trong `low_confidence_fields` (màu vàng cam cảnh báo + Comment giải thích).
        2. Cột `Độ tin cậy` theo thang màu trực quan (Xanh lá >= 90%, Vàng 80-89%, Cam < 80%).
        """
        header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
        header_font = Font(bold=True, color="FFFFFF", name="Arial", size=10)

        # Màu cảnh báo cho ô bị mờ/nhòe hoặc độ tin cậy thấp
        warn_cell_fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
        warn_cell_font = Font(bold=True, color="C00000", name="Arial", size=10)

        # Màu cho cột Độ tin cậy
        conf_high_fill = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
        conf_high_font = Font(bold=True, color="375623", name="Arial", size=10)

        conf_med_fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
        conf_med_font = Font(bold=True, color="833C0C", name="Arial", size=10)

        conf_low_fill = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")
        conf_low_font = Font(bold=True, color="C00000", name="Arial", size=10)

        thin_side = Side(border_style="thin", color="D9D9D9")
        warn_side = Side(border_style="medium", color="ED7D31")
        normal_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
        warn_border = Border(left=warn_side, right=warn_side, top=warn_side, bottom=warn_side)

        col_name_to_idx = {col_name: idx for idx, col_name in enumerate(cls.COLUMNS, 1)}

        # Định dạng hàng tiêu đề (Header)
        for col_idx in range(1, len(cls.COLUMNS) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = normal_border
        ws.row_dimensions[1].height = 26

        # Định dạng từng dòng dữ liệu và highlight các ô cần lưu ý
        for row_offset, item in enumerate(data, start=2):
            meta = item.get("metadata", {}) or {}
            low_fields = set(meta.get("low_confidence_fields", []) or [])
            field_warnings = meta.get("field_warnings", {}) or {}
            field_confidences = meta.get("field_confidences", {}) or {}
            conf_score = float(meta.get("confidence_score", 0.0) or 0.0)

            for col_idx in range(1, len(cls.COLUMNS) + 1):
                c = ws.cell(row=row_offset, column=col_idx)
                c.font = Font(name="Arial", size=10)
                c.alignment = Alignment(vertical="top", wrap_text=True)
                c.border = normal_border

            # Highlight các ô cụ thể bị mờ/nhòe/khuyết trong 10 trường
            for field_key in low_fields:
                col_title = cls.FIELD_TO_COLUMN.get(field_key)
                if not col_title or col_title not in col_name_to_idx:
                    continue
                col_idx = col_name_to_idx[col_title]
                target_cell = ws.cell(row=row_offset, column=col_idx)
                target_cell.fill = warn_cell_fill
                target_cell.font = warn_cell_font
                target_cell.border = warn_border

                # Đính kèm Comment giải thích lý do cần chú ý khi người dùng di chuột vào ô
                warning_msg = field_warnings.get(
                    field_key,
                    f"Độ tin cậy thấp ({field_confidences.get(field_key, 0):.1f}%) do chữ gốc mờ/nhòe. Cần kiểm tra lại."
                )
                comment = Comment(warning_msg, "AI Confidence Auditor")
                comment.width = 320
                comment.height = 90
                target_cell.comment = comment

            # Tô màu cột "Độ tin cậy"
            conf_col_idx = col_name_to_idx.get("Độ tin cậy")
            if conf_col_idx:
                conf_cell = ws.cell(row=row_offset, column=conf_col_idx)
                conf_cell.alignment = Alignment(horizontal="center", vertical="center")
                if conf_score >= 90.0:
                    conf_cell.fill = conf_high_fill
                    conf_cell.font = conf_high_font
                elif conf_score >= 80.0:
                    conf_cell.fill = conf_med_fill
                    conf_cell.font = conf_med_font
                else:
                    conf_cell.fill = conf_low_fill
                    conf_cell.font = conf_low_font

                if low_fields:
                    flagged_cols = ", ".join(cls.FIELD_TO_COLUMN.get(f, f) for f in low_fields)
                    conf_comment = Comment(
                        f"Độ tin cậy tổng thể: {conf_score:.1f}%\n"
                        f"Các ô cần kiểm tra lại (đã tô vàng): {flagged_cols}",
                        "AI Confidence Auditor"
                    )
                    conf_comment.width = 300
                    conf_comment.height = 80
                    conf_cell.comment = conf_comment

        # Căn chỉnh độ rộng cột dễ nhìn
        col_widths = {
            "STT": 6,
            "Tên tệp": 24,
            "Well link": 18,
            "CONTRACT NAME": 24,
            "TITLE": 38,
            "PUBLIC DATE": 15,
            "ORIGINATOR": 26,
            "CONTRACTOR": 20,
            "AUTHOR": 28,
            "LANGUAGE": 14,
            "DOCUMENT TYPE": 20,
            "ENCL": 8,
            "Encl Name": 48,
            "Độ tin cậy": 14,
            "Đường dẫn tệp gốc": 32,
            "Trạng thái trùng lặp": 28,
        }
        for col_name, width in col_widths.items():
            if col_name in col_name_to_idx:
                col_letter = get_column_letter(col_name_to_idx[col_name])
                ws.column_dimensions[col_letter].width = width

    @classmethod
    def export(cls, data: List[Dict[str, Any]], output_path: Path) -> Path:
        if not data:
            df = pd.DataFrame(columns=cls.COLUMNS)
        else:
            rows = []
            for idx, item in enumerate(data, 1):
                meta = item.get("metadata", {}) or {}
                from src.extraction.domain_cleaner import standardize_language, classify_document_type
                sample_txt = f"{meta.get('title', '')} {meta.get('encl_name', '')}".strip()
                doc_type_val = meta.get("document_type")
                if not doc_type_val or doc_type_val == "General_Report":
                    doc_type_val = classify_document_type(title=meta.get("title", ""), current_type=doc_type_val or "")
                conf_val = float(meta.get("confidence_score", 0.0) or 0.0)
                conf_display = f"{conf_val:.1f}%" if conf_val > 0 else "N/A"

                # Xác định rõ nguyên nhân trùng lặp: SHA-256 hay SimHash nội dung mờ
                dup_status = meta.get("duplicate_status")
                if not dup_status:
                    if item.get("is_duplicate"):
                        if meta.get("title") == "Tài liệu trùng lặp":
                            dup_status = "Trùng lặp 100% (SHA-256)"
                        else:
                            dup_status = "Gần trùng lặp (SimHash >95%)"
                    else:
                        dup_status = "Gốc (Không trùng)"

                rows.append({
                    "STT": idx,
                    "Tên tệp": item.get("file_name", ""),
                    "Well link": meta.get("well_link", "N/A"),
                    "CONTRACT NAME": meta.get("contract_name", "N/A"),
                    "TITLE": meta.get("title", ""),
                    "PUBLIC DATE": meta.get("public_date", ""),
                    "ORIGINATOR": meta.get("originator", ""),
                    "CONTRACTOR": meta.get("contractor", ""),
                    "AUTHOR": meta.get("author", ""),
                    "LANGUAGE": standardize_language(meta.get("language", "English"), sample_text=sample_txt),
                    "DOCUMENT TYPE": doc_type_val,
                    "ENCL": meta.get("encl", "No"),
                    "Encl Name": meta.get("encl_name", "None"),
                    "Độ tin cậy": conf_display,
                    "Đường dẫn tệp gốc": item.get("file_path", ""),
                    "Trạng thái trùng lặp": dup_status
                })
            df = pd.DataFrame(rows)

        # 1. Ghi tệp Excel vào bộ nhớ tạm cục bộ trước (Local-First Staging)
        # Giúp tránh làm hỏng hoặc xóa trắng (0 byte) tệp Excel trên ổ đĩa mạng LAN khi mất kết nối giữa chừng
        local_temp_dir = settings.CACHE_DIR / "temp_reports"
        local_temp_dir.mkdir(parents=True, exist_ok=True)
        temp_export_file = local_temp_dir / f"export_{uuid.uuid4().hex[:8]}_{output_path.name}"

        try:
            with pd.ExcelWriter(temp_export_file, engine="openpyxl") as writer:
                df.to_excel(writer, index=False, sheet_name="Báo cáo Dầu khí")
                ws = writer.sheets["Báo cáo Dầu khí"]
                cls._apply_formatting_and_highlights(ws, data)
        except Exception as write_err:
            logger.error(f"Lỗi khi khởi tạo báo cáo Excel tạm cục bộ: {write_err}")
            if temp_export_file.exists():
                try:
                    temp_export_file.unlink()
                except Exception:
                    pass
            raise write_err

        # 2. Chuyển tệp từ local lên thư mục đích (hỗ trợ mạng LAN với cơ chế Two-Phase Safe Copy)
        target_dir = output_path.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        target_tmp = target_dir / f"{output_path.name}.nlp_tmp"

        def _do_safe_deploy(target_dest: Path) -> Path:
            shutil.copy2(temp_export_file, target_tmp)
            if target_tmp.stat().st_size != temp_export_file.stat().st_size:
                raise IOError(f"Lỗi truyền tải Excel: kích thước không khớp ({target_tmp.stat().st_size} != {temp_export_file.stat().st_size})")
            target_tmp.replace(target_dest)
            return target_dest

        try:
            deployed_path = _do_safe_deploy(output_path)
            return deployed_path
        except PermissionError:
            # Nếu file Excel đích đang bị người dùng khác mở khóa trên mạng LAN, lưu bản sao có timestamp
            import datetime
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = target_dir / f"{output_path.stem}_{ts}.xlsx"
            logger.warning(f"Tệp {output_path.name} đang bị mở khóa trong ứng dụng khác. Đang lưu báo cáo phụ: {backup_path.name}")
            try:
                deployed_path = _do_safe_deploy(backup_path)
                return deployed_path
            except Exception as e2:
                logger.error(f"Lưu file Excel phụ thất bại trên mạng LAN: {e2}")
                raise e2
        except Exception as lan_err:
            logger.error(f"Lỗi xuất báo cáo Excel lên thư mục đích/LAN ({output_path}): {lan_err}")
            if target_tmp.exists():
                try:
                    target_tmp.unlink()
                except Exception:
                    pass
            raise lan_err
        finally:
            if temp_export_file.exists():
                try:
                    temp_export_file.unlink()
                except Exception:
                    pass

