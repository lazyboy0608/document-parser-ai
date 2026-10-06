import json
import re
from typing import Dict, Any, Optional
import ollama

from config.settings import settings
from .schemas import DocumentMetadata
from .prompts import (
    IDENTITY_SYSTEM_PROMPT,
    IDENTITY_USER_PROMPT,
    ENTITIES_SYSTEM_PROMPT,
    ENTITIES_USER_PROMPT,
    SYSTEM_PROMPT,
    EXTRACTION_USER_PROMPT,
)
from .preprocessor import extract_metadata_context
from .domain_cleaner import clean_metadata_fields, extract_enclosures_from_raw_text
from src.utils.logger import logger


class LLMExtractor:
    """
    Trích xuất metadata báo cáo dầu khí sử dụng kiến trúc Tách Cụm Ngữ Nghĩa (Targeted Clustered Extraction):
    1. Cụm 1 (Định danh & Thời gian): well_link, contract_name, title, public_date
    2. Cụm 2 (Chủ thể Pháp nhân & Tác giả): originator, contractor, author
    3. Cụm 3 (Python Engine): encl, encl_name, language (chạy siêu tốc trong 0.01s)
    4. Hậu kiểm chéo ngữ nghĩa (Semantic Cross-Validation) chống nhầm lẫn cột.
    """
    def __init__(self, model_name: str = settings.OLLAMA_MODEL, base_url: str = settings.OLLAMA_BASE_URL):
        self.model_name = model_name
        self.client = ollama.Client(host=base_url)

    def _extract_identity(self, context_text: str) -> Dict[str, str]:
        """
        Trích xuất Cụm 1: Định danh tài liệu và Mốc thời gian (Title, Well, Contract, Date).
        """
        user_content = IDENTITY_USER_PROMPT.replace("{text_content}", context_text)
        try:
            response = self.client.chat(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": IDENTITY_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                format="json",
                options={
                    "temperature": 0.0,
                    "num_thread": getattr(settings, "OLLAMA_NUM_THREAD", 4),
                    "num_ctx": getattr(settings, "OLLAMA_NUM_CTX", 2048),
                    "num_predict": getattr(settings, "OLLAMA_NUM_PREDICT", 256),
                },
            )
            raw = response.get("message", {}).get("content", "{}")
            return json.loads(raw)
        except Exception as e:
            logger.warning(f"Lỗi khi trích xuất Cụm 1 (Identity): {e}")
            return {}

    def _extract_entities(self, context_text: str) -> Dict[str, str]:
        """
        Trích xuất Cụm 2: Chủ thể Pháp nhân và Con người (Originator, Contractor, Author).
        """
        user_content = ENTITIES_USER_PROMPT.replace("{text_content}", context_text)
        try:
            response = self.client.chat(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": ENTITIES_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                format="json",
                options={
                    "temperature": 0.0,
                    "num_thread": getattr(settings, "OLLAMA_NUM_THREAD", 4),
                    "num_ctx": getattr(settings, "OLLAMA_NUM_CTX", 2048),
                    "num_predict": getattr(settings, "OLLAMA_NUM_PREDICT", 256),
                },
            )
            raw = response.get("message", {}).get("content", "{}")
            return json.loads(raw)
        except Exception as e:
            logger.warning(f"Lỗi khi trích xuất Cụm 2 (Entities): {e}")
            return {}

    @staticmethod
    def _reconcile_and_validate(meta_dict: Dict[str, Any]) -> Dict[str, Any]:
        """
        Bộ quy tắc Hậu kiểm Chéo Ngữ nghĩa (Semantic Cross-Validation):
        Tự động phát hiện và khắc phục các trường hợp nhầm lẫn cột phổ biến.
        """
        well = str(meta_dict.get("well_link", "N/A") or "N/A").strip()
        contract = str(meta_dict.get("contract_name", "N/A") or "N/A").strip()
        author = str(meta_dict.get("author", "None") or "None").strip()
        originator = str(meta_dict.get("originator", "None") or "None").strip()

        # 1. Phát hiện nếu well_link chứa mã Lô/Hợp đồng (BLOCK, LÔ, PSC, BASIN)
        well_upper = well.upper()
        if any(kw in well_upper for kw in ["BLOCK", "BLOCKS", "LÔ", "LO ", "PSC", "BASIN"]):
            if contract in {"N/A", "None", "Chưa rõ", "Unknown", ""}:
                # Chuyển well_link sang contract_name
                meta_dict["contract_name"] = well
                meta_dict["well_link"] = "N/A"

        # 2. Phát hiện nếu Tác giả (author) bị ghi nhầm tên Công ty/Tổ chức
        author_upper = author.upper()
        corp_indicators = [
            "LTD", "LIMITED", "CORP", "CORPORATION", "INC", "INCORPORATED",
            "JOC", "JV", "COMPANY", "CO.", "CÔNG TY", "TỔNG CÔNG TY", "XÍ NGHIỆP",
            "VIETSOVPETRO", "PETROVIETNAM", "ZARUBEZHNEFT", "HALLIBURTON", "SCHLUMBERGER", "SANTOS"
        ]
        if any(ind in author_upper for ind in corp_indicators):
            if originator in {"N/A", "None", "Chưa rõ", "Unknown", ""}:
                meta_dict["originator"] = author
            meta_dict["author"] = "None"

        return meta_dict

    def extract(self, raw_text: str) -> DocumentMetadata:
        """
        Trích xuất hoàn chỉnh 10 trường metadata theo kiến trúc tách cụm.
        """
        if not raw_text or not raw_text.strip():
            return DocumentMetadata(
                title="Tài liệu rỗng / Không đọc được",
                well_link="N/A",
                contract_name="N/A",
                public_date="Chưa rõ",
                originator="Chưa rõ",
                contractor="N/A",
                author="Chưa rõ",
                language="English",
                encl="No",
                encl_name="None",
            )

        # Rút trích vùng ngữ cảnh đầu và đuôi tài liệu (tối ưu token trên CPU Core i3)
        context_text = extract_metadata_context(raw_text)

        # 1. Chạy Cụm 1: Định danh & Mốc thời gian
        logger.info("-> [CLUSTER 1] Đang trích xuất Định danh & Thời gian (Title, Well, Contract, Date)...")
        id_data = self._extract_identity(context_text)

        # 2. Chạy Cụm 2: Chủ thể Pháp nhân & Tác giả
        logger.info("-> [CLUSTER 2] Đang trích xuất Chủ thể & Tác giả (Originator, Contractor, Author)...")
        ent_data = self._extract_entities(context_text)

        # 3. Cụm 3: Phụ bản (Trích xuất nhanh qua Regex đa ngôn ngữ)
        encl_count, encl_names = extract_enclosures_from_raw_text(raw_text)

        # Hợp nhất dữ liệu thô
        merged: Dict[str, Any] = {
            "well_link": str(id_data.get("well_link", "N/A") or "N/A").strip(),
            "contract_name": str(id_data.get("contract_name", "N/A") or "N/A").strip(),
            "title": str(id_data.get("title", "Chưa rõ") or "Chưa rõ").strip(),
            "public_date": str(id_data.get("public_date", "Chưa rõ") or "Chưa rõ").strip(),
            "originator": str(ent_data.get("originator", "Chưa rõ") or "Chưa rõ").strip(),
            "contractor": str(ent_data.get("contractor", "N/A") or "N/A").strip(),
            "author": str(ent_data.get("author", "None") or "None").strip(),
            "language": "English",  # Sẽ được chuẩn hóa tự động ở clean_metadata_fields
            "encl": encl_count,
            "encl_name": encl_names,
        }

        # 4. Hậu kiểm chéo ngữ nghĩa
        merged = self._reconcile_and_validate(merged)

        # 5. Làm sạch miền nghiệp vụ dầu khí & chuẩn hóa ngôn ngữ ISO 639
        meta = DocumentMetadata(**merged)
        return clean_metadata_fields(meta, raw_text=raw_text)
