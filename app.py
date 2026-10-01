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
        default=settings.REPORTS_DIR / "tong_hop_tai_lieu.xlsx",
        help="Đường dẫn tệp Excel báo cáo tổng hợp"
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
    args = parser.parse_args()

    # Tự động nhận diện thư mục đầu ra D:\outputNLP nếu có sẵn hoặc theo chỉ định
    if args.output_dir is None:
        cand_output = Path("D:/outputNLP")
        if cand_output.exists():
            args.output_dir = cand_output
        else:
            args.output_dir = settings.OUTPUT_DIR
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Tự động xuất file Excel trong thư mục output_dir nếu người dùng không chỉ định file cụ thể
    default_report = settings.REPORTS_DIR / "tong_hop_tai_lieu.xlsx"
    if args.output_report.resolve() == default_report.resolve():
        if args.output_dir.resolve() != settings.OUTPUT_DIR.resolve():
            args.output_report = args.output_dir / "tong_hop_tai_lieu.xlsx"
        elif args.input_dir.resolve() != settings.INPUT_DIR.resolve():
            args.output_report = args.input_dir / "tong_hop_tai_lieu.xlsx"

    logger.info("=" * 65)
    logger.info("  HỆ THỐNG XỬ LÝ & TỔNG HỢP TÀI LIỆU TỰ ĐỘNG (OCR + VLM + LLM)")
    logger.info(f"  Mô hình Text LLM: {settings.OLLAMA_MODEL} | VLM: {settings.VLM_MODEL}")
    logger.info(f"  Thư mục đầu vào : {args.input_dir}")
    logger.info(f"  Thư mục phân loại: {args.output_dir} (Cơ chế: {args.mode.upper()})")
    logger.info(f"  Tệp báo cáo Excel: {args.output_report}")
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
    else:
        pipeline = DocumentPipeline(output_dir=args.output_dir, mode=args.mode)
        pipeline.run(input_dir=args.input_dir, output_report=args.output_report)


if __name__ == "__main__":
    main()

