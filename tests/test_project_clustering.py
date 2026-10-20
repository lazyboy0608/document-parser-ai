import sys
import tempfile
import unittest
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.clustering.file_organizer import FileOrganizer, resolve_cluster_folder_name


class TestProjectClustering(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.output_dir = self.base_path / "output_test"
        self.output_dir.mkdir(parents=True)
        self.organizer = FileOrganizer(base_output_dir=self.output_dir)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_resolve_cluster_folder_name_priorities(self):
        """Kiểm tra độ ưu tiên phân cụm thư mục."""
        # 1. Trùng lặp luôn ưu tiên số 1
        name = resolve_cluster_folder_name(
            is_duplicate=True,
            contract_name="BLOCKS 15-1 & 15-2",
            well_link="15-1-RD-1X",
            originator="PVEP"
        )
        self.assertEqual(name, "Trùng lặp")

        # 2. Hợp đồng / Lô mỏ ưu tiên số 2 (khi không trùng)
        name = resolve_cluster_folder_name(
            is_duplicate=False,
            contract_name="BLOCKS 15-1 & 15-2",
            well_link="15-1-RD-1X",
            originator="PVEP"
        )
        self.assertEqual(name, "BLOCKS_15-1_&_15-2")

        # 3. Giếng khoan ưu tiên số 3 (khi contract_name là N/A)
        name = resolve_cluster_folder_name(
            is_duplicate=False,
            contract_name="N/A",
            well_link="15-1-RD-1X",
            originator="PVEP"
        )
        self.assertEqual(name, "Well_15-1-RD-1X")

        # 4. Đơn vị ban hành ưu tiên số 4 (khi cả Block lẫn Giếng đều N/A)
        name = resolve_cluster_folder_name(
            is_duplicate=False,
            contract_name="N/A",
            well_link="N/A",
            originator="Vietsovpetro"
        )
        self.assertEqual(name, "Vietsovpetro")

        # 5. Thể loại tài liệu làm fallback
        name = resolve_cluster_folder_name(
            is_duplicate=False,
            contract_name="N/A",
            well_link="N/A",
            originator="N/A",
            document_type="Seismic_Report"
        )
        self.assertEqual(name, "Seismic_Report")

        # 6. Fallback cuối cùng nếu toàn bộ đều N/A/General
        name = resolve_cluster_folder_name(
            is_duplicate=False,
            contract_name="N/A",
            well_link="N/A",
            originator="N/A",
            document_type="General_Report"
        )
        self.assertEqual(name, "Tai_lieu_chung")

    def test_duplicate_routing_to_trung_lap_folder(self):
        """Kiểm tra cả file trùng SHA-256 và SimHash đều được di chuyển vào folder 'Trùng lặp'."""
        src_file = self.base_path / "dup_doc.pdf"
        src_file.write_bytes(b"Duplicate document content")

        target = self.organizer.organize(
            source_file=src_file,
            contract_name="BLOCKS 15-1",
            is_duplicate=True,
            mode="copy",
            date_str="20261020"
        )

        self.assertIsNotNone(target)
        self.assertTrue(target.exists())
        self.assertEqual(target.parent.name, "Trùng lặp")
        self.assertIn("Trùng lặp", str(target))

    def test_project_block_routing(self):
        """Kiểm tra tài liệu được gom theo tên Lô / Dự án."""
        src_file = self.base_path / "geo_report.pdf"
        src_file.write_bytes(b"Geological report for Block 05-1")

        target = self.organizer.organize(
            source_file=src_file,
            contract_name="LÔ 05-1",
            well_link="LK-104",
            is_duplicate=False,
            mode="copy",
            date_str="20261020"
        )

        self.assertIsNotNone(target)
        self.assertTrue(target.exists())
        self.assertEqual(target.parent.name, "LÔ_05-1")

    def test_well_routing_when_block_missing(self):
        """Kiểm tra tài liệu gom theo số hiệu Giếng khi không có thông tin Lô."""
        src_file = self.base_path / "well_log.pdf"
        src_file.write_bytes(b"Well logging data")

        target = self.organizer.organize(
            source_file=src_file,
            contract_name="N/A",
            well_link="20005XP-BK20",
            is_duplicate=False,
            mode="copy",
            date_str="20261020"
        )

        self.assertIsNotNone(target)
        self.assertTrue(target.exists())
        self.assertEqual(target.parent.name, "Well_20005XP-BK20")


if __name__ == "__main__":
    unittest.main()
