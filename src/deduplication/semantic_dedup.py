import re
from typing import List, Set, Optional, Tuple
from datasketch import MinHash, MinHashLSH


def tokenize(text: str, n_gram: int = 3) -> Set[str]:
    """Tách từ/n-gram ký tự tiếng Việt để so khớp tương đồng nội dung."""
    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    words = cleaned.split()
    if len(words) < n_gram:
        return set(words)
    return {"_".join(words[i:i + n_gram]) for i in range(len(words) - n_gram + 1)}


class SemanticDeduplicator:
    """
    Lọc gần trùng lặp (Near-Duplicate Detection) bằng MinHash và LSH (Locality-Sensitive Hashing).
    Độ phức tạp tính toán xấp xỉ O(1) khi tra cứu trong hàng vạn tài liệu.
    """
    def __init__(self, threshold: float = 0.85, num_perm: int = 128):
        self.threshold = threshold
        self.num_perm = num_perm
        self.lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
        self.minhashes = {}

    def _create_minhash(self, text: str) -> MinHash:
        m = MinHash(num_perm=self.num_perm)
        tokens = tokenize(text)
        for t in tokens:
            m.update(t.encode("utf-8"))
        return m

    def check_and_add(self, doc_id: str, text: str) -> Tuple[bool, List[str]]:
        m = self._create_minhash(text)
        duplicates = self.lsh.query(m)
        
        # Lọc chính doc_id nếu đã tồn tại
        matching = [d for d in duplicates if d != doc_id]
        is_dup = len(matching) > 0

        if not is_dup:
            self.lsh.insert(doc_id, m)
            self.minhashes[doc_id] = m

        return is_dup, matching
