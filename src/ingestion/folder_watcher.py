import os
import queue
import threading
import time
from pathlib import Path
from typing import Set, Optional

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

from config.settings import settings
from src.utils.logger import logger
from src.ingestion.document_loader import DocumentLoader
from src.pipeline import DocumentPipeline


def is_ignored_file(file_path: Path, output_report: Optional[Path] = None) -> bool:
    """
    Loại bỏ các tệp tạm của hệ điều hành / Office / trình duyệt và chính tệp báo cáo Excel:
    - Chính tệp báo cáo Excel đích (tránh kích hoạt vòng lặp vô tận khi AI ghi file)
    - Tệp ẩn bắt đầu bằng '.'
    - Tệp khóa tạm của MS Word/Excel bắt đầu bằng '~$'
    - Tệp đang tải dở (.tmp, .part, .crdownload, .partial)
    """
    try:
        resolved = file_path.resolve()
    except OSError:
        resolved = file_path

    if output_report is not None:
        try:
            if resolved == output_report.resolve():
                return True
        except OSError:
            pass

    name = resolved.name
    # Bỏ qua tệp khóa tạm thời hoặc file ẩn
    if name.startswith("~$") or name.startswith("."):
        return True
    if resolved.suffix.lower() in {".tmp", ".part", ".crdownload", ".partial"}:
        return True
    # Bỏ qua nếu là file Excel báo cáo tổng hợp
    if resolved.suffix.lower() == ".xlsx" and "tong_hop_tai_lieu" in name.lower():
        return True
    if resolved.suffix.lower() not in DocumentLoader.SUPPORTED_EXTENSIONS:
        return True
    return False



def wait_until_file_ready(
    file_path: Path,
    stable_seconds: float = settings.WATCH_STABLE_SECONDS,
    timeout: float = settings.WATCH_TIMEOUT_SECONDS,
    check_interval: float = 1.0,
) -> bool:
    """
    Chống lỗi Incomplete File Write khi copy file nặng (50MB+) hoặc hàng trăm file qua mạng LAN/SMB:
    1. Kiểm tra kích thước tệp (st_size) > 0 và không thay đổi liên tục trong khoảng `stable_seconds` (mặc định 3 giây).
    2. Thử mở tệp ở chế độ đọc/ghi nhị phân ('rb+') để xác nhận tiến trình copy của OS/Windows Explorer
       đã nhả hoàn toàn file lock (không còn bị giữ quyền ghi độc quyền).
    """
    start_time = time.monotonic()
    last_size = -1
    stable_start: Optional[float] = None

    while (time.monotonic() - start_time) < timeout:
        if not file_path.exists():
            return False

        try:
            current_size = file_path.stat().st_size
        except OSError:
            current_size = -1

        if current_size > 0 and current_size == last_size:
            if stable_start is None:
                stable_start = time.monotonic()
            elif (time.monotonic() - stable_start) >= stable_seconds:
                # Xác nhận OS đã nhả khóa ghi bằng cách thử mở ở chế độ 'rb+'
                try:
                    with open(file_path, "rb+"):
                        pass
                    return True
                except PermissionError:
                    # Nếu tệp là Read-Only thực sự trên đĩa nhưng không bị tiến trình khác khóa ghi
                    if not os.access(file_path, os.W_OK):
                        try:
                            with open(file_path, "rb"):
                                pass
                            return True
                        except OSError:
                            pass
                    # Vẫn đang bị tiến trình copy giữ lock -> tiếp tục đợi
                    stable_start = None
                except OSError:
                    stable_start = None
        else:
            last_size = current_size
            stable_start = None

        time.sleep(check_interval)

    logger.error(f"Quá thời gian chờ ({timeout}s) mà tệp vẫn chưa ghi xong hoặc bị khóa: {file_path.name}")
    return False


class HotFolderEventHandler(FileSystemEventHandler):
    """
    Lắng nghe sự kiện tạo mới / di chuyển tệp hoặc cả thư mục con vào trong Hot Folder.
    Đẩy đường dẫn tệp vào hàng đợi xử lý bất đồng bộ (Queue) để không làm nghẽn luồng của Watchdog.
    """
    def __init__(
        self,
        file_queue: queue.Queue,
        queued_paths: Set[str],
        lock: threading.Lock,
        output_report: Optional[Path] = None,
    ):
        super().__init__()
        self.file_queue = file_queue
        self.queued_paths = queued_paths
        self.lock = lock
        self.output_report = output_report

    def _enqueue_file(self, path: Path):
        try:
            resolved = path.resolve()
        except OSError:
            return

        if is_ignored_file(resolved, output_report=self.output_report):
            return

        path_str = str(resolved)
        with self.lock:
            if path_str in self.queued_paths:
                return
            self.queued_paths.add(path_str)

        logger.info(f"📥 [WATCHER] Phát hiện tài liệu mới: {resolved.name} -> Đưa vào hàng đợi kiểm tra hoàn tất ghi file...")
        self.file_queue.put(resolved)


    def _enqueue_directory(self, dir_path: Path):
        """Khi người dùng quăng nguyên 1 folder chứa nhiều tài liệu vào Hot Folder."""
        try:
            for item in dir_path.rglob("*"):
                if item.is_file():
                    self._enqueue_file(item)
        except OSError as e:
            logger.warning(f"Không thể quét thư mục mới {dir_path}: {e}")

    def on_created(self, event: FileSystemEvent):
        target = Path(os.fsdecode(event.src_path))
        if event.is_directory:
            logger.info(f"📂 [WATCHER] Phát hiện thư mục mới được thả vào: {target.name}")
            self._enqueue_directory(target)
        else:
            self._enqueue_file(target)

    def on_moved(self, event: FileSystemEvent):
        dest_raw = getattr(event, "dest_path", None)
        if not dest_raw:
            return
        target = Path(os.fsdecode(dest_raw))
        if event.is_directory:
            logger.info(f"📂 [WATCHER] Phát hiện thư mục được di chuyển/đổi tên vào: {target.name}")
            self._enqueue_directory(target)
        else:
            self._enqueue_file(target)


class HotFolderWatcher:
    """
    Dịch vụ chạy ngầm 24/7 theo dõi thư mục chung (Hot Folder / Shared Network Drive):
    1. Khi khởi động, quét một lượt các tệp tồn đọng chưa xử lý trong thư mục.
    2. Lắng nghe đệ quy (recursive=True) mọi file hoặc thư mục con được người dùng thả vào.
    3. Chờ từng file ghi hoàn tất 100% (wait_until_file_ready) trước khi gọi AI Pipeline.
    4. Xử lý xong tự động phân loại vào output_documents và cập nhật ngay vào file Excel báo cáo.
    """
    def __init__(
        self,
        watch_dir: Path = settings.INPUT_DIR,
        output_dir: Optional[Path] = None,
        output_report: Optional[Path] = None,
        stable_seconds: float = settings.WATCH_STABLE_SECONDS,
        mode: str = "move",
    ):
        self.watch_dir = Path(watch_dir).resolve()
        self.output_dir = Path(output_dir or settings.OUTPUT_DIR).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.custom_output_report = Path(output_report).resolve() if output_report else None
        self.output_report = self.custom_output_report

        self.stable_seconds = stable_seconds
        self.mode = mode

        self.pipeline = DocumentPipeline(output_dir=self.output_dir, mode=self.mode)
        self.file_queue: queue.Queue[Optional[Path]] = queue.Queue()
        self.queued_paths: Set[str] = set()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.observer = Observer()

    def _worker_loop(self):
        """
        Luồng Worker chạy ngầm lấy từng tệp từ hàng đợi, chờ ghi xong, trích xuất AI và cập nhật Excel.
        Nếu có nhiều tệp được thả cùng lúc (ví dụ cả folder 50 file), tự động gom nhóm cập nhật Excel.
        """
        while not self.stop_event.is_set():
            try:
                file_path = self.file_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if file_path is None:
                self.file_queue.task_done()
                break

            path_str = str(file_path)
            try:
                # Nếu ở chế độ copy và file đã xử lý xong trước đó, bỏ qua để tránh lặp
                if self.mode == "copy" and self.pipeline.state_tracker.is_processed(path_str):
                    logger.debug(f"File {file_path.name} đã được xử lý (chế độ copy), bỏ qua.")
                    continue

                logger.info(f"⏳ [WATCHER] Đang chờ file ghi hoàn tất (ổn định {self.stable_seconds}s): {file_path.name}...")
                is_ready = wait_until_file_ready(file_path, stable_seconds=self.stable_seconds)
                if not is_ready:
                    logger.warning(f"⚠️ Bỏ qua {file_path.name} vì tệp không khả dụng hoặc chưa ghi xong.")
                    continue

                logger.info(f"🚀 [WATCHER] File đã sẵn sàng 100%! Bắt đầu trích xuất AI: {file_path.name}")
                from datetime import datetime
                today_str = datetime.now().strftime("%Y%m%d")
                result = self.pipeline.process_single_file(file_path, date_str=today_str)
                if result is not None:
                    daily_report = self.custom_output_report or (self.output_dir / today_str / f"tong_hop_tai_lieu_{today_str}.xlsx")
                    # Cập nhật ngay dòng mới vào file Excel tổng hợp theo ngày
                    try:
                        self.pipeline.export_current_report(
                            input_dir=self.watch_dir,
                            output_report=daily_report,
                            date_str=today_str
                        )
                        logger.info(f"✅ [WATCHER] Đã xử lý, phân loại & cập nhật Excel thành công cho: {file_path.name}")
                    except Exception as export_err:
                        logger.error(f"⚠️ [WATCHER] Không thể cập nhật báo cáo Excel do lỗi mạng LAN/IO: {export_err}")
            except Exception as e:
                logger.error(f"❌ [WATCHER] Lỗi không mong muốn khi xử lý {file_path.name}: {e}")
            finally:
                with self.lock:
                    self.queued_paths.discard(path_str)
                self.file_queue.task_done()

    def start(self, run_initial_scan: bool = True):
        """Khởi chạy dịch vụ lắng nghe thư mục."""
        self.watch_dir.mkdir(parents=True, exist_ok=True)

        if run_initial_scan:
            logger.info("🔍 [WATCHER] Kiểm tra các tài liệu hiện có trong thư mục trước khi vào chế độ trực chiến...")
            from datetime import datetime
            today_str = datetime.now().strftime("%Y%m%d")
            daily_report = self.custom_output_report or (self.output_dir / today_str / f"tong_hop_tai_lieu_{today_str}.xlsx")
            self.pipeline.run(input_dir=self.watch_dir, output_report=daily_report, date_str=today_str)

        event_handler = HotFolderEventHandler(
            self.file_queue, self.queued_paths, self.lock, output_report=self.custom_output_report
        )
        self.observer.schedule(event_handler, str(self.watch_dir), recursive=True)
        self.observer.start()

        worker_thread = threading.Thread(target=self._worker_loop, name="HotFolderWorker", daemon=True)
        worker_thread.start()

        logger.info("=" * 65)
        logger.info(f"👀 [HOT FOLDER WATCHER] Đang lắng nghe trực tuyến tại: {self.watch_dir}")
        logger.info("   -> Hãy thả tệp hoặc cả thư mục chứa tài liệu vào đường dẫn trên.")
        logger.info(f"   -> Thời gian xác nhận hoàn tất ghi file: {self.stable_seconds} giây")
        logger.info("   -> Nhấn Ctrl+C để dừng dịch vụ.")
        logger.info("=" * 65)

        try:
            while not self.stop_event.is_set():
                time.sleep(2.0)
                if self.stop_event.is_set():
                    break
                # Kiểm tra trạng thái hoạt động (Heartbeat) của Observer và thư mục Hot Folder trên mạng LAN
                observer_alive = False
                try:
                    observer_alive = self.observer.is_alive()
                except Exception:
                    observer_alive = False

                dir_accessible = False
                try:
                    dir_accessible = self.watch_dir.exists()
                except Exception:
                    dir_accessible = False

                if not observer_alive or not dir_accessible:
                    logger.warning(
                        f"⚠️ [WATCHER] Phát hiện mất kết nối tới Hot Folder ({self.watch_dir}) "
                        f"hoặc luồng Observer đã dừng (Observer alive: {observer_alive}, Dir exists: {dir_accessible}). "
                        "Bắt đầu chu trình tự phục hồi kết nối..."
                    )
                    # Dừng observer cũ nếu còn sót
                    try:
                        if self.observer.is_alive():
                            self.observer.stop()
                            self.observer.join(timeout=2.0)
                    except Exception:
                        pass

                    # Vòng lặp thăm dò kết nối lại (Probing loop)
                    reconnected = False
                    while not self.stop_event.is_set():
                        try:
                            if self.watch_dir.exists():
                                logger.info(f"🔄 [WATCHER] Đã khôi phục kết nối tới {self.watch_dir}! Đang khởi động lại Observer...")
                                self.observer = Observer()
                                event_handler = HotFolderEventHandler(
                                    self.file_queue, self.queued_paths, self.lock, output_report=self.custom_output_report
                                )
                                self.observer.schedule(event_handler, str(self.watch_dir), recursive=True)
                                self.observer.start()
                                reconnected = True
                                break
                        except Exception as probe_err:
                            logger.debug(f"Đang chờ mạng LAN/Hot Folder phục hồi: {probe_err}")

                        time.sleep(3.0)

                    if reconnected and not self.stop_event.is_set():
                        logger.info("🔍 [WATCHER] Chạy quét bù (Catch-up Scan) cho các tệp đã upload trong thời gian mất mạng...")
                        from datetime import datetime
                        today_str = datetime.now().strftime("%Y%m%d")
                        daily_report = self.custom_output_report or (self.output_dir / today_str / f"tong_hop_tai_lieu_{today_str}.xlsx")
                        try:
                            self.pipeline.run(input_dir=self.watch_dir, output_report=daily_report, date_str=today_str)
                            logger.info("✅ [WATCHER] Quét bù hoàn tất. Đã tiếp tục lắng nghe sự kiện bình thường.")
                        except Exception as catch_err:
                            logger.error(f"Lỗi khi thực hiện quét bù sau phục hồi mạng: {catch_err}")

        except KeyboardInterrupt:
            logger.info("🛑 Đang nhận tín hiệu dừng (Ctrl+C)...")
            self.stop()

    def stop(self):
        """Dừng dịch vụ an toàn."""
        self.stop_event.set()
        self.file_queue.put(None)
        try:
            if getattr(self, "observer", None) and self.observer.is_alive():
                self.observer.stop()
                self.observer.join(timeout=5.0)
        except Exception:
            pass
        logger.info("👋 Dịch vụ Hot Folder Watcher đã dừng an toàn.")
