from pathlib import Path
from typing import List, Dict, Any, Optional

from config.settings import settings
from src.utils.logger import logger
from src.utils.state_tracker import StateTracker
from src.ingestion.document_loader import DocumentLoader
from src.deduplication.pipeline_dedup import DeduplicationPipeline, DedupResult
from src.extraction.pipeline_ai import AIExtractionPipeline
from src.clustering.file_organizer import FileOrganizer
from src.reporting.excel_exporter import ExcelExporter


class DocumentPipeline:
    """
    Bộ điều phối toàn diện (Pipeline Orchestrator) Hybrid:
    - TẦNG 1: DeduplicationPipeline siêu tốc thuần CPU (~0.05s) với liên kết 2 chiều & cập nhật ngược.
      Nếu phát hiện trùng lặp -> Short-circuit dừng ngay lập tức, không gọi AI.
    - TẦNG 2: AIExtractionPipeline chuyên sâu chỉ tiếp nhận tệp mới tinh.
    """
    def __init__(self, output_dir: Optional[Path] = None, mode: str = "move", db_path: Optional[Path] = None):
        self.output_dir = Path(output_dir or settings.OUTPUT_DIR).resolve()
        self.mode = mode
        self.db_path = Path(db_path).resolve() if db_path else (settings.CACHE_DIR / "pipeline_state.db")
        self.state_tracker = StateTracker(self.db_path)
        self.file_organizer = FileOrganizer(self.output_dir)

        # Khởi tạo Tầng 1: Lọc trùng lặp CPU siêu tốc
        self.dedup_pipeline = DeduplicationPipeline(
            state_tracker=self.state_tracker,
            file_organizer=self.file_organizer,
            mode=self.mode,
            simhash_threshold_bits=settings.SIMHASH_THRESHOLD_BITS,
        )

        # Khởi tạo Tầng 2: Trích xuất AI (LLM + VLM)
        self.ai_pipeline = AIExtractionPipeline()

        # Giữ lại các thuộc tính cũ để tương thích ngược hoàn toàn với unit tests/caller cũ
        self.exact_dedup = self.dedup_pipeline.exact_dedup
        self.simhash_dedup = self.dedup_pipeline.simhash_dedup
        self.active_simhashes = self.dedup_pipeline.active_simhashes
        self.diff_analyzer = self.dedup_pipeline.diff_analyzer
        self.llm_extractor = self.ai_pipeline.llm_extractor
        self.vlm_extractor = self.ai_pipeline.vlm_extractor

    def process_single_file(self, file_path: Path, date_str: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Xử lý đơn lẻ 1 tệp tài liệu (phục vụ cả quét lô hàng loạt lẫn cơ chế Hot Folder Watcher).
        """
        file_path = file_path.resolve()
        if not file_path.exists() or not file_path.is_file():
            return None

        if file_path.suffix.lower() not in DocumentLoader.SUPPORTED_EXTENSIONS:
            return None

        if not date_str:
            from datetime import datetime
            date_str = datetime.now().strftime("%Y%m%d")

        # 1. Kiểm tra nếu đã xử lý thành công trong checkpoint SQLite trước đó
        if self.state_tracker.is_processed(str(file_path)):
            completed_all = self.state_tracker.get_all_completed()
            cached_item = completed_all.get(str(file_path))
            if cached_item:
                meta_cached = cached_item.get("metadata", {}) or {}
                if meta_cached.get("confidence_score", 0.0) > 0:
                    logger.info(f"-> Đã xử lý trong phiên trước (Load từ cache): {file_path.name}")
                    target_path = self.file_organizer.organize(
                        source_file=file_path,
                        document_type=meta_cached.get("document_type"),
                        language=meta_cached.get("language"),
                        contract_name=meta_cached.get("contract_name"),
                        well_link=meta_cached.get("well_link"),
                        originator=meta_cached.get("originator"),
                        is_duplicate=cached_item.get("is_duplicate", False),
                        mode=self.mode,
                        date_str=date_str,
                    )
                    final_path = target_path if self.mode == "move" else file_path
                    item_res = dict(cached_item)
                    item_res["file_path"] = str(final_path)
                    item_res["file_name"] = target_path.name
                    item_res["date_str"] = date_str
                    if self.mode == "move" and target_path != file_path:
                        self.state_tracker.delete_state(str(file_path))
                        self.state_tracker.save_state(
                            str(target_path),
                            cached_item.get("file_hash", ""),
                            "COMPLETED",
                            is_duplicate=cached_item.get("is_duplicate", False),
                            duplicate_of=cached_item.get("duplicate_of"),
                            metadata=meta_cached
                        )
                    return item_res
                # Nếu bản ghi cache cũ chưa được tính Độ tin cậy -> Nạp văn bản & chấm điểm động cho bản ghi này
                logger.info(f"-> Tính toán bổ sung Độ tin cậy & Highlight cho bản ghi cache: {file_path.name}")
                try:
                    doc_data = DocumentLoader.load_document(file_path)
                    from src.extraction.schemas import DocumentMetadata
                    from src.extraction.confidence_scorer import evaluate_metadata_confidence
                    meta_obj = clean_metadata_fields(DocumentMetadata(**meta_cached), raw_text=doc_data.get("text", ""))
                    meta_obj = evaluate_metadata_confidence(
                        meta_obj,
                        raw_text=doc_data.get("text", ""),
                        is_scanned=bool(doc_data.get("is_scanned", False)),
                        ocr_lines=doc_data.get("ocr_lines", []),
                    )
                    file_hash = self.exact_dedup.calculate_file_hash(file_path)
                    updated_dict = meta_obj.model_dump()
                    target_path = self.file_organizer.organize(
                        source_file=file_path,
                        document_type=getattr(meta_obj, "document_type", None),
                        language=meta_obj.language,
                        contract_name=meta_obj.contract_name,
                        well_link=getattr(meta_obj, "well_link", None),
                        originator=meta_obj.originator,
                        is_duplicate=cached_item.get("is_duplicate", False),
                        mode=self.mode,
                        date_str=date_str,
                    )
                    if not target_path:
                        logger.error(f"Không thể di chuyển file cached {file_path.name} do lỗi truyền tải mạng LAN.")
                        return None

                    final_path = target_path if self.mode == "move" else file_path
                    if self.mode == "move" and target_path != file_path:
                        self.state_tracker.delete_state(str(file_path))
                    self.state_tracker.save_state(
                        str(final_path),
                        file_hash,
                        "COMPLETED",
                        is_duplicate=cached_item.get("is_duplicate", False),
                        duplicate_of=cached_item.get("duplicate_of"),
                        metadata=updated_dict,
                    )
                    cached_item["metadata"] = updated_dict
                    cached_item["file_path"] = str(final_path)
                    cached_item["file_name"] = target_path.name
                    cached_item["date_str"] = date_str
                    logger.info(
                        f"-> [CONFIDENCE] {file_path.name}: {meta_obj.confidence_score:.1f}% | "
                        f"Highlight: {meta_obj.low_confidence_fields or 'Không có'}"
                    )
                    return cached_item
                except Exception as e:
                    logger.warning(f"Không thể tính bổ sung confidence cho {file_path.name}: {e}")
                    return cached_item

        # 2. Lọc trùng lặp tuyệt đối (Tầng 1 - Exact Hash)
        file_hash = self.exact_dedup.calculate_file_hash(file_path)
        # 2. TẦNG 1: LỌC TRÙNG LẶP SIÊU TỐC THUẦN CPU (~0.05s) & LIÊN KẾT 2 CHIỀU
        dedup_res: DedupResult = self.dedup_pipeline.process(file_path, date_str=date_str)
        if dedup_res.is_duplicate:
            # Short-circuit: Trùng lặp -> Dừng ngay lập tức, hoàn toàn không gọi AI
            final_p = dedup_res.final_path or file_path
            tgt_name = dedup_res.target_path.name if dedup_res.target_path else file_path.name
            return {
                "file_name": tgt_name,
                "file_path": str(final_p),
                "is_duplicate": True,
                "duplicate_of": dedup_res.matched_orig_name,
                "duplicate_group_id": dedup_res.group_id,
                "metadata": dedup_res.inherited_metadata or {},
                "date_str": date_str,
            }

        doc_data = dedup_res.doc_data
        if not doc_data:
            # Trường hợp lỗi nạp văn bản
            return None

        file_hash = dedup_res.file_hash
        simhash_hex = dedup_res.simhash_hex

        # 3. TẦNG 2: AI EXTRACTION PIPELINE CHUYÊN SÂU (Chỉ chạy cho tệp MỚI TINH)
        metadata = self.ai_pipeline.process(file_path=file_path, doc_data=doc_data)
        meta_dict = metadata.model_dump()
        meta_dict["duplicate_status"] = "Gốc (Không trùng)"

        # 4. Phân cụm và tổ chức file vào thư mục đích theo Ngày & Dự án
        target_path = self.file_organizer.organize(
            source_file=file_path,
            document_type=metadata.document_type,
            language=metadata.language,
            contract_name=metadata.contract_name,
            well_link=metadata.well_link,
            originator=metadata.originator,
            is_duplicate=False,
            mode=self.mode,
            date_str=date_str,
        )

        if not target_path:
            logger.error(f"Không thể di chuyển file {file_path.name} sang thư mục đích do lỗi mạng LAN/IO.")
            self.state_tracker.save_state(str(file_path), file_hash, "ERROR")
            return None

        final_path = target_path if self.mode == "move" else file_path

        # 5. Lưu checkpoint vào SQLite (nếu move, xóa file nguồn ở dropzone và lưu vị trí mới)
        if self.mode == "move" and target_path != file_path:
            self.state_tracker.delete_state(str(file_path))
            self.state_tracker.save_state(
                str(target_path),
                file_hash,
                "COMPLETED",
                is_duplicate=False,
                duplicate_of=None,
                metadata=meta_dict,
                simhash=simhash_hex
            )
        else:
            self.state_tracker.save_state(
                str(final_path),
                file_hash,
                "COMPLETED",
                is_duplicate=False,
                duplicate_of=None,
                metadata=meta_dict,
                simhash=simhash_hex
            )

        # 6. Đăng ký tệp mới độc nhất vào danh sách tra cứu để các tệp nạp sau đối chiếu
        self.dedup_pipeline.register_new_unique_file(final_path, file_hash, simhash_hex)

        return {
            "file_name": target_path.name,
            "file_path": str(final_path),
            "is_duplicate": False,
            "duplicate_of": None,
            "metadata": meta_dict,
            "date_str": date_str,
        }

    def export_current_report(
        self,
        input_dir: Optional[Path] = None,
        output_report: Optional[Path] = None,
        date_str: Optional[str] = None,
    ) -> Optional[Path]:
        """
        Tổng hợp toàn bộ tài liệu của ngày <date_str> đã di chuyển/phân loại trong output_dir (kết hợp SQLite state)
        và cập nhật ra file Excel riêng của ngày đó: <output_dir>/<YYYYMMDD>/tong_hop_tai_lieu_<YYYYMMDD>.xlsx.
        Không tự tạo file Excel trống nếu thư mục ngày đó chưa có tài liệu.
        """
        if not date_str:
            from datetime import datetime
            date_str = datetime.now().strftime("%Y%m%d")

        daily_dir = (self.output_dir / date_str).resolve()
        if not daily_dir.exists():
            logger.info(f"Thư mục ngày {date_str} chưa tồn tại ({daily_dir}), bỏ qua tạo báo cáo Excel trống.")
            return None

        # Quét các tệp tài liệu thực tế hiện diện trong thư mục ngày hôm đó
        daily_files = [
            f for f in daily_dir.rglob("*")
            if (
                f.is_file()
                and f.suffix.lower() in DocumentLoader.SUPPORTED_EXTENSIONS
                and not f.name.startswith("~$")
                and not f.name.startswith(".")
                and f.suffix.lower() != ".xlsx"
            )
        ]

        if not daily_files:
            logger.info(f"Không có tài liệu nào trong thư mục ngày {date_str}, bỏ qua tạo báo cáo Excel trống.")
            return None

        if output_report is None:
            output_report = daily_dir / f"tong_hop_tai_lieu_{date_str}.xlsx"
        else:
            output_report = Path(output_report).resolve()

        completed_all = self.state_tracker.get_all_completed()
        path_lookup = {norm_p: item for norm_p, item in completed_all.items()}
        name_lookup = {Path(norm_p).name.lower(): item for norm_p, item in completed_all.items()}

        results = []
        seen_files = set()
        for f in sorted(daily_files, key=lambda x: x.name.lower()):
            f_res = str(f.resolve())
            f_name = f.name
            f_lower = f_name.lower()
            if f_lower in seen_files:
                continue
            seen_files.add(f_lower)

            if f_res in path_lookup:
                item_copy = dict(path_lookup[f_res])
                item_copy["file_path"] = f_res
                item_copy["file_name"] = f_name
                results.append(item_copy)
            elif f_lower in name_lookup:
                item_copy = dict(name_lookup[f_lower])
                item_copy["file_path"] = f_res
                item_copy["file_name"] = f_name
                results.append(item_copy)
            else:
                results.append({
                    "file_name": f_name,
                    "file_path": f_res,
                    "is_duplicate": False,
                    "metadata": {}
                })

        saved_path = ExcelExporter.export(results, output_report)
        logger.info(f"📊 Đã cập nhật báo cáo Excel ngày {date_str} ({len(results)} tài liệu) tại: {saved_path}")

        # Chỉ sao lưu bản phụ nếu REPORTS_DIR là thư mục độc lập khác với output_dir
        backup_dir = settings.REPORTS_DIR.resolve()
        if backup_dir != self.output_dir.resolve() and backup_dir != daily_dir:
            backup_report = (backup_dir / f"tong_hop_tai_lieu_{date_str}.xlsx").resolve()
            if output_report != backup_report:
                try:
                    ExcelExporter.export(results, backup_report)
                except Exception as e:
                    logger.warning(f"Không thể cập nhật bản sao lưu tại {backup_report}: {e}")

        return saved_path

    def run(
        self,
        input_dir: Path = settings.INPUT_DIR,
        output_report: Optional[Path] = None,
        date_str: Optional[str] = None,
    ) -> Optional[Path]:
        input_dir = Path(input_dir).resolve()
        if not date_str:
            from datetime import datetime
            date_str = datetime.now().strftime("%Y%m%d")

        logger.info(f"Khởi động pipeline xử lý tài liệu tại: {input_dir}")

        # 1. Tìm kiếm tất cả tệp được hỗ trợ trong input_dir
        all_files = [
            f.resolve() for f in input_dir.rglob("*")
            if f.is_file() and f.suffix.lower() in DocumentLoader.SUPPORTED_EXTENSIONS
                 and not f.name.startswith("~$")
                 and not f.name.startswith(".")
        ]

        total_files = len(all_files)
        logger.info(f"Đã phát hiện tổng cộng {total_files} tài liệu cần xử lý.")

        if total_files == 0:
            logger.warning(f"Không có tệp nào trong {input_dir}. Vui lòng sao chép tài liệu vào thư mục này.")
            return None

        processed_results: List[Dict[str, Any]] = []

        for idx, file_path in enumerate(all_files, 1):
            logger.info(f"[{idx}/{total_files}] Đang xử lý: {file_path.name}")
            try:
                res = self.process_single_file(file_path, date_str=date_str)
                if res is not None:
                    processed_results.append(res)
            except Exception as e:
                logger.error(f"❌ [BYPASS LỖI] Lỗi không mong muốn khi xử lý '{file_path.name}': {e}. Bỏ qua tệp này để tiếp tục xử lý các tệp kế tiếp.")

        # Xuất báo cáo tổng kết Excel theo ngày
        saved_path = self.export_current_report(
            input_dir=input_dir,
            output_report=output_report,
            date_str=date_str
        )
        logger.info(f"✅ Hoàn tất đợt xử lý! Báo cáo Excel: {saved_path}")
        return saved_path

