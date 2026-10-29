from pathlib import Path
from typing import Dict, Any, Optional

from src.utils.logger import logger
from config.settings import settings
from src.extraction.llm_extractor import LLMExtractor
from src.extraction.vlm_extractor import VLMExtractor
from src.extraction.domain_cleaner import clean_metadata_fields
from src.extraction.confidence_scorer import evaluate_metadata_confidence
from src.extraction.schemas import DocumentMetadata


class AIExtractionPipeline:
    """
    TẦNG 2: AI EXTRACTION PIPELINE CHUYÊN SÂU.
    - NGUYÊN TẮC: CHỈ TIẾP NHẬN VÀ XỬ LÝ CÁC TỆP MỚI TINH (Non-duplicate).
    - Tận dụng cơ chế Lai 2 Tầng (Two-Tier Hybrid Architecture):
      + Tầng 1: Text LLM (Qwen-2.5:3b) siêu tốc trên Unified Text Stream.
      + Tầng 2: Targeted VLM (Qwen2.5-VL) thẩm định bìa scan, con dấu khi cần thiết.
    - Chuẩn hóa lỗi chính tả Dầu khí (domain_cleaner) và chấm điểm tin cậy đa tín hiệu (confidence_scorer).
    """

    def __init__(
        self,
        llm_extractor: Optional[LLMExtractor] = None,
        vlm_extractor: Optional[VLMExtractor] = None,
    ):
        self.llm_extractor = llm_extractor or LLMExtractor()
        self.vlm_extractor = vlm_extractor or VLMExtractor()

    def process(self, file_path: Path, doc_data: Dict[str, Any]) -> DocumentMetadata:
        """
        Trích xuất và chuẩn hóa metadata bằng AI cho tệp tài liệu mới.
        """
        raw_text = doc_data.get("text", "")
        page_images = doc_data.get("page_images", [])
        is_scanned = bool(doc_data.get("is_scanned", False))
        has_digital_content = "(Digital)" in raw_text or (len(raw_text.strip()) >= 250 and not is_scanned)
        is_pure_scan = is_scanned and "(Digital)" not in raw_text

        metadata: Optional[DocumentMetadata] = None

        # 1. Trích xuất siêu tốc bằng Text LLM trên dòng văn bản đã qua OCR / số hóa
        if raw_text and raw_text.strip():
            try:
                logger.info(f"-> [HYBRID TEXT - Tầng 1] Trích xuất siêu tốc qua Text LLM: {file_path.name}")
                metadata = self.llm_extractor.extract(raw_text)
            except Exception as llm_err:
                logger.warning(f"Text LLM gặp sự cố ({llm_err}): {file_path.name}")
                metadata = None

        # 2. Thẩm định thị giác có mục tiêu (Targeted VLM)
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
                    # Thẩm định thị giác trên bìa/con dấu (1 trang đầu)
                    logger.info(f"-> [HYBRID VLM - Tầng 2 Thẩm định] Thẩm định con dấu/bìa scan từ: {page_images[0].name}")
                    vlm_meta = self.vlm_extractor.extract_from_images([page_images[0]])
                    if vlm_meta.originator and vlm_meta.originator not in {"N/A", "None", "Chưa rõ"}:
                        metadata.originator = vlm_meta.originator
                    if (not metadata.well_link or metadata.well_link in {"N/A", "None"}) and vlm_meta.well_link and vlm_meta.well_link != "N/A":
                        metadata.well_link = vlm_meta.well_link
                    if (not metadata.contract_name or metadata.contract_name in {"N/A", "None"}) and vlm_meta.contract_name and vlm_meta.contract_name != "N/A":
                        metadata.contract_name = vlm_meta.contract_name
            except Exception as vlm_err:
                logger.warning(f"VLM gặp sự cố ({vlm_err}), duy trì kết quả từ Text LLM.")

        # Fallback an toàn cuối cùng
        if metadata is None:
            metadata = self.llm_extractor.extract(raw_text)

        # 3. Chuẩn hóa lỗi chính tả chuyên ngành Dầu khí
        metadata = clean_metadata_fields(metadata, raw_text=raw_text)

        # 4. Đánh giá độ tin cậy động và nhận diện các ô cần highlight
        metadata = evaluate_metadata_confidence(
            metadata,
            raw_text=raw_text,
            is_scanned=is_scanned,
            ocr_lines=doc_data.get("ocr_lines", []),
        )
        logger.info(
            f"-> [CONFIDENCE] Độ tin cậy tài liệu: {metadata.confidence_score:.1f}% | "
            f"Cần chú ý (Highlight): {metadata.low_confidence_fields or 'Không có'}"
        )

        return metadata
