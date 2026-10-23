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

            # Ghi chú trực quan đối chiếu nhanh sai khác cho tệp trùng lặp
            dup_col_idx = col_name_to_idx.get("Trạng thái trùng lặp")
            if dup_col_idx:
                dup_cell = ws.cell(row=row_offset, column=dup_col_idx)
                if item.get("is_duplicate"):
                    diff_info = meta.get("diff_info") or {}
                    summary_txt = diff_info.get("summary")
                    sim_pct = diff_info.get("similarity_pct", 95.0)
                    if summary_txt:
                        comment_txt = (
                            f"🔍 ĐỐI CHIẾU SAI KHÁC NHANH (AI Diff):\n"
                            f"• Độ tương đồng: {sim_pct:.1f}%\n"
                            f"• Điểm khác biệt: {summary_txt}\n"
                            f"• Chi tiết: Xem Tab 'Đối chiếu Trùng lặp'."
                        )
                        diff_comment = Comment(comment_txt, "AI Semantic Diff Auditor")
                        diff_comment.width = 330
                        diff_comment.height = 95
                        dup_cell.comment = diff_comment
                elif meta.get("duplicate_variants"):
                    # Trường hợp tệp nạp đầu tiên nhưng có các tệp phái sinh/trùng lặp nạp sau
                    variants_list = meta.get("duplicate_variants", [])
                    vars_str = ", ".join(variants_list)
                    gid = meta.get("duplicate_group_id", "")
                    comment_txt = (
                        f"⚠️ CẢNH BÁO TÀI LIỆU GỐC CÓ BẢN PHÁI SINH:\n"
                        f"• Nhóm tài liệu: {gid or 'Chung nhóm'}\n"
                        f"• Các tệp trùng lặp/nháp nạp sau: {vars_str}\n"
                        f"• Vui lòng kiểm tra chi tiết tại Tab 'Đối chiếu Trùng lặp'."
                    )
                    rev_comment = Comment(comment_txt, "AI Document Group Auditor")
                    rev_comment.width = 330
                    rev_comment.height = 95
                    dup_cell.comment = rev_comment

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
            "Trạng thái trùng lặp": 40,
        }
        for col_name, width in col_widths.items():
            if col_name in col_name_to_idx:
                col_letter = get_column_letter(col_name_to_idx[col_name])
                ws.column_dimensions[col_letter].width = width

    @classmethod
    def _export_diff_sheet(cls, writer: pd.ExcelWriter, data: List[Dict[str, Any]]):
        """Tự động sinh Sheet 2 'Đối chiếu Trùng lặp' gom toàn bộ các tệp trùng lặp trong ngày."""
        dup_items = [item for item in data if item.get("is_duplicate")]
        if not dup_items:
            return

        diff_rows = []
        for idx, item in enumerate(dup_items, 1):
            meta = item.get("metadata", {}) or {}
            diff_info = meta.get("diff_info", {}) or {}
            orig_name = item.get("duplicate_of") or meta.get("duplicate_of") or "Chưa rõ"
            sim_pct_val = diff_info.get("similarity_pct")
            if sim_pct_val is not None:
                sim_str = f"{float(sim_pct_val):.1f}%"
            elif meta.get("duplicate_type") == "exact" or meta.get("title") == "Tài liệu trùng lặp":
                sim_str = "100.0%"
            else:
                sim_str = "≥95.0%"

            summary_text = diff_info.get("summary") or (
                "Trùng khớp tuyệt đối 100% nội dung (cùng mã băm SHA-256)"
                if sim_str == "100.0%"
                else f"Gần trùng lặp nội dung với tệp '{orig_name}'"
            )

            added_snippets = "\n• ".join(diff_info.get("added_snippets", []))
            if added_snippets:
                added_snippets = "• " + added_snippets

            deleted_snippets = "\n• ".join(diff_info.get("deleted_snippets", []))
            if deleted_snippets:
                deleted_snippets = "• " + deleted_snippets

            group_id = item.get("duplicate_group_id") or meta.get("duplicate_group_id") or "N/A"

            diff_rows.append({
                "STT": idx,
                "Mã nhóm (Group ID)": group_id,
                "Tên tệp trùng lặp": item.get("file_name", ""),
                "Tệp gốc đối chiếu": orig_name,
                "Độ tương đồng": sim_str,
                "Tóm tắt sai khác (AI Diff Summary)": summary_text,
                "Trích đoạn Thêm mới (+)": added_snippets or "(Không có)",
                "Trích đoạn Bị cắt bỏ (-)": deleted_snippets or "(Không có)",
            })

        df_diff = pd.DataFrame(diff_rows)
        df_diff.to_excel(writer, index=False, sheet_name="Đối chiếu Trùng lặp")
        ws_diff = writer.sheets["Đối chiếu Trùng lặp"]

        # Định dạng Header Sheet 2
        header_fill = PatternFill(start_color="104C82", end_color="104C82", fill_type="solid")
        header_font = Font(bold=True, color="FFFFFF", name="Arial", size=10)
        thin_side = Side(border_style="thin", color="D9D9D9")
        normal_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)

        for col_idx in range(1, len(df_diff.columns) + 1):
            cell = ws_diff.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = normal_border
        ws_diff.row_dimensions[1].height = 26

        # Định dạng Data Rows Sheet 2
        for row_idx in range(2, len(diff_rows) + 2):
            ws_diff.row_dimensions[row_idx].height = 36
            for col_idx in range(1, len(df_diff.columns) + 1):
                c = ws_diff.cell(row=row_idx, column=col_idx)
                c.font = Font(name="Arial", size=9.5)
                c.border = normal_border
                c.alignment = Alignment(vertical="top", wrap_text=True)
                if col_idx in {1, 4}:  # STT và Độ tương đồng
                    c.alignment = Alignment(horizontal="center", vertical="center")

        diff_widths = {
            "STT": 6,
            "Tên tệp trùng lặp": 28,
            "Tệp gốc đối chiếu": 28,
            "Độ tương đồng": 16,
            "Tóm tắt sai khác (AI Diff Summary)": 45,
            "Trích đoạn Thêm mới (+)": 35,
            "Trích đoạn Bị cắt bỏ (-)": 35,
        }
        for col_name, width in diff_widths.items():
            if col_name in df_diff.columns:
                col_letter = get_column_letter(df_diff.columns.get_loc(col_name) + 1)
                ws_diff.column_dimensions[col_letter].width = width

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

                # Xác định rõ trạng thái trùng lặp và tên tệp gốc đối chiếu (loại bỏ thuật ngữ SimHash/SHA-256)
                dup_status = meta.get("duplicate_status")
                if not dup_status:
                    if item.get("is_duplicate"):
                        dup_of = item.get("duplicate_of") or meta.get("duplicate_of")
                        if meta.get("title") == "Tài liệu trùng lặp" or meta.get("duplicate_type") == "exact":
                            dup_status = f"Trùng khớp 100% (với {dup_of})" if dup_of else "Trùng khớp 100%"
                        else:
                            dup_status = f"Gần trùng lặp (với {dup_of})" if dup_of else "Gần trùng lặp"
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
                # Tự động xuất Sheet 2 "Đối chiếu Trùng lặp" nếu có tệp trùng lặp
                cls._export_diff_sheet(writer, data)
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

