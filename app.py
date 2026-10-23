import argparse
import sys
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

if sys.platform.startswith("win"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from config.settings import settings
from src.utils.logger import logger
from src.pipeline import DocumentPipeline
from src.ingestion.folder_watcher import HotFolderWatcher


def main():
    parser = argparse.ArgumentParser(
        description="Hệ thống AI Trích xuất, Phân loại & Tổng hợp Tài liệu Dầu khí Tự động"
    )
    parser.add_argument(
        "--watch", "-w",
        action="store_true",
        help="Kích hoạt chế độ Hot Folder Watcher (tự động lắng nghe ngầm thư mục chung 24/7)"
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=settings.INPUT_DIR,
        help="Đường dẫn thư mục đầu vào / thư mục mạng nội bộ cần lắng nghe"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Đường dẫn thư mục lưu trữ tài liệu phân loại (ví dụ: D:\\outputNLP)"
    )
    parser.add_argument(
        "--output-report",
        type=Path,
        default=None,
        help="Đường dẫn tệp Excel báo cáo tổng hợp (Mặc định: tự động theo ngày <output_dir>/<YYYYMMDD>/tong_hop_tai_lieu_<YYYYMMDD>.xlsx)"
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["move", "copy"],
        default="move",
        help="Cơ chế tổ chức file: 'move' (di chuyển & dọn sạch) hoặc 'copy' (sao chép nhân bản)"
    )
    parser.add_argument(
        "--stable-seconds",
        type=float,
        default=settings.WATCH_STABLE_SECONDS,
        help="Số giây kiểm tra kích thước tệp ổn định và nhả khóa trước khi đọc (chống lỗi ghi file chưa xong)"
    )
    parser.add_argument(
        "--sync-excel",
        type=Path,
        default=None,
        help="Đồng bộ ngược dữ liệu do con người chỉnh sửa từ tệp Excel vào CSDL SQLite (Human-in-the-Loop)"
    )
    parser.add_argument(
        "--dedup-only",
        action="store_true",
        help="Chế độ chạy riêng Tầng 1: Lọc trùng lặp & liên kết 2 chiều siêu tốc thuần CPU, không gọi AI"
    )
    args = parser.parse_args()

    # Xử lý lệnh đồng bộ ngược từ Excel nếu người dùng chỉ định
    if args.sync_excel:
        from src.reporting.sync_engine import ExcelSyncEngine
        cand_output = Path("D:/outputNLP")
        out_d = args.output_dir if args.output_dir else (cand_output if cand_output.exists() else settings.OUTPUT_DIR)
        sync_engine = ExcelSyncEngine(output_dir=out_d)
        res = sync_engine.sync_from_excel(args.sync_excel)
        logger.info(f"Hoàn thành đồng bộ: {res['updated_files_count']} tệp được cập nhật.")
        return

    # Tự động nhận diện thư mục đầu ra D:\outputNLP nếu có sẵn hoặc theo chỉ định
    if args.output_dir is None:
        cand_output = Path("D:/outputNLP")
        if cand_output.exists():
            args.output_dir = cand_output
        else:
            args.output_dir = settings.OUTPUT_DIR
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    report_display = (
        str(args.output_report)
        if args.output_report
        else f"Tự động theo ngày ({args.output_dir}/<YYYYMMDD>/tong_hop_tai_lieu_<YYYYMMDD>.xlsx)"
    )

    logger.info("=" * 65)
    logger.info("  HỆ THỐNG XỬ LÝ & TỔNG HỢP TÀI LIỆU TỰ ĐỘNG (OCR + VLM + LLM)")
    logger.info(f"  Mô hình Text LLM: {settings.OLLAMA_MODEL} | VLM: {settings.VLM_MODEL}")
    logger.info(f"  Thư mục đầu vào : {args.input_dir}")
    logger.info(f"  Thư mục phân loại: {args.output_dir} (Cơ chế: {args.mode.upper()})")
    logger.info(f"  Tệp báo cáo Excel: {report_display}")
    logger.info("=" * 65)

    if args.watch:
        watcher = HotFolderWatcher(
            watch_dir=args.input_dir,
            output_dir=args.output_dir,
            output_report=args.output_report,
            stable_seconds=args.stable_seconds,
            mode=args.mode,
        )
        watcher.start(run_initial_scan=True)
    elif args.dedup_only:
        logger.info("⚡ Đang chạy chế độ lọc trùng lặp độc lập thuần CPU (--dedup-only)...")
        pipeline = DocumentPipeline(output_dir=args.output_dir, mode=args.mode)
        all_files = [
            f.resolve() for f in args.input_dir.rglob("*")
            if f.is_file() and not f.name.startswith("~$") and not f.name.startswith(".")
        ]
        logger.info(f"Phát hiện {len(all_files)} tài liệu. Bắt đầu sàng lọc...")
        dup_count = 0
        for f in all_files:
            res = pipeline.dedup_pipeline.process(f)
            if res.is_duplicate:
                dup_count += 1
                logger.info(f"  [TRÙNG LẶP] {f.name} -> {res.matched_orig_name} (Nhóm: {res.group_id})")
        logger.info(f"✅ Hoàn tất lọc trùng lặp CPU! Phát hiện {dup_count}/{len(all_files)} tệp trùng lặp.")
        pipeline.export_current_report(input_dir=args.input_dir, output_report=args.output_report)
    else:
        pipeline = DocumentPipeline(output_dir=args.output_dir, mode=args.mode)
        pipeline.run(input_dir=args.input_dir, output_report=args.output_report)


if __name__ == "__main__":
    main()

