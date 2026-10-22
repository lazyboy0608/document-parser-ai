import difflib
import html
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from src.utils.logger import logger


class DocumentDiffAnalyzer:
    """
    Bộ phân tích và đối chiếu sai khác nội dung giữa hai tài liệu trùng lặp (Semantic Diff Analyzer).
    Sinh báo cáo HTML trực quan 2 cột song song (Side-by-side) và tóm tắt sai khác bằng AI.
    """

    def __init__(self, llm_extractor: Optional[Any] = None):
        self.llm_extractor = llm_extractor

    @staticmethod
    def _split_into_paragraphs(text: str) -> List[str]:
        """Tách văn bản thành danh sách các đoạn có ý nghĩa, loại bỏ khoảng trắng rác."""
        lines = [line.strip() for line in text.splitlines()]
        paragraphs = []
        buf = []
        for line in lines:
            if not line:
                if buf:
                    paragraphs.append(" ".join(buf))
                    buf = []
            elif line.startswith("--- [") and line.endswith("] ---"):
                # Bỏ qua các nhãn phân trang kỹ thuật
                continue
            else:
                buf.append(line)
        if buf:
            paragraphs.append(" ".join(buf))
        return [p for p in paragraphs if p]

    def generate_ai_summary(
        self,
        added_chunks: List[str],
        deleted_chunks: List[str],
        sim_pct: float,
        orig_name: str,
        dup_name: str
    ) -> str:
        """Sử dụng LLM để tóm tắt điểm sai khác cốt lõi trong 1-2 câu súc tích."""
        added_text = " ; ".join(added_chunks[:5])
        deleted_text = " ; ".join(deleted_chunks[:5])

        if not added_text and not deleted_text:
            return f"Hai tài liệu '{dup_name}' và '{orig_name}' hoàn toàn đồng nhất về mặt nội dung (100% khớp)."

        # Nếu có LLM Extractor, gọi LLM sinh tóm tắt thông minh
        if self.llm_extractor and hasattr(self.llm_extractor, "extract_raw"):
            try:
                prompt = (
                    "Bạn là chuyên gia phân tích và thẩm định tài liệu kỹ thuật ngành Dầu khí.\n"
                    f"Dưới đây là các phần văn bản sai khác giữa bản gốc '{orig_name}' và bản phái sinh/nháp '{dup_name}':\n"
                    f"- Đoạn bị xóa/thay thế ở bản gốc: {deleted_text[:1200] or 'Không có'}\n"
                    f"- Đoạn được thêm mới ở bản nháp: {added_text[:1200] or 'Không có'}\n\n"
                    "Yêu cầu: Hãy tóm tắt ngắn gọn trong ĐÚNG 1 ĐẾN 2 CÂU tiếng Việt điểm khác biệt cốt lõi nhất "
                    "(nêu rõ bản nháp thêm/bớt điều gì quan trọng, các phần còn lại có giữ nguyên không). Không dài dòng."
                )
                summary = self.llm_extractor.extract_raw(prompt).strip()
                if summary and len(summary) >= 15:
                    return summary
            except Exception as e:
                logger.debug(f"Không thể gọi LLM tóm tắt diff: {e}")

        # Fallback tự động nếu không gọi LLM: Sinh tóm tắt mẫu chuẩn xác định lượng
        parts = []
        if added_text:
            snippet = added_chunks[0][:80] + ("..." if len(added_chunks[0]) > 80 else "")
            parts.append(f"bổ sung nội dung mới (ví dụ: '{snippet}')")
        if deleted_text:
            snippet = deleted_chunks[0][:80] + ("..." if len(deleted_chunks[0]) > 80 else "")
            parts.append(f"lược bỏ một số đoạn từ bản gốc (ví dụ: '{snippet}')")

        diff_desc = " và ".join(parts) if parts else "có sự điều chỉnh nhỏ về cách dùng từ"
        return (
            f"Bản nháp giữ nguyên cấu trúc chung ({sim_pct:.1f}% tương đồng so với bản gốc '{orig_name}'), "
            f"chủ yếu {diff_desc}."
        )

    def analyze_diff(
        self,
        orig_text: str,
        dup_text: str,
        orig_name: str,
        dup_name: str,
        sim_pct: float,
    ) -> Dict[str, Any]:
        """
        So sánh sai khác nội dung giữa hai tài liệu và trích xuất tóm tắt AI cùng các đoạn thêm/bớt.
        """
        orig_paras = self._split_into_paragraphs(orig_text)
        dup_paras = self._split_into_paragraphs(dup_text)

        matcher = difflib.SequenceMatcher(None, orig_paras, dup_paras)

        added_chunks = []
        deleted_chunks = []

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "replace":
                for idx_orig in range(i1, i2):
                    deleted_chunks.append(orig_paras[idx_orig])
                for idx_dup in range(j1, j2):
                    added_chunks.append(dup_paras[idx_dup])

            elif tag == "delete":
                for idx_orig in range(i1, i2):
                    deleted_chunks.append(orig_paras[idx_orig])

            elif tag == "insert":
                for idx_dup in range(j1, j2):
                    added_chunks.append(dup_paras[idx_dup])

        # Ưu tiên trích đoạn có chứa từ ngữ thực sự mới lạ (không xuất hiện trong văn bản gốc)
        orig_lower = orig_text.lower()
        def _novelty_key(chunk: str) -> int:
            novel_words = [w for w in chunk.split() if len(w) >= 4 and w.lower() not in orig_lower]
            return len(novel_words)

        sorted_added = sorted(added_chunks, key=_novelty_key, reverse=True)
        top_added = sorted_added[:3] if sorted_added else []

        dup_lower = dup_text.lower()
        def _del_novelty_key(chunk: str) -> int:
            deleted_words = [w for w in chunk.split() if len(w) >= 4 and w.lower() not in dup_lower]
            return len(deleted_words)

        sorted_deleted = sorted(deleted_chunks, key=_del_novelty_key, reverse=True)
        top_deleted = sorted_deleted[:3] if sorted_deleted else []

        # Tạo tóm tắt AI
        ai_summary = self.generate_ai_summary(
            added_chunks=sorted_added,
            deleted_chunks=sorted_deleted,
            sim_pct=sim_pct,
            orig_name=orig_name,
            dup_name=dup_name
        )

        return {
            "duplicate_file": dup_name,
            "original_file": orig_name,
            "similarity_pct": sim_pct,
            "summary": ai_summary,
            "added_count": len(added_chunks),
            "deleted_count": len(deleted_chunks),
            "added_snippets": top_added,
            "deleted_snippets": top_deleted,
        }

    def analyze_and_generate_html(
        self,
        orig_text: str,
        dup_text: str,
        orig_name: str,
        dup_name: str,
        sim_pct: float,
        output_html_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Tương thích ngược: Phân tích diff không xuất file HTML."""
        return self.analyze_diff(
            orig_text=orig_text,
            dup_text=dup_text,
            orig_name=orig_name,
            dup_name=dup_name,
            sim_pct=sim_pct,
        )
