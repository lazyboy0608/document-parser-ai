import json
import sqlite3
from pathlib import Path
from typing import Optional, Dict, Any


class StateTracker:
    """
    Theo dõi tiến trình xử lý hàng nghìn tài liệu bằng SQLite.
    Giúp lưu checkpoint, tránh việc chương trình bị gián đoạn phải chạy lại từ đầu.
    """
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS processed_files (
                    file_path TEXT PRIMARY KEY,
                    file_hash TEXT,
                    simhash TEXT,
                    status TEXT,
                    is_duplicate INTEGER DEFAULT 0,
                    metadata_json TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Tự động nâng cấp bảng nếu thiếu cột simhash từ phiên bản trước
            cursor.execute("PRAGMA table_info(processed_files);")
            columns = [col[1] for col in cursor.fetchall()]
            if "simhash" not in columns:
                cursor.execute("ALTER TABLE processed_files ADD COLUMN simhash TEXT;")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_simhash ON processed_files(simhash);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_file_hash ON processed_files(file_hash);")

            # Đồng bộ các bản ghi đường dẫn tương đối cũ sang đường dẫn tuyệt đối chuẩn
            cursor.execute("SELECT file_path, file_hash, simhash, status, is_duplicate, metadata_json FROM processed_files")
            rows = cursor.fetchall()
            for fp, fhash, shash, st, is_dup, mjson in rows:
                norm_p = self._norm(fp)
                if norm_p != fp:
                    # Nếu bản ghi chuẩn hóa chưa có hoặc đang bị đánh dấu trùng lặp nhầm, ghi đè bằng bản gốc
                    cursor.execute("SELECT is_duplicate FROM processed_files WHERE file_path = ?", (norm_p,))
                    existing = cursor.fetchone()
                    if existing is None or (existing[0] == 1 and is_dup == 0):
                        cursor.execute(
                            "INSERT OR REPLACE INTO processed_files (file_path, file_hash, simhash, status, is_duplicate, metadata_json) VALUES (?, ?, ?, ?, ?, ?)",
                            (norm_p, fhash, shash, st, is_dup, mjson),
                        )
                    cursor.execute("DELETE FROM processed_files WHERE file_path = ?", (fp,))
            conn.commit()

    @staticmethod
    def _norm(file_path: str) -> str:
        try:
            return str(Path(file_path).resolve())
        except Exception:
            return str(file_path)

    def is_processed(self, file_path: str) -> bool:
        norm_p = self._norm(file_path)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT status FROM processed_files WHERE file_path = ? OR file_path = ?",
                (norm_p, str(file_path)),
            )
            row = cursor.fetchone()
            return row is not None and row[0] == "COMPLETED"

    def save_state(
        self,
        file_path: str,
        file_hash: str,
        status: str,
        is_duplicate: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
        simhash: Optional[str] = None
    ):
        norm_p = self._norm(file_path)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # Xóa bản ghi đường dẫn tương đối cũ (nếu có) để tránh trùng lặp với đường dẫn tuyệt đối
            if norm_p != str(file_path):
                cursor.execute("DELETE FROM processed_files WHERE file_path = ?", (str(file_path),))

            # Nếu simhash không được truyền vào, giữ lại simhash cũ đã có (nếu có)
            if simhash is None:
                cursor.execute("SELECT simhash FROM processed_files WHERE file_path = ?", (norm_p,))
                row = cursor.fetchone()
                if row and row[0]:
                    simhash = row[0]

            cursor.execute("""
                INSERT OR REPLACE INTO processed_files (file_path, file_hash, simhash, status, is_duplicate, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                norm_p,
                file_hash,
                simhash,
                status,
                1 if is_duplicate else 0,
                json.dumps(metadata, ensure_ascii=False) if metadata else None
            ))
            conn.commit()

    def delete_state(self, file_path: str):
        """Xóa trạng thái của tệp khỏi cơ sở dữ liệu (ví dụ khi tệp được di chuyển sang vị trí mới)."""
        norm_p = self._norm(file_path)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM processed_files WHERE file_path = ? OR file_path = ?", (norm_p, str(file_path)))
            conn.commit()

    def cleanup_missing_files(self) -> int:
        """
        Dọn dẹp các bản ghi trong SQLite nếu tệp vật lý không còn tồn tại trên đĩa.
        Giúp đồng bộ trạng thái khi người dùng chủ động xóa tệp cũ để test lại hoặc xử lý lại từ đầu.
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT file_path FROM processed_files")
            rows = cursor.fetchall()
            deleted_count = 0
            for (fp,) in rows:
                try:
                    p = Path(fp)
                    if not p.exists():
                        cursor.execute("DELETE FROM processed_files WHERE file_path = ?", (fp,))
                        deleted_count += 1
                except Exception:
                    pass
            conn.commit()
            return deleted_count

    def get_all_completed(self) -> Dict[str, Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT file_path, is_duplicate, metadata_json FROM processed_files WHERE status = 'COMPLETED'")
            rows = cursor.fetchall()
            results = {}
            for path_str, is_dup, meta_json in rows:
                norm_p = self._norm(path_str)
                meta = json.loads(meta_json) if meta_json else {}
                # Ưu tiên giữ bản ghi không phải "Tài liệu trùng lặp" nếu có cả bản ghi cũ và mới
                if norm_p in results and meta.get("title") == "Tài liệu trùng lặp":
                    continue
                results[norm_p] = {
                    "file_name": Path(norm_p).name,
                    "file_path": norm_p,
                    "is_duplicate": bool(is_dup),
                    "metadata": meta
                }
            return results

    def get_active_hashes(self) -> set[str]:
        """
        Lấy toàn bộ mã băm SHA-256 của các tệp đã xử lý thành công mà tệp đích THỰC SỰ ĐANG TỒN TẠI trên đĩa.
        Nếu tệp vật lý đã bị xóa khỏi đĩa, mã băm đó không được coi là trùng lặp nữa.
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT file_path, file_hash FROM processed_files WHERE status = 'COMPLETED' AND file_hash IS NOT NULL")
            active_hashes = set()
            for fp, fhash in cursor.fetchall():
                try:
                    if Path(fp).exists():
                        active_hashes.add(fhash)
                except Exception:
                    pass
            return active_hashes

    def get_all_hashes(self) -> set[str]:
        """Lấy toàn bộ mã băm SHA-256 của các tệp đang tồn tại trên đĩa."""
        return self.get_active_hashes()

    def get_active_simhashes(self) -> Dict[str, str]:
        """
        Lấy danh sách mã SimHash {file_path: simhash} của các tệp đã xử lý thành công
        mà tệp đích THỰC SỰ ĐANG TỒN TẠI trên đĩa.
        Chỉ lấy các tệp gốc không bị đánh dấu trùng lặp để làm mốc đối chiếu chuẩn.
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT file_path, simhash FROM processed_files "
                "WHERE status = 'COMPLETED' AND simhash IS NOT NULL AND is_duplicate = 0"
            )
            active_simhashes = {}
            for fp, shash in cursor.fetchall():
                try:
                    if Path(fp).exists():
                        active_simhashes[fp] = shash
                except Exception:
                    pass
            return active_simhashes



