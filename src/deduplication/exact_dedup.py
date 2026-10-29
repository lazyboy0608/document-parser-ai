import hashlib
from pathlib import Path
from typing import Dict, Optional, Set, Tuple


class ExactDeduplicator:
    """
    Lọc trùng lặp tuyệt đối (Exact Duplication) dựa trên mã băm MD5 / SHA-256.
    Tốc độ cực nhanh (vài mili-giây/file), giúp loại bỏ ngay các tài liệu copy giống nhau 100%
    trước khi tốn tài nguyên chạy OCR hay gọi LLM.
    Đồng thời lưu vết tên/đường dẫn tệp gốc để báo cáo cho người dùng.
    """
    def __init__(self):
        self.seen_hashes: Set[str] = set()
        self.hash_to_path: Dict[str, str] = {}

    @staticmethod
    def calculate_file_hash(file_path: Path, block_size: int = 65536) -> str:
        """Tính SHA-256 của tệp nhị phân."""
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            for block in iter(lambda: f.read(block_size), b""):
                hasher.update(block)
        return hasher.hexdigest()

    @staticmethod
    def calculate_text_hash(text: str) -> str:
        """Tính MD5 của nội dung văn bản (đã chuẩn hóa khoảng trắng)."""
        normalized = " ".join(text.split()).strip().lower()
        return hashlib.md5(normalized.encode("utf-8")).hexdigest()

    def register_file(self, file_path: Path, file_hash: Optional[str] = None):
        """Đăng ký một tệp gốc vào danh bạ kiểm tra trùng lặp."""
        f_hash = file_hash or self.calculate_file_hash(file_path)
        self.seen_hashes.add(f_hash)
        self.hash_to_path[f_hash] = str(file_path)

    def unregister_file(self, file_path: Path, file_hash: Optional[str] = None):
        """Xóa mã băm của tệp khỏi danh bạ kiểm tra trùng lặp khi tệp bị xóa."""
        fp_str = str(file_path)
        fp_name = file_path.name
        hashes_to_remove = set()
        if file_hash:
            hashes_to_remove.add(file_hash)
        for h, p in list(self.hash_to_path.items()):
            try:
                if p == fp_str or Path(p).resolve() == Path(fp_str).resolve() or Path(p).name == fp_name:
                    hashes_to_remove.add(h)
            except Exception:
                if p == fp_str or Path(p).name == fp_name:
                    hashes_to_remove.add(h)
        for h in hashes_to_remove:
            self.seen_hashes.discard(h)
            self.hash_to_path.pop(h, None)

    def find_duplicate(self, file_path: Path) -> Tuple[bool, Optional[str]]:
        """
        Kiểm tra tệp có trùng khớp SHA-256 với tệp nào trước đó không.
        Returns:
            Tuple[is_duplicate, matched_file_path]
        """
        f_hash = self.calculate_file_hash(file_path)
        if f_hash in self.hash_to_path:
            return True, self.hash_to_path[f_hash]
        if f_hash in self.seen_hashes:
            return True, None
        
        self.seen_hashes.add(f_hash)
        self.hash_to_path[f_hash] = str(file_path)
        return False, None

    def is_duplicate_file(self, file_path: Path) -> bool:
        """Kiểm tra và ghi nhận mã băm của tệp (Tương thích ngược)."""
        is_dup, _ = self.find_duplicate(file_path)
        return is_dup

