import sys
import unittest
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ingestion.pdf_parser import extract_text_from_pdf
from src.extraction.domain_cleaner import (
    is_missing_or_unknown_phrase,
    clean_metadata_fields,
)
from src.extraction.confidence_scorer import evaluate_metadata_confidence
from src.extraction.schemas import DocumentMetadata


class TestGroup1Fixes(unittest.TestCase):
    def test_toc_page_clean_digital(self):
        """Kiểm tra trang Mục lục có nhiều dấu chấm dẫn dòng không bị nhận diện nhầm thành trang Scan."""
        pdf_path = Path(r"D:\outputNLP\20261015\Seismic_Report\Báo cáo thực tập.pdf")
        if not pdf_path.exists():
            self.skipTest(f"Không tìm thấy tệp mẫu {pdf_path}")

        # Trích xuất văn bản từ tệp PDF mẫu có trang mục lục 1.500 dấu chấm
        text, has_scanned_page, page_images = extract_text_from_pdf(
            pdf_path,
            head_pages=5,
            tail_pages=1
        )

        # Trang 4 (Mục lục) giờ đây phải được nhận diện là Digital sạch
        # Do đó toàn bộ tài liệu số này KHÔNG ĐƯỢC có has_scanned_page = True
        self.assertFalse(
            has_scanned_page,
            "LỖI: Tệp PDF số có trang Mục lục vẫn bị nhận diện nhầm thành tài liệu Scan!"
        )
        self.assertIn("--- [Trang 4 (Digital)] ---", text)

    def test_missing_phrase_detection(self):
        """Kiểm tra hàm nhận diện câu báo thiếu thông tin đa ngôn ngữ."""
        missing_samples = [
            "Không có thông tin trong đoạn văn bản",
            "Không có thông tin trong tài liệu",
            "Không đề cập trong văn bản",
            "Không được đề cập",
            "Chưa có thông tin",
            "Không tìm thấy thông tin",
            "Chưa rõ",
            "chưa cung cấp",
            "Không rõ",
            "not mentioned in text",
            "no information provided",
            "unknown",
            "N/A",
            "None",
            "未提供具体合同名称",
            "未提及",
            "无",
        ]
        for s in missing_samples:
            self.assertTrue(
                is_missing_or_unknown_phrase(s),
                f"LỖI: Chưa nhận diện được câu báo thiếu: '{s}'"
            )

        valid_samples = [
            "Báo cáo kết quả nghiên cứu địa chất Lô 104",
            "PVD LOGGING COMPANY",
            "ThS. Phan Hữu Trung",
            "20005XP-BK20",
            "BLOCKS 15-1 & 15-2",
            "19/10/2026",
            "May 1997",
        ]
        for s in valid_samples:
            self.assertFalse(
                is_missing_or_unknown_phrase(s),
                f"LỖI: Nhận diện nhầm dữ liệu hợp lệ thành câu báo thiếu: '{s}'"
            )

    def test_clean_metadata_fields_normalization(self):
        """Kiểm tra clean_metadata_fields chuẩn hóa các câu văn xuôi báo thiếu về chuẩn N/A / Chưa rõ."""
        meta = DocumentMetadata(
            title="Không có thông tin trong đoạn văn bản",
            public_date="Không có thông tin trong tài liệu",
            contract_name="Chưa có thông tin",
            well_link="H VIỆN",  # Ảo giác phi dầu khí
            originator="Không đề cập trong văn bản",
            contractor="Viện Dầu khí Việt Nam",
            author="Không có thông tin",
            language="Vietnamese",
            document_type="Seismic_Report",
        )

        cleaned = clean_metadata_fields(meta, raw_text="Viện Dầu khí Việt Nam. Giảng viên ThS. Phan Hữu Trung")

        # Tiêu đề và ngày tháng thiếu -> 'Chưa rõ'
        self.assertEqual(cleaned.title, "Chưa rõ")
        self.assertEqual(cleaned.public_date, "Chưa rõ")

        # Tác giả thiếu -> 'None'
        self.assertEqual(cleaned.author, "None")

        # Hợp đồng và đơn vị thiếu -> 'N/A'
        self.assertEqual(cleaned.contract_name, "N/A")
        self.assertEqual(cleaned.originator, "N/A")

        # Giếng khoan ảo 'H VIỆN' không có mã giếng -> 'N/A'
        self.assertEqual(cleaned.well_link, "N/A")

        # Nhà thầu thật có trong văn bản -> Giữ nguyên
        self.assertEqual(cleaned.contractor, "Viện Dầu khí Việt Nam")

    def test_confidence_scorer_missing_penalty(self):
        """Kiểm tra bộ tính điểm tin cậy phạt điểm và highlight đúng khi thiếu Tiêu đề / Ngày tháng."""
        meta = DocumentMetadata(
            title="Không có thông tin trong đoạn văn bản",  # Hoặc "Chưa rõ"
            public_date="Chưa rõ",
            contract_name="N/A",
            well_link="N/A",
            originator="Nguyễn Hoàng Việt",
            contractor="Viện Dầu khí Việt Nam",
            author="Nguyễn Hoàng Việt",
            language="Vietnamese",
            document_type="Seismic_Report",
        )

        evaluated = evaluate_metadata_confidence(
            meta,
            raw_text="Nguyễn Hoàng Việt sinh viên thực tập tại Viện Dầu khí Việt Nam.",
            is_scanned=False,
        )

        # Vì thiếu Tiêu đề và Ngày tháng, cả 2 trường phải bị phạt xuống 45% và bị đưa vào highlight
        self.assertEqual(evaluated.field_confidences["title"], 45.0)
        self.assertEqual(evaluated.field_confidences["public_date"], 45.0)
        self.assertIn("title", evaluated.low_confidence_fields)
        self.assertIn("public_date", evaluated.low_confidence_fields)

        # Tổng điểm tin cậy phải giảm mạnh từ 97.2% xuống xấp xỉ ~81%
        self.assertLess(
            evaluated.confidence_score,
            85.0,
            f"LỖI: Thiếu Tiêu đề và Ngày tháng mà điểm vẫn cao: {evaluated.confidence_score}%"
        )


if __name__ == "__main__":
    unittest.main()
