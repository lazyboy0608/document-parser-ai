import json
from typing import Dict, Any, Optional
import ollama

from config.settings import settings
from .schemas import DocumentMetadata
from .prompts import SYSTEM_PROMPT, EXTRACTION_USER_PROMPT
from .preprocessor import extract_metadata_context
from src.utils.logger import logger


class LLMExtractor:
    """
    Trích xuất metadata báo cáo dầu khí (10 trường) sử dụng Ollama + Qwen-2.5:3b.
    Hỗ trợ chế độ JSON Mode và xử lý đa ngữ Anh - Việt - Nga.
    """
    def __init__(self, model_name: str = settings.OLLAMA_MODEL, base_url: str = settings.OLLAMA_BASE_URL):
        self.model_name = model_name
        self.client = ollama.Client(host=base_url)

    def extract(self, raw_text: str) -> DocumentMetadata:
        """
        Trích xuất 10 trường metadata từ văn bản tài liệu thô.
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
                encl_name="None"
            )

        # Rút trích vùng ngữ cảnh 3-5 trang đầu (tối đa 6000 ký tự)
        context_text = extract_metadata_context(raw_text)
        user_content = EXTRACTION_USER_PROMPT.replace("{text_content}", context_text)

        try:
            response = self.client.chat(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content}
                ],
                format="json",
                options={"temperature": 0.1}
            )

            raw_response = response.get("message", {}).get("content", "{}")
            parsed_data = json.loads(raw_response)

            from .domain_cleaner import clean_metadata_fields

            # Chuẩn hóa về DocumentMetadata schema chuẩn 10 trường
            meta = DocumentMetadata(
                well_link=str(parsed_data.get("well_link", "N/A") or "N/A").strip(),
                contract_name=str(parsed_data.get("contract_name", "N/A") or "N/A").strip(),
                title=str(parsed_data.get("title", "Chưa rõ") or "Chưa rõ").strip(),
                public_date=str(parsed_data.get("public_date", "Chưa rõ") or "Chưa rõ").strip(),
                originator=str(parsed_data.get("originator", "Chưa rõ") or "Chưa rõ").strip(),
                contractor=str(parsed_data.get("contractor", "N/A") or "N/A").strip(),
                author=str(parsed_data.get("author", "Chưa rõ") or "Chưa rõ").strip(),
                language=str(parsed_data.get("language", "English") or "English").strip(),
                encl=str(parsed_data.get("encl", "No") or "No").strip(),
                encl_name=str(parsed_data.get("encl_name", "None") or "None").strip(),
            )
            return clean_metadata_fields(meta, raw_text=context_text)

        except Exception as e:
            logger.error(f"Lỗi khi trích xuất qua Ollama: {e}")
            return DocumentMetadata(
                title="Lỗi trích xuất LLM",
                well_link="N/A",
                contract_name="N/A",
                public_date="Chưa rõ",
                originator="Chưa rõ",
                contractor="N/A",
                author="Chưa rõ",
                language="English",
                encl="No",
                encl_name="None"
            )
