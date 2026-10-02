from pathlib import Path
from typing import List, Dict, Any, Optional

from config.settings import settings
from src.utils.logger import logger
from src.utils.state_tracker import StateTracker
from src.ingestion.document_loader import DocumentLoader
from src.deduplication.exact_dedup import ExactDeduplicator
from src.deduplication.semantic_dedup import SemanticDeduplicator
from src.extraction.llm_extractor import LLMExtractor
from src.extraction.vlm_extractor import VLMExtractor
from src.extraction.domain_cleaner import clean_metadata_fields
from src.clustering.file_organizer import FileOrganizer
# from src.clustering.topic_clustering import TopicClusterer
from src.reporting.excel_exporter import ExcelExporter


class DocumentPipeline:
    """
    Bộ điều phối toàn diện (Pipeline Orchestrator) Hybrid:
    1. Quét tài liệu đầu vào
    2. Checkpoint chống crash với SQLite
    3. Lọc trùng lặp 2 tầng (Exact Hash & Semantic MinHash)
    4. Trích xuất text thông minh (Hỗ trợ PDF số, PDF scan bằng PaddleOCR, DOCX, DOC, TXT)
    5. Điều phối Hybrid:
       - Tài liệu số (Word, PDF sạch) -> Fast LLM (Qwen-2.5:3b)
       - Tài liệu scan mờ/nhòe/con dấu -> VLM (Qwen2.5-VL)
    6. Chuẩn hóa lỗi chính tả chuyên ngành Dầu khí
    7. Tự động phân loại và gom vào thư mục theo [CONTRACT_NAME / ORIGINATOR]
    8. Xuất báo cáo tổng hợp ra file Excel (.xlsx)
    """
    def __init__(self, output_dir: Optional[Path] = None, mode: str = "move"):
        self.output_dir = Path(output_dir or settings.OUTPUT_DIR).resolve()
        self.mode = mode
        self.state_tracker = StateTracker(settings.CACHE_DIR / "pipeline_state.db")
        # Tự động dọn dẹp các bản ghi cũ của các tệp không còn tồn tại trên đĩa (tránh duplicate ảo)
        self.state_tracker.cleanup_missing_files()
        self.exact_dedup = ExactDeduplicator()
        # Nạp danh sách mã băm SHA-256 từ các tệp THỰC SỰ ĐANG TỒN TẠI trên đĩa để nhận diện trùng lặp
        self.exact_dedup.seen_hashes.update(self.state_tracker.get_active_hashes())
        self.semantic_dedup = SemanticDeduplicator(threshold=settings.SIMILARITY_THRESHOLD)
        self.llm_extractor = LLMExtractor()
        self.vlm_extractor = VLMExtractor()
        self.file_organizer = FileOrganizer(self.output_dir)

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
                        language=meta_cached.get("language"),
                        contract_name=meta_cached.get("contract_name"),
                        originator=meta_cached.get("originator"),
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
                        language=meta_obj.language,
                        contract_name=meta_obj.contract_name,
                        originator=meta_obj.originator,
                        mode=self.mode,
                        date_str=date_str,
                    )
                    final_path = target_path if self.mode == "move" else file_path
                    if self.mode == "move" and target_path != file_path:
                        self.state_tracker.delete_state(str(file_path))
                    self.state_tracker.save_state(
                        str(final_path),
                        file_hash,
                        "COMPLETED",
                        is_duplicate=cached_item.get("is_duplicate", False),
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
        if self.exact_dedup.is_duplicate_file(file_path):
            logger.warning(f"-> Phát hiện trùng lặp tuyệt đối (100% giống file khác): {file_path.name}")
            dup_meta = {
                "title": "Tài liệu trùng lặp",
                "contract_name": "N/A",
                "well_link": "N/A",
                "public_date": "N/A",
                "originator": "N/A",
                "contractor": "N/A",
                "author": "N/A",
                "language": "N/A",
                "encl": "No",
                "encl_name": "None",
                "confidence_score": 0.0,
            }
            target_path = self.file_organizer.organize(
                source_file=file_path,
                language="Duplicates",
                mode=self.mode,
                date_str=date_str,
            )
            final_path = target_path if self.mode == "move" else file_path
            if self.mode == "move" and target_path != file_path:
                self.state_tracker.delete_state(str(file_path))
            self.state_tracker.save_state(str(final_path), file_hash, "COMPLETED", is_duplicate=True, metadata=dup_meta)
            return {
                "file_name": target_path.name,
                "file_path": str(final_path),
                "is_duplicate": True,
                "metadata": dup_meta,
                "date_str": date_str,
            }

        # 3. Đọc tài liệu (Hỗ trợ PDF số, PDF scan bằng PaddleOCR, DOCX, DOC, TXT)
        try:
            doc_data = DocumentLoader.load_document(file_path)
            raw_text = doc_data.get("text", "")
        except Exception as e:
            logger.error(f"Lỗi khi nạp file {file_path.name}: {e}")
            self.state_tracker.save_state(str(file_path), file_hash, "ERROR")
            return None

        # 4. Lọc gần trùng lặp (Tầng 2 - Semantic MinHash)
        is_semantic_dup, matching_ids = self.semantic_dedup.check_and_add(str(file_path), raw_text)
        if is_semantic_dup:
            logger.warning(f"-> Gần trùng lặp nội dung với: {matching_ids[0]}")

        # 5. Trích xuất metadata có cấu trúc (Cơ chế Lai 2 Tầng - Two-Tier Hybrid Architecture)
        metadata = None
        page_images = doc_data.get("page_images", [])
        is_scanned = bool(doc_data.get("is_scanned", False))
        has_digital_content = "(Digital)" in raw_text or (len(raw_text.strip()) >= 250 and not is_scanned)
        is_pure_scan = is_scanned and "(Digital)" not in raw_text

        # TẦNG 1: Đối với tài liệu số (Digital) hoặc tài liệu lai (Hybrid):
        # Ưu tiên chạy Text LLM siêu tốc trên toàn bộ Unified Text Stream
        if has_digital_content or not is_pure_scan:
            try:
                logger.info(f"-> [HYBRID TEXT - Tầng 1] Trích xuất siêu tốc qua Qwen-2.5:3b từ văn bản số: {file_path.name}")
                metadata = self.llm_extractor.extract(raw_text)
            except Exception as llm_err:
                logger.warning(f"Text LLM gặp sự cố ({llm_err}), chuyển hướng sang VLM...")
                metadata = None

        # TẦNG 2: Thẩm định thị giác có mục tiêu (Targeted VLM)
        # Kích hoạt khi:
        # A) Tài liệu là bản scan thuần (100% scan không có text số).
        # B) Hoặc tài liệu lai (Hybrid có bìa scan/con dấu) nhưng Tầng 1 chưa xác định rõ Đơn vị ban hành (Originator).
        needs_vlm_verification = (
            is_pure_scan
            or metadata is None
            or (is_scanned and page_images and (not metadata.originator or metadata.originator in {"N/A", "None", "Chưa rõ", "Unknown"}))
        )

        if needs_vlm_verification and page_images and settings.ENABLE_HYBRID_VLM:
            try:
                if is_pure_scan or metadata is None:
                    logger.info(f"-> [HYBRID VLM - Toàn phần] Kích hoạt VLM {settings.VLM_MODEL} cho tài liệu scan: {file_path.name}")
                    metadata = self.vlm_extractor.extract_from_images(page_images)
                else:
                    # Thẩm định thị giác có mục tiêu trên trang bìa/con dấu (chỉ gửi 1-2 trang đầu)
                    logger.info(f"-> [HYBRID VLM - Tầng 2 Thẩm định] Thẩm định thị giác con dấu/bìa scan từ: {page_images[0].name}")
                    vlm_meta = self.vlm_extractor.extract_from_images([page_images[0]])
                    # Hợp nhất thông tin bổ sung từ con dấu/bìa vào kết quả của Tầng 1
                    if vlm_meta.originator and vlm_meta.originator not in {"N/A", "None", "Chưa rõ"}:
                        metadata.originator = vlm_meta.originator
                    if (not metadata.well_link or metadata.well_link in {"N/A", "None"}) and vlm_meta.well_link and vlm_meta.well_link != "N/A":
                        metadata.well_link = vlm_meta.well_link
                    if (not metadata.contract_name or metadata.contract_name in {"N/A", "None"}) and vlm_meta.contract_name and vlm_meta.contract_name != "N/A":
                        metadata.contract_name = vlm_meta.contract_name
            except Exception as vlm_err:
                logger.warning(f"VLM gặp sự cố ({vlm_err}), duy trì kết quả từ Text LLM.")

        # Fallback an toàn cuối cùng nếu cả 2 tầng đều chưa có metadata
        if metadata is None:
            metadata = self.llm_extractor.extract(raw_text)

        # Chuẩn hóa triệt để lỗi chính tả đặc thù scan mờ Dầu khí và nhận diện ngôn ngữ tự động
        metadata = clean_metadata_fields(metadata, raw_text=raw_text)

        # Đánh giá độ tin cậy động (Multi-Signal Confidence) và phát hiện các ô mờ/nhòe cần highlight
        from src.extraction.confidence_scorer import evaluate_metadata_confidence
        metadata = evaluate_metadata_confidence(
            metadata,
            raw_text=raw_text,
            is_scanned=bool(doc_data.get("is_scanned", False)),
            ocr_lines=doc_data.get("ocr_lines", []),
        )
        logger.info(
            f"-> [CONFIDENCE] Độ tin cậy tài liệu: {metadata.confidence_score:.1f}% | "
            f"Cần chú ý (Highlight): {metadata.low_confidence_fields or 'Không có'}"
        )

        meta_dict = metadata.model_dump()

        # 6. Phân cụm và tổ chức file vào thư mục đích theo Ngày & Ngôn ngữ: <output_dir>/<YYYYMMDD>/<LANGUAGE>/<file>
        target_path = self.file_organizer.organize(
            source_file=file_path,
            language=metadata.language,
            contract_name=metadata.contract_name,
            originator=metadata.originator,
            mode=self.mode,
            date_str=date_str,
        )

        final_path = target_path if self.mode == "move" else file_path

        # 7. Lưu checkpoint vào SQLite (nếu move, xóa file nguồn ở dropzone và lưu vị trí mới)
        if self.mode == "move" and target_path != file_path:
            self.state_tracker.delete_state(str(file_path))
            self.state_tracker.save_state(
                str(target_path), file_hash, "COMPLETED", is_duplicate=is_semantic_dup, metadata=meta_dict
            )
        else:
            self.state_tracker.save_state(
                str(final_path), file_hash, "COMPLETED", is_duplicate=is_semantic_dup, metadata=meta_dict
            )

        return {
            "file_name": target_path.name,
            "file_path": str(final_path),
            "is_duplicate": is_semantic_dup,
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
            res = self.process_single_file(file_path, date_str=date_str)
            if res is not None:
                processed_results.append(res)

        # Xuất báo cáo tổng kết Excel theo ngày
        saved_path = self.export_current_report(
            input_dir=input_dir,
            output_report=output_report,
            date_str=date_str
        )
        logger.info(f"✅ Hoàn tất đợt xử lý! Báo cáo Excel: {saved_path}")
        return saved_path

