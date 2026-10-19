import sys
import tempfile
import unittest
import gc
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils.state_tracker import StateTracker
from src.clustering.file_organizer import FileOrganizer
from src.reporting.excel_exporter import ExcelExporter


class TestLANFaultTolerance(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.db_path = self.base_path / "test_state.db"
        self.state_tracker = StateTracker(db_path=self.db_path)

    def tearDown(self):
        del self.state_tracker
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass

    def test_volume_accessibility_guard(self):
        """Test that missing files on an unreachable drive/path are NOT deleted from SQLite."""
        # 1. Existing local file
        real_file = self.base_path / "real_doc.pdf"
        real_file.write_text("dummy content")
        self.state_tracker.save_state(str(real_file), "hash_real", "COMPLETED")

        # 2. Simulated disconnected network drive (e.g. Z:\non_existent_mount\file.pdf)
        fake_lan_file = Path("Z:/network_share/disconnected_drive/doc_lan.pdf")
        self.state_tracker.save_state(str(fake_lan_file), "hash_lan", "COMPLETED", simhash="simhash_123")

        # 3. Simulate deleted file whose parent DOES exist
        del_file = self.base_path / "intentionally_deleted.pdf"
        self.state_tracker.save_state(str(del_file), "hash_del", "COMPLETED")

        # Verify initial states
        self.assertTrue(self.state_tracker.is_processed(str(real_file)))
        self.assertTrue(self.state_tracker.is_processed(str(fake_lan_file)))
        self.assertTrue(self.state_tracker.is_processed(str(del_file)))

        # Run cleanup
        removed_count = self.state_tracker.cleanup_missing_files()

        # Exactly 1 file should be removed (del_file) because its parent exists
        self.assertEqual(removed_count, 1)
        self.assertFalse(self.state_tracker.is_processed(str(del_file)))

        # real_file should still exist
        self.assertTrue(self.state_tracker.is_processed(str(real_file)))

        # CRITICAL TEST: fake_lan_file MUST NOT be removed because Z:/ is inaccessible
        self.assertTrue(self.state_tracker.is_processed(str(fake_lan_file)))

        # Active hashes should also preserve fake_lan_file
        active_hashes = self.state_tracker.get_active_hashes()
        self.assertIn("hash_lan", active_hashes)

    def test_two_phase_safe_transfer(self):
        """Test that FileOrganizer safely moves files and handles errors gracefully."""
        src_dir = self.base_path / "input"
        dst_dir = self.base_path / "output"
        src_dir.mkdir()
        dst_dir.mkdir()

        test_file = src_dir / "sample_doc.pdf"
        test_file.write_bytes(b"Header and Body content of PDF")

        organizer = FileOrganizer(base_output_dir=dst_dir)
        target = organizer.organize(
            source_file=test_file,
            document_type="Contract",
            language="Vietnamese",
            mode="move",
            date_str="20261019"
        )

        self.assertIsNotNone(target)
        self.assertTrue(target.exists())
        self.assertFalse(test_file.exists())  # Source deleted in move mode
        # Verify no .nlp_tmp left behind
        tmp_files = list(dst_dir.rglob("*.nlp_tmp"))
        self.assertEqual(len(tmp_files), 0)

    def test_excel_exporter_local_first_staging(self):
        """Test that ExcelExporter writes to local staging and deploys atomically."""
        out_excel = self.base_path / "reports" / "20261019" / "tong_hop_tai_lieu_20261019.xlsx"
        dummy_data = [
            {
                "file_name": "test_doc.pdf",
                "file_path": str(self.base_path / "test_doc.pdf"),
                "is_duplicate": False,
                "metadata": {
                    "title": "Báo cáo thử nghiệm an toàn LAN",
                    "contract_name": "Block 05-1",
                    "well_link": "N/A",
                    "public_date": "19/10/2026",
                    "originator": "PVEP",
                    "contractor": "Vietsovpetro",
                    "author": "Nguyen Van A",
                    "language": "Vietnamese",
                    "document_type": "Daily_Report",
                    "confidence_score": 95.5,
                    "low_confidence_fields": [],
                }
            }
        ]

        result_path = ExcelExporter.export(dummy_data, out_excel)
        self.assertTrue(result_path.exists())
        self.assertGreater(result_path.stat().st_size, 0)
        # Verify no .nlp_tmp remains
        tmp_files = list(out_excel.parent.glob("*.nlp_tmp"))
        self.assertEqual(len(tmp_files), 0)


if __name__ == "__main__":
    unittest.main()
