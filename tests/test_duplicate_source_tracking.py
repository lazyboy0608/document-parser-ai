import sys
import tempfile
import unittest
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.deduplication.exact_dedup import ExactDeduplicator
from src.deduplication.simhash_dedup import SimHashDeduplicator
from src.reporting.excel_exporter import ExcelExporter


class TestDuplicateSourceTracking(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_exact_duplicate_source_tracking(self):
        """Kiểm tra nhận diện đúng tên tệp gốc khi trùng lặp tuyệt đối (100% SHA-256)."""
        dedup = ExactDeduplicator()

        f1 = self.base_path / "original_document.pdf"
        f1.write_bytes(b"Exact binary content for oil and gas report 123456")

        f2 = self.base_path / "copy_document.pdf"
        f2.write_bytes(b"Exact binary content for oil and gas report 123456")

        # Đăng ký / kiểm tra file 1
        is_dup1, orig1 = dedup.find_duplicate(f1)
        self.assertFalse(is_dup1)
        self.assertIsNone(orig1)

        # Kiểm tra file 2 trùng file 1
        is_dup2, orig2 = dedup.find_duplicate(f2)
        self.assertTrue(is_dup2)
        self.assertIsNotNone(orig2)
        self.assertEqual(Path(orig2).name, "original_document.pdf")

    def test_near_duplicate_source_tracking(self):
        """Kiểm tra nhận diện đúng tên tệp gốc khi gần trùng lặp SimHash."""
        simhash_dedup = SimHashDeduplicator(threshold_bits=3)

        text_orig = "Báo cáo thực tập tại Viện Dầu khí Việt Nam VPI về nghiên cứu địa chất và giếng khoan. " * 20
        text_draft = text_orig + " ABC.ABCDEFGH"

        h_orig = simhash_dedup.compute_simhash(text_orig)
        h_draft = simhash_dedup.compute_simhash(text_draft)

        active_hashes = {
            str(self.base_path / "Báo cáo thực tập.pdf"): h_orig
        }

        is_dup, matched_file, dist = simhash_dedup.find_near_duplicate(h_draft, active_hashes)
        self.assertTrue(is_dup)
        self.assertIsNotNone(matched_file)
        self.assertEqual(Path(matched_file).name, "Báo cáo thực tập.pdf")

    def test_excel_export_duplicate_format_cleanliness(self):
        """Kiểm tra báo cáo Excel hiển thị đúng format, có tên tệp gốc và loại bỏ chuỗi kỹ thuật thuật toán."""
        test_data = [
            {
                "file_name": "Báo cáo thực tập-draft.docx",
                "file_path": str(self.base_path / "Trùng lặp" / "Báo cáo thực tập-draft.docx"),
                "is_duplicate": True,
                "duplicate_of": "Báo cáo thực tập.pdf",
                "metadata": {
                    "title": "Báo cáo thực tập VPI",
                    "duplicate_of": "Báo cáo thực tập.pdf"
                }
            },
            {
                "file_name": "exact_copy.pdf",
                "file_path": str(self.base_path / "Trùng lặp" / "exact_copy.pdf"),
                "is_duplicate": True,
                "duplicate_of": "sample.pdf",
                "metadata": {
                    "title": "Tài liệu trùng lặp",
                    "duplicate_type": "exact",
                    "duplicate_of": "sample.pdf"
                }
            },
            {
                "file_name": "Báo cáo thực tập.pdf",
                "file_path": str(self.base_path / "Báo cáo thực tập.pdf"),
                "is_duplicate": False,
                "metadata": {
                    "title": "Báo cáo thực tập VPI"
                }
            }
        ]

        out_excel = self.base_path / "test_report.xlsx"
        ExcelExporter.export(test_data, out_excel)
        self.assertTrue(out_excel.exists())

        import pandas as pd
        df = pd.read_excel(out_excel)

        # Kiểm tra hàng 1: Gần trùng lặp
        row1_status = str(df.loc[df["Tên tệp"] == "Báo cáo thực tập-draft.docx", "Trạng thái trùng lặp"].values[0])
        self.assertEqual(row1_status, "Gần trùng lặp (với Báo cáo thực tập.pdf)")
        self.assertNotIn("SimHash", row1_status)
        self.assertNotIn(">95%", row1_status)

        # Kiểm tra hàng 2: Trùng khớp 100%
        row2_status = str(df.loc[df["Tên tệp"] == "exact_copy.pdf", "Trạng thái trùng lặp"].values[0])
        self.assertEqual(row2_status, "Trùng khớp 100% (với sample.pdf)")
        self.assertNotIn("SHA-256", row2_status)

        # Kiểm tra hàng 3: Gốc không trùng
        row3_status = str(df.loc[df["Tên tệp"] == "Báo cáo thực tập.pdf", "Trạng thái trùng lặp"].values[0])
        self.assertEqual(row3_status, "Gốc (Không trùng)")


if __name__ == "__main__":
    unittest.main()
