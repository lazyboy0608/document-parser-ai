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
        self.exact_dedup = ExactDeduplicator()
        # Nạp danh sách mã băm SHA-256 đã xử lý từ SQLite để nhận diện trùng lặp xuyên phiên
        self.exact_dedup.seen_hashes.update(self.state_tracker.get_all_hashes())
        self.semantic_dedup = SemanticDeduplicator(threshold=settings.SIMILARITY_THRESHOLD)
        self.llm_extractor = LLMExtractor()
        self.vlm_extractor = VLMExtractor()
        self.file_organizer = FileOrganizer(self.output_dir)

    def process_single_file(self, file_path: Path) -> Optional[Dict[str, Any]]:
        """
        Xử lý đơn lẻ 1 tệp tài liệu (phục vụ cả quét lô hàng loạt lẫn cơ chế Hot Folder Watcher).
        """
        file_path = file_path.resolve()
        if not file_path.exists() or not file_path.is_file():
            return None

        if file_path.suffix.lower() not in DocumentLoader.SUPPORTED_EXTENSIONS:
            return None

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
                        mode=self.mode
                    )
                    final_path = target_path if self.mode == "move" else file_path
                    item_res = dict(cached_item)
                    item_res["file_path"] = str(final_path)
                    item_res["file_name"] = target_path.name
                    if self.mode == "move" and target_path != file_path:
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
                    self.state_tracker.save_state(
                        str(file_path),
                        file_hash,
                        "COMPLETED",
                        is_duplicate=cached_item.get("is_duplicate", False),
                        metadata=updated_dict,
                    )
                    cached_item["metadata"] = updated_dict
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
                "encl_name": "None"
            }
            self.state_tracker.save_state(str(file_path), file_hash, "COMPLETED", is_duplicate=True, metadata=dup_meta)
            return {
                "file_name": file_path.name,
                "file_path": str(file_path),
                "is_duplicate": True,
                "metadata": dup_meta
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

        # 5. Trích xuất metadata có cấu trúc (Cơ chế Hybrid VLM + LLM)
        metadata = None
        page_images = doc_data.get("page_images", [])

        # Nếu là tài liệu scan và có ảnh trang bìa/phê duyệt -> Ưu tiên kích hoạt VLM
        if doc_data.get("is_scanned") and page_images and settings.ENABLE_HYBRID_VLM:
            try:
                logger.info(f"-> [HYBRID VLM] Kích hoạt mô hình thị giác {settings.VLM_MODEL} cho tài liệu scan: {file_path.name}")
                metadata = self.vlm_extractor.extract_from_images(page_images)
            except Exception as vlm_err:
                logger.warning(f"VLM gặp sự cố ({vlm_err}), tự động chuyển về nhánh Text + LLM...")
                metadata = None

        # Nhánh Text + Qwen-2.5:3b (dành cho file Word, PDF số, hoặc fallback khi VLM không chạy)
        if metadata is None:
            logger.info(f"-> [HYBRID TEXT] Đang trích xuất qua Qwen-2.5:3b...")
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

        # 6. Lưu checkpoint vào SQLite
        meta_dict = metadata.model_dump()
        self.state_tracker.save_state(str(file_path), file_hash, "COMPLETED", is_duplicate=is_semantic_dup, metadata=meta_dict)

        # 7. Phân cụm và tổ chức file vào thư mục đích theo Ngôn ngữ: <output_dir>/<LANGUAGE>/<file>
        target_path = self.file_organizer.organize(
            source_file=file_path,
            language=metadata.language,
            contract_name=metadata.contract_name,
            originator=metadata.originator,
            mode=self.mode
        )

        # Nếu ở chế độ move, cập nhật lại đường dẫn tệp mới trong SQLite checkpoint
        if self.mode == "move" and target_path != file_path:
            self.state_tracker.save_state(
                str(target_path), file_hash, "COMPLETED", is_duplicate=is_semantic_dup, metadata=meta_dict
            )

        final_path = target_path if self.mode == "move" else file_path

        return {
            "file_name": target_path.name,
            "file_path": str(final_path),
            "is_duplicate": is_semantic_dup,
            "metadata": meta_dict
        }

    def export_current_report(
        self,
        input_dir: Path = settings.INPUT_DIR,
        output_report: Path = settings.REPORTS_DIR / "tong_hop_tai_lieu.xlsx"
    ) -> Path:
        """
        Tổng hợp toàn bộ tài liệu đã hoàn tất trong input_dir và output_dir (từ SQLite state) và cập nhật ra file Excel.
        Hỗ trợ lưu trực tiếp tại thư mục mạng nội bộ (D:\\outputNLP hoặc D:\\testNLP) và sao lưu tại data\\reports.
        """
        input_dir = Path(input_dir).resolve()
        output_report = Path(output_report).resolve()
        completed_all = self.state_tracker.get_all_completed()

        # Quét các tệp tài liệu thực tế hiện diện trong output_dir và input_dir
        tracked_paths = set()
        tracked_names = set()
        for folder in [self.output_dir, input_dir]:
            if folder.exists():
                for f in folder.rglob("*"):
                    if (
                        f.is_file()
                        and f.suffix.lower() in DocumentLoader.SUPPORTED_EXTENSIONS
                        and not f.name.startswith("~$")
                        and not f.name.startswith(".")
                        and f.suffix.lower() != ".xlsx"
                    ):
                        tracked_paths.add(str(f.resolve()))
                        tracked_names.add(f.name.lower())

        if tracked_paths:
            results = []
            seen_names = set()
            for path_str, item in completed_all.items():
                p = Path(path_str).resolve()
                p_name_lower = p.name.lower()
                # Khớp theo đường dẫn tuyệt đối hoặc tên tệp đã được di chuyển sang output_dir
                if str(p) in tracked_paths or p_name_lower in tracked_names:
                    if p_name_lower not in seen_names:
                        seen_names.add(p_name_lower)
                        target_candidates = [
                            tp for tp in tracked_paths if Path(tp).name.lower() == p_name_lower
                        ]
                        if target_candidates:
                            item_copy = dict(item)
                            item_copy["file_path"] = target_candidates[0]
                            results.append(item_copy)
                        else:
                            results.append(item)
        else:
            results = [
                item for path_str, item in completed_all.items()
                if Path(path_str).exists()
            ]

        saved_path = ExcelExporter.export(results, output_report)
        logger.info(f"📊 Đã cập nhật báo cáo Excel ({len(results)} tài liệu) tại: {saved_path}")

        # Đồng thời sao lưu 1 bản vào data/reports/tong_hop_tai_lieu.xlsx nếu output_report là thư mục mạng
        backup_dir = settings.REPORTS_DIR
        backup_report = (backup_dir / "tong_hop_tai_lieu.xlsx").resolve()
        if output_report != backup_report:
            try:
                ExcelExporter.export(results, backup_report)
            except Exception as e:
                logger.warning(f"Không thể cập nhật bản sao lưu tại {backup_report}: {e}")

        return saved_path


    def run(
        self,
        input_dir: Path = settings.INPUT_DIR,
        output_report: Path = settings.REPORTS_DIR / "tong_hop_tai_lieu.xlsx"
    ) -> Path:
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
            return output_report

        processed_results: List[Dict[str, Any]] = []

        for idx, file_path in enumerate(all_files, 1):
            logger.info(f"[{idx}/{total_files}] Đang xử lý: {file_path.name}")
            res = self.process_single_file(file_path)
            if res is not None:
                processed_results.append(res)

        # Xuất báo cáo tổng kết Excel
        saved_path = ExcelExporter.export(processed_results, output_report)
        logger.info(f"✅ Hoàn tất! Báo cáo Excel đã được lưu tại: {saved_path}")
        return saved_path

