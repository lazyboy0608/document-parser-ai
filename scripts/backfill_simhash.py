import sys
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

if sys.platform.startswith("win"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import sqlite3
from config.settings import settings
from src.utils.logger import logger
from src.ingestion.document_loader import DocumentLoader
from src.deduplication.simhash_dedup import SimHashDeduplicator


def backfill_simhashes():
    db_path = settings.CACHE_DIR / "pipeline_state.db"
    if not db_path.exists():
        logger.warning(f"CSDL không tồn tại tại {db_path}")
        return

    simhash_dedup = SimHashDeduplicator(threshold_bits=3)
    
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT file_path, status, is_duplicate FROM processed_files WHERE simhash IS NULL")
        records = cursor.fetchall()

        logger.info(f"Tìm thấy {len(records)} bản ghi chưa có mã SimHash cần quét bù...")
        updated_count = 0
        skipped_count = 0

        for file_path_str, status, is_dup in records:
            p = Path(file_path_str)
            if not p.exists():
                logger.warning(f"Tệp không tồn tại trên đĩa, bỏ qua: {file_path_str}")
                skipped_count += 1
                continue

            try:
                doc_data = DocumentLoader.load_document(p)
                raw_text = doc_data.get("text", "")
                if not raw_text.strip():
                    logger.warning(f"Tệp rỗng hoặc không có text: {p.name}")
                    continue

                shash = simhash_dedup.compute_simhash(raw_text)
                cursor.execute(
                    "UPDATE processed_files SET simhash = ? WHERE file_path = ?",
                    (shash, file_path_str)
                )
                updated_count += 1
                logger.info(f"✓ Cập nhật SimHash [{shash}] cho: {p.name}")
            except Exception as e:
                logger.error(f"Lỗi khi tính SimHash cho {p.name}: {e}")
                skipped_count += 1

        conn.commit()
        logger.info(f"Hoàn thành quét bù SimHash: Đã cập nhật {updated_count} tệp, Bỏ qua {skipped_count} tệp.")


if __name__ == "__main__":
    backfill_simhashes()
