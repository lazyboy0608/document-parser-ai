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
            if "duplicate_of" not in columns:
                cursor.execute("ALTER TABLE processed_files ADD COLUMN duplicate_of TEXT;")
            if "duplicate_group_id" not in columns:
                cursor.execute("ALTER TABLE processed_files ADD COLUMN duplicate_group_id TEXT;")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_simhash ON processed_files(simhash);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_file_hash ON processed_files(file_hash);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_duplicate_of ON processed_files(duplicate_of);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_duplicate_group_id ON processed_files(duplicate_group_id);")

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
        duplicate_of: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        simhash: Optional[str] = None,
        duplicate_group_id: Optional[str] = None
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

            # Nếu duplicate_of không truyền vào, kiểm tra trong metadata hoặc giữ lại giá trị cũ
            if duplicate_of is None:
                if metadata and metadata.get("duplicate_of"):
                    duplicate_of = str(metadata["duplicate_of"])
                else:
                    cursor.execute("SELECT duplicate_of FROM processed_files WHERE file_path = ?", (norm_p,))
                    row = cursor.fetchone()
                    if row and row[0]:
                        duplicate_of = row[0]

            # Nếu duplicate_group_id không truyền vào, lấy giá trị cũ hoặc từ metadata
            if duplicate_group_id is None:
                if metadata and metadata.get("duplicate_group_id"):
                    duplicate_group_id = str(metadata["duplicate_group_id"])
                else:
                    cursor.execute("SELECT duplicate_group_id FROM processed_files WHERE file_path = ?", (norm_p,))
                    row = cursor.fetchone()
                    if row and row[0]:
                        duplicate_group_id = row[0]

            if metadata is not None:
                if duplicate_of:
                    metadata["duplicate_of"] = duplicate_of
                if duplicate_group_id:
                    metadata["duplicate_group_id"] = duplicate_group_id

            cursor.execute("""
                INSERT OR REPLACE INTO processed_files (file_path, file_hash, simhash, status, is_duplicate, duplicate_of, duplicate_group_id, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                norm_p,
                file_hash,
                simhash,
                status,
                1 if is_duplicate else 0,
                duplicate_of,
                duplicate_group_id,
                json.dumps(metadata, ensure_ascii=False) if metadata else None
            ))
            conn.commit()

    def update_duplicate_reverse_link(
        self,
        orig_file_identifier: str,
        dup_file_name: str,
        group_id: Optional[str] = None
    ) -> bool:
        """
        Cập nhật ngược (Reverse Link) cho tệp gốc khi có tệp phái sinh / trùng lặp nạp sau.
        - Tìm bản ghi của tệp gốc qua file_path hoặc tên file (Path(file_path).name).
        - Thêm dup_file_name vào danh sách duplicate_variants trong metadata.
        - Cập nhật duplicate_group_id.
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            norm_id = self._norm(orig_file_identifier)
            # Tìm chính xác qua đường dẫn
            cursor.execute("SELECT file_path, duplicate_group_id, metadata_json FROM processed_files WHERE file_path = ?", (norm_id,))
            row = cursor.fetchone()

            # Nếu không tìm thấy, tìm qua tên file (Path(file_path).name)
            if not row:
                target_name = Path(orig_file_identifier).name
                cursor.execute("SELECT file_path, duplicate_group_id, metadata_json FROM processed_files")
                all_rows = cursor.fetchall()
                for fp, gid, mjson in all_rows:
                    if Path(fp).name == target_name:
                        row = (fp, gid, mjson)
                        break

            if not row:
                return False

            orig_path, current_gid, meta_json = row
            meta = json.loads(meta_json) if meta_json else {}

            # Cập nhật danh sách các biến thể trùng lặp
            variants = meta.get("duplicate_variants", [])
            if not isinstance(variants, list):
                variants = []
            if dup_file_name not in variants:
                variants.append(dup_file_name)
            meta["duplicate_variants"] = variants
            meta["has_duplicates"] = True

            # Gán trạng thái rõ ràng
            meta["duplicate_status"] = f"Có bản trùng lặp (với {', '.join(variants)})"

            # Cập nhật Group ID nếu có
            final_gid = group_id or current_gid or meta.get("duplicate_group_id")
            if final_gid:
                meta["duplicate_group_id"] = final_gid

            cursor.execute("""
                UPDATE processed_files
                SET duplicate_group_id = ?, metadata_json = ?
                WHERE file_path = ?
            """, (final_gid, json.dumps(meta, ensure_ascii=False), orig_path))
            conn.commit()
            return True

    @staticmethod
    def _is_volume_accessible(path: Path) -> bool:
        """
        Kiểm tra xem phân vùng lưu trữ hoặc thư mục chứa tệp có đang kết nối/khả dụng không.
        Giúp phân biệt giữa:
        1. Người dùng chủ động xóa tệp (thư mục cha vẫn truy cập bình thường nhưng tệp không còn).
        2. Mạng LAN bị ngắt kết nối (toàn bộ đường dẫn mạng / ổ đĩa chia sẻ không thể truy cập).
        """
        try:
            parent = path.parent
            if parent.exists():
                return True
            # Kiểm tra phân vùng gốc ổ đĩa hoặc điểm chia sẻ UNC (vd: D:\, Z:\, \\server\share)
            root = Path(path.anchor)
            if root and root.exists():
                return True
            return False
        except (OSError, PermissionError):
            return False

    def delete_state(self, file_path: str):
        """Xóa trạng thái của tệp khỏi cơ sở dữ liệu (ví dụ khi tệp được di chuyển sang vị trí mới)."""
        norm_p = self._norm(file_path)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM processed_files WHERE file_path = ? OR file_path = ?", (norm_p, str(file_path)))
            conn.commit()

    def delete_records_by_prefix(self, dir_path: str) -> list[str]:
        """Xóa toàn bộ các bản ghi có đường dẫn nằm trong thư mục dir_path."""
        norm_dir = self._norm(dir_path)
        prefix1 = norm_dir if norm_dir.endswith(("\\", "/")) else norm_dir + "\\"
        prefix2 = norm_dir if norm_dir.endswith(("\\", "/")) else norm_dir + "/"
        prefix_pattern1 = f"{prefix1}%"
        prefix_pattern2 = f"{prefix2}%"

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT file_path FROM processed_files WHERE file_path LIKE ? OR file_path LIKE ? OR file_path = ?",
                (prefix_pattern1, prefix_pattern2, norm_dir)
            )
            rows = cursor.fetchall()
            deleted_paths = [r[0] for r in rows]
            if deleted_paths:
                cursor.execute(
                    "DELETE FROM processed_files WHERE file_path LIKE ? OR file_path LIKE ? OR file_path = ?",
                    (prefix_pattern1, prefix_pattern2, norm_dir)
                )
                conn.commit()
            return deleted_paths

    def get_record(self, file_path: str) -> Optional[Dict[str, Any]]:
        """Lấy thông tin chi tiết một bản ghi theo đường dẫn tệp."""
        norm_p = self._norm(file_path)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT file_path, file_hash, simhash, status, is_duplicate, duplicate_of, duplicate_group_id, metadata_json "
                "FROM processed_files WHERE file_path = ? OR file_path = ?",
                (norm_p, str(file_path))
            )
            row = cursor.fetchone()
            if not row:
                return None
            fp, fhash, shash, st, is_dup, dup_of, dup_gid, mjson = row
            return {
                "file_path": fp,
                "file_hash": fhash,
                "simhash": shash,
                "status": st,
                "is_duplicate": bool(is_dup),
                "duplicate_of": dup_of,
                "duplicate_group_id": dup_gid,
                "metadata": json.loads(mjson) if mjson else {}
            }

    def cleanup_missing_files(self, return_paths: bool = False):
        """
        Dọn dẹp các bản ghi trong SQLite nếu tệp vật lý không còn tồn tại trên đĩa.
        Có cơ chế bảo vệ Network Accessibility Guard: Chỉ xóa khi phân vùng/thư mục chứa tệp
        thực sự khả dụng (tránh xóa nhầm toàn bộ CSDL khi mất mạng LAN hoặc ổ đĩa mạng chưa mount).
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT file_path FROM processed_files")
            rows = cursor.fetchall()
            deleted_paths = []
            for (fp,) in rows:
                try:
                    p = Path(fp)
                    # Nếu phân vùng lưu trữ / mạng LAN không khả dụng, tuyệt đối không xóa bản ghi
                    if not self._is_volume_accessible(p):
                        continue
                    if not p.exists():
                        cursor.execute("DELETE FROM processed_files WHERE file_path = ?", (fp,))
                        deleted_paths.append(fp)
                except Exception:
                    pass
            conn.commit()
            return deleted_paths if return_paths else len(deleted_paths)

    def get_all_completed(self) -> Dict[str, Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT file_path, is_duplicate, duplicate_of, duplicate_group_id, metadata_json FROM processed_files WHERE status = 'COMPLETED'")
            rows = cursor.fetchall()
            results = {}
            for path_str, is_dup, dup_of, dup_gid, meta_json in rows:
                norm_p = self._norm(path_str)
                meta = json.loads(meta_json) if meta_json else {}
                if dup_of and "duplicate_of" not in meta:
                    meta["duplicate_of"] = dup_of
                if dup_gid and "duplicate_group_id" not in meta:
                    meta["duplicate_group_id"] = dup_gid
                # Ưu tiên giữ bản ghi không phải "Tài liệu trùng lặp" nếu có cả bản ghi cũ và mới
                if norm_p in results and meta.get("title") == "Tài liệu trùng lặp":
                    continue
                results[norm_p] = {
                    "file_name": Path(norm_p).name,
                    "file_path": norm_p,
                    "is_duplicate": bool(is_dup),
                    "duplicate_of": dup_of or meta.get("duplicate_of"),
                    "duplicate_group_id": dup_gid or meta.get("duplicate_group_id"),
                    "metadata": meta
                }
            return results

    def get_active_exact_hashes(self) -> Dict[str, str]:
        """
        Lấy ánh xạ {file_hash: file_path} của các tệp đã xử lý thành công (không trùng lặp).
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT file_hash, file_path FROM processed_files WHERE status = 'COMPLETED' AND is_duplicate = 0 AND file_hash IS NOT NULL AND file_hash != ''"
            )
            return {fhash: fp for fhash, fp in cursor.fetchall()}

    def get_active_hashes(self) -> set[str]:
        """
        Lấy toàn bộ mã băm SHA-256 của các tệp đã xử lý thành công.
        Nếu tệp nằm trên ổ đĩa mạng đang tạm thời offline, vẫn bảo toàn mã băm để đối chiếu trùng lặp.
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT file_path, file_hash FROM processed_files WHERE status = 'COMPLETED' AND file_hash IS NOT NULL")
            active_hashes = set()
            for fp, fhash in cursor.fetchall():
                try:
                    p = Path(fp)
                    if p.exists():
                        active_hashes.add(fhash)
                    elif not self._is_volume_accessible(p):
                        # Phân vùng mạng đang tạm thời ngắt kết nối -> vẫn giữ mã băm
                        active_hashes.add(fhash)
                except Exception:
                    pass
            return active_hashes

    def get_all_hashes(self) -> set[str]:
        """Lấy toàn bộ mã băm SHA-256 của các tệp đang tồn tại trên đĩa."""
        return self.get_active_hashes()

    def get_active_simhashes(self) -> Dict[str, str]:
        """
        Lấy danh sách mã SimHash {file_path: simhash} của các tệp đã xử lý thành công.
        Chỉ lấy các tệp gốc không bị đánh dấu trùng lặp để làm mốc đối chiếu chuẩn.
        Nếu tệp nằm trên ổ đĩa mạng đang tạm thời offline, vẫn bảo toàn SimHash.
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
                    p = Path(fp)
                    if p.exists():
                        active_simhashes[fp] = shash
                    elif not self._is_volume_accessible(p):
                        # Phân vùng mạng đang tạm thời ngắt kết nối -> vẫn giữ mã SimHash
                        active_simhashes[fp] = shash
                except Exception:
                    pass
            return active_simhashes



