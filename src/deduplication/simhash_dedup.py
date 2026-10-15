import re
from typing import Dict, List, Optional, Tuple

try:
    from simhash import Simhash
    HAS_SIMHASH_PKG = True
except ImportError:
    HAS_SIMHASH_PKG = False
    import hashlib


def get_character_shingles(text: str, k: int = 4) -> List[str]:
    """
    Sinh các k-shingles (cụm ký tự liên tiếp) để băm nội dung đa ngữ (Việt - Nga - Anh).
    Loại bỏ các tiêu đề phân trang giả lập (--- [Trang X ...] ---), khoảng trắng thừa và ký tự đặc biệt, chuẩn hóa chữ thường.
    Phương pháp này không phụ thuộc vào từ điển từ vựng của từng ngôn ngữ.
    """
    # Loại bỏ các chuỗi đánh dấu kỹ thuật (--- [...] ---) do parser tự động thêm vào
    text_clean = re.sub(r"---\s*\[.*?\]\s*---", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"[\s\W_]+", "", text_clean.lower())
    if len(cleaned) < k:
        return [cleaned] if cleaned else ["empty"]
    return [cleaned[i:i + k] for i in range(len(cleaned) - k + 1)]


class SimHashDeduplicator:
    """
    Bộ lọc trùng lặp nội dung mờ (Near-Duplicate Detection) bằng thuật toán SimHash 64-bit.
    - Chuyển đổi văn bản thành mã băm 16 ký tự Hexadecimal (64-bit).
    - So sánh độ tương đồng bằng Khoảng cách Hamming (Hamming Distance).
    - Ngưỡng mặc định: threshold_bits = 3 (Khoảng cách Hamming <= 3 / 64 bits tương đương độ tương đồng >= 95%).
    """

    def __init__(self, threshold_bits: int = 3, shingle_size: int = 4):
        self.threshold_bits = threshold_bits
        self.shingle_size = shingle_size

    def compute_simhash(self, text: str) -> str:
        """
        Tính toán mã SimHash 64-bit của chuỗi văn bản và trả về chuỗi Hex 16 ký tự.
        """
        shingles = get_character_shingles(text, k=self.shingle_size)
        
        if HAS_SIMHASH_PKG:
            # Sử dụng thư viện simhash chính thức
            val = Simhash(shingles).value
        else:
            # Thuật toán Pure-Python SimHash 64-bit chuẩn (dự phòng độc lập)
            v = [0] * 64
            for s in shingles:
                # 64-bit hash từ MD5
                h = int(hashlib.md5(s.encode("utf-8")).hexdigest()[:16], 16)
                for i in range(64):
                    bit = (h >> i) & 1
                    if bit == 1:
                        v[i] += 1
                    else:
                        v[i] -= 1
            val = 0
            for i in range(64):
                if v[i] > 0:
                    val |= (1 << i)

        # Định dạng chuỗi Hex chuẩn 16 ký tự (64 bit)
        return f"{val:016x}"

    @staticmethod
    def hamming_distance(hash1_hex: str, hash2_hex: str) -> int:
        """
        Tính số lượng bit khác biệt giữa 2 mã SimHash 64-bit (Khoảng cách Hamming).
        Thực hiện qua phép XOR bitwise cấp phần cứng siêu tốc.
        """
        try:
            val1 = int(hash1_hex, 16)
            val2 = int(hash2_hex, 16)
            return bin(val1 ^ val2).count("1")
        except Exception:
            return 64

    @staticmethod
    def similarity_percentage(distance: int, total_bits: int = 64) -> float:
        """
        Quy đổi khoảng cách Hamming sang phần trăm tương đồng nội dung.
        VD: distance=0 -> 100%, distance=1 -> 98.4%, distance=2 -> 96.9%, distance=3 -> 95.3%
        """
        dist = max(0, min(total_bits, distance))
        return round((1.0 - (dist / total_bits)) * 100.0, 1)

    def find_near_duplicate(
        self,
        new_hash_hex: str,
        active_hashes: Dict[str, str],
        threshold_bits: Optional[int] = None
    ) -> Tuple[bool, Optional[str], int]:
        """
        Đối chiếu mã SimHash mới với danh sách mã SimHash lịch sử trong CSDL.
        
        Args:
            new_hash_hex: Mã SimHash hex 16 ký tự của tệp đang xét
            active_hashes: Dictionary {file_path: simhash_hex} của các tệp đã lưu trong CSDL
            threshold_bits: Ngưỡng lệch bit tối đa (mặc định lấy self.threshold_bits)
            
        Returns:
            Tuple[is_duplicate, matched_file_path, min_distance]
        """
        limit = threshold_bits if threshold_bits is not None else self.threshold_bits
        best_match = None
        min_distance = 64

        for file_path, existing_hash in active_hashes.items():
            if not existing_hash or len(existing_hash) != 16:
                continue
            dist = self.hamming_distance(new_hash_hex, existing_hash)
            if dist < min_distance:
                min_distance = dist
                best_match = file_path
                # Nếu khoảng cách là 0 (nội dung giống hệt), dừng sớm
                if dist == 0:
                    break

        is_dup = min_distance <= limit and best_match is not None
        return is_dup, best_match, min_distance
