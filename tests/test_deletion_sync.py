import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from src.utils.state_tracker import StateTracker
from src.pipeline import DocumentPipeline
from src.ingestion.folder_watcher import HotFolderWatcher
from src.reporting.excel_exporter import ExcelExporter


class TestDeletionSync(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.watch_dir = self.test_dir / "hot_folder"
        self.output_dir = self.test_dir / "output"
        self.db_path = self.test_dir / "test_pipeline.db"
        self.watch_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.pipeline = DocumentPipeline(
            output_dir=self.output_dir,
            mode="move",
            db_path=self.db_path
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_startup_reconciliation(self):
        """Kiểm tra đối soát CSDL khi khởi động: Tệp bị xóa khi app tắt sẽ được dọn sạch khỏi SQLite."""
        # 1. Tạo 3 tệp vật lý giả lập
        file1 = self.output_dir / "file1.pdf"
        file2 = self.output_dir / "file2.pdf"
        file3 = self.output_dir / "file3.pdf"
        file1.write_text("noi dung 1", encoding="utf-8")
        file2.write_text("noi dung 2", encoding="utf-8")
        file3.write_text("noi dung 3", encoding="utf-8")

        # Lưu 3 tệp vào SQLite và nạp mã băm
        self.pipeline.state_tracker.save_state(str(file1), "hash_1", "COMPLETED", simhash="simhash_1")
        self.pipeline.state_tracker.save_state(str(file2), "hash_2", "COMPLETED", simhash="simhash_2")
        self.pipeline.state_tracker.save_state(str(file3), "hash_3", "COMPLETED", simhash="simhash_3")
        self.pipeline.dedup_pipeline.reload_hashes_from_db()

        self.assertEqual(len(self.pipeline.dedup_pipeline.active_simhashes), 3)

        # 2. Người dùng mở Windows Explorer và xóa file1, file2
        file1.unlink()
        file2.unlink()

        # 3. Khởi động đối soát CSDL
        cleaned = self.pipeline.reconcile_database_with_disk()
        self.assertEqual(cleaned, 2)

        # Kiểm tra SQLite chỉ còn đúng file3
        all_completed = self.pipeline.state_tracker.get_all_completed()
        self.assertEqual(len(all_completed), 1)
        self.assertIn(str(file3.resolve()), all_completed)

        # Kiểm tra bộ nhớ đệm băm trong RAM cũng chỉ còn file3
        self.assertNotIn(str(file1.resolve()), self.pipeline.dedup_pipeline.active_simhashes)
        self.assertNotIn(str(file2.resolve()), self.pipeline.dedup_pipeline.active_simhashes)
        self.assertIn(str(file3.resolve()), self.pipeline.dedup_pipeline.active_simhashes)

    def test_clean_empty_date_folder_on_file_deletion(self):
        """Khi xóa hết tài liệu trong ngày, file Excel và thư mục ngày phải được dọn sạch hoàn toàn."""
        date_str = "20261029"
        daily_dir = self.output_dir / date_str
        doc_dir = daily_dir / "Contract"
        doc_dir.mkdir(parents=True, exist_ok=True)

        sample_doc = doc_dir / "contract_01.pdf"
        sample_doc.write_text("Noi dung hop dong dau khi", encoding="utf-8")

        # Lưu trạng thái và xuất file Excel
        self.pipeline.state_tracker.save_state(str(sample_doc), "hash_c1", "COMPLETED")
        self.pipeline.export_current_report(date_str=date_str)

        excel_report = daily_dir / f"tong_hop_tai_lieu_{date_str}.xlsx"
        self.assertTrue(excel_report.exists())

        # Người dùng xóa contract_01.pdf
        sample_doc.unlink()
        # Kích hoạt xử lý xóa tệp
        self.pipeline.handle_file_deleted(sample_doc)

        # CSDL phải không còn bản ghi
        self.assertFalse(self.pipeline.state_tracker.is_processed(str(sample_doc)))

        # File Excel và thư mục ngày rỗng phải được dọn sạch
        self.assertFalse(excel_report.exists())
        self.assertFalse(daily_dir.exists())

    def test_realtime_file_deletion_watcher(self):
        """Kiểm tra Watcher bắt sự kiện xóa tệp thời gian thực trong output_dir."""
        watcher = HotFolderWatcher(
            watch_dir=self.watch_dir,
            output_dir=self.output_dir,
            stable_seconds=0.5,
            mode="move",
            db_path=self.db_path,
        )

        import threading
        watcher_thread = threading.Thread(target=lambda: watcher.start(run_initial_scan=False), daemon=True)
        watcher_thread.start()
        time.sleep(1.0)  # Đợi Observer sẵn sàng

        try:
            # Tạo 1 tệp trong output_dir
            date_dir = self.output_dir / "20261029"
            date_dir.mkdir(parents=True, exist_ok=True)
            doc_file = date_dir / "test_doc.pdf"
            doc_file.write_text("Nội dung thử nghiệm", encoding="utf-8")

            watcher.pipeline.state_tracker.save_state(str(doc_file), "hash_doc", "COMPLETED", simhash="sim_doc")
            watcher.pipeline.dedup_pipeline.reload_hashes_from_db()
            self.assertTrue(watcher.pipeline.state_tracker.is_processed(str(doc_file)))

            # Người dùng xóa test_doc.pdf trên đĩa
            doc_file.unlink()

            # Đợi Watcher bắt sự kiện on_deleted và cập nhật SQLite
            deleted_from_db = False
            for _ in range(20):
                time.sleep(0.2)
                if not watcher.pipeline.state_tracker.is_processed(str(doc_file)):
                    deleted_from_db = True
                    break

            self.assertTrue(deleted_from_db, "Watcher không xóa bản ghi trong SQLite khi tệp bị xóa!")
            self.assertNotIn(str(doc_file.resolve()), watcher.pipeline.dedup_pipeline.active_simhashes)
        finally:
            watcher.stop()

    def test_reupload_after_deletion(self):
        """Sau khi xóa tệp trong output_dir, nạp lại tệp đó vào watch_dir phải được xử lý như tệp mới."""
        sample_file = self.watch_dir / "bao_cao_doc_lap.txt"
        sample_file.write_text("Văn bản phân tích tài nguyên dầu khí", encoding="utf-8")

        # 1. Xử lý ban đầu
        res = self.pipeline.process_single_file(sample_file, date_str="20261029")
        self.assertIsNotNone(res)
        self.assertFalse(res["is_duplicate"])

        out_path = Path(res["file_path"])
        self.assertTrue(out_path.exists())

        # 2. Xóa tệp khỏi output_dir và dọn dẹp CSDL
        out_path.unlink()
        self.pipeline.handle_file_deleted(out_path)
        self.assertFalse(self.pipeline.state_tracker.is_processed(str(out_path)))

        # 3. Tạo lại tệp cùng nội dung vào watch_dir
        sample_file_2 = self.watch_dir / "bao_cao_doc_lap.txt"
        sample_file_2.write_text("Văn bản phân tích tài nguyên dầu khí", encoding="utf-8")

        # 4. Kiểm tra lại qua Tầng 1 lọc trùng lặp
        dedup_res = self.pipeline.dedup_pipeline.process(sample_file_2, date_str="20261029")

        # BẮT BUỘC: Không được nhận nhầm là trùng lặp với tệp ma đã bị xóa
        self.assertFalse(dedup_res.is_duplicate, "Tệp nạp lại bị nhận nhầm là trùng lặp với bản ghi đã xóa!")

    def test_ignore_office_lock_file_deletion(self):
        """Tệp tạm Office (~$) bị xóa hoặc tệp không nằm trong SQLite tuyệt đối không kích hoạt xuất Excel."""
        date_str = "20261029"
        daily_dir = self.output_dir / date_str
        daily_dir.mkdir(parents=True, exist_ok=True)

        # 1. Tạo tệp tạm Office
        temp_office_file = daily_dir / "~$140-xm820b.DOC"
        temp_office_file.write_text("lock data", encoding="utf-8")

        # Mock export_current_report để kiểm tra nó KHÔNG được gọi
        called = []
        orig_export = self.pipeline.export_current_report
        self.pipeline.export_current_report = lambda *args, **kwargs: called.append(True)

        try:
            # Xóa tệp tạm
            temp_office_file.unlink()
            self.pipeline.handle_file_deleted(temp_office_file)
            self.assertEqual(len(called), 0, "handle_file_deleted không được gọi export_current_report cho tệp tạm ~$!")

            # Thử với tệp không có trong SQLite
            untracked_file = daily_dir / "untracked_doc.pdf"
            self.pipeline.handle_file_deleted(untracked_file)
            self.assertEqual(len(called), 0, "handle_file_deleted không được gọi export_current_report cho tệp không có trong SQLite!")
        finally:
            self.pipeline.export_current_report = orig_export

    def test_wait_until_file_ready_read_only_mode(self):
        """wait_until_file_ready với check_write=False hoạt động ngay khi tệp đọc được mà không cần quyền ghi."""
        from src.ingestion.folder_watcher import wait_until_file_ready
        test_file = self.test_dir / "test_read.xlsx"
        test_file.write_text("sample content", encoding="utf-8")

        # Gọi với check_write=False
        ready = wait_until_file_ready(test_file, stable_seconds=0.1, timeout=2.0, check_interval=0.05, check_write=False)
        self.assertTrue(ready, "wait_until_file_ready phải trả về True nhanh chóng khi check_write=False!")


if __name__ == "__main__":
    unittest.main()

