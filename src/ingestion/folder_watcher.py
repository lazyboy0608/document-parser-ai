import os
import queue
import threading
import time
from pathlib import Path
from typing import Set, Optional, Dict, Any

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

from config.settings import settings
from src.utils.logger import logger
from src.ingestion.document_loader import DocumentLoader
from src.pipeline import DocumentPipeline
from src.reporting.sync_engine import ExcelSyncEngine


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
    check_write: bool = True,
) -> bool:
    """
    Chống lỗi Incomplete File Write khi copy file nặng (50MB+) hoặc hàng trăm file qua mạng LAN/SMB:
    1. Kiểm tra kích thước tệp (st_size) > 0 và không thay đổi liên tục trong khoảng `stable_seconds` (mặc định 3 giây).
    2. Nếu check_write=True: Thử mở tệp ở chế độ đọc/ghi nhị phân ('rb+') để xác nhận tiến trình copy của OS/Windows Explorer
       đã nhả hoàn toàn file lock (không còn bị giữ quyền ghi độc quyền).
    3. Nếu check_write=False: Chỉ cần tệp đọc được ('rb') và kích thước ổn định (áp dụng khi người dùng mở Excel và bấm Save).
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
                if not check_write:
                    # Chỉ cần mở đọc thành công ở chế độ 'rb'
                    try:
                        with open(file_path, "rb"):
                            pass
                        return True
                    except OSError:
                        stable_start = None
                else:
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
        pipeline: Optional[Any] = None,
    ):
        super().__init__()
        self.file_queue = file_queue
        self.queued_paths = queued_paths
        self.lock = lock
        self.output_report = output_report
        self.pipeline = pipeline

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

    def on_deleted(self, event: FileSystemEvent):
        target = Path(os.fsdecode(event.src_path))
        with self.lock:
            try:
                self.queued_paths.discard(str(target.resolve()))
            except Exception:
                pass
        if self.pipeline and not event.is_directory:
            if target.name.startswith("~$") or target.name.startswith("."):
                return
            if target.suffix.lower() in DocumentLoader.SUPPORTED_EXTENSIONS:
                self.pipeline.handle_file_deleted(target)


class ExcelSyncEventHandler(FileSystemEventHandler):
    """
    Lắng nghe sự kiện người dùng:
    1. Chỉnh sửa và Lưu file Excel báo cáo (Ctrl + S) trong thư mục output_dir -> Tự động đồng bộ CSDL.
    2. Xóa tệp tài liệu hoặc thư mục trong output_dir -> Tự động dọn dẹp CSDL và giải phóng bộ nhớ băm.
    Có cơ chế chống vòng lặp vô tận (Infinite Loop Guard) và Cool-down Window.
    """
    def __init__(
        self,
        excel_queue: queue.Queue,
        system_modifying: Set[str],
        lock: threading.Lock,
        cooldown_seconds: float = 6.0,
        pipeline: Optional[Any] = None,
    ):
        super().__init__()
        self.excel_queue = excel_queue
        self.system_modifying = system_modifying
        self.lock = lock
        self.cooldown_seconds = cooldown_seconds
        self.last_sync_times: Dict[str, float] = {}
        self.pipeline = pipeline

    def _check_and_enqueue(self, event_path: str):
        target = Path(event_path)
        name = target.name
        # Chỉ quan tâm đến file Excel báo cáo tổng hợp
        if not (name.endswith(".xlsx") and "tong_hop_tai_lieu" in name.lower()):
            return
        # Bỏ qua file khóa tạm thời của MS Excel (~$) hoặc file ẩn (.)
        if name.startswith("~$") or name.startswith("."):
            return

        resolved = str(target.resolve())
        now = time.monotonic()

        with self.lock:
            # 1. Bỏ qua nếu file này do chính hệ thống đang ghi ra (chống vòng lặp vô tận)
            if resolved in self.system_modifying:
                return

            # 2. Bỏ qua nếu vừa mới đồng bộ xong trong khoảng cooldown_seconds
            last_time = self.last_sync_times.get(resolved, 0.0)
            if (now - last_time) < self.cooldown_seconds:
                return

            self.last_sync_times[resolved] = now

        logger.info(f"📊 [EXCEL WATCHER] Phát hiện file Excel vừa được lưu/thay đổi: {target.name} -> Đưa vào hàng đợi tự động đồng bộ...")
        self.excel_queue.put(target)

    def on_modified(self, event: FileSystemEvent):
        if not event.is_directory:
            self._check_and_enqueue(os.fsdecode(event.src_path))

    def on_created(self, event: FileSystemEvent):
        if not event.is_directory:
            self._check_and_enqueue(os.fsdecode(event.src_path))

    def on_deleted(self, event: FileSystemEvent):
        target = Path(os.fsdecode(event.src_path))
        if target.name.startswith("~$") or target.name.startswith("."):
            return
        if event.is_directory:
            if self.pipeline:
                self.pipeline.handle_directory_deleted(target)
        else:
            ext = target.suffix.lower()
            if ext in DocumentLoader.SUPPORTED_EXTENSIONS:
                if self.pipeline:
                    self.pipeline.handle_file_deleted(target)


class HotFolderWatcher:
    """
    Dịch vụ chạy ngầm 24/7 theo dõi thư mục chung (Hot Folder / Shared Network Drive):
    1. Khi khởi động, quét một lượt các tệp tồn đọng chưa xử lý trong thư mục.
    2. Lắng nghe đệ quy (recursive=True) mọi file hoặc thư mục con được người dùng thả vào.
    3. Chờ từng file ghi hoàn tất 100% (wait_until_file_ready) trước khi gọi AI Pipeline.
    4. Xử lý xong tự động phân loại vào output_documents và cập nhật ngay vào file Excel báo cáo.
    5. [MỚI] Tự động đồng bộ ngược khi người dùng lưu file Excel (Zero-Click Auto Sync).
    """
    def __init__(
        self,
        watch_dir: Path = settings.INPUT_DIR,
        output_dir: Optional[Path] = None,
        output_report: Optional[Path] = None,
        stable_seconds: float = settings.WATCH_STABLE_SECONDS,
        mode: str = "move",
        db_path: Optional[Path] = None,
    ):
        self.watch_dir = Path(watch_dir).resolve()
        self.output_dir = Path(output_dir or settings.OUTPUT_DIR).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.custom_output_report = Path(output_report).resolve() if output_report else None
        self.output_report = self.custom_output_report

        self.stable_seconds = stable_seconds
        self.mode = mode
        self.db_path = Path(db_path).resolve() if db_path else None

        self.pipeline = DocumentPipeline(output_dir=self.output_dir, mode=self.mode, db_path=self.db_path)
        self.sync_engine = ExcelSyncEngine(db_path=self.db_path, output_dir=self.output_dir)

        # Hàng đợi tiếp nhận tài liệu đầu vào
        self.file_queue: queue.Queue[Optional[Path]] = queue.Queue()
        self.queued_paths: Set[str] = set()

        # Hàng đợi tự động đồng bộ Excel khi người dùng Lưu file
        self.excel_queue: queue.Queue[Optional[Path]] = queue.Queue()
        self.system_modifying_excel: Set[str] = set()

        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.observer = Observer()

    def _excel_sync_loop(self):
        """
        Luồng Worker chạy ngầm chuyên trách tự động đồng bộ khi người dùng lưu file Excel.
        """
        while not self.stop_event.is_set():
            try:
                excel_path = self.excel_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if excel_path is None:
                self.excel_queue.task_done()
                break

            resolved_p = str(excel_path.resolve())
            try:
                # Đợi cho đến khi Excel ghi xong file (người dùng bấm Save)
                wait_sec = min(self.stable_seconds, 1.0)
                logger.info(f"⏳ [AUTO-SYNC] Đang đợi MS Excel hoàn tất ghi tệp ({wait_sec}s): {excel_path.name}...")
                is_ready = wait_until_file_ready(excel_path, stable_seconds=wait_sec, timeout=10.0, check_write=False)
                if not is_ready:
                    logger.warning(f"⚠️ [AUTO-SYNC] Bỏ qua đồng bộ vì tệp {excel_path.name} chưa sẵn sàng để đọc.")
                    continue

                # Đánh dấu cờ hệ thống đang cập nhật để Watcher không tự bắt lặp lại
                with self.lock:
                    self.system_modifying_excel.add(resolved_p)

                try:
                    logger.info(f"🔄 [AUTO-SYNC] Bắt đầu tự động đồng bộ dữ liệu người dùng đã sửa từ: {excel_path.name}")
                    sync_res = self.sync_engine.sync_from_excel(excel_path, auto_recluster=True)
                    if sync_res.get("updated_files_count", 0) > 0:
                        logger.info(
                            f"✨ [AUTO-SYNC THÀNH CÔNG] Đã tự động cập nhật {sync_res['updated_files_count']} tệp vào CSDL "
                            f"và tái phân cụm {len(sync_res.get('reclustered_files', []))} thư mục!"
                        )
                    else:
                        logger.info(f"ℹ️ [AUTO-SYNC] File Excel {excel_path.name} không có thay đổi mới nào so với CSDL.")
                finally:
                    # Giữ cờ trong 3 giây để triệt tiêu mọi sự kiện IO dư âm của quá trình ghi lại Excel
                    time.sleep(3.0)
                    with self.lock:
                        self.system_modifying_excel.discard(resolved_p)

            except Exception as sync_err:
                logger.error(f"❌ [AUTO-SYNC] Lỗi khi tự động đồng bộ từ {excel_path.name}: {sync_err}")
            finally:
                self.excel_queue.task_done()

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

        # 0. Tự động đối soát CSDL với đĩa vật lý trước khi khởi động
        self.pipeline.reconcile_database_with_disk()

        if run_initial_scan:
            logger.info("🔍 [WATCHER] Kiểm tra các tài liệu hiện có trong thư mục trước khi vào chế độ trực chiến...")
            from datetime import datetime
            today_str = datetime.now().strftime("%Y%m%d")
            daily_report = self.custom_output_report or (self.output_dir / today_str / f"tong_hop_tai_lieu_{today_str}.xlsx")
            self.pipeline.run(input_dir=self.watch_dir, output_report=daily_report, date_str=today_str)

        event_handler = HotFolderEventHandler(
            self.file_queue, self.queued_paths, self.lock, output_report=self.custom_output_report, pipeline=self.pipeline
        )
        self.observer.schedule(event_handler, str(self.watch_dir), recursive=True)

        # Đăng ký theo dõi sự kiện lưu file Excel trong thư mục đầu ra output_dir
        excel_handler = ExcelSyncEventHandler(
            excel_queue=self.excel_queue,
            system_modifying=self.system_modifying_excel,
            lock=self.lock,
            cooldown_seconds=6.0,
            pipeline=self.pipeline,
        )
        self.observer.schedule(excel_handler, str(self.output_dir), recursive=True)

        self.observer.start()

        worker_thread = threading.Thread(target=self._worker_loop, name="HotFolderWorker", daemon=True)
        worker_thread.start()

        excel_sync_thread = threading.Thread(target=self._excel_sync_loop, name="ExcelAutoSyncWorker", daemon=True)
        excel_sync_thread.start()

        logger.info("=" * 65)
        logger.info(f"👀 [HOT FOLDER WATCHER] Đang lắng nghe trực tuyến tại: {self.watch_dir}")
        logger.info(f"📊 [AUTO-SYNC EXCEL] Đang lắng nghe sự kiện lưu Excel tại: {self.output_dir}")
        logger.info("   -> Hãy thả tệp hoặc cả thư mục chứa tài liệu vào đường dẫn trên.")
        logger.info("   -> Mở file Excel chỉnh sửa và bấm Ctrl+S để tự động đồng bộ CSDL.")
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
                                excel_handler = ExcelSyncEventHandler(
                                    excel_queue=self.excel_queue,
                                    system_modifying=self.system_modifying_excel,
                                    lock=self.lock,
                                    cooldown_seconds=6.0,
                                )
                                self.observer.schedule(excel_handler, str(self.output_dir), recursive=True)
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
        self.excel_queue.put(None)
        try:
            if getattr(self, "observer", None) and self.observer.is_alive():
                self.observer.stop()
                self.observer.join(timeout=5.0)
        except Exception:
            pass
        logger.info("👋 Dịch vụ Hot Folder Watcher đã dừng an toàn.")
